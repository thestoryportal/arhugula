"""B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER (R-FS-1) — resumable `cascade_policy=pause` for the
`EVALUATOR_OPTIMIZER` generate→evaluate loop.

The single-owner sequential analogue of `B-HANDOFF-PAUSE` (#681), but the resume cursor is
an ITERATION CURSOR over the loop's completed generate/evaluate STEPS (keyed by the
monotonic `entry_index`), not a stage list: a step failure under `cascade_policy=pause`
(TEAM tier) with a bound `pause_resume_protocol` captures an
`EvaluatorOptimizerResumeState` (the contiguous completed-step prefix + their recovered
outputs) and returns PAUSED; `api.resume` (`execute_workflow(pause_snapshot_input=...)`)
recovers the completed prefix (NOT re-dispatched) and re-dispatches from the failed step,
HONORING THE ORIGINAL MAX-ITERATION CAP across the resume boundary.

THE LOAD-BEARING WITNESSES (a naive "resume → PAUSED → SUCCESS" passes while silently
breaking these):
1. the completed prefix's dispatcher is NEVER re-invoked across pause+resume (at-most-once);
2. the iteration cap is reconstructed across the resume boundary (recovered generates count
   toward the cap — a resume does NOT get a fresh full cap on top of the recovered work).

Materializes the §25.15.1 `pause → PAUSED` row EXTENDED to the sequential EO loop per
§25.18's named `EVALUATOR_OPTIMIZER` impl-order. Only `pause` is materialized; `proceed` /
`cascade-cancel` retain EO's existing terminal-FAILED disposition (the surgical additive
scope). No operator gate.
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
    EvaluatorOptimizerResumeState,
    EvaluatorOptimizerStepResumeState,
    PauseSnapshot,
    WorkflowPauseReason,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    _DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS,
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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-eo-pause")
_PAUSE_TIER = PersonaTier.TEAM_BINDING  # → cascade_policy = pause
_PROCEED_TIER = PersonaTier.SOLO_DEVELOPER  # → cascade_policy = proceed
_ANCHOR = "0" * 64  # constant MVP pause-context anchor (no material diff on resume)
_WF = "wf-eo-pause"
_GENERATE = "generate"
_EVALUATE = "evaluate"


def _manifest(persona_tier: PersonaTier = _PAUSE_TIER) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=_WF,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.EVALUATOR_OPTIMIZER,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _loop_steps() -> list[WorkflowStep]:
    return [
        WorkflowStep(
            step_id=StepID(_GENERATE),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": _GENERATE},
        ),
        WorkflowStep(
            step_id=StepID(_EVALUATE),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": _EVALUATE},
        ),
    ]


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
    """Driver context WITH a bound `pause_resume_protocol` so the EO `pause` branch can
    capture a snapshot + return PAUSED, and the entry-point resume detection can validate
    + admit `execute_workflow(pause_snapshot_input=...)`."""

    def __init__(self, *, ledger: Any, emitter: _Emitter, channel: Any = None) -> None:
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
        self.inter_step_output_channel = channel


class _CtxNoProtocol(_CtxP):
    """Like `_CtxP` but with NO protocol bound — exercises the detect-then-refuse path."""

    def __init__(self, *, ledger: Any, emitter: _Emitter) -> None:
        super().__init__(ledger=ledger, emitter=emitter)
        self.pause_resume_protocol = None


class _Registry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.DECLARATIVE_STEP:
            return self._dispatcher
        raise StepKindDispatcherNotBoundError(step_kind)


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _Registry(dispatcher))


class _EoDispatcher:
    """An EVALUATOR_OPTIMIZER dispatcher fake.

    - generate (`step_id == _GENERATE`) → `{"draft": <1-based generate-call>}`.
    - evaluate (`step_id == _EVALUATE`) → `{"accepted": <bool>}`; accepts on the
      `accept_on_evaluate`-th evaluation (1-based) and after; `None` never accepts.
    - `fail_on_call` (1-based TOTAL dispatch ordinal) RAISES (the cascade trigger).
    Records the per-step dispatch order so the at-most-once resume witness can assert a
    recovered prefix is never re-dispatched.
    """

    def __init__(
        self, *, accept_on_evaluate: int | None = None, fail_on_call: int | None = None
    ) -> None:
        self._accept_on = accept_on_evaluate
        self._fail_on = fail_on_call
        self.calls = 0
        self.generate_calls = 0
        self.evaluate_calls = 0
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        if self._fail_on is not None and self.calls == self._fail_on:
            raise RuntimeError(f"simulated dispatch failure at call {self.calls}")
        sid = str(step.step_id)
        self.dispatched.append(sid)
        if sid == _GENERATE:
            self.generate_calls += 1
            return {"draft": self.generate_calls}
        self.evaluate_calls += 1
        accepted = self._accept_on is not None and self.evaluate_calls >= self._accept_on
        return {"accepted": accepted, "feedback": f"rev-{self.evaluate_calls}"}


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
# Capture — a real EO pause returns PAUSED + an EO-aware snapshot
# ---------------------------------------------------------------------------


def test_eo_pause_with_protocol_returns_paused_with_eo_snapshot() -> None:
    """TEAM persona → pause, protocol bound: the evaluate of iteration 1 fails after
    gen0+eval0(non-accept)+gen1 complete → the run PAUSES (not the interim FAILED) with a
    hash-valid `PauseSnapshot` carrying an `EvaluatorOptimizerResumeState` (the contiguous
    completed-step prefix entries 0,1,2 + their recovered outputs)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    # calls: 1=gen0, 2=eval0(non-accept), 3=gen1, 4=eval1 → FAIL on call 4.
    result = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=None, fail_on_call=4),
        ctx=ctx,
    )
    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    eo = snap.evaluator_optimizer_resume
    assert eo is not None
    # ONLY the EO carrier is populated (mutually exclusive with the other 3).
    assert snap.fan_out_resume is None
    assert snap.peer_fan_out_resume is None
    assert snap.handoff_resume is None
    # The completed PREFIX (entries 0,1,2 = gen0,eval0,gen1) captured contiguously.
    assert [cs.entry_index for cs in eo.completed_steps] == [0, 1, 2]
    assert [cs.declared_step_index for cs in eo.completed_steps] == [0, 1, 0]
    assert [cs.step_id for cs in eo.completed_steps] == [_GENERATE, _EVALUATE, _GENERATE]
    assert eo.completed_steps[0].output == {"draft": 1}
    assert eo.completed_steps[2].output == {"draft": 2}
    # The snapshot is hash-valid (covers evaluator_optimizer_resume); step_index is the
    # failed step's DECLARED ordinal (the failed eval1 → 1), a valid `steps` position.
    assert snap.step_index == 1
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        evaluator_optimizer_resume=eo,
    )
    # The completed loop state salvages as partial_state (no silent loss).
    assert result.partial_state is not None
    assert result.partial_state["output"] == {"draft": 2}


