"""Tests for U-CP-31 — `topology.*` + `subagent.*` namespace schemas.

Acceptance-criterion coverage (C-CP-14 §14.2):
  #1 topology.* 10 attributes verbatim -> test_topology_namespace_cardinality_ten,
                                          test_topology_attributes_match_spec_verbatim
  #2 subagent.* 7 attributes verbatim  -> test_subagent_namespace_cardinality_seven,
                                          test_subagent_attributes_match_spec_verbatim
  #3 SubAgentResultStatus 3 values     -> test_sub_agent_result_status_cardinality_three
  #4 D6 ingestion at U-CP-54 §24.1.A   -> structural (export-manifest consumer)
"""

from __future__ import annotations

from harness_cp.schema_attribute_enums import AttributeValueType, Cardinality
from harness_cp.topology_subagent_namespace import (
    SUBAGENT_NAMESPACE_SCHEMA,
    TOPOLOGY_NAMESPACE_SCHEMA,
    SubAgentAttributeSchema,
    SubAgentResultStatus,
    TopologyAttributeSchema,
)


def test_topology_namespace_cardinality_ten() -> None:
    """Acceptance #1 — exactly 10 `topology.*` attributes."""
    assert len(TOPOLOGY_NAMESPACE_SCHEMA) == 10
    assert all(isinstance(a, TopologyAttributeSchema) for a in TOPOLOGY_NAMESPACE_SCHEMA)


def test_topology_attributes_match_spec_verbatim() -> None:
    """Acceptance #1 — the 10 attribute names match C-CP-14 §14.2 verbatim."""
    names = [a.attribute_name for a in TOPOLOGY_NAMESPACE_SCHEMA]
    assert names == [
        "topology.pattern",
        "topology.fan_out_cap",
        "topology.cascade_policy",
        "topology.workload_class",
        "topology.concurrent_token_budget_at_dispatch",
        "topology.results_collected",
        "topology.results_failed",
        "topology.cascade_applied",
        "topology.synthesis_token_budget",
        "topology.cascade_decision_audit_ledger_id",
    ]
    for a in TOPOLOGY_NAMESPACE_SCHEMA:
        assert isinstance(a.value_type, AttributeValueType)
        assert isinstance(a.cardinality, Cardinality)
        assert a.emitted_on  # span name present


def test_subagent_namespace_cardinality_seven() -> None:
    """Acceptance #2 — exactly 7 `subagent.*` attributes."""
    assert len(SUBAGENT_NAMESPACE_SCHEMA) == 7
    assert all(isinstance(a, SubAgentAttributeSchema) for a in SUBAGENT_NAMESPACE_SCHEMA)


def test_subagent_attributes_match_spec_verbatim() -> None:
    """Acceptance #2 — the 7 attribute names match C-CP-14 §14.2 verbatim."""
    names = [a.attribute_name for a in SUBAGENT_NAMESPACE_SCHEMA]
    assert names == [
        "subagent.span.id",
        "subagent.parent_span_id",
        "subagent.result_status",
        "subagent.request_blocked_by_budget",
        "subagent.tokens_in",
        "subagent.tokens_out",
        "subagent.cached_tokens_in",
    ]
    for a in SUBAGENT_NAMESPACE_SCHEMA:
        assert isinstance(a.value_type, AttributeValueType)
        assert isinstance(a.cardinality, Cardinality)
        assert a.emitted_on


def test_sub_agent_result_status_cardinality_four() -> None:
    """`SubAgentResultStatus` declares exactly 4 values (B-HIERARCHICAL-PAUSE added
    `paused` for the child-pause span path — runtime spec §14.7.2 step 7)."""
    assert len(SubAgentResultStatus) == 4
    assert {s.value for s in SubAgentResultStatus} == {
        "completed",
        "failed",
        "cascade-cancelled",
        "paused",
    }
