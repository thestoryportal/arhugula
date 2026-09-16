#!/usr/bin/env python3
"""B-215 admission gate for the review loop: attestations in, one decision out.

Every `just review-with-failover` invocation passes through `admit()` before any
reviewer subprocess starts ([LAW:single-enforcer] — codex_review.main() is the one
seam both channels share). The gate converts the review loop's authoring-time
disciplines from instruction-following into refusals:

- CURRENCY: every admission requires the current bytes (head + diff digest via
  `rw.code_binding`, the same formula the reviewer binding uses,
  [LAW:one-source-of-truth]) to carry a sweep-run attestation — a preflight or a
  sweep bound to exactly this diff. A REVIEWER_UNAVAILABLE round reviewed nothing
  and never satisfies it (codex r2 P1).
- OBLIGATIONS: every finding_id ever recorded for the arc (both loop producers —
  round numbers are per-producer scales, so round arithmetic is incoherent across
  them, codex r2 P1) must be answered by some sweep attestation before the next
  round is admitted; refusals enumerate the unanswered ids.
- TERMINATION (the C-HE-21 §1 v1.5-X5 checkpoint, operator-ratified): review
  invocations spent — distinct (producer, round) pairs — beyond the budget refuse
  until a RECORDED decision continues or holds. Never an auto-stop: continuation is
  unbounded via auditable ask-gated extensions, not env flags
  ([LAW:no-mode-explosion]), and the refusal carries the late-round-productivity
  counter-evidence for the decider.

A refusal is NOT a review terminal: C-HE-16 §3 closes that enum at
{APPROVE, BLOCK, REVIEWER_UNAVAILABLE}, and a refused invocation never begins —
no C-HE-24 row is written, no round outcome is recorded, the wrapper exits 3
behind its own distinct stderr line (`codex-review: GATE_REFUSED (<code>)`).
Since U-HE-49 (C-HE-21 §1 X6b) the logged launch step (`launch` verb, invoked by
`just review-with-failover-logged` before the wrapper process is created) reads
the same decision at the launch seam and mints per-attempt round-log names, so a
refused launch spends no reviewer call and claims no write-once round name.

State lives at `.harness/review_loop_gate_state.json` — local and gitignored,
same class as codex_loop's state: transient loop progress, never a tracked
artifact. Round history is deliberately NOT stored here: it derives from the
C-HE-24 gate log, the same source `round_n_for` mints from. The invariant that
follows ([LAW:one-source-of-truth], tested): losing or corrupting the state file
only TIGHTENS the gate (attestations vanish, rounds do not), so attest verbs may
loudly re-initialize a corrupt file — the safe direction — while `admit()` on a
corrupt file refuses (an unreadable state must never read as clean,
[LAW:no-silent-failure]).

Single-writer: the arc-serial holder discipline (one lane per arc) excludes
concurrent writers of the state file; appends are read-rewrite, not CAS. The same
discipline flow-excludes the admit-to-emission window (two concurrent wrapper
invocations for one arc) AND the admit-to-review-binding window inside one
invocation (HEAD moving between `admit()` and `run_codex_review`'s own
`compute_binding`, or between the primary and the failover child): the loop is
serial within its lane and blocked on its own foreground subprocess, and round
minting itself (`round_n_for`) carries the identical documented residual,
registered to U-HE-19/21 — the gate adds no new window and holds no lock.

Trust boundary (the u-he-33 `_record_detection` precedent, reaffirmed here): the
gate enforces DISCIPLINE against drift, not security against an agent with local
write access to this checkout — such an agent could edit this module, the gate log,
or the guard itself; fabricating attestation state is out of scope beyond the loud
containment refusals above. The `review-attest-budget` verb stays ask-gated so the
sanctioned path to more rounds is operator-visible; the filesystem is not a
sanctioned path.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import stat as stat_module
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import finding_record as fr
import review_wrapper_common as rw

STATE_REL = Path(".harness/review_loop_gate_state.json")
PREFLIGHT_SCRIPT_REL = Path(".claude/skills/defect-class-preflight/scripts/preflight-grep.sh")
#: The skill's class table, consulted at the same seam as the sweep script: the gate
#: never copies the table ([LAW:one-source-of-truth]); it asks the script.
CLASSIFY_SCRIPT_REL = Path(".claude/skills/defect-class-preflight/scripts/refresh-classes.py")
#: A finding no preflight class matches is a NEW-CLASS CANDIDATE, and the skill's own
#: repair loop (SKILL.md "When a reviewer catches what this sweep missed") obliges its
#: author to say what became of it. The obligation executed zero times over ten
#: absorption commits while it lived in prose ([A] §4, charter §1) — so the sweep
#: template names each unmatched finding and attest refuses the file until every one
#: carries an `intake:` line of one of three kinds:
#:   class-extended <class-number> — an existing class row now matches it
#:   new-class <name>              — a class was added that matches it
#:   instance-only <reason>        — a one-off; no class should carry it
#: A repair claim is VERIFIED, not trusted: attest re-classifies with the live table,
#: and a finding still unmatched under a repair claim refuses the attestation.
INTAKE_PREFIX = "intake:"
INTAKE_REPAIR_KINDS = ("class-extended", "new-class")
INTAKE_KINDS = (*INTAKE_REPAIR_KINDS, "instance-only")
#: Producers whose rows are review-LOOP rounds. Merge-gate lenses and detection
#: producers are excluded: lens rounds are per-producer and their numbers collide
#: with loop rounds; detection rows carry round_n=None (same filter round_n_for uses).
LOOP_PRODUCERS = ("codex_review_wrapper", "gemini_review_wrapper")
#: The checkpoint interval — NOT an auto-stopping cap: C-HE-21 §1 (v1.5 X5, the
#: checkpoint carve-out this arc's spec leg landed) forecloses ending review by round
#: count alone and admits refuse-until-recorded-decision with unbounded ask-gated
#: continuation, which is exactly what BUDGET_EXHAUSTED implements. N=10 follows the
#: merge-gate fix-and-re-gate cap (a different loop, but the workspace's one
#: operator-ratified round-scale precedent, 2026-08-01).
DEFAULT_ROUND_BUDGET = 10

_HIT_LABEL = re.compile(r"^\[(.+)\]$", re.MULTILINE)

#: The permission guard's bareword env-prefix grammar (`HARNESS_*=<value>` strip):
#: recipe prefixes render ids literally only inside it (codex u-sr-04 r9 P2).
_RECIPE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

#: WR-10 ([B] a2/F14): the template verbs write this token under every pre-filled
#: obligation; _read_answers refuses any answers file still carrying it, so a
#: label-complete but answer-empty template can never attest.
TEMPLATE_PLACEHOLDER = "TODO(answer)"


class GateError(Exception):
    """Loud gate failure (unreadable state, unrunnable sweep script)."""


# ── typed state ([LAW:parse-dont-validate]: JSON crosses once, here) ─────────


@dataclass(frozen=True)
class PreflightAttestation:
    arc_id: str
    head_sha: str
    diff_digest: str
    hit_labels: tuple[str, ...]
    answers_digest: str
    ts: str


@dataclass(frozen=True)
class SweepAttestation:
    arc_id: str
    head_sha: str
    diff_digest: str  # full code binding, not head alone: a different --base is a different diff
    finding_ids: tuple[str, ...]  # the obligations this sweep answered (any producer, any round)
    answers_digest: str
    ts: str
    # findings no class matched that the author dispositioned as one-offs — the
    # skill's intake escape hatch, recorded so its rate is measurable (a repaired
    # finding needs no record: it matches the table, which is the durable evidence)
    intake_instance_only: tuple[str, ...] = ()


@dataclass(frozen=True)
class BudgetExtension:
    arc_id: str
    extra_rounds: int
    reason: str
    ts: str


@dataclass(frozen=True)
class GateState:
    preflights: tuple[PreflightAttestation, ...] = ()
    sweeps: tuple[SweepAttestation, ...] = ()
    extensions: tuple[BudgetExtension, ...] = ()


_KINDS = {
    "preflight": (PreflightAttestation, "preflights"),
    "sweep": (SweepAttestation, "sweeps"),
    "budget_extension": (BudgetExtension, "extensions"),
}
_TUPLE_FIELDS = ("hit_labels", "finding_ids", "intake_instance_only")


# ── decisions ([LAW:types-are-the-program]: three arms, no boolean folklore) ─


@dataclass(frozen=True)
class Allowed:
    round_n: int


@dataclass(frozen=True)
class Inactive:
    """Gate not in force (unreserved arc, any mode) — allow, loudly."""

    reason: str


@dataclass(frozen=True)
class Refused:
    code: str  # PREFLIGHT_MISSING | PREFLIGHT_STALE | SWEEP_MISSING
    #           | BUDGET_EXHAUSTED | STATE_UNREADABLE
    detail: str
    recipe: str


Decision = Allowed | Inactive | Refused


def state_path(root: Path) -> Path:
    return root / STATE_REL


def _read_state_bytes(path: Path) -> bytes | None:
    """Containment read (the finding_record/merge_door idiom, codex r1 P1 on this arc):
    O_NOFOLLOW + post-open fstat S_ISREG so a pre-planted symlink or special file at the
    state path is a loud refusal, never a follow. None = no file (empty state)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GateError(f"{path} refused (containment): {exc}") from exc
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise GateError(f"{path} is not a regular file -- refused (containment)")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse_state(data: bytes, path: Path) -> GateState:
    try:
        raw = json.loads(data)
        buckets: dict[str, list] = {attr: [] for _, attr in _KINDS.values()}
        for rec in raw["records"]:
            cls, attr = _KINDS[rec["kind"]]
            fields = {k: v for k, v in rec.items() if k != "kind"}
            for tf in _TUPLE_FIELDS:
                if tf in fields:
                    if not isinstance(fields[tf], list) or not all(
                        isinstance(x, str) for x in fields[tf]
                    ):
                        raise ValueError(f"{tf} is not a list of strings")
                    fields[tf] = tuple(fields[tf])
            obj = cls(**fields)
            # dataclasses do not enforce annotations (codex r4 P2): a persisted
            # extra_rounds="2" would parse here and TypeError later inside decide()
            # instead of resolving to STATE_UNREADABLE — validate scalars now
            for f in dataclasses.fields(obj):
                want = int if f.type == "int" else str if f.type == "str" else None
                val = getattr(obj, f.name)
                # bool subclasses int (codex r6 P2): extra_rounds=true must refuse,
                # not extend the budget by one
                if want is not None and (not isinstance(val, want) or isinstance(val, bool)):
                    raise ValueError(f"{f.name} is not {want.__name__}")
            if isinstance(obj, BudgetExtension) and obj.extra_rounds < 1:
                # the tightening invariant lives in the type (codex r5 P2): a persisted
                # non-positive extension would loosen the gate when state is lost
                raise ValueError("extra_rounds must be >= 1")
            buckets[attr].append(obj)
        return GateState(**{attr: tuple(v) for attr, v in buckets.items()})
    except (KeyError, TypeError, ValueError) as exc:
        raise GateError(f"{path} is unreadable as gate state: {exc}") from exc


