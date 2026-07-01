"""B-FANOUT-OUTPUT-REPLAY — `_determine_fanout_resume` reconstruction unit tests.

R-FS-1 standalone arc B-FANOUT-OUTPUT-REPLAY (operator ratified A — non-attested
branch-index-keyed recovery sidecar). On a mid-fan-out crash the durable F2 ledger
is BINARY (branch terminals buffer + drain ATOMICALLY at the barrier per CP §25.12
D1.b), so the STORE is the SOLE which-branches-completed authority. These verify the
store → synthetic-resume-state reconstruction (net-add #2) in isolation:

- completed = the store's branch keys; the recovered `step_id` comes from the STORE
  (CAPTURE-time identity) so the existing strategy material-diff guard fails closed on
  a changed body;
- integrity = present-vs-readable: a present-but-unreadable branch (or a missing
  orchestrator when workers completed) FAILS CLOSED, never silently re-dispatched.

The strategy-side reuse (threading the reconstructed state through the existing pause-
resume path) is covered by the net-add #3 wiring tests; this isolates the helper.
"""

from __future__ import annotations

import pytest
from harness_core import StepID
from harness_cp.engine_class import EngineClass
from harness_cp.pause_resume_protocol_types import (
    FanOutResumeState,
    PeerFanOutResumeState,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    _determine_fanout_resume,
    _FanOutStoreCorruptError,
    _FanOutStoreOrchestratorMaybeRanError,
    _FanOutStoreTimeoutAmbiguousError,
    _orchestrator_subagent_recoverable,
)
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import FanoutTimeoutDisposition

_RUN_KEY = "run-idem-key-fanout"


def _steps(n: int) -> list[WorkflowStep]:
    """`n` declarative steps — only `len(steps)` is read by the helper."""
    return [
        WorkflowStep(
            step_id=StepID(f"step-{i}"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"i": i},
        )
        for i in range(n)
    ]


class _FakeStore:
    """A minimal `engine_output_store`-shaped stub (the driver duck-types it)."""

    def __init__(
        self,
        *,
        branches: dict[int, tuple[str, dict[str, object] | None]],
        orchestrator: tuple[str, dict[str, object]] | None = None,
        corrupt_branches: tuple[int, ...] = (),
        orchestrator_corrupt: bool = False,
        dispositions: dict[int, str] | None = None,
        cardinality: int | None = None,
        cardinality_present: bool | None = None,
        dispatch_instrumented: bool = False,
        orchestrator_dispatched: bool = False,
        orchestrator_dispatched_kind: str | None = None,
        orchestrator_dispatched_step_id: str | None = None,
        orchestrator_dispatched_proceed_unstamped: bool = False,
        orchestrator_subagent_recoverable: bool = False,
        orchestrator_subagent_engine: str | None = None,
        dispatched_kinds: dict[int, str | None] | None = None,
        synthesis_present: bool = False,
    ) -> None:
        self._branches = dict(branches)
        self._orchestrator = orchestrator
        self._corrupt = set(corrupt_branches)
        self._orchestrator_corrupt = orchestrator_corrupt
        self._cardinality = cardinality
        # Cardinality MARKER presence (presence-only, file-exists) is distinct from the readable
        # value: a present-but-TORN marker exists (present=True) but `read_fanout_cardinality`
        # returns None. Default: present iff a readable value was supplied. Set explicitly to
        # model a torn marker (cardinality_present=True, cardinality=None).
        self._cardinality_present = (
            cardinality_present if cardinality_present is not None else cardinality is not None
        )
        # Per-branch terminal disposition (default "completed"); set "timed_out" or a
        # "completed" with a None output to exercise the disposition-class recovery.
        self._dispositions = dict(dispositions) if dispositions else {}
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-DISPATCH — the per-run dispatch-instrumented
        # stamp + the orchestrator reserve-before-dispatch marker (presence-only).
        self._dispatch_instrumented = dispatch_instrumented
        self._orchestrator_dispatched = orchestrator_dispatched
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION — the orchestrator's
        # DISPATCH-TIME kind recorded in its marker (the at-most-once changed-manifest guard;
        # None = un-recorded / pre-v1.64 marker → NOT re-fire-safe → fail-closed).
        self._orchestrator_dispatched_kind = orchestrator_dispatched_kind
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING — the orchestrator's
        # DISPATCH-TIME step_id (the fence-recoverable same-step_id guard; None = un-recorded /
        # torn marker → mismatch → fail-closed; Codex [P1]).
        self._orchestrator_dispatched_step_id = orchestrator_dispatched_step_id
        self._orchestrator_dispatched_proceed_unstamped = orchestrator_dispatched_proceed_unstamped
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT — whether the orchestrator's
        # SUB_AGENT_DISPATCH child was recoverable at dispatch (the marker; False = absent /
        # non-recoverable → the resume classifier fails closed).
        self._orchestrator_subagent_recoverable = orchestrator_subagent_recoverable
        # B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD — the orchestrator's
        # DISPATCH-TIME child EngineClass value (the cross-engine-class swap guard; None = absent /
        # torn → mismatch → fail-closed).
        self._orchestrator_subagent_engine = orchestrator_subagent_engine
        # B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION / TIMEOUT-REPLAY — the dispatch-time
        # kind recorded in each branch's `.dispatched` marker (the at-most-once changed-
        # manifest guard; None = un-recorded / torn marker → fail-closed at the classifier).
        self._dispatched_kinds = dict(dispatched_kinds) if dispatched_kinds else {}
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION (Codex R4) — a POST_JOIN_SYNTHESIS
        # capture (written last by the fan-out writer); its presence proves the run advanced past
        # the orchestrator phase → a downstream-artifact corruption signal.
        self._synthesis_present = synthesis_present

    def read_branch_records(
        self, run_key: str
    ) -> dict[int, tuple[str, str, dict[str, object] | None]]:
        return {
            bi: (sid, self._dispositions.get(bi, "completed"), out)
            for bi, (sid, out) in self._branches.items()
        }

    def present_branch_indexes(self, run_key: str) -> set[int]:
        return set(self._branches) | self._corrupt

    def read_orchestrator_output(self, run_key: str) -> tuple[str, dict[str, object]] | None:
        return None if self._orchestrator_corrupt else self._orchestrator

    def orchestrator_present(self, run_key: str) -> bool:
        return self._orchestrator_corrupt or self._orchestrator is not None

    def read_fanout_cardinality(self, run_key: str) -> int | None:
        return self._cardinality

    def fanout_cardinality_present(self, run_key: str) -> bool:
        return self._cardinality_present

    def dispatch_instrumented(self, run_key: str) -> bool:
        return self._dispatch_instrumented

    def orchestrator_dispatched(self, run_key: str) -> bool:
        return self._orchestrator_dispatched

    def orchestrator_dispatched_kind(self, run_key: str) -> str | None:
        return self._orchestrator_dispatched_kind

    def orchestrator_dispatched_step_id(self, run_key: str) -> str | None:
        return self._orchestrator_dispatched_step_id

    def orchestrator_dispatched_proceed_unstamped(self, run_key: str) -> bool:
        return self._orchestrator_dispatched_proceed_unstamped

    def orchestrator_subagent_child_recoverable(self, run_key: str) -> bool:
        return self._orchestrator_subagent_recoverable

    def orchestrator_dispatched_child_engine_class(self, run_key: str) -> str | None:
        return self._orchestrator_subagent_engine

    def present_dispatched_indexes(self, run_key: str) -> set[int]:
        return set(self._dispatched_kinds)

    def synthesis_present(self, run_key: str) -> bool:
        return self._synthesis_present

    def dispatched_branch_kinds(self, run_key: str) -> dict[int, str | None]:
        return dict(self._dispatched_kinds)


