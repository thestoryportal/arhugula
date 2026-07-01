"""B1-impl-9 — U-CP-89 `HIERARCHICAL_DELEGATION` driver strategy (CP plan v2.32 §2.2).

The FOURTH non-linear topology strategy: **recursive `ORCHESTRATOR_WORKERS` with
depth** (C-CP-25 §25.11 row). At each level `steps[0]` is the orchestrator/parent
and `steps[1:]` are its direct children (workers); a worker of kind
`SUB_AGENT_DISPATCH` recurses — its dispatcher re-enters `execute_workflow` with
the child's own manifest + step sequence (the existing C-RT-17 §14.7.4
`ChildWorkflowRunner` seam), and when that child declares `HIERARCHICAL_DELEGATION`
the recursion re-enters the strategy, so the fan-out cap 3 per parent + gate-level
descent + bottom-up barrier composition hold at EVERY level.

The strategy adds exactly two things over `ORCHESTRATOR_WORKERS` (U-CP-88), which
it **REUSES at each level (NOT a parallel re-implementation — the AC):**
(1) materialization (a manifest may declare HIERARCHICAL_DELEGATION → recursion
re-enters the capped strategy), and (2) the **fan-out cap 3 per parent**
(C-CP-10 §10.3; detect-then-refuse FAILED, never silent truncation).

Acceptance-criterion coverage (Implementation_Plan_Control_Plane_v2_32.md
U-CP-89):
  materialization (no-longer-raises) + reuse:
      → test_hierarchical_delegation_single_level_runs_like_orchestrator_workers
      → test_hierarchical_delegation_reuses_orchestrator_workers_aggregate_shape
  fan-out cap 3 per parent (C-CP-10 §10.3, detect-then-refuse):
      → test_hierarchical_delegation_fanout_cap_exceeded_fails_loud
      → test_hierarchical_delegation_cap_boundary_three_children_not_rejected
      → test_hierarchical_delegation_cap_re_enforced_at_each_recursion_level
  2-level delegation (genuine depth; bottom-up composition):
      → test_hierarchical_delegation_two_level_delegation_composes_bottom_up
  gate-level monotonic descent across depth (C-CP-12 §12.2; HONEST — see below):
      → test_hierarchical_delegation_gate_level_monotonic_across_depth
      → test_sub_agent_descent_is_equality_default_recorded_not_applied
  persisted branch-causality at depth:
      → test_hierarchical_delegation_branch_causality_at_depth
  deterministic-append (branch-index order, NOT completion order):
      → test_hierarchical_delegation_persisted_in_branch_index_order
  cascade-cancel idempotency (resume-terminality, obl. 7):
      → test_hierarchical_delegation_cascade_cancel_terminality
  nested barrier (the U-CP-89 property U-CP-88 could not exercise):
      → test_hierarchical_delegation_outer_deadline_bounds_parent_over_wedged_grandchild
  cross-level timestamp monotonicity on the real zero-tolerance ledger (drain-time
  re-stamping — physical-append-order == timestamp-order by construction, §25.12):
      → test_hierarchical_delegation_live_real_ledger_chain_valid_at_depth
      → test_hierarchical_delegation_live_real_ledger_chain_valid_with_linear_child

**Gate-level descent honesty (C-CP-12 §12.2 is monotonic-≤, equality the valid
default).** `dispatch_sub_agent` ALWAYS returns `child_gate_level ==
parent_gate_level` (the blast-radius downgrade rides `child_blast_radius_ceiling`,
not the gate level), and `child_workflow_runner` drops the computed descent —
the child re-seeds its executed gate from its own manifest (pre-existing v1.6 MVP
child-context sharing). So these tests assert the monotonic INVARIANT (never
ascends) across the genuine 2-level tree, with a genuine non-equal descent driven
by the child manifest's declared `default_gate_level` (honestly attributed — NOT
a harness-computed strict descent). The recorded-not-applied seam is documented
at `.harness/class_3_hierarchical_delegation_descent_recorded_not_applied.md`.

Authority: `Spec_Control_Plane_v1_32.md` §25.10/§25.11/§25.13/§25.15 +
`Implementation_Plan_Control_Plane_v2_32.md` §2.2 (U-CP-89).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import ActionID, PersonaTier, StepID, WorkloadClass
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.sub_agent_brief import (
    ClearTaskBoundaries,
    OutputSchema,
    OutputSchemaKind,
    SubAgentBrief,
    compute_brief_summary_hash,
)
from harness_cp.sub_agent_gate_level_descent import dispatch_sub_agent
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
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-hierarchical-delegation")

# Persona tier → resolved cascade_policy (§11.4 D4 tunable):
#   SOLO_DEVELOPER → proceed ; TEAM_BINDING → pause ; MTC → cascade-cancel.
_PROCEED_TIER = PersonaTier.SOLO_DEVELOPER
_CASCADE_CANCEL_TIER = PersonaTier.MULTI_TENANT_COMPLIANCE


def _manifest(
    *,
    workflow_id: str = "wf-hd",
    persona_tier: PersonaTier = _PROCEED_TIER,
    default_gate_level: GateLevel | None = None,
    topology_pattern: TopologyPattern = TopologyPattern.HIERARCHICAL_DELEGATION,
) -> WorkflowManifestEntry:
    """A HIERARCHICAL_DELEGATION manifest (default). Admissibility is enforced at
    workflow-binding (§25.10 Invariant 2), NOT re-checked by the driver — the
    workload_class is irrelevant to execution; PIPELINE_AUTOMATION reuses the
    known persona→cascade_policy mapping. `default_gate_level` seeds
    `resolve_parent_gate_level` (the C-CP-12 §12.2 descent root for this level).
    `topology_pattern` lets a child manifest declare a DIFFERENT topology (e.g. a
    SINGLE_THREADED_LINEAR child of a hierarchical parent)."""
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=topology_pattern,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
        default_gate_level=default_gate_level,
    )


def _orchestrator_step(name: str = "orchestrator") -> WorkflowStep:
    # `name` distinguishes per-level orchestrators in a recursion (the dispatcher
    # records `step_context` by step_id; same-named orchestrators at two levels
    # would collide in that dict — distinct names keep each level observable).
    return WorkflowStep(
        step_id=StepID(name),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"role": name},
    )


def _leaf_worker(name: str) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(name),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"name": name},
    )


def _sub_agent_worker(
    name: str,
    *,
    child_manifest: WorkflowManifestEntry,
    child_steps: list[WorkflowStep],
) -> WorkflowStep:
    """A worker that recurses: a `SUB_AGENT_DISPATCH` step whose payload carries
    the child workflow's manifest + step sequence (typed-at-dispatcher per
    C-CP-25 §25.3.3.4 opaque-to-driver discipline; mirrors the real
    `SubAgentDispatchPayload`)."""
    return WorkflowStep(
        step_id=StepID(name),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload={"child_manifest": child_manifest, "child_steps": child_steps},
    )


def _level(orchestrator_then_workers: list[WorkflowStep]) -> list[WorkflowStep]:
    """[orchestrator, *workers] — convenience for readability."""
    return orchestrator_then_workers


class _RecordingLedger:
    """In-memory `LedgerWriterLike` that records drained appends in order. Append
    is lock-guarded — the recursing child run drains to this SAME sink from a
    `cp-fanout` worker thread (mirroring the real shared-ledger child context),
    so the list append must be thread-safe (the real IS writer holds a lock)."""

    actor: Actor

    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []
        self._lock = threading.Lock()

    def append(self, payload: Any, write_key: Any) -> Any:
        with self._lock:
            self.appends.append((payload, write_key))
        return "appended"

    @property
    def is_genesis(self) -> bool:
        return len(self.appends) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appends)


class _RealLedgerWriter:
    """A `LedgerWriterLike` drain sink backed by the REAL IS writer (dedup,
    timestamp-monotonicity, hash-chain construction, JSONL persistence all
    exercised — `verify_chain` then re-verifies the §6.3 chain)."""

    def __init__(self, *, handle: JsonlLedgerHandle, actor: Actor) -> None:
        self._handle = handle
        self.actor = actor
        self.results: list[WriteResult] = []
        self._lock = threading.Lock()

    def append(self, payload: Any, write_key: Any) -> None:
        with self._lock:
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
        self._lock = threading.Lock()

    def emit(self, event_class: WorkflowEventClass) -> None:
        with self._lock:
            self.emits.append(event_class)


class _Ctx:
    """Minimal fake `DriverContext` (the strategy reads
    `procedural_tier_snapshot_resolver` via `getattr(..., None)` — absent → None)."""

    def __init__(self, *, ledger: Any, emitter: _Emitter) -> None:
        import asyncio

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


class _HierarchicalDispatcher:
    """Mirrors the REAL runtime `SUB_AGENT_DISPATCH` dispatcher + child runner.

    - `DECLARATIVE_STEP` leaves (orchestrators + leaf workers) echo an output
      keyed by `step_id` and record the `step_context` they were handed (so a
      test can read the gate-level / branch / role at each level).
    - `SUB_AGENT_DISPATCH` steps RECURSE: re-enter `execute_workflow` with the
      child's own manifest + step sequence, sharing the parent `ctx` +
      `step_dispatchers` registry — EXACTLY as `child_workflow_runner._runner`
      does (the harness-computed descent is recorded-not-applied; the child
      re-seeds its executed gate from its own manifest). Child SUCCESS →
      `final_state` becomes this step's output; child FAILED → raise (the
      orchestrator-workers cascade trigger).

    `registry` is set after the registry wraps this dispatcher (the child reuses
    the same registry, so a `SUB_AGENT_DISPATCH` grandchild recurses again)."""

    def __init__(
        self,
        *,
        ctx: DriverContext,
        fail_step_ids: set[str] | None = None,
        block_step_ids: set[str] | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.ctx = ctx
        self.registry: StepDispatcherRegistry | None = None
        self.contexts: dict[str, StepExecutionContext] = {}
        self._fail = fail_step_ids or set()
        self._block = block_step_ids or set()
        self._release = release

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.contexts[step_id] = step_context
        if step.step_kind is StepKind.SUB_AGENT_DISPATCH:
            assert self.registry is not None, "registry must be wired before dispatch"
            child_manifest = cast(WorkflowManifestEntry, step.step_payload["child_manifest"])
            child_steps = cast(list[WorkflowStep], step.step_payload["child_steps"])
            child_result = execute_workflow(
                child_manifest,
                child_steps,
                run_id=f"child-run-{step_id}",
                ctx=self.ctx,
                default_model_binding=_DEFAULT_BINDING,
                step_dispatchers=self.registry,
            )
            if child_result.status is RunStatus.FAILED:
                raise RuntimeError(f"sub-agent child failed: {child_result.fail_class}")
            return {"role": step_id, "child": dict(child_result.final_state or {})}
        # A leaf step.
        if step_id in self._block and self._release is not None:
            assert self._release.wait(timeout=5.0), f"{step_id} never released"
        if step_id in self._fail:
            raise RuntimeError(f"simulated failure at {step_id}")
        return {"role": step_id, "echoed": dict(step.step_payload)}


class _Registry:
    """Binds the hierarchical dispatcher for both DECLARATIVE_STEP (orchestrators
    + leaf workers) and SUB_AGENT_DISPATCH (the recursion primitive)."""

    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind in (StepKind.DECLARATIVE_STEP, StepKind.SUB_AGENT_DISPATCH):
            return self._dispatcher
        raise StepKindDispatcherNotBoundError(step_kind)


def _run(
    *,
    steps: list[WorkflowStep],
    ledger: Any,
    persona_tier: PersonaTier = _PROCEED_TIER,
    default_gate_level: GateLevel | None = None,
    workflow_id: str = "wf-hd",
    dispatcher: _HierarchicalDispatcher | None = None,
    emitter: _Emitter | None = None,
) -> tuple[Any, _HierarchicalDispatcher, _Emitter]:
    emitter = emitter if emitter is not None else _Emitter()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter))
    disp = dispatcher if dispatcher is not None else _HierarchicalDispatcher(ctx=ctx)
    registry = cast(StepDispatcherRegistry, _Registry(cast(StepDispatcher, disp)))
    disp.registry = registry
    result = execute_workflow(
        _manifest(
            workflow_id=workflow_id,
            persona_tier=persona_tier,
            default_gate_level=default_gate_level,
        ),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=registry,
    )
    return result, disp, emitter


def _branch_entries(ledger: _RecordingLedger) -> list[Any]:
    """Drained payloads carrying branch_metadata, in drain order."""
    return [payload for payload, _wk in ledger.appends if payload.branch_metadata is not None]


def _brief() -> SubAgentBrief:
    boundaries = ClearTaskBoundaries(
        in_scope=("a",), out_of_scope=("b",), termination_criteria=("c",)
    )
    out_fmt = OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT)

    def _build(h: str) -> SubAgentBrief:
        return SubAgentBrief(
            objective="o",
            output_format=out_fmt,
            guidance="g",
            task_boundaries=boundaries,
            summary_hash=h,
        )

    return _build(compute_brief_summary_hash(_build("0" * 64)))


# ---------------------------------------------------------------------------
# Materialization + reuse (the AC: "reuses ORCHESTRATOR_WORKERS, NOT a re-impl")
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_single_level_runs_like_orchestrator_workers() -> None:
    """A single HIERARCHICAL_DELEGATION level (orchestrator + leaf workers, no
    recursion) runs through the reused ORCHESTRATOR_WORKERS machinery → SUCCESS
    with the deterministic fold. (Materialization: no longer raises.)"""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(
        steps=_level([_orchestrator_step(), _leaf_worker("w0"), _leaf_worker("w1")]),
        ledger=ledger,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state["worker_outputs"]) == {"w0", "w1"}


def test_hierarchical_delegation_reuses_orchestrator_workers_aggregate_shape() -> None:
    """The aggregate is the ORCHESTRATOR_WORKERS fold shape verbatim
    (`{orchestrator, worker_outputs}`) — the strategy reuses U-CP-88's
    `_aggregate_orchestrator_workers`, not a parallel re-implementation."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(
        steps=_level([_orchestrator_step(), _leaf_worker("w0")]), ledger=ledger
    )
    assert result.final_state is not None
    assert set(result.final_state) == {"orchestrator", "worker_outputs"}