def load_state(root: Path) -> GateState:
    """Parse the state file to typed attestations; raise GateError on any malformed
    content — the caller decides direction (admit refuses; attest verbs re-init a
    CORRUPT file, but never a containment refusal)."""
    path = state_path(root)
    data = _read_state_bytes(path)
    if data is None:
        return GateState()
    return _parse_state(data, path)


def _append_record(root: Path, kind: str, record: object) -> None:
    """Read-rewrite append. A corrupt existing file is re-initialized LOUDLY —
    licensed by the tightening-direction invariant (module docstring). A containment
    refusal (symlink/special file) is NOT corruption: it propagates as GateError, and
    the publish goes through a same-directory temp + os.replace (replacing a symlink
    path swaps the link itself, never writing through it)."""
    path = state_path(root)
    data = _read_state_bytes(path)  # GateError on symlink/special -- never re-init those
    state = GateState()
    if data is not None:
        try:
            # the TYPED parse is the one validity authority ([LAW:one-source-of-truth]):
            # a JSON-shaped file carrying schema-invalid records is just as corrupt,
            # and preserving its records would leave the state forever unreadable
            state = _parse_state(data, path)
        except GateError:
            print(
                f"review-gate: corrupt {path} — re-init (attestations lost, gate tightens only)",
                file=sys.stderr,
            )
    existing = [
        {"kind": k, **asdict(a)} for k, (_, attr) in _KINDS.items() for a in getattr(state, attr)
    ]
    existing.append({"kind": kind, **asdict(record)})
    path.parent.mkdir(parents=True, exist_ok=True)
    # per-pid temp + exclusive create (codex r2 P1, r5 P2): O_EXCL|O_NOFOLLOW cannot
    # land on any pre-existing entry (a planted link fails loud), and the unique name
    # removes the shared-fixed-tmp hazard between an operator verb and a loop attest.
    # A crash may leak one tmp; the next same-pid run fails loud on it — remove by hand.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError as exc:  # a pre-existing entry (planted link or crash leftover)
        raise GateError(f"{tmp} refused (containment): {exc}") from exc
    try:
        payload = (json.dumps({"records": existing}, indent=1) + "\n").encode()
        view = memoryview(payload)
        while view:  # write-all (codex r3 P2): a permitted short write must not
            view = view[os.write(fd, view) :]  # publish truncated JSON over valid state
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ── pure core ([LAW:effects-at-boundaries]: no IO below this line) ───────────


def _loop_rounds(rows: list[dict], arc_id: str) -> list[dict]:
    return [
        r
        for r in rows
        if r.get("arc_id") == arc_id
        and r.get("producer") in LOOP_PRODUCERS
        and r.get("round_n") is not None
    ]


def unanswered_findings(state: GateState, rows: list[dict], arc_id: str) -> list[str]:
    """Findings are OBLIGATIONS, not round rows (codex r2 P1): round numbers are
    per-producer scales, so a standalone gemini finding minted at its own round 1
    while codex sits at round 5 must still block admission. The unit is the
    finding_id; coverage is a set difference against every sweep ever attested.
    An IN-SCOPE finding row without a finding_id raises GateError (codex r8 P2):
    silently skipping it would let valid-JSON log corruption erase an obligation."""
    all_ids = set()
    for r in _loop_rounds(rows, arc_id):
        if r.get("record_kind") != "finding":
            continue
        if not r.get("finding_id"):
            raise GateError(
                f"gate log finding row for {arc_id} (producer {r.get('producer')}, "
                f"round {r.get('round_n')}) has no finding_id — obligations underivable"
            )
        all_ids.add(r["finding_id"])
    answered: set[str] = set()
    for s in state.sweeps:
        if s.arc_id == arc_id:
            answered.update(s.finding_ids)
    return sorted(all_ids - answered)