def test_parallelization_reconstructs_peer_resume_from_store() -> None:
    """2 of 3 peer branches completed → a PeerFanOutResumeState with branch_count=3;
    the missing ordinal (1) is absent → left re-dispatchable."""
    store = _FakeStore(branches={0: ("step-0", {"o": 0}), 2: ("step-2", {"o": 2})})
    result = _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.PARALLELIZATION)
    assert isinstance(result, PeerFanOutResumeState)
    assert result.branch_count == 3
    assert {b.branch_index for b in result.branches} == {0, 2}
    assert all(b.terminal_status == "completed" for b in result.branches)
    by_index = {b.branch_index: b for b in result.branches}
    assert by_index[0].output == {"o": 0}
    assert by_index[2].step_id == "step-2"


def test_orchestrator_reconstructs_fan_out_resume_with_orchestrator_output() -> None:
    """2 of 3 workers + the orchestrator completed → a FanOutResumeState carrying the
    recovered orchestrator output + worker_count=3 (steps[1:])."""
    store = _FakeStore(
        branches={0: ("w-0", {"o": 0}), 1: ("w-1", {"o": 1})},
        orchestrator=("orch", {"plan": "delegate"}),
        cardinality=4,
    )
    result = _determine_fanout_resume(
        store, _RUN_KEY, _steps(4), TopologyPattern.ORCHESTRATOR_WORKERS
    )
    assert isinstance(result, FanOutResumeState)
    assert result.worker_count == 3  # len(steps) - 1
    assert result.orchestrator_output == {"plan": "delegate"}
    assert result.orchestrator_step_id == "orch"
    assert {b.branch_index for b in result.branches} == {0, 1}
    assert result.paused_child_branches == ()  # a crash has no paused children


def test_recovered_step_id_comes_from_store_not_current_body() -> None:
    """The store is the CAPTURE-time identity authority: the reconstructed branch
    step_id is the STORE's value (so the strategy material-diff guard can later detect
    a changed body), NOT re-derived from the current steps."""
    store = _FakeStore(branches={0: ("captured-step-id", {"o": 0})})
    result = _determine_fanout_resume(store, _RUN_KEY, _steps(1), TopologyPattern.PARALLELIZATION)
    assert isinstance(result, PeerFanOutResumeState)
    assert result.branches[0].step_id == "captured-step-id"


def test_no_completed_branch_returns_none_fresh_run() -> None:
    """No completed branch in the store → None (the strategy runs fresh, byte-identical)."""
    store = _FakeStore(branches={})
    assert (
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.PARALLELIZATION)
        is None
    )


def test_present_but_unreadable_branch_fails_closed() -> None:
    """A branch file present but unreadable (corruption / tamper) → fail closed, NOT a
    silent re-dispatch (which would mask the corruption + re-fire a landed effect)."""
    store = _FakeStore(branches={0: ("w-0", {"o": 0})}, corrupt_branches=(1,))
    with pytest.raises(_FanOutStoreCorruptError, match="present but unreadable"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.PARALLELIZATION)


def test_torn_cardinality_marker_changed_cardinality_fails_closed() -> None:
    """A PRESENT-but-TORN cardinality marker (read→None because unreadable, present→True) on a
    resume with a CHANGED branch count fails closed, NOT silently dropping the original in-flight
    branches. The consumer distinguishes torn (present) from genuinely-absent via
    `fanout_cardinality_present` — mirroring the orchestrator path + the store author's documented
    `[[durable-recovery-presence-validity-scope]]` intent; a validity-proxy `read→None` would
    conflate torn + absent and skip the changed-cardinality guard. (Regression: the consumer
    previously used `read_fanout_cardinality is not None`, so a torn marker bypassed the guard.)"""
    store = _FakeStore(
        branches={0: ("step-0", {"o": 0})}, cardinality=None, cardinality_present=True
    )
    with pytest.raises(_FanOutStoreCorruptError, match="present but unreadable"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(2), TopologyPattern.PARALLELIZATION)