def test_eo_pause_emits_resumption_not_workflow_start_on_resume() -> None:
    """The resume envelope emits RESUMPTION (the completed prefix already ran in the
    original envelope), not a second WORKFLOW_START."""
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(fail_on_call=4),
        ctx=pause_ctx,
    )
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    emitter = _Emitter()
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=emitter))
    _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=1),  # the resumed evaluate accepts
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    assert WorkflowEventClass.WORKFLOW_START not in emitter.emits


# ---------------------------------------------------------------------------
# Resume — the discriminating full-chain witnesses
# ---------------------------------------------------------------------------


def test_eo_resume_recovers_prefix_exactly_once_pending_evaluate() -> None:
    """THE AT-MOST-ONCE WITNESS (evaluate-failure pause) — through the real
    `execute_workflow(pause_snapshot_input=...)` resume path: the completed prefix (gen0,
    eval0, gen1) is NOT re-dispatched on resume (their dispatcher is never invoked); only
    the failed-then-recovered evaluate (entry 3) re-dispatches → SUCCESS, fusing the
    RECOVERED gen1 draft with the fresh accepting evaluation."""
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(steps=_loop_steps(), dispatcher=_EoDispatcher(fail_on_call=4), ctx=pause_ctx)
    assert paused.status is RunStatus.PAUSED
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    resume_dispatcher = _EoDispatcher(accept_on_evaluate=1)  # the resumed evaluate accepts
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=resume_dispatcher,
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    # ONLY the failed evaluate re-dispatched; gen0/eval0/gen1 recovered, never re-fired.
    assert resume_dispatcher.dispatched == [_EVALUATE]
    assert resume_dispatcher.generate_calls == 0
    # The SUCCESS final_state fuses the recovered gen1 draft with the fresh accept.
    assert result.final_state is not None
    assert result.final_state["accepted"] is True
    assert result.final_state["output"] == {"draft": 2}  # the RECOVERED gen1 output


