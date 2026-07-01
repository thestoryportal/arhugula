"""H_T-CP-19 Layer 3 e2e — `default_gate_level` cross-deployment monotonicity.

Verifies the end-to-end chain: `WorkflowManifestEntry.default_gate_level`
(operator-supplied seed) → `workflow_driver.resolve_parent_gate_level()`
(CP spec v1.20 §6.1.Y composition site) → `StepExecutionContext.parent_gate_level`
→ `dispatch_sub_agent(parent_gate_level=...)` → `SubAgentGateLevelDescent`
(C-CP-12 §12.2 monotonic-descent invariant).

Closes the H_T-CP-19 Layer 3 verification gate per the operator-deferred Q3
ratification at `.harness/class_1_fork_h_t_cp_19_default_gate_level_spec_extension.md`.
Reframing per advisor 2026-05-27: D5 §1.5.2 monotonicity does not require a
multi-deployment substrate — the contract is `ManifestEntry → driver → descent`,
which is in-process. Two manifest fixtures with distinct `default_gate_level`
values + the resolved descent at the sub-agent boundary IS the contract surface.

Coverage:
  - resolve_parent_gate_level — None → AUTO fallback (v1.6 MVP)
  - resolve_parent_gate_level — explicit AUTO / ASK / DENY flow through
  - chain — manifest.default_gate_level → step_context.parent_gate_level
  - chain — step_context.parent_gate_level → SubAgentGateLevelDescent.child_gate_level
  - monotonicity — assert_monotonic_descent rejects ascent above manifest seed
  - cross-deployment — two manifests differing only in default_gate_level
    produce distinct child gate levels at the sub-agent boundary
"""

from __future__ import annotations

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import ActionID, PersonaTier, WorkloadClass
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.sub_agent_brief import (
    ClearTaskBoundaries,
    OutputSchema,
    OutputSchemaKind,
    SubAgentBrief,
)
from harness_cp.sub_agent_gate_level_descent import (
    assert_monotonic_descent,
    dispatch_sub_agent,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import resolve_parent_gate_level
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

_CHAIN = FallbackChain(
    primary=ProviderCandidate(provider="anthropic", model="m", family=ProviderFamily.ANTHROPIC),
    same_family=(),
    cross_family=(),
    terminal=None,
)


def _manifest(default_gate_level: GateLevel | None = None) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id="wf-1",
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
        default_gate_level=default_gate_level,
    )


def _brief() -> SubAgentBrief:
    return SubAgentBrief(
        objective="objective",
        output_format=OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT),
        guidance="g",
        task_boundaries=ClearTaskBoundaries(
            in_scope=("a",), out_of_scope=("b",), termination_criteria=("c",)
        ),
        summary_hash="0" * 64,
    )


# --- §1 resolve_parent_gate_level — composition expression at v1.20 §6.1.Y ---


def test_resolve_parent_gate_level_none_falls_back_to_auto() -> None:
    """v1.6 MVP behavior preserved: None → GateLevel.AUTO."""
    entry = _manifest(default_gate_level=None)
    assert resolve_parent_gate_level(entry) is GateLevel.AUTO


def test_resolve_parent_gate_level_explicit_auto_flows_through() -> None:
    """Explicit AUTO surfaces as AUTO (semantically identical to None at runtime)."""
    entry = _manifest(default_gate_level=GateLevel.AUTO)
    assert resolve_parent_gate_level(entry) is GateLevel.AUTO


def test_resolve_parent_gate_level_explicit_ask_flows_through() -> None:
    """Operator-supplied ASK preserves stricter posture at the seed."""
    entry = _manifest(default_gate_level=GateLevel.ASK)
    assert resolve_parent_gate_level(entry) is GateLevel.ASK


def test_resolve_parent_gate_level_explicit_deny_flows_through() -> None:
    """Operator-supplied DENY preserves strictest posture at the seed."""
    entry = _manifest(default_gate_level=GateLevel.DENY)
    assert resolve_parent_gate_level(entry) is GateLevel.DENY


# --- §2 Manifest → sub-agent descent chain (D5 §1.5.2 monotonicity) ---------


@pytest.mark.parametrize(
    "default_gate_level,expected",
    [
        (None, GateLevel.AUTO),
        (GateLevel.AUTO, GateLevel.AUTO),
        (GateLevel.ASK, GateLevel.ASK),
        (GateLevel.DENY, GateLevel.DENY),
    ],
)
def test_manifest_default_gate_level_chains_to_sub_agent_descent(
    default_gate_level: GateLevel | None, expected: GateLevel
) -> None:
    """End-to-end chain: manifest.default_gate_level → resolve_parent_gate_level
    → dispatch_sub_agent(parent_gate_level=...) → descent.child_gate_level.

    The monotonic-descent default at C-CP-12 §12.2 admits equality, so
    child_gate_level == parent_gate_level when no operator override is supplied.
    Verifies the seed flows unmodified from manifest to the sub-agent boundary.
    """
    entry = _manifest(default_gate_level=default_gate_level)
    parent_gate_level = resolve_parent_gate_level(entry)
    assert parent_gate_level is expected

    descent = dispatch_sub_agent(
        parent_action_id=ActionID("act-001"),
        parent_gate_level=parent_gate_level,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        sub_agent_brief=_brief(),
        operator_override=None,
    )
    assert descent.parent_gate_level is expected
    assert descent.child_gate_level is expected