def test_absent_cardinality_marker_does_not_fail_closed() -> None:
    """CONTROL (the preserved torn-vs-absent distinction) — a GENUINELY-ABSENT cardinality marker
    (read→None, present→False: a pre-cardinality / pre-arc journal) does NOT fail closed on a
    changed branch count; only a TORN (present) marker does. Branch 0 reconstructs normally."""
    store = _FakeStore(
        branches={0: ("step-0", {"o": 0})}, cardinality=None, cardinality_present=False
    )
    result = _determine_fanout_resume(store, _RUN_KEY, _steps(2), TopologyPattern.PARALLELIZATION)
    assert isinstance(result, PeerFanOutResumeState)  # absent ≠ torn → no fail-closed


def test_orchestrator_missing_when_workers_completed_fails_closed() -> None:
    """Workers completed but the orchestrator output is absent — an inconsistent store
    (the orchestrator completes before any worker dispatches) → fail closed."""
    store = _FakeStore(branches={0: ("w-0", {"o": 0})}, orchestrator=None)
    with pytest.raises(_FanOutStoreCorruptError, match="absent but workers completed"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_unreadable_when_workers_completed_fails_closed() -> None:
    """The orchestrator file present-but-unreadable is distinguished from absent in the
    fail-closed diagnostic (consumes the `orchestrator_present` discriminator)."""
    store = _FakeStore(branches={0: ("w-0", {"o": 0})}, orchestrator_corrupt=True)
    with pytest.raises(_FanOutStoreCorruptError, match="present but unreadable"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_recovered_with_zero_workers_completed() -> None:
    """A crash after the orchestrator (`steps[0]`) captured but BEFORE any worker
    completes must STILL recover the orchestrator (it dispatches first) — else
    re-dispatching `steps[0]` double-fires its effect (out-of-family Codex [P1]). The
    resume state carries an EMPTY branch set → every worker re-dispatches fresh."""
    store = _FakeStore(branches={}, orchestrator=("orch", {"plan": "delegate"}), cardinality=4)
    result = _determine_fanout_resume(
        store, _RUN_KEY, _steps(4), TopologyPattern.ORCHESTRATOR_WORKERS
    )
    assert isinstance(result, FanOutResumeState)
    assert result.branches == ()  # zero workers completed
    assert result.orchestrator_output == {"plan": "delegate"}
    assert result.worker_count == 3  # len(steps) - 1


def test_orchestrator_unreadable_with_zero_workers_fails_closed() -> None:
    """A present-but-unreadable orchestrator file fails closed EVEN with zero workers
    completed (corruption / tamper is never silently treated as a fresh run)."""
    store = _FakeStore(branches={}, orchestrator_corrupt=True)
    with pytest.raises(_FanOutStoreCorruptError, match="present but unreadable"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_absent_with_zero_workers_returns_none_fresh() -> None:
    """Nothing captured (orchestrator absent + no worker) → None (fresh run)."""
    store = _FakeStore(branches={}, orchestrator=None)
    assert (
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)
        is None
    )


# --- B-FANOUT-CRASH-RESUME-ORCHESTRATOR-DISPATCH: the orchestrator's own fire→capture window ---
def test_orchestrator_maybe_ran_raises_when_instrumented() -> None:
    """Orchestrator dispatched (marker present) + instrumented stamp + output absent + no worker
    → MAYBE-RAN → fail closed (re-dispatch would risk a double-fire on the strict tiers)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
    )
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="never captured"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_orphaned_marker_without_stamp_fails_closed() -> None:
    """An orchestrator marker present but the run NOT dispatch-instrumented is an INCONSISTENT
    store, NOT a pre-arc journal: the marker is a NEW file written only by the instrumented code
    (strictly AFTER the stamp), so a pre-arc journal carries NO marker and a marker-without-stamp
    can only be corruption / tamper / partial loss → fail closed (the marker's presence alone is
    the maybe-ran signal; never a fresh re-dispatch — out-of-family Codex [P2])."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=False,
        orchestrator_dispatched=True,
    )
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="never captured"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_provably_not_run_returns_none_fresh() -> None:
    """Instrumented but NO orchestrator marker → provably-not-run → None (safe fresh re-run)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=False,
    )
    assert (
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)
        is None
    )


def test_parallelization_orchestrator_marker_topology_mismatch_raises() -> None:
    """A PARALLELIZATION resume whose store holds an orchestrator DISPATCH marker (output absent)
    is a changed-topology resume of a maybe-ran orchestrator → fail closed (the widened guard)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
    )
    with pytest.raises(_FanOutStoreCorruptError, match="topology mismatch"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.PARALLELIZATION)