# ---------------------------------------------------------------------------
# Fan-out cap 3 per parent (C-CP-10 §10.3 — detect-then-refuse, no truncation)
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_fanout_cap_exceeded_fails_loud() -> None:
    """A level with > 3 DIRECT children (4 workers) is rejected detect-then-refuse:
    FAILED + the cap fail_class, NO workflow.start emit, NO ledger append (parity
    with the topology/engine entry gate — never a silent truncation)."""
    ledger = _RecordingLedger()
    result, _disp, emitter = _run(
        steps=_level(
            [
                _orchestrator_step(),
                _leaf_worker("w0"),
                _leaf_worker("w1"),
                _leaf_worker("w2"),
                _leaf_worker("w3"),
            ]
        ),
        ledger=ledger,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "hierarchical-delegation-fanout-cap-exceeded" in result.fail_class
    assert "4 children" in result.fail_class
    # Detect-then-refuse: no side effects before the refusal.
    assert emitter.emits == []
    assert ledger.appends == []


def test_hierarchical_delegation_cap_boundary_three_children_not_rejected() -> None:
    """Contrasting baseline — exactly 3 children is AT the cap, NOT rejected
    (the cap is `> 3`, not `>= 3`): the run proceeds to SUCCESS."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(
        steps=_level(
            [_orchestrator_step(), _leaf_worker("w0"), _leaf_worker("w1"), _leaf_worker("w2")]
        ),
        ledger=ledger,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state["worker_outputs"]) == {"w0", "w1", "w2"}


def test_hierarchical_delegation_cap_re_enforced_at_each_recursion_level() -> None:
    """The cap re-checks at EVERY level whose child manifest declares
    HIERARCHICAL_DELEGATION: a root within cap whose recursing child exceeds the
    cap fails — the child's cap-FAILED raises in the sub-agent dispatcher →
    propagates as the root worker's failure (cascade)."""
    # Child level: 4 grandchildren → exceeds the cap when the child re-enters
    # the (recursive) HIERARCHICAL_DELEGATION strategy.
    child_manifest = _manifest(workflow_id="wf-hd-child")
    child_steps = _level(
        [
            _orchestrator_step(),
            _leaf_worker("g0"),
            _leaf_worker("g1"),
            _leaf_worker("g2"),
            _leaf_worker("g3"),
        ]
    )
    # Root: cascade-cancel so a worker (child) failure → run-level FAILED.
    root_steps = _level(
        [
            _orchestrator_step(),
            _sub_agent_worker("sub", child_manifest=child_manifest, child_steps=child_steps),
        ]
    )
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(
        steps=root_steps, ledger=ledger, persona_tier=_CASCADE_CANCEL_TIER
    )
    assert result.status is RunStatus.FAILED


# ---------------------------------------------------------------------------
# 2-level delegation (genuine depth; bottom-up composition) — the headline AC
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_two_level_delegation_composes_bottom_up() -> None:
    """A 2-level delegation: root orchestrator + a SUB_AGENT_DISPATCH worker that
    recurses into a HIERARCHICAL_DELEGATION child (its own orchestrator + ≤3
    grandchildren). Each parent barriers on its children and composes bottom-up —
    the child's fold appears nested inside the root worker's output. Reuses
    ORCHESTRATOR_WORKERS at BOTH levels."""
    child_manifest = _manifest(workflow_id="wf-hd-child")
    child_steps = _level([_orchestrator_step(), _leaf_worker("g0"), _leaf_worker("g1")])
    root_steps = _level(
        [
            _orchestrator_step(),
            _leaf_worker("w0"),
            _sub_agent_worker("sub", child_manifest=child_manifest, child_steps=child_steps),
        ]
    )
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(steps=root_steps, ledger=ledger)

    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    worker_outputs = result.final_state["worker_outputs"]
    # Both root workers folded; the recursing worker's output carries the child's
    # OWN bottom-up fold (grandchildren composed upward into it).
    assert set(worker_outputs) == {"w0", "sub"}
    child_fold = worker_outputs["sub"]["child"]
    assert set(child_fold["worker_outputs"]) == {"g0", "g1"}


# ---------------------------------------------------------------------------
# Gate-level monotonic descent across depth (C-CP-12 §12.2 — HONEST)
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_gate_level_monotonic_across_depth() -> None:
    """The executed gate-level NEVER ascends across the genuine 2-level tree
    (C-CP-12 §12.2 monotonic invariant). The within-level descent is the §12.2
    equality default (`compose_branch_child_context` copies the parent gate); the
    genuine non-equal descent here is the child manifest's DECLARED lower gate
    (AUTO < ASK) — honestly attributed to the manifest, NOT a harness-computed
    strict descent (`dispatch_sub_agent` returns equality; the runner drops it —
    see `test_sub_agent_descent_is_equality_default_recorded_not_applied`)."""
    child_manifest = _manifest(workflow_id="wf-hd-child", default_gate_level=GateLevel.AUTO)
    child_steps = _level([_orchestrator_step("orch-child"), _leaf_worker("g0")])
    root_steps = _level(
        [
            _orchestrator_step("orch-root"),
            _sub_agent_worker("sub", child_manifest=child_manifest, child_steps=child_steps),
        ]
    )
    ledger = _RecordingLedger()
    # Root declares ASK; child declares AUTO (rank AUTO=0 < ASK=1 → strict descent).
    result, disp, _emitter = _run(steps=root_steps, ledger=ledger, default_gate_level=GateLevel.ASK)
    assert result.status is RunStatus.SUCCESS

    # Root-level leaves (orchestrator + the recursing worker context) see ASK.
    assert disp.contexts["orch-root"].parent_gate_level == GateLevel.ASK
    assert disp.contexts["sub"].parent_gate_level == GateLevel.ASK
    # Child-level leaf (grandchild) sees AUTO — strictly below the root gate.
    assert disp.contexts["g0"].parent_gate_level == GateLevel.AUTO
    # Monotonic invariant: the child gate never ascends above the parent gate
    # (AUTO rank 0 <= ASK rank 1; C-CP-12 §12.2).
    assert _gate_rank(GateLevel.AUTO) <= _gate_rank(GateLevel.ASK)


def _gate_rank(level: GateLevel) -> int:
    return {GateLevel.AUTO: 0, GateLevel.ASK: 1, GateLevel.DENY: 2}[level]


def test_sub_agent_descent_is_equality_default_recorded_not_applied() -> None:
    """The recorded-not-applied seam (the Class-3 honesty note): the
    harness-computed sub-agent descent (`dispatch_sub_agent`, C-CP-12 §12.2)
    returns `child_gate_level == parent_gate_level` (equality default — the
    blast-radius downgrade rides `child_blast_radius_ceiling`, not the gate
    level). The cross-level EXECUTED descent in the test above comes from the
    child manifest, NOT this computed value (the runner drops it)."""
    descent = dispatch_sub_agent(
        parent_action_id=ActionID("workflow:wf-hd:step:1"),
        parent_gate_level=GateLevel.ASK,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        sub_agent_brief=_brief(),
        operator_override=None,
    )
    assert descent.child_gate_level == GateLevel.ASK  # equality — recorded-not-applied
    assert descent.child_gate_level == descent.parent_gate_level


# ---------------------------------------------------------------------------
# Persisted branch-causality at depth
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_branch_causality_at_depth() -> None:
    """Branch causality persists at EVERY level and is globally unique across the
    recursion: each level's branch entries carry `(parent_action_id, branch_index)`
    scoped to THAT level's orchestrator action_id, and the two levels' parents are
    distinct (different `workflow_id` namespaces — `workflow:wf-hd:step:0` for the
    root level, `workflow:wf-hd-child:step:0` for the child level) → no cross-level
    causality collision (IS spec v1.8 §5.4 global-action_id uniqueness at depth).

    NB: the cross-level *link* (root worker → child workflow) is written by the
    runtime `SUB_AGENT_DISPATCH` dispatch-audit entry (`_compose_and_persist_audit`,
    pre-existing U-RT-59), NOT by the driver — out of U-CP-89's scope; this CP-axis
    test-double omits it, so the assertion is on per-level causality + uniqueness."""
    child_manifest = _manifest(workflow_id="wf-hd-child")
    child_steps = _level([_orchestrator_step("orch-child"), _leaf_worker("g0"), _leaf_worker("g1")])
    root_steps = _level(
        [
            _orchestrator_step("orch-root"),
            _leaf_worker("w0"),
            _sub_agent_worker("sub", child_manifest=child_manifest, child_steps=child_steps),
        ]
    )
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(steps=root_steps, ledger=ledger)
    assert result.status is RunStatus.SUCCESS

    parents = {str(e.branch_metadata.parent_action_id) for e in _branch_entries(ledger)}
    # Both levels persisted branch causality, each scoped to its own workflow_id
    # orchestrator — distinct parents, no cross-level collision.
    assert "workflow:wf-hd:step:0" in parents
    assert "workflow:wf-hd-child:step:0" in parents
    # Every persisted branch identity (parent_action_id, branch_index) is globally
    # unique across the whole recursion (no two branch *step* entries collide).
    identities = [
        (str(e.branch_metadata.parent_action_id), e.branch_metadata.branch_index)
        for e in _branch_entries(ledger)
        if e.branch_metadata.terminal_status is None
    ]
    assert len(identities) == len(set(identities)), (
        f"branch identities not unique at depth: {identities}"
    )


# ---------------------------------------------------------------------------
# Deterministic-append (branch-index order, NOT completion order)
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_persisted_in_branch_index_order() -> None:
    """Workers complete in REVERSE branch-index order (a hard sync point, not
    timing) yet the drained branch entries persist in branch-index order — the
    §25.12 deterministic-append guarantee carried through the reused
    ORCHESTRATOR_WORKERS drain."""
    n_workers = 3
    events = {i: threading.Event() for i in range(n_workers)}

    class _ReverseDispatcher:
        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            sid = str(step.step_id)
            if sid == "orchestrator":
                return {"role": "orchestrator"}
            idx = int(step.step_payload["name"].removeprefix("w"))
            higher = idx + 1
            if higher < n_workers:
                assert events[higher].wait(timeout=10.0), f"worker {higher} never completed"
            events[idx].set()
            return {"role": sid, "index": idx}

    ledger = _RecordingLedger()
    emitter = _Emitter()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter))
    registry = cast(StepDispatcherRegistry, _Registry(cast(StepDispatcher, _ReverseDispatcher())))
    result = execute_workflow(
        _manifest(),
        _level([_orchestrator_step(), *(_leaf_worker(f"w{i}") for i in range(n_workers))]),
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=registry,
    )
    assert result.status is RunStatus.SUCCESS
    branch_indices = [e.branch_metadata.branch_index for e in _branch_entries(ledger)]
    assert branch_indices == sorted(branch_indices)


