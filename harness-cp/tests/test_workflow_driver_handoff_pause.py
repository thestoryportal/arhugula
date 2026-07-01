"""B-HANDOFF-PAUSE (R-FS-1) — resumable `cascade_policy=pause` for `DECENTRALIZED_HANDOFF`.

The single-owner sequential analogue of `B-FANOUT-PAUSE` (ORCHESTRATOR_WORKERS, #661) /
`B-FANOUT-PAUSE-PARALLELIZATION` (#679) / `B-HIERARCHICAL-PAUSE` (#680): a stage failure
under `cascade_policy=pause` (TEAM tier) with a bound `pause_resume_protocol` captures a
`HandoffResumeState` STAGE CURSOR (the contiguous completed prefix + their recovered
outputs + the declared stage count) and returns PAUSED; `api.resume`
(`execute_workflow(pause_snapshot_input=...)`) re-walks the body, RECOVERS the completed
prefix (NOT re-dispatched), and re-dispatches from the cursor stage onward.

THE LOAD-BEARING WITNESSES (handoff causality is the non-hollow signal — a naive
"resume → PAUSED → SUCCESS" passes while silently breaking the chain):
1. the completed prefix's dispatcher is NEVER re-invoked across pause+resume (at-most-once);
2. the resumed stage's `parent_action_id` chains off the LAST COMPLETED stage's `action_id`
   (NOT re-anchored to the workflow origin `workflow:{wf}:step:0`).

Materializes the §25.15.1 `pause → PAUSED` row EXTENDED to single-owner sequential per
§25.18's named `DECENTRALIZED_HANDOFF` impl-order (CP spec v1.46). No operator gate.
"""

from __future__ import annotations

import asyncio
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
from harness_cp.handoff_context import StateSummary
from harness_cp.pause_resume_protocol import PauseResumeProtocol, _compute_snapshot_hash
from harness_cp.pause_resume_protocol_types import (
    HandoffResumeState,
    HandoffStageResumeState,
    PauseSnapshot,
    WorkflowPauseReason,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    DriverContext,
    StepDispatcher,
    StepDispatcherRegistry,
    StepKindDispatcherNotBoundError,
    execute_workflow,
)
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier

_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-handoff-pause")
_PAUSE_TIER = PersonaTier.TEAM_BINDING  # → cascade_policy = pause
_PROCEED_TIER = PersonaTier.SOLO_DEVELOPER  # → cascade_policy = proceed
_ANCHOR = "0" * 64  # constant MVP pause-context anchor (no material diff on resume)
_WF = "wf-dh-pause"
_ORIGIN = f"workflow:{_WF}:step:0"


def _manifest(persona_tier: PersonaTier = _PAUSE_TIER) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=_WF,
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
    return WorkflowStep(
        step_id=StepID(name),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"stage": name},
    )


class _RecordingLedger:
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


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
        self.emits.append(event_class)


def _pause_context_reader() -> tuple[StateSummary, str]:
    return (
        StateSummary(
            relevant_entries=(),
            summary_text="",
            summary_hash="0" * 64,
            idempotency_key=Identifier(""),
            external_references=(),
        ),
        _ANCHOR,
    )


def _protocol() -> PauseResumeProtocol:
    return PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=_pause_context_reader,
    )


class _CtxP:
    """Driver context WITH a bound `pause_resume_protocol` so the handoff `pause`
    branch can capture a snapshot + return PAUSED, and the entry-point resume
    detection can validate + admit `execute_workflow(pause_snapshot_input=...)`."""

    def __init__(self, *, ledger: Any, emitter: _Emitter) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = emitter
        self.drained_flag = asyncio.Event()
        self.pause_requested_flag = asyncio.Event()
        self.pause_resume_protocol = _protocol()
        self.ledger_reader = None
        self.tracer_provider = NoOpTracerProvider()
        self.validator_framework = None
        self.tenant_id = None
        # B-INTERSTEP-HANDOFF — no inter-step channel bound (opt-out); the data-flow
        # re-seed path is exercised separately where a channel IS bound.
        self.inter_step_output_channel = None