# --- B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION (R-FS-1, CP spec v1.64 §1) — the
#     re-fire-safe relaxation: a maybe-ran orchestrator of a re-fire-safe dispatch-time kind
#     re-runs fresh; an effect-bearing / un-kinded one stays fail-closed ----------------------
@pytest.mark.parametrize("kind", [StepKind.INFERENCE_STEP, StepKind.DECLARATIVE_STEP])
def test_orchestrator_maybe_ran_re_fire_safe_kind_returns_none_fresh(kind: StepKind) -> None:
    """A maybe-ran orchestrator whose recorded DISPATCH-TIME kind is re-fire-safe
    (INFERENCE_STEP / DECLARATIVE_STEP — no external effect) re-dispatches fresh: None (the
    unchanged fresh re-run), NOT a fail-closed. The common ORCHESTRATOR_WORKERS shape (steps[0]
    is an LLM that farms sub-tasks → INFERENCE_STEP) now recovers from its own fire→capture
    window instead of failing the strict-tier crash-resume closed."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=kind.value,
    )
    assert (
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)
        is None
    )


def _orch_steps(
    orch_kind: StepKind, *, orch_step_id: str = "step-0", n: int = 3
) -> list[WorkflowStep]:
    """`n` steps where `steps[0]` (the orchestrator) carries `orch_kind` + `orch_step_id`."""
    steps = _steps(n)
    steps[0] = WorkflowStep(
        step_id=StepID(orch_step_id), step_kind=orch_kind, step_payload={"i": 0}
    )
    return steps


@pytest.mark.parametrize("kind", [StepKind.TOOL_STEP, StepKind.MANAGED_AGENTS])
def test_orchestrator_maybe_ran_fence_recoverable_same_kind_recovers(kind: StepKind) -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (R-FS-1) — a maybe-ran
    orchestrator whose DISPATCH-TIME kind is FENCE-RECOVERABLE (TOOL_STEP / MANAGED_AGENTS), with
    the resumed `steps[0]` the SAME kind AND the SAME step_id, RECOVERS: None (re-run fresh → the
    orchestrator re-dispatches into the auto-active fence, at-most-once at the sink). Was: fail
    closed pre-this-arc."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=kind.value,
        orchestrator_dispatched_step_id="step-0",
    )
    assert (
        _determine_fanout_resume(
            store, _RUN_KEY, _orch_steps(kind), TopologyPattern.ORCHESTRATOR_WORKERS
        )
        is None
    )


@pytest.mark.parametrize("kind", [StepKind.TOOL_STEP, StepKind.MANAGED_AGENTS])
def test_orchestrator_maybe_ran_fence_recoverable_changed_step_id_fails_closed(
    kind: StepKind,
) -> None:
    """The same-step_id guard (out-of-family Codex [P1]) — a fence-recoverable orchestrator
    re-supplied at the SAME kind but a CHANGED step_id (rename / reorder) FAILS CLOSED: the runtime
    fence key includes step_id, so a renamed re-dispatch composes a DIFFERENT fence key, misses the
    held claim, and would double-fire the original effect."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=kind.value,
        orchestrator_dispatched_step_id="step-0",  # marker recorded "step-0"
    )
    # Resumed orchestrator renamed to "step-0-renamed" (same kind) → fence-key mismatch.
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            _orch_steps(kind, orch_step_id="step-0-renamed"),
            TopologyPattern.ORCHESTRATOR_WORKERS,
        )


def test_orchestrator_maybe_ran_sub_agent_dispatch_non_recoverable_child_fails_closed() -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT negative control — a maybe-ran
    SUB_AGENT_DISPATCH orchestrator whose child is NOT re-dispatch-recoverable STAYS fail-closed.
    Here BOTH halves of the dual gate are False: the dispatch-time marker recorded no recoverable
    child (`orchestrator_subagent_recoverable=False`), AND the resumed `steps[0]` opaque payload
    has no child manifest (`_subagent_child_recoverable` → False). A SUB_AGENT orchestrator is
    recursively fenced at its CHILD's tool sinks, so it recovers ONLY when its child can auto-resume
    result-faithfully (a durable engine class {ESR,WAL,SAVE_POINT,RECONCILER} ∧ LINEAR ∧ leaf) —
    else fail closed (PURE_PATTERN_NO_ENGINE + the non-leaf / fan-out-child residuals)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.SUB_AGENT_DISPATCH.value,
        orchestrator_dispatched_step_id="step-0",
        orchestrator_subagent_recoverable=False,
    )
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            _orch_steps(StepKind.SUB_AGENT_DISPATCH),
            TopologyPattern.ORCHESTRATOR_WORKERS,
        )


def _recoverable_orch_steps(
    *,
    orch_step_id: str = "step-0",
    child_kind: StepKind = StepKind.TOOL_STEP,
    engine: EngineClass = EngineClass.EVENT_SOURCED_REPLAY,
) -> list[WorkflowStep]:
    """`steps[0]` is a SUB_AGENT_DISPATCH orchestrator whose opaque payload describes a
    RE-DISPATCH-RECOVERABLE child ({ESR,WAL,SAVE_POINT,RECONCILER} ∧ LINEAR ∧ leaf) →
    `_subagent_child_recoverable` → True. `engine` is the resumed manifest's child engine class
    (compared against the dispatch-time marker by the cross-engine-class swap guard)."""
    steps = _steps(3)
    steps[0] = WorkflowStep(
        step_id=StepID(orch_step_id),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload={
            "child_manifest_entry": {
                "engine_class": engine.value,
                "topology_pattern": TopologyPattern.SINGLE_THREADED_LINEAR.value,
            },
            "child_steps": [{"step_kind": child_kind.value}],
        },
    )
    return steps


def test_orchestrator_maybe_ran_sub_agent_recoverable_child_recovers() -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT (R-FS-1) positive — a maybe-ran
    SUB_AGENT_DISPATCH orchestrator whose child is recoverable BOTH at dispatch (the marker,
    `orchestrator_subagent_recoverable=True`) AND in the resumed manifest
    (`_subagent_child_recoverable(steps[0])` → True), with the SAME step_id, RECOVERS: None
    (re-run the whole fan-out fresh → the orchestrator re-dispatches → its child auto-resumes from
    its durable store under the deterministic run_id, result-faithfully). Was: fail closed."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.SUB_AGENT_DISPATCH.value,
        orchestrator_dispatched_step_id="step-0",
        orchestrator_subagent_recoverable=True,
        orchestrator_subagent_engine=EngineClass.EVENT_SOURCED_REPLAY.value,
    )
    assert (
        _determine_fanout_resume(
            store, _RUN_KEY, _recoverable_orch_steps(), TopologyPattern.ORCHESTRATOR_WORKERS
        )
        is None
    )


