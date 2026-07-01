"""B1-impl-10 — U-CP-90 `DECENTRALIZED_HANDOFF` driver strategy (CP plan v2.32 §3.2).

The 5th + LAST non-linear topology driver strategy: single-owner-at-a-time
sequential handoff. Each declared step is a per-role stage-expert (U-CP-81
`agent_role`) that OWNS the workflow in turn, then hands ownership to the next via
a `HandoffContext` (C-CP-13, existing); terminal when no further handoff;
`cascade_policy` (U-CP-85) on a stage failure.

**Non-hollow by ledger construction** (the persisted distinction the adversarial
reviewer's hollow-discriminator demands):
- vs `EVALUATOR_OPTIMIZER` (NO `branch_metadata`) — every stage persists
  `branch_metadata` (it is a per-role branch entry).
- vs `ORCHESTRATOR_WORKERS` (a STAR — every worker's `branch_metadata.parent_action_id`
  is the ONE orchestrator) — here it CHAINS: stage *i*'s
  `branch_metadata.parent_action_id` is stage *(i-1)*'s `action_id`.

Sequential ⟹ no concurrent drains (sidesteps the arc-15 F1-01 sibling-drain gap)
and stages dispatch through the ordinary `StepDispatcher` (never `SUB_AGENT_DISPATCH`),
so the live multi-stage e2e genuinely SUCCEEDS (no sync/async-bridge recursion).

Authority: `Spec_Control_Plane_v1_32.md` §25.11 (the DECENTRALIZED_HANDOFF row) +
§25.13/§25.14/§25.15 + C-CP-13 (`HandoffContext`); `Implementation_Plan_Control_Plane_v2_32.md`
§2.2 (U-CP-90).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from harness_core import PersonaTier, StepID, WorkloadClass
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    DriverContext,
    StepDispatcher,
    StepKindDispatcherNotBoundError,
    execute_workflow,
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
from harness_is.state_ledger_write import append_ledger_entry, read_ledger

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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-decentralized-handoff")

# Persona tier → resolved cascade_policy (§11.4 D4 tunable):
#   SOLO_DEVELOPER → proceed ; TEAM_BINDING → pause ; MTC → cascade-cancel.
_PROCEED_TIER = PersonaTier.SOLO_DEVELOPER
_PAUSE_TIER = PersonaTier.TEAM_BINDING
_CASCADE_CANCEL_TIER = PersonaTier.MULTI_TENANT_COMPLIANCE


def _manifest(
    *,
    workflow_id: str = "wf-dh",
    persona_tier: PersonaTier = _CASCADE_CANCEL_TIER,
) -> WorkflowManifestEntry:
    """A DECENTRALIZED_HANDOFF manifest. Admissibility is enforced at
    workflow-binding (§25.10 Invariant 2), NOT re-checked by the driver — the
    workload_class is irrelevant to execution. `persona_tier` selects the §11.4 D4
    cascade_policy the driver reads on a stage failure."""
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.DECENTRALIZED_HANDOFF,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _stage(name: str) -> WorkflowStep:
    """A stage-expert step. The role is derived from the step_id (U-RT-114 indexes
    `RoutingManifest.per_role_bindings` on it; the catalog is B4)."""
    return WorkflowStep(
        step_id=StepID(name),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"stage": name},
    )


class _RecordingLedger:
    """In-memory `LedgerWriterLike` that records drained appends in order."""

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


class _RealLedgerWriter:
    """A `LedgerWriterLike` drain sink backed by the REAL IS writer (dedup,
    timestamp-monotonicity, hash-chain construction, JSONL persistence all
    exercised — `verify_chain` then re-verifies the §6.3 chain)."""

    def __init__(self, *, handle: JsonlLedgerHandle, actor: Actor) -> None:
        self._handle = handle
        self.actor = actor

    def append(self, payload: Any, write_key: Any) -> None:
        append_ledger_entry(self._handle, payload, write_key)

    @property
    def is_genesis(self) -> bool:
        return read_ledger(self._handle) == []

    @property
    def entry_count(self) -> int:
        return len(read_ledger(self._handle))


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
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


class _HandoffDispatcher:
    """Sequential stage dispatcher. Echoes an output keyed by `step_id`, records
    the `step_context` each stage was handed (to assert the per-role context) + the
    dispatch order, and tracks max concurrency (single-owner-at-a-time → 1).
    `fail_step_ids` raises for a stage (the cascade trigger)."""

    def __init__(self, *, fail_step_ids: set[str] | None = None) -> None:
        self.contexts: dict[str, StepExecutionContext] = {}
        self.order: list[str] = []
        self._fail = fail_step_ids or set()
        self._in_flight = 0
        self.max_concurrent = 0

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self._in_flight += 1
        self.max_concurrent = max(self.max_concurrent, self._in_flight)
        try:
            step_id = str(step.step_id)
            self.contexts[step_id] = step_context
            self.order.append(step_id)
            if step_id in self._fail:
                raise RuntimeError(f"simulated stage failure at {step_id}")
            return {"role": step_id, "echoed": dict(step.step_payload)}
        finally:
            self._in_flight -= 1


class _Registry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.DECLARATIVE_STEP:
            return self._dispatcher
        raise StepKindDispatcherNotBoundError(step_kind)


def _run(
    *,
    steps: list[WorkflowStep],
    ledger: Any,
    persona_tier: PersonaTier = _CASCADE_CANCEL_TIER,
    workflow_id: str = "wf-dh",
    dispatcher: _HandoffDispatcher | None = None,
    emitter: _Emitter | None = None,
) -> tuple[Any, _HandoffDispatcher, _Emitter]:
    emitter = emitter if emitter is not None else _Emitter()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter))
    disp = dispatcher if dispatcher is not None else _HandoffDispatcher()
    result = execute_workflow(
        _manifest(workflow_id=workflow_id, persona_tier=persona_tier),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_Registry(disp),
    )
    return result, disp, emitter


def _step_entries(ledger: _RecordingLedger) -> list[Any]:
    """The drained STEP entries (branch_metadata present, terminal_status None) in
    append order — one per stage that ran."""
    return [
        p
        for p, _k in ledger.appends
        if p.branch_metadata is not None and p.branch_metadata.terminal_status is None
    ]


# ---------------------------------------------------------------------------
# Materialization + the functional AC
# ---------------------------------------------------------------------------


def test_decentralized_handoff_is_materialized_not_rejected() -> None:
    """The pattern no longer raises `TopologyPatternNotYetMaterializedError` — a
    single-stage DECENTRALIZED_HANDOFF run resolves to its strategy + SUCCEEDS."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(steps=[_stage("s0")], ledger=ledger)
    assert result.status is RunStatus.SUCCESS


