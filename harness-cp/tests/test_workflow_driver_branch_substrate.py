"""B1-impl-2 branch substrate — U-CP-80 + U-CP-81 (CP plan v2.32 §2.2).

U-CP-80 — driver-strategy dispatch table (C-CP-25 §25.10): the dispatch table
enumerates all six `TopologyPattern` members; `SINGLE_THREADED_LINEAR`
resolves to the inline-loop strategy, the five non-linear patterns raise
`TopologyPatternNotYetMaterializedError` until their strategy units land.

U-CP-81 — branch `StepExecutionContext` composition (C-CP-25 §25.11/§25.12/
§25.14): `compose_branch_child_context` composes a path-aware branch child
context (causality + role fields + descended gate-level). The
`SINGLE_THREADED_LINEAR` path composes no branch child context (the e2e
regression for that lives at `test_workflow_driver.py`).

Authority: `Spec_Control_Plane_v1_32.md` §25.10/§25.11/§25.12/§25.14 +
`Implementation_Plan_Control_Plane_v2_32.md` §2.2 (U-CP-80/U-CP-81).
"""

from __future__ import annotations

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_cp.cp_shared_types import AgentRole
from harness_cp.gate_level_rule import _RANK, GateLevel
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    _DRIVER_STRATEGY_DISPATCH,
    _DriverStrategyStatus,
    resolve_driver_strategy,
)
from harness_cp.workflow_driver_errors import TopologyPatternNotYetMaterializedError
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    compose_branch_child_context,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-branch-substrate")

# The non-linear patterns still NOT_YET_MATERIALIZED after U-CP-86 landed
# PARALLELIZATION (the fan-out-barrier-aggregate strategy), U-CP-87 landed
# EVALUATOR_OPTIMIZER (the sequential generate→evaluate→regenerate loop),
# U-CP-88 landed ORCHESTRATOR_WORKERS (the orchestrator-dispatch-collect fan-out),
# and U-CP-89 landed HIERARCHICAL_DELEGATION (recursive bounded-fan-out reusing
# ORCHESTRATOR_WORKERS). All four are excluded — each resolves to its own
# `_DriverStrategyStatus` member and no longer raises (the per-strategy
# `no-longer-raises` AC); their e2e behavior lives at
# `test_workflow_driver_parallelization.py` +
# `test_workflow_driver_evaluator_optimizer.py` +
# `test_workflow_driver_orchestrator_workers.py` +
# `test_workflow_driver_hierarchical_delegation.py`. U-CP-90 landed
# DECENTRALIZED_HANDOFF (single-owner sequential handoff) — the FIFTH + LAST
# non-linear pattern; its e2e lives at
# `test_workflow_driver_decentralized_handoff.py`. ALL SIX TopologyPattern values
# are now materialized; NO pattern remains NOT_YET_MATERIALIZED (the sentinel
# status + `TopologyPatternNotYetMaterializedError` are retained for any FUTURE
# pattern — exercised via monkeypatch below).


def _linear_step_context(
    *,
    parent_action_id: str = "workflow:wf-1:step:0",
    parent_gate_level: GateLevel = GateLevel.ASK,
) -> StepExecutionContext:
    """A per-step context as the SINGLE_THREADED_LINEAR path composes it
    (no branch fields)."""
    return StepExecutionContext(
        workflow_id="wf-1",
        parent_action_id=parent_action_id,
        parent_gate_level=parent_gate_level,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=_ACTOR,
        parent_entry_hash="",
        parent_idempotency_key="k",
        tenant_id=None,
        step_index=0,
    )


# ---------------------------------------------------------------------------
# U-CP-80 — driver-strategy dispatch table (C-CP-25 §25.10)
# ---------------------------------------------------------------------------


def test_dispatch_table_enumerates_every_topology_pattern() -> None:
    """Exhaustiveness — the table keys are exactly the closed-at-6
    `TopologyPattern` enum (no member missing → resolution never falls
    through to a KeyError; a strategy lands by flipping its entry)."""
    assert set(_DRIVER_STRATEGY_DISPATCH) == set(TopologyPattern)


def test_single_threaded_linear_resolves_to_linear_inline() -> None:
    """SINGLE_THREADED_LINEAR is materialized — resolves to the inline-loop
    strategy, no raise (regression-safe gate)."""
    assert (
        resolve_driver_strategy(TopologyPattern.SINGLE_THREADED_LINEAR)
        is _DriverStrategyStatus.LINEAR_INLINE
    )


def test_no_topology_pattern_remains_not_yet_materialized() -> None:
    """U-CP-90 landed the LAST strategy (DECENTRALIZED_HANDOFF) — all six
    `TopologyPattern` values are materialized, so NO dispatch entry is
    `NOT_YET_MATERIALIZED` and `resolve_driver_strategy` never raises for a real
    pattern (the `no-longer-raises` AC, now complete across all six)."""
    assert all(
        _DRIVER_STRATEGY_DISPATCH[p] is not _DriverStrategyStatus.NOT_YET_MATERIALIZED
        for p in TopologyPattern
    )
    for p in TopologyPattern:
        resolve_driver_strategy(p)  # no raise for any real pattern


