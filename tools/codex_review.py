#!/usr/bin/env python3
"""Fail-closed wrapper for the Codex review channel (C-HE-18).

Mirrors tools/agy_review.py's hardening: bounded timeout, exit-code-independent
capture, declared-schema parse, permanent/transient classification, and a
REVIEWER_UNAVAILABLE terminal on ANY parse failure. When stdout is inconclusive
it reads the channel's own session artifact (the PR #1386 mode: log frozen at
313 bytes for 130 s after process exit while the real verdict sat only in
~/.codex/sessions/) -- and still requires a positive schema parse from it.

Exit 0 APPROVE / 1 BLOCK / 2 REVIEWER_UNAVAILABLE / 3 GATE_REFUSED. The terminal
line on stderr (`codex-review: <TERMINAL>`) is the completion signal callers
read; the exit code is a convenience, never a verdict (C-HE-15 §1). GATE_REFUSED
(B-215 admission gate) is NOT a review terminal — the review never began: no
C-HE-24 row, no round outcome, C-HE-16 §3's terminal enum untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import review_loop_gate as rlg
import review_wrapper_common as rw
from agy_review import (
    GEMINI_PROMPT_VERSION,
    TerminationRequested,
    gemini_config_hash,
    handle_termination_signal,
    run_bounded,
)

#: Codex's home (`CODEX_HOME` when set -- the child inherits it through reviewer_env, so the
#: config the binding hashes and the session tree the fallback reads must follow it; codex round 9).
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
SESSIONS_DIR = CODEX_HOME / "sessions"
ARTIFACT_LAG_S = 130.0  # measured PR #1386 lag (C-HE-18 §2)
ARTIFACT_POLL_S = 2.0
PROMPT_VERSION = "codex-review-v2-exec"
CHANNEL = "codex"
PRODUCER = "codex_review_wrapper"
MAX_FINDINGS = 8
#: run_bounded's own timeout exit status (agy_review.run_bounded appends the detail to stderr).
_RUN_BOUNDED_TIMEOUT_RC = 124


def review_instructions(binding: dict[str, str], *, nonce: str = "") -> str:
    """The review brief + output contract, run through `codex exec` in a scratch directory that
    holds EXACTLY the bound bytes (`bound.diff`) and the tree at `head_sha` (`tree/`, a
    `git archive` export -- never the live worktree, whose uncommitted edits to touched files
    would otherwise be visible as context; codex round 10). Interface probes re-run at
    execution: `codex review --base` rejects a PROMPT; `codex review "<prompt>"` treats it as a
    review target and, on a real PR-sized diff, answers in its own native findings JSON and
    ignores the output contract (round 3); `codex exec` follows the brief and hands the final
    message back through `-o`."""
    return (
        "You are an out-of-family code reviewer for an agent-harness monorepo. Review EXACTLY "
        f"the committed diff in ./bound.diff (`git diff --binary {binding['base_sha']} "
        f"{binding['head_sha']}`); the full tree at that HEAD is exported read-only under "
        "./tree/ for context -- open nothing outside this directory. Look for real defects: "
        "correctness, hook and permission semantics, contract drift, unsafe state handling, "
        "concurrency, and tests that would stay green if the change were reverted. Report at "
        f"most {MAX_FINDINGS} findings, each with an exact "
        "file:line location and a concrete message; no style nits; only findings proven by the "
        "diff. Do not edit files. When done, print ONE fenced ```json block, and nothing after "
        "it, with exactly these keys: verdict (APPROVE|BLOCK), findings (array of "
        "{severity: P1|P2|P3, location, message}; BLOCK requires at least one finding, APPROVE "
        "requires none), and copy these six values VERBATIM: "
        + ", ".join(f"{k}={binding[k]}" for k in rw.BINDING_FIELDS)
        + ". No other keys. A missing or altered value invalidates the review."
        + (f" (Invocation token {nonce}; do not put it in the JSON.)" if nonce else "")
    )


#: The CLI overrides the channel runs with -- part of the bound configuration (codex round 10).
#: Model and effort are pinned here rather than inherited from ~/.codex/config.toml: out-of-family
#: review runs gpt-5.6-sol at medium reasoning effort (operator direction 2026-09-15, token cost);
#: before the pin, u-he-42 rounds 1-3 ran the config default, gpt-6-astra at high. A 2026-09-15
#: probe on codex-cli 0.154.0 confirmed `codex exec` records exactly these overrides in its
#: session rollout (`"model"`, `"reasoning_effort"`).
CODEX_OVERRIDES = (
    "-c",
    'model="gpt-5.6-sol"',
    "-c",
    'model_reasoning_effort="medium"',
    "-c",
    'preferred_auth_method="chatgpt"',
)


def build_command(workdir: Path, instructions: str, *, output_file: Path) -> list[str]:
    """`codex exec` in a read-only sandbox rooted at the scratch `workdir` (an exported tree,
    not a git checkout: `--skip-git-repo-check`); the final assistant message is ALSO written
    to `output_file` (`-o`), the channel's most reliable output surface (probed 2026-08-19)."""
    return [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        *CODEX_OVERRIDES,
        "-C",
        str(workdir),
        "-o",
        str(output_file),
        instructions,
    ]


