"""U-RT-26 — sub-agent handoff + brief runtime registry tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L5 U-RT-26:
  #1 handoff registry queryable.
     -> test_dispatch_returns_descent_with_parent_carry_through
     -> test_dispatch_response_hash_round_trips_against_brief_summary_hash
     -> test_compose_dispatch_audit_carries_descent_gate_level
     -> test_brief_inheritance_table_surfaces_canonical_4_rows
     -> test_inheritance_for_software_engineering
     -> test_inheritance_for_pipeline_automation_uses_per_stage_rule
  #2 brief schemas enforced.
     -> test_brief_extra_field_raises_validation_error
     -> test_brief_missing_required_field_raises_validation_error
     -> test_brief_canonicalize_is_deterministic
     -> test_brief_canonicalize_excludes_summary_hash
     -> test_brief_summary_hash_matches_sha256_of_canonicalized_bytes

Plus invariant + stage materialization tests:
  -> test_assert_descent_at_or_below_parent_passes
  -> test_assert_descent_above_parent_raises
  -> test_assert_ascent_at_or_above_parent_passes
  -> test_assert_ascent_below_parent_raises
  -> test_materialize_returns_stage_with_registry
  -> test_handoff_stage_is_frozen
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import DeploymentSurface, WorkloadClass
from harness_core.identity import ActionID
from harness_cp.brief_authoring_inheritance import (
    BRIEF_AUTHORING_INHERITANCE,
    InheritanceRule,
)
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry
from harness_cp.sub_agent_brief import (
    ClearTaskBoundaries,
    OutputSchema,
    OutputSchemaKind,
    SubAgentBrief,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.handoff import (
    HandoffStage,
    RuntimeHandoffRegistry,
    materialize_handoff_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> RuntimeConfig:
    """Build a minimal `RuntimeConfig` for materialize tests.

    The U-RT-26 composer does not consume manifest fields — the registry is
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


def _registry(tmp_path: Path) -> RuntimeHandoffRegistry:
    return materialize_handoff_stage(_config(tmp_path)).registry


def _brief() -> SubAgentBrief:
    """A minimal valid `SubAgentBrief` for AC tests."""
    return SubAgentBrief(
        objective="search the index for 'foo'",
        output_format=OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT),
        guidance="prefer exact-match results",
        task_boundaries=ClearTaskBoundaries(
            in_scope=("index-search",),
            out_of_scope=("index-write",),
            termination_criteria=("first result returned",),
        ),
        summary_hash="0" * 64,
    )


# ---------------------------------------------------------------------------
# AC #1 — handoff registry queryable.
# ---------------------------------------------------------------------------


def test_dispatch_returns_descent_with_parent_carry_through(tmp_path: Path) -> None:
    """`dispatch` produces a `SubAgentGateLevelDescent` that carries the
    parent gate-level and sandbox-tier through to the child by default
    (monotonic-descent admits equality; ascent is structurally prohibited)."""
    registry = _registry(tmp_path)
    descent = registry.dispatch(
        parent_action_id=ActionID("act-001"),
        parent_gate_level=GateLevel.DENY,
        parent_sandbox_tier=SandboxTier.TIER_2_CONTAINER,
        sub_agent_brief=_brief(),
        operator_override=None,
    )
    assert descent.parent_gate_level is GateLevel.DENY
    assert descent.child_gate_level is GateLevel.DENY
    assert descent.parent_sandbox_tier is SandboxTier.TIER_2_CONTAINER
    assert descent.child_sandbox_tier is SandboxTier.TIER_2_CONTAINER
    assert descent.override_applied is False
    assert descent.override_audit_ref is None


def test_dispatch_with_operator_override_marks_override_applied(
    tmp_path: Path,
) -> None:
    """An `operator_override` mapping flips `override_applied` to True; the
    `override_audit_ref` remains None at dispatch-time (filled at audit-write)."""
    registry = _registry(tmp_path)
    descent = registry.dispatch(
        parent_action_id=ActionID("act-002"),
        parent_gate_level=GateLevel.ASK,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        sub_agent_brief=_brief(),
        operator_override={"relax": "blast-radius"},
    )
    assert descent.override_applied is True
    assert descent.override_audit_ref is None