# ---------------------------------------------------------------------------
# Cascade-cancel idempotency (resume-terminality, obl. 7)
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_cascade_cancel_terminality() -> None:
    """Under cascade-cancel (MTC), a worker failure terminates the fan-out and
    every branch persists a discriminating `terminal_status`; `resume_should_
    redispatch` is False for each persisted terminal (obl. 7 — no double-dispatch
    on resume). Carried through the reused ORCHESTRATOR_WORKERS cascade machinery."""
    ledger = _RecordingLedger()
    emitter = _Emitter()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter))
    disp = _HierarchicalDispatcher(ctx=ctx, fail_step_ids={"w0"})
    registry = cast(StepDispatcherRegistry, _Registry(cast(StepDispatcher, disp)))
    disp.registry = registry
    result = execute_workflow(
        _manifest(persona_tier=_CASCADE_CANCEL_TIER),
        _level([_orchestrator_step(), _leaf_worker("w0"), _leaf_worker("w1"), _leaf_worker("w2")]),
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=registry,
    )
    assert result.status is RunStatus.FAILED
    terminals = [
        e.branch_metadata.terminal_status
        for e in _branch_entries(ledger)
        if e.branch_metadata.terminal_status is not None
    ]
    assert terminals, "cascade-cancel must persist discriminating terminal_status per branch"
    for terminal in terminals:
        assert resume_should_redispatch(terminal) is False
    # A never-dispatched branch (None) WOULD re-dispatch — the contrasting control.
    assert resume_should_redispatch(None) is True