def export_bound_tree(repo: Path, head_sha: str, base_sha: str, dest: Path) -> None:
    """Materialise what the reviewer may see: `dest/bound.diff` (rw.bound_diff, the digested
    bytes) and `dest/tree/` = `git archive <head_sha>` -- the committed tree, never the live
    worktree (codex round 10)."""
    (dest / "tree").mkdir(parents=True, exist_ok=True)
    (dest / "bound.diff").write_bytes(rw.bound_diff(repo, base_sha, head_sha))
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", head_sha],
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest / "tree")], input=archive, check=True)


def _config_hash(repo: Path | None = None) -> str:
    """Everything that configures the channel: the user config, the repo-scoped `.codex/config.toml`
    (codex exec reads project config), and the CLI overrides (codex round 10)."""
    h = hashlib.sha256()
    for cfg in (CODEX_HOME / "config.toml", (repo or Path.cwd()) / ".codex" / "config.toml"):
        h.update(cfg.read_bytes() if cfg.exists() else b"")
        h.update(b"\0")
    h.update(" ".join(CODEX_OVERRIDES).encode())
    return h.hexdigest()[:16]


def _binding(repo: Path, base: str) -> dict[str, str]:
    return rw.compute_binding(
        repo, base, channel=CHANNEL, prompt_version=PROMPT_VERSION, config_hash=_config_hash(repo)
    )


def artifact_text(path: Path) -> str:
    """Session artifacts are JSONL envelopes: the assistant text (with its fenced block) sits
    INSIDE string fields, newline-escaped. Only the ASSISTANT's output is collected --
    `agent_message` events, assistant `response_item`s, `task_complete.last_agent_message` --
    never tool output,
    the user brief or the echoed diff (a fenced JSON example in reviewed text would otherwise
    make the channel's own verdict look ambiguous; codex round 10). Envelopes are deserialised
    line by line; a non-JSON line is ignored."""
    out: list[str] = []

    def strings(v: object) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                strings(x)
        elif isinstance(v, list):
            for x in v:
                strings(x)

    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        kind = d.get("type")
        if kind == "event_msg" and payload.get("type") == "agent_message":
            strings(payload.get("message"))
        elif kind == "event_msg" and payload.get("type") == "task_complete":
            strings(payload.get("last_agent_message"))
        elif kind == "response_item" and payload.get("role") == "assistant":
            strings(payload.get("content"))
    return "\n".join(out)


