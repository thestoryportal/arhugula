"""B-ENGINE-OUTPUT-REPLAY — durable per-run output store unit tests.

Verifies the crash-survivable journal mechanics (round-trip across a fresh store
instance = a process restart; per-step keying; last-wins; torn-append skip + the
self-healing leading newline) in isolation — the substrate the producer +
resume-rehydration compose with. Mirrors `test_r_cc_1_api_resume.py`'s durable-
store unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness_runtime.lifecycle.engine_output_store import (
    EngineOutputStore,
    engine_output_dir_for,
)

_RUN_KEY = "run-idem-key-abc"


def test_record_then_read_round_trip(tmp_path: Path) -> None:
    """record() persists per-step outputs; a FRESH store over the same dir (a
    process restart) reads them back keyed by step_index."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record(_RUN_KEY, 0, "step-0", {"role": "gen", "v": 1})
    store.record(_RUN_KEY, 1, "step-1", {"role": "eval", "v": 2})

    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # restart
    outputs = fresh.read_outputs(_RUN_KEY)
    assert outputs == {
        0: ("step-0", {"role": "gen", "v": 1}),
        1: ("step-1", {"role": "eval", "v": 2}),
    }


def test_missing_run_returns_empty(tmp_path: Path) -> None:
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.read_outputs("never-recorded") == {}


def test_last_record_per_step_index_wins(tmp_path: Path) -> None:
    """An idempotent re-record of the same step_index (e.g. a resume re-write)
    yields the latest output (last-wins)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record(_RUN_KEY, 0, "step-0", {"v": "first"})
    store.record(_RUN_KEY, 0, "step-0", {"v": "second"})
    assert store.read_outputs(_RUN_KEY) == {0: ("step-0", {"v": "second"})}


def test_per_run_isolation(tmp_path: Path) -> None:
    """Each run_key has a dedicated file; a read for one run never returns another's."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record("run-a", 0, "s0", {"r": "a"})
    store.record("run-b", 0, "s0", {"r": "b"})
    assert store.read_outputs("run-a") == {0: ("s0", {"r": "a"})}
    assert store.read_outputs("run-b") == {0: ("s0", {"r": "b"})}


def test_torn_trailing_line_is_skipped_not_corrupting(tmp_path: Path) -> None:
    """A crash mid-append leaves a partial trailing line with no newline; the next
    record's leading-newline self-heal makes the fragment its own (skipped) line,
    so the prior committed records + the new clean record are all readable."""
    journal_dir = tmp_path / "eo"
    store = EngineOutputStore(journal_dir=journal_dir)
    store.record(_RUN_KEY, 0, "step-0", {"v": 0})
    # Simulate a crash mid-append: a partial record with NO trailing newline.
    import hashlib

    digest = hashlib.sha256(_RUN_KEY.encode("utf-8")).hexdigest()
    journal_file = journal_dir / f"{digest}.jsonl"
    with journal_file.open("a", encoding="utf-8") as handle:
        handle.write('{"step_index": 1, "step_id": "TORN-NO-NEWLINE')
    # A clean record after the torn append must be readable + not corrupt step-0.
    store.record(_RUN_KEY, 1, "step-1", {"v": 1})
    outputs = store.read_outputs(_RUN_KEY)
    assert outputs[0] == ("step-0", {"v": 0})
    assert outputs[1] == ("step-1", {"v": 1})


def test_corrupt_line_skipped_committed_records_survive(tmp_path: Path) -> None:
    """A garbage middle line is skipped; the surrounding committed records survive
    (per-line fail-soft; the resume-site prefix-completeness check is the
    fail-closed gate for a MISSING committed step)."""
    journal_dir = tmp_path / "eo"
    store = EngineOutputStore(journal_dir=journal_dir)
    store.record(_RUN_KEY, 0, "step-0", {"v": 0})
    import hashlib

    digest = hashlib.sha256(_RUN_KEY.encode("utf-8")).hexdigest()
    journal_file = journal_dir / f"{digest}.jsonl"
    with journal_file.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all}\n")
    store.record(_RUN_KEY, 2, "step-2", {"v": 2})
    outputs = store.read_outputs(_RUN_KEY)
    assert outputs == {0: ("step-0", {"v": 0}), 2: ("step-2", {"v": 2})}
    # The gap at index 1 is the corruption the rehydrate site fails closed on.
    assert 1 not in outputs