def decide(
    state: GateState,
    rows: list[dict],
    *,
    arc_id: str,
    head_sha: str,
    diff_digest: str,
    budget: int = DEFAULT_ROUND_BUDGET,
    lane_id: str = "<lane-id>",
) -> Allowed | Refused:
    # every recipe command carries the arc/lane prefix (codex u-sr-04 r4 P2): the
    # attest/template verbs resolve identity via env_arc_and_lane(), so a bare
    # command in a refusal recipe binds the branch-* fallback arc — an agent
    # following the gate's own recovery text could never clear the reserved arc.
    # Ids render literally only inside the guard's bareword-prefix grammar
    # (r9 P2, bound to the permission-guard `HARNESS_*=` charset): an id the guard
    # could not strip would render a malformed or ask-stranded command, so such an
    # id falls back to the documented placeholder form.
    if _RECIPE_ID.match(arc_id) and _RECIPE_ID.match(lane_id):
        pfx = f"HARNESS_ARC_ID={arc_id} HARNESS_LANE_ID={lane_id} "
    else:
        pfx = "HARNESS_ARC_ID=<arc-id> HARNESS_LANE_ID=<lane-id> "
    scoped = _loop_rounds(rows, arc_id)
    # budget counts review invocations SPENT — distinct (producer, round) pairs, since
    # round numbers are per-producer scales and a max() across them undercounts
    # (codex r2 P1: codex round 5 + three standalone gemini rounds = 8 spent, not 5)
    rounds_spent = len({(r["producer"], r["round_n"]) for r in scoped})
    allowed_total = budget + sum(e.extra_rounds for e in state.extensions if e.arc_id == arc_id)
    if rounds_spent >= allowed_total:
        return Refused(
            code="BUDGET_EXHAUSTED",
            detail=f"{rounds_spent} review rounds spent for {arc_id}; budget {allowed_total}",
            recipe=(
                "round budget spent — this is the register-and-hold point, not a bug to "
                "keep iterating: register the residual findings as a forward item and "
                "defer (`bash tools/04-loop/defer.sh <arc> '<reason>'`); an operator may "
                f"instead extend deliberately via `{pfx}just review-attest-budget` "
                "(ask-gated; the prefix binds the extension to this arc, not the "
                "branch-* fallback). "
                "Weigh the counter-evidence before holding: on INVENTING arcs late "
                "rounds have measured productive (all 8 P1s at round >=10, "
                ".harness/session-audit-2026-08-22-u-he-29.md §4) — extension exists "
                "exactly so that evidence is never silently lost."
            ),
        )
    try:
        unanswered = unanswered_findings(state, rows, arc_id)
    except GateError as exc:
        return Refused(
            code="STATE_UNREADABLE",
            detail=str(exc),
            recipe="inspect the gate log row named above — an obligation cannot be skipped",
        )
    if unanswered:
        return Refused(
            code="SWEEP_MISSING",
            detail="findings without a class-sibling sweep answer: " + ", ".join(unanswered),
            recipe=(
                "classify the miss, grep the diff for class siblings, commit the "
                f"absorption, then labels-before-answers (WR-10): `{pfx}just "
                "review-template-sweep <answers-file>` pre-fills every outstanding "
                "finding_id and hit label; fill each placeholder, then "
                f"`{pfx}just review-attest-sweep <answers-file>`"
            ),
        )
    # currency invariant (codex r2 P1, subsuming the earlier post-APPROVE residual):
    # the CURRENT bytes must carry a sweep-run attestation — a preflight or a sweep
    # bound to exactly this (head, digest). A REVIEWER_UNAVAILABLE round minted no
    # findings and reviewed nothing, so it never satisfies this by itself.
    arc_attestations = [a for a in (*state.preflights, *state.sweeps) if a.arc_id == arc_id]
    if not any(a.head_sha == head_sha and a.diff_digest == diff_digest for a in arc_attestations):
        if not arc_attestations:
            return Refused(
                code="PREFLIGHT_MISSING",
                detail=f"no preflight attestation for {arc_id}",
                recipe=(
                    "run the defect-class-preflight sweep, COMMIT the work, then "
                    f"labels-before-answers AFTER the final commit (WR-10): `{pfx}just "
                    "review-template-preflight <answers-file>` pre-fills every hit "
                    "label; fill each placeholder, then "
                    f"`{pfx}just review-attest-preflight <answers-file>`"
                ),
            )
        return Refused(
            code="PREFLIGHT_STALE",
            detail=(
                f"no attestation covers the reviewable diff "
                f"{head_sha[:12]}/{diff_digest[:12]} — the tree moved since the last "
                "preflight or sweep"
            ),
            recipe=(
                # template-first here too (codex u-sr-04 r4 P2): a moved tree can
                # carry new labels, and attest-first would rediscover them by the
                # exact failed trial WR-10 eliminates
                f"labels-before-answers on the moved tree (WR-10): `{pfx}just "
                "review-template-preflight <fresh-answers-file>` over the new range, "
                "carry forward the still-true answers and fill the rest, then "
                f"`{pfx}just review-attest-preflight <fresh-answers-file>` (every "
                "reviewed byte is attested-swept, always)"
            ),
        )
    return Allowed(round_n=max((r["round_n"] for r in scoped), default=0) + 1)


# ── edge: admit ──────────────────────────────────────────────────────────────


def _reservation_exists(arc_id: str) -> bool:
    try:
        import reservations as rs
    except ImportError as exc:
        # only the SUBSTRATE being absent reads as unreserved (pre-S4b venues); a
        # broken import INSIDE reservations (renamed symbol, missing dependency) is
        # unreadable state and must refuse upstream, not inactivate (codex r8 P1)
        if exc.name == "reservations":
            return False
        raise
    return rs.current(arc_id) is not None


def admit(repo: Path, base: str, arc_id: str) -> Decision:
    # Reservation scope FIRST: Inactive means the gate is not in force for this arc,
    # so an unreadable state file must not refuse an out-of-scope invocation (it broke
    # the whole wrapper battery on a live schema migration before this ordering).
    try:
        reserved = _reservation_exists(arc_id)
    except Exception as exc:
        # Unreadable store REFUSES (codex r4 P1): "cannot tell" must never DISABLE
        # the gate — a corrupt/symlinked reservation generation would otherwise turn
        # an actually-reserved arc's gate off (the couldn't-look-reads-as-clean class).
        return Refused(
            code="STATE_UNREADABLE",
            detail=f"reservation store unreadable for {arc_id}: {exc}",
            recipe=(
                "inspect the reservation store (tools/reservations.py show --arc-id "
                f"{arc_id}); the gate cannot distinguish reserved from unreserved"
            ),
        )
    if not reserved:
        # Unreserved → Inactive in EVERY mode (codex r3 P1): the headless-degradation
        # path is SANCTIONED — roadmap-continue proceeds unreserved when the permission
        # layer refuses the reserve, and the C-HE-25 recording ladder already no-ops
        # there; a loop-mode hard refusal would strand that flow with an unsatisfiable
        # recipe. The forgotten-HARNESS_ARC_ID session this accepts stays visible: its
        # rounds mint under the branch-*/-nolane fallback ids on every C-HE-24 row.
        return Inactive(reason=f"arc {arc_id} unreserved — attestation gate not in force")
    try:
        state = load_state(repo)
    except GateError as exc:
        return Refused(
            code="STATE_UNREADABLE",
            detail=str(exc),
            recipe=(
                "inspect the file; any attest verb re-initializes it loudly (state loss "
                "only tightens the gate — round counts live in the gate log)"
            ),
        )
    try:
        binding = rw.code_binding(repo, base)
    except subprocess.CalledProcessError as exc:
        # An unresolvable base / broken git is a WRAPPER-infrastructure failure, not an
        # admission fact: defer to run_codex_review's own binding path, which classifies
        # it REVIEWER_UNAVAILABLE under the existing terminal contract (codex r1 P2).
        return Inactive(
            reason=f"code binding unavailable ({exc}) — the wrapper's binding path classifies"
        )
    try:
        rows = fr.read_rows()
    except Exception as exc:
        # the gate log is the round/obligation AUTHORITY (codex r7 P2): unreadable
        # must refuse, matching the state-file and reservation-store disciplines —
        # never a raw traceback out of the wrapper
        return Refused(
            code="STATE_UNREADABLE",
            detail=f"gate log unreadable: {exc}",
            recipe="inspect .harness/merge-gate-log.jsonl (or HARNESS_GATE_LOG) — the "
            "gate cannot derive rounds or obligations without it",
        )
    # the lane comes from the same env boundary that named the arc, so refusal
    # recipes render the exact working prefix (a fallback lane renders truthfully
    # as the fallback the verbs would themselves resolve)
    _, lane_id = rw.env_arc_and_lane()
    return decide(
        state,
        rows,
        arc_id=arc_id,
        head_sha=binding["head_sha"],
        diff_digest=binding["diff_digest"],
        lane_id=lane_id,
    )