def test_orchestrator_maybe_ran_sub_agent_cross_engine_swap_fails_closed() -> None:
    """SAME-ENGINE guard (out-of-family Codex [P1], …-RECONCILER-CHILD arc), orchestrator path — RED
    without the guard. The DISPATCH-TIME marker recorded a RECONCILER child engine but the RESUMED
    steps[0] child engine is SAVE_POINT, both recoverable + SAME step_id: `compose_child_run_id_seed`
    is engine-class-agnostic, so the swap would re-dispatch the orchestrator's child against the SAME
    durable store through SAVE_POINT recovery instead of the RECONCILER CAS path, bypassing
    at-most-once. The same-engine conjunct fails it closed."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.SUB_AGENT_DISPATCH.value,
        orchestrator_dispatched_step_id="step-0",
        orchestrator_subagent_recoverable=True,
        orchestrator_subagent_engine=EngineClass.RECONCILER_LOOP.value,  # marker: RECONCILER
    )
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            # resumed steps[0] child engine: SAVE_POINT (≠ marker RECONCILER)
            _recoverable_orch_steps(engine=EngineClass.SAVE_POINT_CHECKPOINT),
            TopologyPattern.ORCHESTRATOR_WORKERS,
        )


def test_orchestrator_maybe_ran_sub_agent_marker_engine_missing_fails_closed() -> None:
    """A torn / pre-arc orchestrator marker with NO recorded child engine
    (`orchestrator_subagent_engine=None`) fails closed even with both recoverable + same step_id:
    `_orchestrator_dispatched_child_engine` → None → the same-engine conjunct (engine is not None)
    fails."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.SUB_AGENT_DISPATCH.value,
        orchestrator_dispatched_step_id="step-0",
        orchestrator_subagent_recoverable=True,
        orchestrator_subagent_engine=None,  # marker engine un-recorded
    )
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(
            store, _RUN_KEY, _recoverable_orch_steps(), TopologyPattern.ORCHESTRATOR_WORKERS
        )


def test_orchestrator_maybe_ran_sub_agent_recoverable_at_dispatch_not_resumed_fails_closed() -> (
    None
):
    """[P1-b] dual gate — the child was recoverable at DISPATCH (the marker) but the RESUMED
    manifest's `steps[0]` is NON-recoverable (here the default opaque payload has no child
    manifest). The re-dispatch would run the now-non-recoverable child fresh → double-fire /
    suffix-only corruption → fail closed (the resumed-side conjunct)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.SUB_AGENT_DISPATCH.value,
        orchestrator_dispatched_step_id="step-0",
        orchestrator_subagent_recoverable=True,  # marker recoverable...
    )
    # ...but the resumed steps[0] opaque payload is non-recoverable (`_orch_steps` payload {"i":0}).
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            _orch_steps(StepKind.SUB_AGENT_DISPATCH),
            TopologyPattern.ORCHESTRATOR_WORKERS,
        )


def test_orchestrator_maybe_ran_sub_agent_recoverable_at_resumed_not_dispatch_fails_closed() -> (
    None
):
    """[P1-b] dual gate — the resumed manifest's `steps[0]` is recoverable but the DISPATCH-TIME
    marker recorded NO recoverable child (`orchestrator_subagent_recoverable=False`: no durable
    child records to auto-resume from). The dispatch-time marker is the authority → fail closed."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.SUB_AGENT_DISPATCH.value,
        orchestrator_dispatched_step_id="step-0",
        orchestrator_subagent_recoverable=False,  # marker NOT recoverable...
    )
    # ...even though the resumed steps[0] IS recoverable.
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(
            store, _RUN_KEY, _recoverable_orch_steps(), TopologyPattern.ORCHESTRATOR_WORKERS
        )


