"""Durable per-run output-carrying event-history store (B-ENGINE-OUTPUT-REPLAY).

R-FS-1 standalone `B-*` arc **B-ENGINE-OUTPUT-REPLAY** (design-fork-first per
X-AL-3). Materializes the C-CP-08 §8.1 `engine_replay` "**activity outputs cached
and replayed**" clause that EVENT_SOURCED_REPLAY ships DEGENERATE today (the
"no re-execution of activities" clause is already delivered as skip-prefix; see
`.harness/r-fs-1-e-impl-1-finding.md` §2).

Why a dedicated store (not the IS F2 ledger)
--------------------------------------------
The F2 IS state-ledger (`harness_is.state_ledger_write.EntryPayload`, C-IS-07
§7.1) carries only a `response_hash` **digest** — never the activity output, by
design (the ledger is causality + integrity, not data storage). Extending the
`EntryPayload` to carry outputs would ripple the C-IS-05 §5.2 entry hash + the
JSONL shape + the IS contract for a CP/runtime-local replay concern — foreclosed
by I-6 (hand-roll, no vendored event-sourcing framework) + ADR-F2 (the ledger's
six-field shape is frozen). So the output history lives in a dedicated, harness-
owned store, mirroring the crash-survivable journal mechanism proven in
`JournalWorkflowPauseStore` (#475 → R-CC-1 arc #3): per-key JSONL file, `fsync` +
directory-fsync, torn-append self-healing, fail-closed-on-corruption.

The store ↔ ledger skew discipline (the load-bearing correctness rule)
----------------------------------------------------------------------
Two durable substrates now record per step: the **F2 ledger** (the `resume_at`
authority — the count of contiguous materialized steps) and **this store** (the
data). A crash between them de-syncs them, so the producer writes here BEFORE the
ledger-append that `resume_at` counts (`workflow_driver.py:_append_step_ledger_
entry`) — the B-EFFECT-FENCE RESERVE-before-COMMIT shape: the store always holds
≥ the ledger's materialized prefix. Consequently a resume drives rehydration by
`resume_at` (NOT "load whatever's in the store" — the store may hold one extra
uncommitted step from a crash-after-store-before-ledger) and FAILS CLOSED if an
output is missing for a step the ledger says is materialized (corruption — the
symmetric of B-FANOUT-PAUSE's identity-mismatch fail-close).

Keying
------
One file per `run_idempotency_key` — the SAME stable id the resume join
(`_determine_resume_at`) uses (the F2 step key is `f(run_idempotency_key,
step_index)`, and an EVENT_SOURCED_REPLAY restart re-runs with the same `run_id`
→ the same `run_idempotency_key`), so a capture-side write and a fresh-bootstrap
resume-side read find the same file (restart-survival).

Residence
---------
Co-located under the bootstrap-resolved `STATE_LEDGER` dir as an
`engine-output/` subdir — harness-internal *recovery substrate* (like the
pause-journal), NOT a canonical `PathClass` artifact (IS-AL-1 forecloses
inventing a canonical artifact class, not every internal file).

Authority: runtime spec C-RT-32 (NEW); `.harness/r-fs-1-e-impl-1-finding.md` §4.
Mechanism mirrors `JournalWorkflowPauseStore`.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

__all__ = [
    "ENGINE_OUTPUT_BRANCHES_SUFFIX",
    "ENGINE_OUTPUT_SUBDIR",
    "EngineOutputStore",
    "engine_output_dir_for",
]

#: Subdirectory under the resolved ``STATE_LEDGER`` directory holding the
#: per-run engine-output journals (co-location sibling of ``pause-journal``).
ENGINE_OUTPUT_SUBDIR = "engine-output"

#: Per-run directory suffix holding the CONCURRENT fan-out branch journals
#: (B-FANOUT-OUTPUT-REPLAY). The linear store keys one ``{digest}.jsonl`` FILE
#: per run (single-writer); a concurrent fan-out has N branch writers, so each
#: branch gets its OWN file under ``{digest}.branches/`` — no shared-file
#: contention (the advisor's per-branch-FILE keying). No collision with the
#: linear ``{digest}.jsonl`` file (a ``.branches`` DIR vs a ``.jsonl`` file).
ENGINE_OUTPUT_BRANCHES_SUFFIX = ".branches"


def engine_output_dir_for(state_ledger_dir: Path) -> Path:
    """Return the engine-output directory co-located under the STATE_LEDGER dir.

    The single source of truth for the journal directory location, consumed by
    BOTH the stage-5 factory (deriving ``state_ledger_dir`` from the ledger
    writer handle) and the resume path — both compute the same path for the same
    ``(workflow_class, deployment_surface)``, so a capture-side write and a
    fresh-bootstrap resume-side read find the same file (restart-survival).
    """
    return state_ledger_dir / ENGINE_OUTPUT_SUBDIR


class EngineOutputStore:
    """Durable append-only per-run step-output journal (output-carrying history).

    ``record(run_key, step_index, step_id, output)`` appends one JSONL line to the
    run's ``<journal_dir>/<sha256(run_key)>.jsonl`` file (durably, ``fsync``-ed).
    ``read_outputs(run_key)`` reads every parseable line back into a
    ``{step_index: (step_id, output)}`` map (last-wins per index; an unparseable
    torn line is skipped). The caller (the resume rehydration site) validates
    prefix completeness against ``resume_at`` and fails closed on a gap.
    """

    def __init__(self, *, journal_dir: Path) -> None:
        self._journal_dir = Path(journal_dir)

    def record(
        self,
        run_key: str,
        step_index: int,
        step_id: str,
        output: Mapping[str, Any],
    ) -> None:
        """Append one step-output record to the run's file, durably (fsync-ed)."""
        record = {
            "step_index": int(step_index),
            "step_id": str(step_id),
            "output": dict(output),
        }
        # Deterministic, sorted-key serialization (mirrors the pause journal). The
        # output Mapping is the dispatcher's already-produced opaque value; the
        # store does NOT introspect it (the §25.3.3.4 step-body-opaque discipline).
        line = json.dumps(record, sort_keys=True)
        self._append(run_key, line)

    def read_outputs(self, run_key: str) -> dict[int, tuple[str, dict[str, Any]]]:
        """Return ``{step_index: (step_id, output)}`` for every parseable record.

        Empty when the run has no journal file. A torn trailing line (crash mid-
        append) is SKIPPED (it is the uncommitted step `resume_at` ignores anyway);
        a later record for the same `step_index` wins (idempotent re-record). The
        caller checks prefix completeness against `resume_at` (fail-closed on a gap).
        """
        path = self._journal_file(run_key)
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A read-level failure (incl. invalid-UTF-8) yields no recoverable
            # outputs → the caller's prefix-completeness check fails closed.
            return {}
        outputs: dict[int, tuple[str, dict[str, Any]]] = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            parsed = self._parse_record(line)
            if parsed is not None:
                step_index, step_id, output = parsed
                outputs[step_index] = (step_id, output)
        return outputs

    def journal_exists(self, run_key: str) -> bool:
        """Whether a journal FILE exists for the run (regardless of readability).

        The resume rehydration uses this to discriminate, when `read_outputs`
        returns empty, between "no journal at all" (a config flip — the original run
        had `engine_output_replay=False`, so nothing was ever recorded → degrade to
        the empty-channel path) and "a journal exists but yields no readable records"
        (an unreadable / corrupt store → fail closed, never silently drop cached
        outputs). Per-decorrelated-review: advisor caught the config-flip degrade,
        Codex caught that a read-failure must NOT be collapsed into it."""
        return self._journal_file(run_key).exists()

    # -- B-FANOUT-OUTPUT-REPLAY: concurrent-fan-out branch capture ------------
    #
    # The STORE is the SOLE authority for which-branches-completed on a fan-out
    # crash-resume: the durable F2 ledger is BINARY for a concurrent fan-out
    # (branch terminals buffer into per-branch `BufferingLedgerWriter`s and drain
    # ATOMICALLY at the barrier per CP §25.12 D1.b), so a mid-fan-out crash leaves
    # an EMPTY ledger but the per-branch journals hold the completed outputs.
    # `step_id` is recorded at CAPTURE time — the load-bearing identity that lets
    # the existing resume material-diff guard detect a changed body on replay.

    def record_branch(
        self,
        run_key: str,
        branch_index: int,
        step_id: str,
        terminal_status: str,
        output: Mapping[str, Any] | None,
    ) -> None:
        """Append one branch terminal-DISPOSITION record to the branch's OWN file, durably.

        The store records the branch's terminal **disposition** (``completed`` /
        ``timed_out`` / ``scoped_aborted``) for EVERY branch that reaches a terminal boundary
        — NOT only output-bearing clean successes — so a crash-resume can distinguish
        recover-and-fold (``completed`` with ``output``), recover-as-terminal (``completed``
        with ``output is None`` — a ran-and-errored branch whose effect LANDED, never
        re-dispatched, never folded), the irreducibly-ambiguous ``timed_out`` (a deadline-cut
        in-flight dispatch may or may not have landed → the caller FAILS CLOSED), and
        ``scoped_aborted`` (CP spec v1.74 §1 — a branch the operator scoped-aborted via
        ``EffectFenceResolution.ABORT_BRANCH``: ``output is None``, never re-dispatched, but
        distinguished from a ran-and-errored ``completed`` so the CP crash-resume reconstruct
        reproduces the in-resume all-abort FAILED rather than a vacuous PAUSED/PARTIAL). The
        IS-hash-bearing F2 ledger terminal entry stays ``completed`` for a scoped-abort branch
        (no §5.2 IS-hash change); only this runtime store carries the distinguishing value. An
        output-only schema made every non-clean-success disposition invisible (the at-most-once
        fail-open class); recording disposition closes it.

        Per-branch file (``{digest}.branches/branch-{branch_index}.jsonl``) so N concurrent
        branch writers never contend on a shared handle. RESERVE-before-COMMIT: the caller
        fsyncs this BEFORE the branch's terminal ledger-append, so the store always holds
        >= the (binary) ledger's committed branch set.
        """
        record = {
            "step_id": str(step_id),
            "terminal_status": str(terminal_status),
            "output": dict(output) if output is not None else None,
        }
        line = json.dumps(record, sort_keys=True)
        self._append_path(self._branch_file(run_key, branch_index), line)

    def read_branch_records(
        self, run_key: str
    ) -> dict[int, tuple[str, str, dict[str, Any] | None]]:
        """Return ``{branch_index: (step_id, terminal_status, output | None)}`` for every
        READABLE branch.

        Empty when no fan-out branch journals exist (config flip / first run). The
        ``branch_index`` is the filename authority (``branch-{n}.jsonl``); a present but
        UNREADABLE branch file is omitted here and surfaced by `present_branch_indexes` so
        the caller fails closed (never silently re-dispatching a corrupt branch). ``output``
        is ``None`` for a terminal-no-output branch (ran-and-errored / timed-out /
        scoped-aborted). ``terminal_status`` is one of ``completed`` / ``timed_out`` /
        ``scoped_aborted`` (any other value is treated as corrupt → fail-closed).
        """
        branches_dir = self._branches_dir(run_key)
        if not branches_dir.exists():
            return {}
        records: dict[int, tuple[str, str, dict[str, Any] | None]] = {}
        for path in branches_dir.glob("branch-*.jsonl"):
            branch_index = self._branch_index_from_name(path.name)
            if branch_index is None:
                continue
            parsed = self._read_last_branch_disposition(path)
            if parsed is not None:
                records[branch_index] = parsed
        return records

    def present_branch_indexes(self, run_key: str) -> set[int]:
        """Return the set of branch ordinals whose journal FILE exists (any state).

        The fail-closed discriminator (the branch-level analogue of
        `journal_exists`): ``present_branch_indexes - read_branch_outputs.keys()``
        is the set of branch files that EXIST but yield no readable record — a
        corrupt branch the caller must fail closed on rather than re-dispatch.
        """
        branches_dir = self._branches_dir(run_key)
        if not branches_dir.exists():
            return set()
        indexes: set[int] = set()
        for path in branches_dir.glob("branch-*.jsonl"):
            branch_index = self._branch_index_from_name(path.name)
            if branch_index is not None:
                indexes.add(branch_index)
        return indexes

    def record_orchestrator(
        self,
        run_key: str,
        step_id: str,
        output: Mapping[str, Any],
    ) -> None:
        """Capture the ORCHESTRATOR_WORKERS ``steps[0]`` output (not a branch).

        The orchestrator output rides the `FanOutResumeState.orchestrator_output`
        field on resume; captured to a dedicated ``orchestrator.jsonl`` under the
        branches dir so it does not collide with the ``branch-*`` worker files.
        """
        record = {"step_id": str(step_id), "output": dict(output)}
        line = json.dumps(record, sort_keys=True)
        self._append_path(self._orchestrator_file(run_key), line)

    def read_orchestrator_output(self, run_key: str) -> tuple[str, dict[str, Any]] | None:
        """Return the captured ``(step_id, output)`` orchestrator record, or None.

        None means ABSENT (no orchestrator captured). A present-but-unreadable
        orchestrator file is surfaced by `orchestrator_present` so the caller fails
        closed (the symmetric of the per-branch corrupt-detection).
        """
        path = self._orchestrator_file(run_key)
        if not path.exists():
            return None
        return self._read_last_branch_record(path)

    def orchestrator_present(self, run_key: str) -> bool:
        """Whether an orchestrator journal FILE exists (regardless of readability)."""
        return self._orchestrator_file(run_key).exists()

    def record_fanout_cardinality(self, run_key: str, branch_count: int) -> None:
        """Record the capture-time fan-out CARDINALITY (the total branch/step count) once
        per run, so a crash-resume fails closed on a CHANGED cardinality. A manifest
        redefined with FEWER branches between crash + resume would otherwise pass the
        per-branch material-diff (the surviving prefix matches) and silently DROP the
        original in-flight branches (out-of-family Codex [P2]). Idempotent (last-wins)."""
        line = json.dumps({"branch_count": int(branch_count)}, sort_keys=True)
        self._append_path(self._cardinality_file(run_key), line)

    def fanout_cardinality_present(self, run_key: str) -> bool:
        """Whether the fan-out cardinality MARKER file exists (presence-only, like
        ``orchestrator_present`` / ``present_dispatched_indexes``).

        Distinct from ``read_fanout_cardinality`` (which returns ``None`` for BOTH an absent
        marker AND a present-but-torn/unreadable one): the crash-resume classifier needs to tell
        an ABSENT cardinality (the genuine pre-cardinality window) from a PRESENT-but-unreadable
        one (corruption — the run advanced past the cardinality write, the marker is fsynced after
        ``record_orchestrator``). A torn marker still proves the run advanced past capture, so its
        presence — not its readability — is the corruption signal (out-of-family Codex [P2];
        ``[[durable-recovery-presence-validity-scope]]``: presence ≠ validity)."""
        return self._cardinality_file(run_key).exists()

    def read_fanout_cardinality(self, run_key: str) -> int | None:
        """Return the recorded capture-time fan-out cardinality, or None if unrecorded."""
        path = self._cardinality_file(run_key)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        result: int | None = None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(loaded, dict):
                continue
            count = cast("dict[str, object]", loaded).get("branch_count")
            if isinstance(count, int):
                result = count
        return result

    # -- B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE: reserve-before-DISPATCH --
    #
    # The branch-output capture above is RESERVE-before-COMMIT — it proves a branch
    # COMPLETED. It does NOT distinguish a branch that NEVER dispatched from one that
    # RAN its effect but crashed before the capture (both leave NO branch record). For
    # the strict tiers (PAUSE / CASCADE_CANCEL) that ambiguity forces a fail-closed on
    # any INCOMPLETE crash-resume — re-dispatching an absent (maybe-ran) effect-BEARING
    # branch would risk a double-fire. This sidecar adds the missing at-most-once
    # primitive: a durable per-(run, branch) "dispatched" MARKER written + fsynced
    # strictly BEFORE the branch body dispatches. The load-bearing invariant: within an
    # instrumented run, marker-ABSENT ⟺ the branch's effect did NOT fire. So an absent
    # branch with NO dispatch marker is PROVABLY not-yet-run (safe to re-dispatch fresh,
    # first-and-only); a dispatch marker with no terminal capture is MAYBE-RAN (the
    # narrow fire→capture window — still fail-closed; its resolution is a follow-on).
    # The fan-out analogue of the §14.22 C-RT-31 effect-fence `try_reserve` at branch
    # granularity. Marker presence is the whole signal — `step_id` is recorded for
    # parity + future material-diff, never read by the strict-tier classifier.
    #
    # The per-run "instrumented" STAMP is the CROSS-VERSION guard
    # (`[[durable-recovery-presence-validity-scope]]`: presence ≠ validity). A crash
    # journal written by PRE-arc code has NO markers for ANY branch — INCLUDING a
    # maybe-ran one — so classifying its absent branches "provably not-run" would
    # re-dispatch + double-fire, the exact failure this arc prevents. The stamp is
    # written ONCE at fan-out start (before any branch dispatches) by marker-instrumented
    # code only; the strict-tier classifier trusts the per-branch markers ONLY when the
    # stamp is present, and retains the conservative fail-closed for an un-stamped (old)
    # journal.

    def record_branch_dispatched(
        self,
        run_key: str,
        branch_index: int,
        step_id: str,
        step_kind: str,
        child_recoverable: bool | None = None,
        child_engine_class: str | None = None,
        proceed_unstamped: bool | None = None,
    ) -> None:
        """Mark a fan-out branch as DISPATCHED (the reserve-before-dispatch marker), durably.

        The caller writes this — fsynced — strictly BEFORE the branch body dispatches, so on
        any crash a present marker proves the branch's effect MAY have fired and an absent
        marker proves it did NOT. Its OWN per-branch file (``{digest}.branches/branch-{i}.
        dispatched``) so N concurrent branch writers never contend; no collision with the
        ``branch-*.jsonl`` terminal-capture files (a ``.dispatched`` vs ``.jsonl`` suffix).
        Idempotent (last-wins; presence is the signal). ``step_id`` is recorded for parity
        with ``record_branch`` (a future material-diff). ``step_kind`` is the branch's
        DISPATCH-TIME step kind — read by ``dispatched_branch_kinds`` so the maybe-ran
        re-fire-safety classifier keys on the ORIGINAL kind, never the (possibly changed)
        resumed manifest's kind (the at-most-once changed-manifest guard, B-FANOUT-CRASH-
        RESUME-MAYBE-RAN-RESOLUTION).

        ``child_recoverable`` (B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT) — for a
        SUB_AGENT_DISPATCH worker only, whether its CHILD was RE-DISPATCH-RECOVERABLE at
        DISPATCH (a durable replay engine under either LINEAR or supported fan-out topology, with
        nested children recursively recoverable — so the child's tool sinks auto-fence, the child's
        own crash-resume is durable, AND its ``final_state`` reconstructs). Read by
        ``subagent_child_recoverable_indexes`` so a
        maybe-ran SUB_AGENT_DISPATCH worker is re-dispatch-recoverable (its child auto-resumes
        under the deterministic run_id) ONLY when its child can auto-resume RESULT-FAITHFULLY —
        else the classifier fails closed (a non-recoverable child re-runs from scratch →
        double-fire, or an unsupported topology/engine cannot reconstruct result-faithfully →
        fold corruption). The DISPATCH-TIME value (the at-most-once changed-manifest guard).
        ``None`` (every non-SUB_AGENT_DISPATCH branch + pre-arc markers) → the field is OMITTED
        so those markers hash/parse byte-identically to before.

        ``child_engine_class`` (B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD) — for
        a SUB_AGENT_DISPATCH worker only, the child's DISPATCH-TIME ``EngineClass`` value string.
        The cross-engine-class swap guard (out-of-family Codex [P1]): once >1 durable engine class
        is re-dispatch-recoverable ({ESR,WAL,SAVE_POINT,RECONCILER}), ``child_recoverable=True``
        no longer pins WHICH recovery mechanism the child will use. A maybe-ran child whose marker
        recorded one recoverable engine (e.g. RECONCILER) but whose RESUMED manifest supplies a
        DIFFERENT recoverable engine (e.g. SAVE_POINT) under the SAME ``step_id`` passes both
        boolean recoverability gates; because ``compose_child_run_id_seed`` is
        engine-class-AGNOSTIC (hashes only parent_idempotency_key + branch_path +
        child_workflow_id), the swap re-dispatches the child against the SAME durable store through
        a DIFFERENT recovery
        mechanism → the RECONCILER CAS at-most-once protection is bypassed (unlike the documented
        child-swap / tool-swap parities, which CHANGE the key). Read by
        ``dispatched_branch_child_engine_classes`` so the maybe-ran gate requires the marker engine
        == the resumed engine (fail closed on mismatch / ``None``) — the engine-class leg of the
        same-identity guard (mirrors the same-kind + changed-step_id legs). ``None`` (every
        non-SUB_AGENT_DISPATCH branch + pre-arc markers) → the field is OMITTED so those markers
        hash/parse byte-identically to before."""
        record: dict[str, object] = {"step_id": str(step_id), "step_kind": str(step_kind)}
        if child_recoverable is not None:
            record["child_recoverable"] = bool(child_recoverable)
        if child_engine_class is not None:
            record["child_engine_class"] = str(child_engine_class)
        line = json.dumps(record, sort_keys=True)
        self._append_path(self._branch_dispatched_file(run_key, branch_index), line)

    def subagent_child_recoverable_indexes(self, run_key: str) -> set[int]:
        """Branch ordinals whose DISPATCH-TIME marker recorded ``child_recoverable=True``.

        B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT — a maybe-ran SUB_AGENT_DISPATCH worker is
        recoverable-by-re-dispatch (its child auto-resumes under the deterministic run_id, with
        result-faithful ``final_state`` reconstruction) ONLY if its child was RECOVERABLE at
        dispatch (durable LINEAR or supported fan-out, with nested children recursively
        recoverable, recorded here from the opaque child manifest).
        The marker is the DISPATCH-TIME value (the at-most-once changed-manifest guard — a child
        edited recoverable→non-recoverable between dispatch + resume must STILL be classified by
        the dispatch-time value). Absent / ``False`` / torn / pre-arc → NOT in the set → the
        classifier fails closed (the #701 decline-mirror; never an auto-recover). Presence of the
        per-branch marker file is via ``glob`` (mirrors ``dispatched_branch_kinds``); the bool is
        best-effort (any read/parse failure → not-recoverable)."""
        branches_dir = self._branches_dir(run_key)
        if not branches_dir.exists():
            return set()
        indexes: set[int] = set()
        for path in branches_dir.glob("branch-*.dispatched"):
            branch_index = self._dispatched_index_from_name(path.name)
            if branch_index is None:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                if record.get("child_recoverable") is True:
                    indexes.add(branch_index)
            except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
                # torn / pre-arc / invalid-encoding marker → not provably recoverable →
                # fail-closed at the classifier (presence ≠ validity).
                continue
        return indexes

    def present_dispatched_indexes(self, run_key: str) -> set[int]:
        """Return the set of branch ordinals with a DISPATCHED marker file (any state).

        The strict-tier classifier's authority for which branches BEGAN dispatch. A branch
        in this set but ABSENT from ``read_branch_records`` is MAYBE-RAN (dispatched, no
        terminal capture — the fire→capture window) → fail closed. A branch absent from BOTH
        is PROVABLY not-yet-run → safe to re-dispatch. Presence-only (the marker carries no
        readability gate — an unreadable/torn marker still proves dispatch BEGAN, which is
        the conservative reading: treat it as maybe-ran)."""
        branches_dir = self._branches_dir(run_key)
        if not branches_dir.exists():
            return set()
        indexes: set[int] = set()
        for path in branches_dir.glob("branch-*.dispatched"):
            branch_index = self._dispatched_index_from_name(path.name)
            if branch_index is not None:
                indexes.add(branch_index)
        return indexes

    def dispatched_branch_kinds(self, run_key: str) -> dict[int, str | None]:
        """Return ``{branch_index: dispatch-time step_kind}`` for every DISPATCHED marker.

        The maybe-ran re-fire-safety classifier keys on this — the ORIGINAL (dispatch-time)
        step kind recorded in the marker, NOT the resumed manifest's current kind. This is the
        at-most-once changed-manifest guard: a branch that dispatched as an effect-bearing kind
        and crashed before terminal capture, then is re-supplied at the same ordinal as a
        re-fire-safe kind, must STILL be classified by its original effect-bearing kind (else
        the relaxation would re-dispatch + double-fire the original effect). A marker missing /
        with an unreadable / non-str ``step_kind`` (a pre-arc v1.60/v1.61 marker, or a torn
        write) maps to ``None`` → the classifier treats it as NOT re-fire-safe (fail closed —
        cannot prove the original kind). Presence-only on the index (mirrors
        ``present_dispatched_indexes``); the kind is best-effort."""
        branches_dir = self._branches_dir(run_key)
        if not branches_dir.exists():
            return {}
        kinds: dict[int, str | None] = {}
        for path in branches_dir.glob("branch-*.dispatched"):
            branch_index = self._dispatched_index_from_name(path.name)
            if branch_index is None:
                continue
            kind: str | None = None
            try:
                record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                raw = record.get("step_kind")
                kind = raw if isinstance(raw, str) else None
            except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
                # torn / pre-arc / invalid-encoding marker → unknown → fail-closed at the
                # classifier. UnicodeDecodeError (invalid UTF-8 bytes from read_text) is a
                # ValueError subclass — listed explicitly so the torn-marker safety boundary is
                # self-evident; json's ValueError + IndexError (empty file) + AttributeError
                # (non-dict JSON) cover the other corruption shapes.
                kind = None
            kinds[branch_index] = kind
        return kinds

    def dispatched_branch_step_ids(self, run_key: str) -> dict[int, str | None]:
        """Return ``{branch_index: dispatch-time step_id}`` for every DISPATCHED marker.

        The fence-recoverable maybe-ran classifier (B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-
        MAYBE-RAN-FENCE-STEP-ID) keys on this — the ORIGINAL (dispatch-time) ``step_id``
        recorded in the marker, so a fence-recoverable (TOOL_STEP / MANAGED_AGENTS) ordinal's
        held effect-fence reserve key ``(parent_idempotency_key, step_id)`` is reproducible at
        crash-resume / pause-reconstruct time WITHOUT reading the opaque ``step_payload``. Two
        uses: (1) carried into the reconstructed ``effect_fence_paused_branches`` entry's
        ``step_id`` so the api.resume material-diff guard fail-closes on a changed step_id (the
        deferred-resume double-fire fix); (2) the crash-time changed-step_id conjunct in
        ``_fence_unrecoverable_maybe_ran_indices`` (an operator-edited crash-resume manifest that
        kept the kind but changed the step_id would otherwise re-dispatch under a DIFFERENT fence
        key → double-fire). A marker missing / with an unreadable / non-str ``step_id`` (a torn
        write) maps to ``None`` → the classifier treats it as NOT fence-recoverable (fail closed —
        cannot prove the original key). Presence-only on the index (mirrors
        ``present_dispatched_indexes`` / ``dispatched_branch_kinds``); the step_id is
        best-effort."""
        branches_dir = self._branches_dir(run_key)
        if not branches_dir.exists():
            return {}
        step_ids: dict[int, str | None] = {}
        for path in branches_dir.glob("branch-*.dispatched"):
            branch_index = self._dispatched_index_from_name(path.name)
            if branch_index is None:
                continue
            step_id: str | None = None
            try:
                record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                raw = record.get("step_id")
                step_id = raw if isinstance(raw, str) else None
            except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
                # torn / pre-arc / invalid-encoding marker → unknown → fail-closed at the
                # classifier (same corruption boundary as ``dispatched_branch_kinds``).
                step_id = None
            step_ids[branch_index] = step_id
        return step_ids

    def dispatched_branch_child_engine_classes(self, run_key: str) -> dict[int, str | None]:
        """Return ``{branch_index: dispatch-time child EngineClass value}`` for every DISPATCHED
        marker (B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD).

        The cross-engine-class swap guard (out-of-family Codex [P1]): the maybe-ran SUB_AGENT
        recovery gate keys on this — the ORIGINAL (dispatch-time) child engine class recorded in
        the marker — and requires it to EQUAL the resumed manifest's child engine class. Without
        it, a maybe-ran child whose marker recorded one recoverable engine (e.g. RECONCILER) but
        whose resumed manifest supplies a DIFFERENT recoverable engine (e.g. SAVE_POINT) under the
        same ``step_id`` passes both boolean recoverability gates and re-dispatches against the SAME
        engine-class-agnostic ``compose_child_run_id_seed`` durable store through a DIFFERENT
        recovery mechanism → the RECONCILER CAS at-most-once protection is bypassed. A marker
        missing / with an unreadable / non-str ``child_engine_class`` (a non-SUB_AGENT branch, a
        pre-arc marker, or a torn write) maps to ``None`` → the gate treats it as a mismatch (fail
        closed — cannot prove
        the original engine). Presence-only on the index (mirrors ``dispatched_branch_kinds`` /
        ``dispatched_branch_step_ids``); the engine class is best-effort."""
        branches_dir = self._branches_dir(run_key)
        if not branches_dir.exists():
            return {}
        engines: dict[int, str | None] = {}
        for path in branches_dir.glob("branch-*.dispatched"):
            branch_index = self._dispatched_index_from_name(path.name)
            if branch_index is None:
                continue
            engine: str | None = None
            try:
                record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                raw = record.get("child_engine_class")
                engine = raw if isinstance(raw, str) else None
            except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
                # torn / pre-arc / invalid-encoding marker → unknown → fail-closed at the
                # classifier (same corruption boundary as ``dispatched_branch_kinds``).
                engine = None
            engines[branch_index] = engine
        return engines

    def record_dispatch_instrumented(self, run_key: str) -> None:
        """Stamp this run as DISPATCH-INSTRUMENTED (the cross-version guard), durably.

        Written ONCE at fan-out start (before any branch dispatches) by worker/peer marker-
        instrumented code only. The strict-tier classifier trusts the per-branch dispatch markers
        ONLY when this stamp is present — an un-stamped journal (written by PRE-arc or PROCEED
        worker code, which has no markers for ANY branch including a maybe-ran one) retains the
        conservative incomplete-recovery fail-closed. Strict-tier orchestrator reserve-before-
        dispatch also writes this stamp before any worker fan-out starts because the orchestrator
        pristine-window classifier already uses it to validate marker contents. PROCEED
        orchestrator markers intentionally do not stamp this worker trust gate. Idempotent
        (last-wins; presence is the signal)."""
        line = json.dumps({"instrumented": True}, sort_keys=True)
        self._append_path(self._dispatch_instrumented_file(run_key), line)

    def dispatch_instrumented(self, run_key: str) -> bool:
        """Whether the per-run dispatch-instrumented STAMP file exists (the cross-version guard)."""
        return self._dispatch_instrumented_file(run_key).exists()

    # -- B-FANOUT-CRASH-RESUME-ORCHESTRATOR-DISPATCH: reserve-before-DISPATCH for the
    #    ORCHESTRATOR_WORKERS `steps[0]` orchestrator --------------------------------
    #
    # The orchestrator (`steps[0]`) is the SEQUENTIAL dispatch that runs FIRST, before
    # any worker fans out. `record_orchestrator` above is RESERVE-before-COMMIT — it
    # captures the output AFTER the orchestrator completes, proving it ran. It does NOT
    # cover the orchestrator's OWN fire→capture window: a crash AFTER the orchestrator
    # fires its effect but BEFORE `record_orchestrator` leaves no orchestrator record
    # (and no cardinality marker), so a fresh re-run re-dispatches `steps[0]` → a
    # potential double-fire on the compliance tier. This marker is the orchestrator
    # analogue of the per-WORKER reserve-before-dispatch marker above (a SINGLE marker,
    # not per-index — there is exactly one orchestrator): written + fsynced strictly
    # BEFORE the orchestrator body dispatches. It is a distinct signal from the worker
    # dispatch-instrumented stamp: strict tiers may stamp that trust gate for the existing
    # orchestrator pristine-window classifier, while PROCEED can write this marker without making
    # missing worker branch markers trustworthy on a later strict-tier resume. The orchestrator
    # dispatch is SYNCHRONOUS (no `ensure_future` / await between the marker write and the
    # dispatch), so the marker→dispatch sequence has no yield point — no false-positive
    # marker is possible without the worker path's atomicity dance.

    def record_orchestrator_dispatched(
        self,
        run_key: str,
        step_id: str,
        step_kind: str,
        child_recoverable: bool | None = None,
        child_engine_class: str | None = None,
        proceed_unstamped: bool | None = None,
    ) -> None:
        """Mark the ORCHESTRATOR_WORKERS ``steps[0]`` orchestrator as DISPATCHED, durably.

        The reserve-before-DISPATCH marker for the orchestrator: written — fsynced — strictly
        BEFORE the orchestrator body dispatches, so on any crash a present marker proves the
        orchestrator's effect MAY have fired and an absent marker proves it did NOT. Its OWN
        file (``{digest}.branches/orchestrator.dispatched``) — no collision with the
        ``orchestrator.jsonl`` terminal-capture file (a ``.dispatched`` vs ``.jsonl`` suffix).
        Idempotent (last-wins; presence is the signal). ``step_id`` is recorded for parity with
        ``record_orchestrator`` (a future material-diff), never read by the classifier.
        ``step_kind`` is the orchestrator's DISPATCH-TIME step kind — read by
        ``orchestrator_dispatched_kind`` so the maybe-ran re-fire-safety classifier keys on the
        ORIGINAL kind, never the (possibly changed) resumed manifest's kind (the at-most-once
        changed-manifest guard, B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION — the
        single-orchestrator analogue of the per-branch ``record_branch_dispatched`` kind).

        ``child_recoverable`` (B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT) — for a
        SUB_AGENT_DISPATCH orchestrator only, whether its CHILD was RE-DISPATCH-RECOVERABLE at
        DISPATCH (the same durable LINEAR / supported fan-out / recursive-child predicate the
        worker uses). Read by ``orchestrator_subagent_child_recoverable`` so a
        maybe-ran SUB_AGENT_DISPATCH orchestrator is re-dispatch-recoverable (re-running the
        whole fan-out fresh re-dispatches the orchestrator, whose child auto-resumes under the
        deterministic run_id) ONLY when its child can auto-resume RESULT-FAITHFULLY — else the
        classifier fails closed (the single-orchestrator analogue of the worker
        ``record_branch_dispatched(child_recoverable=...)``). The DISPATCH-TIME value (the
        at-most-once changed-manifest guard). ``None`` (every non-SUB_AGENT_DISPATCH
        orchestrator + pre-arc markers) → the field is OMITTED so those markers hash/parse
        byte-identically to before.

        ``child_engine_class`` (B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD) — the
        orchestrator analogue of the worker ``record_branch_dispatched(child_engine_class=...)``:
        the child's DISPATCH-TIME ``EngineClass`` value string. Read by
        ``orchestrator_dispatched_child_engine_class`` so the maybe-ran orchestrator gate requires
        the marker engine == the resumed engine (the cross-engine-class swap guard, out-of-family
        Codex [P1]; fail closed on mismatch / ``None``). ``None`` (every non-SUB_AGENT_DISPATCH
        orchestrator + pre-arc markers) → the field is OMITTED so those markers hash/parse
        byte-identically to before.

        ``proceed_unstamped`` marks a PROCEED orchestrator marker that intentionally did NOT write
        the worker ``dispatch-instrumented`` stamp. It is omitted for strict-tier markers and read
        only by the effect-free pristine-window classifier so arbitrary orphaned unstamped markers
        still fail closed."""
        record: dict[str, object] = {"step_id": str(step_id), "step_kind": str(step_kind)}
        if child_recoverable is not None:
            record["child_recoverable"] = bool(child_recoverable)
        if child_engine_class is not None:
            record["child_engine_class"] = str(child_engine_class)
        if proceed_unstamped is not None:
            record["proceed_unstamped"] = bool(proceed_unstamped)
        line = json.dumps(record, sort_keys=True)
        self._append_path(self._orchestrator_dispatched_file(run_key), line)

    def orchestrator_dispatched(self, run_key: str) -> bool:
        """Whether the orchestrator reserve-before-DISPATCH MARKER file exists (any state).

        Presence-only (no readability gate — a torn/unreadable marker still proves dispatch
        BEGAN, the conservative reading: treat as maybe-ran → fail closed). The resume
        classifier consults this ONLY for the ORCHESTRATOR_WORKERS / HIERARCHICAL carriers
        (a PARALLELIZATION peer fan-out never writes it; an orchestrator marker on a
        PARALLELIZATION resume is a changed-topology mismatch → fail closed)."""
        return self._orchestrator_dispatched_file(run_key).exists()

    def orchestrator_dispatched_kind(self, run_key: str) -> str | None:
        """Return the orchestrator's recorded DISPATCH-TIME ``step_kind`` (the single-orchestrator
        analogue of the per-branch ``dispatched_branch_kinds``).

        The maybe-ran re-fire-safety classifier keys on this — the ORIGINAL (dispatch-time) kind
        recorded in the orchestrator marker, NOT the resumed manifest's current kind. This is the
        at-most-once changed-manifest guard: an orchestrator that dispatched as an effect-bearing
        kind and crashed before terminal capture, then is re-supplied at ``steps[0]`` as a
        re-fire-safe kind, must STILL be classified by its original effect-bearing kind (else the
        relaxation would re-dispatch + double-fire the original effect). A marker missing / with an
        unreadable / non-str ``step_kind`` (a pre-v1.81 v1.79-era orchestrator marker, which
        recorded only ``step_id``, or a torn write) maps to ``None`` → the classifier treats it as
        NOT re-fire-safe (fail closed — cannot prove the original kind; the v1.79 behavior
        preserved). Presence remains the v1.79 fail-closed signal (``orchestrator_dispatched``);
        the kind is best-effort."""
        path = self._orchestrator_dispatched_file(run_key)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            raw = record.get("step_kind")
            return raw if isinstance(raw, str) else None
        except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
            # torn / pre-v1.81 / invalid-encoding marker → unknown → fail-closed at the
            # classifier (mirrors dispatched_branch_kinds' torn-marker safety boundary).
            return None

    def orchestrator_dispatched_step_id(self, run_key: str) -> str | None:
        """Return the orchestrator's recorded DISPATCH-TIME ``step_id`` (already stored by
        ``record_orchestrator_dispatched``, the single-orchestrator step-identity).

        Read by the B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING fence-recoverable
        relaxation: a fence-recoverable (TOOL_STEP / MANAGED_AGENTS) orchestrator re-dispatches into
        the runtime effect fence, whose key INCLUDES ``step_id`` — so a maybe-ran orchestrator
        re-supplied at ``steps[0]`` with the SAME kind but a CHANGED ``step_id`` (rename / reorder)
        would compose a DIFFERENT fence key, miss the held claim, and double-fire the original
        effect. The relaxation compares this recorded step_id against the resumed
        ``steps[0].step_id`` and fails closed on mismatch (out-of-family Codex [P1]). A marker
        missing / with an unreadable / non-str ``step_id`` (a torn write) maps to ``None`` →
        mismatch → fail closed."""
        path = self._orchestrator_dispatched_file(run_key)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            raw = record.get("step_id")
            return raw if isinstance(raw, str) else None
        except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
            return None

    def orchestrator_dispatched_child_engine_class(self, run_key: str) -> str | None:
        """Return the orchestrator's recorded DISPATCH-TIME child ``EngineClass`` value
        (B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD).

        The orchestrator analogue of ``dispatched_branch_child_engine_classes``: the maybe-ran
        SUB_AGENT orchestrator gate keys on this — the ORIGINAL (dispatch-time) child engine class —
        and requires it to EQUAL the resumed ``steps[0]`` child engine class (the
        cross-engine-class swap guard, out-of-family Codex [P1]). Without it, a maybe-ran
        orchestrator whose marker recorded one recoverable engine but whose resumed manifest
        supplies a DIFFERENT recoverable engine under the same ``step_id`` passes both boolean
        recoverability gates and re-dispatches against the SAME engine-class-agnostic
        ``compose_child_run_id_seed`` durable store through a DIFFERENT recovery mechanism → the
        RECONCILER CAS at-most-once protection is bypassed. A marker missing / with an unreadable /
        non-str ``child_engine_class`` (a non-SUB_AGENT orchestrator, a pre-arc marker, or a torn
        write) maps to ``None`` → mismatch → fail closed."""
        path = self._orchestrator_dispatched_file(run_key)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            raw = record.get("child_engine_class")
            return raw if isinstance(raw, str) else None
        except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
            return None

    def orchestrator_dispatched_proceed_unstamped(self, run_key: str) -> bool:
        """Whether the orchestrator marker was written by PROCEED without the worker stamp.

        PROCEED writes the orchestrator reserve-before-DISPATCH marker so its own fire→capture
        crash window is visible, but deliberately does not write ``dispatch-instrumented.marker``
        because PROCEED worker fan-out still has no per-worker dispatch markers. This field is the
        narrow provenance bit that lets the resume classifier recover effect-free orchestrators
        without treating arbitrary orphaned unstamped markers as trustworthy."""
        path = self._orchestrator_dispatched_file(run_key)
        if not path.exists():
            return False
        try:
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            return record.get("proceed_unstamped") is True
        except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
            return False

    def orchestrator_subagent_child_recoverable(self, run_key: str) -> bool:
        """Whether the orchestrator's DISPATCH-TIME marker recorded ``child_recoverable=True``.

        B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT — a maybe-ran SUB_AGENT_DISPATCH
        orchestrator is recoverable-by-re-dispatch (re-running the whole fan-out fresh
        re-dispatches the orchestrator, whose child auto-resumes under the deterministic run_id,
        with result-faithful ``final_state`` reconstruction) ONLY if its child was RECOVERABLE at
        dispatch (durable LINEAR or supported fan-out, with nested children recursively
        recoverable, recorded here from the opaque child manifest).
        The marker is the DISPATCH-TIME value (the at-most-once changed-manifest guard — a child
        edited recoverable→non-recoverable between dispatch + resume must STILL be classified by
        the dispatch-time value; the resumed-side half is the CP driver's
        ``_subagent_child_recoverable(steps[0])`` re-check). Absent / ``False`` / torn / pre-arc →
        ``False`` → the classifier fails closed (the #701 decline-mirror; never an auto-recover).
        The single-orchestrator analogue of the per-branch
        ``subagent_child_recoverable_indexes``."""
        path = self._orchestrator_dispatched_file(run_key)
        if not path.exists():
            return False
        try:
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            return record.get("child_recoverable") is True
        except (OSError, UnicodeDecodeError, ValueError, IndexError, KeyError, AttributeError):
            # torn / pre-arc / invalid-encoding marker → not provably recoverable → fail-closed
            # at the classifier (presence ≠ validity).
            return False

    # -- B-FANOUT-OUTPUT-REPLAY PR2: terminal POST_JOIN_SYNTHESIS capture -----
    #
    # The synthesis output is the ONE genuine residual the #719 C9 probe named:
    # non-deterministic (an LLM compose), carries NO ledger `response_hash`, and on
    # a W3 crash (between synthesis-capture and run-finalize) the store is its SOLE
    # authority with nothing to cross-check. So it carries a record-local capture-time
    # SELF-HASH (computed by the CP caller over `step_id` + `output`, the
    # `PauseSnapshot._compute_snapshot_hash` shape) — ONE record, a harness-internal
    # integrity field, NOT a hash-chained second authority and NOT in the §6 chain.
    # On replay the caller recomputes the hash over the read record and fails closed on
    # a mismatch (corruption / tamper). The `step_id` is the material-diff identity (a
    # changed synthesis body on resume → caller fails closed).

    def record_synthesis(
        self,
        run_key: str,
        step_id: str,
        output: Mapping[str, Any],
        self_hash: str,
    ) -> None:
        """Capture the terminal POST_JOIN_SYNTHESIS step output to its own durable file.

        Mirrors `record_orchestrator` (a dedicated ``synthesis.jsonl`` under the branches
        dir, no collision with the ``branch-*`` / ``orchestrator`` files) but ALSO records
        the record-local capture-time ``self_hash``. RESERVE-before-COMMIT: the caller fsyncs
        this BEFORE the synthesis terminal ledger-append, so a crash that committed the ledger
        entry always finds the captured output on resume. Idempotent (last-wins)."""
        record = {"step_id": str(step_id), "output": dict(output), "self_hash": str(self_hash)}
        line = json.dumps(record, sort_keys=True)
        self._append_path(self._synthesis_file(run_key), line)

    def read_synthesis(self, run_key: str) -> tuple[str, dict[str, Any], str] | None:
        """Return the captured ``(step_id, output, self_hash)`` synthesis record, or None.

        None means ABSENT (no synthesis captured — a fresh first dispatch, OR a crash BEFORE
        the synthesis ran). A present-but-unreadable synthesis file is surfaced by
        `synthesis_present` so the caller fails closed (the symmetric of the per-branch /
        orchestrator corrupt-detection)."""
        path = self._synthesis_file(run_key)
        if not path.exists():
            return None
        return self._read_last_synthesis_record(path)

    def synthesis_present(self, run_key: str) -> bool:
        """Whether a synthesis journal FILE exists (regardless of readability).

        The fail-closed discriminator: a synthesis file that EXISTS but yields no readable
        record (`read_synthesis` returns None) is a corrupt capture the caller fails closed on
        — never re-dispatching a fresh (non-reproducible) synthesis that would mask the
        corruption."""
        return self._synthesis_file(run_key).exists()

    def record_reconciler_fanout_resume_finalized(self, run_key: str) -> None:
        """Mark that a RECONCILER fan-out resume committed past the strategy finish boundary.

        Branch and synthesis records are reserve-before-commit sidecars; their completeness does
        not prove the resumed run finalized. This post-finish marker is the only fan-out sidecar
        the driver uses to skip a duplicate RECONCILER CAS on an idempotent re-drive.
        """
        line = json.dumps({"reconciler_fanout_resume_finalized": True}, sort_keys=True)
        self._append_path(self._reconciler_fanout_resume_finalized_file(run_key), line)

    def reconciler_fanout_resume_finalized(self, run_key: str) -> bool:
        """Whether the RECONCILER fan-out resume finalized marker exists."""
        return self._reconciler_fanout_resume_finalized_file(run_key).exists()

    # -- durable journal I/O (mirrors JournalWorkflowPauseStore) --------------

    @staticmethod
    def _digest(run_key: str) -> str:
        """The filesystem-safe, collision-free per-run name component."""
        return hashlib.sha256(run_key.encode("utf-8")).hexdigest()

    def _journal_file(self, run_key: str) -> Path:
        """The per-run LINEAR journal file (filesystem-safe, collision-free name)."""
        return self._journal_dir / f"{self._digest(run_key)}.jsonl"

    def _branches_dir(self, run_key: str) -> Path:
        """The per-run directory holding the CONCURRENT fan-out branch journals."""
        return self._journal_dir / f"{self._digest(run_key)}{ENGINE_OUTPUT_BRANCHES_SUFFIX}"

    def _branch_file(self, run_key: str, branch_index: int) -> Path:
        """The per-branch journal file under the run's branches dir."""
        return self._branches_dir(run_key) / f"branch-{int(branch_index)}.jsonl"

    def _orchestrator_file(self, run_key: str) -> Path:
        """The ORCHESTRATOR_WORKERS ``steps[0]`` journal under the branches dir."""
        return self._branches_dir(run_key) / "orchestrator.jsonl"

    def _cardinality_file(self, run_key: str) -> Path:
        """The per-run fan-out CARDINALITY marker under the branches dir."""
        return self._branches_dir(run_key) / "cardinality.jsonl"

    def _synthesis_file(self, run_key: str) -> Path:
        """The terminal POST_JOIN_SYNTHESIS step journal under the branches dir."""
        return self._branches_dir(run_key) / "synthesis.jsonl"

    def _reconciler_fanout_resume_finalized_file(self, run_key: str) -> Path:
        """The post-finish marker for RECONCILER fan-out resume CAS idempotence."""
        return self._branches_dir(run_key) / "reconciler-fanout-resume-finalized.marker"

    def _branch_dispatched_file(self, run_key: str, branch_index: int) -> Path:
        """The per-branch reserve-before-DISPATCH marker file under the run's branches dir."""
        return self._branches_dir(run_key) / f"branch-{int(branch_index)}.dispatched"

    def _dispatch_instrumented_file(self, run_key: str) -> Path:
        """The per-run dispatch-instrumented STAMP file (the cross-version guard)."""
        return self._branches_dir(run_key) / "dispatch-instrumented.marker"

    def _orchestrator_dispatched_file(self, run_key: str) -> Path:
        """The orchestrator reserve-before-DISPATCH marker file under the run's branches dir."""
        return self._branches_dir(run_key) / "orchestrator.dispatched"

    @staticmethod
    def _branch_index_from_name(name: str) -> int | None:
        """Parse ``branch-{n}.jsonl`` → ``n``; None for any other filename."""
        prefix, suffix = "branch-", ".jsonl"
        if not (name.startswith(prefix) and name.endswith(suffix)):
            return None
        try:
            return int(name[len(prefix) : -len(suffix)])
        except ValueError:
            return None

    @staticmethod
    def _dispatched_index_from_name(name: str) -> int | None:
        """Parse ``branch-{n}.dispatched`` → ``n``; None for any other filename."""
        prefix, suffix = "branch-", ".dispatched"
        if not (name.startswith(prefix) and name.endswith(suffix)):
            return None
        try:
            return int(name[len(prefix) : -len(suffix)])
        except ValueError:
            return None

    def _read_last_branch_record(self, path: Path) -> tuple[str, dict[str, Any]] | None:
        """Return the last readable ``(step_id, output)`` in a branch/orchestrator file.

        ``None`` when the file is unreadable or holds no parseable record (corrupt) —
        the caller's presence-vs-readability check is the fail-closed gate. A torn
        trailing line is skipped; a later record for the same file wins (idempotent
        re-record), mirroring the linear `read_outputs` last-wins discipline.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        result: tuple[str, dict[str, Any]] | None = None
        for line in text.splitlines():
            if not line.strip():
                continue
            parsed = self._parse_branch_record(line)
            if parsed is not None:
                result = parsed
        return result

    def _read_last_branch_disposition(
        self, path: Path
    ) -> tuple[str, str, dict[str, Any] | None] | None:
        """Return the last readable ``(step_id, terminal_status, output | None)`` in a
        branch file (the disposition-bearing branch reader; the orchestrator stays on the
        2-field `_read_last_branch_record`).

        ``None`` when unreadable / no parseable record (the presence-vs-readability
        fail-closed gate). Torn trailing line skipped; later record wins. ``output`` may be
        ``None`` (a terminal-no-output branch). ``terminal_status`` defaults to ``completed``
        when absent (keeps the parser total)."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        result: tuple[str, str, dict[str, Any] | None] | None = None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(loaded, dict):
                continue
            record = cast("dict[str, object]", loaded)
            step_id = record.get("step_id")
            terminal_status = record.get("terminal_status", "completed")
            output = record.get("output")
            if not isinstance(step_id, str) or not isinstance(terminal_status, str):
                continue
            # An unknown terminal_status is SEMANTICALLY corrupt (tamper / a future schema):
            # treat the record as unreadable so the presence-vs-readability gate surfaces it
            # in the corrupt set → the resume site fails closed, never treating it as a clean
            # `completed` it could replay or skip (out-of-family Codex [P2]).
            # `scoped_aborted` (CP spec v1.74 §1 / runtime spec v1.84 §14.23) is ADDITIVE — a
            # fan-out branch the operator scoped-aborted (`ABORT_BRANCH`): a `completed`-shaped
            # terminal (output is None, never re-dispatched) the CP crash-resume reconstruct
            # must distinguish from a ran-and-errored `completed` to reproduce the in-resume
            # all-abort FAILED across a crash. Recognized here (NOT corrupt) so it flows through
            # `read_branch_records`; omitting it would fail-closed a mixed abort+survivor recovery
            # (`[[closed-schema-extension-enforced-vs-advisory]]` — the guard is ENFORCED).
            if terminal_status not in ("completed", "timed_out", "scoped_aborted"):
                continue
            # A `scoped_aborted` branch is recorded output=None BY CONSTRUCTION (the operator
            # aborted it — nothing folds). A `scoped_aborted` record carrying a non-None output is
            # a MALFORMED / tampered sidecar → treat as corrupt (skip → surfaced by
            # present_branch_indexes → fail closed), NEVER readable: the CP crash-resume seed loop
            # folds any non-None recovered output into `collected` BEFORE marking the ordinal
            # scoped-aborted, so an accepted malformed record would mask an all-abort FAILED as a
            # folded PARTIAL/SUCCESS (`[[durable-recovery-presence-validity-scope]]`: presence ≠
            # validity; out-of-family Codex [P2]).
            if terminal_status == "scoped_aborted" and output is not None:
                continue
            if output is not None and not isinstance(output, dict):
                continue
            result = (step_id, terminal_status, cast("dict[str, Any] | None", output))
        return result

    def _read_last_synthesis_record(self, path: Path) -> tuple[str, dict[str, Any], str] | None:
        """Return the last readable ``(step_id, output, self_hash)`` in the synthesis file.

        ``None`` when unreadable / no parseable record (the presence-vs-readability
        fail-closed gate). Torn trailing line skipped; later record wins (idempotent
        re-record). All three fields are REQUIRED — a record missing ``self_hash`` (a
        pre-PR2 / tampered capture) is treated as unreadable so the caller fails closed
        rather than replaying an un-integrity-checked synthesis."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        result: tuple[str, dict[str, Any], str] | None = None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(loaded, dict):
                continue
            record = cast("dict[str, object]", loaded)
            step_id = record.get("step_id")
            output = record.get("output")
            self_hash = record.get("self_hash")
            if (
                not isinstance(step_id, str)
                or not isinstance(output, dict)
                or not isinstance(self_hash, str)
            ):
                continue
            result = (step_id, cast("dict[str, Any]", output), self_hash)
        return result

    @staticmethod
    def _parse_branch_record(line: str) -> tuple[str, dict[str, Any]] | None:
        """Parse one branch/orchestrator line into ``(step_id, output)`` or ``None``."""
        try:
            loaded = json.loads(line)
            if not isinstance(loaded, dict):
                return None
            record = cast("dict[str, object]", loaded)
            step_id = record["step_id"]
            output = record["output"]
            if not isinstance(step_id, str) or not isinstance(output, dict):
                return None
            return (step_id, cast("dict[str, Any]", output))
        except (ValueError, KeyError, TypeError):
            return None

    def _append(self, run_key: str, line: str) -> None:
        """Append one JSONL record to the run's LINEAR file, durably."""
        self._append_path(self._journal_file(run_key), line)

    def _append_path(self, path: Path, line: str) -> None:
        """Append one JSONL record to ``path``, durably (fsync-ed).

        The record is ``fsync``-ed before returning so a host crash after a
        ``record`` cannot lose an already-written output. Directory-fsyncs persist
        the new dirents (best-effort POSIX). Torn-append self-healing: a leading
        newline separates a prior crash's partial trailing line so it becomes its
        own (skipped) line rather than corrupting the next record. (Mirrors the
        pause-journal hardenings caught by out-of-family Codex.)
        """
        journal_dir = path.parent
        # The chain of not-yet-existing ancestor directories `mkdir(parents=True)` will
        # create (deepest first). A single mkdir for the first fan-out sidecar write can
        # create BOTH the `{digest}.branches` per-run dir AND the top-level `engine-output`
        # dir; fsyncing only the leaf loses an intermediate dir's dirent on a host crash
        # even though `record_branch` returned (out-of-family Codex [P2]). Each new dir's
        # dirent lives in ITS parent → fsync every new dir's parent below.
        new_dirs: list[Path] = []
        probe = journal_dir
        while not probe.exists():
            new_dirs.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent
        journal_dir.mkdir(parents=True, exist_ok=True)
        is_new_file = not path.exists()
        needs_leading_newline = (not is_new_file) and self._last_byte_is_not_newline(path)
        with path.open("a", encoding="utf-8") as handle:
            if needs_leading_newline:
                handle.write("\n")
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if is_new_file:
            self._fsync_dir(journal_dir)
        for new_dir in new_dirs:
            self._fsync_dir(new_dir.parent)

    @staticmethod
    def _last_byte_is_not_newline(path: Path) -> bool:
        """Return ``True`` iff the file's last byte is not ``\\n`` (a torn append)."""
        try:
            with path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except OSError:
            return True

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """fsync a directory so a freshly-created file's dirent is durable.

        Best-effort: directory fsync is unsupported on some platforms/filesystems,
        where it is a no-op rather than a failure.
        """
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    @staticmethod
    def _parse_record(line: str) -> tuple[int, str, dict[str, Any]] | None:
        """Parse one journal line into ``(step_index, step_id, output)`` or ``None``.

        A corrupt / torn line returns ``None`` (skipped) — fail soft per-line; the
        caller's prefix-completeness check is the fail-closed gate for a MISSING
        committed step.
        """
        try:
            loaded = json.loads(line)
            if not isinstance(loaded, dict):
                return None
            record = cast("dict[str, object]", loaded)
            step_index = record["step_index"]
            step_id = record["step_id"]
            output = record["output"]
            if (
                not isinstance(step_index, int)
                or isinstance(step_index, bool)
                or not isinstance(step_id, str)
                or not isinstance(output, dict)
            ):
                return None
            return (step_index, step_id, cast("dict[str, Any]", output))
        except (ValueError, KeyError, TypeError):
            return None