def test_journal_exists_discriminates_absent_from_recorded(tmp_path: Path) -> None:
    """`journal_exists` is the rehydrate discriminator: False before any record (a
    config flip → degrade), True after (a recorded run; if read then yields nothing
    → unreadable → fail-closed)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.journal_exists(_RUN_KEY) is False
    store.record(_RUN_KEY, 0, "step-0", {"v": 0})
    assert store.journal_exists(_RUN_KEY) is True
    # A FRESH store over the same dir (a restart) also sees the existing journal.
    assert EngineOutputStore(journal_dir=tmp_path / "eo").journal_exists(_RUN_KEY) is True


def test_engine_output_dir_for_co_locates_under_state_ledger(tmp_path: Path) -> None:
    assert engine_output_dir_for(tmp_path) == tmp_path / "engine-output"


# -- B-FANOUT-OUTPUT-REPLAY: concurrent-fan-out branch sidecar -----------------
#
# The STORE is the sole authority for which-branches-completed on a fan-out
# crash-resume (the durable ledger is binary for a concurrent fan-out). These
# verify per-branch capture/read, the per-branch-FILE isolation that lets N
# concurrent writers avoid contention, the present-vs-readable fail-closed
# discriminator, and the orchestrator-output capture.


def test_record_branch_then_read_round_trip(tmp_path: Path) -> None:
    """record_branch() persists per-branch outputs keyed by branch_index; a FRESH
    store over the same dir (a crash + fresh-bootstrap resume) reads them back."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "worker-0", "completed", {"out": "b0"})
    store.record_branch(_RUN_KEY, 2, "worker-2", "completed", {"out": "b2"})

    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.read_branch_records(_RUN_KEY) == {
        0: ("worker-0", "completed", {"out": "b0"}),
        2: ("worker-2", "completed", {"out": "b2"}),
    }
    assert fresh.present_branch_indexes(_RUN_KEY) == {0, 2}


def test_branch_disposition_round_trip(tmp_path: Path) -> None:
    """The store records the terminal DISPOSITION for every branch — completed-with-output,
    completed-no-output (ran-and-errored), and timed_out — read back across a fresh store
    (crash + restart). The disposition is what closes the at-most-once recovery class."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"out": 0})
    store.record_branch(_RUN_KEY, 1, "w1", "completed", None)  # ran-and-errored (no output)
    store.record_branch(_RUN_KEY, 2, "w2", "timed_out", None)  # deadline-cut (ambiguous)

    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.read_branch_records(_RUN_KEY) == {
        0: ("w0", "completed", {"out": 0}),
        1: ("w1", "completed", None),
        2: ("w2", "timed_out", None),
    }


def test_scoped_aborted_disposition_round_trip_not_corrupt(tmp_path: Path) -> None:
    """B-FANOUT-EFFECT-FENCE-SCOPED-ABORT-CRASH-DURABLE (CP spec v1.74 §1 / runtime spec v1.84
    §14.23) — `scoped_aborted` is an ADDITIVE recognized disposition (a branch the operator
    scoped-aborted via ABORT_BRANCH: output None, never re-dispatched, distinct from a
    ran-and-errored `completed`). It round-trips AND is NOT treated as corrupt — without the
    additive accept it would be dropped → surfaced in the fail-closed corrupt set → a mixed
    abort+survivor crash-resume would wrongly fail closed instead of finalizing PARTIAL."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "w0", "scoped_aborted", None)
    store.record_branch(_RUN_KEY, 1, "w1", "completed", {"out": 1})  # a survivor

    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    records = fresh.read_branch_records(_RUN_KEY)
    assert records == {
        0: ("w0", "scoped_aborted", None),
        1: ("w1", "completed", {"out": 1}),
    }
    # NOT corrupt — the scoped-abort branch is a readable terminal, not in the fail-closed set.
    assert fresh.present_branch_indexes(_RUN_KEY) - set(records.keys()) == set()


