"""Full-chain witness — driver → real registry → REAL PostJoinSynthesisStepDispatcher
→ final_state (R-FS-1 arc B-POSTJOIN-LLM-SYNTHESIS; CP spec v1.54 + runtime §14.24).

The genuine end-to-end seam the CP-side witnesses (mock dispatcher) + the runtime
unit witnesses (dispatcher called directly) + the bootstrap-binding tests each
leave half-proven (`[[full-chain-witness-not-half-proofs]]`): here `execute_workflow`
(the CP driver) carves the terminal synthesis step, runs the fan-out, looks the
synthesis dispatcher up in a registry, and invokes the **REAL**
`PostJoinSynthesisStepDispatcher` — whose output must reach `final_state` AND
whose inner must have seen the branch-index-ordered composed siblings. Deterministic
(the inner is a recording stub; no provider) — lives in harness-runtime because it
imports BOTH `execute_workflow` (harness-cp) AND the runtime dispatcher.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from harness_core import PersonaTier, StepID, WorkloadClass
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import StepEffectiveBinding, StepOverride
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    DriverContext,
    StepDispatcher,
    StepDispatcherRegistry,
    StepKindDispatcherNotBoundError,
    execute_workflow,
)
from harness_cp.workflow_driver_types import RunStatus, StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.post_join_synthesis_dispatch import (
    POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX,
    PostJoinSynthesisStepDispatcher,
)

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-postjoin-full-chain")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)


class _RecordingLedger:
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


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[Any] = []

    def emit(self, event_class: Any) -> None:
        self.emits.append(event_class)


class _RecordingCpIsWiring:
    """Records `emit_override_state_ledger_entry` calls (async, run via the driver's
    `_run_protocol_method_sync`) — to witness synthesis override provenance."""

    def __init__(self) -> None:
        self.override_emits: list[dict[str, Any]] = []

    async def emit_override_state_ledger_entry(
        self, *, workflow_id: str, step_id: str, post_override_step_config: Any, actor: Any
    ) -> None:
        self.override_emits.append({"workflow_id": workflow_id, "step_id": step_id})


class _Ctx:
    def __init__(self, *, ledger: Any, emitter: _Emitter, cp_is_wiring: Any = None) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = emitter
        self.drained_flag = asyncio.Event()
        self.pause_resume_protocol = None
        self.pause_requested_flag = asyncio.Event()
        self.ledger_reader = None
        self.tracer_provider = NoOpTracerProvider()
        self.validator_framework = None
        self.tenant_id = None
        self.cp_is_wiring = cp_is_wiring


class _WorkerEcho:
    """The DECLARATIVE_STEP (worker) dispatcher — echoes a per-branch output."""

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        return {"worker": str(step.step_id), "echoed": dict(step.step_payload)}


class _RecordingInner:
    """The inner LLM dispatcher the REAL synthesis dispatcher wraps — records the
    composed step it is handed (so the test can prove the siblings were composed
    through the real dispatcher) + returns a fixed synthesized aggregate."""

    def __init__(self) -> None:
        self.composed_messages: Any = None
        self.received_context: Any = None

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> Mapping[str, Any]:
        self.composed_messages = list(step.step_payload["messages"])
        self.received_context = step_context
        return {"synthesis": "full-chain-composed"}


class _Registry:
    def __init__(self, *, worker: StepDispatcher, synthesis: StepDispatcher) -> None:
        self._worker = worker
        self._synthesis = synthesis

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.DECLARATIVE_STEP:
            return self._worker
        if step_kind is StepKind.POST_JOIN_SYNTHESIS:
            return self._synthesis
        raise StepKindDispatcherNotBoundError(step_kind)


def _manifest(per_step_overrides: dict[Any, Any] | None = None) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id="wf-postjoin-full-chain",
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.PARALLELIZATION,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides=per_step_overrides or {},
    )


def _worker(index: int) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(f"worker-{index}"),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"index": index},
    )


def _synthesis_step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"messages": [{"role": "system", "content": "synthesize"}]},
    )


def test_post_join_synthesis_full_chain_driver_to_final_state() -> None:
    """`execute_workflow` (CP driver) carves the terminal synthesis, runs the
    fan-out, looks up POST_JOIN_SYNTHESIS in the registry, and invokes the REAL
    PostJoinSynthesisStepDispatcher — its output reaches `final_state`, and its
    inner saw the branch-index-ordered composed siblings. The whole seam, no proxy
    on the dispatcher (the inner LLM call is a recording stub for determinism)."""
    inner = _RecordingInner()
    real_synthesis_dispatcher = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    registry = cast(
        StepDispatcherRegistry,
        _Registry(worker=_WorkerEcho(), synthesis=real_synthesis_dispatcher),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))

    result = execute_workflow(
        _manifest(),
        [_worker(0), _worker(1), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    # The REAL synthesis dispatcher's output reached final_state THROUGH the driver.
    assert result.status is RunStatus.SUCCESS
    assert result.final_state == {"synthesis": "full-chain-composed"}
    # The REAL dispatcher composed the 2 branch-index-ordered siblings into the LLM
    # input (the inner saw them) — proving the carve→dispatch→compose chain end-to-end.
    sibling_msg = inner.composed_messages[-1]["content"]
    assert sibling_msg.startswith(POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX)
    assert '"branch_index": 0' in sibling_msg
    assert '"branch_index": 1' in sibling_msg
    assert "worker-0" in sibling_msg and "worker-1" in sibling_msg


class _RaisingInner:
    """An inner that RAISES — to witness the post-barrier failed-run mapping."""

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> Mapping[str, Any]:
        raise RuntimeError("synthesis LLM blew up")


def test_post_join_synthesis_full_chain_dispatch_failure_maps_to_failed_run() -> None:
    """Codex [P2] regression — a synthesis dispatch that RAISES post-barrier maps to
    a FAILED RunResult (the exception does NOT escape execute_workflow after the
    branch buffers drained; mirrors the inline per-step failed-run mapping)."""
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, _RaisingInner())),
        ),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))

    result = execute_workflow(
        _manifest(),
        [_worker(0), _worker(1), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.FAILED
    assert result.final_state is None
    assert result.fail_class is not None and "post-join-synthesis-failure" in result.fail_class


def test_post_join_synthesis_full_chain_per_step_override_reaches_context() -> None:
    """Codex [P2] regression — a per-step `agent_role` override on the SYNTHESIS step
    folds into the synthesis step_context (NOT the fan-out parent's, which is None),
    so runtime routing reads the synthesis step's own role."""
    inner = _RecordingInner()
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner)),
        ),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))
    overrides = {
        StepID("synthesis"): StepOverride(
            step_id=StepID("synthesis"), agent_role=AgentRole("synthesizer")
        )
    }

    result = execute_workflow(
        _manifest(per_step_overrides=overrides),
        [_worker(0), _worker(1), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.SUCCESS
    # The synthesis context the dispatcher received carries the OVERRIDE role, not
    # the fan-out parent's None — the per-step override folded into ITS context.
    assert inner.received_context.agent_role == AgentRole("synthesizer")


def test_post_join_synthesis_full_chain_context_action_id_matches_ledger_entry() -> None:
    """Out-of-family Codex round 8 [P2] regression — the synthesis context's
    `parent_action_id` is CONSISTENT with the synthesis step's OWN disclosing ledger entry
    action_id (`workflow:{wf}:post-join-synthesis:{N}`), NOT the generic `...:step:{N}`. A
    downstream record referencing this context's parent_action_id (cost / span / HITL audit)
    now resolves to a REAL ledger entry; the prior `:step:` value referenced a non-existent
    synthesis entry. (Proves the value-consistency; consumer-side join behavior is not
    asserted here.)"""
    inner = _RecordingInner()
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner)),
        ),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))

    result = execute_workflow(
        _manifest(),
        [_worker(0), _worker(1), _synthesis_step()],  # 2 branches → synthesis_index = 2
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.SUCCESS
    # Matches `_append_synthesis_ledger_entry`'s action_id — the audit join is intact.
    assert (
        inner.received_context.parent_action_id
        == "workflow:wf-postjoin-full-chain:post-join-synthesis:2"
    )
    assert ":step:" not in inner.received_context.parent_action_id


def test_post_join_synthesis_full_chain_emits_step_boundary() -> None:
    """Codex [P2] regression — a successful synthesis is a real step that executed,
    so it emits one ADDITIONAL lifecycle STEP_BOUNDARY beyond the fan-out branches
    (telemetry: `workflow.step_count` counts it). Compared to the byte-identical
    fold run over the same workers."""

    def _run(steps: list[WorkflowStep]) -> _Emitter:
        emitter = _Emitter()
        ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=emitter))
        registry = cast(
            StepDispatcherRegistry,
            _Registry(
                worker=_WorkerEcho(),
                synthesis=PostJoinSynthesisStepDispatcher(
                    inner=cast(StepDispatcher, _RecordingInner())
                ),
            ),
        )
        execute_workflow(
            _manifest(),
            steps,
            run_id="run-1",
            ctx=ctx,
            default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
            step_dispatchers=registry,
        )
        return emitter

    def _boundaries(e: _Emitter) -> int:
        return e.emits.count(WorkflowEventClass.STEP_BOUNDARY)

    with_synthesis = _run([_worker(0), _worker(1), _synthesis_step()])
    fold_only = _run([_worker(0), _worker(1)])
    # Exactly ONE more STEP_BOUNDARY when the synthesis step runs.
    assert _boundaries(with_synthesis) == _boundaries(fold_only) + 1