def test_eo_resume_recovers_prefix_exactly_once_pending_generate() -> None:
    """THE AT-MOST-ONCE WITNESS (generate-failure pause) — the generate of iteration 1
    fails after gen0+eval0(non-accept); the prefix (gen0, eval0) is recovered, and the
    failed generate + its evaluate re-dispatch on resume → SUCCESS."""
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    # calls: 1=gen0, 2=eval0(non-accept), 3=gen1 → FAIL on call 3.
    paused = _run(steps=_loop_steps(), dispatcher=_EoDispatcher(fail_on_call=3), ctx=pause_ctx)
    assert paused.status is RunStatus.PAUSED
    snapshot = paused.pause_snapshot
    assert snapshot is not None
    eo = snapshot.evaluator_optimizer_resume
    assert eo is not None
    assert [cs.entry_index for cs in eo.completed_steps] == [0, 1]  # gen0, eval0
    assert snapshot.step_index == 0  # failed gen1 → declared ordinal 0

    resume_dispatcher = _EoDispatcher(accept_on_evaluate=1)  # resumed iter's evaluate accepts
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=resume_dispatcher,
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    # The failed generate + its evaluate re-dispatched; the prefix (gen0,eval0) recovered.
    assert resume_dispatcher.dispatched == [_GENERATE, _EVALUATE]


def test_eo_resume_honors_iteration_cap_across_boundary() -> None:
    """THE CAP-RECONSTRUCTION WITNESS — a generate-failure pause at iteration 1, then a
    resume whose evaluations NEVER accept, must hit the ORIGINAL max-iteration cap (not a
    fresh full cap on top of the recovered iteration). With MAX=3 and iteration 0 recovered,
    the resume runs exactly iterations 1 + 2 (2 more generates) → SUCCESS accepted=False at
    iterations=MAX. A broken cap (loop restarting from 0) would run MAX more iterations."""
    cap = _DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS
    assert cap == 3
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    # calls: 1=gen0, 2=eval0(non-accept), 3=gen1 → FAIL (pause with iter0 recovered).
    paused = _run(steps=_loop_steps(), dispatcher=_EoDispatcher(fail_on_call=3), ctx=pause_ctx)
    assert paused.status is RunStatus.PAUSED
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    resume_dispatcher = _EoDispatcher(accept_on_evaluate=None)  # never accepts → forces cap
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=resume_dispatcher,
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["accepted"] is False
    # The cap spans the resume boundary: total iterations == MAX (NOT MAX + recovered).
    assert result.final_state["iterations"] == cap
    # The resume dispatched exactly the remaining iterations (iter1 + iter2 = 2 generates),
    # NOT a fresh full cap of MAX generates.
    assert resume_dispatcher.generate_calls == cap - 1


