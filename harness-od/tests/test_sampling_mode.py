"""Tests for U-OD-11 — per-deployment-surface sampling mode + always-sampled set.

Test set per the U-OD-11 §3.4.1 (v2.5) `Tests:` field — covers acceptance
#1-#7 against C-OD-09 §9.1 / §9.2 / §9.3.
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.observability_matrix import ACTIVE_CELLS, CellID
from harness_od.sampling_mode import (
    ALWAYS_SAMPLED_EVENT_CLASSES,
    FILES_OPERATION_KIND_ATTR,
    MEMORY_OPERATION_KIND_ATTR,
    PER_DEPLOYMENT_SURFACE_SAMPLING,
    SamplingDecision,
    SamplingMode,
    is_always_sampled,
    sampling_decision,
)

# §9.2 always-sampled member set — the 18 rows, byte-exact per the §9.2 table.
_EXPECTED_ALWAYS_SAMPLED: frozenset[str] = frozenset(
    {
        "sandbox.violation",
        "sandbox.tier_escalation",
        "hitl.gate.evaluated",
        "hitl.invocation.opened",
        "hitl.invocation.responded",
        "hitl.invocation.timed_out",
        "fallback.triggered",
        "breaker.tripped",
        "topology.fanout.opened",
        "topology.fanout.closed",
        "subagent.span",
        "mcp.tool.call",
        "audit.*",
        "files.operation",
        "memory.operation",
        "validator.fail.*",
        "managed_agents.runtime",
        "skill.activation",
    }
)

_SOLO = PersonaTier.SOLO_DEVELOPER


# --- acc #1 ----------------------------------------------------------------
def test_sampling_mode_cardinality_two() -> None:
    """`SamplingMode` enumerates exactly 2 values per §9.1."""
    assert len(SamplingMode) == 2
    assert set(SamplingMode) == {
        SamplingMode.HEAD_BASED_DEV,
        SamplingMode.TAIL_BASED_PROD,
    }


# --- acc #2 ----------------------------------------------------------------
def test_per_surface_sampling_local_head_based() -> None:
    """local-development → head-based per §9.1."""
    assert (
        PER_DEPLOYMENT_SURFACE_SAMPLING[DeploymentSurface.LOCAL_DEVELOPMENT]
        is SamplingMode.HEAD_BASED_DEV
    )


def test_per_surface_sampling_self_hosted_tail_based() -> None:
    """self-hosted-server → tail-based per §9.1."""
    assert (
        PER_DEPLOYMENT_SURFACE_SAMPLING[DeploymentSurface.SELF_HOSTED_SERVER]
        is SamplingMode.TAIL_BASED_PROD
    )


def test_per_surface_sampling_managed_cloud_tail_based() -> None:
    """managed-cloud → tail-based per §9.1."""
    assert (
        PER_DEPLOYMENT_SURFACE_SAMPLING[DeploymentSurface.MANAGED_CLOUD]
        is SamplingMode.TAIL_BASED_PROD
    )


def test_per_surface_sampling_covers_all_surfaces() -> None:
    """`PER_DEPLOYMENT_SURFACE_SAMPLING` maps every `DeploymentSurface`."""
    assert set(PER_DEPLOYMENT_SURFACE_SAMPLING) == set(DeploymentSurface)


# --- acc #3 ----------------------------------------------------------------
def test_always_sampled_event_classes_cardinality_eighteen() -> None:
    """`ALWAYS_SAMPLED_EVENT_CLASSES` has cardinality 18 per §9.2."""
    assert len(ALWAYS_SAMPLED_EVENT_CLASSES) == 18


def test_always_sampled_event_class_members_byte_exact_per_9_2() -> None:
    """Member set is byte-exact against the §9.2 table (18 rows)."""
    assert ALWAYS_SAMPLED_EVENT_CLASSES == _EXPECTED_ALWAYS_SAMPLED


# --- acc #4 + acc #6 -------------------------------------------------------
def test_sampling_decision_always_sampled_event() -> None:
    """Any always-sampled event → `SAMPLE_ALWAYS` regardless of base_rate."""
    cell = CellID(
        persona_tier=_SOLO,
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
    )
    for event_class in ALWAYS_SAMPLED_EVENT_CLASSES:
        # Independent of base-rate (acc #4): low and high base-rates both
        # yield SAMPLE_ALWAYS.
        assert sampling_decision(cell, event_class, base_rate=0.0) is SamplingDecision.SAMPLE_ALWAYS
        assert sampling_decision(cell, event_class, base_rate=1.0) is SamplingDecision.SAMPLE_ALWAYS


def test_sampling_decision_base_rate_event_below_threshold() -> None:
    """A non-always-sampled event → `SAMPLE_AT_BASE_RATE` (acc #6)."""
    cell = CellID(
        persona_tier=_SOLO,
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
    )
    assert sampling_decision(cell, "chat", base_rate=0.05) is SamplingDecision.SAMPLE_AT_BASE_RATE
    assert (
        sampling_decision(cell, "execute_tool", base_rate=0.5)
        is SamplingDecision.SAMPLE_AT_BASE_RATE
    )


# --- acc #5 ----------------------------------------------------------------
def test_always_sampled_preserved_across_bridging_arc_transitions() -> None:
    """Always-sampled set is uniform across all cells (§9.3).

    Per §9.3 the always-sampled set is a hard floor at the deployment-binding
    layer and per-cell sampling within it is uniform — so for any pair of
    cells the always-sampled decision is identical (destination set is the
    same set as the source set; superset relation holds trivially). The
    8-bridging-arc traversal verification is U-OD-32's surface; U-OD-11
    supplies the invariant substrate verified here.
    """
    cells = sorted(ACTIVE_CELLS, key=lambda c: (c.persona_tier, c.deployment_surface))
    for source in cells:
        for dest in cells:
            for event_class in ALWAYS_SAMPLED_EVENT_CLASSES:
                src_decision = sampling_decision(source, event_class, base_rate=0.1)
                dst_decision = sampling_decision(dest, event_class, base_rate=0.1)
                assert src_decision is SamplingDecision.SAMPLE_ALWAYS
                assert dst_decision is SamplingDecision.SAMPLE_ALWAYS


# --- acc #7 ----------------------------------------------------------------
def test_audit_glob_in_always_sampled_set() -> None:
    """`audit.*` is in the always-sampled set (acc #7 — multi-tenant audit)."""
    assert "audit.*" in ALWAYS_SAMPLED_EVENT_CLASSES
    cell = CellID(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
    )
    assert sampling_decision(cell, "audit.*", base_rate=0.2) is SamplingDecision.SAMPLE_ALWAYS


# --- acc #3 (reliability-critical member spot check) -----------------------
def test_breaker_tripped_in_always_sampled_set() -> None:
    """`breaker.tripped` is in the always-sampled set per §9.2."""
    assert "breaker.tripped" in ALWAYS_SAMPLED_EVENT_CLASSES


# --- B7 §9.2 conditional-by-attribute rows (over-sampling refinement) -------
#
# `is_always_sampled(name, attributes)` honors the §9.2 conditional qualifiers:
# files.operation/memory.operation always-sample only at mutation `kind`;
# validator.fail.* only at `permanence=permanent`. The non-mutation / transient
# complements fall to the §10.1 base-rate regime (return False here).

_VALIDATOR_FAIL_NAME = "validator.fail.semantic_inconsistency"
# §9.2/§10.2 attribute name + value, pinned literally to the spec contract.
_PERMANENCE_ATTR = "validator.fail.permanence"


@pytest.mark.parametrize(
    ("name", "attrs", "expected"),
    [
        # files.operation — §9.2 mutation always-sample; §10.1 complement base-rate.
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "upload"}, True),
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "delete"}, True),
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "list"}, False),
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "metadata"}, False),
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "reference"}, False),
        # memory.operation — §9.2 mutation always-sample; §10.1 complement base-rate.
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "write"}, True),
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "update"}, True),
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "delete"}, True),
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "read"}, False),
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "list"}, False),
        # validator.fail.* — permanent always-sample; transient base-rate.
        (_VALIDATOR_FAIL_NAME, {_PERMANENCE_ATTR: "permanent"}, True),
        (_VALIDATOR_FAIL_NAME, {_PERMANENCE_ATTR: "transient"}, False),
    ],
)
def test_is_always_sampled_conditional_rows_honor_attributes(
    name: str, attrs: dict[str, str], expected: bool
) -> None:
    """The three §9.2 attribute-conditional rows resolve by their span
    attribute: mutation/permanent → always-sample; non-mutation/transient →
    fall to §10.1 base-rate (False)."""
    assert is_always_sampled(name, attrs) is expected