def test_post_join_synthesis_full_chain_override_emits_provenance() -> None:
    """Codex [P2] regression — a per-step override on the SYNTHESIS step emits the
    override-application state-ledger entry (provenance), like the linear /
    branch paths (C-CP-06 §6.6 all-paths contract)."""
    wiring = _RecordingCpIsWiring()
    ctx = cast(
        DriverContext,
        _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), cp_is_wiring=wiring),
    )
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(
                inner=cast(StepDispatcher, _RecordingInner())
            ),
        ),
    )
    overrides = {
        StepID("synthesis"): StepOverride(
            step_id=StepID("synthesis"), agent_role=AgentRole("synthesizer")
        )
    }

    result = execute_workflow(
        _manifest(per_step_overrides=overrides),
        [_worker(0), _worker(1), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.SUCCESS
    # The synthesis step's override emitted exactly one provenance entry, keyed to it.
    assert [e["step_id"] for e in wiring.override_emits] == ["synthesis"]


def test_post_join_synthesis_full_chain_non_terminal_placement_fails_closed() -> None:
    """Codex [P2] regression — a POST_JOIN_SYNTHESIS step NOT in the terminal slot is
    rejected fail-closed (it would otherwise run as an ordinary branch with no
    siblings → a wasted LLM call folded into the aggregate)."""
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(
                inner=cast(StepDispatcher, _RecordingInner())
            ),
        ),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))

    result = execute_workflow(
        _manifest(),
        # synthesis is NOT terminal (a worker follows it) → misplaced.
        [_worker(0), _synthesis_step(), _worker(1)],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.FAILED
    assert result.final_state is None
    assert result.fail_class is not None and "post-join-synthesis-misplaced" in result.fail_class


def test_post_join_synthesis_full_chain_zero_branch_fails_closed() -> None:
    """Codex [P2] regression — a lone POST_JOIN_SYNTHESIS (no fan-out step to compose)
    is rejected fail-closed; it would otherwise carve to empty branch_steps → the
    strategy's empty-steps early return SILENTLY DROPS it."""
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(
                inner=cast(StepDispatcher, _RecordingInner())
            ),
        ),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))

    result = execute_workflow(
        _manifest(),
        [_synthesis_step()],  # synthesis with NO fan-out branch
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None and "post-join-synthesis-misplaced" in result.fail_class


class HITLPauseRequestedSignal(BaseException):
    """Name-matches the runtime durable-async HITL pause signal (a BaseException the
    driver name-matches) — to witness the synthesis durable-pause fail-closed mapping."""


class _PauseSignallingInner:
    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> Mapping[str, Any]:
        raise HITLPauseRequestedSignal


def test_post_join_synthesis_full_chain_durable_hitl_pause_fails_closed() -> None:
    """Codex [P1] regression — a durable-async HITL pause signal (BaseException) raised
    during the synthesis dispatch maps to a FAILED RunResult (not an escaping
    BaseException, not a dead-end PAUSED that cannot resume): a paused terminal
    post-barrier synthesis has no resumable re-entry today."""
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(
                inner=cast(StepDispatcher, _PauseSignallingInner())
            ),
        ),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))

    result = execute_workflow(
        _manifest(),
        [_worker(0), _worker(1), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.FAILED
    assert result.final_state is None
    assert result.fail_class is not None and "not resumable" in result.fail_class


class StepDispatchTimeoutError(Exception):
    """Name-matches the runtime SyncDispatcherFacade timeout (which the CP driver
    name-matches, harness-cp not importing harness-runtime) — to witness the
    synthesis timeout fail-class canonicalization."""


class _TimeoutInner:
    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> Mapping[str, Any]:
        raise StepDispatchTimeoutError("synthesis dispatch timed out")


def test_post_join_synthesis_full_chain_timeout_preserves_fail_class() -> None:
    """Codex [P2] regression — a synthesis dispatch timeout maps to the canonical
    RT-FAIL-STEP-DISPATCH-TIMEOUT (not the bare `StepDispatchTimeoutError` class name),
    so failure-taxonomy-keyed alerts catch timed-out synthesis steps like inline ones."""
    registry = cast(
        StepDispatcherRegistry,
        _Registry(
            worker=_WorkerEcho(),
            synthesis=PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, _TimeoutInner())),
        ),
    )
    ctx = cast(DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter()))

    result = execute_workflow(
        _manifest(),
        [_worker(0), _worker(1), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=registry,
    )

    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None and "RT-FAIL-STEP-DISPATCH-TIMEOUT" in result.fail_class