def test_eo_resume_reseeds_inter_step_channel_for_first_dispatched_step() -> None:
    """B-INTERSTEP across resume: the inter-step channel is re-seeded from the recovered
    prefix so the FIRST re-dispatched step reads its predecessor's recovered output as
    upstream context (the data-flow survives the pause boundary). Evaluate-failure pause →
    the re-dispatched evaluate reads the recovered gen1 draft, not None."""

    class _Channel:
        def __init__(self) -> None:
            self._records: list[tuple[str, Any]] = []

        def record(self, step_id: str, output: Any) -> None:
            self._records.append((step_id, output))

        def most_recent_output(self) -> Any:
            return self._records[-1][1] if self._records else None

    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(steps=_loop_steps(), dispatcher=_EoDispatcher(fail_on_call=4), ctx=pause_ctx)
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    channel = _Channel()

    class _ReadingDispatcher:
        def __init__(self) -> None:
            self.upstream_at_dispatch: list[Any] = []

        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            self.upstream_at_dispatch.append(channel.most_recent_output())
            return {"accepted": True}

    resume_dispatcher = _ReadingDispatcher()
    resume_ctx = cast(
        DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter(), channel=channel)
    )
    result = _run(
        steps=_loop_steps(),
        dispatcher=cast(StepDispatcher, resume_dispatcher),
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    # The first re-dispatched step (the failed evaluate) saw the RECOVERED gen1 draft.
    assert resume_dispatcher.upstream_at_dispatch[0] == {"draft": 2}


def test_eo_re_pause_then_resume_unions_prefix() -> None:
    """RE-PAUSE (pause→resume→pause→resume): the first resume fails again at a later step,
    capturing a SECOND cursor that UNIONS the recovered prefix + the newly-completed steps;
    a second resume then completes. The cursor stays a contiguous prefix across resumes."""
    # Pause 1: fail on call 3 (gen1) → prefix = entries 0,1 (gen0, eval0).
    ctx1 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused1 = _run(steps=_loop_steps(), dispatcher=_EoDispatcher(fail_on_call=3), ctx=ctx1)
    snap1 = paused1.pause_snapshot
    assert snap1 is not None
    assert snap1.evaluator_optimizer_resume is not None
    assert len(snap1.evaluator_optimizer_resume.completed_steps) == 2

    # Resume 1: gen1 + eval1(non-accept) succeed, then gen2 FAILS (call 3 of this run).
    # → prefix unions entries 0,1 (recovered) + 2,3 (gen1, eval1 this run) = 4 contiguous.
    ctx2 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused2 = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=None, fail_on_call=3),
        ctx=ctx2,
        pause_snapshot_input=snap1,
    )
    assert paused2.status is RunStatus.PAUSED
    snap2 = paused2.pause_snapshot
    assert snap2 is not None
    eo2 = snap2.evaluator_optimizer_resume
    assert eo2 is not None
    assert [cs.entry_index for cs in eo2.completed_steps] == [0, 1, 2, 3]  # contiguous union
    assert snap2.step_index == 0  # failed gen2 → declared ordinal 0

    # Resume 2: gen2 + eval2(accept) → SUCCESS (iteration cap honored across two pauses).
    resume_dispatcher = _EoDispatcher(accept_on_evaluate=1)
    ctx3 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=resume_dispatcher,
        ctx=ctx3,
        pause_snapshot_input=snap2,
    )
    assert result.status is RunStatus.SUCCESS
    # Only iteration 2's gen+eval ran on the final resume; entries 0..3 recovered.
    assert resume_dispatcher.dispatched == [_GENERATE, _EVALUATE]
    assert result.final_state is not None
    assert result.final_state["iterations"] == 3  # the original MAX cap, across both pauses


# ---------------------------------------------------------------------------
# Negative controls + integrity + scope
# ---------------------------------------------------------------------------


def _captured_eo_snapshot(eo: EvaluatorOptimizerResumeState, *, step_index: int) -> PauseSnapshot:
    return asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id=_WF,
            run_id="run-1",
            step_index=step_index,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            evaluator_optimizer_resume=eo,
        )
    )


