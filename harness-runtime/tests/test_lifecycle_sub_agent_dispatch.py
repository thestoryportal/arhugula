"""Tests for U-RT-59 — sub-agent dispatch composer (C-RT-17 §14.7).

Acceptance-criterion coverage (per Phase-2 Session-3 Track-A v2.5 L9-ter):
  AC #2  — Protocol satisfaction
      → test_runtime_sub_agent_dispatcher_satisfies_step_dispatcher_protocol
  AC #3  — SubAgentDispatchPayload validation
      → test_payload_validation_rejects_mis_shaped_payload
      → test_payload_validation_accepts_4_field_shape
  AC #4  — HandoffContext composition
      → test_handoff_context_composed_per_v1_6_mvp_table
      → test_handoff_context_audit_trail_link_per_path_a_v1_6
  AC #5  — gate-level descent + topology admissibility
      → test_dispatch_invokes_handoff_registry_with_step_context_seeds
      → test_topology_primary_passes_strict_gate
      → test_topology_cross_pattern_admissible_passes_strict_gate
      → test_topology_neither_primary_nor_admissible_raises
  AC #6  — subagent.span + topology.* emission
      → test_dispatch_emits_exactly_one_subagent_span
      → test_subagent_span_carries_7_subagent_attributes
      → test_subagent_span_carries_2_narrow_topology_attributes
      → test_subagent_span_does_not_carry_8_fanout_topology_attributes
      → test_attribute_names_come_from_canonical_carrier
  AC #7  — child runner invocation
      → test_dispatch_invokes_child_workflow_runner
      → test_child_runner_receives_handoff_context_and_descent
  AC #8  — child result mapping
      → test_success_maps_to_completed_returns_final_state
      → test_drained_maps_to_completed_returns_partial_state
      → test_failed_raises_sub_agent_child_failed_error_with_span_attrs
  AC #9  — audit-entry 4-substep composition (UN-STRUCK at v1.7 §14.7.2 step 8)
      → test_dispatcher_takes_audit_writer_kwarg_at_v1_7
      → test_step8a_composes_cp_audit_entry
      → test_step8b_writes_f2_dispatch_action_entry
      → test_step8c_8d_persists_od_audit_entry_through_writer
      → test_step8b_failure_raises_audit_compose_error_on_success_path
      → test_step8_failure_swallowed_on_failed_child_path
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier, StepID, WorkloadClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.handoff_context import ActionKind, StateSummary
from harness_cp.pause_resume_protocol_types import PauseSnapshot, WorkflowPauseReason
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.sub_agent_brief import (
    ClearTaskBoundaries,
    OutputSchema,
    OutputSchemaKind,
    SubAgentBrief,
)
from harness_cp.sub_agent_gate_level_descent import SubAgentGateLevelDescent
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.topology_subagent_namespace import (
    SUBAGENT_NAMESPACE_SCHEMA,
    TOPOLOGY_NAMESPACE_SCHEMA,
)
from harness_cp.workflow_driver import StepDispatcher
from harness_cp.workflow_driver_types import (
    RunResult,
    RunStatus,
    StepExecutionContext,
    StepKind,
    SubAgentChildPausedError,
    WorkflowStep,
    compose_branch_child_context,
    compose_branch_path,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_entry_schema import Identifier as _Identifier
from harness_od.audit_ledger_types import SignatureAlgorithm
from harness_runtime.lifecycle.audit_writer import RuntimeAuditLedgerWriter
from harness_runtime.lifecycle.handoff import RuntimeHandoffRegistry
from harness_runtime.lifecycle.state_ledger import LedgerWriter
from harness_runtime.lifecycle.sub_agent_dispatch import (
    RuntimeSubAgentDispatcher,
    SubAgentChildFailedError,
    SubAgentDispatchAuditComposeError,
    SubAgentDispatchPayload,
    SubAgentDispatchPayloadShapeError,
    SubAgentDispatchTopologyInadmissibleError,
    compose_child_run_id_seed,
    subagent_child_recoverable,
)
from harness_runtime.lifecycle.topology_dispatcher import RuntimeTopologyDispatcher
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ---------------------------------------------------------------------------
# Fixture scaffolding
# ---------------------------------------------------------------------------


_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic",
        model="claude-haiku-4-5",
        family=ProviderFamily.ANTHROPIC,
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-sub-agent-runtime")


def _brief() -> SubAgentBrief:
    return SubAgentBrief(
        objective="extract structured fields from a free-text input",
        output_format=OutputSchema(
            schema_kind=OutputSchemaKind.JSON_SCHEMA,
            schema_body='{"type":"object"}',
        ),
        guidance="prefer recall over precision",
        task_boundaries=ClearTaskBoundaries(
            in_scope=("field extraction",),
            out_of_scope=("freeform summarization",),
            termination_criteria=("all fields present or null",),
        ),
        summary_hash="0" * 64,
    )


def _child_manifest(
    *,
    workload_class: WorkloadClass = WorkloadClass.SOFTWARE_ENGINEERING,
    topology: TopologyPattern = TopologyPattern.HIERARCHICAL_DELEGATION,
    engine_class: EngineClass = EngineClass.PURE_PATTERN_NO_ENGINE,
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id="child-wf",
        workload_class=workload_class,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=engine_class,
        topology_pattern=topology,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _child_steps(
    kinds: tuple[StepKind, ...] = (StepKind.INFERENCE_STEP,),
) -> tuple[WorkflowStep, ...]:
    return tuple(
        WorkflowStep(
            step_id=StepID(f"child-step-{i}"),
            step_kind=kind,
            step_payload={"index": i},
        )
        for i, kind in enumerate(kinds)
    )


def _payload(
    *,
    workload_class: WorkloadClass = WorkloadClass.SOFTWARE_ENGINEERING,
    topology: TopologyPattern = TopologyPattern.HIERARCHICAL_DELEGATION,
    engine_class: EngineClass = EngineClass.PURE_PATTERN_NO_ENGINE,
    child_step_kinds: tuple[StepKind, ...] = (StepKind.INFERENCE_STEP,),
) -> SubAgentDispatchPayload:
    return SubAgentDispatchPayload(
        child_workflow_id="child-wf",
        child_manifest_entry=_child_manifest(
            workload_class=workload_class, topology=topology, engine_class=engine_class
        ),
        child_steps=_child_steps(child_step_kinds),
        brief=_brief(),
    )


def _step(payload: SubAgentDispatchPayload | None = None) -> WorkflowStep:
    p = payload if payload is not None else _payload()
    return WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload=p.model_dump(),
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="parent-wf",
        parent_action_id="workflow:parent-wf:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=_ACTOR,
        parent_entry_hash="",
        parent_idempotency_key="0" * 64,
        tenant_id=None,
        step_index=0,
    )


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-0",
        model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        hitl_placement=None,
        override_applied=False,
        override_audit_ref=None,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _tracer_provider_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp, exporter


class _MockChildWorkflowRunner:
    """Test fixture per spec §14.7 "Deferred to implementation discretion".

    Records the kwargs of each invocation; returns canned `RunResult` from
    its `next_result` slot. Bound via `RuntimeSubAgentDispatcher`'s
    `child_workflow_runner` constructor kwarg per AC #7.
    """

    def __init__(self, *, next_result: RunResult) -> None:
        self.calls: list[Mapping[str, Any]] = []
        self.next_result = next_result

    def __call__(
        self,
        *,
        workflow_id: str,
        manifest_entry: WorkflowManifestEntry,
        steps: Sequence[WorkflowStep],
        handoff_context: Any,
        descent: SubAgentGateLevelDescent,
        default_model_binding: ModelBinding,
        pause_snapshot_input: Any = None,
        child_run_id_seed: str | None = None,
    ) -> RunResult:
        self.calls.append(
            {
                "workflow_id": workflow_id,
                "manifest_entry": manifest_entry,
                "steps": tuple(steps),
                "handoff_context": handoff_context,
                "descent": descent,
                "default_model_binding": default_model_binding,
                # B-HIERARCHICAL-PAUSE — the child resume snapshot threaded on resume
                # (None on a first dispatch).
                "pause_snapshot_input": pause_snapshot_input,
                # B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT — the deterministic child run_id
                # seed (None when the child is non-recoverable / a non-fanout dispatch).
                "child_run_id_seed": child_run_id_seed,
            }
        )
        return self.next_result


def _success_result() -> RunResult:
    return RunResult(
        workflow_id="child-wf",
        run_id="child-run-1",
        status=RunStatus.SUCCESS,
        terminal_step_index=None,
        partial_state=None,
        final_state={"child_field": "value"},
        fail_class=None,
    )


def _drained_result() -> RunResult:
    return RunResult(
        workflow_id="child-wf",
        run_id="child-run-1",
        status=RunStatus.DRAINED,
        terminal_step_index=0,
        partial_state={"partial_field": "partial_value"},
        final_state=None,
        fail_class=None,
    )


def _failed_result() -> RunResult:
    return RunResult(
        workflow_id="child-wf",
        run_id="child-run-1",
        status=RunStatus.FAILED,
        terminal_step_index=0,
        partial_state=None,
        final_state=None,
        fail_class="step-failure: RuntimeError: simulated child failure",
    )


def _build_ledger_writer(tmp_path: Path) -> LedgerWriter:
    """Build a real `LedgerWriter` rooted in `tmp_path` (test isolation).

    Bypasses `PathResolver` — constructs the `JsonlLedgerHandle` directly
    against a tmp-path JSONL file so tests don't need a path-class registry
    fixture.
    """
    from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle

    path = tmp_path / "state.jsonl"
    path.touch()
    handle = JsonlLedgerHandle(canonical_path=path, exists=True, entry_count=0)
    return LedgerWriter(handle=handle, actor=_ACTOR)


def _dispatcher(
    tmp_path: Path,
    *,
    child_result: RunResult | None = None,
    audit_writer_override: RuntimeAuditLedgerWriter | None = None,
    ledger_writer_override: LedgerWriter | None = None,
) -> tuple[RuntimeSubAgentDispatcher, _MockChildWorkflowRunner, InMemorySpanExporter]:
    """Compose a RuntimeSubAgentDispatcher with mocked child runner + real
    handoff/topology registries + real `LedgerWriter` + `RuntimeAuditLedgerWriter`
    (rooted in `tmp_path`) + InMemorySpanExporter for verification.

    v1.7 §14.7.2 step 8 4-substep contract requires the dispatcher to wire
    `ledger_writer` (8b F2-write), `audit_writer` (8d OD persistence),
    `audit_signing_key_id` + `audit_signing_algorithm` (8c CP→OD converter
    signing), and `time_source` (8b F2 timestamp).
    """
    from datetime import UTC, datetime

    tp, exporter = _tracer_provider_with_exporter()
    runner = _MockChildWorkflowRunner(
        next_result=child_result if child_result is not None else _success_result()
    )
    ledger_writer = ledger_writer_override or _build_ledger_writer(tmp_path)
    audit_writer = audit_writer_override or RuntimeAuditLedgerWriter(
        ledger_writer=ledger_writer,
        time_source=lambda: datetime.now(UTC),
    )
    dispatcher = RuntimeSubAgentDispatcher(
        handoff_registry=RuntimeHandoffRegistry(),
        topology_dispatcher=RuntimeTopologyDispatcher(),
        tracer_provider=tp,
        child_workflow_runner=runner,  # type: ignore[arg-type]
        ledger_writer=ledger_writer,
        audit_writer=audit_writer,
        audit_signing_key_id="test-signing-key",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        time_source=lambda: datetime.now(UTC),
        procedural_tier_snapshot_resolver=lambda: _Identifier("b" * 64),
    )
    return dispatcher, runner, exporter


# ---------------------------------------------------------------------------
# AC #2 — Protocol satisfaction
# ---------------------------------------------------------------------------


def test_runtime_sub_agent_dispatcher_satisfies_step_dispatcher_protocol(tmp_path: Path) -> None:
    """AC #2: isinstance check passes via @runtime_checkable Protocol."""
    dispatcher, _, _ = _dispatcher(tmp_path)
    assert isinstance(dispatcher, StepDispatcher)


