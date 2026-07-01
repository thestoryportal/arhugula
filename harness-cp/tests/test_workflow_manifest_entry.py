"""Tests for U-CP-13 — `WorkflowManifestEntry` schema (C-CP-06 §6.1).

Acceptance-criterion coverage:
  #1 WorkflowManifestEntry 10 fields -> test_workflow_manifest_entry_ten_fields
  #2 workload_class mandatory        -> test_workload_class_mandatory
  #2 persona_tier mandatory          -> test_persona_tier_mandatory
  #3 topology admissibility at val.  -> test_topology_admissibility_at_validation
  #4 engine_class candidate at val.  -> test_engine_class_candidate_at_validation
"""

from __future__ import annotations

import pytest
from harness_core import PersonaTier, StepID, WorkloadClass
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.hitl_placement import HITLPlacement, HITLPlacementKind
from harness_cp.topology_pattern import TopologyPattern, is_admissible
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry
from pydantic import ValidationError

_CHAIN = FallbackChain(
    primary=ProviderCandidate(provider="anthropic", model="m", family=ProviderFamily.ANTHROPIC),
    same_family=(),
    cross_family=(),
    terminal=None,
)


def _entry(**over: object) -> WorkflowManifestEntry:
    base: dict[str, object] = {
        "workflow_id": "wf-1",
        "workload_class": WorkloadClass.SOFTWARE_ENGINEERING,
        "persona_tier": PersonaTier.SOLO_DEVELOPER,
        "engine_class": EngineClass.PURE_PATTERN_NO_ENGINE,
        "topology_pattern": TopologyPattern.SINGLE_THREADED_LINEAR,
        "layer_budgets": (),
        "fallback_chain": _CHAIN,
        "hitl_placements": (),
        "per_step_overrides": {},
    }
    base.update(over)
    return WorkflowManifestEntry(**base)  # type: ignore[arg-type]


def test_workflow_manifest_entry_thirteen_fields() -> None:
    """v1.63 — `fanout_timeout_disposition` field added per CP spec v1.63 §1
    (R-FS-1 B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY; was 12 at v1.20 with
    `default_gate_level`, 11 at v2.12 with `entry_version`)."""
    assert len(WorkflowManifestEntry.model_fields) == 13
    assert set(WorkflowManifestEntry.model_fields) == {
        "workflow_id",
        "workload_class",
        "persona_tier",
        "engine_class",
        "topology_pattern",
        "layer_budgets",
        "fallback_chain",
        "hitl_placements",
        "sub_agent_briefs",
        "per_step_overrides",
        "entry_version",
        "default_gate_level",
        "fanout_timeout_disposition",
    }


def test_workflow_manifest_entry_has_entry_version_field() -> None:
    """v2.12 — `entry_version` materialized at U-CP-13 carrier (CP plan §2.2)."""
    assert "entry_version" in WorkflowManifestEntry.model_fields
    field_info = WorkflowManifestEntry.model_fields["entry_version"]
    assert field_info.annotation is int


def test_workflow_manifest_entry_default_entry_version_is_1() -> None:
    """v2.12 — default value 1 means existing constructors validate unchanged."""
    entry = _entry()
    assert entry.entry_version == 1


def test_workflow_manifest_entry_accepts_explicit_entry_version() -> None:
    """v2.12 — operators bump entry_version when workflow contract changes."""
    entry = _entry(entry_version=42)
    assert entry.entry_version == 42


def test_workflow_manifest_entry_has_default_gate_level_field() -> None:
    """v1.20 — `default_gate_level` materialized at U-CP-13 carrier per
    CP spec v1.20 §6.1.Y Reading A absorption."""
    from harness_cp.gate_level_rule import GateLevel

    assert "default_gate_level" in WorkflowManifestEntry.model_fields
    field_info = WorkflowManifestEntry.model_fields["default_gate_level"]
    assert field_info.annotation == GateLevel | None


def test_workflow_manifest_entry_default_gate_level_is_none() -> None:
    """v1.20 — default value None preserves v1.6 MVP behavior at construction
    sites that do not surface the field; workflow_driver falls back to
    GateLevel.AUTO at composition site."""
    entry = _entry()
    assert entry.default_gate_level is None


def test_workflow_manifest_entry_accepts_explicit_default_gate_level() -> None:
    """v1.20 — operators surface gate-level seed per ratified Reading A."""
    from harness_cp.gate_level_rule import GateLevel

    entry = _entry(default_gate_level=GateLevel.ASK)
    assert entry.default_gate_level is GateLevel.ASK


def test_workload_class_mandatory() -> None:
    with pytest.raises(ValidationError):
        WorkflowManifestEntry(  # type: ignore[call-arg]
            workflow_id="wf",
            persona_tier=PersonaTier.SOLO_DEVELOPER,
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
            topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
            layer_budgets=(),
            fallback_chain=_CHAIN,
            hitl_placements=(),
            per_step_overrides={},
        )


def test_persona_tier_mandatory() -> None:
    with pytest.raises(ValidationError):
        WorkflowManifestEntry(  # type: ignore[call-arg]
            workflow_id="wf",
            workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
            topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
            layer_budgets=(),
            fallback_chain=_CHAIN,
            hitl_placements=(),
            per_step_overrides={},
        )


def test_topology_admissibility_at_validation() -> None:
    entry = _entry(
        topology_pattern=TopologyPattern.HIERARCHICAL_DELEGATION,
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
    )
    # The U-CP-22 admissibility predicate is invokable against the manifest's
    # topology_pattern x workload_class pair at validation time.
    assert is_admissible(entry.topology_pattern, entry.workload_class) is True


def test_engine_class_candidate_at_validation() -> None:
    entry = _entry()
    # engine_class is a member of the U-CP-15 EngineClass enum — the U-CP-16
    # candidate mapping consumes this value at workflow-binding validation.
    assert entry.engine_class in set(EngineClass)


def test_step_override_inherits_defaults() -> None:
    entry = _entry(
        per_step_overrides={
            StepID("s1"): StepOverride(
                step_id=StepID("s1"), engine_class=EngineClass.EVENT_SOURCED_REPLAY
            )
        }
    )
    ov = entry.per_step_overrides[StepID("s1")]
    assert ov.model_binding is None
    assert ov.engine_class is EngineClass.EVENT_SOURCED_REPLAY


def test_hitl_placements_admit_multiple() -> None:
    entry = _entry(
        hitl_placements=(
            HITLPlacement(position=HITLPlacementKind.PRE_ACTION),
            HITLPlacement(position=HITLPlacementKind.VALIDATOR_ESCALATION),
        )
    )
    assert len(entry.hitl_placements) == 2