# ── edge: launch (U-HE-49; C-HE-21 §1 X6b) ───────────────────────────────────

#: A trailing per-attempt suffix on a round-log stem (`r9-a2` → `-a2`).
#: Canonical minted attempt suffix: positive decimal, no leading zero (codex r5 —
#: `-a0`/`-a01` are never minted and must read as foreign, not as attempts).
_ATTEMPT_SUFFIX = re.compile(r"-a[1-9]\d*$")


def attempt_destination(requested: str, existing_names: list[str]) -> str:
    """Mint the per-attempt destination for a requested round-log name.

    [LAW:one-source-of-truth] this function is the one place attempt names come
    from. Every launch attempt gets a fresh name unconditionally — `r9.log` →
    `r9-a1.log`, then `r9-a2.log` while `r9-a1.log` exists —
    [LAW:dataflow-not-control-flow] the name is f(request, prior attempts), never
    "reuse the bare name if free", so a refused or failed attempt can never claim
    the write-once name a retry needs (C-HE-21 §1 X6b; [B] F13: r9's relaunch
    reused `r9.log` → PUBLISH FAILED exit 4). The `r<N>` prefix survives the
    suffix, so arc_metrics.ROUND_ID_RE keys every attempt to its round and
    round_metrics collapses a refused attempt plus its retry into one round.
    """
    parent, _, base = requested.rpartition("/")
    stem, dot, ext = base.rpartition(".")
    if not dot:  # no extension: the whole basename is the stem
        stem, ext = base, ""
    suffix = f".{ext}" if dot else ""
    # idempotent on its own output: a requested `r9-a1.log` mints round 9's NEXT
    # attempt, never a nested `r9-a1-a1.log`
    stem = _ATTEMPT_SUFFIX.sub("", stem)
    attempt_of = re.compile(rf"^{re.escape(stem)}-a([1-9]\d*){re.escape(suffix)}$")
    taken = [int(m.group(1)) for name in existing_names if (m := attempt_of.match(name))]
    out = f"{stem}-a{max(taken, default=0) + 1}{suffix}"
    return f"{parent}/{out}" if parent else out