# ---------------------------------------------------------------------------
# AC #3 — payload validation
# ---------------------------------------------------------------------------


def test_payload_validation_rejects_mis_shaped_payload(tmp_path: Path) -> None:
    """AC #3: a SUB_AGENT_DISPATCH step with bad step_payload raises typed error."""
    dispatcher, _, _ = _dispatcher(tmp_path)
    bad_step = WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload={"random": "junk"},
    )
    with pytest.raises(SubAgentDispatchPayloadShapeError):
        dispatcher.dispatch(_binding(), bad_step, step_context=_step_context())


def test_payload_validation_accepts_4_field_shape(tmp_path: Path) -> None:
    """AC #3: valid 4-field payload validates + composer proceeds."""
    payload = _payload()
    assert payload.child_workflow_id == "child-wf"
    assert payload.brief.objective.startswith("extract")
    assert len(payload.child_steps) == 1


# ---------------------------------------------------------------------------
# AC #4 — HandoffContext composition
# ---------------------------------------------------------------------------


def test_handoff_context_composed_per_v1_6_mvp_table(tmp_path: Path) -> None:
    """AC #4: composer constructs the 7-field HandoffContext per §14.7.3 table."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    assert len(runner.calls) == 1
    hc = runner.calls[0]["handoff_context"]
    assert hc.proposed_action.action_kind == ActionKind.SUB_AGENT_DISPATCH
    assert hc.proposed_action.payload == {"objective": _brief().objective}
    assert hc.agent_confidence is None
    assert hc.failed_attempts == ()
    assert hc.alternatives_considered == ()
    assert hc.state_summary.summary_text == ""
    assert hc.state_summary.relevant_entries[0].action_id == _step_context().parent_action_id
    assert hc.retry_history.retry_count == 0


def test_handoff_context_audit_trail_link_per_path_a_v1_6(tmp_path: Path) -> None:
    """AC #4: audit_trail_link constructed from step_context per v1.6 Path A."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    hc = runner.calls[0]["handoff_context"]
    assert hc.audit_trail_link.action_id == _step_context().parent_action_id
    assert hc.audit_trail_link.entry_hash == _step_context().parent_entry_hash
    # Actor projected from IS Actor.actor_id → CP ActorIdentity per carrier-map.
    assert str(hc.audit_trail_link.actor) == _ACTOR.actor_id


# ---------------------------------------------------------------------------
# AC #5 — gate-level descent + topology admissibility
# ---------------------------------------------------------------------------