def test_eo_snapshot_hash_covers_eo_resume_tamper_rejected() -> None:
    """Integrity: a snapshot whose recovered step output is TAMPERED (without re-hashing)
    is REJECTED at resume → FAILED + CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION (no silent-tamper
    gap on the recovered outputs the resumed loop trusts)."""
    good = _captured_eo_snapshot(
        EvaluatorOptimizerResumeState(
            completed_steps=(
                EvaluatorOptimizerStepResumeState(
                    entry_index=0, declared_step_index=0, step_id=_GENERATE, output={"draft": 1}
                ),
                EvaluatorOptimizerStepResumeState(
                    entry_index=1,
                    declared_step_index=1,
                    step_id=_EVALUATE,
                    output={"accepted": False},
                ),
            ),
        ),
        step_index=0,  # 2-step prefix → the failed step's declared ordinal is 0
    )
    tampered = good.model_copy(
        update={
            "evaluator_optimizer_resume": EvaluatorOptimizerResumeState(
                completed_steps=(
                    EvaluatorOptimizerStepResumeState(
                        entry_index=0,
                        declared_step_index=0,
                        step_id=_GENERATE,
                        output={"draft": 999999},  # tampered
                    ),
                    EvaluatorOptimizerStepResumeState(
                        entry_index=1,
                        declared_step_index=1,
                        step_id=_EVALUATE,
                        output={"accepted": False},
                    ),
                ),
            )
        }
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=1),
        ctx=ctx,
        pause_snapshot_input=tampered,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class == "CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION"


def test_eo_resume_step_count_mismatch_fails_closed() -> None:
    """Material-diff guard: a cursor coherent in itself but resumed against a body that is
    not the 2 declared EO steps fails CLOSED rather than recovering stale outputs."""
    snapshot = _captured_eo_snapshot(
        EvaluatorOptimizerResumeState(
            completed_steps=(
                EvaluatorOptimizerStepResumeState(
                    entry_index=0, declared_step_index=0, step_id=_GENERATE, output={"draft": 1}
                ),
                EvaluatorOptimizerStepResumeState(
                    entry_index=1,
                    declared_step_index=1,
                    step_id=_EVALUATE,
                    output={"accepted": False},
                ),
            ),
        ),
        step_index=0,  # 2-step prefix → the failed step's declared ordinal is 0
    )
    one_step = [_loop_steps()[0]]  # a 1-step body (not the 2-step generate→evaluate)
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=one_step,
        dispatcher=_EoDispatcher(accept_on_evaluate=1),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "evaluator-optimizer-resume-body-mismatch" in result.fail_class
    assert "step-count-mismatch" in result.fail_class


def test_eo_resume_step_identity_mismatch_fails_closed() -> None:
    """Material-diff guard: a recovered step whose `step_id` no longer matches the
    re-supplied body slot (a rename) fails CLOSED rather than replaying a stale output into
    a different step."""
    snapshot = _captured_eo_snapshot(
        EvaluatorOptimizerResumeState(
            completed_steps=(
                EvaluatorOptimizerStepResumeState(
                    entry_index=0,
                    declared_step_index=0,
                    step_id="renamed-generate",
                    output={"draft": 1},
                ),
            ),
        ),
        step_index=1,
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),  # body's generate is "generate", not "renamed-generate"
        dispatcher=_EoDispatcher(accept_on_evaluate=1),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "step-identity-mismatch" in result.fail_class


def test_eo_resume_accepted_step_in_prefix_fails_closed() -> None:
    """Material-diff guard: a tampered cursor smuggling an ACCEPTING evaluation into the
    recovered prefix is incoherent (an accept terminates the loop SUCCESS, never pauses) →
    fail closed."""
    snapshot = _captured_eo_snapshot(
        EvaluatorOptimizerResumeState(
            completed_steps=(
                EvaluatorOptimizerStepResumeState(
                    entry_index=0, declared_step_index=0, step_id=_GENERATE, output={"draft": 1}
                ),
                EvaluatorOptimizerStepResumeState(
                    entry_index=1,
                    declared_step_index=1,
                    step_id=_EVALUATE,
                    output={"accepted": True},  # an accept in the prefix — incoherent
                ),
            ),
        ),
        step_index=0,  # 2-step prefix → the failed step's declared ordinal is 0
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=1),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "accepted-step-in-prefix" in result.fail_class


def test_eo_resume_cursor_step_index_incoherent_fails_closed() -> None:
    """Material-diff guard: a snapshot whose `step_index` disagrees with the cursor prefix
    length (an internally-inconsistent / tampered snapshot) fails closed rather than
    resuming from a position the pause step contradicts."""
    snapshot = _captured_eo_snapshot(
        EvaluatorOptimizerResumeState(
            completed_steps=(
                EvaluatorOptimizerStepResumeState(
                    entry_index=0, declared_step_index=0, step_id=_GENERATE, output={"draft": 1}
                ),
            ),
        ),
        step_index=5,  # prefix length is 1, not 5
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=1),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "cursor-step-index-mismatch" in result.fail_class


