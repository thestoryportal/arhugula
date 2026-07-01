"""B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-FANOUT-CHILD (R-FS-1) — by-execution witness.

Does a re-dispatched FAN-OUT child (PARALLELIZATION / {ESR,WAL,SAVE_POINT,RECONCILER})
reconstruct its AGGREGATE `final_state` at the fan-out crash-resume site
(`_crash_fan_out_resume`, CP workflow_driver — NOT the LINEAR `reconstruct_final_state` seed,
which the concurrent strategies return before) under the deterministic child run_id,
result-faithfully + at-most-once?

This is the load-bearing CHILD-half of the FANOUT-CHILD arc family. The predicate admits fan-out
children backed by the durable replay set {ESR,WAL,SAVE_POINT,RECONCILER} as
re-dispatch-recoverable; the recovery MECHANISM is this: on the parent's re-dispatch the child
re-runs under its deterministic `child_run_id` (a pure function of
`compose_child_run_id_seed`), its captured branches replay from the B-FANOUT-OUTPUT-REPLAY branch
store and its in-flight branches re-dispatch through the child's OWN fan-out maybe-ran machinery.
The CP↔runtime classifier verdict + parity are witnessed at `test_lifecycle_sub_agent_dispatch.py`;
this drives the REAL `compose_child_workflow_runner` -> `execute_workflow` over a `run_key`-respecting
`EngineOutputStore`, two-phase (clean capture -> delete one in-flight branch -> resume under the SAME
deterministic seed), proving the aggregate reconstruction COMPOSITION the arc relies on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from harness_core import PersonaTier, StepID, WorkloadClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import _compute_run_idempotency_key
from harness_cp.workflow_driver_types import RunStatus, StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.child_workflow_runner import compose_child_workflow_runner
from harness_runtime.lifecycle.engine_output_store import EngineOutputStore, engine_output_dir_for
from harness_runtime.lifecycle.sub_agent_dispatch import compose_child_run_id_seed

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-fanout-child")
_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_CHILD_WF = "fanout-child-wf"
_PARENT_KEY = "parent-idem-key-worker-branch-0"
_ENTRY_VERSION = 1


def _manifest(
    engine_class: EngineClass = EngineClass.EVENT_SOURCED_REPLAY,
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=_CHILD_WF,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.SOLO_DEVELOPER,  # PROCEED — survivor harvest on a branch crash
        engine_class=engine_class,
        topology_pattern=TopologyPattern.PARALLELIZATION,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _branch_step(idx: int, kind: StepKind = StepKind.DECLARATIVE_STEP) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(f"branch-{idx}"),
        step_kind=kind,
        step_payload={"index": idx},
    )


class _Ledger:
    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> str:
        self.appends.append((payload, write_key))
        return "appended"

    @property
    def is_genesis(self) -> bool:
        return len(self.appends) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appends)


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[Any] = []

    def emit(self, event_class: Any) -> None:
        self.emits.append(event_class)


class _CountingDispatcher:
    """Echoes `{"branch": index}` and records every dispatched step_id (the fire-once witness)."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = (binding, step_context)
        self.dispatched.append(str(step.step_id))
        return {"branch": int(step.step_payload["index"])}


class _Registry:
    def __init__(self, dispatcher: _CountingDispatcher) -> None:
        self._d = dispatcher

    def lookup(self, step_kind: StepKind) -> _CountingDispatcher:
        _ = step_kind
        return self._d


class _Ctx:
    def __init__(
        self, *, ledger: _Ledger, store: EngineOutputStore, dispatchers: _Registry
    ) -> None:
        import asyncio

        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = _Emitter()
        self.ledger_reader = None
        self.engine_output_store = store
        self.step_dispatchers = dispatchers
        self.tracer_provider = NoOpTracerProvider()
        self.drained_flag = asyncio.Event()
        self.pause_requested_flag = asyncio.Event()
        self.pause_resume_protocol = None
        self.validator_framework = None
        self.tenant_id = None
        self.inter_step_output_channel = None
        self.engine_recovery_loop = None


def _drive(
    store: EngineOutputStore,
    dispatcher: _CountingDispatcher,
    seed: str,
    *,
    kind: StepKind = StepKind.DECLARATIVE_STEP,
    engine_class: EngineClass = EngineClass.EVENT_SOURCED_REPLAY,
) -> Any:
    ctx = _Ctx(ledger=_Ledger(), store=store, dispatchers=_Registry(dispatcher))
    runner = compose_child_workflow_runner(cast(Any, ctx))
    return runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(engine_class),
        steps=[_branch_step(0, kind), _branch_step(1, kind), _branch_step(2, kind)],
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,  # CRASH-resume
        child_run_id_seed=seed,
    )