def test_dispatch_invokes_handoff_registry_with_step_context_seeds(tmp_path: Path) -> None:
    """AC #5a: composer's handoff_registry.dispatch receives step_context seeds."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    descent = runner.calls[0]["descent"]
    assert isinstance(descent, SubAgentGateLevelDescent)
    # Descent's child_gate_level descends from step_context.parent_gate_level
    # (AUTO at MVP); no operator override → child inherits AUTO.
    assert descent.child_gate_level == GateLevel.AUTO


def test_topology_primary_passes_strict_gate(tmp_path: Path) -> None:
    """AC #5b: child topology = workload's primary topology passes step 4.

    Path A resolution of the U-RT-59 topology-admissibility Class 1 fork
    (`.harness/class_1_tension_u_rt_59_topology_admissibility_predicate.md`):
    composer step 4 gates on `is_topology_permitted(pattern, workload)` —
    the C-CP-11 §11.1 primary topologies ∪ C-CP-10 §10.3 cross-pattern
    union predicate, NOT the bare §10.3 `is_admissible` predicate.

    SINGLE_THREADED_LINEAR is PIPELINE_AUTOMATION's primary topology per
    C-CP-11 §11.1 row 3 — the strict gate must accept it. (The same case
    against SOFTWARE_ENGINEERING fails per the third test below: it is
    neither SE's primary nor a §10.3 cross-pattern-admissible alternative.)
    """
    payload = _payload(
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )
    dispatcher, runner, exporter = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(payload), step_context=_step_context())
    spans = [s for s in exporter.get_finished_spans() if s.name == "subagent.span"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["topology.pattern"] == TopologyPattern.SINGLE_THREADED_LINEAR.value
    assert len(runner.calls) == 1


def test_topology_cross_pattern_admissible_passes_strict_gate(tmp_path: Path) -> None:
    """AC #5b: child topology = §10.3 cross-pattern admissible passes step 4.

    HIERARCHICAL_DELEGATION + SOFTWARE_ENGINEERING is one of the 5
    cross-pattern admissible cells in `_CROSS_PATTERN_ADMISSIBLE` — non-primary
    but admissibility-closed in the workload's `permitted_patterns`. The
    strict gate accepts it because the union predicate covers both halves.
    """
    payload = _payload(
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        topology=TopologyPattern.HIERARCHICAL_DELEGATION,
    )
    dispatcher, runner, exporter = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(payload), step_context=_step_context())
    spans = [s for s in exporter.get_finished_spans() if s.name == "subagent.span"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["topology.pattern"] == TopologyPattern.HIERARCHICAL_DELEGATION.value
    assert len(runner.calls) == 1


def test_topology_neither_primary_nor_admissible_raises(tmp_path: Path) -> None:
    """AC #5b: child topology not in workload's permitted set raises.

    SINGLE_THREADED_LINEAR + SOFTWARE_ENGINEERING is neither SE's primary
    (which is EVALUATOR_OPTIMIZER + ORCHESTRATOR_WORKERS per C-CP-11 §11.1
    row 1) nor a §10.3 cross-pattern admissible alternative
    (HIERARCHICAL_DELEGATION is the only SE cross-pattern). The strict gate
    raises `SubAgentDispatchTopologyInadmissibleError` before `subagent.span`
    opens — no partial span emission.
    """
    payload = _payload(
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )
    dispatcher, runner, exporter = _dispatcher(tmp_path)
    with pytest.raises(SubAgentDispatchTopologyInadmissibleError):
        dispatcher.dispatch(_binding(), _step(payload), step_context=_step_context())
    # No partial subagent.span emitted (gate fires before span open).
    spans = [s for s in exporter.get_finished_spans() if s.name == "subagent.span"]
    assert len(spans) == 0
    # Child runner not invoked (composer short-circuited at step 4).
    assert len(runner.calls) == 0


# ---------------------------------------------------------------------------
# AC #6 — subagent.span emission
# ---------------------------------------------------------------------------


def test_dispatch_emits_exactly_one_subagent_span(tmp_path: Path) -> None:
    """AC #6: exactly one subagent.span per composer invocation."""
    dispatcher, _, exporter = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    spans = [s for s in exporter.get_finished_spans() if s.name == "subagent.span"]
    assert len(spans) == 1


def test_subagent_span_carries_7_subagent_attributes(tmp_path: Path) -> None:
    """AC #6: the 7 subagent.* attributes per C-CP-14 §14.2 verbatim."""
    dispatcher, _, exporter = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    span = next(s for s in exporter.get_finished_spans() if s.name == "subagent.span")
    attrs = dict(span.attributes or {})
    expected = {schema.attribute_name for schema in SUBAGENT_NAMESPACE_SCHEMA}
    assert expected.issubset(set(attrs.keys()))


def test_subagent_span_carries_2_narrow_topology_attributes(tmp_path: Path) -> None:
    """AC #6: narrow-subset 2 topology.* attributes (pattern + workload_class)."""
    dispatcher, _, exporter = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    span = next(s for s in exporter.get_finished_spans() if s.name == "subagent.span")
    attrs = dict(span.attributes or {})
    assert "topology.pattern" in attrs
    assert "topology.workload_class" in attrs


def test_subagent_span_does_not_carry_8_fanout_topology_attributes(tmp_path: Path) -> None:
    """AC #6: explicit absence of 8 fan-out-specific topology.* attributes."""
    dispatcher, _, exporter = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    span = next(s for s in exporter.get_finished_spans() if s.name == "subagent.span")
    attrs = dict(span.attributes or {})
    forbidden = {
        "topology.fan_out_cap",
        "topology.cascade_policy",
        "topology.results_collected",
        "topology.results_failed",
        "topology.cascade_applied",
        "topology.synthesis_token_budget",
        "topology.cascade_decision_audit_ledger_id",
        "topology.concurrent_token_budget_at_dispatch",
    }
    assert forbidden.isdisjoint(set(attrs.keys()))


def test_attribute_names_come_from_canonical_carrier(tmp_path: Path) -> None:
    """AC #6: subagent.* + topology.pattern + topology.workload_class are sourced
    from the canonical CP-side carrier (no hand-coded attribute strings)."""
    subagent_carrier_names = {s.attribute_name for s in SUBAGENT_NAMESPACE_SCHEMA}
    topology_carrier_names = {s.attribute_name for s in TOPOLOGY_NAMESPACE_SCHEMA}
    # Sanity: the 2 narrow-subset attribute names live in the carrier.
    assert "topology.pattern" in topology_carrier_names
    assert "topology.workload_class" in topology_carrier_names
    # Sanity: all 7 subagent.* names live in the carrier.
    assert "subagent.span.id" in subagent_carrier_names
    assert "subagent.result_status" in subagent_carrier_names


# ---------------------------------------------------------------------------
# AC #7 — child runner invocation
# ---------------------------------------------------------------------------


def test_dispatch_invokes_child_workflow_runner(tmp_path: Path) -> None:
    """AC #7: composer invokes the injected child runner with the child shape."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["workflow_id"] == "child-wf"
    assert call["manifest_entry"].workflow_id == "child-wf"
    assert len(call["steps"]) == 1
    assert call["default_model_binding"] == _binding().model_binding


def test_child_runner_receives_handoff_context_and_descent(tmp_path: Path) -> None:
    """AC #7: child runner sees fully-composed HandoffContext + descent."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    call = runner.calls[0]
    assert call["handoff_context"].proposed_action.action_kind == ActionKind.SUB_AGENT_DISPATCH
    assert isinstance(call["descent"], SubAgentGateLevelDescent)


# ---------------------------------------------------------------------------
# AC #8 — child result mapping
# ---------------------------------------------------------------------------


def test_success_maps_to_completed_returns_final_state(tmp_path: Path) -> None:
    """AC #8a: child SUCCESS → subagent.result_status=completed; returns final_state."""
    dispatcher, _, exporter = _dispatcher(tmp_path, child_result=_success_result())
    out = dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    assert out == {"child_field": "value"}
    span = next(s for s in exporter.get_finished_spans() if s.name == "subagent.span")
    attrs = dict(span.attributes or {})
    assert attrs["subagent.result_status"] == "completed"
    assert attrs["subagent.request_blocked_by_budget"] is False


def test_drained_maps_to_completed_returns_partial_state(tmp_path: Path) -> None:
    """AC #8b: child DRAINED → subagent.result_status=completed; returns partial_state."""
    dispatcher, _, exporter = _dispatcher(tmp_path, child_result=_drained_result())
    out = dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    assert out == {"partial_field": "partial_value"}
    span = next(s for s in exporter.get_finished_spans() if s.name == "subagent.span")
    attrs = dict(span.attributes or {})
    assert attrs["subagent.result_status"] == "completed"


def test_failed_raises_sub_agent_child_failed_error_with_span_attrs(tmp_path: Path) -> None:
    """AC #8c: child FAILED → subagent.result_status=failed + raises typed error."""
    dispatcher, _, exporter = _dispatcher(tmp_path, child_result=_failed_result())
    with pytest.raises(SubAgentChildFailedError):
        dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    span = next(s for s in exporter.get_finished_spans() if s.name == "subagent.span")
    attrs = dict(span.attributes or {})
    assert attrs["subagent.result_status"] == "failed"


# ---------------------------------------------------------------------------
# B-HIERARCHICAL-PAUSE (R-FS-1) — child PAUSE surfacing + resume-snapshot forwarding.
# The CP-side capture + resume re-entry is witnessed end-to-end in
# `harness-cp/tests/test_workflow_driver_fanout_pause.py`; these unit-prove the
# runtime dispatcher seam that test's faithful double stands in for.
# ---------------------------------------------------------------------------


def _paused_result(snapshot: PauseSnapshot) -> RunResult:
    return RunResult(
        workflow_id="child-wf",
        run_id="child-run-1",
        status=RunStatus.PAUSED,
        terminal_step_index=None,
        partial_state=None,
        final_state=None,
        fail_class=None,
        pause_snapshot=snapshot,
    )


def _child_snapshot() -> PauseSnapshot:
    return PauseSnapshot(
        workflow_id="child-wf",
        run_id="child-run-1",
        step_index=1,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        state_summary=StateSummary(
            relevant_entries=(),
            summary_text="",
            summary_hash="0" * 64,
            idempotency_key=_Identifier(""),
            external_references=(),
        ),
        snapshot_hash="a" * 64,
        created_at=1,
        state_ledger_anchor="anchor",
    )


def test_paused_child_raises_sub_agent_child_paused_error(tmp_path: Path) -> None:
    """B-HIERARCHICAL-PAUSE — a child sub-workflow returning PAUSED is SURFACED as a
    typed `SubAgentChildPausedError` carrying the child's snapshot (NOT swallowed as
    success-equivalent), so the parent fan-out captures the cursor + pauses honestly."""
    snap = _child_snapshot()
    dispatcher, _, exporter = _dispatcher(tmp_path, child_result=_paused_result(snap))
    with pytest.raises(SubAgentChildPausedError) as exc_info:
        dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    assert exc_info.value.child_snapshot is snap
    assert exc_info.value.child_workflow_id == "child-wf"
    span = next(s for s in exporter.get_finished_spans() if s.name == "subagent.span")
    attrs = dict(span.attributes or {})
    assert attrs["subagent.result_status"] == "paused"
    # The canonical 7-attr schema: the paused branch raises before the common
    # close-time token block, so it must set the token attrs itself (Codex [P2]).
    assert attrs["subagent.tokens_in"] == 0
    assert attrs["subagent.tokens_out"] == 0
    assert attrs["subagent.cached_tokens_in"] == 0


def test_paused_child_without_snapshot_fails_honestly(tmp_path: Path) -> None:
    """A PAUSED RunResult MUST carry its pause_snapshot (the §25.2 contract). A PAUSED
    with no snapshot cannot be resumed → fail as a child failure (never a
    false-resumable / silent-success)."""
    bad = RunResult(
        workflow_id="child-wf",
        run_id="child-run-1",
        status=RunStatus.PAUSED,
        terminal_step_index=None,
        partial_state=None,
        final_state=None,
        fail_class=None,
        pause_snapshot=None,
    )
    dispatcher, _, _ = _dispatcher(tmp_path, child_result=bad)
    with pytest.raises(SubAgentChildFailedError):
        dispatcher.dispatch(_binding(), _step(), step_context=_step_context())


def test_child_resume_snapshot_forwarded_to_runner(tmp_path: Path) -> None:
    """B-HIERARCHICAL-PAUSE — on resume, `step_context.child_resume_snapshot` is
    forwarded to the child runner as `pause_snapshot_input` so the child re-enters at
    its cursor. `None` on a normal (first) dispatch."""
    snap = _child_snapshot()
    dispatcher, runner, _ = _dispatcher(tmp_path)

    # Normal dispatch → runner receives pause_snapshot_input=None.
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    assert runner.calls[-1]["pause_snapshot_input"] is None

    # Resume dispatch → the step context carries the child snapshot → forwarded.
    resume_ctx = _step_context().model_copy(update={"child_resume_snapshot": snap})
    dispatcher.dispatch(_binding(), _step(), step_context=resume_ctx)
    assert runner.calls[-1]["pause_snapshot_input"] is snap


def test_child_runner_resume_workflow_id_mismatch_fails_closed() -> None:
    """B-HIERARCHICAL-PAUSE (Codex [P2]) — the real `ChildWorkflowRunner` FAILS CLOSED
    when a resume snapshot's `workflow_id` does not match the child being invoked (a
    parent edited between pause + resume so the same SUB_AGENT_DISPATCH step_id points
    to a different child). The guard fires before `execute_workflow` / `ctx` is used."""
    from types import SimpleNamespace

    from harness_runtime.lifecycle.child_workflow_runner import compose_child_workflow_runner

    snap = _child_snapshot()  # workflow_id="child-wf"
    runner = compose_child_workflow_runner(cast(Any, SimpleNamespace(step_dispatchers=None)))
    with pytest.raises(ValueError, match="child resume workflow-id mismatch"):
        runner(
            workflow_id="a-different-child-wf",
            manifest_entry=cast(Any, None),
            steps=(),
            handoff_context=cast(Any, None),
            descent=cast(Any, None),
            default_model_binding=cast(Any, None),
            pause_snapshot_input=snap,
        )


def test_child_workflow_runner_opts_into_final_state_reconstruct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT (R-FS-1) — WIRING WITNESS. The real
    `compose_child_workflow_runner` forwards `reconstruct_final_state=True` to
    `execute_workflow` (the child opt-in), so a durable-engine-class child resume over a
    committed prefix reconstructs its COMPLETE `final_state` for the parent fold (a
    suffix-only child final_state silently corrupts the parent aggregate per finding-v2).
    Composes with the CP-side full-chain reconstruct witnesses (which prove that
    `execute_workflow(reconstruct_final_state=True)` actually seeds the prefix from the
    durable store). The real-runner-THROUGH-execute_workflow integration remains the
    pre-existing AC #7 coverage residual (needs a fully-bootstrapped HarnessContext)."""
    from types import SimpleNamespace

    import harness_runtime.lifecycle.child_workflow_runner as cwr

    captured: dict[str, Any] = {}

    def _spy_execute_workflow(*_args: Any, **kwargs: Any) -> RunResult:
        captured.update(kwargs)
        return _success_result()

    monkeypatch.setattr(cwr, "execute_workflow", _spy_execute_workflow)
    runner = cwr.compose_child_workflow_runner(cast(Any, SimpleNamespace(step_dispatchers={})))
    result = runner(
        workflow_id="child-wf",
        manifest_entry=cast(Any, None),
        steps=(),
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=cast(Any, None),
        pause_snapshot_input=None,  # first dispatch → skips the workflow-id guard
    )
    # The opt-in is forwarded → execute_workflow reconstructs the child's final_state.
    assert captured.get("reconstruct_final_state") is True
    assert result.status is RunStatus.SUCCESS


# ---------------------------------------------------------------------------
# AC #9 — audit-entry 4-substep composition (UN-STRUCK at v1.7 §14.7.2 step 8)
# ---------------------------------------------------------------------------


def test_dispatcher_takes_audit_writer_kwarg_at_v1_7(tmp_path: Path) -> None:
    """AC #9 contract surface: `RuntimeSubAgentDispatcher` exposes the 5 new
    v1.7 step-8 kwargs (ledger_writer, audit_writer, audit_signing_key_id,
    audit_signing_algorithm, time_source). Inverts the v1.6 MVP structural
    assertion (which forbade `audit_writer` per the Class 1 fork strike).
    """
    import inspect

    sig = inspect.signature(RuntimeSubAgentDispatcher)
    for required in (
        "ledger_writer",
        "audit_writer",
        "audit_signing_key_id",
        "audit_signing_algorithm",
        "time_source",
    ):
        assert required in sig.parameters, (
            f"RuntimeSubAgentDispatcher missing {required!r} kwarg required "
            f"by spec v1.7 §14.7.2 step 8 4-substep audit composition"
        )


def test_step8a_composes_cp_audit_entry(tmp_path: Path) -> None:
    """AC #9.8a: dispatch produces a `CPAuditLedgerEntry` for the dispatch fact.

    Verified indirectly via the F2 entry's existence at 8b (the dispatch
    fact's ground truth) — see `test_step8b_writes_f2_dispatch_action_entry`.
    """
    dispatcher, _, _ = _dispatcher(tmp_path)
    out = dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    assert out is not None


def test_step8b_writes_f2_dispatch_action_entry(tmp_path: Path) -> None:
    """AC #9.8b: composer writes an F2 state-ledger entry whose action_id
    encodes the dispatch action — `dispatch:<parent_action_id>:<child_index>`
    per spec §14.7.2 step 8b.
    """
    from harness_is.state_ledger_write import read_ledger

    ledger_writer = _build_ledger_writer(tmp_path)
    dispatcher, _, _ = _dispatcher(tmp_path, ledger_writer_override=ledger_writer)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    entries = read_ledger(ledger_writer.handle)
    dispatch_entries = [e for e in entries if str(e.action_id).startswith("dispatch:")]
    assert len(dispatch_entries) == 1, (
        f"expected exactly one F2 dispatch entry at step 8b; got {len(dispatch_entries)}"
    )
    assert "workflow:parent-wf:step:0" in str(dispatch_entries[0].action_id)


def test_step8b_f2_entry_populates_procedural_tier_snapshot_ref(
    tmp_path: Path,
) -> None:
    """R-003: the 8b F2 dispatch entry populates the `procedural_tier_snapshot_ref`
    D-derivative sidecar via the injected resolver closure (workflow-context
    emission per IS spec v1.3 §C-IS-05 §5.1). `_dispatcher` wires the standard
    test resolver `lambda: Identifier("b" * 64)`.
    """
    from harness_is.state_ledger_write import read_ledger

    ledger_writer = _build_ledger_writer(tmp_path)
    dispatcher, _, _ = _dispatcher(tmp_path, ledger_writer_override=ledger_writer)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    entries = read_ledger(ledger_writer.handle)
    dispatch_entries = [e for e in entries if str(e.action_id).startswith("dispatch:")]
    assert len(dispatch_entries) == 1
    assert dispatch_entries[0].procedural_tier_snapshot_ref == _Identifier("b" * 64)


def test_step8b_resolver_raise_halts_before_ledger_write(tmp_path: Path) -> None:
    """R-003 HALT: a raising procedural-tier resolver propagates as
    `SubAgentDispatchAuditComposeError` on the success path (caught by the
    step-8 `except Exception` at the composer) and NO F2 dispatch entry is
    written — the resolver fires before `ledger_writer.append` at 8b.
    """
    from harness_is.state_ledger_write import read_ledger

    ledger_writer = _build_ledger_writer(tmp_path)
    dispatcher, _, _ = _dispatcher(tmp_path, ledger_writer_override=ledger_writer)

    def _boom() -> _Identifier:
        raise RuntimeError("resolver boom")

    dispatcher.procedural_tier_snapshot_resolver = _boom

    with pytest.raises(SubAgentDispatchAuditComposeError):
        dispatcher.dispatch(_binding(), _step(), step_context=_step_context())

    entries = read_ledger(ledger_writer.handle)
    dispatch_entries = [e for e in entries if str(e.action_id).startswith("dispatch:")]
    assert dispatch_entries == [], (
        "resolver raise must HALT before the 8b ledger write; no dispatch entry"
    )


def test_step8c_8d_persists_od_audit_entry_through_writer(tmp_path: Path) -> None:
    """AC #9.8c+8d: dispatch produces a CP→OD converter call (8c) + OD
    `AuditLedgerEntry` persisted via `audit_writer.append` (8d); verified by
    reading the audit-tagged entries back from the underlying ledger.
    """
    ledger_writer = _build_ledger_writer(tmp_path)
    dispatcher, _, _ = _dispatcher(tmp_path, ledger_writer_override=ledger_writer)
    dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    # `RuntimeAuditLedgerWriter` wraps audit entries with action_id prefix
    # `audit:<tag>:` per its append discipline; presence of such an entry
    # confirms 8c→8d completed end-to-end.
    audit_writer = dispatcher.audit_writer
    audit_entries = audit_writer.read_all()
    assert len(audit_entries) == 1, (
        f"expected exactly one OD audit entry persisted at step 8d; got {len(audit_entries)}"
    )


def test_step8b_failure_raises_audit_compose_error_on_success_path(
    tmp_path: Path,
) -> None:
    """v1.7 §14.7.2 step 8 failure semantics: F2-write failure on SUCCESS path
    raises `SubAgentDispatchAuditComposeError` mapping to
    `RT-FAIL-SUB-AGENT-AUDIT-COMPOSE`.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    failing_ledger = MagicMock(spec=LedgerWriter)
    failing_ledger.actor = _ACTOR
    failing_ledger.append.side_effect = OSError("simulated F2-write failure")
    audit_writer = RuntimeAuditLedgerWriter(
        ledger_writer=_build_ledger_writer(tmp_path),
        time_source=lambda: datetime.now(UTC),
    )
    dispatcher, _, _ = _dispatcher(
        tmp_path,
        ledger_writer_override=failing_ledger,
        audit_writer_override=audit_writer,
    )
    with pytest.raises(SubAgentDispatchAuditComposeError):
        dispatcher.dispatch(_binding(), _step(), step_context=_step_context())


def test_step8_failure_swallowed_on_failed_child_path(tmp_path: Path) -> None:
    """v1.7 §14.7.2 step 8 failure semantics: on FAILED child path, audit
    composition failures are swallowed; `SubAgentChildFailedError` remains the
    primary fault per "the audit-trail-fact record is preserved even when
    downstream substeps fail" clause.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    failing_ledger = MagicMock(spec=LedgerWriter)
    failing_ledger.actor = _ACTOR
    failing_ledger.append.side_effect = OSError("simulated F2-write failure")
    audit_writer = RuntimeAuditLedgerWriter(
        ledger_writer=_build_ledger_writer(tmp_path),
        time_source=lambda: datetime.now(UTC),
    )
    dispatcher, _, _ = _dispatcher(
        tmp_path,
        child_result=_failed_result(),
        ledger_writer_override=failing_ledger,
        audit_writer_override=audit_writer,
    )
    # Even with a broken F2-write surface, the child-failure remains the
    # surfaced fault — audit composition is best-effort.
    with pytest.raises(SubAgentChildFailedError):
        dispatcher.dispatch(_binding(), _step(), step_context=_step_context())


# ---------------------------------------------------------------------------
# AC #7 sub-coverage residual — compose_child_workflow_runner real factory
# ---------------------------------------------------------------------------
#
# AC #7's recursive child-runner verification at integration scope (real
# `compose_child_workflow_runner` invoking `execute_workflow` with the
# parent's `ctx.step_dispatchers` registry, real OTel parent-span-id
# linkage between subagent.span + child's workflow.start) is a known
# coverage gap at U-RT-59 v1.6 MVP. The Protocol contract (composer →
# runner kwargs) is covered above via `_MockChildWorkflowRunner`; the
# implementation-side recursion is not exercised end-to-end.
#
# Closure target: jointly with the resolution of
# `.harness/class_1_tension_u_rt_59_async_sync_step_dispatcher.md`. Real
# child-runner integration requires either (a) operator workflows binding
# INFERENCE_STEP via the WorkflowObject.step_dispatchers override, or (b)
# the async/sync resolution arc landing so ctx.step_dispatchers binds
# INFERENCE_STEP at bootstrap. Path (a) is achievable today without
# blocking on the Class 1 fork; if/when authored, add an integration test
# here that exercises the real compose_child_workflow_runner.


def test_compose_child_workflow_runner_factory_is_constructible(tmp_path: Path) -> None:
    """Smoke check: real `compose_child_workflow_runner(ctx)` builds a callable.

    Validates the factory shape without invoking the runner (which would
    require a fully-bootstrapped HarnessContext + ctx.step_dispatchers
    bound; deferred per the AC #7 sub-coverage residual above).
    """
    from harness_runtime.lifecycle.child_workflow_runner import (
        compose_child_workflow_runner,
    )

    # The factory is signature-stable independent of ctx validity at
    # construction; it closes over ctx for later use. Constructing with a
    # placeholder ctx exercises the factory + Protocol satisfaction.
    class _CtxStub:
        step_dispatchers: Any = None

    runner = compose_child_workflow_runner(cast(Any, _CtxStub()))
    assert callable(runner)


# ---------------------------------------------------------------------------
# AC #9 integration — multi-dispatch chain + IS hash-chain integrity
# ---------------------------------------------------------------------------


def test_three_sequential_dispatches_chain_through_audit_writer(tmp_path: Path) -> None:
    """v1.7 §14.7.2 step 8 integration: three sequential sub-agent dispatches
    each produce both an F2 dispatch entry (step 8b) AND an OD audit entry
    (step 8d) through the IS-anchored ledger; the underlying IS hash chain
    remains VALID per `verify_chain` (C-IS-06 §6.4).
    """
    from harness_is.chain_verification import VerificationStatus, verify_chain
    from harness_is.state_ledger_write import read_ledger

    ledger_writer = _build_ledger_writer(tmp_path)
    dispatcher, _, _ = _dispatcher(tmp_path, ledger_writer_override=ledger_writer)

    # Three sequential dispatches with distinct parent_action_ids → distinct
    # F2 dispatch action_ids; idempotency-key collisions avoided per the
    # composer's `dispatch:<parent_action_id>:<child_index>` scheme.
    for i in range(3):
        ctx = StepExecutionContext(
            workflow_id="parent-wf",
            parent_action_id=f"workflow:parent-wf:step:{i}",
            parent_gate_level=GateLevel.AUTO,
            parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
            parent_actor=_ACTOR,
            parent_entry_hash="",
            parent_idempotency_key="0" * 64,
            tenant_id=None,
            step_index=i,
        )
        dispatcher.dispatch(_binding(), _step(), step_context=ctx)

    # Read back: 3 F2 dispatch entries (8b) + 3 OD audit-wrapped entries (8d)
    # interleaved in append order.
    entries = read_ledger(ledger_writer.handle)
    dispatch_entries = [e for e in entries if str(e.action_id).startswith("dispatch:")]
    audit_entries = [e for e in entries if str(e.action_id).startswith("audit:")]
    assert len(dispatch_entries) == 3, (
        f"expected 3 F2 dispatch entries; got {len(dispatch_entries)}"
    )
    assert len(audit_entries) == 3, f"expected 3 OD audit-wrapped entries; got {len(audit_entries)}"

    # IS hash chain across all 6 entries remains VALID per C-IS-06 §6.4.
    result = verify_chain(entries)
    assert result.status == VerificationStatus.VALID, (
        f"IS hash chain verification failed across the 6 sequential entries: {result}"
    )


# ---------------------------------------------------------------------------
# B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — recoverability predicate +
# seed-gating witnesses (the corrected predicate over the #746 reverted branch).
# ---------------------------------------------------------------------------


def test_subagent_child_recoverable_true_for_linear_esr_leaf() -> None:
    """The WITNESSED slice: a {ESR} ∧ SINGLE_THREADED_LINEAR child whose steps are all
    TOOL/INFERENCE/DECLARATIVE/HITL (no nested SUB_AGENT/MANAGED) → RECOVERABLE."""
    payload = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.INFERENCE_STEP, StepKind.TOOL_STEP, StepKind.DECLARATIVE_STEP),
    )
    assert subagent_child_recoverable(payload) is True


