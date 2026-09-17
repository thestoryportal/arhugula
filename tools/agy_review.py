#!/usr/bin/env python3
"""Run an OAuth-authenticated Antigravity CLI diff review and fail closed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import review_wrapper_common as rw  # C-HE-15/16 shared core

MODEL_ARGUMENT = "Gemini 3.1 Pro (High)"
EXPECTED_MODEL_LABEL = "Gemini 3.1 Pro (High)"
MAX_DIFF_PART_BYTES = 32 * 1024
MAX_REVIEW_SEGMENT_BYTES = 96 * 1024
MAX_REVIEW_RESULT_BYTES = 32 * 1024
#: The two budget constants live in review_wrapper_common (C-HE-16 §3, one home for both
#: channels); re-exported here under their historical names.
TOTAL_REVIEW_TIMEOUT_SECONDS = rw.TOTAL_BUDGET_S  # 1260.0
MAX_AGY_PRINT_TIMEOUT_SECONDS = int(rw.PER_ATTEMPT_TIMEOUT_S)  # 1200
PARENT_TIMEOUT_GRACE_SECONDS = 5
ARTIFACT_COMPLETE_MARKER = "ARTIFACT: COMPLETE"
SEGMENT_COMPLETE_MARKER = "SEGMENT: COMPLETE"
PROVIDER_ENV = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)
VERDICTS = {"VERDICT: APPROVE", "VERDICT: BLOCK"}
MODEL_LABEL_RE = re.compile(r'Propagating selected model override to backend: label="([^"]+)"')
TERMINATION_GRACE_SECONDS = 5.0
GEMINI_PROMPT_VERSION = "gemini-review-v2-json"
CHANNEL = "gemini"
PRODUCER = "gemini_review_wrapper"
#: C-HE-29 (U-HE-43): under `HARNESS_SHADOW_LENS=1` this wrapper is the second reviewer's SHADOW
#: lens — off the blocking path. Its rows carry a distinct producer so the review-loop gate's
#: budget (LOOP_PRODUCERS) and the reservation's per-round outcomes never count them, and the
#: `no_finding` marker `emit_outcome` writes for a clean terminal is the scored-round marker
#: C-HE-29 §2 requires. Producer names are `:`-free identifiers (C-HE-24 §2).
SHADOW_PRODUCER = "gemini-shadow"


def shadow_lens() -> bool:
    return os.environ.get("HARNESS_SHADOW_LENS") == "1"


def producer_name() -> str:
    return SHADOW_PRODUCER if shadow_lens() else PRODUCER


def gemini_config_hash() -> str:
    """The channel configuration a verdict is bound to (C-HE-15 §3 `config_hash`): the pinned
    model label + the per-call print timeout. Shared with codex_review's failover path so both
    compute the identical expected binding."""
    return hashlib.sha256(f"{MODEL_ARGUMENT}|{MAX_AGY_PRINT_TIMEOUT_SECONDS}".encode()).hexdigest()[
        :16
    ]


def gemini_binding(repo: Path, base: str) -> dict[str, str]:
    return rw.compute_binding(
        repo,
        base,
        channel=CHANNEL,
        prompt_version=GEMINI_PROMPT_VERSION,
        config_hash=gemini_config_hash(),
    )


def json_block_instruction(binding: dict[str, str]) -> str:
    return (
        "Immediately BEFORE the completion marker, print ONE fenced ```json block with exactly "
        "these keys: verdict (APPROVE|BLOCK), findings (array of {severity: P1|P2|P3, "
        "location, message}), and these six values copied VERBATIM: "
        + ", ".join(f"{k}={binding[k]}" for k in rw.BINDING_FIELDS)
        + ". No other keys.\n"
    )


class TerminationRequested(BaseException):
    def __init__(self, signum: int) -> None:
        self.exit_code = 128 + signum


def handle_termination_signal(signum: int, _frame: object) -> None:
    raise TerminationRequested(signum)


def terminate_bounded(process: subprocess.Popen[str]) -> None:
    """Terminate the direct child and its original process group without an unbounded wait."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.terminate()
        except (PermissionError, ProcessLookupError):
            pass
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        direct_alive = process.poll() is None
        group_alive = False
        if os.name == "posix":
            try:
                os.killpg(process.pid, 0)
                group_alive = True
            except (PermissionError, ProcessLookupError):
                pass
        if not direct_alive and not group_alive:
            return
        remaining = deadline - time.monotonic()
        if direct_alive:
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(min(0.05, remaining))
    # The leader may exit on TERM while a background descendant ignores it. Escalate
    # the still-live original process group only after the real group grace period.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except (PermissionError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_bounded(
    args: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command with bounded waits and best-effort process-group cleanup."""
    streams = []
    process: subprocess.Popen[str] | None = None
    try:
        stdin_stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        streams.append(stdin_stream)
        stdout_stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        streams.append(stdout_stream)
        stderr_stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        streams.append(stderr_stream)
        if input_text is not None:
            stdin_stream.write(input_text)
            stdin_stream.seek(0)
        deferred_signal: int | None = None

        def defer_termination(signum: int, _frame: object) -> None:
            nonlocal deferred_signal
            deferred_signal = signum

        previous_sigterm_handler = None
        previous_sigint_handler = None
        if os.name == "posix" and threading.current_thread() is threading.main_thread():
            previous_sigterm_handler = signal.signal(signal.SIGTERM, defer_termination)
            previous_sigint_handler = signal.signal(signal.SIGINT, defer_termination)
        popen_error: OSError | None = None
        try:
            try:
                try:
                    process = subprocess.Popen(
                        args,
                        cwd=cwd,
                        stdin=stdin_stream,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                        text=True,
                        errors="replace",
                        env=env,
                        start_new_session=os.name == "posix",
                    )
                except OSError as exc:
                    popen_error = exc
            finally:
                if previous_sigterm_handler is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm_handler)
                if previous_sigint_handler is not None:
                    signal.signal(signal.SIGINT, previous_sigint_handler)
            if deferred_signal is not None:
                raise TerminationRequested(deferred_signal)
            if popen_error is not None:
                return subprocess.CompletedProcess(args, 127, "", str(popen_error))
            assert process is not None
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_bounded(process)
            else:
                # Hooks/reviewers are one-shot commands: a successful leader must not leave a
                # background descendant holding the temporary stdio files open.
                terminate_bounded(process)
        except BaseException:
            if process is not None:
                terminate_bounded(process)
            raise
        stdout_stream.seek(0)
        stderr_stream.seek(0)
        stdout = stdout_stream.read()
        stderr = stderr_stream.read()
        if timed_out:
            detail = f"command timed out after {timeout} seconds"
            stderr = f"{stderr.rstrip()}\n{detail}".lstrip()
            return subprocess.CompletedProcess(args, 124, stdout, stderr)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        for stream in streams:
            stream.close()


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def collect_diff(repo: Path, base_sha: str, head_sha: str) -> bytes:
    """The reviewer's payload is EXACTLY the bound bytes (`rw.bound_diff`, the same call the
    digest hashes) -- committed base_sha..head_sha, never the working tree or untracked files
    (codex round 3: the wrapper's own gate-log append and any WIP would otherwise be reviewed
    under a digest that does not describe them; the loop commits before it reviews). Returned
    as bytes and written to the diff parts as bytes: no decode/re-encode may alter a hunk the
    digest covers (codex round 4)."""
    try:
        raw = rw.bound_diff(repo, base_sha, head_sha)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(err or "git diff failed") from exc
    if not raw.strip():
        raise RuntimeError("review diff is empty")
    return raw


def write_diff_parts(directory: Path, diff: bytes | str) -> list[Path]:
    """Write ordered byte chunks small enough for Antigravity's file viewer. The bytes are the
    bound diff verbatim; a chunk boundary never splits a UTF-8 multi-byte sequence (backs up
    over continuation bytes, at most 3), and a non-UTF-8 byte passes through untouched."""
    encoded = diff.encode("utf-8") if isinstance(diff, str) else diff
    parts: list[Path] = []
    start = 0
    while start < len(encoded):
        end = min(start + MAX_DIFF_PART_BYTES, len(encoded))
        if end < len(encoded):
            back = 0
            while back < 3 and end - back > start and (encoded[end - back] & 0xC0) == 0x80:
                back += 1
            if back and end - back > start and (encoded[end - back] & 0xC0) != 0x80:
                end -= back
        path = directory / f"review-part-{len(parts) + 1:03d}.diff"
        path.write_bytes(encoded[start:end])
        parts.append(path)
        start = end
    return parts


def group_diff_parts(diff_paths: list[Path]) -> list[list[Path]]:
    """Build context-bounded windows with one raw part of boundary overlap."""
    groups: list[list[Path]] = []
    start = 0
    while start < len(diff_paths):
        current: list[Path] = []
        current_bytes = 0
        end = start
        while end < len(diff_paths):
            path = diff_paths[end]
            part_bytes = path.stat().st_size
            if part_bytes > MAX_REVIEW_SEGMENT_BYTES:
                raise ValueError(f"diff part exceeds review segment limit: {path}")
            if current and current_bytes + part_bytes > MAX_REVIEW_SEGMENT_BYTES:
                break
            current.append(path)
            current_bytes += part_bytes
            end += 1
        groups.append(current)
        if end == len(diff_paths):
            break
        start = end - 1 if len(current) > 1 else end
    return groups


def review_prompt(
    repo: Path,
    diff_paths: list[Path],
    *,
    binding: dict[str, str],
    segment_index: int | None = None,
    segment_count: int | None = None,
) -> str:
    numbered_paths = "\n".join(f"{index}. {path}" for index, path in enumerate(diff_paths, start=1))
    if segment_index is None or segment_count is None:
        scope = (
            f"The authoritative workspace root is {repo}. The complete diff is split into "
            f"{len(diff_paths)} ordered parts so every read stays below the file-view limit. Use "
        )
        completion_marker = ARTIFACT_COMPLETE_MARKER
        concatenation_scope = "the complete diff"
        # Only the artifact-complete output carries the schema block (C-HE-15 §4); segment
        # outputs feed the synthesis, which carries it.
        synopsis = json_block_instruction(binding)
    else:
        scope = (
            f"The authoritative workspace root is {repo}. This is review segment "
            f"{segment_index} of {segment_count} for one complete diff. This segment is split into "
            f"{len(diff_paths)} ordered parts so every read stays below the file-view limit. Use "
        )
        completion_marker = SEGMENT_COMPLETE_MARKER
        concatenation_scope = "this assigned review segment"
        synopsis = (
            "Adjacent review segments share one complete 32 KiB raw part so evidence crossing a "
            "window boundary remains visible; treat duplicated boundary text as context and report "
            "each finding once. "
            "Before the completion marker, include a concise SEGMENT SYNOPSIS of changed "
            "contracts, definitions, references, and open questions needed for a later "
            "cross-segment synthesis.\n"
        )
    return (
        "You are an out-of-family code reviewer for an agent-harness monorepo.\n"
        f"{scope}"
        f"exactly {len(diff_paths)} read-only view_file calls, one for each numbered part in "
        f"order. The parts concatenate byte-for-byte into {concatenation_scope}:\n"
        f"{numbered_paths}\n"
        "Do not open surrounding workspace files, grep or search, or "
        "inspect any other path.\n"
        "Review that concrete artifact for real defects: correctness, hook and permission "
        "semantics, contract drift, unsafe state handling, and tests that would stay green if "
        "the change were reverted. Report at most 5 findings, numbered F1..Fn and tagged\n"
        "[P1]/[P2]/[P3] with file:line; no style nits. Do not edit files. Finish immediately "
        "after analyzing that diff.\n"
        "Only report a finding proven entirely by the supplied diff. Do not infer helper semantics "
        "when its definition is absent from the diff. Test-only regression coverage for behavior "
        "outside a scoped delta is valid and need not repeat the implementation change.\n"
        "The --add-dir exposes the absolute diff path inside the Antigravity sandbox; successful "
        "access to this artifact is required before analysis.\n"
        "Do not invoke terminal commands or request command permission. Do not invoke URL, "
        "browser, or MCP tools.\n"
        "Do not read dotfiles, environment files, or user configuration. "
        f"{synopsis}"
        "Only after every numbered part was read completely without truncation, emit the exact "
        "line\n"
        f"{completion_marker}\n"
        "immediately before the verdict. If any part is incomplete or unreadable, omit that "
        "marker and use VERDICT: BLOCK. End with exactly\n"
        "VERDICT: APPROVE or VERDICT: BLOCK as the final non-empty line.\n\n"
        "Apply the current official Codex hook protocol when reviewing hook code:\n"
        "Shell and unified-exec hooks match as Bash; apply_patch matches apply_patch, Edit, "
        "or Write.\n"
        "Those aliases affect matcher selection only.\n"
        'A matcher of "Bash" already covers Shell and unified-exec aliases.\n'
        'Hook payload still reports exactly tool_name: "apply_patch" or "Bash".\n'
        "Both canonical names and documented aliases are valid in matcher regexes; neither "
        "form is a defect.\n"
        "The *** Add File: and *** Update File: markers are Codex apply_patch command syntax, "
        "not Antigravity schema.\n"
        "Bash/apply_patch arguments are in tool_input.command.\n"
        "The provider-free Codex runtime witness is also a local Responses API model double. "
        "At that model wire boundary, an apply_patch custom_tool_call correctly carries the raw "
        "patch string in item.input; Codex then normalizes it into hook tool_input.command. Do "
        "not require the Responses custom-tool input string to be a JSON object.\n"
        "The common cwd is a runtime-supplied common field for the session, not a value inside "
        "agent-supplied tool_input. A successful git rev-parse check mirrors the Claude "
        "pre-commit gate; root comparison is optional hardening, not a Claude-parity requirement "
        "unless a concrete Codex path can replace the runtime session cwd. Do not substitute "
        "Antigravity, IDE, or app-server tool schemas for this Codex hook schema.\n\n"
        "The Antigravity --model display label is intentional and empirically required: the "
        "programmatic-looking slug silently selected Flash on this CLI, while this label selected "
        "Pro. The wrapper independently parses the route log and fails closed unless the effective "
        "backend label is exactly Gemini 3.1 Pro (High).\n\n"
        "The standing operator authorization requires this OAuth review to run headlessly with no "
        "permission gate. The agy invocation therefore deliberately combines --sandbox, plan mode, "
        "and --dangerously-skip-permissions while the reviewer brief forbids tool use. "
        "Do not report "
        "that approved flag combination by itself; report only a concrete sandbox escape or an "
        "actual unauthorized effect reachable through it.\n"
    )


def synthesis_prompt(repo: Path, result_paths: list[Path], *, binding: dict[str, str]) -> str:
    numbered_paths = "\n".join(f"{index}. {path}" for index, path in enumerate(result_paths, 1))
    return (
        "You are the final out-of-family synthesis reviewer for an agent-harness monorepo.\n"
        "Synthesize the completed segment reviews into one final verdict.\n"
        f"The authoritative workspace root is {repo}. Independent Gemini 3.1 Pro review "
        "segments covered every byte of one ordered diff under a fail-closed wrapper. Their "
        f"validated results are in {len(result_paths)} files. Use exactly {len(result_paths)} "
        "read-only view_file calls, one for each numbered result in order:\n"
        f"{numbered_paths}\n"
        "Do not open surrounding workspace files, grep or search, or inspect any other path. "
        "Do not invoke terminal, URL, browser, or MCP tools. Adjacent raw-review windows overlap "
        "by one complete 32 KiB part. Reconcile duplicate findings from that overlap and "
        "compare the segment synopses for cross-segment contract drift, incompatible definitions, "
        "or inconsistent references. Preserve any proven blocking finding. Report at most 5 "
        "findings, numbered F1..Fn and tagged [P1]/[P2]/[P3] with file:line; no style nits. "
        "Only report findings supported by the segment results. If any segment verdict is BLOCK, "
        "the synthesis verdict must also be BLOCK. "
        f"{json_block_instruction(binding)}"
        "After every numbered result was read "
        "completely, emit the exact line\n"
        f"{ARTIFACT_COMPLETE_MARKER}\n"
        "immediately before the verdict. If any result is incomplete or unreadable, omit that "
        "marker and use VERDICT: BLOCK. End with exactly VERDICT: APPROVE or VERDICT: BLOCK as "
        "the final non-empty line.\n"
    )


def verify_effective_model(log_path: Path) -> str | None:
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"could not read Antigravity route log: {exc}"
    labels = MODEL_LABEL_RE.findall(log)
    if not labels:
        return "Antigravity did not report an effective backend model"
    selected = sorted(set(labels))
    if selected != [EXPECTED_MODEL_LABEL]:
        return f"effective model mismatch: expected {EXPECTED_MODEL_LABEL}; observed {selected}"
    return None


def agy_print_timeout(timeout: float) -> str:
    seconds = max(
        1,
        min(MAX_AGY_PRINT_TIMEOUT_SECONDS, int(timeout) - PARENT_TIMEOUT_GRACE_SECONDS),
    )
    return "20m" if seconds == MAX_AGY_PRINT_TIMEOUT_SECONDS else f"{seconds}s"


def agy_command(scratch: str, log_path: Path, prompt: str, *, timeout: float) -> list[str]:
    return [
        "agy",
        "--sandbox",
        "--dangerously-skip-permissions",
        "--new-project",
        "--mode",
        "plan",
        "--model",
        MODEL_ARGUMENT,
        "--add-dir",
        scratch,
        "--log-file",
        str(log_path),
        "--print-timeout",
        agy_print_timeout(timeout),
        "-p",
        prompt,
    ]


def validate_review_output(output: str, marker: str) -> tuple[str | None, str | None]:
    output_lines = [line.strip() for line in output.splitlines() if line.strip()]
    marker_name = "artifact" if marker == ARTIFACT_COMPLETE_MARKER else "segment"
    if marker not in output_lines:
        return f"missing exact {marker_name}-completeness marker", None
    if len(output_lines) < 2 or output_lines[-2] != marker:
        return f"{marker_name}-completeness marker must immediately precede verdict", None
    final_line = output_lines[-1] if output_lines else ""
    if final_line not in VERDICTS:
        return "missing exact final verdict", None
    return None, final_line


#: `--outcome-json PATH`: the wrapper's OWN terminal envelope, written at the single terminal
#: site of each path. The D-C failover (codex_review._run_gemini_failover) consumes THIS, never
#: the raw vendor stdout (codex round 3: a vendor exit-2 path could still print a schema-valid
#: APPROVE block that a raw re-parse would count).
OUTCOME_SINK: Path | None = None


def _sink(outcome: rw.ReviewOutcome) -> None:
    """Exclusive-create only, and never inside the repo: the recipe is auto-allowed under loop
    mode with the path as a positional argument, so an existing file (the gate log, a tracked
    ledger) must be un-overwritable and no stray file may land in the checkout (codex round 5).
    A refused sink is reported and the run's exit code stands; the failover then sees no
    envelope and records the reason itself."""
    if OUTCOME_SINK is None:
        return
    target = OUTCOME_SINK.resolve()
    if target.is_relative_to(rw.REPO.resolve()):
        print(f"agy-review: --outcome-json refused: {target} is inside the repo", file=sys.stderr)
        return
    body = json.dumps(
        {
            "terminal": outcome.terminal,
            "channel": outcome.channel,
            "failure_class": outcome.failure_class,
            "reason": outcome.reason,
            "findings": outcome.findings,
            "binding": outcome.binding,
            "source": outcome.source,
        },
        sort_keys=True,
    )
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        print(
            f"agy-review: --outcome-json refused: {target} exists (never overwritten)",
            file=sys.stderr,
        )
        return
    with os.fdopen(fd, "w") as fh:
        fh.write(body)


def _emit(outcome: rw.ReviewOutcome) -> None:
    """C-HE-24 rows for this invocation's terminal (`producer=gemini_review_wrapper`) + the
    C-HE-25 per-round outcome on the arc's reservation when one exists, THEN the outcome
    envelope. Write-first: a verdict that cannot be recorded does not count (C-HE-15 §1 /
    C-HE-23 §2), so the envelope the failover reads is written only after the rows landed
    (codex round 4)."""
    arc_id, lane_id = rw.env_arc_and_lane()
    written = rw.emit_outcome(
        outcome, producer=producer_name(), arc_id=arc_id, lane_id=lane_id, round_n=None
    )  # the round is minted under the log lock (codex round 7); every terminal yields >= 1 row
    round_n = written[0]["round_n"]
    if os.environ.get("HARNESS_FAILOVER_CHILD") != "1" and not shadow_lens():
        # As a failover CHILD the reservation's C-HE-25 outcome is recorded by the PARENT
        # after it ACCEPTS the envelope (binding-matched): a parent-rejected envelope must
        # not leave the child's verdict standing as the arc's deciding leg while the caller
        # acted on REVIEWER_UNAVAILABLE (codex r17 / merge-gate spec-conformance P2). The
        # gate-log rows above stay write-first regardless -- the VERDICT record is never
        # deferred, only the reservation summary.
        rw.record_round_outcome_if_reserved(
            arc_id,
            round_n,
            channel=CHANNEL,
            terminal=outcome.terminal,
            finding_count=len(outcome.findings),
        )
    _sink(outcome)


def _unavailable(reason: str, *, failure_class: str, binding: dict[str, str] | None) -> int:
    """The single REVIEWER_UNAVAILABLE exit: classified, printed, recorded (C-HE-16 §3)."""
    print(f"agy-review: reviewer unavailable ({failure_class}): {reason}", file=sys.stderr)
    _emit(rw.ReviewOutcome("REVIEWER_UNAVAILABLE", CHANNEL, failure_class, reason, [], binding))
    return 2


def report_process_failure(
    proc: subprocess.CompletedProcess[str], *, binding: dict[str, str] | None = None
) -> int:
    """A non-zero exit is REVIEWER_UNAVAILABLE (never proc.returncode: terminal states are
    exactly the C-HE-16 §3 triple), classified permanent/transient by the C-HE-16 §4 table."""
    output = proc.stdout.strip()
    if output:
        print(output)
    detail = proc.stderr.strip() or f"exit {proc.returncode}"
    cls = rw.classify(CHANNEL, rw.classifier_text(proc.stdout, detail))
    return _unavailable(detail, failure_class=cls, binding=binding)


def _bounded_with_retry(
    cmd_for,
    deadline: float,
    *,
    cwd: Path,
    env: dict[str, str],
    marker: str,
    binding: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """C-HE-16 §3 (v1.2 X2): one bounded re-invocation on a transient failure; timeout 1 =
    min(cap, remaining), timeout 2 = min(cap, remaining - 30). None when the shared deadline
    leaves no room for a stage (whole-review deadline expired). Permanent failures skip the
    retry. An attempt SUCCEEDS only when it would count: marker + verdict line for a segment;
    for the artifact-complete output ALSO the positive schema parse against `binding` (codex
    round 3: a marker-valid but schema-invalid final output must consume the retry)."""
    last: subprocess.CompletedProcess[str] | None = None
    for n in (1, 2):
        remaining = deadline - time.monotonic()
        margin = 0.0 if n == 1 else rw.SECOND_ATTEMPT_MARGIN_S
        timeout = min(rw.PER_ATTEMPT_TIMEOUT_S, remaining - margin)
        if timeout <= PARENT_TIMEOUT_GRACE_SECONDS:
            return last
        proc = run_bounded(cmd_for(timeout), cwd=cwd, timeout=timeout, env=env)
        if proc.returncode == 124 and "timed out" in (proc.stderr or ""):
            # A timed-out attempt is never re-invoked (spec v1.2 X2; codex round 11): only a
            # stub of the shared budget remains, and later segments / synthesis need it.
            return proc
        output = proc.stdout.strip()
        validation_error, verdict_line = validate_review_output(output, marker)
        final = binding is not None and marker == ARTIFACT_COMPLETE_MARKER
        terminal = rw.parse_verdict(CHANNEL, output, binding).terminal if final else None
        parsed = not final or terminal != "REVIEWER_UNAVAILABLE"
        blocks = terminal == "BLOCK" or (not final and verdict_line == "VERDICT: BLOCK")
        if validation_error is None and parsed and (proc.returncode == 0 or blocks):
            # A non-zero exit with a BLOCK (bound block on the final output; VERDICT line on a
            # segment) is a terminal, not a retry: re-invoking could replace blocking findings
            # with an APPROVE (codex rounds 4/7).
            return proc
        if rw.classify(CHANNEL, rw.classifier_text(proc.stdout, proc.stderr)) == "permanent":
            return proc
        last = proc
    return last


def _fatal_binding(exc: subprocess.CalledProcessError) -> int:
    # An unresolvable base is caller-side and human-fixable: permanent, never a traceback.
    err = (
        exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
    )
    return _unavailable(
        f"binding: {' '.join(exc.cmd[:4])} failed: {err.strip()[:300]}",
        failure_class="permanent",
        binding=None,
    )


def _head(repo: Path) -> str:
    return rw._git(repo, "rev-parse", "HEAD")


def _nonzero_exit_verdict(
    proc: subprocess.CompletedProcess[str], binding: dict[str, str], marker: str
) -> bool:
    """A non-zero reviewer exit still BLOCKS when its output is a BLOCK -- the bound fenced
    verdict on the artifact-complete output, the `VERDICT: BLOCK` line on a segment (findings
    are preserved); a parsed APPROVE from a failed process is never counted (gemini round 3,
    codex round 7)."""
    output = proc.stdout.strip()
    if marker == ARTIFACT_COMPLETE_MARKER:
        return rw.parse_verdict(CHANNEL, output, binding).terminal == "BLOCK"
    return validate_review_output(output, marker)[1] == "VERDICT: BLOCK"


def run_review(repo: Path, base: str) -> int:
    try:
        binding = gemini_binding(repo, base)
    except subprocess.CalledProcessError as exc:
        return _fatal_binding(exc)
    try:
        diff = collect_diff(repo, binding["base_sha"], binding["head_sha"])
    except RuntimeError as exc:
        return _unavailable(str(exc), failure_class="transient", binding=binding)

    env = rw.reviewer_env()  # every secret-shaped variable dropped + hooks inert (codex round 6)
    for name in PROVIDER_ENV:
        env.pop(name, None)
    deadline = time.monotonic() + TOTAL_REVIEW_TIMEOUT_SECONDS
    try:
        with tempfile.TemporaryDirectory(prefix="arhugula-agy-review-") as scratch:
            diff_paths = write_diff_parts(Path(scratch), diff)
            groups = group_diff_parts(diff_paths)
            if len(groups) == 1:
                review_specs = [
                    (
                        review_prompt(repo, groups[0], binding=binding),
                        ARTIFACT_COMPLETE_MARKER,
                        Path(scratch) / "route.log",
                    )
                ]
            else:
                review_specs = [
                    (
                        review_prompt(
                            repo,
                            group,
                            binding=binding,
                            segment_index=index,
                            segment_count=len(groups),
                        ),
                        SEGMENT_COMPLETE_MARKER,
                        Path(scratch) / f"route-segment-{index:03d}.log",
                    )
                    for index, group in enumerate(groups, 1)
                ]

            accepted_outputs: list[str] = []
            accepted_verdicts: list[str] = []
            result_paths: list[Path] = []
            for index, (prompt, marker, log_path) in enumerate(review_specs, 1):
                proc = _bounded_with_retry(
                    lambda timeout, p=prompt, lp=log_path: agy_command(
                        scratch, lp, p, timeout=timeout
                    ),
                    deadline,
                    cwd=repo,
                    env=env,
                    marker=marker,
                    binding=binding,
                )
                if proc is None:
                    return _unavailable(
                        "whole-review deadline expired", failure_class="transient", binding=binding
                    )
                if proc.returncode != 0 and not _nonzero_exit_verdict(proc, binding, marker):
                    return report_process_failure(proc, binding=binding)
                model_error = verify_effective_model(log_path)
                if model_error is not None:
                    return _unavailable(model_error, failure_class="permanent", binding=binding)
                output = proc.stdout.strip()
                validation_error, verdict = validate_review_output(output, marker)
                if validation_error is not None:
                    if output:
                        print(output, file=sys.stderr)
                    return _unavailable(
                        validation_error, failure_class="transient", binding=binding
                    )
                assert verdict is not None
                if marker == SEGMENT_COMPLETE_MARKER and len(output.encode("utf-8")) > (
                    MAX_REVIEW_RESULT_BYTES
                ):
                    return _unavailable(
                        "segment review output exceeds synthesis limit",
                        failure_class="transient",
                        binding=binding,
                    )
                accepted_outputs.append(output)
                accepted_verdicts.append(verdict)
                if len(groups) > 1:
                    result_path = Path(scratch) / f"review-result-{index:03d}.txt"
                    covered = ", ".join(path.name for path in groups[index - 1])
                    result_path.write_text(
                        f"SEGMENT {index}/{len(groups)}\nCOVERED PARTS: {covered}\n{output}\n",
                        encoding="utf-8",
                    )
                    result_paths.append(result_path)

            if len(groups) > 1:
                log_path = Path(scratch) / "route-synthesis.log"
                synthesis = synthesis_prompt(repo, result_paths, binding=binding)
                proc = _bounded_with_retry(
                    lambda timeout: agy_command(scratch, log_path, synthesis, timeout=timeout),
                    deadline,
                    cwd=repo,
                    env=env,
                    marker=ARTIFACT_COMPLETE_MARKER,
                    binding=binding,
                )
                if proc is None:
                    return _unavailable(
                        "whole-review deadline expired", failure_class="transient", binding=binding
                    )
                if proc.returncode != 0 and not _nonzero_exit_verdict(
                    proc, binding, ARTIFACT_COMPLETE_MARKER
                ):
                    return report_process_failure(proc, binding=binding)
                model_error = verify_effective_model(log_path)
                if model_error is not None:
                    return _unavailable(model_error, failure_class="permanent", binding=binding)
                output = proc.stdout.strip()
                validation_error, verdict = validate_review_output(output, ARTIFACT_COMPLETE_MARKER)
                if validation_error is not None:
                    if output:
                        print(output, file=sys.stderr)
                    return _unavailable(
                        validation_error, failure_class="transient", binding=binding
                    )
                assert verdict is not None
            else:
                output = accepted_outputs[0]
                verdict = accepted_verdicts[0]
    except OSError as exc:
        return _unavailable(str(exc), failure_class="transient", binding=binding)

    # C-HE-15 §1/§4: the marker + VERDICT line are agy's truncation guard; the verdict COUNTS
    # only on a positive parse of the fenced block against gemini.schema.json with the six
    # binding values byte-equal to this invocation's own (rw.parse_verdict).
    outcome = rw.parse_verdict(CHANNEL, output, binding)
    if outcome.terminal == "REVIEWER_UNAVAILABLE":
        if output:
            print(output, file=sys.stderr)
        return _unavailable(outcome.reason, failure_class="transient", binding=binding)
    current = _head(repo)
    if current != binding["head_sha"]:
        # valid for head_sha, but the caller reads this terminal as "HEAD reviewed" (codex round 10)
        return _unavailable(
            f"HEAD moved during the review ({binding['head_sha'][:12]} -> {current[:12]}); "
            "re-run on the current head",
            failure_class="transient",
            binding=binding,
        )
    if verdict == "VERDICT: BLOCK" and outcome.terminal != "BLOCK":
        print(output, file=sys.stderr)
        return _unavailable(
            "fenced verdict contradicts the VERDICT line",
            failure_class="transient",
            binding=binding,
        )
    if "VERDICT: BLOCK" in accepted_verdicts and outcome.terminal != "BLOCK":
        print(output, file=sys.stderr)
        return _unavailable(
            "synthesis contradicted a blocking segment verdict",
            failure_class="transient",
            binding=binding,
        )
    _emit(outcome)
    print(f"agy-review: effective model: {EXPECTED_MODEL_LABEL}")
    print(output)  # the raw output already carries the findings; stdout still ends VERDICT: X
    if outcome.terminal == "BLOCK":
        print("agy-review: blocking findings require resolution", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    global OUTCOME_SINK
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="main")
    parser.add_argument(
        "--outcome-json",
        default=None,
        help="write this wrapper's own terminal envelope (terminal/failure_class/reason/"
        "findings/binding/source) to PATH -- the D-C failover's input (C-HE-17)",
    )
    args = parser.parse_args()
    OUTCOME_SINK = Path(args.outcome_json) if args.outcome_json else None
    # B-215 admission gate (codex r1 P1 on the gate arc): an UNGATED standalone
    # gemini review would mint loop rounds that poison the guarded wrapper's round
    # count (last > 0 skips the entry preflight). The failover CHILD skips — its
    # parent already admitted the round — marked by the same env the reservation-
    # outcome split keys on; in loop mode a hand-set marker cannot ride the guard's
    # exact-shape allowlist (only HARNESS_ARC_ID/LANE_ID prefixes are stripped).
    # The SHADOW lens (C-HE-29, U-HE-43) is off the blocking path by contract: it is launched
    # after the blocking chain reached its terminal, its rows carry `producer=gemini-shadow`,
    # which the gate's budget and obligations never count, so admission does not apply.
    if os.environ.get("HARNESS_FAILOVER_CHILD") != "1" and not shadow_lens():
        import review_loop_gate as rlg

        decision = rlg.admit(Path.cwd(), args.base, rw.env_arc_and_lane()[0])
        if isinstance(decision, rlg.Inactive):
            print(f"gemini-review: review gate INACTIVE — {decision.reason}", file=sys.stderr)
        elif isinstance(decision, rlg.Refused):
            print(f"review gate: {decision.detail}", file=sys.stderr)
            print(f"  recipe: {decision.recipe}", file=sys.stderr)
            print(f"gemini-review: GATE_REFUSED ({decision.code})", file=sys.stderr)
            return 3
    previous_sigterm = signal.signal(signal.SIGTERM, handle_termination_signal)
    try:
        return run_review(Path.cwd(), args.base)
    except TerminationRequested as exc:
        return exc.exit_code
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