# ---------------------------------------------------------------------------
# Nested barrier — outer deadline bounds the parent over a wedged grandchild
# (the U-CP-89 property U-CP-88 could not exercise; `_BRANCH_INFLIGHT_DISPATCHES`
# is EXTENDED, not replaced, at each nested barrier — workflow_driver.py:924)
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_outer_deadline_bounds_parent_over_wedged_grandchild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grandchild dispatch wedges (blocks well past the deadline). The OUTER
    (root) barrier deadline still bounds the root's return — proving the inner
    in-flight dispatch registered in the OUTER barrier's `_BRANCH_INFLIGHT_
    DISPATCHES` chain (extended, not replaced). The root returns FAILED in ~the
    deadline, NOT the grandchild's full block time."""
    import harness_cp.workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_FANOUT_BARRIER_DEADLINE_SECONDS", 0.5)
    release = threading.Event()

    child_manifest = _manifest(workflow_id="wf-hd-child", persona_tier=_CASCADE_CANCEL_TIER)
    # The grandchild "g-wedge" blocks on `release` (capped at 5s so no thread
    # leaks past the test); the 0.5s OUTER deadline must cut it well before then.
    child_steps = _level([_orchestrator_step(), _leaf_worker("g-wedge")])
    root_steps = _level(
        [
            _orchestrator_step(),
            _sub_agent_worker("sub", child_manifest=child_manifest, child_steps=child_steps),
        ]
    )

    ledger = _RecordingLedger()
    emitter = _Emitter()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter))
    disp = _HierarchicalDispatcher(ctx=ctx, block_step_ids={"g-wedge"}, release=release)
    registry = cast(StepDispatcherRegistry, _Registry(cast(StepDispatcher, disp)))
    disp.registry = registry

    started = time.monotonic()
    try:
        result = execute_workflow(
            _manifest(persona_tier=_CASCADE_CANCEL_TIER),
            root_steps,
            run_id="run-1",
            ctx=ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,
        )
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert result.status is RunStatus.FAILED
    # The OUTER 0.5s deadline cut the wedged grandchild — the parent did NOT wait
    # the grandchild's 5s block. Generous 3s ceiling absorbs scheduling jitter.
    assert elapsed < 3.0, (
        f"outer deadline did not bound the parent over a wedged grandchild ({elapsed:.2f}s)"
    )


# ---------------------------------------------------------------------------
# Live e2e — real IS writer; §6.3 hash chain re-verifies post-drain AT DEPTH
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_live_real_ledger_chain_valid_at_depth(tmp_path: Path) -> None:
    """A genuine 2-level delegation through the REAL IS writer: root + child
    entries persist across the recursion (dedup / timestamp-monotonicity /
    hash-chain construction all exercised), then `verify_chain` re-verifies the
    §6.3 chain VALID post-drain — proving the recursive drain composes a valid
    single-parent chain at depth."""
    child_manifest = _manifest(workflow_id="wf-hd-child")
    child_steps = _level([_orchestrator_step(), _leaf_worker("g0"), _leaf_worker("g1")])
    root_steps = _level(
        [
            _orchestrator_step(),
            _leaf_worker("w0"),
            _sub_agent_worker("sub", child_manifest=child_manifest, child_steps=child_steps),
        ]
    )
    handle = JsonlLedgerHandle(
        canonical_path=tmp_path / "ledger.jsonl", exists=False, entry_count=0
    )
    writer = _RealLedgerWriter(handle=handle, actor=_ACTOR)
    result, _disp, _emitter = _run(steps=root_steps, ledger=writer)

    assert result.status is RunStatus.SUCCESS
    entries = read_ledger(handle)
    assert verify_chain(entries).status is VerificationStatus.VALID


def test_hierarchical_delegation_live_real_ledger_chain_valid_with_linear_child(
    tmp_path: Path,
) -> None:
    """The advisor's trap, guarded: a hierarchical parent whose SUB_AGENT_DISPATCH
    worker recurses into a SINGLE_THREADED_LINEAR child (the most common sub-agent
    child) on the REAL zero-tolerance ledger. The linear inline path appends each
    step at its real `now()` DURING the parent's barrier; the parent BUFFERS its
    entries and drains them LATE (post-barrier). Because `drain_branch_buffers`
    re-stamps the parent's buffered entries to the drain moment (after the child's
    inline appends), physical-append-order == timestamp-order by construction →
    `verify_chain` VALID. (Were the parent's entries stamped at fan-out START
    instead, they'd precede the child's later inline timestamps →
    NonMonotonicTimestampError — the cross-level inversion drain-time stamping
    dissolves; `[[test-bypass-as-runtime-truth-pattern]]` one level up.)"""
    child_manifest = _manifest(
        workflow_id="wf-hd-lin-child",
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
    )
    # A linear child = a flat step sequence (no orchestrator/worker split); each
    # step appends inline with its own timestamp.
    child_steps = [_leaf_worker("lin0"), _leaf_worker("lin1"), _leaf_worker("lin2")]
    root_steps = _level(
        [
            _orchestrator_step("orch-root"),
            _leaf_worker("w0"),
            _sub_agent_worker("sub", child_manifest=child_manifest, child_steps=child_steps),
        ]
    )
    handle = JsonlLedgerHandle(
        canonical_path=tmp_path / "ledger.jsonl", exists=False, entry_count=0
    )
    writer = _RealLedgerWriter(handle=handle, actor=_ACTOR)
    result, _disp, _emitter = _run(steps=root_steps, ledger=writer)

    assert result.status is RunStatus.SUCCESS
    entries = read_ledger(handle)
    assert verify_chain(entries).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3/§4) — opt-in TOP-LEVEL synthesis step
# ---------------------------------------------------------------------------


class _HDLeafEchoDispatcher:
    """Minimal DECLARATIVE_STEP echo for a single-level HIERARCHICAL witness."""

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        return {"role": str(step.step_id), "echoed": dict(step.step_payload)}


class _HDSynthesisCapturingDispatcher:
    def __init__(self) -> None:
        self.received_siblings: Any = None

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self.received_siblings = step_context.sibling_outputs
        return {"synthesis": "hd-composed", "n": len(step_context.sibling_outputs)}


class _HDBranchOrSynthesisRegistry:
    def __init__(self, branch: StepDispatcher, synthesis: StepDispatcher) -> None:
        self._branch = branch
        self._synthesis = synthesis

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.DECLARATIVE_STEP:
            return self._branch
        if step_kind is StepKind.POST_JOIN_SYNTHESIS:
            return self._synthesis
        raise StepKindDispatcherNotBoundError(step_kind)


def _hd_synthesis_step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"prompt": "compose"},
    )


def test_hierarchical_top_level_post_join_synthesis_replaces_compose_and_not_capped() -> None:
    """A terminal POST_JOIN_SYNTHESIS step at the TOP HIERARCHICAL level REPLACES
    the deterministic compose on SUCCESS, composing the branch-index-ordered leaf
    siblings. With 3 leaf workers AT the cap-3 + a synthesis (5 steps total), the
    run SUCCEEDS — proving the synthesis is carved OUT of the branch set and NOT
    counted toward the fan-out cap (CP spec v1.54 §3 — top-level only)."""
    ledger = _RecordingLedger()
    synth = _HDSynthesisCapturingDispatcher()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=_Emitter()))
    result = execute_workflow(
        _manifest(),
        [
            _orchestrator_step(),
            _leaf_worker("leaf-0"),
            _leaf_worker("leaf-1"),
            _leaf_worker("leaf-2"),
            _hd_synthesis_step(),
        ],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry,
            _HDBranchOrSynthesisRegistry(_HDLeafEchoDispatcher(), synth),
        ),
    )
    # NOT FAILED-cap-exceeded: the synthesis was carved out → 3 workers (= cap).
    assert result.status is RunStatus.SUCCESS
    assert result.final_state == {"synthesis": "hd-composed", "n": 3}
    assert [bi for bi, _o in synth.received_siblings] == [0, 1, 2]
    assert any(str(wk.step_id).startswith("post-join-synthesis") for _p, wk in ledger.appends)


def test_hierarchical_without_synthesis_uses_deterministic_compose() -> None:
    """Negative control: absent a synthesis step, the deterministic compose is
    byte-identical to pre-v1.54 (no synthesis entry)."""
    ledger = _RecordingLedger()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=_Emitter()))
    result = execute_workflow(
        _manifest(),
        [_orchestrator_step(), _leaf_worker("leaf-0"), _leaf_worker("leaf-1")],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry,
            _HDBranchOrSynthesisRegistry(
                _HDLeafEchoDispatcher(), _HDSynthesisCapturingDispatcher()
            ),
        ),
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state) == {"orchestrator", "worker_outputs"}
    assert not any(str(wk.step_id).startswith("post-join-synthesis") for _p, wk in ledger.appends)