def find_session_artifact(
    head_sha: str, *, started_at: float, now: float, root: Path = SESSIONS_DIR, nonce: str = ""
) -> Path | None:
    """Newest file under root modified after started_at whose content contains head_sha
    (C-HE-18 §2) AND this invocation's nonce when one was issued -- two concurrent reviews of
    one head carry identical bindings, so the nonce is what keeps an invocation from consuming
    its sibling's artifact (codex round 9)."""
    if not root.is_dir():
        return None
    # Whole-second floor: a filesystem may truncate mtime granularity, so an artifact written
    # right after a fractional `started_at` must not fall below it (gemini round 3). No upper
    # bound: the head_sha content check is the binding, and `now` only names the scan time.
    del now
    floor = math.floor(started_at)
    candidates: list[tuple[float, Path]] = []
    for p in root.rglob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= floor:
            candidates.append((mtime, p))
    for _, p in sorted(candidates, key=lambda t: t[0], reverse=True):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if head_sha in text and (not nonce or nonce in text):
            return p
    return None


def _default_invoke(
    repo: Path, instructions: str, binding: dict[str, str]
) -> Callable[[float], rw.Attempt]:
    def invoke(timeout: float) -> rw.Attempt:
        # Subscription auth, never the metered key, no harness secrets, hooks inert
        # (rw.reviewer_env). NOT --ephemeral: C-HE-18 §2's session-artifact fallback needs
        # the persisted rollout. The reviewer is rooted at a scratch export of the BOUND tree.
        env = rw.reviewer_env()
        with tempfile.TemporaryDirectory(prefix="arhugula-codex-review-") as scratch:
            work = Path(scratch) / "review"
            export_bound_tree(repo, binding["head_sha"], binding["base_sha"], work)
            out_file = Path(scratch) / "last-message.md"
            proc = run_bounded(
                build_command(work, instructions, output_file=out_file),
                cwd=work,
                timeout=timeout,
                env=env,
            )
            # The `-o` file is the channel's own final-message surface: read it as stdout when
            # it exists (stdout may be truncated/frozen -- the PR #1386 mode; stderr carries the
            # transcript). The same positive parse applies to whichever surface is used.
            stdout = proc.stdout or ""
            if out_file.exists():
                last = out_file.read_text(errors="replace")
                if last.strip():
                    stdout = last
        timed_out = proc.returncode == _RUN_BOUNDED_TIMEOUT_RC and "timed out" in (
            proc.stderr or ""
        )
        return rw.Attempt(stdout, proc.stderr or "", proc.returncode, timed_out)

    return invoke