def test_orchestrator_maybe_ran_sub_agent_changed_step_id_fails_closed() -> None:
    """Same-step_id guard (manifest-stability parity with the worker SUB_AGENT path) — a
    recoverable SUB_AGENT orchestrator re-supplied at a CHANGED step_id (rename / reorder) is a
    DIFFERENT logical orchestrator → fail closed even though both gate halves are recoverable."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.SUB_AGENT_DISPATCH.value,
        orchestrator_dispatched_step_id="step-0",  # marker recorded "step-0"
        orchestrator_subagent_recoverable=True,
    )
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            _recoverable_orch_steps(orch_step_id="step-0-renamed"),
            TopologyPattern.ORCHESTRATOR_WORKERS,
        )


def test_orchestrator_subagent_recoverable_helper_fails_closed_on_store_without_reader() -> None:
    """Out-of-family Codex [P2] — the additive `orchestrator_subagent_child_recoverable` reader is
    read DEFENSIVELY (`getattr`): a store predating v1.86 (or a partial custom/test store) that does
    NOT implement it maps to `False` (fail closed), NEVER an `AttributeError` that would break the
    resume classifier — mirroring the worker `_subagent_recoverable_marker_indexes` boundary."""

    class _StoreWithoutReader:
        pass

    assert _orchestrator_subagent_recoverable(_StoreWithoutReader(), _RUN_KEY) is False


def test_orchestrator_subagent_recoverable_helper_reads_present_reader() -> None:
    """The helper reads the marker when the store DOES implement it (the v1.86 store API)."""

    class _StoreWithReader:
        def orchestrator_subagent_child_recoverable(self, run_key: str) -> bool:
            return True

    assert _orchestrator_subagent_recoverable(_StoreWithReader(), _RUN_KEY) is True


def test_orchestrator_maybe_ran_changed_manifest_kind_keyed_on_marker() -> None:
    """The classification keys on the MARKER's recorded dispatch-time kind, NOT the resumed
    manifest: an orchestrator dispatched as an effect-bearing TOOL_STEP that crashed before
    capture STAYS fail-closed even though the resumed manifest's steps[0] is DECLARATIVE
    (`_steps()` builds DECLARATIVE_STEP steps) — the at-most-once changed-manifest guard."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.TOOL_STEP.value,  # marker says effect-bearing
    )
    # `_steps()` yields DECLARATIVE_STEP (re-fire-safe) — but the MARKER governs → fail closed.
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="effect-bearing"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_maybe_ran_re_fire_safe_kind_without_stamp_fails_closed() -> None:
    """An orphaned/corrupt orchestrator marker — a re-fire-safe recorded kind but NO dispatch-
    instrumented STAMP — is an INCONSISTENT store whose recorded kind cannot be trusted → fail
    closed REGARDLESS of kind (the stamp is written STRICTLY BEFORE the marker, so a
    marker-without-stamp is corruption / tamper / partial loss, never a legitimate re-fire-safe
    re-run — out-of-family Codex [P2], mirroring the worker `_instrumented` gate +
    `[[durable-recovery-presence-validity-scope]]`)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=False,  # NO stamp → orphaned / corrupt store
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.INFERENCE_STEP.value,  # re-fire-safe kind, but...
    )
    with pytest.raises(_FanOutStoreOrchestratorMaybeRanError, match="orphaned dispatch marker"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_maybe_ran_re_fire_safe_proceed_unstamped_recovers() -> None:
    """A PROCEED-origin unstamped marker is the one safe no-stamp exception for effect-free kinds.

    The worker dispatch-instrumented stamp remains absent, so strict-tier worker marker trust is
    not widened; the separate PROCEED provenance bit is required to distinguish this marker from
    an arbitrary orphaned/corrupt unstamped marker."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=False,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.INFERENCE_STEP.value,
        orchestrator_dispatched_proceed_unstamped=True,
    )

    assert (
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)
        is None
    )