def test_decentralized_handoff_three_stage_pipeline_succeeds() -> None:
    """U-CP-90 functional AC: a 3-stage pipeline runs to SUCCESS; the final_state
    carries every stage's output + the handoff chain."""
    ledger = _RecordingLedger()
    result, disp, _emitter = _run(steps=[_stage("s0"), _stage("s1"), _stage("s2")], ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert disp.order == ["s0", "s1", "s2"]  # sequential, declaration order
    assert result.final_state is not None
    assert set(result.final_state["stages"]) == {"s0", "s1", "s2"}
    assert len(result.final_state["handoffs"]) == 2  # N-1 handoffs for N stages


def test_decentralized_handoff_per_role_stage_experts() -> None:
    """Each stage is dispatched under a per-role context whose `agent_role` is the
    stage's step_id (U-CP-81 + the U-RT-114 role seam — distinct binding per stage)."""
    ledger = _RecordingLedger()
    _result, disp, _emitter = _run(
        steps=[_stage("planner"), _stage("coder"), _stage("reviewer")], ledger=ledger
    )
    assert str(disp.contexts["planner"].agent_role) == "planner"
    assert str(disp.contexts["coder"].agent_role) == "coder"
    assert str(disp.contexts["reviewer"].agent_role) == "reviewer"
    # Distinct roles per stage (the non-hollow per-role specialization).
    roles = {str(disp.contexts[sid].agent_role) for sid in ("planner", "coder", "reviewer")}
    assert len(roles) == 3


def test_decentralized_handoff_single_owner_at_a_time() -> None:
    """Single-owner-at-a-time: no two stages own the workflow concurrently — the
    dispatcher never sees more than one in-flight stage (serial ownership)."""
    ledger = _RecordingLedger()
    _result, disp, _emitter = _run(
        steps=[_stage("a"), _stage("b"), _stage("c"), _stage("d")], ledger=ledger
    )
    assert disp.max_concurrent == 1


def test_decentralized_handoff_composes_handoff_chain() -> None:
    """Ownership transfers stage-to-stage via HandoffContext (C-CP-13): N-1 handoff
    records, each `from_action_id` = the handing-off stage's action_id, `to_stage`/
    `to_role` = the next stage-expert; the action_kind is the sub-agent-boundary
    placement (C-CP-17 §17.1)."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(steps=[_stage("s0"), _stage("s1"), _stage("s2")], ledger=ledger)
    assert result.final_state is not None
    handoffs = result.final_state["handoffs"]
    assert [h["to_stage"] for h in handoffs] == ["s1", "s2"]
    assert [h["to_role"] for h in handoffs] == ["s1", "s2"]
    assert all(h["action_kind"] == "sub_agent_dispatch" for h in handoffs)
    # Each handoff's from_action_id is the prior stage's persisted step action_id.
    step_action_ids = [str(p.action_id) for p in _step_entries(ledger)]
    assert handoffs[0]["from_action_id"] == step_action_ids[0]  # s0 → s1
    assert handoffs[1]["from_action_id"] == step_action_ids[1]  # s1 → s2


def test_decentralized_handoff_terminal_when_no_further_handoff() -> None:
    """Terminal when no further handoff (structural): the LAST stage composes no
    handoff — a single-stage run produces zero handoffs."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(steps=[_stage("only")], ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["handoffs"] == []


def test_decentralized_handoff_empty_steps_success() -> None:
    """Empty step sequence → trivially SUCCESS (mirrors the other strategies)."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(steps=[], ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state == {"stages": {}, "handoffs": []}
    assert ledger.appends == []


# ---------------------------------------------------------------------------
# Non-hollow ledger discriminator (the persisted distinction)
# ---------------------------------------------------------------------------


def test_decentralized_handoff_branch_metadata_chains_not_star() -> None:
    """The non-hollow ledger signal: every stage persists `branch_metadata` (vs
    EVALUATOR_OPTIMIZER which persists NONE), and `parent_action_id` CHAINS
    stage→stage (vs ORCHESTRATOR_WORKERS where every worker shares the ONE
    orchestrator parent — a star)."""
    ledger = _RecordingLedger()
    _result, _disp, _emitter = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")], ledger=ledger, workflow_id="wf-dh"
    )
    step_entries = _step_entries(ledger)
    assert len(step_entries) == 3  # every stage persisted a branch_metadata entry
    action_ids = [str(p.action_id) for p in step_entries]
    parents = [str(p.branch_metadata.parent_action_id) for p in step_entries]
    # Stage 0 anchors at the workflow origin; each later stage chains off its predecessor.
    assert parents[0] == "workflow:wf-dh:step:0"
    assert parents[1] == action_ids[0]
    assert parents[2] == action_ids[1]
    # The chain discriminator: the parents are all DISTINCT (a star would repeat one).
    assert len(set(parents)) == 3
    # Single owner → branch_index 0 throughout (the ordering rides the chain).
    assert all(p.branch_metadata.branch_index == 0 for p in step_entries)


def test_decentralized_handoff_persists_completed_terminal_per_stage() -> None:
    """Each completed stage writes a fresh `completed` terminal entry (U-CP-84) —
    dispatch-boundary disposition, append-only (never a mutation)."""
    ledger = _RecordingLedger()
    _result, _disp, _emitter = _run(steps=[_stage("s0"), _stage("s1")], ledger=ledger)
    terminals = [
        p
        for p, _k in ledger.appends
        if p.branch_metadata is not None and p.branch_metadata.terminal_status is not None
    ]
    assert len(terminals) == 2
    assert all(p.branch_metadata.terminal_status == "completed" for p in terminals)


# ---------------------------------------------------------------------------
# cascade_policy on a stage failure (degenerate for single-owner)
# ---------------------------------------------------------------------------


def test_decentralized_handoff_cascade_cancel_on_stage_failure_fails() -> None:
    """MTC tier → cascade-cancel: a middle stage failure stops the chain → FAILED;
    the completed prefix still persists (no silent loss), the failed + later stages do not."""
    ledger = _RecordingLedger()
    result, disp, _emitter = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        ledger=ledger,
        persona_tier=_CASCADE_CANCEL_TIER,
        dispatcher=_HandoffDispatcher(fail_step_ids={"s1"}),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "decentralized-handoff-stage-failure" in result.fail_class
    assert disp.order == ["s0", "s1"]  # s2 never ran (chain stopped)
    # Only the completed prefix (s0) persisted a step entry.
    assert [str(p.action_id).split(":branch:")[0] for p in _step_entries(ledger)] == [
        "workflow:wf-dh:step:0"
    ]


def test_decentralized_handoff_proceed_on_stage_failure_partial() -> None:
    """SOLO tier → proceed: a stage failure stops the chain but salvages the
    completed-stage prefix → PARTIAL (partial_state populated, final_state None)."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        ledger=ledger,
        persona_tier=_PROCEED_TIER,
        dispatcher=_HandoffDispatcher(fail_step_ids={"s2"}),
    )
    assert result.status is RunStatus.PARTIAL
    assert result.final_state is None
    assert result.partial_state is not None
    assert set(result.partial_state["stages"]) == {"s0", "s1"}  # completed prefix salvaged