def test_fanout_child_reconstructs_aggregate_under_deterministic_seed(tmp_path: Path) -> None:
    """The load-bearing witness. Phase 1: a clean fan-out child run captures its 3 branches to the
    store under the seed-derived run_key. Delete branch 1's terminal record (the in-flight-at-crash
    branch). Phase 2: re-drive under the SAME deterministic seed + SAME store (fresh ledger). The
    fan-out crash-resume at `_crash_fan_out_resume` reconstructs: branches 0+2 replay (fire-once,
    recovered from the store), only branch 1 re-dispatches, and the aggregate final_state is
    result-faithful to a clean run."""
    seed = compose_child_run_id_seed(_PARENT_KEY, _CHILD_WF)
    # Deterministic: a parent-crash re-dispatch re-derives the SAME seed (the recovery prerequisite).
    assert compose_child_run_id_seed(_PARENT_KEY, _CHILD_WF) == seed

    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))

    # Phase 1: clean run captures all 3 branches under the seed-derived run_key.
    phase1 = _CountingDispatcher()
    r1 = _drive(store, phase1, seed)
    assert r1.status is RunStatus.SUCCESS
    assert sorted(phase1.dispatched) == ["branch-0", "branch-1", "branch-2"]

    # The branch store is found under the deterministic seed-derived run_key (the load-bearing seam).
    run_key = _compute_run_idempotency_key(seed, _CHILD_WF, extras=(str(_ENTRY_VERSION),))
    assert store.present_branch_indexes(run_key) == {0, 1, 2}

    # Model a branch IN-FLIGHT at the crash: no terminal record landed for branch 1 (its journal
    # file never fsynced — delete it on disk, the faithful "absent disposition" the only
    # re-dispatchable case).
    store._branch_file(run_key, 1).unlink()  # model an in-flight crash (no terminal record)
    assert store.present_branch_indexes(run_key) == {0, 2}

    # Phase 2 (resume): re-drive under the SAME seed + SAME store, FRESH dispatcher + ledger.
    phase2 = _CountingDispatcher()
    r2 = _drive(store, phase2, seed)

    assert r2.status is RunStatus.SUCCESS
    # AT-MOST-ONCE: branches 0+2 were recovered from the store (NOT re-dispatched); only the
    # in-flight branch 1 re-fires.
    assert phase2.dispatched == ["branch-1"]


def test_fanout_child_committed_effect_bearing_branches_not_re_fired(tmp_path: Path) -> None:
    """AT-MOST-ONCE for COMMITTED EFFECTS (advisor assertion #4) — the SAME reconstruction with
    EFFECT-BEARING (TOOL_STEP) branches: the committed branches captured in the store are recovered,
    NOT re-dispatched, so their external effects fire EXACTLY ONCE across the crash+resume. Only the
    in-flight branch (absent disposition — never committed) re-dispatches; that re-dispatch is the
    child's OWN fan-out maybe-ran machinery (the existing B-FANOUT-CRASH-RESUME-MAYBE-RAN family,
    tier-governed), not a re-fire of a committed effect. This is the effect-bearing analogue of the
    DECLARATIVE witness above — the gap the arc closes is the AGGREGATE reconstruction COMPOSITION,
    and a committed TOOL_STEP branch must never re-fire on the child's re-dispatch."""
    seed = compose_child_run_id_seed(_PARENT_KEY, _CHILD_WF)
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))

    phase1 = _CountingDispatcher()
    _drive(store, phase1, seed, kind=StepKind.TOOL_STEP)
    run_key = _compute_run_idempotency_key(seed, _CHILD_WF, extras=(str(_ENTRY_VERSION),))
    store._branch_file(run_key, 1).unlink()  # branch 1 in-flight at crash (no terminal record)

    phase2 = _CountingDispatcher()
    r2 = _drive(store, phase2, seed, kind=StepKind.TOOL_STEP)
    assert r2.status is RunStatus.SUCCESS
    # The COMMITTED effect-bearing branches 0+2 did NOT re-fire (recovered from the store); only the
    # in-flight branch 1 re-dispatched — at-most-once for committed effects across crash+resume.
    assert phase2.dispatched == ["branch-1"]

    # RESULT-FIDELITY: the resumed aggregate matches a clean no-crash run.
    baseline_store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path / "baseline"))
    baseline = _drive(baseline_store, _CountingDispatcher(), "baseline-seed")
    assert r2.final_state is not None and baseline.final_state is not None
    assert r2.final_state.get("branch_outputs") == baseline.final_state.get("branch_outputs")
    assert r2.final_state.get("aggregate") == baseline.final_state.get("aggregate")