def test_not_yet_materialized_sentinel_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retained sentinel mechanism: a pattern mapped to `NOT_YET_MATERIALIZED`
    (synthesized via monkeypatch — none is, post-U-CP-90) STILL raises the typed
    error at `resolve_driver_strategy`. Guards against a future pattern landing in
    the table un-materialized and silently running instead of raising."""
    monkeypatch.setitem(
        _DRIVER_STRATEGY_DISPATCH,
        TopologyPattern.DECENTRALIZED_HANDOFF,
        _DriverStrategyStatus.NOT_YET_MATERIALIZED,
    )
    with pytest.raises(TopologyPatternNotYetMaterializedError):
        resolve_driver_strategy(TopologyPattern.DECENTRALIZED_HANDOFF)


def test_parallelization_resolves_to_its_materialized_strategy() -> None:
    """U-CP-86 — PARALLELIZATION is materialized: its dispatch entry is the
    PARALLELIZATION strategy status and `resolve_driver_strategy` no longer
    raises (the `no-longer-raises` AC for this pattern)."""
    assert (
        _DRIVER_STRATEGY_DISPATCH[TopologyPattern.PARALLELIZATION]
        is _DriverStrategyStatus.PARALLELIZATION
    )
    assert (
        resolve_driver_strategy(TopologyPattern.PARALLELIZATION)
        is _DriverStrategyStatus.PARALLELIZATION
    )


def test_evaluator_optimizer_resolves_to_its_materialized_strategy() -> None:
    """U-CP-87 — EVALUATOR_OPTIMIZER is materialized: its dispatch entry is the
    EVALUATOR_OPTIMIZER strategy status and `resolve_driver_strategy` no longer
    raises (the `no-longer-raises` AC for this pattern)."""
    assert (
        _DRIVER_STRATEGY_DISPATCH[TopologyPattern.EVALUATOR_OPTIMIZER]
        is _DriverStrategyStatus.EVALUATOR_OPTIMIZER
    )
    assert (
        resolve_driver_strategy(TopologyPattern.EVALUATOR_OPTIMIZER)
        is _DriverStrategyStatus.EVALUATOR_OPTIMIZER
    )


def test_orchestrator_workers_resolves_to_its_materialized_strategy() -> None:
    """U-CP-88 — ORCHESTRATOR_WORKERS is materialized: its dispatch entry is the
    ORCHESTRATOR_WORKERS strategy status and `resolve_driver_strategy` no longer
    raises (the `no-longer-raises` AC for this pattern)."""
    assert (
        _DRIVER_STRATEGY_DISPATCH[TopologyPattern.ORCHESTRATOR_WORKERS]
        is _DriverStrategyStatus.ORCHESTRATOR_WORKERS
    )
    assert (
        resolve_driver_strategy(TopologyPattern.ORCHESTRATOR_WORKERS)
        is _DriverStrategyStatus.ORCHESTRATOR_WORKERS
    )


def test_hierarchical_delegation_resolves_to_its_materialized_strategy() -> None:
    """U-CP-89 — HIERARCHICAL_DELEGATION is materialized: its dispatch entry is the
    HIERARCHICAL_DELEGATION strategy status and `resolve_driver_strategy` no longer
    raises (the `no-longer-raises` AC for this pattern)."""
    assert (
        _DRIVER_STRATEGY_DISPATCH[TopologyPattern.HIERARCHICAL_DELEGATION]
        is _DriverStrategyStatus.HIERARCHICAL_DELEGATION
    )
    assert (
        resolve_driver_strategy(TopologyPattern.HIERARCHICAL_DELEGATION)
        is _DriverStrategyStatus.HIERARCHICAL_DELEGATION
    )


def test_decentralized_handoff_resolves_to_its_materialized_strategy() -> None:
    """U-CP-90 — DECENTRALIZED_HANDOFF is materialized (the LAST pattern): its
    dispatch entry is the DECENTRALIZED_HANDOFF strategy status and
    `resolve_driver_strategy` no longer raises (the `no-longer-raises` AC for the
    final pattern)."""
    assert (
        _DRIVER_STRATEGY_DISPATCH[TopologyPattern.DECENTRALIZED_HANDOFF]
        is _DriverStrategyStatus.DECENTRALIZED_HANDOFF
    )
    assert (
        resolve_driver_strategy(TopologyPattern.DECENTRALIZED_HANDOFF)
        is _DriverStrategyStatus.DECENTRALIZED_HANDOFF
    )


# ---------------------------------------------------------------------------
# U-CP-81 — branch StepExecutionContext composition (C-CP-25 §25.11/12/14)
# ---------------------------------------------------------------------------


def test_linear_context_has_no_branch_fields() -> None:
    """A per-step context composed the linear way carries no branch fields
    (the defaults are None)."""
    ctx = _linear_step_context()
    assert ctx.branch_index is None
    assert ctx.agent_role is None


def test_compose_branch_child_sets_causality_and_role() -> None:
    """The branch child context carries branch_index, agent_role, and the
    spawning step's action_id set VERBATIM as parent_action_id (IS spec
    v1.8 §5.4 — no branch_path at the causality key)."""
    parent = _linear_step_context(parent_action_id="workflow:wf-1:step:3")
    child = compose_branch_child_context(parent, branch_index=2, agent_role=AgentRole("planner"))
    assert child.branch_index == 2
    assert child.agent_role == AgentRole("planner")
    assert child.parent_action_id == "workflow:wf-1:step:3"


def test_compose_branch_child_inherits_non_branch_fields() -> None:
    """Non-branch fields are inherited verbatim from the spawning context."""
    parent = _linear_step_context()
    child = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("worker"))
    assert child.workflow_id == parent.workflow_id
    assert child.parent_sandbox_tier == parent.parent_sandbox_tier
    assert child.parent_actor == parent.parent_actor
    assert child.parent_entry_hash == parent.parent_entry_hash
    assert child.parent_idempotency_key == parent.parent_idempotency_key
    assert child.tenant_id == parent.tenant_id
    assert child.step_index == parent.step_index


def test_compose_branch_child_gate_level_descends_monotonically() -> None:
    """The child gate-level descends monotonically (<= parent) per
    C-CP-12 §12.2 — equality is the valid §12.2 default."""
    for parent_gate in (GateLevel.AUTO, GateLevel.ASK, GateLevel.DENY):
        parent = _linear_step_context(parent_gate_level=parent_gate)
        child = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("worker"))
        assert _RANK[child.parent_gate_level] <= _RANK[parent.parent_gate_level]


def test_compose_branch_child_rejects_negative_branch_index() -> None:
    """branch_index must be >= 0 (IS spec v1.8 §5.4)."""
    parent = _linear_step_context()
    with pytest.raises(ValueError, match="branch_index"):
        compose_branch_child_context(parent, branch_index=-1, agent_role=AgentRole("x"))


def test_compose_branch_child_is_frozen() -> None:
    """The composed branch context is still frozen (extra=forbid, frozen)."""
    parent = _linear_step_context()
    child = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("worker"))
    with pytest.raises(ValueError, match="frozen"):
        child.branch_index = 5  # type: ignore[misc]


def test_sibling_branches_share_parent_action_id_distinct_by_branch_index() -> None:
    """Two sibling branches under the SAME spawning step share its action_id
    (set verbatim) and are discriminated by branch_index — so the pair
    (parent_action_id, branch_index) is distinct (IS spec v1.8 §5.4:
    branch_index is unique per parent_action_id)."""
    parent = _linear_step_context(parent_action_id="workflow:wf-1:step:3")
    b0 = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("w"))
    b1 = compose_branch_child_context(parent, branch_index=1, agent_role=AgentRole("w"))
    # Both carry the spawning step's action_id verbatim (no path extension).
    assert b0.parent_action_id == b1.parent_action_id == "workflow:wf-1:step:3"
    # The pair is distinct via the local ordinal.
    assert (b0.parent_action_id, b0.branch_index) != (b1.parent_action_id, b1.branch_index)


def test_nested_fanout_identity_rests_on_global_action_id_uniqueness() -> None:
    """The nested-uniqueness property (U-CP-81 functional AC) per IS spec
    v1.8 §5.4: `(parent_action_id, branch_index)` uniquely identifies a
    branch even under NESTED fan-out — BECAUSE `action_id` is globally
    unique per IS §5 and the spawning step's action_id is set VERBATIM (no
    branch_path; Route X action_id-encoding was rejected).

    A nested branch's `parent_action_id` is the inner spawning step's
    action_id. The branch-step action_id composition (U-CP-82+) must yield
    globally-unique action_ids for steps inside sibling branches (the IS §5
    invariant); given that, two same-ordinal nested branches under DISTINCT
    inner steps get distinct identities. This composer passes the spawning
    action_id through verbatim — it does NOT synthesize identity, so it
    neither creates nor collapses the distinction.
    """
    # Two distinct inner spawning steps — e.g. step 7 inside sibling outer
    # branches 0 and 1 — carry globally-unique action_ids per IS §5 (the
    # invariant U-CP-82+ branch-step composition honors).
    inner_in_0 = _linear_step_context(parent_action_id="workflow:wf-1:step:3:branch:0:step:7")
    inner_in_1 = _linear_step_context(parent_action_id="workflow:wf-1:step:3:branch:1:step:7")

    nested_0 = compose_branch_child_context(inner_in_0, branch_index=0, agent_role=AgentRole("w"))
    nested_1 = compose_branch_child_context(inner_in_1, branch_index=0, agent_role=AgentRole("w"))

    # parent_action_id is the spawning step's action_id VERBATIM (no extension).
    assert nested_0.parent_action_id == "workflow:wf-1:step:3:branch:0:step:7"
    assert nested_1.parent_action_id == "workflow:wf-1:step:3:branch:1:step:7"
    # Same local ordinal (k=0); distinct because the spawning action_ids differ.
    assert nested_0.branch_index == nested_1.branch_index == 0
    assert (nested_0.parent_action_id, nested_0.branch_index) != (
        nested_1.parent_action_id,
        nested_1.branch_index,
    )