def run_codex_review(
    repo: Path,
    base: str,
    *,
    invoke: Callable[[float], rw.Attempt] | None = None,
    clock: Callable[[], float] = time.monotonic,
    nonce: str | None = None,
) -> rw.ReviewOutcome:
    nonce = nonce if nonce is not None else uuid.uuid4().hex
    try:
        binding = _binding(repo, base)
    except subprocess.CalledProcessError as exc:
        # An unresolvable base / detached state is a caller-side, human-fixable condition:
        # REVIEWER_UNAVAILABLE(permanent) with the git error as evidence, never a traceback
        # (gemini failover round 2 on the S1 branch: "an invalid base reference ... will
        # crash the process entirely during the binding phase").
        return rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE",
            CHANNEL,
            "permanent",
            f"binding: {' '.join(exc.cmd[:4])} failed: {(exc.stderr or '').strip()[:300]}",
        )
    instructions = review_instructions(binding, nonce=nonce)
    invoke = invoke or _default_invoke(repo, instructions, binding)
    started_wall = time.time()
    deadline = clock() + rw.TOTAL_BUDGET_S
    wall_deadline = started_wall + rw.TOTAL_BUDGET_S
    source = "stdout"

    def conclusive(text: str) -> bool:
        return rw.parse_verdict(CHANNEL, text, binding).terminal != "REVIEWER_UNAVAILABLE"

    def attempt_with_artifact(timeout: float) -> rw.Attempt:
        nonlocal source
        att = invoke(timeout)
        if conclusive(att.stdout):
            if att.timed_out:
                # The verdict was already on stdout / in the -o file when OUR cap killed the
                # process: it counts (a discarded bound BLOCK would let the failover approve
                # over proven findings -- codex round 6); rc None = not a failed process.
                return rw.Attempt(att.stdout, att.stderr, None, False)
            return att
        if att.timed_out:
            # Killed at the cap: the verdict may already be on stderr (the transcript echo) or
            # in the session artifact (the CLI persists the final message before it finishes
            # streaming). One look each, no wait -- the same positive parse + byte-compare
            # applies; otherwise the timeout stands (codex rounds 5/11).
            if conclusive(att.stderr):
                source = "stderr"
                return rw.Attempt(att.stderr, att.stderr, None, False)
            art = find_session_artifact(
                binding["head_sha"],
                started_at=started_wall,
                now=time.time(),
                root=SESSIONS_DIR,
                nonce=nonce,
            )
            if art is not None:
                text = artifact_text(art)
                if conclusive(text):
                    source = "session-artifact"
                    # rc None: the process was killed by OUR cap; the artifact's verdict is
                    # complete and is not a failed process's approval (codex round 5).
                    return rw.Attempt(text, att.stderr, None, False)
            return att
        # `codex review` echoes its transcript (final message included) on stderr: the same
        # positive parse + byte-compare applies there before the session artifact is consulted.
        if conclusive(att.stderr):
            source = "stderr"
            return rw.Attempt(att.stderr, att.stderr, att.returncode, False)
        # (a timed-out attempt reaches here only when neither stdout, the -o file nor the
        # artifact was conclusive; run_with_retry then records the timeout)
        # stdout inconclusive: wait up to ARTIFACT_LAG_S for the session artifact, never past
        # the shared 1260 s budget (Codex round-1 P2 on the plan).
        end = min(time.time() + ARTIFACT_LAG_S, wall_deadline)
        while True:
            art = find_session_artifact(
                binding["head_sha"],
                started_at=started_wall,
                now=time.time(),
                root=SESSIONS_DIR,
                nonce=nonce,
            )
            if art is not None:
                text = artifact_text(art)
                if conclusive(text):
                    source = "session-artifact"
                    return rw.Attempt(text, att.stderr, att.returncode, False)
            if time.time() >= end:
                break
            time.sleep(min(ARTIFACT_POLL_S, max(0.0, end - time.time())))
        return att

    outcome = rw.run_with_retry(
        attempt_with_artifact, channel=CHANNEL, expected=binding, deadline=deadline, clock=clock
    )
    if outcome.terminal != "REVIEWER_UNAVAILABLE":
        outcome.source = source
        current = _head(repo)
        if current != binding["head_sha"]:
            # The verdict is valid for head_sha and is recorded as such, but the CALLER reads
            # this invocation's terminal as "HEAD is reviewed": a commit that landed during the
            # (up to 1260 s) review must not inherit the verdict (codex round 10).
            return rw.ReviewOutcome(
                "REVIEWER_UNAVAILABLE",
                CHANNEL,
                "transient",
                f"HEAD moved during the review ({binding['head_sha'][:12]} -> {current[:12]}); "
                "re-run on the current head",
                [],
                binding,
            )
    return outcome


def _head(repo: Path) -> str:
    return rw._git(repo, "rev-parse", "HEAD")


def _emit_rows(
    outcome: rw.ReviewOutcome,
    *,
    producer: str = PRODUCER,
    channel: str = CHANNEL,
    round_n: int | None = None,
) -> int:
    arc_id, lane_id = rw.env_arc_and_lane()
    written = rw.emit_outcome(
        outcome, producer=producer, arc_id=arc_id, lane_id=lane_id, round_n=round_n
    )  # round minted under the log lock when None (codex round 7); every terminal yields a row
    n = written[0]["round_n"]
    rw.record_round_outcome_if_reserved(
        arc_id,
        n,
        channel=channel,
        terminal=outcome.terminal,
        finding_count=len(outcome.findings),
    )
    return n


def _gemini_config_hash() -> str:
    """The gemini channel's own config-hash rule (agy_review.gemini_config_hash), so the failover
    computes the identical expected binding the wrapper prompts for."""
    return gemini_config_hash()