def test_scoped_aborted_with_output_is_corrupt_fail_closed(tmp_path: Path) -> None:
    """out-of-family Codex [P2]: a `scoped_aborted` record carrying a non-None output is a
    MALFORMED / tampered sidecar (a scoped-abort branch is output=None by construction). It must
    be treated as corrupt (omitted from read_branch_records, surfaced by present_branch_indexes →
    fail closed), NOT readable — else the CP seed loop folds the spurious output into `collected`,
    masking an all-abort FAILED as a folded PARTIAL (`[[durable-recovery-presence-validity-scope]]`:
    presence ≠ validity)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"o": 0})
    store._branch_file(_RUN_KEY, 1).write_text(
        '{"output": {"spurious": true}, "step_id": "w1", "terminal_status": "scoped_aborted"}',
        encoding="utf-8",
    )
    readable = set(store.read_branch_records(_RUN_KEY).keys())
    present = store.present_branch_indexes(_RUN_KEY)
    assert readable == {0}  # the malformed scoped_aborted-with-output record is omitted
    assert present - readable == {1}  # the fail-closed corrupt set


def test_unknown_disposition_is_unreadable_fail_closed(tmp_path: Path) -> None:
    """A parseable record with an UNKNOWN terminal_status (tamper / a future schema) is
    treated as UNREADABLE — omitted from read_branch_records but surfaced by
    present_branch_indexes, so the resume site fails closed (out-of-family Codex [P2])."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"o": 0})
    store._branch_file(_RUN_KEY, 1).write_text(
        '{"output": null, "step_id": "w1", "terminal_status": "bogus"}', encoding="utf-8"
    )
    readable = set(store.read_branch_records(_RUN_KEY).keys())
    present = store.present_branch_indexes(_RUN_KEY)
    assert readable == {0}  # the bogus-disposition record is omitted (unreadable)
    assert present == {0, 1}
    assert present - readable == {1}  # the fail-closed corrupt set


def test_branch_files_are_per_branch_isolated(tmp_path: Path) -> None:
    """Each branch writes its OWN file (no shared-handle contention for N
    concurrent writers); the filename is the branch-index authority."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"r": 0})
    store.record_branch(_RUN_KEY, 1, "w1", "completed", {"r": 1})
    branches_dir = store._branches_dir(_RUN_KEY)
    files = sorted(p.name for p in branches_dir.glob("branch-*.jsonl"))
    assert files == ["branch-0.jsonl", "branch-1.jsonl"]


def test_branch_present_but_unreadable_is_fail_closed_signal(tmp_path: Path) -> None:
    """A present-but-corrupt branch file is OMITTED from read_branch_records but
    SURFACED by present_branch_indexes — the set difference is the corrupt set the
    resume site fails closed on (never silently re-dispatching a corrupt branch)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"r": 0})
    # Branch 1's file exists but holds no parseable record (corruption / tamper).
    store._branch_file(_RUN_KEY, 1).write_text("{ not json", encoding="utf-8")
    present = store.present_branch_indexes(_RUN_KEY)
    readable = set(store.read_branch_records(_RUN_KEY).keys())
    assert present == {0, 1}
    assert readable == {0}
    assert present - readable == {1}  # the fail-closed corrupt set