def test_fanout_child_save_point_reconstructs_aggregate_under_deterministic_seed(
    tmp_path: Path,
) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-FANOUT-CHILD-SAVE-POINT (R-FS-1) — the load-bearing
    by-execution witness for the SAVE_POINT leg.

    Identical two-phase structure to the ESR/WAL witness above, but the child fan-out runs under
    `engine_class=SAVE_POINT_CHECKPOINT`. This is the CAN-vs-DOES proof advisor demanded: the
    branch-capture producer (`_capture_branch_terminal`, gated ONLY by `_fanout_replay_store`) must
    actually FIRE for a SAVE_POINT fan-out run — before the `_FANOUT_REPLAY_ENGINE_CLASSES` widen the
    gate returned `None` for SAVE_POINT, so Phase 1 captured NOTHING and this test was RED at the
    `present_branch_indexes == {0, 1, 2}` assertion. With the widen the SAME class-agnostic
    `EngineOutputStore` captures the SAVE_POINT branches and `_crash_fan_out_resume` reconstructs the
    aggregate, recovering committed branches 0+2 and re-dispatching only the in-flight branch 1
    (at-most-once preserved). SAVE_POINT is the §11.2 ABOVE_ENGINE reading — the harness branch store
    is the sole aggregate authority, no engine-owned competing substrate."""
    sp = EngineClass.SAVE_POINT_CHECKPOINT
    seed = compose_child_run_id_seed(_PARENT_KEY, _CHILD_WF)
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))

    # Phase 1: clean SAVE_POINT fan-out run captures all 3 branches (RED before the gate widen —
    # `_fanout_replay_store` returned None for SAVE_POINT → no capture).
    phase1 = _CountingDispatcher()
    r1 = _drive(store, phase1, seed, kind=StepKind.TOOL_STEP, engine_class=sp)
    assert r1.status is RunStatus.SUCCESS
    run_key = _compute_run_idempotency_key(seed, _CHILD_WF, extras=(str(_ENTRY_VERSION),))
    assert store.present_branch_indexes(run_key) == {0, 1, 2}

    # Model branch 1 in-flight at the crash (no terminal record fsynced).
    store._branch_file(run_key, 1).unlink()
    assert store.present_branch_indexes(run_key) == {0, 2}

    # Phase 2 (resume): re-drive under the SAME seed + store. Committed effect-bearing branches 0+2
    # are recovered (NOT re-fired); only in-flight branch 1 re-dispatches.
    phase2 = _CountingDispatcher()
    r2 = _drive(store, phase2, seed, kind=StepKind.TOOL_STEP, engine_class=sp)
    assert r2.status is RunStatus.SUCCESS
    assert phase2.dispatched == ["branch-1"]

    # RESULT-FIDELITY: the resumed SAVE_POINT aggregate matches a clean no-crash SAVE_POINT run.
    baseline_store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path / "baseline"))
    baseline = _drive(
        baseline_store,
        _CountingDispatcher(),
        "baseline-seed",
        kind=StepKind.TOOL_STEP,
        engine_class=sp,
    )
    assert r2.final_state is not None and baseline.final_state is not None
    assert r2.final_state.get("branch_outputs") == baseline.final_state.get("branch_outputs")
    assert r2.final_state.get("aggregate") == baseline.final_state.get("aggregate")


def test_fanout_child_reconciler_reconstructs_aggregate_under_deterministic_seed(
    tmp_path: Path,
) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-FANOUT-CHILD-RECONCILER — by-execution witness.

    RECONCILER fan-out children use the same class-agnostic branch-output store for AGGREGATE
    reconstruction as ESR/WAL/SAVE_POINT. The reconciler substrate remains the convergence/CAS
    authority; it does not own the per-branch output map. RED before this close: the fan-out replay
    gate returned None for RECONCILER, so no branch records were captured."""
    rc = EngineClass.RECONCILER_LOOP
    seed = compose_child_run_id_seed(_PARENT_KEY, _CHILD_WF)
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))

    phase1 = _CountingDispatcher()
    r1 = _drive(store, phase1, seed, kind=StepKind.TOOL_STEP, engine_class=rc)
    assert r1.status is RunStatus.SUCCESS
    run_key = _compute_run_idempotency_key(seed, _CHILD_WF, extras=(str(_ENTRY_VERSION),))
    assert store.present_branch_indexes(run_key) == {0, 1, 2}

    store._branch_file(run_key, 1).unlink()
    assert store.present_branch_indexes(run_key) == {0, 2}

    phase2 = _CountingDispatcher()
    r2 = _drive(store, phase2, seed, kind=StepKind.TOOL_STEP, engine_class=rc)
    assert r2.status is RunStatus.SUCCESS
    assert phase2.dispatched == ["branch-1"]

    baseline_store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path / "baseline"))
    baseline = _drive(
        baseline_store,
        _CountingDispatcher(),
        "baseline-seed",
        kind=StepKind.TOOL_STEP,
        engine_class=rc,
    )
    assert r2.final_state is not None and baseline.final_state is not None
    assert r2.final_state.get("branch_outputs") == baseline.final_state.get("branch_outputs")
    assert r2.final_state.get("aggregate") == baseline.final_state.get("aggregate")