#: `just gemini-review` carries its own C-HE-16 §3 budget; this outer cap only fences a wedged
#: `just`/preflight, never the review itself.
FAILOVER_SUBPROCESS_CAP_S = rw.TOTAL_BUDGET_S + 60.0
GEMINI_PRODUCER = "gemini_review_wrapper"


def _read_envelope(path: Path, expected: dict[str, str]) -> rw.ReviewOutcome | None:
    """The gemini wrapper's OWN terminal envelope (`agy_review --outcome-json`): terminal in the
    C-HE-16 §3 triple, findings a list, and -- identical bar -- a terminal verdict's binding
    byte-equal to THIS invocation's expected six values. Anything else -> None (not an
    envelope we can trust)."""
    if not path.exists():
        return None
    try:
        env = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(env, dict) or env.get("terminal") not in rw.TERMINAL_STATES:
        return None
    findings = env.get("findings")
    if not isinstance(findings, list):
        return None
    if env["terminal"] != "REVIEWER_UNAVAILABLE" and env.get("binding") != expected:
        return rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE",
            "gemini",
            "transient",
            f"failover envelope bound to a different invocation: {env.get('binding')!r}",
            [],
            dict(expected),
        )
    return rw.ReviewOutcome(
        env["terminal"],
        "gemini",
        env.get("failure_class"),
        str(env.get("reason") or ""),
        findings,
        dict(expected),
        env.get("source"),
    )


def _run_gemini_failover(repo: Path, base: str, chain_round: int | None = None) -> rw.ReviewOutcome:
    """C-HE-17 §1: on primary REVIEWER_UNAVAILABLE run `just gemini-review` ONCE under the
    IDENTICAL bar. The gemini wrapper (U-HE-06) does its own schema parse and records its own
    rows; this reads back its terminal ENVELOPE (`--outcome-json`), never the raw vendor stdout
    (codex round 3: a vendor exit-2 path may still print a schema-valid APPROVE block) and never
    the exit code. Envelope absent = the wrapper never reached a terminal (recipe preflight
    death, missing `just`, cap): classified from stderr and recorded HERE, the one emitter for
    that case (no global row counting -- gemini round 3 / codex round 3)."""
    try:
        binding = rw.compute_binding(
            repo,
            base,
            channel="gemini",
            prompt_version=GEMINI_PROMPT_VERSION,
            config_hash=_gemini_config_hash(),
        )
    except subprocess.CalledProcessError as exc:
        err = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode(errors="replace")
        )
        outcome = rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE",
            "gemini",
            "permanent",
            f"binding: {' '.join(exc.cmd[:4])} failed: {err.strip()[:300]}",
        )
        _emit_rows(outcome, producer=GEMINI_PRODUCER, channel="gemini", round_n=chain_round)
        return outcome
    with tempfile.TemporaryDirectory(prefix="arhugula-gemini-failover-") as scratch:
        envelope = Path(scratch) / "outcome.json"
        # run_bounded (agy_review): bounded wait + process-GROUP cleanup on timeout, so a
        # timed-out `just` cannot leave the wrapper / agy descendants alive to append gate
        # state after this process recorded the failover unavailable (codex round 4). A missing
        # `just` surfaces as its rc-127 CompletedProcess (gemini round 3), never an exception.
        env = dict(os.environ)  # the gemini wrapper strips its own provider env + hooks
        env["HARNESS_FAILOVER_CHILD"] = "1"  # parent records the reservation outcome (below)
        if chain_round is not None:
            # Failover-chain round identity (codex round-14 P1): the two legs of ONE failover
            # share the primary's round number, in BOTH the gate log (round_n_for honors
            # HARNESS_ROUND_N) and the reservation's composite keys ("N/codex" + "N/gemini"),
            # so fold_round_outcomes pairs the legs of the SAME chain -- never a stale
            # same-number leg from an earlier review.
            env["HARNESS_ROUND_N"] = str(chain_round)
        proc = run_bounded(
            ["just", "gemini-review", base, str(envelope)],
            cwd=repo,
            timeout=FAILOVER_SUBPROCESS_CAP_S,
            env=env,
        )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        outcome = _read_envelope(envelope, binding)
    if outcome is not None:
        if outcome.terminal == "REVIEWER_UNAVAILABLE" and "different invocation" in (
            outcome.reason or ""
        ):
            # REJECTED envelope (binding mismatch -- HEAD moved mid-review): the child's
            # persisted verdict is against a different tree; the parent emits its OWN gate
            # row for the terminal the caller actually acts on, and _emit_rows also records
            # the reservation outcome (codex r18 P2: without this row the gate log carried
            # only the child's rejected verdict and the join broke).
            _emit_rows(outcome, producer=GEMINI_PRODUCER, channel="gemini", round_n=chain_round)
            return outcome
        # ACCEPTED envelope: the child recorded its own GATE-LOG rows (write-first); the
        # reservation's C-HE-25 outcome is recorded HERE, from the envelope the parent
        # accepted (codex r17 / merge-gate spec-conformance P2; the child skips it under
        # HARNESS_FAILOVER_CHILD=1).
        arc_id, _lane = rw.env_arc_and_lane()
        if chain_round is not None:
            rw.record_round_outcome_if_reserved(
                arc_id,
                chain_round,
                channel="gemini",
                terminal=outcome.terminal,
                finding_count=len(outcome.findings),
            )
        return outcome
    outcome = rw.ReviewOutcome(
        "REVIEWER_UNAVAILABLE",
        "gemini",
        rw.classify("gemini", rw.classifier_text(stdout, stderr)),
        "no outcome envelope from the gemini wrapper"
        + (f"; {stderr.strip()[-400:]}" if stderr.strip() else ""),
        [],
        binding,
    )
    _emit_rows(outcome, producer=GEMINI_PRODUCER, channel="gemini", round_n=chain_round)
    return outcome


