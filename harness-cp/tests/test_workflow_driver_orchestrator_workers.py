"""B1-impl-8 — U-CP-88 `ORCHESTRATOR_WORKERS` driver strategy (CP plan v2.32 §2.2).

The third non-linear topology strategy: an orchestrator step (steps[0]) is
dispatched first (its action_id parents the fan-out); the worker steps (steps[1:])
fan out concurrently under per-role child contexts; the barrier collects; the
orchestrator composes a deterministic fold. The FIRST `cascade_policy` consumer
(U-CP-85) AND the FIRST role-seam consumer (U-CP-81 `agent_role` + the runtime
read U-RT-114).

`cascade_policy` is resolved from the manifest persona tier (§11.4 D4 tunable:
SOLO→proceed / TEAM→pause / MTC→cascade-cancel), governing the on-worker-failure
reaction (proceed→PARTIAL / pause→PAUSED / cascade-cancel→FAILED).

Acceptance-criterion coverage (Implementation_Plan_Control_Plane_v2_32.md
U-CP-88):
  functional — orchestrator dispatches workers concurrently under per-role child
    contexts; barrier collects; orchestrator composes the final result:
      → test_orchestrator_workers_dispatches_and_composes
      → test_orchestrator_workers_per_role_child_contexts
      → test_orchestrator_workers_orchestrator_action_id_parents_workers
  deterministic-append (branch-index order, NOT completion order):
      → test_orchestrator_workers_persisted_in_branch_index_order
  persisted-branch-causality:
      → test_orchestrator_workers_branch_metadata_causality
  cascade_policy at worker failure (the three flows):
      → test_orchestrator_workers_proceed_partial_on_worker_failure
      → test_orchestrator_workers_pause_paused_on_worker_failure
      → test_orchestrator_workers_cascade_cancel_failed_on_worker_failure
  cascade-cancel idempotency (resume-terminality, obl. 7):
      → test_resume_should_redispatch_terminality
      → test_orchestrator_workers_cascade_cancel_terminal_status_discriminating
  edge cases + lifecycle:
      → test_orchestrator_workers_empty_steps_returns_success
      → test_orchestrator_workers_orchestrator_only_returns_success
      → test_orchestrator_workers_orchestrator_failure_returns_failed
      → test_orchestrator_workers_emits_workflow_start_and_step_boundaries
  live e2e (real IS writer; §6.3 hash chain re-verifies post-drain):
      → test_orchestrator_workers_live_e2e_real_ledger_chain_valid

Authority: `Spec_Control_Plane_v1_32.md` §25.10/§25.11/§25.14/§25.15 +
`Spec_Harness_Runtime_v1.md` v1.48 §2.2/§14.5.3 +
`Implementation_Plan_Control_Plane_v2_32.md` §2.2 (U-CP-88).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from harness_core import PersonaTier, StepID, WorkloadClass
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.hitl_placement import HITLPlacement, HITLPlacementKind
from harness_cp.per_role_catalog import derive_agent_role
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    DriverContext,
    StepDispatcher,
    StepDispatcherRegistry,
    StepKindDispatcherNotBoundError,
    execute_workflow,
    resume_should_redispatch,
)
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_write import (
    WriteResult,
    append_ledger_entry,
    read_ledger,
)

# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------

_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-orchestrator-workers")

# Persona tier → resolved cascade_policy (§11.4 D4 tunable):
#   SOLO_DEVELOPER → proceed ; TEAM_BINDING → pause ; MTC → cascade-cancel.
_PROCEED_TIER = PersonaTier.SOLO_DEVELOPER
_PAUSE_TIER = PersonaTier.TEAM_BINDING
_CASCADE_CANCEL_TIER = PersonaTier.MULTI_TENANT_COMPLIANCE


def _manifest(
    *, workflow_id: str = "wf-ow", persona_tier: PersonaTier = _PROCEED_TIER
) -> WorkflowManifestEntry:
    """An ORCHESTRATOR_WORKERS manifest (engine in scope; admissibility is enforced
    at workflow-binding per §25.10 Invariant 2, NOT re-checked by the driver). The
    persona tier selects the resolved `cascade_policy` flow."""
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.ORCHESTRATOR_WORKERS,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _orchestrator_step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("orchestrator"),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"role": "orchestrator"},
    )


def _worker_step(index: int) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(f"worker-{index}"),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"index": index},
    )


def _steps(n_workers: int) -> list[WorkflowStep]:
    """[orchestrator, worker-0, ..., worker-(n-1)]."""
    return [_orchestrator_step(), *(_worker_step(i) for i in range(n_workers))]


class _RecordingLedger:
    """In-memory `LedgerWriterLike` that records drained appends in order."""

    actor: Actor

    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> Any:
        self.appends.append((payload, write_key))
        return "appended"

    @property
    def is_genesis(self) -> bool:
        return len(self.appends) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appends)


class _RealLedgerWriter:
    """A `LedgerWriterLike` drain sink backed by the REAL IS writer (so dedup,
    timestamp-monotonicity, hash-chain construction, and JSONL persistence are
    all exercised — `verify_chain` then re-verifies the §6.3 chain)."""

    def __init__(self, *, handle: JsonlLedgerHandle, actor: Actor) -> None:
        self._handle = handle
        self.actor = actor
        self.results: list[WriteResult] = []

    def append(self, payload: Any, write_key: Any) -> None:
        self.results.append(append_ledger_entry(self._handle, payload, write_key))

    @property
    def is_genesis(self) -> bool:
        return len(self.results) == 0

    @property
    def entry_count(self) -> int:
        return len(self.results)


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
        self.emits.append(event_class)


class _Ctx:
    """Minimal fake `DriverContext`. The strategy reads
    `procedural_tier_snapshot_resolver` via `getattr(..., None)` — absent here →
    the R-003 sidecar stays `None`."""

    def __init__(self, *, ledger: Any, emitter: _Emitter) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = emitter
        self.drained_flag = asyncio.Event()
        self.pause_requested_flag = asyncio.Event()
        self.pause_resume_protocol = None
        self.ledger_reader = None
        self.tracer_provider = NoOpTracerProvider()
        self.validator_framework = None
        self.tenant_id = None


class _Registry:
    """Binds a single dispatcher for `DECLARATIVE_STEP` (orchestrator + workers
    share the step kind)."""

    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is not StepKind.DECLARATIVE_STEP:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _Registry(dispatcher))


class _OWDispatcher:
    """Echoes a per-step output keyed by `step_id`; records the per-step
    `step_context` (role / branch_index / parent_action_id) it was handed. A
    worker whose `step_id` is in `fail_step_ids` raises (the cascade trigger)."""

    def __init__(self, *, fail_step_ids: set[str] | None = None) -> None:
        self._fail = fail_step_ids or set()
        self.contexts: dict[str, StepExecutionContext] = {}

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.contexts[step_id] = step_context
        if step_id in self._fail:
            raise RuntimeError(f"simulated worker failure at {step_id}")
        return {"role": step_id, "echoed": dict(step.step_payload)}


class _ReverseCompletionWorkerDispatcher:
    """Forces a DETERMINISTIC reverse-index WORKER completion order: worker i waits
    on worker (i+1)'s done-event before completing (no `time.sleep` — a hard sync
    point, not timing-flaky). The orchestrator (step_id "orchestrator") returns
    immediately (it runs first, sequentially, before the fan-out)."""

    def __init__(self, *, n_workers: int) -> None:
        self._events = {i: threading.Event() for i in range(n_workers)}
        self._n = n_workers

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        if str(step.step_id) == "orchestrator":
            return {"role": "orchestrator"}
        idx = int(step.step_payload["index"])
        higher = idx + 1
        if higher < self._n:
            assert self._events[higher].wait(timeout=10.0), f"worker {higher} never completed"
        self._events[idx].set()
        return {"role": f"worker-{idx}", "index": idx}


class _LookupFailFirstRegistry:
    """A registry whose `lookup` RAISES synchronously for a poison step_kind — used
    to deterministically trigger a worker failure BEFORE any dispatch is scheduled,
    so its not-yet-dispatched siblings are cancelled (the empty-buffer → `cancelled`
    post-barrier scan). The orchestrator + ordinary workers use DECLARATIVE_STEP."""

    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.TOOL_STEP:  # the poison kind
            raise StepKindDispatcherNotBoundError(step_kind)
        if step_kind is not StepKind.DECLARATIVE_STEP:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _run(
    *,
    steps: list[WorkflowStep],
    dispatcher: StepDispatcher,
    ledger: Any,
    persona_tier: PersonaTier = _PROCEED_TIER,
    emitter: _Emitter | None = None,
    workflow_id: str = "wf-ow",
    registry: StepDispatcherRegistry | None = None,
) -> Any:
    emitter = emitter if emitter is not None else _Emitter()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter))
    return execute_workflow(
        _manifest(workflow_id=workflow_id, persona_tier=persona_tier),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=registry if registry is not None else _registry(dispatcher),
    )


def _branch_entries(ledger: _RecordingLedger) -> list[Any]:
    """Drained payloads carrying branch_metadata (the worker entries), in order."""
    return [payload for payload, _wk in ledger.appends if payload.branch_metadata is not None]


def _orchestrator_entries(ledger: _RecordingLedger) -> list[Any]:
    """Drained payloads with NO branch_metadata (the orchestrator entry)."""
    return [payload for payload, _wk in ledger.appends if payload.branch_metadata is None]


# ---------------------------------------------------------------------------
# Functional — orchestrator dispatch → per-role workers → barrier → compose
# ---------------------------------------------------------------------------


def test_orchestrator_workers_dispatches_and_composes() -> None:
    """Orchestrator + 3 workers → SUCCESS; the aggregate carries the orchestrator
    output + every worker output keyed by step_id (no silent discard)."""
    ledger = _RecordingLedger()
    result = _run(steps=_steps(3), dispatcher=_OWDispatcher(), ledger=ledger)

    assert result.status is RunStatus.SUCCESS
    assert result.fail_class is None
    assert result.final_state is not None
    assert result.final_state["orchestrator"]["role"] == "orchestrator"
    assert set(result.final_state["worker_outputs"]) == {f"worker-{i}" for i in range(3)}


def test_orchestrator_workers_per_role_child_contexts() -> None:
    """Each worker dispatches under a per-role child context: `agent_role` ==
    its step_id, `branch_index` == its ordinal, descended from the orchestrator's
    action_id (the role seam the runtime read U-RT-114 consumes)."""
    ledger = _RecordingLedger()
    dispatcher = _OWDispatcher()
    _run(steps=_steps(3), dispatcher=dispatcher, ledger=ledger)

    for i in range(3):
        ctx = dispatcher.contexts[f"worker-{i}"]
        # The role the REAL fan-out driver composes on each worker's branch context
        # is exactly the shared B1↔B4 contract `derive_agent_role(step_id)` — the
        # SAME function an operator keys their per_role_bindings catalog on (B4
        # Slice 2). Asserting against the contract (not a re-inlined literal) means a
        # divergence between the driver's derivation and the operator's catalog key
        # would fail HERE, through the real driver — the observed producer↔catalog
        # bridge for "distinct workers get their own per-role binding".
        assert ctx.agent_role == derive_agent_role(StepID(f"worker-{i}"))
        assert ctx.branch_index == i
        assert ctx.parent_action_id == "workflow:wf-ow:step:0"
        # Each worker carries its DECLARED step ordinal (orchestrator=0, workers
        # 1,2,…), NOT the inherited orchestrator step_index 0 — downstream
        # consumers (e.g. the runtime skill-activation hook) key on step_index.
        assert ctx.step_index == i + 1
    # The orchestrator itself has no branch fields (it is the fan-out parent), and
    # is the declared step 0.
    orch = dispatcher.contexts["orchestrator"]
    assert orch.branch_index is None
    assert orch.agent_role is None
    assert orch.step_index == 0
    # All worker step_indexes are distinct (no two workers report as the same step).
    worker_indices = [dispatcher.contexts[f"worker-{i}"].step_index for i in range(3)]
    assert len(set(worker_indices)) == 3


def test_per_step_role_override_replaces_derived_worker_role() -> None:
    """CP spec v1.38 §6.1 (B4 Slice 4) — a per-step `StepOverride.agent_role` on a
    worker REPLACES the B1 fan-out-derived role at the right granularity
    (precedence per-step > derived). By-execution through the real fan-out driver:
    worker-1 carries the operator-assigned role; its non-overridden siblings still
    carry `derive_agent_role(step_id)` (the override is per-step-scoped, not
    leaked across the fan-out). The role is folded onto the single
    `step_context.agent_role` source the runtime read consumes — no second
    dispatch-read authority (§14.5.3 composition-time relaxation)."""
    override_role = AgentRole("audit-specialist")
    manifest = _manifest().model_copy(
        update={
            "per_step_overrides": {
                StepID("worker-1"): StepOverride(
                    step_id=StepID("worker-1"), agent_role=override_role
                )
            }
        }
    )
    ledger = _RecordingLedger()
    dispatcher = _OWDispatcher()
    execute_workflow(
        manifest,
        _steps(3),
        run_id="run-role-fanout",
        ctx=cast(DriverContext, _Ctx(ledger=ledger, emitter=_Emitter())),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
    )
    # worker-1 → the operator override; worker-0/worker-2 → the derived role.
    assert dispatcher.contexts["worker-1"].agent_role == override_role
    assert dispatcher.contexts["worker-0"].agent_role == derive_agent_role(StepID("worker-0"))
    assert dispatcher.contexts["worker-2"].agent_role == derive_agent_role(StepID("worker-2"))
    # The override did NOT collapse the binding's other dimensions: worker-1 still
    # resolves the manifest-default model (override carried role only).
    assert dispatcher.contexts["worker-1"].branch_index == 1


# ---------------------------------------------------------------------------
# R-FS-1 B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — the
# per-step `StepOverride.hitl_placement` override folds (union-by-position) onto
# the worker / orchestrator branch context, keyed from `manifest_entry`
# (the workflow base) so an override on one cell never leaks to a sibling/parent.
# ---------------------------------------------------------------------------


def test_per_step_placement_override_folds_onto_worker_no_sibling_leak() -> None:
    """A per-step `StepOverride.hitl_placement` (NEW position) on worker-1 folds
    onto worker-1's branch context (union); its siblings AND the orchestrator carry
    only the workflow tuple — proving the worker fold is keyed from
    `manifest_entry.hitl_placements` (no cross-worker / parent leak)."""
    workflow_placement = HITLPlacement(position=HITLPlacementKind.PRE_ACTION)
    override_placement = HITLPlacement(position=HITLPlacementKind.SUB_AGENT_BOUNDARY)
    manifest = _manifest().model_copy(
        update={
            "hitl_placements": (workflow_placement,),
            "per_step_overrides": {
                StepID("worker-1"): StepOverride(
                    step_id=StepID("worker-1"), hitl_placement=override_placement
                )
            },
        }
    )
    ledger = _RecordingLedger()
    dispatcher = _OWDispatcher()
    execute_workflow(
        manifest,
        _steps(3),
        run_id="run-placement-fanout",
        ctx=cast(DriverContext, _Ctx(ledger=ledger, emitter=_Emitter())),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
    )
    # worker-1: workflow placement UNION the per-step override.
    assert dispatcher.contexts["worker-1"].hitl_placements == (
        workflow_placement,
        override_placement,
    )
    # siblings + orchestrator: only the workflow tuple (no leak).
    assert dispatcher.contexts["worker-0"].hitl_placements == (workflow_placement,)
    assert dispatcher.contexts["worker-2"].hitl_placements == (workflow_placement,)
    assert dispatcher.contexts["orchestrator"].hitl_placements == (workflow_placement,)


def test_per_step_placement_override_on_orchestrator_does_not_leak_to_workers() -> None:
    """A per-step placement override on the ORCHESTRATOR step folds onto the
    orchestrator's own context (union) but does NOT leak to the workers — the
    workers re-fold from `manifest_entry.hitl_placements`, not the inherited
    orchestrator context."""
    workflow_placement = HITLPlacement(position=HITLPlacementKind.PRE_ACTION)
    override_placement = HITLPlacement(position=HITLPlacementKind.SUB_AGENT_BOUNDARY)
    manifest = _manifest().model_copy(
        update={
            "hitl_placements": (workflow_placement,),
            "per_step_overrides": {
                StepID("orchestrator"): StepOverride(
                    step_id=StepID("orchestrator"), hitl_placement=override_placement
                )
            },
        }
    )
    ledger = _RecordingLedger()
    dispatcher = _OWDispatcher()
    execute_workflow(
        manifest,
        _steps(2),
        run_id="run-placement-orch",
        ctx=cast(DriverContext, _Ctx(ledger=ledger, emitter=_Emitter())),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
    )
    # orchestrator: union; workers: only the workflow tuple (no leak from parent).
    assert dispatcher.contexts["orchestrator"].hitl_placements == (
        workflow_placement,
        override_placement,
    )
    assert dispatcher.contexts["worker-0"].hitl_placements == (workflow_placement,)
    assert dispatcher.contexts["worker-1"].hitl_placements == (workflow_placement,)


def test_orchestrator_workers_orchestrator_action_id_parents_workers() -> None:
    """The orchestrator's persisted action_id is the fan-out parent every worker
    branch descends from (design §6 "workers serialize under the orchestrator's
    parent_action_id")."""
    ledger = _RecordingLedger()
    _run(steps=_steps(2), dispatcher=_OWDispatcher(), ledger=ledger)

    orch_entries = _orchestrator_entries(ledger)
    assert len(orch_entries) == 1
    orchestrator_action_id = str(orch_entries[0].action_id)
    assert orchestrator_action_id == "workflow:wf-ow:step:0"
    for entry in _branch_entries(ledger):
        assert str(entry.branch_metadata.parent_action_id) == orchestrator_action_id


# ---------------------------------------------------------------------------
# Determinism — persisted in branch-index order (NOT completion order)
# ---------------------------------------------------------------------------


def test_orchestrator_workers_persisted_in_branch_index_order() -> None:
    """Workers complete in REVERSE index order, but the drained worker entries
    persist in BRANCH-INDEX order (§25.12 determinism — "first to finish wins" is
    forbidden). The orchestrator entry persists FIRST (the fan-out parent)."""
    ledger = _RecordingLedger()
    _run(
        steps=_steps(3),
        dispatcher=_ReverseCompletionWorkerDispatcher(n_workers=3),
        ledger=ledger,
    )

    # First drained entry is the orchestrator (no branch_metadata).
    assert ledger.appends[0][0].branch_metadata is None
    # Worker entries follow, in branch-index order (each worker: step then terminal).
    branch_indices = [e.branch_metadata.branch_index for e in _branch_entries(ledger)]
    assert branch_indices == [0, 0, 1, 1, 2, 2]


def test_orchestrator_workers_branch_metadata_causality() -> None:
    """Each worker's branch_metadata carries (parent_action_id, branch_index)
    causality; the per-step entry's terminal_status is None, the terminal entry's
    is `completed`."""
    ledger = _RecordingLedger()
    _run(steps=_steps(2), dispatcher=_OWDispatcher(), ledger=ledger)

    by_branch: dict[int, list[Any]] = {}
    for entry in _branch_entries(ledger):
        by_branch.setdefault(entry.branch_metadata.branch_index, []).append(entry)
    assert set(by_branch) == {0, 1}
    for _bi, entries in by_branch.items():
        # step entry (terminal_status None) then terminal entry (completed).
        assert entries[0].branch_metadata.terminal_status is None
        assert entries[1].branch_metadata.terminal_status == "completed"


# ---------------------------------------------------------------------------
# cascade_policy at worker failure — the three flows
# ---------------------------------------------------------------------------


def test_orchestrator_workers_proceed_partial_on_worker_failure() -> None:
    """SOLO persona → proceed: a worker fails → siblings RUN TO COMPLETION → the
    run is PARTIAL (degraded); the completed workers' outputs are salvaged in
    partial_state; the failed worker's step + terminal entries still persist (no
    silent loss)."""
    ledger = _RecordingLedger()
    result = _run(
        steps=_steps(3),
        dispatcher=_OWDispatcher(fail_step_ids={"worker-1"}),
        ledger=ledger,
        persona_tier=_PROCEED_TIER,
    )

    assert result.status is RunStatus.PARTIAL
    assert result.partial_state is not None
    # worker-0 + worker-2 completed and are salvaged; worker-1 (failed) is not.
    assert set(result.partial_state["worker_outputs"]) == {"worker-0", "worker-2"}
    # All three workers' entries persisted (failed worker too — audit-honoring).
    branch_indices = {e.branch_metadata.branch_index for e in _branch_entries(ledger)}
    assert branch_indices == {0, 1, 2}


def test_orchestrator_workers_proceed_records_timed_out_worker_on_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SOLO/proceed: a worker still in-flight when the §25.11 wall-clock deadline
    fires is cancelled mid-dispatch — it MUST record its step + a `timed_out`
    terminal (obl. 3, no silent gap) before the run returns PARTIAL
    (decorrelated-review [P2] regression — the deadline path was previously
    uncovered, so a deadline-cancelled worker buffered nothing)."""
    import harness_cp.workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_FANOUT_BARRIER_DEADLINE_SECONDS", 0.5)
    release = threading.Event()

    class _BlockingDispatcher:
        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            if str(step.step_id) == "worker-0":
                # Block past the deadline (capped so no thread leaks past the test).
                release.wait(timeout=3.0)
            return {"role": str(step.step_id)}

    ledger = _RecordingLedger()
    try:
        result = _run(
            steps=_steps(3),
            dispatcher=cast(StepDispatcher, _BlockingDispatcher()),
            ledger=ledger,
            persona_tier=_PROCEED_TIER,
        )
    finally:
        release.set()

    assert result.status is RunStatus.PARTIAL
    # worker-0 (branch 0) was cancelled mid-dispatch at the deadline → a `timed_out`
    # terminal persisted (obl. 3 — its dispatched effect is NOT a silent gap).
    timed_out = [
        e for e in _branch_entries(ledger) if e.branch_metadata.terminal_status == "timed_out"
    ]
    assert timed_out, "the deadline-cancelled worker must record a timed_out terminal"
    assert any(e.branch_metadata.branch_index == 0 for e in timed_out)


def test_orchestrator_workers_pause_without_protocol_fails_honestly_not_false_paused() -> None:
    """TEAM persona → pause, but NO pause/resume protocol bound: a worker fails →
    the run fails HONESTLY (FAILED + `pause-resume-protocol-not-bound`) rather
    than advertising a non-resumable `PAUSED` (decorrelated-review [P1]/F1-01 —
    the silent-degradation failure mode stays foreclosed).

    B-FANOUT-PAUSE materializes the resumable `pause → PAUSED` ONLY when a
    `pause_resume_protocol` is bound (so a snapshot CAN be captured); without the
    opt-in there is nothing to resume from, so the honest detect-then-refuse is a
    loud FAILED (mirrors `api.resume`'s ResumeProtocolNotBoundError). The completed
    workers' entries STILL persist (no silent loss) + the salvage is carried."""
    ledger = _RecordingLedger()
    result = _run(
        steps=_steps(3),
        dispatcher=_OWDispatcher(fail_step_ids={"worker-0"}),
        ledger=ledger,
        persona_tier=_PAUSE_TIER,
    )

    assert result.status is RunStatus.FAILED
    assert result.fail_class == "orchestrator-workers-pause-resume-protocol-not-bound"
    # No false-PAUSED is ever returned by the fan-out strategy.
    assert result.status is not RunStatus.PAUSED
    # Audit-honoring: the orchestrator + the dispatched workers' entries persisted.
    assert len(_orchestrator_entries(ledger)) == 1
    # The salvaged completed outputs are carried for the operator (no silent loss).
    # pause uses the cascade-cancel barrier, so non-failing siblings are cancelled
    # on the failure — only those that completed CLEANLY before the cancel land in
    # the salvage (which subset is timing-dependent). The invariant: the failed
    # worker is never salvaged, and the salvage ⊆ the non-failing workers.
    assert result.partial_state is not None
    salvaged = set(result.partial_state["worker_outputs"])
    assert "worker-0" not in salvaged
    assert salvaged <= {"worker-1", "worker-2"}


def test_orchestrator_workers_cascade_cancel_failed_on_worker_failure() -> None:
    """MTC persona → cascade-cancel: a worker fails → the run is FAILED; the
    orchestrator + dispatched workers' entries persist (audit-honoring)."""
    ledger = _RecordingLedger()
    result = _run(
        steps=_steps(3),
        dispatcher=_OWDispatcher(fail_step_ids={"worker-0"}),
        ledger=ledger,
        persona_tier=_CASCADE_CANCEL_TIER,
    )

    assert result.status is RunStatus.FAILED
    assert result.fail_class == "orchestrator-workers-cascade-cancel"
    # The orchestrator entry persisted (the fan-out ran).
    assert len(_orchestrator_entries(ledger)) == 1


# ---------------------------------------------------------------------------
# cascade-cancel idempotency (resume-terminality, obl. 7)
# ---------------------------------------------------------------------------


def test_resume_should_redispatch_terminality() -> None:
    """`resume_should_redispatch` (obl. 7): a branch with ANY persisted
    dispatch-boundary terminal disposition MUST NOT be re-dispatched on resume;
    only a `None` (never-reached-a-boundary) branch is re-dispatch-eligible."""
    assert resume_should_redispatch(None) is True
    assert resume_should_redispatch("cancelled") is False
    assert resume_should_redispatch("completed") is False
    assert resume_should_redispatch("timed_out") is False


def test_orchestrator_workers_cascade_cancel_terminal_status_discriminating() -> None:
    """cascade-cancel: a worker that fails BEFORE scheduling any dispatch (here a
    binding/lookup error) → the run is FAILED and its not-yet-dispatched siblings
    record a `cancelled` terminal (the empty-buffer post-barrier scan, obl. 4) so
    `resume_should_redispatch` is False (no double-dispatch on resume)."""
    # The orchestrator + the first worker use DECLARATIVE_STEP; the SECOND worker
    # uses the poison TOOL_STEP kind whose lookup raises synchronously — its
    # coroutine fails before scheduling a dispatch, cascade-cancelling the sibling.
    steps = [
        _orchestrator_step(),
        WorkflowStep(
            step_id=StepID("worker-poison"),
            step_kind=StepKind.TOOL_STEP,
            step_payload={"index": 0},
        ),
        _worker_step(1),
    ]
    ledger = _RecordingLedger()
    dispatcher = _OWDispatcher()
    result = _run(
        steps=steps,
        dispatcher=dispatcher,
        ledger=ledger,
        persona_tier=_CASCADE_CANCEL_TIER,
        registry=cast(StepDispatcherRegistry, _LookupFailFirstRegistry(dispatcher)),
    )

    assert result.status is RunStatus.FAILED
    # Every persisted branch terminal_status is in the discriminating set, and a
    # `cancelled` disposition is present (the not-yet-dispatched workers) →
    # resume-ineligible.
    terminal_statuses = {
        e.branch_metadata.terminal_status
        for e in _branch_entries(ledger)
        if e.branch_metadata.terminal_status is not None
    }
    assert terminal_statuses
    assert terminal_statuses <= {"cancelled", "completed", "timed_out"}
    assert "cancelled" in terminal_statuses
    for status in terminal_statuses:
        assert resume_should_redispatch(status) is False


def test_orchestrator_workers_cascade_cancel_cancelled_only_workers_do_not_inflate_step_count() -> (
    None
):
    """[P2-b] regression — a CASCADE_CANCEL worker cancelled BEFORE dispatch
    buffers ONLY a `cancelled` terminal entry (no STEP entry); it did NOT run a
    step, so it must NOT inflate `workflow.step_count` or emit a `STEP_BOUNDARY`.

    Both workers use the poison TOOL_STEP kind whose `lookup` raises
    synchronously — so NEITHER runs a step regardless of fan-out timing (a
    deterministic all-cancelled-only cell). Only the orchestrator ran a step ⟹
    exactly ONE `STEP_BOUNDARY`. Pre-fix (`entry_count > 0` counting the
    terminal-only buffers as "ran") this would have emitted THREE."""
    steps = [
        _orchestrator_step(),
        WorkflowStep(
            step_id=StepID("worker-poison-0"), step_kind=StepKind.TOOL_STEP, step_payload={}
        ),
        WorkflowStep(
            step_id=StepID("worker-poison-1"), step_kind=StepKind.TOOL_STEP, step_payload={}
        ),
    ]
    ledger = _RecordingLedger()
    emitter = _Emitter()
    dispatcher = _OWDispatcher()
    result = _run(
        steps=steps,
        dispatcher=dispatcher,
        ledger=ledger,
        emitter=emitter,
        persona_tier=_CASCADE_CANCEL_TIER,
        registry=cast(StepDispatcherRegistry, _LookupFailFirstRegistry(dispatcher)),
    )

    assert result.status is RunStatus.FAILED
    assert result.fail_class == "orchestrator-workers-cascade-cancel"
    # Both worker branches buffered ONLY a `cancelled` terminal — NO step entry.
    branch_entries = _branch_entries(ledger)
    assert all(e.branch_metadata.terminal_status == "cancelled" for e in branch_entries)
    assert not any(e.branch_metadata.terminal_status is None for e in branch_entries)
    # The [P2-b] proof: only the orchestrator ran a step → exactly ONE
    # STEP_BOUNDARY (NOT 1 + 2 cancelled-only workers = 3).
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 1


# ---------------------------------------------------------------------------
# Edge cases + lifecycle
# ---------------------------------------------------------------------------


def test_orchestrator_workers_empty_steps_returns_success() -> None:
    """No steps → trivially SUCCESS with an empty aggregate."""
    ledger = _RecordingLedger()
    result = _run(steps=[], dispatcher=_OWDispatcher(), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state == {"orchestrator": {}, "worker_outputs": {}}
    assert ledger.appends == []


def test_orchestrator_workers_orchestrator_only_returns_success() -> None:
    """Orchestrator with NO workers (1 step) → SUCCESS with an empty worker set."""
    ledger = _RecordingLedger()
    result = _run(steps=_steps(0), dispatcher=_OWDispatcher(), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["orchestrator"]["role"] == "orchestrator"
    assert result.final_state["worker_outputs"] == {}
    # Only the orchestrator entry persisted.
    assert len(_orchestrator_entries(ledger)) == 1
    assert _branch_entries(ledger) == []


def test_orchestrator_workers_orchestrator_failure_returns_failed() -> None:
    """The orchestrator step failing (before any worker fan-out) → FAILED; nothing
    is buffered (cascade_policy governs WORKER failure, not the orchestrator)."""
    ledger = _RecordingLedger()
    result = _run(
        steps=_steps(2),
        dispatcher=_OWDispatcher(fail_step_ids={"orchestrator"}),
        ledger=ledger,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "orchestrator-workers-orchestrator-failure" in result.fail_class
    assert ledger.appends == []


def test_orchestrator_workers_emits_workflow_start_and_step_boundaries() -> None:
    """workflow.start emitted once; one STEP_BOUNDARY per persisted-step entity
    (orchestrator + each worker that ran)."""
    ledger = _RecordingLedger()
    emitter = _Emitter()
    _run(steps=_steps(2), dispatcher=_OWDispatcher(), ledger=ledger, emitter=emitter)

    assert emitter.emits.count(WorkflowEventClass.WORKFLOW_START) == 1
    # orchestrator (1) + 2 workers = 3 STEP_BOUNDARY.
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 3


# ---------------------------------------------------------------------------
# Live e2e — real IS writer; §6.3 hash chain re-verifies post-drain
# ---------------------------------------------------------------------------


def test_orchestrator_workers_live_e2e_real_ledger_chain_valid(tmp_path: Path) -> None:
    """ORCHESTRATOR_WORKERS through the REAL IS writer: the orchestrator + worker
    entries persist (dedup / timestamp-monotonicity / hash-chain construction all
    exercised), then `verify_chain` re-verifies the §6.3 chain VALID post-drain."""
    handle = JsonlLedgerHandle(
        canonical_path=tmp_path / "ledger.jsonl", exists=False, entry_count=0
    )
    writer = _RealLedgerWriter(handle=handle, actor=_ACTOR)
    result = _run(steps=_steps(3), dispatcher=_OWDispatcher(), ledger=writer)

    assert result.status is RunStatus.SUCCESS
    # Orchestrator (1) + 3 workers x (step + terminal) = 7 persisted entries.
    assert writer.entry_count == 7
    entries = read_ledger(handle)
    assert verify_chain(entries).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3/§4) — opt-in terminal synthesis step
# ---------------------------------------------------------------------------


class _OWSynthesisCapturingDispatcher:
    """A `POST_JOIN_SYNTHESIS` dispatcher that CAPTURES the branch-index-ordered
    WORKER siblings + returns a synthesized aggregate (stands in for the runtime
    `PostJoinSynthesisStepDispatcher`)."""

    def __init__(self) -> None:
        self.received_siblings: Any = None
        self.received_agent_role: Any = None

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self.received_siblings = step_context.sibling_outputs
        self.received_agent_role = step_context.agent_role
        return {"synthesis": "ow-composed", "n": len(step_context.sibling_outputs)}


class _OWBranchOrSynthesisRegistry:
    """Binds `DECLARATIVE_STEP` (orchestrator + workers) + `POST_JOIN_SYNTHESIS`."""

    def __init__(self, branch: StepDispatcher, synthesis: StepDispatcher) -> None:
        self._branch = branch
        self._synthesis = synthesis

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.DECLARATIVE_STEP:
            return self._branch
        if step_kind is StepKind.POST_JOIN_SYNTHESIS:
            return self._synthesis
        raise StepKindDispatcherNotBoundError(step_kind)


def _ow_synthesis_step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"prompt": "compose"},
    )


def test_orchestrator_workers_post_join_synthesis_replaces_compose_reads_workers() -> None:
    """A terminal POST_JOIN_SYNTHESIS step REPLACES the deterministic orchestrator
    compose on SUCCESS: carved out of the orchestrator+worker set, it receives the
    branch-index-ordered WORKER siblings (NOT the orchestrator, NOT itself) and its
    output is final_state; a disclosing synthesis entry is appended (v1.54 §3/§4)."""
    ledger = _RecordingLedger()
    synth = _OWSynthesisCapturingDispatcher()
    result = _run(
        steps=[*_steps(2), _ow_synthesis_step()],
        dispatcher=_OWDispatcher(),  # unused — the registry overrides it
        ledger=ledger,
        registry=cast(
            StepDispatcherRegistry,
            _OWBranchOrSynthesisRegistry(_OWDispatcher(), synth),
        ),
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state == {"synthesis": "ow-composed", "n": 2}
    # The synthesis read the 2 branch-index-ordered WORKER siblings (worker-0,
    # worker-1) — NOT the orchestrator, NOT the carved synthesis step.
    assert [bi for bi, _o in synth.received_siblings] == [0, 1]
    assert synth.received_siblings[0][1] == {"role": "worker-0", "echoed": {"index": 0}}
    assert any(str(wk.step_id).startswith("post-join-synthesis") for _p, wk in ledger.appends)


def test_orchestrator_workers_without_synthesis_uses_deterministic_compose() -> None:
    """Negative control: absent a synthesis step, the deterministic orchestrator
    compose is byte-identical to pre-v1.54 (no synthesis dispatch, no entry)."""
    ledger = _RecordingLedger()
    result = _run(steps=_steps(2), dispatcher=_OWDispatcher(), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state) == {"orchestrator", "worker_outputs"}
    assert not any(str(wk.step_id).startswith("post-join-synthesis") for _p, wk in ledger.appends)


def test_orchestrator_role_override_does_not_leak_to_synthesis() -> None:
    """Codex round 7 [P2] — for ORCHESTRATOR_WORKERS, `fanout_parent` IS the
    orchestrator context, which carries the ORCHESTRATOR's per-step role override.
    An UNOVERRIDDEN terminal synthesis must NOT inherit that orchestrator role (it
    would mis-route the synthesis under the orchestrator's per-role model). The
    synthesis mirrors the worker pattern: its own override wins, else its own
    step-id-DERIVED role (never the orchestrator's). Both branches witnessed."""
    orch_role = AgentRole("coordinator-special")
    synth_role = AgentRole("synthesis-specialist")

    # (a) orchestrator role override + UNOVERRIDDEN synthesis → synthesis gets its
    #     OWN derived role, NOT the orchestrator's leak.
    manifest_no_synth_override = _manifest().model_copy(
        update={
            "per_step_overrides": {
                StepID("orchestrator"): StepOverride(
                    step_id=StepID("orchestrator"), agent_role=orch_role
                )
            }
        }
    )
    synth = _OWSynthesisCapturingDispatcher()
    result = execute_workflow(
        manifest_no_synth_override,
        [*_steps(2), _ow_synthesis_step()],
        run_id="run-synth-role-noleak",
        ctx=cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter())),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry, _OWBranchOrSynthesisRegistry(_OWDispatcher(), synth)
        ),
    )
    assert result.status is RunStatus.SUCCESS
    assert synth.received_agent_role == derive_agent_role(StepID("synthesis"))
    assert synth.received_agent_role != orch_role  # the leak is closed

    # (b) a per-step role override ON the synthesis step wins (precedence per-step > derived).
    manifest_synth_override = _manifest().model_copy(
        update={
            "per_step_overrides": {
                StepID("orchestrator"): StepOverride(
                    step_id=StepID("orchestrator"), agent_role=orch_role
                ),
                StepID("synthesis"): StepOverride(
                    step_id=StepID("synthesis"), agent_role=synth_role
                ),
            }
        }
    )
    synth2 = _OWSynthesisCapturingDispatcher()
    execute_workflow(
        manifest_synth_override,
        [*_steps(2), _ow_synthesis_step()],
        run_id="run-synth-role-override",
        ctx=cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter())),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry, _OWBranchOrSynthesisRegistry(_OWDispatcher(), synth2)
        ),
    )
    assert synth2.received_agent_role == synth_role