class _Registry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.DECLARATIVE_STEP:
            return self._dispatcher
        raise StepKindDispatcherNotBoundError(step_kind)


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _Registry(dispatcher))


class _HandoffDispatcher:
    """Records each dispatched stage's `step_id` + the `parent_action_id` of the
    `step_context` it was handed (the handoff-chain witness). `fail_step_ids` raises
    for a stage (the cascade trigger)."""

    def __init__(self, *, fail_step_ids: set[str] | None = None) -> None:
        self._fail = fail_step_ids or set()
        self.dispatched: list[str] = []
        self.parent_action_ids: dict[str, str] = {}

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_context is not None:
            self.parent_action_ids[step_id] = step_context.parent_action_id
        if step_id in self._fail:
            raise RuntimeError(f"simulated stage failure at {step_id}")
        return {"role": step_id, "echoed": dict(step.step_payload)}


def _run(
    *,
    steps: list[WorkflowStep],
    dispatcher: StepDispatcher,
    ctx: DriverContext,
    pause_snapshot_input: PauseSnapshot | None = None,
    persona_tier: PersonaTier = _PAUSE_TIER,
) -> Any:
    return execute_workflow(
        _manifest(persona_tier),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
        pause_snapshot_input=pause_snapshot_input,
    )


# ---------------------------------------------------------------------------
# Capture — a real handoff pause returns PAUSED + a handoff-aware snapshot
# ---------------------------------------------------------------------------