_O_DIR_NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _attempt_dir_names(repo: Path, components: list[str]) -> list[str]:
    """Enumerate the rounds directory through an O_NOFOLLOW dir-fd walk (the
    round_log_publish idiom, codex r3 P2): every component opens relative to the
    previous component's fd and refuses a symlink at its own openat, so a
    pre-planted link under .harness/tmp cannot route even this read-only listing
    outside the worktree. A missing component is zero prior attempts — the
    publisher creates the directory at first write."""
    fd = os.open(str(repo), _O_DIR_NOFOLLOW)
    try:
        for comp in components:
            try:
                nxt = os.open(comp, _O_DIR_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return []
            except OSError as exc:  # ELOOP (symlink) / ENOTDIR (file) / perms
                raise GateError(
                    f"refused component {comp!r} in the rounds path (containment): {exc}"
                ) from exc
            os.close(fd)
            fd = nxt
        return os.listdir(fd)
    finally:
        os.close(fd)


def launch(repo: Path, base: str, arc_id: str, requested: str) -> int:
    """Pre-launch admission: evaluate the SAME `admit()` the wrapper enforces,
    BEFORE any reviewer subprocess exists — a launch the gate would refuse is not
    made ([B] F13: r11 launched into BUDGET_EXHAUSTED, r9 into SWEEP_MISSING), so
    a refusal here spends no reviewer call, writes no file, and consumes no round
    identity. [LAW:single-enforcer] the wrapper's own `admit()` stays the
    enforcer of record; this edge reads the same decision at the launch seam,
    where refusing still costs nothing. On admission the per-attempt destination
    goes to stdout (the recipe's dataflow seam); all status goes to stderr.
    """
    decision = admit(repo, base, arc_id)
    if isinstance(decision, Refused):
        print(f"review-launch: {decision.detail}\n  recipe: {decision.recipe}", file=sys.stderr)
        print(
            f"review-launch: GATE_REFUSED ({decision.code}) — launch not made; "
            "no reviewer call, no round log written",
            file=sys.stderr,
        )
        return 3
    if isinstance(decision, Inactive):
        print(f"review-launch: gate INACTIVE — {decision.reason}", file=sys.stderr)
    else:
        print(f"review-launch: ALLOWED (next round {decision.round_n})", file=sys.stderr)
    # Round identity in the requested NAME must be the primary channel's next round
    # (codex r1 P2): after a recorded round 1, a caller re-requesting `r1.log` would
    # mint r1-a2 while the wrapper records round 2 — two review transcripts claiming
    # round 1, which arc_metrics refuses. [LAW:one-source-of-truth] the round the
    # wrapper will record is `round_n_for` (env-forced or per-producer mint) and the
    # name parser is arc_metrics.ROUND_ID_RE — both read here, neither re-derived; a
    # genuinely refused attempt minted no row, so its retry re-passes the same check.
    import arc_metrics as am

    match = am.ROUND_ID_RE.match(Path(requested).stem)
    if not match:
        print(
            f"review-launch: cannot parse a round id from {Path(requested).name!r} — "
            "round-log names carry round identity (r<N>.log; arc_metrics refuses others)",
            file=sys.stderr,
        )
        print(
            "review-launch: GATE_REFUSED (ROUND_NAME_UNPARSEABLE) — launch not made",
            file=sys.stderr,
        )
        return 3
    try:
        expected = rw.next_round_from_rows(arc_id, LOOP_PRODUCERS[0])
    except Exception as exc:
        print(
            f"review-launch: gate log unreadable ({exc}) — cannot bind a round identity",
            file=sys.stderr,
        )
        print("review-launch: GATE_REFUSED (STATE_UNREADABLE) — launch not made", file=sys.stderr)
        return 3
    # A leaked HARNESS_ROUND_N would force the WRAPPER (which honors it) onto a
    # round that already has a primary outcome — a duplicate transcript
    # arc_metrics must refuse (codex r5). The forcing var belongs to the failover
    # child only; refuse any forced value that is not the next unused round.
    forced = os.environ.get("HARNESS_ROUND_N")
    if forced is not None and forced != str(expected):
        print(
            f"review-launch: HARNESS_ROUND_N={forced!r} would force the wrapper to a "
            f"round that is not the next unused primary round ({expected}) — unset it "
            "(the forcing var is for the failover child only)",
            file=sys.stderr,
        )
        print("review-launch: GATE_REFUSED (FORCED_ROUND_STALE) — launch not made", file=sys.stderr)
        return 3
    if Path(requested).name != f"r{expected}.log":
        # canonical-BASENAME equality, not just parsed-number equality (codex
        # r5/r6): aliases like `r01.log` / `round-1.log` / `r1-notes.log` parse
        # to the right number but would mint a SECOND attempt family for one
        # round, and `r1` / `r1.txt` would publish evidence the documented
        # `r*.log` round-log glob omits
        print(
            f"review-launch: requested {Path(requested).name!r} is not the canonical "
            f"name for this launch — the next primary round is {expected}; "
            f"request r{expected}.log (a refused attempt never advances the round)",
            file=sys.stderr,
        )
        print(
            "review-launch: GATE_REFUSED (ROUND_NAME_MISMATCH) — launch not made", file=sys.stderr
        )
        return 3
    # Destination containment BEFORE the paid call (codex r3 P2): the recipe is
    # guard-auto-allowed, so its one caller-derived filesystem input gets the same
    # discipline as the attest verbs' answers file (codex r4 P2 precedent above) —
    # a pre-planted symlink under .harness/tmp must not route even a read-only
    # listing outside the worktree, and a destination the publisher would refuse
    # must refuse HERE, before the reviewer call is spent, not after. The shape
    # rule and the O_NOFOLLOW dir-fd walk are form mirrors of
    # tools/round_log_publish.py — the publisher stays the write-time authority.
    parts = requested.split("/")
    if (
        requested.startswith("/")
        or ".." in parts
        or "" in parts
        or parts[:2] != [".harness", "tmp"]
        or len(parts) < 3
    ):
        print(
            f"review-launch: refused destination {requested!r} — must be a relative "
            "path under .harness/tmp/ (the publisher's own policy, mirrored pre-launch)",
            file=sys.stderr,
        )
        print("review-launch: GATE_REFUSED (DEST_REFUSED) — launch not made", file=sys.stderr)
        return 3
    try:
        existing = _attempt_dir_names(repo, parts[:-1])
    except GateError as exc:
        print(f"review-launch: {exc}", file=sys.stderr)
        print("review-launch: GATE_REFUSED (DEST_REFUSED) — launch not made", file=sys.stderr)
        return 3
    print(attempt_destination(requested, existing))
    return 0


# ── edge: attest verbs ───────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_sweep_script(repo: Path, diff_range: str) -> tuple[str, ...]:
    """Run the textual defect-class sweep over EXACTLY the attested bytes — the
    committed `<base>..<head>` range the binding digest covers, never the working
    tree (empty right after the commit the attestation requires). Return hit
    labels. GateError when the sweep could not run — 'couldn't look' must never
    attest as looked-and-clean."""
    script = repo / PREFLIGHT_SCRIPT_REL
    proc = subprocess.run(
        ["bash", str(script), diff_range], cwd=repo, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise GateError(
            f"preflight sweep did not run (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return tuple(_HIT_LABEL.findall(proc.stdout))


def _classify_findings(
    repo: Path, rows: list[dict], ids: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    """Every outstanding finding's preflight classes, from the skill's class table AS
    COMMITTED AT HEAD (`git show HEAD:<script>`, run as `classify`): the attestation
    binds the committed base..HEAD bytes, so the table that decides 'matched' must be
    the committed one — an uncommitted row could otherwise attest a repair that was
    never landed (codex r1 P2). Same seam as `_run_sweep_script`: the skill owns the
    table, the gate asks. Empty tuple = unmatched = new-class candidate. A script
    that cannot be read or run, or an answer that is not {asked id: [non-empty
    class names]}, is GateError — 'couldn't classify' must never read as 'nothing
    matched' ([LAW:no-silent-failure]; shape check per codex r1 P3)."""
    shown = subprocess.run(
        ["git", "show", f"HEAD:{CLASSIFY_SCRIPT_REL.as_posix()}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        raise GateError(
            f"finding classifier {CLASSIFY_SCRIPT_REL} is not committed at HEAD: "
            f"{shown.stderr.strip()}"
        )
    by_id = {r["finding_id"]: r for r in rows if r.get("finding_id") in ids}
    payload = [
        {
            "finding_id": fid,
            "observed_evidence": by_id[fid].get("observed_evidence") or "",
            "location": by_id[fid].get("location") or "",
        }
        for fid in ids
    ]
    proc = subprocess.run(
        [sys.executable, "-c", shown.stdout, "classify"],
        cwd=repo,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GateError(
            f"finding classifier did not run (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"finding classifier returned an unusable answer: {exc}") from exc
    if not isinstance(out, dict):
        raise GateError("finding classifier returned an unusable answer: not an object")
    result: dict[str, tuple[str, ...]] = {}
    for fid in ids:
        names = out.get(fid)
        if not isinstance(names, list) or not all(isinstance(c, str) and c.strip() for c in names):
            raise GateError(
                f"finding classifier returned an unusable answer for {fid}: expected a "
                f"list of non-empty class names, got {names!r}"
            )
        result[fid] = tuple(names)
    return result


@dataclass(frozen=True)
class Intake:
    """One unmatched finding's authored disposition, parsed once at attest
    ([LAW:parse-dont-validate]): kind is one of INTAKE_KINDS, detail is non-empty."""

    finding_id: str
    kind: str
    detail: str


#: A finding heading: the template's `- finding <id>` line, or its hand-authored
#: `finding <id>` shape — the same label word _has_residue strips. Only a heading owns
#: the lines below it; a cross-reference to an id inside another finding's answer is
#: not a heading (codex r1 P1: two unmatched findings, one citing the other, let the
#: second inherit the first's intake line and attest with no disposition of its own).
_FINDING_HEADING = re.compile(r"^\s*[-*]?\s*finding\s+(\S+)", re.I)


def _parse_intakes(
    ids: tuple[str, ...], unmatched: tuple[str, ...], answers: str
) -> tuple[Intake, ...]:
    """The `intake:` line each unmatched finding owes, attributed to the nearest
    preceding finding heading. Missing, duplicated, orphaned, or malformed →
    GateError naming the finding — an unmatched reviewer finding with no stated
    disposition is exactly the silent non-execution this venue exists to stop."""
    found: dict[str, list[str]] = {n: [] for n in ids}
    owner: str | None = None
    for ln in answers.splitlines():
        m = _FINDING_HEADING.match(ln)
        if m and m.group(1) in found:
            owner = m.group(1)
            continue
        fs = ln.lstrip()
        if not fs.startswith(INTAKE_PREFIX):
            continue
        if owner is None:
            raise GateError(
                f"`{fs}` sits under no finding heading — an intake line belongs directly "
                "below its `- finding <id>` line"
            )
        found[owner].append(fs[len(INTAKE_PREFIX) :].strip())
    out = []
    for n in unmatched:
        if len(found[n]) != 1:
            raise GateError(
                f"finding {n} matches no preflight class and its section carries "
                f"{len(found[n])} `{INTAKE_PREFIX}` lines (exactly one required: "
                f"`{INTAKE_PREFIX} {' | '.join(INTAKE_KINDS)} <detail>`)"
            )
        (line,) = found[n]
        kind, _, detail = line.partition(" ")
        detail = detail.strip()
        if kind not in INTAKE_KINDS or not detail or TEMPLATE_PLACEHOLDER in detail:
            raise GateError(
                f"finding {n}: `{INTAKE_PREFIX} {line}` is not one of "
                f"{', '.join(INTAKE_KINDS)} followed by a detail"
            )
        out.append(Intake(finding_id=n, kind=kind, detail=detail))
    return tuple(out)


def _read_answers(path: Path) -> str:
    # same containment read as the state file (codex r4 P2): the attest verbs are
    # guard-auto-allowed, so their one file INPUT gets the same O_NOFOLLOW +
    # regular-file discipline — an in-worktree symlink must not smuggle an outside
    # file through an approved invocation
    data = _read_state_bytes(path)
    if data is None:
        raise GateError(f"answers file {path} does not exist")
    text = data.decode(errors="replace")
    if not text.strip():
        raise GateError(f"answers file {path} is empty")
    if TEMPLATE_PLACEHOLDER in text:
        # [LAW:single-enforcer] WR-10: the template verbs pre-fill every hit label,
        # which would otherwise let an UNEDITED template attest (labels present,
        # answers absent). Attest stays the one checkpoint: a file still carrying
        # the placeholder token has sections nobody answered — refuse it here, the
        # same seam both attest verbs already cross.
        raise GateError(
            f"answers file {path} still carries {TEMPLATE_PLACEHOLDER!r} — "
            "replace every placeholder with a named answer before attesting"
        )
    return text


def _has_residue(line: str, *tokens: str) -> bool:
    """Author-supplied answer content on the line: non-whitespace beyond the named
    obligation tokens, the placeholder, and bullet/heading punctuation. This is what
    makes 'answered' mean answered (codex u-sr-04 r2 P1): the template writes every
    label and finding id itself, so mere token presence stopped evidencing that an
    author ever looked — a deletion-only edit of the template must not attest."""
    s = line
    # longest token first (codex u-sr-04 r8 P1): replacing `...:1` before `...:10`
    # leaves a stray `0` that reads as residue, answering the longer id's bare line
    for t in sorted((*tokens, TEMPLATE_PLACEHOLDER), key=len, reverse=True):
        s = s.replace(t, " ")
    # the 'finding' label word strips case-insensitively (codex u-sr-04 r6 P2: a
    # capitalized 'Finding <id>' heading with no disposition must not read as an
    # authored answer); obligation tokens above stay case-exact. Quote/backtick
    # marks are punctuation too (r9 P2: an empty `- ""` or code span is not a
    # disposition).
    s = re.sub(r"(?i)\bfinding\b", " ", s)
    return bool(s.strip(" \t-—:*[]().,#\"'`<>"))


def _unanswered_labels(labels: tuple[str, ...], answers: str) -> list[str]:
    # a label is ANSWERED in either authored shape: inline ("label: the answer" —
    # residue on the label's own line), or template-heading style ("[label]" followed,
    # before the next heading, by a content line). Labels are fixed multi-word strings
    # from the sweep script, so plain substring locates them exactly. Residue strips
    # EVERY known label, not just the one under check (codex u-sr-04 r7 P2: a line
    # naming two labels must not let each count as the other's answer).
    lines = answers.splitlines()
    out = []
    for label in labels:
        answered = False
        for i, ln in enumerate(lines):
            if label not in ln:
                continue
            if _has_residue(ln, *labels):
                answered = True
                break
            # heading line: scan its section for a content line (comment lines are
            # tool/template chrome, never an answer)
            for follower in lines[i + 1 :]:
                if follower.lstrip().startswith("["):
                    break
                if not follower.lstrip().startswith("#") and _has_residue(follower):
                    answered = True
                    break
            if answered:
                break
        if not answered:
            out.append(label)
    return out


def _unanswered_ids(ids: tuple[str, ...], answers: str) -> list[str]:
    # token-exact, not substring (codex r2 P2: an answer naming ...:10 must not
    # satisfy the sibling ...:1) — ids are whitespace/punctuation-delimited tokens.
    # The id's line must carry residue beyond EVERY known id (codex u-sr-04 r2 P1 +
    # r7 P2), OR — the pre-template multiline hand shape (r9 P2) — its section must:
    # an id heading followed by the answer on later lines stays valid, the section
    # ending at the next bracket heading or another id's line.
    lines = answers.splitlines()

    def line_ids(ln: str) -> set[str]:
        toks = set(re.split(r"[\s,;()\[\]{}'\"`]+", ln))
        return {n for n in ids if n in toks}

    out = []
    for n in ids:
        answered = False
        for i, ln in enumerate(lines):
            if n not in line_ids(ln):
                continue
            if _has_residue(ln, *ids):
                answered = True
                break
            for follower in lines[i + 1 :]:
                fs = follower.lstrip()
                if fs.startswith("[") or (line_ids(follower) - {n}):
                    break
                # an `intake:` line is the finding's CLASS disposition, parsed by
                # _parse_intakes — never its fix answer, or the intake slot alone
                # would attest an unfixed finding
                if not fs.startswith(("#", INTAKE_PREFIX)) and _has_residue(follower, *ids):
                    answered = True
                    break
            if answered:
                break
        if not answered:
            out.append(n)
    return out


#: The template's provenance stamp: attest parses it back and refuses a mismatch
#: with the freshly recomputed binding (codex u-sr-04 r5 P2: without the check, a
#: template filled against old bytes could attest a moved tree whenever the new
#: label set happened to be equal or smaller). One format, written and parsed here.
_BINDING_STAMP = re.compile(r"^# binding: (\S+) (\S+)\s*$", re.MULTILINE)


def _template_text(
    arc_id: str,
    kind: str,
    diff_range: str,
    diff_digest: str,
    labels: tuple[str, ...],
    finding_ids: tuple[str, ...],
    classes: dict[str, tuple[str, ...]],
) -> str:
    """Answers-template body: every obligation the matching attest verb will enforce,
    pre-filled so authoring happens AGAINST the label set instead of guessing at it
    (WR-10, [B] F14: three attest-by-trial failures). Pure text from values —
    [LAW:dataflow-not-control-flow]: preflight vs sweep differ only in the obligation
    tuples flowing in, never in which operations run. The header deliberately never
    spells the placeholder token: attest refuses the whole FILE on it, so only
    fillable section bodies may carry it."""
    lines = [
        f"# {arc_id} {kind} — named answers",
        f"# binding: {diff_range} {diff_digest}",
        "# Replace every placeholder line with the named answer (line number +",
        "# concrete disposition) — attestation refuses an unfilled file.",
        "",
    ]
    for fid in finding_ids:
        # the id stands whitespace-delimited: _unanswered_ids is token-exact, and a
        # trailing colon would glue into the token and fail its own attest. The class
        # stamp is comment chrome (never residue); an unmatched finding additionally
        # owes an intake line, pre-filled so attest's refusal names a slot the author
        # saw rather than a rule they had to recall.
        stamp = ", ".join(classes[fid]) or "UNMATCHED — no preflight class covers this finding"
        lines += [f"# classes: {stamp}", f"- finding {fid} — {TEMPLATE_PLACEHOLDER}"]
        if not classes[fid]:
            lines += [
                f"  {INTAKE_PREFIX} {TEMPLATE_PLACEHOLDER}  "
                f"({' | '.join(INTAKE_KINDS)} <detail>; a repair claim is re-checked "
                "against the live class table)"
            ]
        lines += [""]
    for label in labels:
        lines += [f"[{label}]", f"- {TEMPLATE_PLACEHOLDER}", ""]
    if not finding_ids and not labels:
        # comment chrome only (codex u-sr-04 r5 P2): non-comment prose here would
        # survive a placeholder-line deletion and read as authored content
        lines += [
            "# No sweep hits over the attested range. Name the diff-level answers",
            "# you still owe (classes walked; new-consumer inventory if any).",
            f"- {TEMPLATE_PLACEHOLDER}",
            "",
        ]
    return "\n".join(lines)


#: The one namespace the template verbs may create files in: the gitignored scratch
#: home (`.harness/tmp/...`, where round logs already live). The permission guard
#: validates only the command's FORM, so the auto-allowed verbs must refuse every
#: other destination here — a template landing in `design-substrate/` would ride an
#: allowlisted invocation past the ask gate that venue's edits normally face (codex
#: u-sr-04 r1 P2), and `.harness/` itself is full of runtime-state authorities an
#: exclusive create could squat while absent (`review_loop_gate_state.json`, r7 P2).
#: Scratch-homing also keeps attestation artifacts out of the stop-gate's tree-dirty
#: view ([B] F6, the WR-12 pollution class).
TEMPLATE_ANCHOR = (".harness", "tmp")


def _template_dir_fd(repo: Path, rel_parts: tuple[str, ...]) -> int:
    """A descriptor for the template's parent directory, refusing a symlink at ANY
    component — the open_scratch_dir shape from merge_gate_log (codex u-sr-04 r1 P1:
    the resolve-then-reopen version was check-then-act; a rename between the resolve
    and the by-pathname open could route the create outside the worktree, since
    O_NOFOLLOW protects only the leaf). Each component opens O_DIRECTORY|O_NOFOLLOW
    relative to the previous descriptor, so check and capture are one syscall at
    every level. Caller owns the fd."""
    try:
        fd = os.open(str(repo), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise GateError(f"{repo} is not a usable worktree root: {exc}") from exc
    for part in rel_parts:
        # provision descriptor-relative (missing .harness/tmp/<arc>-rounds dirs are
        # legitimate); best-effort on purpose — the open below is the single
        # authority on whether this component is usable
        with contextlib.suppress(OSError):
            os.mkdir(part, 0o755, dir_fd=fd)
        try:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as exc:
            os.close(fd)
            raise GateError(f"template destination component {part!r} refused: {exc}") from exc
        os.close(fd)
        fd = nxt
    return fd


def _write_template(repo: Path, answers_path: Path, text: str) -> Path:
    """Publish the answers template under the answers namespace, exclusively.

    Containment (the template verbs are guard-auto-allowed, so their one file OUTPUT
    gets at least the discipline _read_answers gives the input): the destination must
    be a `..`-free path under TEMPLATE_ANCHOR, reached by a dir-fd walk, written as a
    same-directory temp with every byte accounted for, and published by os.link — the
    exclusive-create CAS, so an existing file (hand-authored answers included) refuses
    and a crashed writer leaves only a harmless `.tmp` ([LAW:no-silent-failure]:
    a partial template must never be readable at the final name — a header-only
    fragment would satisfy _read_answers and attest nothing, codex u-sr-04 r1 P2)."""
    rel = answers_path
    if rel.is_absolute():
        try:
            rel = rel.relative_to(repo)
        except ValueError as exc:
            raise GateError(f"template destination {answers_path} is outside the worktree") from exc
    anchor = "/".join(TEMPLATE_ANCHOR)
    if (
        ".." in rel.parts
        or rel.parts[: len(TEMPLATE_ANCHOR)] != TEMPLATE_ANCHOR
        or (len(rel.parts) < len(TEMPLATE_ANCHOR) + 1)
    ):
        raise GateError(
            f"template destination {answers_path} must live under {anchor}/ — the "
            "scratch answers namespace is the only place an auto-allowed template "
            "verb may create a file"
        )
    dfd = _template_dir_fd(repo, rel.parts[:-1])
    name = rel.parts[-1]
    tmp = f".{name}.{os.getpid()}.tmp"
    try:
        try:
            wfd = os.open(
                tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644, dir_fd=dfd
            )
        except OSError as exc:
            raise GateError(f"{answers_path} temp refused (containment): {exc}") from exc
        try:
            view = memoryview(text.encode())
            while view:  # os.write may be short; every byte lands before publication
                view = view[os.write(wfd, view) :]
            written = os.fstat(wfd)
            try:
                os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except FileExistsError as exc:
                raise GateError(
                    f"{answers_path} already exists — the template never overwrites; "
                    "pass this round's fresh answers path or remove the file first"
                ) from exc
            except OSError as exc:
                raise GateError(f"{answers_path} refused (containment): {exc}") from exc
            # link(2) resolves the temp NAME, not the inode we wrote (the round-log
            # publisher's codex r10 precedent, re-found here as u-sr-04 r8 P2): a
            # concurrent unlink+replace of the temp between write and link would
            # install a foreign inode under our name. Verify while wfd is STILL
            # OPEN — that pins the written inode as allocated, so a recycled inode
            # number can never alias the comparison (Linux reuses freed inode
            # numbers immediately; the first CI run of the swap witness proved the
            # closed-fd ordering compares equal on ext4).
            check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            try:
                installed = os.fstat(check)
            finally:
                os.close(check)
            if (installed.st_dev, installed.st_ino) != (written.st_dev, written.st_ino):
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=dfd)
                raise GateError(
                    f"{answers_path} refused: installed inode is not the written one — "
                    "temp name was swapped mid-publish"
                )
        finally:
            os.close(wfd)
    finally:
        with contextlib.suppress(OSError):  # temp is scaffolding either way
            os.unlink(tmp, dir_fd=dfd)
        os.close(dfd)
    return repo / rel


def _template_preflight(repo: Path, base: str, answers_path: Path, arc_id: str) -> int:
    b = rw.code_binding(repo, base)
    diff_range = f"{b['base_sha']}..{b['head_sha']}"
    # [LAW:one-source-of-truth]: same sweep, same binding range _attest_preflight
    # enforces — the template can never carry labels the attest would not check
    labels = _run_sweep_script(repo, diff_range)
    final = _write_template(
        repo,
        answers_path,
        _template_text(arc_id, "preflight", diff_range, b["diff_digest"], labels, (), {}),
    )
    print(f"review-gate: preflight template at {final} ({len(labels)} hit labels pre-filled)")
    return 0


def _template_sweep(repo: Path, base: str, answers_path: Path, arc_id: str) -> int:
    state = _load_state_for_attest(repo)
    try:
        rows = fr.read_rows()
    except Exception as exc:  # unreadable obligation authority: refuse, as attest does
        raise GateError(f"gate log unreadable: {exc}") from exc
    ids = unanswered_findings(state, rows, arc_id)
    if not ids:
        print(
            f"review-gate: no unanswered findings for {arc_id} — nothing to template "
            "(a moved tree re-attests via review-attest-preflight)",
            file=sys.stderr,
        )
        return 1
    b = rw.code_binding(repo, base)
    diff_range = f"{b['base_sha']}..{b['head_sha']}"
    labels = _run_sweep_script(repo, diff_range)
    classes = _classify_findings(repo, rows, tuple(ids))
    final = _write_template(
        repo,
        answers_path,
        _template_text(arc_id, "sweep", diff_range, b["diff_digest"], labels, tuple(ids), classes),
    )
    unmatched = sum(1 for fid in ids if not classes[fid])
    print(
        f"review-gate: sweep template at {final} "
        f"({len(ids)} findings + {len(labels)} hit labels pre-filled; "
        f"{unmatched} findings match no preflight class and owe an intake line)"
    )
    return 0


def _vet_answers(answers: str, diff_range: str, diff_digest: str, path: Path) -> None:
    """Template-provenance + authored-content checks shared by both attest verbs
    ([LAW:single-enforcer] — one seam, both verbs). A binding stamp, when present,
    must match the freshly recomputed binding: answers filled against different
    bytes refuse instead of silently rebinding (codex u-sr-04 r5 P2). And at least
    one non-comment line must carry author residue: with zero obligations nothing
    else distinguishes an authored answer set from deletion-only template chrome
    (r5 P2, completing r2's 'answered means answered' for the hitless arm). A
    hand-authored file carries no stamp and any real answer line has residue, so
    the pre-template authoring shapes attest unchanged."""
    for ln in answers.splitlines():
        if not ln.lstrip().startswith("# binding:"):
            continue
        m = _BINDING_STAMP.match(ln.lstrip())
        if m is None:
            # fail-loud on a mangled stamp (codex u-sr-04 r7 P2): an unparseable
            # binding line must not silently demote the file to hand-authored
            # semantics — the deliberate-deletion arm stays out of scope per the
            # module trust boundary (r6 rejection, on the ledger)
            raise GateError(
                f"answers file {path} carries a malformed '# binding:' stamp "
                f"({ln.strip()!r}) — regenerate the template or restore the stamp"
            )
        if (m.group(1), m.group(2)) != (diff_range, diff_digest):
            raise GateError(
                f"answers file {path} was templated for binding {m.group(1)} "
                f"{m.group(2)[:12]} but the tree now binds {diff_range} "
                f"{diff_digest[:12]} — re-run the template verb on a fresh path and "
                "carry the still-true answers forward"
            )
    if not any(not ln.lstrip().startswith("#") and _has_residue(ln) for ln in answers.splitlines()):
        raise GateError(
            f"answers file {path} carries no authored answer content — comment "
            "chrome and template scaffolding alone do not attest"
        )


def _attest_preflight(repo: Path, base: str, answers_path: Path, arc_id: str) -> int:
    b = rw.code_binding(repo, base)
    labels = _run_sweep_script(repo, f"{b['base_sha']}..{b['head_sha']}")
    answers = _read_answers(answers_path)
    _vet_answers(answers, f"{b['base_sha']}..{b['head_sha']}", b["diff_digest"], answers_path)
    missing = _unanswered_labels(labels, answers)
    if missing:
        print(
            "review-gate: preflight NOT attested — hits without a named answer: "
            + "; ".join(missing),
            file=sys.stderr,
        )
        return 1
    _append_record(
        repo,
        "preflight",
        PreflightAttestation(
            arc_id=arc_id,
            head_sha=b["head_sha"],
            diff_digest=b["diff_digest"],
            hit_labels=labels,
            answers_digest=hashlib.sha256(answers.encode()).hexdigest(),
            ts=_now_iso(),
        ),
    )
    print(
        f"review-gate: preflight attested at {b['head_sha'][:12]} "
        f"({len(labels)} hit labels answered)"
    )
    return 0


def _load_state_for_attest(root: Path) -> GateState:
    """Corrupt CONTENT reads as EMPTY state for attest verbs — the tightening
    direction (obligations maximal, prior attestations void), matching the loud
    re-init _append_record performs at write time. Containment refusals still raise:
    a symlinked state is never 'repaired' through."""
    _read_state_bytes(state_path(root))  # containment raises here
    try:
        return load_state(root)
    except GateError as exc:
        print(f"review-gate: {exc} — treating as EMPTY state (gate tightens only)", file=sys.stderr)
        return GateState()


def _attest_sweep(repo: Path, base: str, answers_path: Path, arc_id: str) -> int:
    """Attest the OUTSTANDING obligations — every unanswered finding_id for the arc,
    any producer, any round (the finding-as-obligation model, codex r2 P1) — plus the
    class-sibling textual floor over the attested bytes."""
    state = _load_state_for_attest(repo)  # containment GateError propagates loud to main
    try:
        rows = fr.read_rows()
    except Exception as exc:  # unreadable obligation authority: refuse to attest (r7 P2)
        raise GateError(f"gate log unreadable: {exc}") from exc
    ids = unanswered_findings(state, rows, arc_id)
    if not ids:
        print(
            f"review-gate: no unanswered findings for {arc_id} — nothing to sweep "
            "(a moved tree re-attests via review-attest-preflight)",
            file=sys.stderr,
        )
        return 1
    b = rw.code_binding(repo, base)  # ONE binding per attest: sweep range and record agree
    # class-sibling textual floor over the attested bytes, same enforcer as preflight
    labels = _run_sweep_script(repo, f"{b['base_sha']}..{b['head_sha']}")
    answers = _read_answers(answers_path)
    _vet_answers(answers, f"{b['base_sha']}..{b['head_sha']}", b["diff_digest"], answers_path)
    missing = _unanswered_ids(tuple(ids), answers) + _unanswered_labels(labels, answers)
    if missing:
        print(
            "review-gate: sweep NOT attested — unanswered: " + "; ".join(missing),
            file=sys.stderr,
        )
        return 1
    # the skill's repair loop, enforced here and only here ([LAW:single-enforcer]):
    # classify with the LIVE table, so a finding whose class repair landed simply
    # matches and owes nothing, while one still unmatched owes a stated intake — and
    # an intake that CLAIMS a repair the table does not show is refused, not trusted
    classes = _classify_findings(repo, rows, tuple(ids))
    unmatched = tuple(fid for fid in ids if not classes[fid])
    intakes = _parse_intakes(tuple(ids), unmatched, answers)
    unlanded = [i for i in intakes if i.kind in INTAKE_REPAIR_KINDS]
    if unlanded:
        print(
            "review-gate: sweep NOT attested — intake claims a class repair the live "
            "table does not show (refresh-classes.py still leaves the finding "
            "unmatched; land the row, or disposition it instance-only): "
            + "; ".join(f"{i.finding_id} ({i.kind} {i.detail})" for i in unlanded),
            file=sys.stderr,
        )
        return 1
    _append_record(
        repo,
        "sweep",
        SweepAttestation(
            arc_id=arc_id,
            head_sha=b["head_sha"],
            diff_digest=b["diff_digest"],
            finding_ids=tuple(ids),
            answers_digest=hashlib.sha256(answers.encode()).hexdigest(),
            ts=_now_iso(),
            intake_instance_only=tuple(i.finding_id for i in intakes),
        ),
    )
    print(
        f"review-gate: sweep attested at {b['head_sha'][:12]} ({len(ids)} findings answered; "
        f"{len(intakes)} unmatched by any preflight class, dispositioned instance-only)"
    )
    return 0


def _attest_budget(repo: Path, extra: int, reason: str, arc_id: str) -> int:
    if extra < 1:
        # a zero/negative "extension" would invert the tightening invariant: dropping
        # the record on corruption recovery would then LOOSEN the gate (codex r5 P2)
        print("review-gate: budget extension must be >= 1 round", file=sys.stderr)
        return 1
    if not reason.strip():
        print("review-gate: budget extension requires a non-empty --reason", file=sys.stderr)
        return 1
    _append_record(
        repo,
        "budget_extension",
        BudgetExtension(arc_id=arc_id, extra_rounds=extra, reason=reason, ts=_now_iso()),
    )
    print(f"review-gate: budget extended by {extra} rounds for {arc_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("attest-preflight", "attest-sweep", "template-preflight", "template-sweep"):
        sp = sub.add_parser(name)
        sp.add_argument("--answers", required=True, help="in-worktree relative path")
        sp.add_argument("--base", default="main")
        sp.add_argument("--repo", default=".")
    sp = sub.add_parser("attest-budget")
    sp.add_argument("--extra", type=int, required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--repo", default=".")
    sp = sub.add_parser("check")
    sp.add_argument("--base", default="main")
    sp.add_argument("--repo", default=".")
    sp = sub.add_parser("launch")
    sp.add_argument("--log", required=True, help="requested round-log destination (r<N>.log)")
    sp.add_argument("--base", default="main")
    sp.add_argument("--repo", default=".")
    args = p.parse_args(argv)
    repo = Path(args.repo).resolve()
    arc_id, _ = rw.env_arc_and_lane()
    try:
        if args.cmd == "attest-preflight":
            return _attest_preflight(repo, args.base, Path(args.answers), arc_id)
        if args.cmd == "attest-sweep":
            return _attest_sweep(repo, args.base, Path(args.answers), arc_id)
        if args.cmd == "template-preflight":
            return _template_preflight(repo, args.base, Path(args.answers), arc_id)
        if args.cmd == "template-sweep":
            return _template_sweep(repo, args.base, Path(args.answers), arc_id)
        if args.cmd == "attest-budget":
            return _attest_budget(repo, args.extra, args.reason, arc_id)
    except GateError as exc:
        print(f"review-gate: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "launch":
        return launch(repo, args.base, arc_id, args.log)
    decision = admit(repo, args.base, arc_id)
    if isinstance(decision, Refused):
        print(f"review-gate: {decision.detail}\n  recipe: {decision.recipe}", file=sys.stderr)
        print(f"review-gate-check: REFUSED ({decision.code})", file=sys.stderr)
        return 3
    if isinstance(decision, Inactive):
        print(f"review-gate-check: INACTIVE — {decision.reason}")
        return 0
    print(f"review-gate-check: ALLOWED (next round {decision.round_n})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