def test_branch_last_record_wins(tmp_path: Path) -> None:
    """An idempotent re-record of the same branch yields the latest (last-wins)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"v": "first"})
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"v": "second"})
    assert store.read_branch_records(_RUN_KEY) == {0: ("w0", "completed", {"v": "second"})}


def test_orchestrator_capture_and_read(tmp_path: Path) -> None:
    """The ORCHESTRATOR_WORKERS steps[0] output rides a dedicated orchestrator
    record (not a branch file); absent → None, present → (step_id, output)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.read_orchestrator_output(_RUN_KEY) is None
    assert store.orchestrator_present(_RUN_KEY) is False
    store.record_orchestrator(_RUN_KEY, "orch-step", {"plan": "delegate"})
    assert store.read_orchestrator_output(_RUN_KEY) == ("orch-step", {"plan": "delegate"})
    assert store.orchestrator_present(_RUN_KEY) is True
    # The orchestrator record does NOT pollute the worker branch set.
    assert store.read_branch_records(_RUN_KEY) == {}
    assert store.present_branch_indexes(_RUN_KEY) == set()


def test_branch_absent_run_returns_empty(tmp_path: Path) -> None:
    """No fan-out journals (config flip / first run) → empty/None, no error."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.read_branch_records("never") == {}
    assert store.present_branch_indexes("never") == set()
    assert store.read_orchestrator_output("never") is None
    assert store.orchestrator_present("never") is False
    assert store.read_synthesis("never") is None
    assert store.synthesis_present("never") is False


def test_synthesis_capture_and_read_round_trip(tmp_path: Path) -> None:
    """PR2 — the terminal POST_JOIN_SYNTHESIS output rides a dedicated synthesis record
    carrying the record-local self-hash; absent → None, present → (step_id, output, self_hash);
    survives a restart (durable)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.read_synthesis(_RUN_KEY) is None
    assert store.synthesis_present(_RUN_KEY) is False
    store.record_synthesis(_RUN_KEY, "synthesis", {"composed": "agg"}, "abc123")
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # restart
    assert fresh.read_synthesis(_RUN_KEY) == ("synthesis", {"composed": "agg"}, "abc123")
    assert fresh.synthesis_present(_RUN_KEY) is True
    # The synthesis record does NOT pollute the worker branch set or the orchestrator.
    assert fresh.read_branch_records(_RUN_KEY) == {}
    assert fresh.read_orchestrator_output(_RUN_KEY) is None


def test_synthesis_last_record_wins(tmp_path: Path) -> None:
    """An idempotent re-record yields the latest (last-wins), like the branch reader."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_synthesis(_RUN_KEY, "synthesis", {"v": "first"}, "h1")
    store.record_synthesis(_RUN_KEY, "synthesis", {"v": "second"}, "h2")
    assert store.read_synthesis(_RUN_KEY) == ("synthesis", {"v": "second"}, "h2")


def test_synthesis_present_but_unreadable_is_fail_closed_signal(tmp_path: Path) -> None:
    """A present-but-corrupt synthesis file → synthesis_present True but read_synthesis None —
    the discriminator the resume site fails closed on (never a silent fresh re-dispatch)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store._synthesis_file(_RUN_KEY).parent.mkdir(parents=True, exist_ok=True)
    store._synthesis_file(_RUN_KEY).write_text("{ not json", encoding="utf-8")
    assert store.synthesis_present(_RUN_KEY) is True
    assert store.read_synthesis(_RUN_KEY) is None


def test_synthesis_record_missing_self_hash_is_unreadable(tmp_path: Path) -> None:
    """A synthesis record lacking the self_hash field (a pre-PR2 / tampered capture) is
    treated as UNREADABLE — the caller fails closed rather than replaying an
    un-integrity-checked synthesis."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store._synthesis_file(_RUN_KEY).parent.mkdir(parents=True, exist_ok=True)
    store._synthesis_file(_RUN_KEY).write_text(
        json.dumps({"step_id": "synthesis", "output": {"x": 1}}), encoding="utf-8"
    )
    assert store.synthesis_present(_RUN_KEY) is True
    assert store.read_synthesis(_RUN_KEY) is None


def test_branch_sidecar_does_not_collide_with_linear_file(tmp_path: Path) -> None:
    """The linear {digest}.jsonl FILE and the {digest}.branches DIR coexist for the
    same run_key (a SUB_AGENT_DISPATCH parent-linear + fan-out child run)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record(_RUN_KEY, 0, "lin-0", {"x": 1})
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"y": 2})
    assert store.read_outputs(_RUN_KEY) == {0: ("lin-0", {"x": 1})}
    assert store.read_branch_records(_RUN_KEY) == {0: ("w0", "completed", {"y": 2})}
    assert store.journal_exists(_RUN_KEY) is True