def _route_to_hitl(arc_id: str, reason: str, *, kind: str = "both-unavailable") -> None:
    """C-HE-20 §1: a REVIEWER_UNAVAILABLE routes to the EXISTING durable HITL queue (loop_lib.sh,
    the C-HE-09 venue; no new escalation store). Three shapes (codex rounds 4/7/9/13):
    `both-unavailable` -> `DEFERRED-HIL` on the ARC (it is blocked); `primary-permanent` ->
    `DEFERRED-HIL` on the CHANNEL item `codex-review-channel` (a stale login / missing binary is
    human-fixable and would otherwise silently demote every future review; the arc itself was
    carried and is NOT marked pending); `primary-transient` -> `NOTIFY` (informational, C-HE-09
    §5). A failed write is reported loudly; the exit code already carries the outcome."""
    lib = rw.REPO / "tools" / "hooks"
    if kind == "both-unavailable":
        call = 'loop_defer "$1" "review-with-failover: REVIEWER_UNAVAILABLE on both channels — $2"'
    elif kind == "primary-permanent":
        call = (
            'loop_defer codex-review-channel "primary channel permanently unavailable; the '
            'failover carried $1 — fix the codex login/binary — $2"'
        )
    else:
        call = (
            'loop_log NOTIFY "review-with-failover: primary REVIEWER_UNAVAILABLE, failover '
            'carried the review — $1: $2"'
        )
    # loop_log swallows a failed append by design (`|| true`); verify the EFFECT -- THIS row,
    # identified by a per-call ref token, is in the ledger -- never the call's status and never
    # an earlier row with the same text (codex rounds 9/10; loop_resolve does the same).
    ref = f"ref {uuid.uuid4().hex[:10]}"
    script = (
        f'. "{lib / "lib.sh"}" && . "{lib / "loop_lib.sh"}" && {call} && '
        'grep -qF -- "$3" "$(loop_status_path)"'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "loop_route", arc_id, f"{reason} [{ref}]", ref],
        cwd=rw.REPO,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(
            f"review-with-failover: HITL row NOT written (rc {proc.returncode}): "
            f"{(proc.stderr or '').strip()[-300:]}",
            file=sys.stderr,
        )