def test_handoff_pause_with_protocol_returns_paused_with_handoff_snapshot() -> None:
    """TEAM persona → pause, protocol bound: stage s2 fails after s0+s1 complete →
    the run PAUSES (not the interim FAILED) with a hash-valid `PauseSnapshot` carrying
    a `HandoffResumeState` (the contiguous completed prefix s0,s1 + their recovered
    outputs + the declared stage count)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(fail_step_ids={"s2"}),
        ctx=ctx,
    )
    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    hr = snap.handoff_resume
    assert hr is not None
    # ONLY the handoff carrier is populated (mutually exclusive with the fan-out ones).
    assert snap.fan_out_resume is None
    assert snap.peer_fan_out_resume is None
    assert hr.stage_count == 3
    # The completed PREFIX (s0, s1) is captured contiguously with recovered outputs.
    assert [cs.stage_index for cs in hr.completed_stages] == [0, 1]
    assert [cs.step_id for cs in hr.completed_stages] == ["s0", "s1"]
    assert hr.completed_stages[0].output == {"role": "s0", "echoed": {"stage": "s0"}}
    assert hr.completed_stages[1].output == {"role": "s1", "echoed": {"stage": "s1"}}
    # The snapshot is hash-valid (covers handoff_resume) + the failed stage index.
    assert snap.step_index == 2
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        handoff_resume=hr,
    )
    # The completed prefix salvages as partial_state (no silent loss).
    assert result.partial_state is not None
    assert set(result.partial_state["stages"]) == {"s0", "s1"}


def test_handoff_pause_emits_resumption_not_workflow_start_on_resume() -> None:
    """The resume envelope emits RESUMPTION (the completed prefix already ran in the
    original envelope), not a second WORKFLOW_START."""
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(fail_step_ids={"s2"}),
        ctx=pause_ctx,
    )
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    emitter = _Emitter()
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=emitter))
    _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(),  # clean → s2 succeeds on resume
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    assert WorkflowEventClass.WORKFLOW_START not in emitter.emits


# ---------------------------------------------------------------------------
# Resume — the discriminating full-chain witnesses
# ---------------------------------------------------------------------------


def test_handoff_resume_recovers_prefix_exactly_once_and_succeeds() -> None:
    """THE AT-MOST-ONCE WITNESS — through the real `execute_workflow(pause_snapshot_
    input=...)` resume path: the completed prefix (s0, s1) is NOT re-dispatched on
    resume (their dispatcher is never invoked); only the failed-then-recovered tail
    (s2) re-dispatches → SUCCESS. A re-dispatch-fresh resume would re-fire s0/s1."""
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(fail_step_ids={"s2"}),
        ctx=pause_ctx,
    )
    assert paused.status is RunStatus.PAUSED
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    resume_dispatcher = _HandoffDispatcher()  # clean → s2 succeeds
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=resume_dispatcher,
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    # The recovered prefix was NEVER re-dispatched; only the tail stage ran.
    assert resume_dispatcher.dispatched == ["s2"]
    # The aggregate fuses the RECOVERED prefix outputs + the FRESH tail output.
    assert result.final_state is not None
    assert set(result.final_state["stages"]) == {"s0", "s1", "s2"}
    assert result.final_state["stages"]["s0"] == {"role": "s0", "echoed": {"stage": "s0"}}
    assert result.final_state["stages"]["s2"] == {"role": "s2", "echoed": {"stage": "s2"}}


def test_handoff_resume_chains_parent_action_id_off_last_completed_stage() -> None:
    """THE LOAD-BEARING CAUSALITY WITNESS — on resume, the re-dispatched stage's
    `parent_action_id` chains off the LAST COMPLETED stage's `action_id`, NOT
    re-anchored to the workflow origin. Established by comparing against a CLEAN full
    run's recorded chain (deterministic recompute): the resumed s2's parent_action_id
    equals the clean run's s2 parent_action_id (== s1's action_id) and is NOT the
    origin. A naive resume that re-anchored s2 to `workflow:{wf}:step:0` FAILS here."""
    # Clean full run → record each stage's parent_action_id (the reference chain).
    clean_dispatcher = _HandoffDispatcher()
    clean_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    clean = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=clean_dispatcher,
        ctx=clean_ctx,
    )
    assert clean.status is RunStatus.SUCCESS
    # s0 anchors at the origin; s1 chains off s0; s2 chains off s1 (the chain shape).
    assert clean_dispatcher.parent_action_ids["s0"] == _ORIGIN
    assert clean_dispatcher.parent_action_ids["s1"] != _ORIGIN
    assert clean_dispatcher.parent_action_ids["s2"] != _ORIGIN
    expected_s2_parent = clean_dispatcher.parent_action_ids["s2"]

    # Pause at s2, then resume.
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(fail_step_ids={"s2"}),
        ctx=pause_ctx,
    )
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    resume_dispatcher = _HandoffDispatcher()
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=resume_dispatcher,
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    # The resumed s2's parent_action_id is the deterministically-recomputed chain
    # anchor (s1's action_id) — IDENTICAL to the clean run, and NOT the origin.
    assert resume_dispatcher.parent_action_ids["s2"] == expected_s2_parent
    assert resume_dispatcher.parent_action_ids["s2"] != _ORIGIN


def test_handoff_resume_reseeds_inter_step_channel_for_first_tail_stage() -> None:
    """B-INTERSTEP-HANDOFF across resume: the inter-step channel is re-seeded from the
    recovered prefix so the FIRST re-dispatched stage reads its predecessor's recovered
    output as upstream context (the data-flow survives the pause boundary)."""

    class _Channel:
        def __init__(self) -> None:
            self._records: list[tuple[str, Any]] = []

        def record(self, step_id: str, output: Any) -> None:
            self._records.append((step_id, output))

        def most_recent_output(self) -> Any:
            return self._records[-1][1] if self._records else None

    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(fail_step_ids={"s2"}),
        ctx=pause_ctx,
    )
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    channel = _Channel()

    class _ReadingDispatcher:
        def __init__(self) -> None:
            self.upstream_at_dispatch: dict[str, Any] = {}

        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            step_id = str(step.step_id)
            self.upstream_at_dispatch[step_id] = channel.most_recent_output()
            return {"role": step_id, "echoed": dict(step.step_payload)}

    resume_dispatcher = _ReadingDispatcher()
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    resume_ctx.inter_step_output_channel = channel  # type: ignore[attr-defined]
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=resume_dispatcher,
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    # s2 (the first re-dispatched stage) saw s1's RECOVERED output re-seeded, not None.
    assert resume_dispatcher.upstream_at_dispatch["s2"] == {
        "role": "s1",
        "echoed": {"stage": "s1"},
    }


# ---------------------------------------------------------------------------
# Negative controls + integrity + backward-compat
# ---------------------------------------------------------------------------


def _captured_handoff_snapshot(handoff_resume: HandoffResumeState) -> PauseSnapshot:
    return asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id=_WF,
            run_id="run-1",
            step_index=2,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            handoff_resume=handoff_resume,
        )
    )


def test_handoff_snapshot_hash_covers_handoff_resume_tamper_rejected() -> None:
    """Integrity: a snapshot whose recovered stage output is TAMPERED (without
    re-hashing) is REJECTED at resume → FAILED + CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION
    (no silent-tamper gap on the recovered outputs the resumed aggregate trusts)."""
    good = _captured_handoff_snapshot(
        HandoffResumeState(
            completed_stages=(
                HandoffStageResumeState(stage_index=0, step_id="s0", output={"amount": 100}),
                HandoffStageResumeState(stage_index=1, step_id="s1", output={"role": "s1"}),
            ),
            stage_count=3,
        )
    )
    tampered = good.model_copy(
        update={
            "handoff_resume": good.handoff_resume.model_copy(  # type: ignore[union-attr]
                update={
                    "completed_stages": (
                        HandoffStageResumeState(
                            stage_index=0, step_id="s0", output={"amount": 999999}
                        ),
                        HandoffStageResumeState(stage_index=1, step_id="s1", output={"role": "s1"}),
                    )
                }
            )
        }
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(),
        ctx=ctx,
        pause_snapshot_input=tampered,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class == "CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION"


def test_handoff_resume_stage_count_mismatch_fails_closed() -> None:
    """Material-diff guard: a snapshot captured with stage_count=4 but resumed against
    a 3-stage body fails CLOSED (a changed body) rather than recovering stale outputs."""
    snapshot = _captured_handoff_snapshot(
        HandoffResumeState(
            completed_stages=(
                HandoffStageResumeState(stage_index=0, step_id="s0", output={"role": "s0"}),
                HandoffStageResumeState(stage_index=1, step_id="s1", output={"role": "s1"}),
            ),
            stage_count=4,  # body declared 3 stages → mismatch
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "decentralized-handoff-resume-body-mismatch" in result.fail_class
    assert "stage-count-mismatch" in result.fail_class


def test_handoff_resume_stage_identity_mismatch_fails_closed() -> None:
    """Material-diff guard: a recovered stage whose `step_id` no longer matches the
    re-supplied body (a same-count rename/reorder) fails CLOSED rather than replaying
    a stale output into a different stage's slot."""
    snapshot = _captured_handoff_snapshot(
        HandoffResumeState(
            completed_stages=(
                HandoffStageResumeState(stage_index=0, step_id="s0", output={"role": "s0"}),
                HandoffStageResumeState(stage_index=1, step_id="RENAMED", output={"role": "s1"}),
            ),
            stage_count=3,
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "stage-identity-mismatch" in result.fail_class


def test_handoff_resume_cursor_step_index_mismatch_fails_closed() -> None:
    """Cross-field coherence guard (out-of-family Codex [P2]): a snapshot that is
    internally hash-valid but INCOHERENT — `snapshot.step_index` disagrees with the
    `completed_stages` prefix length — fails CLOSED rather than resuming from the
    cursor-derived position (which would silently re-dispatch stages the advertised
    pause step says already completed, an at-most-once violation). The snapshot is
    captured via the real protocol (hash-valid) with `step_index=2` but only ONE
    completed stage."""
    snapshot = asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id=_WF,
            run_id="run-1",
            step_index=2,  # advertises a pause at stage 2 (prefix should be len 2)...
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            handoff_resume=HandoffResumeState(
                completed_stages=(  # ...but only ONE completed stage — incoherent
                    HandoffStageResumeState(stage_index=0, step_id="s0", output={"role": "s0"}),
                ),
                stage_count=3,
            ),
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "cursor-step-index-mismatch" in result.fail_class


def test_handoff_resume_under_fresh_run_id_still_chains_correctly() -> None:
    """The production-path witness (advisor: the fixed-run_id tests can't see this).
    `compose_branch_step_action_id` is purely STRUCTURAL (`parent_action_id` +
    `branch_index` + step position; NO `run_id`/`run_idempotency_key`), so a resume
    under a DIFFERENT run_id than the pause recomputes the SAME action_id chain the
    original run persisted — the resumed stage's `parent_action_id` still chains off the
    last completed stage's action_id (NOT a phantom under the fresh run_id, NOT the
    origin). A run_id-dependent chain would point stage-2's parent at an entry the
    ledger never wrote."""
    # Clean full run under run_id="run-1" → the reference chain anchor for s2.
    clean_dispatcher = _HandoffDispatcher()
    clean_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    clean = execute_workflow(
        _manifest(_PAUSE_TIER),
        [_stage("s0"), _stage("s1"), _stage("s2")],
        run_id="run-1",
        ctx=clean_ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(clean_dispatcher),
        pause_snapshot_input=None,
    )
    assert clean.status is RunStatus.SUCCESS
    expected_s2_parent = clean_dispatcher.parent_action_ids["s2"]
    assert expected_s2_parent != _ORIGIN

    # Pause at s2 under run_id="run-1".
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = execute_workflow(
        _manifest(_PAUSE_TIER),
        [_stage("s0"), _stage("s1"), _stage("s2")],
        run_id="run-1",
        ctx=pause_ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(_HandoffDispatcher(fail_step_ids={"s2"})),
        pause_snapshot_input=None,
    )
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    # Resume under a DIFFERENT run_id ("run-2-fresh") — the chain must still hold.
    resume_dispatcher = _HandoffDispatcher()
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = execute_workflow(
        _manifest(_PAUSE_TIER),
        [_stage("s0"), _stage("s1"), _stage("s2")],
        run_id="run-2-fresh",
        ctx=resume_ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(resume_dispatcher),
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    assert resume_dispatcher.dispatched == ["s2"]  # prefix recovered, not re-dispatched
    # The resumed s2's parent_action_id == the clean-run anchor — IDENTICAL despite the
    # fresh run_id (the chain is structural, run_id-independent — holds in production).
    assert resume_dispatcher.parent_action_ids["s2"] == expected_s2_parent
    assert resume_dispatcher.parent_action_ids["s2"] != _ORIGIN


def test_handoff_resume_non_contiguous_prefix_fails_closed() -> None:
    """Material-diff guard: a handoff resume cursor MUST be a contiguous prefix 0..k-1.
    A gap (a snapshot claiming stages 0 + 2 completed but not 1) is incoherent for a
    single-owner sequential chain → fail CLOSED."""
    snapshot = _captured_handoff_snapshot(
        HandoffResumeState(
            completed_stages=(
                HandoffStageResumeState(stage_index=0, step_id="s0", output={"role": "s0"}),
                HandoffStageResumeState(stage_index=2, step_id="s2", output={"role": "s2"}),
            ),
            stage_count=3,
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "non-contiguous-prefix" in result.fail_class


def test_handoff_resume_against_empty_body_fails_closed() -> None:
    """Out-of-family Codex [P2]: a resume snapshot (a non-empty cursor) against an
    EMPTY body must fail CLOSED, NOT hit the empty-body SUCCESS early-return (which
    would silently drop the recovered prefix). The empty-body SUCCESS is placed AFTER
    the material-diff guard so the stage-count mismatch (0 != captured count) fires."""
    snapshot = _captured_handoff_snapshot(
        HandoffResumeState(
            completed_stages=(
                HandoffStageResumeState(stage_index=0, step_id="s0", output={"role": "s0"}),
                HandoffStageResumeState(stage_index=1, step_id="s1", output={"role": "s1"}),
            ),
            stage_count=3,
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[],  # empty body + a non-empty cursor → incoherent
        dispatcher=_HandoffDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "decentralized-handoff-resume-body-mismatch" in result.fail_class


def test_handoff_resume_out_of_range_stage_index_fails_closed_not_indexerror() -> None:
    """Out-of-family Codex [P2]: a contiguous prefix LONGER than the body (with a
    matching stage_count + coherent step_index — an incoherent-but-hash-valid snapshot)
    must fail CLOSED via the bounds check, NOT raise IndexError on `steps[stage_index]`.
    Mirrors the PARALLELIZATION branch-index-out-of-range guard."""
    snapshot = _captured_handoff_snapshot(
        HandoffResumeState(
            completed_stages=(  # 4 contiguous stages 0..3, but the body has only 3
                HandoffStageResumeState(stage_index=0, step_id="s0", output={"role": "s0"}),
                HandoffStageResumeState(stage_index=1, step_id="s1", output={"role": "s1"}),
                HandoffStageResumeState(stage_index=2, step_id="s2", output={"role": "s2"}),
                HandoffStageResumeState(stage_index=3, step_id="s3", output={"role": "s3"}),
            ),
            stage_count=3,  # matches len(steps)=3 below → passes the stage-count check
        )
    ).model_copy(update={"step_index": 4})  # coherent with the 4-entry cursor
    # The snapshot was captured with step_index=2 then overridden to 4; recompute the
    # hash so the override is internally valid (the guard, not corruption, must catch it).
    valid_hash = _compute_snapshot_hash(
        workflow_id=snapshot.workflow_id,
        run_id=snapshot.run_id,
        step_index=snapshot.step_index,
        state_summary=snapshot.state_summary,
        handoff_resume=snapshot.handoff_resume,
    )
    snapshot = snapshot.model_copy(update={"snapshot_hash": valid_hash})
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1"), _stage("s2")],
        dispatcher=_HandoffDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "stage-index-out-of-range" in result.fail_class


def test_handoff_pause_setup_error_fails_loud_not_paused() -> None:
    """Out-of-family Codex [P2]: an UNBOUND `StepKind` is a SETUP/config error, NOT a
    stage dispatch failure. Under TEAM tier (cascade_policy=pause) with a bound protocol
    it must fail LOUD (FAILED + `...-step-kind-not-bound`), NOT be swallowed into a
    resumable PAUSED that would just loop on resume (mirrors the #678 PARALLELIZATION
    pre-flight). The dispatcher lookup is resolved outside the stage-failure cascade."""

    class _UnboundRegistry:
        def lookup(self, step_kind: StepKind) -> StepDispatcher:
            raise StepKindDispatcherNotBoundError(step_kind)

    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = execute_workflow(
        _manifest(_PAUSE_TIER),
        [_stage("s0"), _stage("s1")],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(StepDispatcherRegistry, _UnboundRegistry()),
        pause_snapshot_input=None,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "decentralized-handoff-step-kind-not-bound" in result.fail_class
    assert result.pause_snapshot is None  # NOT a false-resumable PAUSED


def test_handoff_stage_child_pause_fails_closed_not_handoff_paused() -> None:
    """Out-of-family Codex [P2]: a SUB_AGENT_DISPATCH handoff stage whose recursive
    child sub-workflow PAUSES raises `SubAgentChildPausedError` (#680). Under TEAM tier
    (cascade_policy=pause) + a bound protocol, the generic stage-failure handler would
    convert it into a handoff-level PAUSE — DROPPING the child's own cursor (the #680
    swallow-bug, one level over). It must fail CLOSED honestly
    (`...-child-pause-unsupported`), NEVER a false-resumable handoff-PAUSE that loses the
    child's suspended state. The completed prefix still salvages."""
    from harness_cp.workflow_driver_types import SubAgentChildPausedError

    child_snapshot = _captured_handoff_snapshot(
        HandoffResumeState(
            completed_stages=(
                HandoffStageResumeState(stage_index=0, step_id="c0", output={"role": "c0"}),
            ),
            stage_count=2,
        )
    )

    class _ChildPauseDispatcher:
        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            step_id = str(step.step_id)
            if step_id == "s1":
                raise SubAgentChildPausedError(
                    child_workflow_id="child-wf", child_snapshot=child_snapshot
                )
            return {"role": step_id, "echoed": dict(step.step_payload)}

    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1")],
        dispatcher=cast(StepDispatcher, _ChildPauseDispatcher()),
        ctx=ctx,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "decentralized-handoff-child-pause-unsupported" in result.fail_class
    assert result.pause_snapshot is None  # NOT a false handoff-PAUSE that drops the child
    assert result.partial_state is not None
    assert set(result.partial_state["stages"]) == {"s0"}  # prefix salvaged


def test_handoff_pause_no_protocol_bound_fails_honestly() -> None:
    """No `pause_resume_protocol` bound: a `pause` halt cannot capture a snapshot, so a
    PAUSED would advertise un-honorable resumability → honest FAILED (salvaging the
    prefix), NEVER a false PAUSED (the silent-degradation guard)."""

    class _CtxNoProtocol(_CtxP):
        def __init__(self, *, ledger: Any, emitter: _Emitter) -> None:
            super().__init__(ledger=ledger, emitter=emitter)
            self.pause_resume_protocol = None

    ctx = cast(DriverContext, _CtxNoProtocol(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=[_stage("s0"), _stage("s1")],
        dispatcher=_HandoffDispatcher(fail_step_ids={"s1"}),
        ctx=ctx,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "decentralized-handoff-pause-resume-protocol-not-bound" in result.fail_class
    assert result.partial_state is not None
    assert set(result.partial_state["stages"]) == {"s0"}  # prefix salvaged


def test_handoff_resume_byte_compat_when_absent() -> None:
    """Backward-compat: a snapshot with `handoff_resume=None` hashes byte-identically
    to the pre-B-HANDOFF-PAUSE baseline (the canonical-serialization drops the key when
    None) — every existing snapshot still validates."""
    summary, _anchor = _pause_context_reader()
    with_field = _compute_snapshot_hash(
        workflow_id=_WF,
        run_id="run-1",
        step_index=0,
        state_summary=summary,
        handoff_resume=None,
    )
    baseline = _compute_snapshot_hash(
        workflow_id=_WF,
        run_id="run-1",
        step_index=0,
        state_summary=summary,
    )
    assert with_field == baseline


def test_handoff_re_pause_unions_prefix_across_resumes() -> None:
    """Re-pause (pause → resume → pause): a resume whose tail ALSO fails re-PAUSES with
    a cursor unioning the recovered prefix + the newly-completed-on-resume stages (still
    a contiguous prefix). The third stage is never lost across the two pause boundaries."""
    # First pause: s0 completes, s1 fails (4-stage body).
    steps = [_stage("s0"), _stage("s1"), _stage("s2"), _stage("s3")]
    ctx1 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused1 = _run(steps=steps, dispatcher=_HandoffDispatcher(fail_step_ids={"s1"}), ctx=ctx1)
    assert paused1.status is RunStatus.PAUSED
    snap1 = paused1.pause_snapshot
    assert snap1 is not None
    assert snap1.handoff_resume is not None
    assert [cs.stage_index for cs in snap1.handoff_resume.completed_stages] == [0]

    # Resume: s1 now succeeds, s2 fails → RE-pause with the unioned prefix [s0, s1].
    ctx2 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused2 = _run(
        steps=steps,
        dispatcher=_HandoffDispatcher(fail_step_ids={"s2"}),
        ctx=ctx2,
        pause_snapshot_input=snap1,
    )
    assert paused2.status is RunStatus.PAUSED
    snap2 = paused2.pause_snapshot
    assert snap2 is not None
    assert snap2.handoff_resume is not None
    assert [cs.stage_index for cs in snap2.handoff_resume.completed_stages] == [0, 1]
    assert snap2.handoff_resume.stage_count == 4

    # Final resume: everything clean → SUCCESS with the full chain.
    ctx3 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    final = _run(
        steps=steps,
        dispatcher=_HandoffDispatcher(),
        ctx=ctx3,
        pause_snapshot_input=snap2,
    )
    assert final.status is RunStatus.SUCCESS
    assert final.final_state is not None
    assert set(final.final_state["stages"]) == {"s0", "s1", "s2", "s3"}