def test_monotonic_descent_rejects_child_ascent_above_manifest_seed() -> None:
    """C-CP-12 §12.2 monotonic-descent: a child cannot escalate to a stricter
    gate level than the parent. When the manifest seeds AUTO, a sub-agent
    cannot ascend to ASK or DENY — assert_monotonic_descent raises ValueError.
    Rank ordering at gate_level_rule.py: AUTO=0 < ASK=1 < DENY=2.
    """
    entry = _manifest(default_gate_level=GateLevel.AUTO)
    parent_gate_level = resolve_parent_gate_level(entry)

    with pytest.raises(ValueError, match="monotonic-descent violated"):
        assert_monotonic_descent(
            parent_gate_level=parent_gate_level,
            child_gate_level=GateLevel.ASK,
        )
    with pytest.raises(ValueError, match="monotonic-descent violated"):
        assert_monotonic_descent(
            parent_gate_level=parent_gate_level,
            child_gate_level=GateLevel.DENY,
        )


def test_monotonic_descent_admits_equality_and_strict_descent() -> None:
    """Under a stricter manifest seed (DENY), a child MAY remain at DENY
    (equality) or descend to ASK or AUTO (relaxation). The descent direction
    is permitted; only ascent (child rank > parent rank) raises.
    """
    entry = _manifest(default_gate_level=GateLevel.DENY)
    parent_gate_level = resolve_parent_gate_level(entry)

    # All three are <= DENY in rank — no raise.
    assert_monotonic_descent(parent_gate_level=parent_gate_level, child_gate_level=GateLevel.DENY)
    assert_monotonic_descent(parent_gate_level=parent_gate_level, child_gate_level=GateLevel.ASK)
    assert_monotonic_descent(parent_gate_level=parent_gate_level, child_gate_level=GateLevel.AUTO)


# --- §3 Cross-deployment monotonicity (two manifests, distinct outcomes) ----


def test_cross_deployment_two_manifests_produce_distinct_child_gate_levels() -> None:
    """The D5 cross-deployment monotonicity contract per ADR-D5 §1.5.2:
    operators on distinct deployment surfaces who supply distinct
    `default_gate_level` values produce distinct effective gate levels at
    the sub-agent boundary. ZERO multi-deployment substrate required —
    the contract is `ManifestEntry → driver → descent`, in-process per
    advisor reframing 2026-05-27.
    """
    deployment_a = _manifest(default_gate_level=GateLevel.AUTO)
    deployment_b = _manifest(default_gate_level=GateLevel.ASK)

    descent_a = dispatch_sub_agent(
        parent_action_id=ActionID("act-A"),
        parent_gate_level=resolve_parent_gate_level(deployment_a),
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        sub_agent_brief=_brief(),
        operator_override=None,
    )
    descent_b = dispatch_sub_agent(
        parent_action_id=ActionID("act-B"),
        parent_gate_level=resolve_parent_gate_level(deployment_b),
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        sub_agent_brief=_brief(),
        operator_override=None,
    )

    assert descent_a.child_gate_level is GateLevel.AUTO
    assert descent_b.child_gate_level is GateLevel.ASK
    assert descent_a.child_gate_level is not descent_b.child_gate_level


def test_cross_deployment_stricter_seed_dominates_against_default_seed() -> None:
    """A deployment that seeds stricter (DENY) than the v1.6 MVP default
    (AUTO via None) produces a strictly stricter child gate level. This is
    the monotonicity claim: operator-supplied stricter values are always
    respected at the sub-agent boundary; the runtime never silently
    downgrades the seed.
    """
    permissive = _manifest(default_gate_level=None)
    strict = _manifest(default_gate_level=GateLevel.DENY)

    descent_permissive = dispatch_sub_agent(
        parent_action_id=ActionID("act-permissive"),
        parent_gate_level=resolve_parent_gate_level(permissive),
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        sub_agent_brief=_brief(),
        operator_override=None,
    )
    descent_strict = dispatch_sub_agent(
        parent_action_id=ActionID("act-strict"),
        parent_gate_level=resolve_parent_gate_level(strict),
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        sub_agent_brief=_brief(),
        operator_override=None,
    )

    assert descent_permissive.child_gate_level is GateLevel.AUTO
    assert descent_strict.child_gate_level is GateLevel.DENY