def test_eo_resume_odd_over_cap_cursor_fails_closed() -> None:
    """Out-of-family Codex [P2]: an ODD (pending-evaluate) cursor whose recovered generate
    count EXCEEDS the max-iteration cap (7 records / 4 generates at MAX=3) is semantically
    impossible (no real run produces it) → fail closed, rather than replaying iterations
    beyond the cap. The original even-only no-tail check missed this parity."""
    cap = _DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS
    assert cap == 3
    # 7 records (entries 0..6): generates at 0,2,4,6 (4 > cap), evaluates at 1,3,5 (non-accept).
    records = tuple(
        EvaluatorOptimizerStepResumeState(
            entry_index=i,
            declared_step_index=i % 2,
            step_id=_GENERATE if i % 2 == 0 else _EVALUATE,
            output=({"draft": i} if i % 2 == 0 else {"accepted": False}),
        )
        for i in range(7)
    )
    snapshot = _captured_eo_snapshot(
        EvaluatorOptimizerResumeState(completed_steps=records),
        step_index=7 % 2,  # odd cursor → the failed step's declared ordinal is 1 (coherent)
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=1),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "no-resumable-iteration" in result.fail_class


def test_eo_pause_no_protocol_bound_fails_honestly() -> None:
    """Detect-then-refuse: a TEAM-tier (pause) step failure with NO `pause_resume_protocol`
    bound CANNOT capture a resumable snapshot → FAILED with a precise fail_class (never a
    false-resumable PAUSED). The completed prior entries still persist (no silent loss)."""
    ledger = _RecordingLedger()
    ctx = cast(DriverContext, _CtxNoProtocol(ledger=ledger, emitter=_Emitter()))
    result = _run(steps=_loop_steps(), dispatcher=_EoDispatcher(fail_on_call=3), ctx=ctx)
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "evaluator-optimizer-pause-resume-protocol-not-bound" in result.fail_class
    # Iteration 0 (gen0, eval0) persisted; the failing gen1 persists nothing.
    assert len(ledger.appends) == 2


def test_eo_pause_setup_error_unbound_dispatcher_fails_not_paused() -> None:
    """Out-of-family Codex [P2]: a SETUP error (an unbound `StepKind`) under TEAM/`pause`
    with a protocol bound is FAILED, NOT a resumable PAUSED — a missing dispatcher would
    loop forever on resume, so setup/bookkeeping failures must fail directly. Only a genuine
    step-DISPATCH failure takes the pause path."""
    bad_steps = [
        WorkflowStep(
            step_id=StepID(_GENERATE),
            step_kind=StepKind.INFERENCE_STEP,  # the _Registry binds only DECLARATIVE_STEP
            step_payload={"role": _GENERATE},
        ),
        WorkflowStep(
            step_id=StepID(_EVALUATE),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": _EVALUATE},
        ),
    ]
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=bad_steps, dispatcher=_EoDispatcher(accept_on_evaluate=1), ctx=ctx)
    assert result.status is RunStatus.FAILED  # NOT PAUSED (the [P2] guarantee)
    assert result.pause_snapshot is None
    assert result.fail_class is not None
    assert "evaluator-optimizer-setup-or-bookkeeping-failure" in result.fail_class