# ---------------------------------------------------------------------------
# B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE — reserve-before-dispatch markers +
# the cross-version instrumented stamp (real-store round-trip across a restart).
# ---------------------------------------------------------------------------
def test_dispatched_marker_round_trip_across_restart(tmp_path: Path) -> None:
    """record_branch_dispatched() persists a per-branch marker keyed by branch_index; a FRESH
    store instance (= a process restart) reads it back. Marker presence is the at-most-once
    signal — an absent branch with no marker is provably not-yet-run."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch_dispatched(_RUN_KEY, 0, "w0", "inference-step")
    store.record_branch_dispatched(_RUN_KEY, 2, "w2", "inference-step")
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.present_dispatched_indexes(_RUN_KEY) == {0, 2}


def test_dispatched_branch_kinds_round_trip_across_restart(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION — the marker records the DISPATCH-TIME step
    kind so the maybe-ran re-fire-safety classifier keys on the ORIGINAL kind (the at-most-once
    changed-manifest guard), not the resumed manifest's kind. A FRESH store reads it back."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch_dispatched(_RUN_KEY, 0, "w0", "tool-step")
    store.record_branch_dispatched(_RUN_KEY, 1, "w1", "inference-step")
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.dispatched_branch_kinds(_RUN_KEY) == {0: "tool-step", 1: "inference-step"}
    # Absent → empty (a run where no branch began dispatch).
    assert fresh.dispatched_branch_kinds("other-run") == {}


def test_dispatched_branch_step_ids_round_trip_across_restart(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID — the marker records the
    DISPATCH-TIME step_id (alongside the kind) so the fence-recoverable classifier + the
    reconstruct carrier key on the ORIGINAL step_id (the changed-step_id guard). A FRESH store
    (process restart) reads it back."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch_dispatched(_RUN_KEY, 0, "w0", "tool-step")
    store.record_branch_dispatched(_RUN_KEY, 1, "w1", "managed-agents")
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.dispatched_branch_step_ids(_RUN_KEY) == {0: "w0", 1: "w1"}
    # Absent → empty (a run where no branch began dispatch).
    assert fresh.dispatched_branch_step_ids("other-run") == {}


def test_dispatched_branch_step_ids_torn_marker_maps_to_none(tmp_path: Path) -> None:
    """A torn / invalid-UTF-8 `.dispatched` marker maps to step_id=None (→ the CP fence-recoverable
    classifier fails closed; cannot prove the original fence key), never raising — the index is
    still PRESENT (dispatch began), so the marker is unknown-step_id, not absent (mirrors the
    `dispatched_branch_kinds` torn-marker boundary)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch_dispatched(_RUN_KEY, 0, "w0", "tool-step")
    marker = store._branch_dispatched_file(_RUN_KEY, 0)  # reach the path helper directly
    marker.write_bytes(b"\xff\xfe not valid utf-8")  # torn write: invalid UTF-8 bytes
    assert store.dispatched_branch_step_ids(_RUN_KEY) == {0: None}  # unknown → fail-closed
    assert store.present_dispatched_indexes(_RUN_KEY) == {0}  # but dispatch still PROVABLY began