def test_decentralized_handoff_pause_no_protocol_bound_fails_honestly() -> None:
    """TEAM tier → pause with NO pause_resume_protocol bound: the snapshot cannot be
    captured, so a PAUSED would advertise an un-honorable resumability. Fail HONESTLY
    (FAILED + `...-protocol-not-bound`, salvaging the completed prefix), NEVER a false
    PAUSED — the B-HANDOFF-PAUSE materialization's honest-degradation guard (the `_run`
    helper binds no protocol)."""
    ledger = _RecordingLedger()
    result, _disp, _emitter = _run(
        steps=[_stage("s0"), _stage("s1")],
        ledger=ledger,
        persona_tier=_PAUSE_TIER,
        dispatcher=_HandoffDispatcher(fail_step_ids={"s1"}),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "decentralized-handoff-pause-resume-protocol-not-bound" in result.fail_class
    # Completed prefix (s0) still salvaged — no silent loss.
    assert result.partial_state is not None
    assert set(result.partial_state["stages"]) == {"s0"}


# ---------------------------------------------------------------------------
# Real IS ledger — hash-chain VALID through the strategy (mechanism-agnostic)
# ---------------------------------------------------------------------------


def test_decentralized_handoff_live_real_ledger_chain_valid(tmp_path: Path) -> None:
    """A 3-stage handoff on the REAL zero-tolerance IS writer (dedup + monotonic
    timestamp + hash-chain + JSONL persistence) → SUCCESS + `verify_chain` VALID.
    Sequential single-owner = no concurrent drains, so the chain composes cleanly
    (no F1-01 sibling-drain timestamp gap)."""
    handle = JsonlLedgerHandle(
        canonical_path=tmp_path / "ledger.jsonl", exists=False, entry_count=0
    )
    writer = _RealLedgerWriter(handle=handle, actor=_ACTOR)
    result, _disp, _emitter = _run(steps=[_stage("s0"), _stage("s1"), _stage("s2")], ledger=writer)
    assert result.status is RunStatus.SUCCESS
    entries = read_ledger(handle)
    # 3 stages × (step + terminal) = 6 entries, all chained.
    assert len(entries) == 6
    assert verify_chain(entries).status is VerificationStatus.VALID
    # The persisted chain discriminator survives the real writer.
    step_entries = [
        e
        for e in entries
        if e.branch_metadata is not None and e.branch_metadata.terminal_status is None
    ]
    parents = [str(e.branch_metadata.parent_action_id) for e in step_entries]
    assert len(set(parents)) == 3  # chained, not a star


# ---------------------------------------------------------------------------
# B-INTERSTEP-NONLINEAR — handoff-slice inter-step data flow (runtime spec
# §14.21 C-RT-34). Single-owner sequential ⟹ recorded INLINE on the driver
# thread (like the SINGLE_THREADED_LINEAR / EVALUATOR_OPTIMIZER sites), NOT via
# the #648 buffered-branch drain (the 3 genuinely-concurrent topologies need
# that — registered follow-ons). The CP-level witness proves the PRODUCER wiring
# (record fires in stage order; the next stage's dispatch reads most_recent_output);
# the runtime full-chain test proves the REAL provider injection through the path.
# ---------------------------------------------------------------------------


class _FakeOutputChannel:
    """Duck-typed `InterStepOutputChannel` (record + most_recent_output) — the
    cp_is_wiring idiom (the driver consumes via `getattr`, no harness_runtime import)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, step_id: str, output: Any) -> None:
        self.records.append((str(step_id), dict(output)))

    def most_recent_output(self) -> Any:
        return self.records[-1][1] if self.records else None


class _ChannelReadingHandoffDispatcher:
    """A handoff stage dispatcher that records, at EACH dispatch, what the inter-step
    channel's `most_recent_output()` is — a CP-level stand-in for the runtime LLM
    dispatcher's consumer read — so the witness observes what each stage-expert sees
    as upstream context. Returns a per-stage output keyed by step_id."""

    def __init__(self, channel: _FakeOutputChannel) -> None:
        self._channel = channel
        self.seen_upstream: list[Any] = []

    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = (binding, step_context)
        self.seen_upstream.append(self._channel.most_recent_output())
        return {"out": str(step.step_id)}


def test_decentralized_handoff_records_inter_step_output_for_next_stage() -> None:
    """B-INTERSTEP-NONLINEAR handoff slice (producer witness) — each completed stage's
    output is recorded so the NEXT stage-expert's dispatch reads it as upstream context
    (agent B sees agent A's output). Stage 0 sees nothing; each later stage sees its
    immediate predecessor (`most_recent_output()` — sequential, single-owner)."""
    ledger = _RecordingLedger()
    channel = _FakeOutputChannel()
    ctx = _Ctx(ledger=ledger, emitter=_Emitter())
    ctx.inter_step_output_channel = channel  # opt-in (RuntimeConfig.inter_step_data_flow)
    disp = _ChannelReadingHandoffDispatcher(channel)
    result = execute_workflow(
        _manifest(),
        [_stage("s0"), _stage("s1"), _stage("s2")],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_Registry(cast(StepDispatcher, disp)),
    )
    assert result.status is RunStatus.SUCCESS
    # The load-bearing inter-step flow: each stage sees its predecessor's output.
    assert disp.seen_upstream == [None, {"out": "s0"}, {"out": "s1"}]
    # Every completed stage recorded, in handoff (declaration) order.
    assert [sid for sid, _ in channel.records] == ["s0", "s1", "s2"]


def test_decentralized_handoff_opt_out_records_nothing() -> None:
    """NEGATIVE CONTROL — opt-out (`ctx.inter_step_output_channel` unbound) → the
    driver records NOTHING, so every stage sees `None` upstream (byte-identical to
    pre-arc). Proves the record() wiring is gated on the ctx binding: the dispatcher's
    own channel ref stays empty because the driver never records to it."""
    ledger = _RecordingLedger()
    channel = _FakeOutputChannel()  # handed to the dispatcher, but NOT bound on ctx
    ctx = _Ctx(ledger=ledger, emitter=_Emitter())
    # ctx.inter_step_output_channel intentionally absent → getattr(..., None) → no-op.
    disp = _ChannelReadingHandoffDispatcher(channel)
    result = execute_workflow(
        _manifest(),
        [_stage("s0"), _stage("s1"), _stage("s2")],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_Registry(cast(StepDispatcher, disp)),
    )
    assert result.status is RunStatus.SUCCESS
    assert disp.seen_upstream == [None, None, None]
    assert channel.records == []  # opt-out: the driver recorded nothing