def test_eo_child_pause_fails_closed_not_eo_paused() -> None:
    """Out-of-family Codex [P2]: an EO SUB_AGENT_DISPATCH step whose recursive child
    sub-workflow PAUSES (raising the typed `SubAgentChildPausedError`) FAILS CLOSED — EO
    does NOT materialize the cross-recursion child-pause disposition, so converting it to an
    EO-level PAUSE would DROP the child's cursor (the #680 swallow-bug one level over).
    Mirrors `_execute_decentralized_handoff`."""
    from harness_cp.workflow_driver_types import SubAgentChildPausedError

    child_snapshot = _captured_eo_snapshot(
        EvaluatorOptimizerResumeState(
            completed_steps=(
                EvaluatorOptimizerStepResumeState(
                    entry_index=0, declared_step_index=0, step_id="child-gen", output={}
                ),
            )
        ),
        step_index=1,
    )

    class _ChildPausingDispatcher:
        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            if str(step.step_id) == _GENERATE:
                raise SubAgentChildPausedError(
                    child_workflow_id="child-wf", child_snapshot=child_snapshot
                )
            return {"accepted": False}

    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_loop_steps(),
        dispatcher=cast(StepDispatcher, _ChildPausingDispatcher()),
        ctx=ctx,
    )
    assert result.status is RunStatus.FAILED  # NOT PAUSED (the child cursor would be lost)
    assert result.pause_snapshot is None
    assert result.fail_class is not None
    assert "evaluator-optimizer-child-pause-unsupported" in result.fail_class


def test_eo_resume_audit_sequence_unique_across_same_parity_re_pauses() -> None:
    """Out-of-family Codex [P2]: two EO re-pauses on the SAME parity (generate failures at
    entries 2 and 4, both step_index=0) must produce DISTINCT RESUME_ATTEMPTED audit
    `event_sequence_id`s — the iteration cursor's completed-step count is the unique per-pause
    discriminator (step_index alone would collide → the second resume audit entry dropped as
    an idempotent no-op)."""

    class _RecordingCpIsWiring:
        def __init__(self) -> None:
            self.resume_event_sequence_ids: list[int] = []

        async def emit_pause_resume_state_ledger_entry(
            self,
            *,
            workflow_id: str,
            step_id: str,
            protocol_event_kind: Any,
            event_sequence_id: int,
            protocol_state_snapshot: Any,
            actor: Any,
        ) -> None:
            self.resume_event_sequence_ids.append(event_sequence_id)

    wiring = _RecordingCpIsWiring()

    def _ctx_with_wiring() -> DriverContext:
        c = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
        c.cp_is_wiring = wiring  # type: ignore[attr-defined]
        return cast(DriverContext, c)

    # Initial run → pause1 (gen1 fails at entry 2; cursor len 2). No resume emission yet.
    paused1 = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=None, fail_on_call=3),
        ctx=_ctx_with_wiring(),
    )
    snap1 = paused1.pause_snapshot
    assert snap1 is not None and snap1.step_index == 0

    # Resume1 (emits RESUME_ATTEMPTED, seq from cursor len 2) → gen1+eval1 ok, gen2 fails
    # at entry 4 → pause2 (cursor len 4, still step_index 0).
    paused2 = _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=None, fail_on_call=3),
        ctx=_ctx_with_wiring(),
        pause_snapshot_input=snap1,
    )
    snap2 = paused2.pause_snapshot
    assert snap2 is not None and snap2.step_index == 0  # same parity as snap1

    # Resume2 (emits RESUME_ATTEMPTED, seq from cursor len 4).
    _run(
        steps=_loop_steps(),
        dispatcher=_EoDispatcher(accept_on_evaluate=1),
        ctx=_ctx_with_wiring(),
        pause_snapshot_input=snap2,
    )
    # Two same-parity re-pauses emitted DISTINCT resume audit sequences (8 = 2<<2, 16 = 4<<2),
    # NOT a collision at (0<<2)=0.
    assert wiring.resume_event_sequence_ids == [(2 << 2) | 0, (4 << 2) | 0]


def test_eo_existing_snapshot_without_eo_resume_still_validates() -> None:
    """Backward-compat: a `PauseSnapshot` with no `evaluator_optimizer_resume` (every
    pre-arc snapshot) hashes byte-identically — the field is additive/default-None and is
    dropped from the canonical hash when absent."""
    hash_without = _compute_snapshot_hash(
        workflow_id=_WF,
        run_id="run-1",
        step_index=0,
        state_summary=_pause_context_reader()[0],
    )
    hash_with_none = _compute_snapshot_hash(
        workflow_id=_WF,
        run_id="run-1",
        step_index=0,
        state_summary=_pause_context_reader()[0],
        evaluator_optimizer_resume=None,
    )
    assert hash_without == hash_with_none