def test_dispatched_branch_kinds_torn_marker_maps_to_none(tmp_path: Path) -> None:
    """A torn / invalid-UTF-8 `.dispatched` marker maps to kind=None (→ the CP classifier fails
    closed), never raising out of dispatched_branch_kinds (out-of-family Codex [P2]; the index
    is still PRESENT — dispatch began — so the marker is treated as unknown-kind, not absent)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch_dispatched(_RUN_KEY, 0, "w0", "inference-step")
    marker = store._branch_dispatched_file(_RUN_KEY, 0)  # reach the path helper directly
    marker.write_bytes(b"\xff\xfe not valid utf-8")  # torn write: invalid UTF-8 bytes
    assert store.dispatched_branch_kinds(_RUN_KEY) == {0: None}  # unknown kind → fail-closed
    assert store.present_dispatched_indexes(_RUN_KEY) == {0}  # but dispatch still PROVABLY began


def test_dispatched_markers_absent_returns_empty(tmp_path: Path) -> None:
    """No markers recorded → the empty set (a run where no branch began dispatch)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.present_dispatched_indexes(_RUN_KEY) == set()


def test_dispatched_marker_idempotent_last_wins(tmp_path: Path) -> None:
    """A re-recorded marker (a resume re-dispatch of the same branch) is idempotent — the
    branch still appears exactly once in the present set."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch_dispatched(_RUN_KEY, 1, "w1", "inference-step")
    store.record_branch_dispatched(_RUN_KEY, 1, "w1", "inference-step")
    assert store.present_dispatched_indexes(_RUN_KEY) == {1}


def test_dispatch_instrumented_stamp_round_trip(tmp_path: Path) -> None:
    """record_dispatch_instrumented() persists the per-run cross-version stamp; a FRESH store
    reads it back. Absent → False (a pre-arc journal, or a never-stamped run)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.dispatch_instrumented(_RUN_KEY) is False
    store.record_dispatch_instrumented(_RUN_KEY)
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.dispatch_instrumented(_RUN_KEY) is True


def test_orchestrator_dispatched_marker_round_trip_across_restart(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-DISPATCH — record_orchestrator_dispatched() persists the
    orchestrator reserve-before-DISPATCH marker (a single marker per run); a FRESH store instance
    (= a process restart) reads it back. Absent → False (the orchestrator never began dispatch)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.orchestrator_dispatched(_RUN_KEY) is False
    store.record_orchestrator_dispatched(_RUN_KEY, "orch-step", "inference-step")
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.orchestrator_dispatched(_RUN_KEY) is True


def test_orchestrator_dispatched_marker_distinct_from_capture(tmp_path: Path) -> None:
    """The orchestrator .dispatched marker (reserve-before-DISPATCH) is distinct from the
    orchestrator.jsonl terminal capture (reserve-before-COMMIT): the marker can be present while
    the capture is absent — the orchestrator MAYBE-RAN (fired its effect, crashed before capture)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    # dispatched, NOT yet captured
    store.record_orchestrator_dispatched(_RUN_KEY, "orch-step", "inference-step")
    assert store.orchestrator_dispatched(_RUN_KEY) is True
    assert store.orchestrator_present(_RUN_KEY) is False  # no terminal capture → maybe-ran
    store.record_orchestrator(_RUN_KEY, "orch-step", {"plan": "delegate"})  # now captured
    assert store.orchestrator_present(_RUN_KEY) is True
    assert store.orchestrator_dispatched(_RUN_KEY) is True  # both coexist


def test_orchestrator_dispatched_marker_idempotent(tmp_path: Path) -> None:
    """A re-recorded orchestrator marker (a resume re-dispatch) is idempotent — presence-only."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_orchestrator_dispatched(_RUN_KEY, "orch-step", "inference-step")
    store.record_orchestrator_dispatched(_RUN_KEY, "orch-step", "inference-step")
    assert store.orchestrator_dispatched(_RUN_KEY) is True


def test_orchestrator_dispatched_kind_round_trip_across_restart(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION — record_orchestrator_dispatched()
    persists the orchestrator's DISPATCH-TIME kind; a FRESH store (= a restart) reads it via
    orchestrator_dispatched_kind(). Absent run → None (the orchestrator never began dispatch)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.orchestrator_dispatched_kind(_RUN_KEY) is None  # no marker
    store.record_orchestrator_dispatched(_RUN_KEY, "orch-step", "tool-step")
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")  # crash + restart
    assert fresh.orchestrator_dispatched_kind(_RUN_KEY) == "tool-step"
    assert fresh.orchestrator_dispatched_kind("other-run") is None


def test_orchestrator_dispatched_proceed_unstamped_round_trip(tmp_path: Path) -> None:
    """PROCEED-origin unstamped marker provenance is durable and absent by default."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.orchestrator_dispatched_proceed_unstamped(_RUN_KEY) is False
    store.record_orchestrator_dispatched(_RUN_KEY, "orch-step", "inference-step")
    assert store.orchestrator_dispatched_proceed_unstamped(_RUN_KEY) is False
    store.record_orchestrator_dispatched(
        _RUN_KEY,
        "orch-step",
        "inference-step",
        proceed_unstamped=True,
    )
    fresh = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert fresh.orchestrator_dispatched_proceed_unstamped(_RUN_KEY) is True


def test_orchestrator_dispatched_kind_pre_v1_81_marker_maps_to_none(tmp_path: Path) -> None:
    """A pre-v1.81 (v1.79-era) orchestrator marker recorded only {"step_id": ...} with NO
    step_kind. orchestrator_dispatched_kind() maps it to None → the CP classifier treats it as
    NOT re-fire-safe → fail-closed (the v1.79 behavior preserved; never a wrongful re-dispatch)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    marker = store._orchestrator_dispatched_file(_RUN_KEY)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"step_id": "orch-step"}), encoding="utf-8")  # no step_kind
    assert store.orchestrator_dispatched(_RUN_KEY) is True  # presence intact
    assert store.orchestrator_dispatched_kind(_RUN_KEY) is None  # but kind unknown → fail-closed