def test_dispatch_response_hash_round_trips_against_brief_summary_hash(
    tmp_path: Path,
) -> None:
    """The C-CP-12 §12.5 dispatch `response_hash` equals the C-CP-13 §13.2
    brief `summary_hash` — the same canonicalize + sha256 pipeline."""
    registry = _registry(tmp_path)
    brief = _brief()
    assert registry.dispatch_response_hash(brief) == registry.compute_brief_summary_hash(brief)


def test_compose_dispatch_audit_carries_descent_gate_level(tmp_path: Path) -> None:
    """`compose_dispatch_audit` produces a `CPAuditLedgerEntry` whose
    `gate_level` matches the descent's `child_gate_level` value verbatim."""
    registry = _registry(tmp_path)
    brief = _brief()
    descent = registry.dispatch(
        parent_action_id=ActionID("act-003"),
        parent_gate_level=GateLevel.DENY,
        parent_sandbox_tier=SandboxTier.TIER_2_CONTAINER,
        sub_agent_brief=brief,
        operator_override=None,
    )
    entry = registry.compose_dispatch_audit(
        parent_action_id=ActionID("act-003"),
        descent=descent,
        brief_hash=registry.compute_brief_summary_hash(brief),
    )
    assert isinstance(entry, CPAuditLedgerEntry)
    assert entry.gate_level.value == descent.child_gate_level.value
    # Composed entry's action_id encodes the parent identity (§12.5 join).
    assert "act-003" in entry.action_id
    # CP spec v1.28 §16.5.6.X: `timestamp` populated at composer-site
    # clock (universal fix; non-tier-conditional per C-CP-16 §16.2).
    # `prior_event_hash="0"*64` sentinel canonical at solo-developer tier
    # per ADR-D5 §1.4 row 1 ("no hash chain required by default").
    assert entry.timestamp != ""
    assert entry.prior_event_hash == "0" * 64


def test_brief_inheritance_table_surfaces_canonical_4_rows(tmp_path: Path) -> None:
    """`brief_inheritance_table` exposes the 4-row C-CP-13 §13.3 table verbatim."""
    registry = _registry(tmp_path)
    assert registry.brief_inheritance_table is BRIEF_AUTHORING_INHERITANCE
    assert len(registry.brief_inheritance_table) == 4
    workload_classes = {row.workload_class for row in registry.brief_inheritance_table}
    assert workload_classes == set(WorkloadClass)


def test_inheritance_for_software_engineering(tmp_path: Path) -> None:
    """SOFTWARE_ENGINEERING inherits the lead-agent binding; never reduced to Haiku."""
    registry = _registry(tmp_path)
    rule = registry.inheritance_for(WorkloadClass.SOFTWARE_ENGINEERING)
    assert rule.inheritance_rule is InheritanceRule.INHERIT_LEAD_BINDING
    assert rule.reducible_to_haiku is False


def test_inheritance_for_pipeline_automation_uses_per_stage_rule(
    tmp_path: Path,
) -> None:
    """PIPELINE_AUTOMATION inherits the per-stage lead binding (C-CP-13 §13.3)."""
    registry = _registry(tmp_path)
    rule = registry.inheritance_for(WorkloadClass.PIPELINE_AUTOMATION)
    assert rule.inheritance_rule is InheritanceRule.INHERIT_PER_STAGE_LEAD_BINDING


# ---------------------------------------------------------------------------
# AC #2 — brief schemas enforced.
# ---------------------------------------------------------------------------


