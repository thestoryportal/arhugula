"""U-RT-25 — HITL placement runtime registry tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L5 U-RT-25:
  #1 HITL surfaces registered.
     -> test_palette_semantics_surfaces_canonical_table
     -> test_placement_triggers_surfaces_canonical_table
     -> test_palette_invariants_surface_canonical_table
     -> test_per_response_audit_shapes_surface_canonical_table
     -> test_timeout_policies_surface_canonical_table
  #2 timeout degradation emits typed event after configured wait.
     -> test_on_timeout_solo_developer_continues_as_reject
     -> test_on_timeout_team_binding_escalates_to_review_board
     -> test_on_timeout_multi_tenant_compliance_aborts_workflow
     -> test_on_timeout_honors_invocation_with_configured_timeout
  #3 tool-call rewriting wires.
     -> test_rewrite_tool_call_returns_unchanged_when_not_hitl_required
     -> test_rewrite_tool_call_routes_sync_blocking_to_request_human_input
     -> test_rewrite_tool_call_routes_durable_async_to_await_human_approval
     -> test_rewrite_tool_call_routes_both_by_tier_to_escalate_to_human
     -> test_rewrite_tool_call_full_palette_at_no_cross_trust_state
     -> test_rewrite_tool_call_restricted_palette_at_cross_family_active
  #4 select_variant + classify_resume passthroughs:
     -> test_select_variant_sync_blocking_returns_request_human_input
     -> test_select_variant_durable_async_returns_await_human_approval
     -> test_classify_resume_empty_diff_returns_resume_clean
     -> test_classify_resume_material_diff_revalidated_returns_resume_after_revalidation
     -> test_classify_resume_material_diff_failed_returns_abort_revalidation_failed

Plus stage materialization invariants:
  -> test_materialize_returns_stage_with_registry
  -> test_hitl_placement_stage_is_frozen
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_core.identity import ActionID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import (
    ActionKind,
    ExternalReference,
    HandoffContext,
    LedgerEntryRef,
    ProposedAction,
    ReferenceClass,
    RetryHistory,
    StateSummary,
)
from harness_cp.hitl_as_tool_call_rewriting import (
    HITLSemanticVariant,
    RewrittenToolCall,
)
from harness_cp.hitl_placement import HITL_PLACEMENT_TRIGGERS, HITLPlacementKind
from harness_cp.hitl_response_palette import (
    HITL_RESPONSE_SEMANTICS,
    PALETTE_INVARIANTS,
    PER_RESPONSE_AUDIT_ENTRY_SHAPES,
    HITLResponse,
)
from harness_cp.hitl_timeout_degradation import (
    TIMEOUT_DEGRADATION_TABLE,
    TimeoutDegradationKind,
)
from harness_cp.material_diff_detection import MaterialDiff
from harness_cp.pause_resume_protocol import ResumeOutcomeKind
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_cp.workload_binding_engine_class_selection import HITLInvocation
from harness_is.state_ledger_entry_schema import Identifier
from harness_runtime.lifecycle.hitl_placement import (
    HITLPlacementStage,
    RuntimeHITLPlacementRegistry,
    materialize_hitl_placement_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> RuntimeConfig:
    """Build a minimal `RuntimeConfig` for materialize tests.

    The U-RT-25 composer does not consume manifest fields — the registry is
    stateless — so the manifest is left at its empty default.
    """
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _registry(tmp_path: Path) -> RuntimeHITLPlacementRegistry:
    return materialize_hitl_placement_stage(_config(tmp_path)).registry


def _invocation(*, timeout_ms: int | None = 5000) -> HITLInvocation:
    """A minimal `HITLInvocation` with a configured wait (timeout)."""
    return HITLInvocation(
        invocation_id="inv-001",
        placement=HITLPlacementKind.PRE_ACTION.value,
        handoff_context=_handoff_context(),
        response_palette=frozenset(
            {HITLResponse.APPROVE, HITLResponse.EDIT, HITLResponse.REJECT, HITLResponse.RESPOND}
        ),
        timeout=timeout_ms,
        cascade_policy="pause",
        opened_at="2026-05-19T21:00:00Z",
    )


def _handoff_context() -> HandoffContext:
    return HandoffContext(
        proposed_action=ProposedAction(
            action_kind=ActionKind.INFERENCE_STEP, payload={}, brief=None
        ),
        agent_confidence=None,
        failed_attempts=(),
        alternatives_considered=(),
        state_summary=StateSummary(
            relevant_entries=(),
            summary_text="s",
            summary_hash="0" * 64,
            idempotency_key=Identifier("k"),
            external_references=(),
        ),
        audit_trail_link=LedgerEntryRef(
            action_id=ActionID("a0"),
            entry_hash="0" * 64,
            actor=ActorIdentity("op"),
        ),
        retry_history=RetryHistory(attempts=(), retry_count=0),
    )


# ---------------------------------------------------------------------------
# AC #1 — HITL surfaces registered.
# ---------------------------------------------------------------------------


def test_palette_semantics_surfaces_canonical_table(tmp_path: Path) -> None:
    """`palette_semantics` exposes the 4-row C-CP-16 §16.1 table verbatim."""
    registry = _registry(tmp_path)
    assert registry.palette_semantics is HITL_RESPONSE_SEMANTICS
    assert len(registry.palette_semantics) == 4


def test_placement_triggers_surfaces_canonical_table(tmp_path: Path) -> None:
    """`placement_triggers` exposes the 3-row C-CP-17 §17.1 table verbatim."""
    registry = _registry(tmp_path)
    assert registry.placement_triggers is HITL_PLACEMENT_TRIGGERS
    assert len(registry.placement_triggers) == 3
    kinds = {trigger.placement_kind for trigger in registry.placement_triggers}
    assert kinds == {
        HITLPlacementKind.PRE_ACTION,
        HITLPlacementKind.SUB_AGENT_BOUNDARY,
        HITLPlacementKind.VALIDATOR_ESCALATION,
    }


def test_palette_invariants_surface_canonical_table(tmp_path: Path) -> None:
    """`palette_invariants` exposes the C-CP-16 §16.1 invariant table verbatim."""
    registry = _registry(tmp_path)
    assert registry.palette_invariants is PALETTE_INVARIANTS
    assert len(registry.palette_invariants) >= 1


def test_per_response_audit_shapes_surface_canonical_table(tmp_path: Path) -> None:
    """`per_response_audit_shapes` exposes the per-response audit-shape table."""
    registry = _registry(tmp_path)
    assert registry.per_response_audit_shapes is PER_RESPONSE_AUDIT_ENTRY_SHAPES
    assert len(registry.per_response_audit_shapes) == 4


def test_timeout_policies_surface_canonical_table(tmp_path: Path) -> None:
    """`timeout_policies` exposes the 3-row C-CP-21 §21.8 table verbatim."""
    registry = _registry(tmp_path)
    assert registry.timeout_policies is TIMEOUT_DEGRADATION_TABLE
    assert len(registry.timeout_policies) == 3
    tiers = {policy.persona_tier for policy in registry.timeout_policies}
    assert tiers == set(PersonaTier)


# ---------------------------------------------------------------------------
# AC #2 — timeout degradation emits typed event after configured wait.
# ---------------------------------------------------------------------------


def test_on_timeout_solo_developer_fail_closed(tmp_path: Path) -> None:
    """SOLO_DEVELOPER persona tier degrades a timed-out HITL to FAIL_CLOSED (vocab-A)."""
    registry = _registry(tmp_path)
    kind = registry.on_timeout(_invocation(), PersonaTier.SOLO_DEVELOPER)
    assert kind is TimeoutDegradationKind.FAIL_CLOSED


def test_on_timeout_team_binding_escalates_secondary_channel(tmp_path: Path) -> None:
    """TEAM_BINDING persona tier degrades a timed-out HITL to ESCALATE_SECONDARY_CHANNEL."""
    registry = _registry(tmp_path)
    kind = registry.on_timeout(_invocation(), PersonaTier.TEAM_BINDING)
    assert kind is TimeoutDegradationKind.ESCALATE_SECONDARY_CHANNEL


def test_on_timeout_multi_tenant_compliance_fail_closed(tmp_path: Path) -> None:
    """MULTI_TENANT_COMPLIANCE persona tier degrades a timed-out HITL to FAIL_CLOSED
    (vocab-A; override prohibited per C-CP-21 §21.8 — NOT the drifted vocab-B
    `abort-workflow` terminal stop, per U-CP-92)."""
    registry = _registry(tmp_path)
    kind = registry.on_timeout(_invocation(), PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert kind is TimeoutDegradationKind.FAIL_CLOSED


def test_on_timeout_honors_invocation_with_configured_timeout(tmp_path: Path) -> None:
    """AC #2 surface: the registry resolves the typed degradation event given
    a `HITLInvocation` carrying the configured wait (`timeout` field). The L8
    orchestrator threads the wait; this test verifies the post-wait decision."""
    registry = _registry(tmp_path)
    invocation = _invocation(timeout_ms=250)
    assert invocation.timeout == 250
    # After the configured 250 ms wait elapses (at L8), this decision fires.
    kind = registry.on_timeout(invocation, PersonaTier.SOLO_DEVELOPER)
    assert kind is TimeoutDegradationKind.FAIL_CLOSED


# ---------------------------------------------------------------------------
# AC #3 — tool-call rewriting wires.
# ---------------------------------------------------------------------------


def _proposed_action() -> ProposedAction:
    return ProposedAction(
        action_kind=ActionKind.TOOL_CALL,
        payload={"tool": "search", "args": {}},
        brief=None,
    )


def test_rewrite_tool_call_returns_unchanged_when_not_hitl_required(
    tmp_path: Path,
) -> None:
    """`hitl_required=False` → the rewriting passes the call through unchanged."""
    registry = _registry(tmp_path)
    result = registry.rewrite_tool_call(
        tool="search",
        server="mcp.local",
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        proposed_action=_proposed_action(),
        cell_synchrony_class=SynchronyClass.SYNC_BLOCKING,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        hitl_required=False,
    )
    assert isinstance(result, RewrittenToolCall)
    assert result.hitl_required is False
    assert result.variant is None
    assert result.response_palette is None


def test_rewrite_tool_call_routes_sync_blocking_to_request_human_input(
    tmp_path: Path,
) -> None:
    """SYNC_BLOCKING + hitl_required=True → REQUEST_HUMAN_INPUT variant."""
    registry = _registry(tmp_path)
    result = registry.rewrite_tool_call(
        tool="search",
        server="mcp.local",
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        proposed_action=_proposed_action(),
        cell_synchrony_class=SynchronyClass.SYNC_BLOCKING,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        hitl_required=True,
    )
    assert result.hitl_required is True
    assert result.variant is HITLSemanticVariant.REQUEST_HUMAN_INPUT


def test_rewrite_tool_call_routes_durable_async_to_await_human_approval(
    tmp_path: Path,
) -> None:
    """DURABLE_ASYNC + hitl_required=True → AWAIT_HUMAN_APPROVAL variant."""
    registry = _registry(tmp_path)
    result = registry.rewrite_tool_call(
        tool="bulk-update",
        server="mcp.local",
        persona_tier=PersonaTier.TEAM_BINDING,
        proposed_action=_proposed_action(),
        cell_synchrony_class=SynchronyClass.DURABLE_ASYNC,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        hitl_required=True,
    )
    assert result.variant is HITLSemanticVariant.AWAIT_HUMAN_APPROVAL


def test_rewrite_tool_call_routes_both_by_tier_to_escalate_to_human(
    tmp_path: Path,
) -> None:
    """BOTH_BY_TIER + hitl_required=True → ESCALATE_TO_HUMAN variant."""
    registry = _registry(tmp_path)
    result = registry.rewrite_tool_call(
        tool="overlay",
        server="mcp.local",
        persona_tier=PersonaTier.TEAM_BINDING,
        proposed_action=_proposed_action(),
        cell_synchrony_class=SynchronyClass.BOTH_BY_TIER,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        hitl_required=True,
    )
    assert result.variant is HITLSemanticVariant.ESCALATE_TO_HUMAN


def test_rewrite_tool_call_full_palette_at_no_cross_trust_state(
    tmp_path: Path,
) -> None:
    """`CrossTrustBoundaryState.NONE` + hitl_required=True → full 4-response palette."""
    registry = _registry(tmp_path)
    result = registry.rewrite_tool_call(
        tool="search",
        server="mcp.local",
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        proposed_action=_proposed_action(),
        cell_synchrony_class=SynchronyClass.SYNC_BLOCKING,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        hitl_required=True,
    )
    assert result.response_palette == frozenset(
        {HITLResponse.APPROVE, HITLResponse.EDIT, HITLResponse.REJECT, HITLResponse.RESPOND}
    )


def test_rewrite_tool_call_restricted_palette_at_cross_family_active(
    tmp_path: Path,
) -> None:
    """`CROSS_FAMILY_ACTIVE` → U-CP-48 restricted `{REJECT, RESPOND}` palette."""
    registry = _registry(tmp_path)
    result = registry.rewrite_tool_call(
        tool="search",
        server="mcp.local",
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        proposed_action=_proposed_action(),
        cell_synchrony_class=SynchronyClass.SYNC_BLOCKING,
        cross_trust_boundary_state=CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE,
        hitl_required=True,
    )
    assert result.response_palette == frozenset({HITLResponse.REJECT, HITLResponse.RESPOND})


# ---------------------------------------------------------------------------
# AC #4 — select_variant + classify_resume passthroughs.
# ---------------------------------------------------------------------------


def test_select_variant_sync_blocking_returns_request_human_input(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    assert (
        registry.select_variant(SynchronyClass.SYNC_BLOCKING)
        is HITLSemanticVariant.REQUEST_HUMAN_INPUT
    )


def test_select_variant_durable_async_returns_await_human_approval(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    assert (
        registry.select_variant(SynchronyClass.DURABLE_ASYNC)
        is HITLSemanticVariant.AWAIT_HUMAN_APPROVAL
    )


def test_classify_resume_empty_diff_returns_resume_clean(tmp_path: Path) -> None:
    """An empty material-diff set → `RESUME_CLEAN` (C-CP-22 §22.1)."""
    registry = _registry(tmp_path)
    outcome = registry.classify_resume((), revalidation_succeeded=False)
    assert outcome is ResumeOutcomeKind.RESUME_CLEAN


def test_classify_resume_material_diff_revalidated_returns_resume_after_revalidation(
    tmp_path: Path,
) -> None:
    """A material diff with revalidation_succeeded=True → RESUME_AFTER_REVALIDATION."""
    registry = _registry(tmp_path)
    diff = (
        MaterialDiff(
            reference=ExternalReference(
                reference_class=ReferenceClass.F2_LEDGER_ENTRY,
                reference_id="ref-1",
                snapshot_capture_at_pause=b"prior",
            ),
            prior_snapshot=b"prior",
            current_value=b"current",
            is_material=True,
        ),
    )
    outcome = registry.classify_resume(diff, revalidation_succeeded=True)
    assert outcome is ResumeOutcomeKind.RESUME_AFTER_REVALIDATION


def test_classify_resume_material_diff_failed_returns_abort_revalidation_failed(
    tmp_path: Path,
) -> None:
    """A material diff with revalidation_succeeded=False → ABORT_REVALIDATION_FAILED."""
    registry = _registry(tmp_path)
    diff = (
        MaterialDiff(
            reference=ExternalReference(
                reference_class=ReferenceClass.F2_LEDGER_ENTRY,
                reference_id="ref-1",
                snapshot_capture_at_pause=b"prior",
            ),
            prior_snapshot=b"prior",
            current_value=b"current",
            is_material=True,
        ),
    )
    outcome = registry.classify_resume(diff, revalidation_succeeded=False)
    assert outcome is ResumeOutcomeKind.ABORT_REVALIDATION_FAILED


# ---------------------------------------------------------------------------
# Stage materialization invariants.
# ---------------------------------------------------------------------------


def test_materialize_returns_stage_with_registry(tmp_path: Path) -> None:
    stage = materialize_hitl_placement_stage(_config(tmp_path))
    assert isinstance(stage, HITLPlacementStage)
    assert isinstance(stage.registry, RuntimeHITLPlacementRegistry)


def test_hitl_placement_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_hitl_placement_stage(_config(tmp_path))
    with pytest.raises(FrozenInstanceError):
        stage.registry = RuntimeHITLPlacementRegistry()  # type: ignore[misc]