def test_fanout_cardinality_present_distinguishes_absent_from_torn(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION (Codex R3) — fanout_cardinality_present
    is presence-only (file-exists), distinct from read_fanout_cardinality (which returns None for
    BOTH an absent AND a present-but-torn marker). A torn cardinality marker still proves the run
    advanced past capture → the orchestrator re-fire-safe relaxation must fail closed on it."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    assert store.fanout_cardinality_present(_RUN_KEY) is False  # absent
    assert store.read_fanout_cardinality(_RUN_KEY) is None
    store.record_fanout_cardinality(_RUN_KEY, 4)
    assert store.fanout_cardinality_present(_RUN_KEY) is True  # present + readable
    assert store.read_fanout_cardinality(_RUN_KEY) == 4
    # Torn marker: the file EXISTS but holds no parseable branch_count → read returns None,
    # presence stays True (the corruption signal the classifier keys on).
    torn = EngineOutputStore(journal_dir=tmp_path / "torn")
    cfile = torn._cardinality_file(_RUN_KEY)
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cfile.write_text("{not-json", encoding="utf-8")
    assert torn.fanout_cardinality_present(_RUN_KEY) is True  # present...
    assert torn.read_fanout_cardinality(_RUN_KEY) is None  # ...but torn → unreadable


def test_dispatched_marker_does_not_collide_with_branch_capture(tmp_path: Path) -> None:
    """The .dispatched marker file and the .jsonl terminal-capture file coexist per branch
    (the reserve-before-DISPATCH marker is distinct from the reserve-before-COMMIT capture):
    a branch can be both dispatched AND captured (completed), or dispatched-only (maybe-ran)."""
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record_branch_dispatched(_RUN_KEY, 0, "w0", "inference-step")
    store.record_branch(_RUN_KEY, 0, "w0", "completed", {"y": 2})
    store.record_branch_dispatched(
        _RUN_KEY, 1, "w1", "inference-step"
    )  # dispatched but NOT captured (maybe-ran)
    assert store.present_dispatched_indexes(_RUN_KEY) == {0, 1}
    assert store.read_branch_records(_RUN_KEY) == {0: ("w0", "completed", {"y": 2})}
    # maybe-ran discriminator: dispatched − captured = {1}
    assert store.present_dispatched_indexes(_RUN_KEY) - store.read_branch_records(
        _RUN_KEY
    ).keys() == {1}