def test_subagent_child_recoverable_true_for_linear_wal_leaf() -> None:
    """WAL_SEGMENT is the other reconstruction-capable engine class → also RECOVERABLE."""
    payload = _payload(
        engine_class=EngineClass.WAL_SEGMENT,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert subagent_child_recoverable(payload) is True


def test_subagent_child_recoverable_true_for_save_point_child() -> None:
    """POSITIVE (the `…-SAVE-POINT-CHILD` close, R-FS-1): a SAVE_POINT_CHECKPOINT LINEAR leaf child
    is NOW recoverable — its final_state reconstructs (CP v1.79 class-agnostic EngineOutputStore) AND
    a re-dispatched maybe-ran SAVE_POINT child auto-resumes via the engine-class-agnostic F2-prefix
    join (`resume_at>0` → committed prefix not re-fired → at-most-once) firing NO engine-layer
    recovery loop / CAS-claim (no F-1 window). The original "no store → suffix-only → fail closed"
    exclusion reason was RETIRED by v1.79. Flipped from the prior negative control."""
    payload = _payload(
        engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert subagent_child_recoverable(payload) is True


def test_subagent_child_recoverable_true_for_reconciler_child() -> None:
    """POSITIVE (the `…-RECONCILER-CHILD` close, R-FS-1): a RECONCILER_LOOP LINEAR leaf child is NOW
    recoverable — its final_state reconstructs (CP v1.80) AND its re-dispatch is at-most-once-safe.
    A maybe-ran RECONCILER child re-dispatch runs its OWN crash-resume, which fires the U-CP-97
    engine-layer reconverge (`attempt_resume`) gated AT THE CAS CLAIM, upstream of the step loop:
    the not-won-claim cases cleanly auto-resume; the F-1 won-CAS-claim-retry window ABORTs
    (`ABORT_REVALIDATION_FAILED`) → child RunStatus.FAILED *before any step re-executes* → the parent
    fold fails closed (`SubAgentChildFailedError`), NEVER a double-fire or a SUCCESS aggregate. The
    grounding (advisor) confirmed the ABORT→§22.1-HITL disposition is the accepted on-main posture
    (#779/#781); the F-1 engine-lock arc is NOT a prerequisite. Flipped from the prior negative
    control. The full-chain F-1 disposition is witnessed at the integration `test_recursive_child_
    crash_resume_reconciler_f1_abort_*` + `..._parent_folds_fail_closed`."""
    payload = _payload(
        engine_class=EngineClass.RECONCILER_LOOP,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert subagent_child_recoverable(payload) is True


def test_subagent_child_recoverable_false_for_pure_pattern_child() -> None:
    """NEGATIVE CONTROL (the engine-class boundary, R-FS-1): a PURE_PATTERN_NO_ENGINE LINEAR leaf
    child STAYS non-recoverable — it is the lone non-durable engine class with no resume / no durable
    output store, so a re-dispatch cannot auto-resume (it would re-run fresh → no at-most-once / no
    final_state reconstruction). After the `…-RECONCILER-CHILD` close admitted all four durable
    classes, PURE_PATTERN_NO_ENGINE is the sole non-member — this control breaks if the engine-class
    gate is ever accidentally widened to admit it."""
    payload = _payload(
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert subagent_child_recoverable(payload) is False


def test_subagent_child_recoverable_true_for_fanout_child_esr_wal_save_point() -> None:
    """POSITIVE — the FANOUT-CHILD relaxation (R-FS-1): a {ESR}/{WAL}/{SAVE_POINT} child with a
    FAN-OUT topology (PARALLELIZATION / ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION) is
    recoverable. On the parent's re-dispatch the child re-runs under its deterministic child run_id
    and reconstructs its AGGREGATE via the B-FANOUT-OUTPUT-REPLAY branch store (CP
    `_crash_fan_out_resume`) — witnessed end-to-end at the integration probe
    `test_fanout_child_crash_resume_witness.py` (the SAVE_POINT case RED-without-fix there). ESR/WAL
    joined at the FANOUT-CHILD close; SAVE_POINT_CHECKPOINT joined at the `…-FANOUT-CHILD-SAVE-POINT`
    slice (R-FS-1) — the §11.2 ABOVE_ENGINE reading, harness branch store is the sole aggregate
    authority, no CAS/F-1 window. Before the LINEAR-only conjunct 2 every fan-out child returned
    False."""
    for fanout_topology in (
        TopologyPattern.PARALLELIZATION,
        TopologyPattern.ORCHESTRATOR_WORKERS,
        TopologyPattern.HIERARCHICAL_DELEGATION,
    ):
        for engine in (
            EngineClass.EVENT_SOURCED_REPLAY,
            EngineClass.WAL_SEGMENT,
            EngineClass.SAVE_POINT_CHECKPOINT,
        ):
            payload = _payload(
                engine_class=engine,
                topology=fanout_topology,
                child_step_kinds=(StepKind.TOOL_STEP,),
            )
            assert subagent_child_recoverable(payload) is True, (fanout_topology, engine)


def test_subagent_child_recoverable_true_for_fanout_child_reconciler() -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-FANOUT-CHILD-RECONCILER — RECONCILER fan-out
    children now join the recoverable set. The fan-out aggregate authority is the class-agnostic
    B-FANOUT-OUTPUT-REPLAY branch store (the reconciler substrate carries convergence state, not the
    per-branch output map), and the branch-level CAS/F-1 path stays fail-closed through each child's
    existing RECONCILER resume. RED before this close: RECONCILER fan-out children failed conjunct 2."""
    for fanout_topology in (
        TopologyPattern.PARALLELIZATION,
        TopologyPattern.ORCHESTRATOR_WORKERS,
        TopologyPattern.HIERARCHICAL_DELEGATION,
    ):
        payload = _payload(
            engine_class=EngineClass.RECONCILER_LOOP,
            topology=fanout_topology,
            child_step_kinds=(StepKind.TOOL_STEP,),
        )
        assert subagent_child_recoverable(payload) is True, fanout_topology


def test_subagent_child_recoverable_false_for_managed_or_malformed_nested_child_step() -> None:
    """NEGATIVE CONTROL (the recursive conjunct 3, NONLEAF-CHILD arc): a {ESR} ∧ LINEAR child stays
    NON-recoverable when (a) a child step is MANAGED_AGENTS (an unfenced vendor sink with no
    recursively-classifiable child manifest → hard-excluded), or (b) a nested SUB_AGENT_DISPATCH
    child step carries a MIS-SHAPED grandchild payload (the `_child_steps` helper's `{"index": i}`
    cannot validate to `SubAgentDispatchPayload` → the decline-mirror fails closed). The WELL-FORMED
    recoverable-grandchild case is now True — witnessed at
    `test_subagent_child_recoverable_true_for_nested_recoverable_grandchild`."""
    nested_managed = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP, StepKind.MANAGED_AGENTS),
    )
    assert subagent_child_recoverable(nested_managed) is False
    malformed_nested = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.INFERENCE_STEP, StepKind.SUB_AGENT_DISPATCH),
    )
    assert subagent_child_recoverable(malformed_nested) is False


def _nested_payload(
    grandchild: SubAgentDispatchPayload,
    *,
    engine_class: EngineClass = EngineClass.EVENT_SOURCED_REPLAY,
    topology: TopologyPattern = TopologyPattern.SINGLE_THREADED_LINEAR,
    lead_kinds: tuple[StepKind, ...] = (StepKind.INFERENCE_STEP,),
    nested_step_id: str = "child-step-nested-subagent",
) -> SubAgentDispatchPayload:
    """A parent SUB_AGENT_DISPATCH payload (NONLEAF-CHILD recursion fixture): the child is
    {engine_class} ∧ {topology} with `lead_kinds` non-recursive steps FOLLOWED BY one nested
    SUB_AGENT_DISPATCH child step (`nested_step_id`) carrying a WELL-FORMED `grandchild` payload."""
    nested_step = WorkflowStep(
        step_id=StepID(nested_step_id),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload=grandchild.model_dump(),
    )
    return SubAgentDispatchPayload(
        child_workflow_id="parent-child-wf",
        child_manifest_entry=_child_manifest(
            workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
            topology=topology,
            engine_class=engine_class,
        ),
        child_steps=(*_child_steps(lead_kinds), nested_step),
        brief=_brief(),
    )


def test_subagent_child_recoverable_true_for_nested_recoverable_grandchild() -> None:
    """POSITIVE — the NONLEAF-CHILD arc's load-bearing relaxation (R-FS-1), RED-without-fix: a
    {ESR} ∧ LINEAR child whose nested SUB_AGENT_DISPATCH child step carries a RECOVERABLE grandchild
    ({ESR} ∧ LINEAR leaf) is NOW recoverable. Before the recursive conjunct 3 this returned False
    (the prior #770 leaf-only slice). The recursion descends into the grandchild payload and applies
    the SAME predicate — correct at all depths by construction."""
    grandchild = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP, StepKind.INFERENCE_STEP),
    )
    assert subagent_child_recoverable(_nested_payload(grandchild)) is True


def test_subagent_child_recoverable_false_for_nested_nonrecoverable_grandchild() -> None:
    """NEGATIVE CONTROL — the recursion fails closed when the grandchild is itself non-recoverable:
    an unsupported EVALUATOR_OPTIMIZER grandchild (fails the topology∩engine conjunct 2 at depth 2),
    a PURE_PATTERN grandchild (fails conjunct 1 at depth 2), or a grandchild whose own child step is
    MANAGED_AGENTS (fails the recursive conjunct 3 at depth 2). Each non-recoverability propagates up
    to the parent. (A fan-out {ESR,WAL,SAVE_POINT,RECONCILER} grandchild IS recoverable — witnessed at
    `test_subagent_child_recoverable_true_for_nested_fanout_grandchild`.)"""
    fanout_gc = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.EVALUATOR_OPTIMIZER,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert subagent_child_recoverable(_nested_payload(fanout_gc)) is False
    pure_gc = _payload(
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert subagent_child_recoverable(_nested_payload(pure_gc)) is False
    managed_gc = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.MANAGED_AGENTS,),
    )
    assert subagent_child_recoverable(_nested_payload(managed_gc)) is False


def test_subagent_child_recoverable_true_for_nested_fanout_grandchild() -> None:
    """POSITIVE — the FANOUT-CHILD relaxation composes through the recursion (R-FS-1): a {ESR} ∧
    LINEAR child whose nested SUB_AGENT_DISPATCH grandchild is a fan-out {WAL} child is NOW
    recoverable. The recursion descends into the grandchild and applies the SAME topology∩engine
    conjunct 2 — a fan-out {ESR,WAL} grandchild reconstructs via its own fan-out replay store at
    re-dispatch, so the chain is recoverable at every level."""
    fanout_gc = _payload(
        engine_class=EngineClass.WAL_SEGMENT,
        topology=TopologyPattern.ORCHESTRATOR_WORKERS,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert subagent_child_recoverable(_nested_payload(fanout_gc)) is True


def test_subagent_child_recoverable_recurses_to_depth_three() -> None:
    """The recursion bottoms out correctly at arbitrary depth: a depth-3 chain of recoverable LINEAR
    sub-agents → True; flipping the deepest great-grandchild to an unsupported EVALUATOR_OPTIMIZER
    child → False (no fan-out replay substrate; the non-recoverability propagates up through BOTH
    nested levels)."""
    leaf = _payload(
        engine_class=EngineClass.WAL_SEGMENT,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    depth3_good = _nested_payload(_nested_payload(leaf))
    assert subagent_child_recoverable(depth3_good) is True
    bad_leaf = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.EVALUATOR_OPTIMIZER,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    depth3_bad = _nested_payload(_nested_payload(bad_leaf))
    assert subagent_child_recoverable(depth3_bad) is False


def test_resumed_recursive_verdict_catches_changed_grandchild_at_resume() -> None:
    """STALE-RECURSIVE-VERDICT guard (advisor merge-blocker #3a) — the [P1-b] dual gate's
    resumed-side conjunct (`_resumed_subagent_recoverable_by_ordinal`, which the maybe-ran classifier
    requires alongside the dispatch marker) RE-COMPUTES the RECURSIVE `_subagent_child_recoverable`
    on the RESUMED manifest step. So if a child's nested grandchild is edited recoverable→NON-
    recoverable between dispatch and resume (e.g. grandchild engine flipped to PURE_PATTERN), the
    resumed verdict EXCLUDES the ordinal — the parent never auto-resumes against a stale dispatch-time
    recursive verdict (which would re-dispatch the now-non-recoverable child fresh → double-fire).
    The dispatch-time `child_engine_class` swap guard operates at the CHILD's engine; this recursive
    re-computation is what catches a GRANDCHILD-only change."""
    from harness_cp.workflow_driver import _resumed_subagent_recoverable_by_ordinal

    recoverable_grandchild = _nested_payload(
        _payload(
            engine_class=EngineClass.EVENT_SOURCED_REPLAY,
            topology=TopologyPattern.SINGLE_THREADED_LINEAR,
            child_step_kinds=(StepKind.TOOL_STEP,),
        )
    )
    nonrecoverable_grandchild = _nested_payload(
        _payload(
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,  # grandchild edited at resume
            topology=TopologyPattern.SINGLE_THREADED_LINEAR,
            child_step_kinds=(StepKind.TOOL_STEP,),
        )
    )
    # PEER fan-out (offset 0): the single worker branch at ordinal 0 is the SUB_AGENT step.
    assert _resumed_subagent_recoverable_by_ordinal(
        [_step(recoverable_grandchild)], branch_count=1, orchestrated=False
    ) == {0}
    assert (
        _resumed_subagent_recoverable_by_ordinal(
            [_step(nonrecoverable_grandchild)], branch_count=1, orchestrated=False
        )
        == set()  # changed grandchild → resumed verdict excludes the ordinal → dual gate fails closed
    )


def test_recursive_engine_signature_catches_grandchild_engine_swap() -> None:
    """RECURSIVE cross-engine-class swap guard (out-of-family Codex [P1], NONLEAF-CHILD arc) — a
    grandchild engine swap recoverable→recoverable (RECONCILER↔SAVE_POINT under the SAME nested
    workflow_id + step_id) keeps the recursive recoverability verdict True AND the OUTER child engine
    unchanged, so the leaf-only `child_engine_class` guard would pass both legs and the
    engine-agnostic grandchild seed would replay the old durable store through a DIFFERENT recovery
    mechanism (the #784 swap hazard ONE LEVEL DOWN). The RECURSIVE `_subagent_child_engine_class`
    signature folds the grandchild engines in, so the swap CHANGES the marker → the dual-gate
    marker==resumed comparison fails closed. A LEAF child signature stays byte-identical to the plain
    engine value (no regression to the #774..#784 closed scope)."""
    from harness_cp.workflow_driver import _subagent_child_engine_class

    reconciler_gc = _nested_payload(
        _payload(
            engine_class=EngineClass.RECONCILER_LOOP,
            topology=TopologyPattern.SINGLE_THREADED_LINEAR,
            child_step_kinds=(StepKind.TOOL_STEP,),
        )
    )
    savepoint_gc = _nested_payload(
        _payload(
            engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
            topology=TopologyPattern.SINGLE_THREADED_LINEAR,
            child_step_kinds=(StepKind.TOOL_STEP,),
        )
    )
    sig_reconciler = _subagent_child_engine_class(_step(reconciler_gc))
    sig_savepoint = _subagent_child_engine_class(_step(savepoint_gc))
    # The grandchild engine swap CHANGES the recursive signature → dual-gate mismatch → fail closed.
    assert sig_reconciler != sig_savepoint
    assert sig_reconciler is not None and "reconciler-loop" in sig_reconciler
    assert sig_savepoint is not None and "save-point-checkpoint" in sig_savepoint
    # LEAF child (no nested SUB_AGENT) → byte-identical to the plain engine value (pre-arc marker).
    leaf = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    assert _subagent_child_engine_class(_step(leaf)) == EngineClass.EVENT_SOURCED_REPLAY.value


def test_recursive_signature_catches_topology_swap() -> None:
    """CROSS-TOPOLOGY swap guard (out-of-family Codex [P1], FANOUT-CHILD arc) — conjunct 2 now admits
    the SAME engine ({ESR,WAL}) under BOTH SINGLE_THREADED_LINEAR AND a fan-out topology. A maybe-ran
    child dispatched LINEAR-ESR whose resumed manifest swaps ONLY its topology to fan-out (same
    engine/step_id) would pass an engine-only signature AND reuse the same `child_run_id` against a
    DIFFERENT recovery substrate (the LINEAR `reconstruct_final_state` seed vs the fan-out
    `_crash_fan_out_resume` branch store) → run fresh → double-fire. A fan-out↔fan-out swap
    (PARALLELIZATION↔ORCHESTRATOR_WORKERS) is the same hole. The signature folds topology in (LINEAR
    keeps the bare engine value; fan-out prepends `topology:`), so ANY topology swap CHANGES the
    marker → the dual-gate marker==resumed comparison fails closed. RED without the topology fold (the
    bare-engine signature was identical across topologies)."""
    from harness_cp.workflow_driver import _subagent_child_engine_class

    def _sig(topology: TopologyPattern) -> str | None:
        return _subagent_child_engine_class(
            _step(
                _payload(
                    engine_class=EngineClass.EVENT_SOURCED_REPLAY,
                    topology=topology,
                    child_step_kinds=(StepKind.TOOL_STEP,),
                )
            )
        )

    sig_linear = _sig(TopologyPattern.SINGLE_THREADED_LINEAR)
    sig_parallel = _sig(TopologyPattern.PARALLELIZATION)
    sig_orch = _sig(TopologyPattern.ORCHESTRATOR_WORKERS)
    # LINEAR↔fan-out swap (same engine) → marker differs → fail closed.
    assert sig_linear != sig_parallel
    # fan-out↔fan-out swap (same engine, different store shape) → marker differs → fail closed.
    assert sig_parallel != sig_orch
    # LINEAR keeps the bare engine value (the #774..#786 closed-scope byte-identity); fan-out prefixes.
    assert sig_linear == EngineClass.EVENT_SOURCED_REPLAY.value
    assert sig_parallel is not None and sig_parallel.startswith(
        TopologyPattern.PARALLELIZATION.value
    )


def test_recursive_signature_catches_save_point_topology_swap() -> None:
    """CROSS-TOPOLOGY swap guard for the NEW fan-out engine (`…-FANOUT-CHILD-SAVE-POINT`, R-FS-1) —
    admitting SAVE_POINT under fan-out opens the SAME swap surface #788 closed for {ESR,WAL}: a
    maybe-ran child dispatched LINEAR-SAVE_POINT (recoverable via the LINEAR `reconstruct_final_state`
    seed) whose resumed manifest swaps ONLY topology to fan-out (recoverable via the SEPARATE
    `_crash_fan_out_resume` branch store) reuses the SAME engine-class-agnostic `child_run_id` against
    a DIFFERENT recovery substrate → run fresh → double-fire. The `topology:engine` fold makes the
    SAVE_POINT swap CHANGE the marker too → dual-gate fail-closed. Verifies the #788 fold already
    covers the engine this arc newly admits (no new swap hole)."""
    from harness_cp.workflow_driver import _subagent_child_engine_class

    def _sig(topology: TopologyPattern) -> str | None:
        return _subagent_child_engine_class(
            _step(
                _payload(
                    engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
                    topology=topology,
                    child_step_kinds=(StepKind.TOOL_STEP,),
                )
            )
        )

    sig_linear = _sig(TopologyPattern.SINGLE_THREADED_LINEAR)
    sig_parallel = _sig(TopologyPattern.PARALLELIZATION)
    sig_orch = _sig(TopologyPattern.ORCHESTRATOR_WORKERS)
    # LINEAR-SAVE_POINT ↔ fan-out-SAVE_POINT swap (same engine, different substrate) → marker differs.
    assert sig_linear != sig_parallel
    # fan-out↔fan-out SAVE_POINT swap → marker differs (different store shape).
    assert sig_parallel != sig_orch
    # LINEAR keeps the bare engine value; fan-out prefixes `topology:`.
    assert sig_linear == EngineClass.SAVE_POINT_CHECKPOINT.value
    assert sig_parallel is not None and sig_parallel.startswith(
        TopologyPattern.PARALLELIZATION.value
    )


def test_recursive_signature_catches_reconciler_topology_swap() -> None:
    """RECONCILER fan-out admission must preserve the topology-swap guard.

    A linear RECONCILER child and a fan-out RECONCILER child use different recovery substrates, so
    the topology:engine marker must differ even though the engine class is the same.
    """
    from harness_cp.workflow_driver import _subagent_child_engine_class

    def _sig(topology: TopologyPattern) -> str | None:
        return _subagent_child_engine_class(
            _step(
                _payload(
                    engine_class=EngineClass.RECONCILER_LOOP,
                    topology=topology,
                    child_step_kinds=(StepKind.TOOL_STEP,),
                )
            )
        )

    sig_linear = _sig(TopologyPattern.SINGLE_THREADED_LINEAR)
    sig_parallel = _sig(TopologyPattern.PARALLELIZATION)
    sig_orch = _sig(TopologyPattern.ORCHESTRATOR_WORKERS)
    assert sig_linear != sig_parallel
    assert sig_parallel != sig_orch
    assert sig_linear == EngineClass.RECONCILER_LOOP.value
    assert sig_parallel is not None and sig_parallel.startswith(
        TopologyPattern.PARALLELIZATION.value
    )


def test_recursive_signature_catches_grandchild_step_id_rename() -> None:
    """RECURSIVE same-step_id guard (out-of-family Codex [P1] round 2, NONLEAF-CHILD) — RENAMING a
    nested SUB_AGENT step at the SAME index (same grandchild engine + workflow_id) keeps the
    grandchild seed unchanged (it keys on child step INDEX + grandchild workflow_id, NOT the nested
    step_id), so it would auto-resume the OLD durable grandchild effects for a DIFFERENT logical
    step. The recursive signature folds the grandchild `step_id` in → the rename CHANGES the marker
    → the dual-gate marker==resumed comparison fails closed (the recursive analogue of #742)."""
    from harness_cp.workflow_driver import _subagent_child_engine_class

    recoverable_gc = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    sig_a = _subagent_child_engine_class(
        _step(_nested_payload(recoverable_gc, nested_step_id="grandchild-step-A"))
    )
    sig_b = _subagent_child_engine_class(
        _step(_nested_payload(recoverable_gc, nested_step_id="grandchild-step-B"))
    )
    # Same engine, same grandchild — ONLY the nested step_id renamed → signature MUST differ.
    assert sig_a != sig_b
    assert sig_a is not None and "grandchild-step-A" in sig_a


def test_recursive_signature_catches_grandchild_ordinal_and_workflow_id(tmp_path: Path) -> None:
    """RECURSIVE full-identity guard (out-of-family Codex [P1] rounds 5-6, NONLEAF-CHILD) — the
    nested signature records the FULL tuple the grandchild seed keys on. Two further dimensions
    beyond engine + step_id: (1) ORDINAL — inserting a non-sub-agent step BEFORE the nested
    SUB_AGENT shifts its index, so the seed (keyed on the child-step-index `parent_idempotency_key`)
    changes; (2) WORKFLOW_ID — `compose_child_run_id_seed` mixes the grandchild `child_workflow_id`
    in. Without these in the marker, a reorder / re-point would change the seed (fresh grandchild
    run_id → re-fire) while the dual gate passed. Folding both makes the marker CHANGE → fail
    closed."""
    from harness_cp.workflow_driver import _subagent_child_engine_class

    recoverable_gc = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    # ORDINAL: same grandchild + step_id, but a different lead-step count shifts the nested index.
    sig_ord1 = _subagent_child_engine_class(
        _step(_nested_payload(recoverable_gc, lead_kinds=(StepKind.INFERENCE_STEP,)))
    )
    sig_ord2 = _subagent_child_engine_class(
        _step(
            _nested_payload(
                recoverable_gc, lead_kinds=(StepKind.INFERENCE_STEP, StepKind.TOOL_STEP)
            )
        )
    )
    assert sig_ord1 != sig_ord2  # ordinal shift → signature differs → fail closed on a reorder

    # WORKFLOW_ID: same engine + step_id + ordinal, but a different grandchild child_workflow_id.
    def _gc_with_wf(wf: str) -> SubAgentDispatchPayload:
        return SubAgentDispatchPayload(
            child_workflow_id=wf,
            child_manifest_entry=_child_manifest(
                workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
                topology=TopologyPattern.SINGLE_THREADED_LINEAR,
                engine_class=EngineClass.EVENT_SOURCED_REPLAY,
            ),
            child_steps=_child_steps((StepKind.TOOL_STEP,)),
            brief=_brief(),
        )

    sig_wf_a = _subagent_child_engine_class(_step(_nested_payload(_gc_with_wf("gc-wf-A"))))
    sig_wf_b = _subagent_child_engine_class(_step(_nested_payload(_gc_with_wf("gc-wf-B"))))
    assert sig_wf_a != sig_wf_b  # re-point grandchild workflow_id → signature differs → fail closed


def test_cp_runtime_parity_on_partially_malformed_nested_payload() -> None:
    """CP↔runtime PARITY on a partially-valid nested payload (out-of-family Codex [P1], NONLEAF-CHILD)
    — a nested SUB_AGENT step_payload with VALID `child_manifest_entry` + `child_steps` but MISSING
    `child_workflow_id`/`brief`. A runtime-only `SubAgentDispatchPayload.model_validate` would REJECT
    it (→ runtime-False) while the CP defensive mirror ADMITS it (→ CP-True) — the dangerous
    CP-True/runtime-False drift (the outer child's marker admits re-dispatch but the composer passes
    NO seed → the outer child re-runs fresh → its committed prefix DOUBLE-FIRES). Both now delegate
    the nested decision to the SHARED `payload_child_recoverable`, so they AGREE (recoverability does
    not depend on `child_workflow_id`/`brief` — those affect DISPATCHABILITY, fail closed at the
    dispatcher's own validate). RED-without-fix: the old runtime model_validate path returned False
    here while CP returned True."""
    from harness_cp.workflow_driver import _subagent_child_recoverable as _cp_recoverable

    valid_grandchild = _payload(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    gc_dump = valid_grandchild.model_dump()
    # Hand-craft a nested SUB_AGENT step whose payload has the recoverability-relevant fields but is
    # NOT a full SubAgentDispatchPayload (no child_workflow_id, no brief).
    partial_nested_step = WorkflowStep(
        step_id=StepID("child-step-partial-subagent"),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload={
            "child_manifest_entry": gc_dump["child_manifest_entry"],
            "child_steps": gc_dump["child_steps"],
        },
    )
    outer = SubAgentDispatchPayload(
        child_workflow_id="outer-wf",
        child_manifest_entry=_child_manifest(
            workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
            topology=TopologyPattern.SINGLE_THREADED_LINEAR,
            engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        ),
        child_steps=(partial_nested_step,),
        brief=_brief(),
    )
    runtime_verdict = subagent_child_recoverable(outer)
    cp_verdict = _cp_recoverable(_step(outer))
    assert cp_verdict == runtime_verdict, (
        f"CP={cp_verdict} runtime={runtime_verdict} — must agree on a partially-malformed nested "
        "payload (CP-True/runtime-False would admit re-dispatch with no seed → double-fire)"
    )
    assert runtime_verdict is True  # recoverability ignores child_workflow_id/brief


def test_cp_and_runtime_recoverability_predicates_agree() -> None:
    """AGREEMENT WITNESS (advisor secondary) — the CP-side defensive opaque-payload read
    (`_subagent_child_recoverable`, which records the dispatch marker → admits re-dispatch) and the
    runtime typed predicate (`subagent_child_recoverable`, which gates the seed) MUST agree. The
    DANGEROUS drift is CP-True / runtime-False: the classifier admits re-dispatch but the composer
    passes no seed → the child runs fresh → double-fire. Assert both return the SAME verdict on the
    recoverable shape + each negative-control shape."""
    from harness_cp.workflow_driver import _subagent_child_recoverable as _cp_recoverable

    # (payload, expected_verdict) — the expected verdict ALSO locks the engine-class boundary (all
    # four durable classes True / PURE_PATTERN_NO_ENGINE False), so an accidental future widen breaks.
    cases = {
        "linear-esr-leaf": (
            _payload(
                engine_class=EngineClass.EVENT_SOURCED_REPLAY,
                topology=TopologyPattern.SINGLE_THREADED_LINEAR,
                child_step_kinds=(StepKind.TOOL_STEP, StepKind.INFERENCE_STEP),
            ),
            True,
        ),
        "save-point": (
            _payload(
                engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
                topology=TopologyPattern.SINGLE_THREADED_LINEAR,
                child_step_kinds=(StepKind.TOOL_STEP,),
            ),
            True,  # the `…-SAVE-POINT-CHILD` close — now recoverable, CP + runtime agree
        ),
        "reconciler": (
            _payload(
                engine_class=EngineClass.RECONCILER_LOOP,
                topology=TopologyPattern.SINGLE_THREADED_LINEAR,
                child_step_kinds=(StepKind.TOOL_STEP,),
            ),
            True,  # the `…-RECONCILER-CHILD` close — now recoverable (F-1 ABORT→fail-closed-safe)
        ),
        "pure-pattern": (
            _payload(
                engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                topology=TopologyPattern.SINGLE_THREADED_LINEAR,
                child_step_kinds=(StepKind.TOOL_STEP,),
            ),
            False,  # the lone non-durable engine class — no resume → stays non-recoverable
        ),
        "fanout-child-esr": (
            _payload(
                engine_class=EngineClass.EVENT_SOURCED_REPLAY,
                topology=TopologyPattern.PARALLELIZATION,
                child_step_kinds=(StepKind.TOOL_STEP,),
            ),
            True,  # the FANOUT-CHILD relaxation — a fan-out {ESR,WAL} child reconstructs via the store
        ),
        "fanout-child-save-point": (
            _payload(
                engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
                topology=TopologyPattern.PARALLELIZATION,
                child_step_kinds=(StepKind.TOOL_STEP,),
            ),
            True,  # the `…-FANOUT-CHILD-SAVE-POINT` slice — SAVE_POINT joined the fan-out store gate
        ),
        "fanout-child-reconciler": (
            _payload(
                engine_class=EngineClass.RECONCILER_LOOP,
                topology=TopologyPattern.PARALLELIZATION,
                child_step_kinds=(StepKind.TOOL_STEP,),
            ),
            True,  # RECONCILER joins the fan-out branch-store recovery set
        ),
        "nested-subagent-malformed": (
            _payload(
                engine_class=EngineClass.EVENT_SOURCED_REPLAY,
                topology=TopologyPattern.SINGLE_THREADED_LINEAR,
                child_step_kinds=(StepKind.TOOL_STEP, StepKind.SUB_AGENT_DISPATCH),
            ),
            False,  # nested SUB_AGENT with a mis-shaped (`{"index": i}`) grandchild → fail closed
        ),
        "nested-recoverable-grandchild": (
            _nested_payload(
                _payload(
                    engine_class=EngineClass.EVENT_SOURCED_REPLAY,
                    topology=TopologyPattern.SINGLE_THREADED_LINEAR,
                    child_step_kinds=(StepKind.TOOL_STEP, StepKind.INFERENCE_STEP),
                )
            ),
            True,  # the NONLEAF-CHILD relaxation — CP + runtime must BOTH recurse into the grandchild
        ),
        "nested-fanout-esr-grandchild": (
            _nested_payload(
                _payload(
                    engine_class=EngineClass.EVENT_SOURCED_REPLAY,
                    topology=TopologyPattern.PARALLELIZATION,
                    child_step_kinds=(StepKind.TOOL_STEP,),
                )
            ),
            True,  # the FANOUT-CHILD relaxation composes through the recursion (fan-out {ESR,WAL})
        ),
        "nested-fanout-save-point-grandchild": (
            _nested_payload(
                _payload(
                    engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
                    topology=TopologyPattern.PARALLELIZATION,
                    child_step_kinds=(StepKind.TOOL_STEP,),
                )
            ),
            True,  # the `…-FANOUT-CHILD-SAVE-POINT` relaxation composes through the recursion
        ),
        "nested-fanout-reconciler-grandchild": (
            _nested_payload(
                _payload(
                    engine_class=EngineClass.RECONCILER_LOOP,
                    topology=TopologyPattern.PARALLELIZATION,
                    child_step_kinds=(StepKind.TOOL_STEP,),
                )
            ),
            True,  # RECONCILER fan-out grandchild composes through the recursive predicate
        ),
    }
    for name, (payload, expected) in cases.items():
        runtime_verdict = subagent_child_recoverable(payload)
        cp_verdict = _cp_recoverable(_step(payload))  # the CP read over the SUB_AGENT step
        assert cp_verdict == runtime_verdict, (
            f"{name}: CP={cp_verdict} runtime={runtime_verdict} — predicates must agree "
            "(CP-True/runtime-False would admit re-dispatch with no seed → double-fire)"
        )
        assert runtime_verdict is expected, (
            f"{name}: verdict={runtime_verdict} expected={expected} — topology∩engine boundary "
            "(LINEAR and fan-out admit the four durable classes; unsupported topologies fail closed)"
        )


def test_dispatch_seed_none_for_linear_non_fanout_recoverable_child(tmp_path: Path) -> None:
    """SEED-GATING — a recoverable child on a context with NONE of the recovery flags set
    (`branch_index is None`, `is_orchestrator_dispatch=False`, `is_linear_sequential_dispatch=False`
    — the default) gets `child_run_id_seed=None` (legacy fresh `uuid`). The deterministic seed is
    gated to the fan-out-worker / orchestrator / linear-sequential surfaces; an unmarked dispatch is
    excluded (this preserves the sequential-loop-topology exclusion whose steps reuse `step_index`
    across iterations). The linear-sequential POSITIVE case is
    `test_linear_sequential_seed_binds_step_id`."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    recoverable = _payload(
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,  # admits SINGLE_THREADED_LINEAR
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    dispatcher.dispatch(_binding(), _step(recoverable), step_context=_step_context())
    assert runner.calls[-1]["child_run_id_seed"] is None


def _linear_seq_seed(
    tmp_path: Path,
    *,
    step_id: str,
    engine: EngineClass,
    topology: TopologyPattern = TopologyPattern.SINGLE_THREADED_LINEAR,
) -> str | None:
    """The `child_run_id_seed` the real dispatcher composes for a recoverable child under
    `is_linear_sequential_dispatch=True` with the given dispatch-step `step_id` + child
    `engine` + `topology`."""
    child = _payload(
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        engine_class=engine,
        topology=topology,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    linear_ctx = _step_context().model_copy(update={"is_linear_sequential_dispatch": True})
    dispatcher, runner, _ = _dispatcher(tmp_path)
    step = WorkflowStep(
        step_id=StepID(step_id),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload=child.model_dump(),
    )
    dispatcher.dispatch(_binding(), step, step_context=linear_ctx)
    return runner.calls[-1]["child_run_id_seed"]


def test_linear_sequential_seed_binds_step_id_and_engine(tmp_path: Path) -> None:
    """SAME-STEP + SAME-ENGINE IDENTITY for the linear-sequential seed (out-of-family Codex [P1],
    NONLEAF-CHILD) — a recoverable child under `is_linear_sequential_dispatch=True` gets a REAL
    (non-None) deterministic seed that FOLDS IN both `step.step_id` AND the child `engine_class`.
    UNLIKE the fan-out/orchestrator paths the linear-sequential step has NO maybe-ran marker / dual
    gate, so the seed itself must be self-defending: RENAMING the SUB_AGENT (same index) OR SWAPPING
    its child engine (RECONCILER↔SAVE_POINT, same step_id/workflow_id) yields a DIFFERENT seed → the
    edited step gets a fresh run_id → does NOT replay the old child's durable store (under a
    different logical step / through a different recovery mechanism). Without these bindings the
    seeds would be identical (the seed keys only on the index-based parent_idempotency_key)."""
    base = _linear_seq_seed(tmp_path, step_id="sub-agent-A", engine=EngineClass.RECONCILER_LOOP)
    renamed = _linear_seq_seed(tmp_path, step_id="sub-agent-B", engine=EngineClass.RECONCILER_LOOP)
    engine_swapped = _linear_seq_seed(
        tmp_path, step_id="sub-agent-A", engine=EngineClass.SAVE_POINT_CHECKPOINT
    )
    assert base is not None  # real seed (linear-sequential recoverable)
    assert base != renamed  # renamed step → different seed → no wrong-step auto-resume
    assert base != engine_swapped  # engine swap → different seed → no cross-mechanism replay


def test_linear_sequential_seed_binds_topology(tmp_path: Path) -> None:
    """SAME-TOPOLOGY identity for the linear-sequential seed (out-of-family Codex [P1], FANOUT-CHILD
    arc) — the FANOUT-CHILD relax admits a fan-out {ESR,WAL} child as recoverable, so a recoverable
    child under `is_linear_sequential_dispatch=True` may now be LINEAR or fan-out. The
    linear-sequential step has NO maybe-ran dual gate, so the seed must self-defend: SWAPPING the
    child topology (LINEAR↔ORCHESTRATOR_WORKERS — both permitted for PIPELINE_AUTOMATION, same
    step_id/engine/workflow_id) between crash + resume must yield a DIFFERENT seed → the edited step
    gets a fresh run_id → does NOT auto-resume the old store through the wrong recovery substrate
    (the LINEAR `reconstruct_final_state` seed vs the fan-out `_crash_fan_out_resume` branch store).
    RED-without-fix: before the topology fold the seeds were identical (engine + step_id only). The
    fan-out↔fan-out marker-side distinction (PARALLELIZATION vs ORCHESTRATOR_WORKERS) is witnessed at
    `test_recursive_signature_catches_topology_swap`."""
    linear = _linear_seq_seed(
        tmp_path,
        step_id="sub-agent-A",
        engine=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )
    fanout = _linear_seq_seed(
        tmp_path,
        step_id="sub-agent-A",
        engine=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.ORCHESTRATOR_WORKERS,
    )
    assert linear is not None and fanout is not None
    # LINEAR↔fan-out swap (same engine/step_id) → different seed → no wrong-substrate auto-resume.
    assert linear != fanout


def test_dispatch_seed_gated_on_recoverability_non_recoverable_child_gets_none(
    tmp_path: Path,
) -> None:
    """SEED-GATING — a NON-recoverable child (the default PURE_PATTERN / HIERARCHICAL payload) gets
    `child_run_id_seed=None` → the runner falls back to a fresh `uuid` → no auto-resume (pre-arc
    behavior, NO suffix-only corruption on any re-dispatch path)."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(
        _binding(), _step(), step_context=_step_context()
    )  # default = non-recoverable
    assert runner.calls[-1]["child_run_id_seed"] is None


def test_child_run_id_seed_distinct_across_fanout_siblings() -> None:
    """OUT-OF-FAMILY CODEX [P1] FIX — two fan-out SIBLING workers inherit the SAME
    `parent_idempotency_key` from the fan-out parent (the branch-distinct key is the §25.16
    `branch_path`, composed downstream — NOT folded into `parent_idempotency_key`). The seed MUST
    fold in `branch_path` so siblings dispatching the SAME `child_workflow_id` derive DISTINCT child
    run_ids — else they alias one child's durable store / fence state EVEN WITHOUT A CRASH."""
    shared_parent_key = "0" * 64  # both siblings inherit the SAME parent-step key
    sib0 = compose_child_run_id_seed(
        shared_parent_key, "child-wf", branch_path="workflow:wf:step:0:0"
    )
    sib1 = compose_child_run_id_seed(
        shared_parent_key, "child-wf", branch_path="workflow:wf:step:0:1"
    )
    assert sib0 != sib1, "fan-out siblings must derive distinct child run_ids"
    # Resume-stable: the SAME (parent_key, child_wf, branch_path) re-derives the SAME seed (the
    # recovery prerequisite — a crash-resume re-dispatch of branch 0 reuses branch 0's child run_id).
    assert (
        compose_child_run_id_seed(shared_parent_key, "child-wf", branch_path="workflow:wf:step:0:0")
        == sib0
    )
    # A linear (no-branch_path) seed is distinct from any branch seed.
    assert compose_child_run_id_seed(shared_parent_key, "child-wf") not in {sib0, sib1}


def test_dispatch_seed_includes_branch_path_for_fanout_worker(tmp_path: Path) -> None:
    """OUT-OF-FAMILY CODEX [P1] FIX (full-chain) — the composer, dispatching a fan-out WORKER
    branch (branch_index set), folds the §25.16 `branch_path` into the seed it threads to the
    runner, so the seed matches `compose_child_run_id_seed(parent_key, child_wf, branch_path)`
    (NOT the branch_path-less linear form)."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    recoverable = _payload(
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    branch_ctx = compose_branch_child_context(
        _step_context(), branch_index=2, agent_role=AgentRole("worker")
    )
    dispatcher.dispatch(_binding(), _step(recoverable), step_context=branch_ctx)
    expected = compose_child_run_id_seed(
        branch_ctx.parent_idempotency_key,
        recoverable.child_workflow_id,
        branch_path=compose_branch_path(branch_ctx),
    )
    assert runner.calls[-1]["child_run_id_seed"] == expected
    # The branch seed differs from the branch_path-less form (the bug would have used the latter).
    assert runner.calls[-1]["child_run_id_seed"] != compose_child_run_id_seed(
        branch_ctx.parent_idempotency_key, recoverable.child_workflow_id
    )


def _orchestrator_context() -> StepExecutionContext:
    """The orchestrator's OWN dispatch context — `branch_index is None` (it is not a fan-out
    branch) but `is_orchestrator_dispatch=True` (the discriminator the CP driver sets on
    `_execute_orchestrator_workers`' `orchestrator_context`)."""
    return _step_context().model_copy(update={"is_orchestrator_dispatch": True})


def test_dispatch_seed_for_orchestrator_uses_branch_path_none(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT (R-FS-1) — the composer threads the
    DETERMINISTIC `child_run_id_seed` to a SUB_AGENT_DISPATCH ORCHESTRATOR (recoverable child)
    even though `branch_index is None`, keyed via `is_orchestrator_dispatch`. The orchestrator is
    unique within the run (no fan-out siblings), so the seed is the branch_path-LESS form
    (`compose_child_run_id_seed(parent_key, child_wf)`), NOT None and NOT a branch seed."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    recoverable = _payload(
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    orch_ctx = _orchestrator_context()
    dispatcher.dispatch(_binding(), _step(recoverable), step_context=orch_ctx)
    expected = compose_child_run_id_seed(
        orch_ctx.parent_idempotency_key, recoverable.child_workflow_id
    )
    assert runner.calls[-1]["child_run_id_seed"] == expected
    assert runner.calls[-1]["child_run_id_seed"] is not None


def test_orchestrator_seed_distinct_from_worker_seed(tmp_path: Path) -> None:
    """No orchestrator↔worker child aliasing — the orchestrator seeds with `branch_path=None`
    while a fan-out worker INSERTS its `branch_path`, so the two derive DISTINCT child run_ids even
    on the SAME `child_workflow_id` + the SAME inherited `parent_idempotency_key` (workers inherit
    the orchestrator's key verbatim; the branch-distinct key is the downstream `branch_path`)."""
    dispatcher, runner, _ = _dispatcher(tmp_path)
    recoverable = _payload(
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        child_step_kinds=(StepKind.TOOL_STEP,),
    )
    orch_ctx = _orchestrator_context()
    dispatcher.dispatch(_binding(), _step(recoverable), step_context=orch_ctx)
    orch_seed = runner.calls[-1]["child_run_id_seed"]
    # A worker composed from the same parent (same parent_idempotency_key) seeds with branch_path.
    worker_ctx = compose_branch_child_context(
        orch_ctx, branch_index=0, agent_role=AgentRole("worker")
    )
    dispatcher.dispatch(_binding(), _step(recoverable), step_context=worker_ctx)
    worker_seed = runner.calls[-1]["child_run_id_seed"]
    assert orch_seed != worker_seed, "orchestrator + worker child run_ids must not alias"
    # The worker reset `is_orchestrator_dispatch` (compose_branch_child_context) → it took the
    # fan-out-worker seed path (branch_path), not the orchestrator path.
    assert worker_ctx.is_orchestrator_dispatch is False


_ = cast