def test_orchestrator_workers_synthesis_with_zero_workers_fails_closed() -> None:
    """Codex round 6 [P2] — an ORCHESTRATOR_WORKERS `[orchestrator, synthesis]` has
    len == 2 (the orchestrator is steps[0], NOT a branch), so it PASSES the placement
    guard's static `len(steps) < 2` check; but carving the synthesis leaves zero
    workers → the fan-out drains zero siblings. The dispatch-time zero-sibling guard
    rejects it FAILED rather than spend an LLM call on no branch data. The synthesis
    dispatcher is never reached + no disclosing entry is appended."""
    ledger = _RecordingLedger()
    synth = _OWSynthesisCapturingDispatcher()
    result = _run(
        steps=[*_steps(0), _ow_synthesis_step()],  # [orchestrator, synthesis] — zero workers
        dispatcher=_OWDispatcher(),
        ledger=ledger,
        registry=cast(
            StepDispatcherRegistry,
            _OWBranchOrSynthesisRegistry(_OWDispatcher(), synth),
        ),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert result.fail_class.startswith("post-join-synthesis-no-siblings:")
    assert result.final_state is None
    # The synthesis dispatcher never ran (fail-closed before dispatch) — no LLM call,
    # no disclosing ledger entry.
    assert synth.received_siblings is None
    assert not any(str(wk.step_id).startswith("post-join-synthesis") for _p, wk in ledger.appends)