@pytest.mark.parametrize(
    "name",
    ["files.operation", "memory.operation", _VALIDATOR_FAIL_NAME],
)
def test_is_always_sampled_conditional_rows_conservative_when_attr_absent(name: str) -> None:
    """Conservative-absent: a missing discriminating attribute → always-sample
    (never under-sample the §9.3 inviolable floor). This is the byte-identical
    pre-B7 behavior for name-only callers (`attributes=None`)."""
    assert is_always_sampled(name) is True
    assert is_always_sampled(name, {}) is True
    assert is_always_sampled(name, {"unrelated.attr": "x"}) is True


def test_is_always_sampled_unconditional_rows_unaffected_by_attributes() -> None:
    """`subagent.span` stays an unconditional literal (root-ness delivered by
    the ParentBased composition, not an attribute); other set members + base-
    rate members are unaffected by attributes."""
    assert is_always_sampled("subagent.span") is True
    assert is_always_sampled("subagent.span", {"depth": "3"}) is True
    assert is_always_sampled("sandbox.violation", {FILES_OPERATION_KIND_ATTR: "list"}) is True
    # A base-rate-set member never always-samples, even with a mutation-kind attr.
    assert is_always_sampled("chat", {FILES_OPERATION_KIND_ATTR: "upload"}) is False