def test_brief_extra_field_raises_validation_error() -> None:
    """`SubAgentBrief` is `extra="forbid"`; unknown fields raise."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SubAgentBrief(
            objective="x",
            output_format=OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT),
            guidance="y",
            task_boundaries=ClearTaskBoundaries(
                in_scope=(), out_of_scope=(), termination_criteria=()
            ),
            summary_hash="0" * 64,
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_brief_missing_required_field_raises_validation_error() -> None:
    """A missing required field raises `ValidationError`."""
    with pytest.raises(ValidationError, match="objective"):
        SubAgentBrief(  # type: ignore[call-arg]
            output_format=OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT),
            guidance="y",
            task_boundaries=ClearTaskBoundaries(
                in_scope=(), out_of_scope=(), termination_criteria=()
            ),
            summary_hash="0" * 64,
        )


def test_brief_canonicalize_is_deterministic(tmp_path: Path) -> None:
    """Two `canonicalize_brief` calls on the same brief return identical bytes."""
    registry = _registry(tmp_path)
    brief = _brief()
    a = registry.canonicalize_brief(brief)
    b = registry.canonicalize_brief(brief)
    assert a == b


def test_brief_canonicalize_excludes_summary_hash(tmp_path: Path) -> None:
    """Canonicalization excludes `summary_hash` (self-referential field).

    Two briefs differing only in `summary_hash` canonicalize to identical bytes.
    """
    registry = _registry(tmp_path)
    a = _brief()
    b = a.model_copy(update={"summary_hash": "f" * 64})
    assert registry.canonicalize_brief(a) == registry.canonicalize_brief(b)


def test_brief_summary_hash_matches_sha256_of_canonicalized_bytes(
    tmp_path: Path,
) -> None:
    """`compute_brief_summary_hash` == `sha256(canonicalize_brief(brief))`."""
    registry = _registry(tmp_path)
    brief = _brief()
    expected = hashlib.sha256(registry.canonicalize_brief(brief)).hexdigest()
    assert registry.compute_brief_summary_hash(brief) == expected


# ---------------------------------------------------------------------------
# Invariant enforcers — C-CP-12 §12.2 + C-AS-11.
# ---------------------------------------------------------------------------


def test_assert_descent_at_or_below_parent_passes(tmp_path: Path) -> None:
    """child_gate_level <= parent_gate_level passes silently."""
    registry = _registry(tmp_path)
    # Equality permitted.
    registry.assert_descent(GateLevel.DENY, GateLevel.DENY)
    # Strict descent permitted.
    registry.assert_descent(GateLevel.DENY, GateLevel.ASK)


def test_assert_descent_above_parent_raises(tmp_path: Path) -> None:
    """child_gate_level > parent_gate_level raises (§12.2 ascent prohibited)."""
    registry = _registry(tmp_path)
    with pytest.raises(ValueError, match="monotonic-descent"):
        registry.assert_descent(GateLevel.ASK, GateLevel.DENY)


def test_assert_ascent_at_or_above_parent_passes(tmp_path: Path) -> None:
    """child_sandbox_tier >= parent_sandbox_tier passes silently."""
    registry = _registry(tmp_path)
    # Equality permitted.
    registry.assert_ascent(SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_1_PROCESS)
    # Strict ascent permitted.
    registry.assert_ascent(SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_2_CONTAINER)


def test_assert_ascent_below_parent_raises(tmp_path: Path) -> None:
    """child_sandbox_tier < parent_sandbox_tier raises (C-AS-11 descent prohibited)."""
    registry = _registry(tmp_path)
    with pytest.raises(ValueError):
        registry.assert_ascent(SandboxTier.TIER_2_CONTAINER, SandboxTier.TIER_1_PROCESS)


# ---------------------------------------------------------------------------
# Stage materialization invariants.
# ---------------------------------------------------------------------------


def test_materialize_returns_stage_with_registry(tmp_path: Path) -> None:
    stage = materialize_handoff_stage(_config(tmp_path))
    assert isinstance(stage, HandoffStage)
    assert isinstance(stage.registry, RuntimeHandoffRegistry)


def test_handoff_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_handoff_stage(_config(tmp_path))
    with pytest.raises(FrozenInstanceError):
        stage.registry = RuntimeHandoffRegistry()  # type: ignore[misc]