def test_orchestrator_absent_with_cardinality_present_is_corrupt_not_maybe_ran() -> None:
    """An orchestrator dispatch marker with NO terminal capture but a PRESENT cardinality marker
    is CORRUPT, not the maybe-ran fire→capture window — the cardinality marker is fsynced AFTER
    record_orchestrator, so its presence PROVES the orchestrator captured → an absent capture
    means the capture was LOST. Fail closed as `_FanOutStoreCorruptError`, NOT a re-fire-safe
    fresh re-run (out-of-family Codex [P2] R2): only an ABSENT cardinality is the true
    pre-cardinality maybe-ran window. Even a re-fire-safe + instrumented marker is corrupt here."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.INFERENCE_STEP.value,  # re-fire-safe, BUT...
        cardinality=4,  # ...cardinality PRESENT → the run advanced past capture → corrupt
    )
    with pytest.raises(_FanOutStoreCorruptError, match="capture was LOST"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(4), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_absent_with_torn_cardinality_marker_is_corrupt() -> None:
    """A present-but-TORN cardinality marker (the file EXISTS but `read_fanout_cardinality`
    returns None) is STILL corruption, not the pre-cardinality maybe-ran window — the marker's
    PRESENCE (not its readability) proves the run advanced past orchestrator capture. The guard
    keys on `fanout_cardinality_present` (presence-only), so a torn marker fails closed even with
    a re-fire-safe + instrumented orchestrator (out-of-family Codex [P2] R3;
    `[[durable-recovery-presence-validity-scope]]`: presence ≠ validity)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.INFERENCE_STEP.value,  # re-fire-safe + instrumented
        cardinality=None,  # read returns None (torn) ...
        cardinality_present=True,  # ... but the marker FILE exists → corrupt, not pre-cardinality
    )
    with pytest.raises(_FanOutStoreCorruptError, match="capture was LOST"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(4), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_absent_with_surviving_worker_dispatch_marker_is_corrupt() -> None:
    """A surviving WORKER dispatch marker with the orchestrator output + cardinality LOST is
    CORRUPT, not the pre-cardinality maybe-ran window — a worker dispatch marker is written only
    AFTER the orchestrator+cardinality phase, so its survival proves the run advanced past
    orchestrator capture (out-of-family Codex [P2] R4). The DEFAULT-DENY guard fails closed on the
    surviving worker marker even with a re-fire-safe + instrumented orchestrator + absent
    cardinality (the partial-loss case the cardinality-presence guard alone missed)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.INFERENCE_STEP.value,  # re-fire-safe + instrumented
        cardinality=None,  # cardinality LOST (absent) ...
        dispatched_kinds={0: StepKind.TOOL_STEP.value},  # ... but a worker dispatch marker survived
    )
    with pytest.raises(_FanOutStoreCorruptError, match="capture was LOST"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(4), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_absent_with_surviving_corrupt_worker_capture_is_corrupt() -> None:
    """A surviving (present-but-unreadable) WORKER branch capture with the orchestrator output
    LOST fails closed CORRUPT. Here the PRE-EXISTING present-but-unreadable branch guard fires
    FIRST (`... branch journal(s) present but unreadable`), before the orchestrator-maybe-ran
    branch — so a corrupt worker capture never reaches the re-fire-safe path. The DEFAULT-DENY
    `present_branch_indexes` downstream check is therefore defensive backup for that pre-existing
    guard (self-complete: a worker capture present, readable OR corrupt, proves advanced-past).
    Either way the result is the same — fail closed, never a fresh re-dispatch."""
    store = _FakeStore(
        branches={},  # nothing READABLE ...
        corrupt_branches=(0,),  # ... but a worker capture file is present-but-unreadable
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.INFERENCE_STEP.value,
    )
    with pytest.raises(_FanOutStoreCorruptError, match="unreadable"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(4), TopologyPattern.ORCHESTRATOR_WORKERS)


def test_orchestrator_absent_with_surviving_synthesis_capture_is_corrupt() -> None:
    """A surviving POST_JOIN_SYNTHESIS capture with the orchestrator output LOST is CORRUPT — the
    synthesis is written LAST (after every worker), so its presence proves the run advanced past
    orchestrator capture. The DEFAULT-DENY guard keys on `synthesis_present` → fail closed even
    with a re-fire-safe + instrumented orchestrator + absent cardinality (the artifact Codex had
    not hit, surfaced by the writer-artifact audit rather than reactively)."""
    store = _FakeStore(
        branches={},
        orchestrator=None,
        dispatch_instrumented=True,
        orchestrator_dispatched=True,
        orchestrator_dispatched_kind=StepKind.INFERENCE_STEP.value,
        synthesis_present=True,  # a post-join synthesis capture survived → advanced-past → corrupt
    )
    with pytest.raises(_FanOutStoreCorruptError, match="capture was LOST"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(4), TopologyPattern.ORCHESTRATOR_WORKERS)


# --- disposition class (the keystone): completed-with-output / completed-no-output /
#     timed-out, across topologies ------------------------------------------------------
def test_timed_out_branch_fails_closed() -> None:
    """A TIMED_OUT branch under the DEFAULT `fanout_timeout_disposition=FAIL_CLOSED` is
    irreducibly ambiguous (a deadline-cut in-flight dispatch may or may not have landed) →
    crash-resume FAILS CLOSED, never a silent re-dispatch. Byte-identical to v1.55 §1
    (the disposition param defaults to FAIL_CLOSED — every existing caller is unchanged)."""
    store = _FakeStore(branches={0: ("w0", {"o": 0})}, dispositions={0: "timed_out"})
    with pytest.raises(_FanOutStoreTimeoutAmbiguousError, match="FAIL_CLOSED"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.PARALLELIZATION)


# --- B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY (R-FS-1, CP spec v1.63 §1) — the operator-set
#     fan-out timeout disposition policy: FAIL_CLOSED / RECOVER_AS_TERMINAL / RE_DISPATCH --
def test_timeout_recover_as_terminal_recovers_degraded_non_contributor() -> None:
    """RECOVER_AS_TERMINAL: a deadline-cut branch is recovered as a `completed`-no-output
    degraded non-contributor (output None → not folded, not re-dispatched). The surviving
    branch is recovered. cascade_policy then governs the degraded reaction (separation of
    concerns, §2). No raise — the run can recover instead of failing closed."""
    store = _FakeStore(
        branches={0: ("w0", {"o": 0}), 1: ("w1", None)},
        dispositions={1: "timed_out"},
    )
    result = _determine_fanout_resume(
        store,
        _RUN_KEY,
        _steps(3),
        TopologyPattern.PARALLELIZATION,
        FanoutTimeoutDisposition.RECOVER_AS_TERMINAL,
    )
    assert isinstance(result, PeerFanOutResumeState)
    by_index = {b.branch_index: b for b in result.branches}
    assert by_index[0].output == {"o": 0}  # survivor recovered
    assert 1 in by_index  # the timed_out branch IS recovered (not excluded)
    assert by_index[1].output is None  # degraded non-contributor (never folded/re-dispatched)


def test_timeout_re_dispatch_re_fire_safe_branch_excluded_for_redispatch() -> None:
    """RE_DISPATCH + a re-fire-safe (DECLARATIVE_STEP) deadline-cut branch → EXCLUDED from
    the recovered tuple so the existing crash-resume re-dispatch path re-runs it fresh
    (a re-fire-safe re-run has no external effect to double-fire). The survivor stays
    recovered; the timed_out re-fire-safe branch is absent → re-dispatchable."""
    store = _FakeStore(
        branches={0: ("w0", {"o": 0}), 1: ("w1", None)},
        dispositions={1: "timed_out"},
        dispatched_kinds={1: StepKind.DECLARATIVE_STEP.value},
    )
    result = _determine_fanout_resume(
        store,
        _RUN_KEY,
        _steps(3),
        TopologyPattern.PARALLELIZATION,
        FanoutTimeoutDisposition.RE_DISPATCH,
    )
    assert isinstance(result, PeerFanOutResumeState)
    assert {b.branch_index for b in result.branches} == {0}  # branch 1 EXCLUDED → re-dispatched


def test_timeout_re_dispatch_effect_bearing_branch_fails_closed() -> None:
    """RE_DISPATCH + an EFFECT-BEARING (TOOL_STEP) deadline-cut branch → FAIL CLOSED: its
    effect may have landed, so re-dispatch would double-fire. At-most-once is the GATE, not
    operator-overridable (the operator selected RE_DISPATCH but it cannot fail-open)."""
    store = _FakeStore(
        branches={0: ("w0", {"o": 0}), 1: ("w1", None)},
        dispositions={1: "timed_out"},
        dispatched_kinds={1: StepKind.TOOL_STEP.value},
    )
    with pytest.raises(_FanOutStoreTimeoutAmbiguousError, match="RE-FIRE-UNSAFE"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            _steps(3),
            TopologyPattern.PARALLELIZATION,
            FanoutTimeoutDisposition.RE_DISPATCH,
        )


def test_timeout_re_dispatch_un_kinded_marker_fails_closed() -> None:
    """RE_DISPATCH + a deadline-cut branch with NO recorded dispatch-time kind (a pre-arc /
    torn marker → None) → FAIL CLOSED. The classifier cannot prove the original kind
    re-fire-safe, so the conservative reading refuses re-dispatch (the changed-manifest
    at-most-once guard reuse, §3)."""
    store = _FakeStore(
        branches={0: ("w0", {"o": 0}), 1: ("w1", None)},
        dispositions={1: "timed_out"},
        dispatched_kinds={},  # no recorded kind for branch 1
    )
    with pytest.raises(_FanOutStoreTimeoutAmbiguousError, match="RE-FIRE-UNSAFE"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            _steps(3),
            TopologyPattern.PARALLELIZATION,
            FanoutTimeoutDisposition.RE_DISPATCH,
        )


def test_timeout_re_dispatch_mixed_safe_and_unsafe_fails_closed() -> None:
    """RE_DISPATCH + a MIXED timed_out set (one re-fire-safe DECLARATIVE + one effect-bearing
    TOOL_STEP) → FAIL CLOSED. ANY effect-bearing deadline-cut branch under RE_DISPATCH forces
    the conservative fail-closed (the unsafe one cannot be re-dispatched)."""
    store = _FakeStore(
        branches={0: ("w0", None), 1: ("w1", None)},
        dispositions={0: "timed_out", 1: "timed_out"},
        dispatched_kinds={0: StepKind.DECLARATIVE_STEP.value, 1: StepKind.TOOL_STEP.value},
    )
    with pytest.raises(_FanOutStoreTimeoutAmbiguousError, match="RE-FIRE-UNSAFE"):
        _determine_fanout_resume(
            store,
            _RUN_KEY,
            _steps(3),
            TopologyPattern.PARALLELIZATION,
            FanoutTimeoutDisposition.RE_DISPATCH,
        )


def test_timeout_orchestrator_re_dispatch_uses_worker_count_bound() -> None:
    """RE_DISPATCH on an ORCHESTRATOR_WORKERS resume bounds the re-fire-safety check by the
    WORKER count (len(steps) - 1, orchestrator-excluded), matching the dispatch-marker
    ordinal scheme. A re-fire-safe timed_out worker is excluded for re-dispatch; the
    orchestrator + survivor stay recovered."""
    store = _FakeStore(
        branches={0: ("w0", {"o": 0}), 1: ("w1", None)},
        orchestrator=("orch", {"plan": "x"}),
        cardinality=3,
        dispositions={1: "timed_out"},
        dispatched_kinds={1: StepKind.INFERENCE_STEP.value},
    )
    result = _determine_fanout_resume(
        store,
        _RUN_KEY,
        _steps(3),
        TopologyPattern.ORCHESTRATOR_WORKERS,
        FanoutTimeoutDisposition.RE_DISPATCH,
    )
    assert isinstance(result, FanOutResumeState)
    assert result.orchestrator_output == {"plan": "x"}
    assert {b.branch_index for b in result.branches} == {0}  # worker 1 EXCLUDED → re-dispatched


def test_errored_no_output_branch_recovered_as_terminal() -> None:
    """A COMPLETED branch with NO output (ran-and-errored, effect LANDED) is recovered as
    TERMINAL (output None → not re-dispatched, not folded), never re-firing the effect."""
    store = _FakeStore(
        branches={0: ("w0", {"o": 0}), 1: ("w1", None)},
        dispositions={1: "completed"},
    )
    result = _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.PARALLELIZATION)
    assert isinstance(result, PeerFanOutResumeState)
    by_index = {b.branch_index: b for b in result.branches}
    assert by_index[1].terminal_status == "completed"
    assert by_index[1].output is None  # errored: terminal, recovered, NOT re-dispatched


def test_parallelization_with_orchestrator_record_fails_closed_changed_topology() -> None:
    """A PARALLELIZATION manifest resuming a run whose store holds an ORCHESTRATOR record
    is a changed-topology resume (the run key does not bind topology) → fail closed rather
    than reinterpret worker records as peers or drop the orchestrator effect (Codex [P2])."""
    store = _FakeStore(branches={0: ("w0", {"o": 0})}, orchestrator=("orch", {"plan": "x"}))
    with pytest.raises(_FanOutStoreCorruptError, match="topology mismatch"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.PARALLELIZATION)


def test_changed_cardinality_fails_closed() -> None:
    """A store that recorded a 3-branch fan-out, resumed with a 1-branch manifest (a changed
    body the surviving-prefix material-diff cannot catch), FAILS CLOSED rather than silently
    dropping the original in-flight branches (out-of-family Codex [P2])."""
    store = _FakeStore(branches={0: ("w0", {"o": 0})}, cardinality=3)
    with pytest.raises(_FanOutStoreCorruptError, match="cardinality mismatch"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(1), TopologyPattern.PARALLELIZATION)


def test_orchestrator_record_without_cardinality_marker_fails_closed() -> None:
    """The orchestrator record is fsynced BEFORE the cardinality marker — a crash between
    them leaves an orchestrator record with NO cardinality, so a changed worker set could
    reuse the old orchestrator output undetected. Fail closed (out-of-family Codex [P2])."""
    store = _FakeStore(
        branches={0: ("w-0", {"o": 0})},
        orchestrator=("orch", {"plan": "x"}),
        cardinality=None,
    )
    with pytest.raises(_FanOutStoreCorruptError, match="cardinality marker is absent"):
        _determine_fanout_resume(store, _RUN_KEY, _steps(3), TopologyPattern.ORCHESTRATOR_WORKERS)