def _report(outcome: rw.ReviewOutcome, *, label: str) -> None:
    for f in outcome.findings:
        print(f"- [{f['severity']}] {f['location']}: {f['message']}")
    tail = f" ({outcome.failure_class}: {outcome.reason})" if outcome.reason else ""
    src = f" [source: {outcome.source}]" if outcome.source not in (None, "stdout") else ""
    print(f"{label}: {outcome.terminal}{tail}{src}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="main")
    p.add_argument(
        "--failover",
        action="store_true",
        help="D-C failover (C-HE-17): on REVIEWER_UNAVAILABLE run `just gemini-review` once "
        "under the identical bar; its verdict blocks",
    )
    p.add_argument(
        "--invoke-test-empty", action="store_true", help=argparse.SUPPRESS
    )  # test seam: zero-byte channel
    args = p.parse_args(argv)
    invoke = (lambda timeout: rw.Attempt("", "", 0, False)) if args.invoke_test_empty else None

    # B-215 admission gate — BEFORE any reviewer subprocess and before round minting.
    # No HARNESS_ROUND_N skip: the failover child is `just gemini-review` (agy_review),
    # which never re-enters this main(), so nothing legitimate needs a bypass here.
    decision = rlg.admit(Path.cwd(), args.base, rw.env_arc_and_lane()[0])
    if isinstance(decision, rlg.Inactive):
        print(f"codex-review: review gate INACTIVE — {decision.reason}", file=sys.stderr)
    elif isinstance(decision, rlg.Refused):
        print(f"review gate: {decision.detail}", file=sys.stderr)
        print(f"  recipe: {decision.recipe}", file=sys.stderr)
        print(f"codex-review: GATE_REFUSED ({decision.code})", file=sys.stderr)
        return 3

    # Mirror agy_review's SIGTERM discipline (U-HE-35 codex r8 P2): run_bounded spawns
    # the vendor CLI in ITS OWN session, so only this process can tear that group down —
    # TerminationRequested unwinds through run_bounded's BaseException arm, which calls
    # terminate_bounded on the vendor group. Without this handler a SIGTERM killed the
    # wrapper by default disposition and orphaned the paid vendor process.
    previous_sigterm = signal.signal(signal.SIGTERM, handle_termination_signal)
    try:
        return _reviewed_main(args, invoke)
    except TerminationRequested as exc:
        return exc.exit_code
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def _reviewed_main(args: argparse.Namespace, invoke) -> int:
    chain: dict[str, int] = {}

    def primary() -> rw.ReviewOutcome:
        outcome = run_codex_review(Path.cwd(), args.base, invoke=invoke)
        # the primary's row lands before the failover runs; rounds are ARC-GLOBAL
        # (round_n_for), so the chain round the failover child is forced to cannot be
        # duplicated by any independent invocation minting after this row lands
        # (codex rounds 15/16/18 P1-P2).
        chain["round"] = _emit_rows(outcome)
        _report(outcome, label="codex-review")
        return outcome

    if not args.failover:
        return rw.exit_code(primary())
    first, fo = rw.run_with_failover(
        primary, lambda: _run_gemini_failover(Path.cwd(), args.base, chain.get("round"))
    )
    if fo is None:
        return rw.exit_code(first)
    # NO second emission for the failover: `just gemini-review` (agy_review, U-HE-06) already
    # wrote its rows and round outcome, or `_run_gemini_failover` did when the recipe never ran.
    _report(fo, label="gemini-review (failover)")
    if fo.terminal == "REVIEWER_UNAVAILABLE":
        both = f"codex: {first.reason}; gemini: {fo.reason}"
        print(f"review-with-failover: BOTH channels unavailable -- {both}", file=sys.stderr)
        _route_to_hitl(rw.env_arc_and_lane()[0], both, kind="both-unavailable")
    else:
        _route_to_hitl(
            rw.env_arc_and_lane()[0],
            f"codex: {first.reason}; gemini: {fo.terminal}",
            kind="primary-permanent" if first.failure_class == "permanent" else "primary-transient",
        )
    return rw.exit_code(fo)


if __name__ == "__main__":
    raise SystemExit(main())
