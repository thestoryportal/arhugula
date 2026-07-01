"""B-FANOUT-PAUSE (R-FS-1) — resumable `cascade_policy=pause` fan-out.

Materializes the cleared CP spec §25.15.1 `pause → PAUSED` row ("composes with
C-CP-26 PauseResumeProtocol + C-RT-35 `api.resume`") for the `ORCHESTRATOR_WORKERS`
fan-out, flipping the interim `class_3_fanout_pause_resume_not_yet_materialized`
deviation (FAILED + `not-yet-materialized`) to a genuine resumable PAUSED.

The honest bar (the interim foreclosed a FALSE-`PAUSED`): a PAUSED is returned
ONLY when a `pause_resume_protocol` is bound so a `FanOutResumeState`-bearing
`PauseSnapshot` can actually be captured, and `api.resume` (via the real
`execute_workflow(pause_snapshot_input=...)` entry-point resume detection — the
exact path the runtime `api.resume` drives) genuinely re-enters the strategy:
terminal branches are SKIPPED (§25.15.2 obligation 7, outputs recovered), the
not-yet-dispatched ones re-dispatched.

The completed-branch OUTPUT recovery is the materialization of the R-CC-1 design
§1.1 re-open trigger (the ledger carries causality + `terminal_status`, NOT the
dispatch output) — carried in the snapshot, COVERED by `snapshot_hash`.

Authority: `Spec_Control_Plane_v1_32.md` §25.15.1 + §25.15.2 obl. 7;
`pause_resume_protocol_types.py` C-CP-26 (FanOutResumeState / PauseSnapshot);
`.harness/class_3_fanout_pause_resume_not_yet_materialized.md` (closed by this arc).
"""

from __future__ import annotations

import asyncio
import threading
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
    EffectFencePausedBranchResumeState,
    EffectFenceResolution,
    FanOutBranchResumeState,
    FanOutResumeState,
    OrchestratorEffectFencePausedResumeState,
    PausedChildBranchResumeState,
    PauseSnapshot,
    ResumeContext,
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
    SubAgentChildPausedError,
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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-fanout-pause")
_PAUSE_TIER = PersonaTier.TEAM_BINDING  # → cascade_policy = pause
_ANCHOR = "0" * 64  # constant MVP pause-context anchor (no material diff on resume)


def _manifest(
    workflow_id: str = "wf-fp",
    _topology: TopologyPattern = TopologyPattern.ORCHESTRATOR_WORKERS,
    persona_tier: PersonaTier = _PAUSE_TIER,
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=_topology,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _steps(n_workers: int) -> list[WorkflowStep]:
    return [
        WorkflowStep(
            step_id=StepID("orchestrator"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": "orchestrator"},
        ),
        *(
            WorkflowStep(
                step_id=StepID(f"worker-{i}"),
                step_kind=StepKind.DECLARATIVE_STEP,
                step_payload={"index": i},
            )
            for i in range(n_workers)
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
    """MVP constant-sentinel reader (mirrors the runtime factory): empty
    StateSummary + a constant anchor → resume detects no material diff → admits."""
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
    """Driver context WITH a bound `pause_resume_protocol` (the pause/resume
    opt-in) so the fan-out `pause` branch can capture a snapshot + return PAUSED,
    and `execute_workflow(pause_snapshot_input=...)` entry-point resume detection
    can validate + admit a resume."""

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


class _Registry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is not StepKind.DECLARATIVE_STEP:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _Registry(dispatcher))


class _CountingDispatcher:
    """Echoes `{step_id, payload}`; records every dispatched step_id (so a resume
    can assert which branches were re-dispatched vs terminal-skipped). A step_id
    in `fail_step_ids` raises (the cascade trigger)."""

    def __init__(self, *, fail_step_ids: set[str] | None = None) -> None:
        self._fail = fail_step_ids or set()
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id in self._fail:
            raise RuntimeError(f"simulated worker failure at {step_id}")
        return {"role": step_id, "echoed": dict(step.step_payload)}


class _GatedFailDispatcher:
    """Forces a DETERMINISTIC all-terminal pause: worker-0 completes cleanly and
    sets a gate; worker-1 waits on that gate THEN fails. So both branches reach a
    terminal disposition (worker-0 `completed`+output / worker-1 ran-and-errored
    `completed`/no-output) BEFORE the barrier resolves — no not-yet-dispatched
    (cancelled) branch, no timing race. The orchestrator returns immediately."""

    def __init__(self) -> None:
        self._gate = threading.Event()
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id == "orchestrator":
            return {"role": "orchestrator"}
        if step_id == "worker-0":
            self._gate.set()
            return {"role": "worker-0", "echoed": dict(step.step_payload)}
        # worker-1: wait until worker-0 has completed, then fail (the trigger).
        assert self._gate.wait(timeout=10.0), "worker-0 never completed"
        raise RuntimeError("simulated worker-1 failure (after worker-0 completed)")


class _SynthDispatcher:
    """B-FANOUT-PAUSE-SYNTHESIS — handles the orchestrator + worker branches
    (`DECLARATIVE_STEP`) AND the terminal `POST_JOIN_SYNTHESIS` step (returning a
    DISTINCT `{synthesized, from}` marker so a test can prove the run's aggregate is the
    SYNTHESIZED output, NOT the deterministic orchestrator+workers fold). Records every
    dispatched step_id."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step.step_kind is StepKind.POST_JOIN_SYNTHESIS:
            siblings = tuple(
                sid for sid, _ in (getattr(step_context, "sibling_outputs", None) or ())
            )
            return {"synthesized": True, "from": siblings}
        return {"role": step_id, "echoed": dict(step.step_payload)}


class _SynthRegistry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind not in (StepKind.DECLARATIVE_STEP, StepKind.POST_JOIN_SYNTHESIS):
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _synthesis_step(step_id: str = "synthesis") -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(step_id),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"messages": [], "params": {"max_tokens": 64}},
    )


def _run(
    *,
    steps: list[WorkflowStep],
    dispatcher: StepDispatcher,
    ctx: DriverContext,
    pause_snapshot_input: PauseSnapshot | None = None,
    workflow_id: str = "wf-fp",
    topology: TopologyPattern = TopologyPattern.ORCHESTRATOR_WORKERS,
    persona_tier: PersonaTier = _PAUSE_TIER,
) -> Any:
    return execute_workflow(
        _manifest(workflow_id, topology, persona_tier),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
        pause_snapshot_input=pause_snapshot_input,
    )


# ---------------------------------------------------------------------------
# Capture — a real fan-out pause returns PAUSED + a fan-out-aware snapshot
# ---------------------------------------------------------------------------


def test_pause_with_protocol_returns_paused_with_fan_out_snapshot() -> None:
    """TEAM persona → pause, protocol bound: worker-1 fails (after worker-0
    completes) → the run PAUSES (not the interim FAILED) with a hash-valid
    `PauseSnapshot` carrying a `FanOutResumeState` (orchestrator output recovered;
    the terminal branches + worker-0's recovered output)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=_GatedFailDispatcher(), ctx=ctx)

    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    assert snap.fan_out_resume is not None
    fr = snap.fan_out_resume
    assert fr.worker_count == 2
    assert fr.orchestrator_output == {"role": "orchestrator"}
    # worker-0 completed cleanly → terminal + its output recovered into the snapshot.
    by_index = {b.branch_index: b for b in fr.branches}
    assert by_index[0].terminal_status == "completed"
    assert by_index[0].step_id == "worker-0"  # identity captured for resume validation
    assert by_index[0].output == {"role": "worker-0", "echoed": {"index": 0}}
    # worker-1 ran-and-errored → terminal `completed` (dispatch-boundary), no output.
    assert by_index[1].terminal_status == "completed"
    assert by_index[1].step_id == "worker-1"
    assert by_index[1].output is None
    # The snapshot is hash-valid (covers fan_out_resume).
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        fan_out_resume=fr,
    )


def test_pause_emits_resumption_not_workflow_start_on_resume() -> None:
    """The resume envelope emits RESUMPTION (the orchestrator + terminal workers
    already ran in the original envelope), not a second WORKFLOW_START."""
    emitter = _Emitter()
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=emitter))
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator"},
            orchestrator_step_id="orchestrator",
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="worker-0",
                    terminal_status="completed",
                    output={"role": "worker-0"},
                ),
            ),
            worker_count=2,
        )
    )
    _run(
        steps=_steps(2),
        dispatcher=_CountingDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    assert WorkflowEventClass.WORKFLOW_START not in emitter.emits


# ---------------------------------------------------------------------------
# Resume — the real `execute_workflow(pause_snapshot_input=...)` witness
# ---------------------------------------------------------------------------


def _captured_snapshot(
    *, fan_out_resume: FanOutResumeState, workflow_id: str = "wf-fp"
) -> PauseSnapshot:
    """A hash-valid fan-out snapshot, captured through the real protocol (NOT a
    hand-mutated model) — the exact shape a prior `pause` halt would produce."""
    return asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id=workflow_id,
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            fan_out_resume=fan_out_resume,
        )
    )


def test_resume_skips_terminal_recovers_outputs_and_redispatches_rest() -> None:
    """THE WITNESS — through the real `execute_workflow(pause_snapshot_input=...)`
    entry-point resume detection (the path `api.resume` drives):
      (1) the terminal branch (worker-0) is NOT re-dispatched (obligation 7),
      (2) the not-yet-dispatched branch (worker-1) IS re-dispatched,
      (3) the orchestrator is NOT re-dispatched (recovered),
      (4) the aggregate fuses the RECOVERED worker-0 output + the FRESH worker-1
          output + the recovered orchestrator output → SUCCESS."""
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator", "recovered": True},
            orchestrator_step_id="orchestrator",
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="worker-0",
                    terminal_status="completed",
                    output={"role": "worker-0", "recovered": True},
                ),
            ),  # worker-1 ABSENT → left re-dispatchable
            worker_count=2,
        )
    )
    dispatcher = _CountingDispatcher()
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=dispatcher, ctx=ctx, pause_snapshot_input=snapshot)

    assert result.status is RunStatus.SUCCESS
    # (1)+(3): the terminal worker-0 + the orchestrator were NOT re-dispatched.
    assert "worker-0" not in dispatcher.dispatched
    assert "orchestrator" not in dispatcher.dispatched
    # (2): only the re-dispatchable worker-1 ran on resume.
    assert dispatcher.dispatched == ["worker-1"]
    # (4): the aggregate fuses recovered (orchestrator + worker-0) + fresh (worker-1).
    assert result.final_state is not None
    assert result.final_state["orchestrator"] == {"role": "orchestrator", "recovered": True}
    assert result.final_state["worker_outputs"]["worker-0"] == {
        "role": "worker-0",
        "recovered": True,
    }
    assert result.final_state["worker_outputs"]["worker-1"] == {
        "role": "worker-1",
        "echoed": {"index": 1},
    }


def test_resume_all_terminal_with_a_failed_branch_is_partial_not_silent_success() -> None:
    """Real pause → real resume round-trip (the GatedFail all-terminal pause):
    both workers terminal at pause (worker-0 completed, worker-1 FAILED) → resume
    re-dispatches NOTHING and surfaces **PARTIAL** (degraded), NOT a bare silent
    SUCCESS dropping the failure — the silent-degradation class this arc forecloses
    (advisor [P1]; mirrors the `proceed`-cascade `any_failed → PARTIAL`). worker-0's
    output is recovered; the failed worker-1 contributes nothing + is not re-fired
    (obligation 7 + at-most-once)."""
    pause_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = _run(steps=_steps(2), dispatcher=_GatedFailDispatcher(), ctx=pause_ctx)
    assert paused.status is RunStatus.PAUSED
    snapshot = paused.pause_snapshot
    assert snapshot is not None

    resume_dispatcher = _CountingDispatcher()
    resume_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2),
        dispatcher=resume_dispatcher,
        ctx=resume_ctx,
        pause_snapshot_input=snapshot,
    )
    # A recovered branch FAILED → degraded → PARTIAL (not silent SUCCESS).
    assert result.status is RunStatus.PARTIAL
    # Both branches were terminal at pause → NOTHING re-dispatched on resume.
    assert resume_dispatcher.dispatched == []
    # The salvaged aggregate is on partial_state; worker-0 recovered, worker-1 gone.
    assert result.partial_state is not None
    assert "worker-0" in result.partial_state["worker_outputs"]
    assert "worker-1" not in result.partial_state["worker_outputs"]


# ---------------------------------------------------------------------------
# Negative controls + integrity + backward-compat
# ---------------------------------------------------------------------------


def test_snapshot_hash_covers_fan_out_resume_tamper_rejected() -> None:
    """Integrity: a snapshot whose recovered branch output is TAMPERED (without
    re-hashing) is REJECTED at resume → FAILED + CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION
    (no silent-tamper gap on the data the resumed aggregate trusts)."""
    good = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator"},
            orchestrator_step_id="orchestrator",
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="worker-0",
                    terminal_status="completed",
                    output={"amount": 100},
                ),
            ),
            worker_count=2,
        )
    )
    # Tamper the recovered output, keeping the STALE hash → corruption.
    tampered = good.model_copy(
        update={
            "fan_out_resume": good.fan_out_resume.model_copy(  # type: ignore[union-attr]
                update={
                    "branches": (
                        FanOutBranchResumeState(
                            branch_index=0,
                            step_id="worker-0",
                            terminal_status="completed",
                            output={"amount": 999999},
                        ),
                    )
                }
            )
        }
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2), dispatcher=_CountingDispatcher(), ctx=ctx, pause_snapshot_input=tampered
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class == "CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION"


def test_negative_control_empty_branches_loses_recovery() -> None:
    """Persistence is load-bearing: a snapshot whose `branches` is EMPTY (no
    recovered worker-0 output) re-dispatches BOTH workers and the aggregate does
    NOT contain a recovered worker-0 — proving the recovered output in the
    snapshot is what populates the aggregate, not an incidental re-run."""
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator", "recovered": True},
            orchestrator_step_id="orchestrator",
            branches=(),  # nothing recovered → both workers re-dispatchable
            worker_count=2,
        )
    )
    dispatcher = _CountingDispatcher()
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=dispatcher, ctx=ctx, pause_snapshot_input=snapshot)
    assert result.status is RunStatus.SUCCESS
    # BOTH workers re-dispatched (no terminal skip); worker-0's output is the FRESH
    # one (no "recovered" marker), proving the recovered-output path is the only
    # source of a recovered value (vs. this incidental re-run).
    assert set(dispatcher.dispatched) == {"worker-0", "worker-1"}
    assert result.final_state is not None
    assert result.final_state["worker_outputs"]["worker-0"] == {
        "role": "worker-0",
        "echoed": {"index": 0},
    }


def test_resume_worker_count_mismatch_fails_closed() -> None:
    """Material-diff guard: a snapshot captured with worker_count=3 but resumed
    against a 2-worker body fails CLOSED (the recovered ordinals no longer map to
    these steps — a changed body) rather than re-dispatching a mismatched set."""
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator"},
            orchestrator_step_id="orchestrator",
            branches=(),
            worker_count=3,
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2), dispatcher=_CountingDispatcher(), ctx=ctx, pause_snapshot_input=snapshot
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "resume-worker-count-mismatch" in result.fail_class


def test_linear_snapshot_hash_byte_identical_backward_compat() -> None:
    """Backward-compat: a snapshot with NO `fan_out_resume` (every linear /
    single-step pause) hashes byte-identically to the pre-B-FANOUT-PAUSE formula
    (the key is added to the canonical dict ONLY when fan_out_resume is present)
    → existing durable snapshots still validate."""
    summary = _pause_context_reader()[0]
    with_field = _compute_snapshot_hash(
        workflow_id="wf", run_id="r", step_index=0, state_summary=summary, fan_out_resume=None
    )
    legacy_canonical_hash = _compute_snapshot_hash(
        workflow_id="wf", run_id="r", step_index=0, state_summary=summary
    )
    assert with_field == legacy_canonical_hash


def test_fan_out_snapshot_survives_json_roundtrip() -> None:
    """Durable-store fidelity: the fan-out snapshot round-trips through
    model_dump(mode="json") → model_validate (the JournalWorkflowPauseStore path)
    with `fan_out_resume` intact AND the hash still valid."""
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator"},
            orchestrator_step_id="orchestrator",
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="worker-0",
                    terminal_status="completed",
                    output={"k": "v"},
                ),
                FanOutBranchResumeState(
                    branch_index=1, step_id="worker-1", terminal_status="timed_out", output=None
                ),
            ),
            worker_count=3,
        )
    )
    restored = PauseSnapshot.model_validate(snapshot.model_dump(mode="json"))
    assert restored == snapshot
    assert restored.fan_out_resume is not None
    assert restored.snapshot_hash == _compute_snapshot_hash(
        workflow_id=restored.workflow_id,
        run_id=restored.run_id,
        step_index=restored.step_index,
        state_summary=restored.state_summary,
        fan_out_resume=restored.fan_out_resume,
    )


# ---------------------------------------------------------------------------
# Decorrelated-review hardening — re-pause union, hierarchical gate, identity
# ---------------------------------------------------------------------------


def test_resume_redispatch_failing_worker_re_pauses_with_unioned_branches() -> None:
    """A re-dispatched worker failing AGAIN under `pause` re-PAUSES with a snapshot
    whose `branches` UNION the prior-recovered + this-round-terminal sets (advisor
    [secondary] — the re-pause claim was untested). worker-0 recovered; worker-1
    fails on re-dispatch → the new snapshot carries BOTH (worker-0's recovered
    output carried forward + worker-1 newly terminal)."""
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator", "recovered": True},
            orchestrator_step_id="orchestrator",
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="worker-0",
                    terminal_status="completed",
                    output={"role": "worker-0", "recovered": True},
                ),
            ),  # worker-1 + worker-2 absent → re-dispatchable
            worker_count=3,
        )
    )
    dispatcher = _CountingDispatcher(fail_step_ids={"worker-1"})
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(3), dispatcher=dispatcher, ctx=ctx, pause_snapshot_input=snapshot)

    # A re-dispatched worker failed under pause (protocol bound) → re-PAUSED.
    assert result.status is RunStatus.PAUSED
    new_snap = result.pause_snapshot
    assert new_snap is not None and new_snap.fan_out_resume is not None
    by_index = {b.branch_index: b for b in new_snap.fan_out_resume.branches}
    # UNION: the prior-recovered worker-0 (carried forward, output preserved) +
    # the newly-terminal worker-1 (failed this round).
    assert 0 in by_index and 1 in by_index
    assert by_index[0].output == {"role": "worker-0", "recovered": True}
    assert by_index[1].output is None  # ran-and-errored → no output
    # worker-0 was NOT re-dispatched (terminal-skipped); worker-1 WAS (and failed).
    assert "worker-0" not in dispatcher.dispatched
    assert "worker-1" in dispatcher.dispatched


def test_hierarchical_delegation_pause_materializes_resumable_paused() -> None:
    """B-HIERARCHICAL-PAUSE (R-FS-1) — HIERARCHICAL_DELEGATION REUSES
    `_execute_orchestrator_workers` and now threads `pause_resumable=True` + the
    resume snapshot. A TEAM/pause level-local worker failure materializes a GENUINE
    resumable PAUSED (the interim `...-not-yet-materialized` FAILED is RETIRED) — the
    same `FanOutResumeState` mechanism ORCHESTRATOR_WORKERS uses, now wired for the
    recursion-heavy topology. A resume round-trip completes (worker-0's terminal is
    skipped, the re-dispatchable worker re-runs)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2),
        dispatcher=_GatedFailDispatcher(),
        ctx=ctx,
        topology=TopologyPattern.HIERARCHICAL_DELEGATION,
    )
    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    assert snap.fan_out_resume is not None
    assert snap.fan_out_resume.worker_count == 2
    # No recursive child paused here (level-local worker pause) → empty.
    assert snap.fan_out_resume.paused_child_branches == ()
    # Hash-valid (covers fan_out_resume).
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        fan_out_resume=snap.fan_out_resume,
    )
    # Resume round-trip: HIERARCHICAL re-enters with the snapshot + a clean dispatcher.
    ctx2 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    resumed = _run(
        steps=_steps(2),
        dispatcher=_CountingDispatcher(),
        ctx=ctx2,
        topology=TopologyPattern.HIERARCHICAL_DELEGATION,
        pause_snapshot_input=snap,
    )
    assert resumed.status is not RunStatus.PAUSED
    assert resumed.status in (RunStatus.SUCCESS, RunStatus.PARTIAL)


def test_resume_body_identity_mismatch_fails_closed() -> None:
    """Codex [P1] — a valid (same worker_count) snapshot whose recovered branch
    `step_id` does NOT match the re-supplied body (a worker rename / reorder) fails
    CLOSED rather than silently attributing the recovered output to the wrong step.
    The hash is valid (captured for the renamed id), so this is caught by the
    in-strategy identity guard, not the snapshot_hash."""
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator"},
            orchestrator_step_id="orchestrator",
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="renamed-worker",  # the body has "worker-0" at index 0
                    terminal_status="completed",
                    output={"stale": True},
                ),
            ),
            worker_count=2,
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2), dispatcher=_CountingDispatcher(), ctx=ctx, pause_snapshot_input=snapshot
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "branch-identity-mismatch" in result.fail_class


def test_pause_captures_in_flight_sibling_completed_output() -> None:
    """Codex [P1] — a sibling IN-FLIGHT when the barrier cancels it (because
    another worker failed) runs to completion under the shield; its successful
    OUTPUT must be captured into the snapshot (else resume skips it as terminal +
    drops the output). worker-0 is mid-dispatch (a brief sleep) when worker-1
    fails → worker-0 completes under the shield → its output is recovered."""
    import time

    class _InFlightCompletesDispatcher:
        def __init__(self) -> None:
            self._started = threading.Event()

        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            sid = str(step.step_id)
            if sid == "orchestrator":
                return {"role": "orchestrator"}
            if sid == "worker-0":
                self._started.set()
                time.sleep(0.05)  # in-flight when worker-1 fails; completes under the shield
                return {"role": "worker-0", "in_flight_completed": True}
            assert self._started.wait(timeout=10.0), "worker-0 never started"
            raise RuntimeError("worker-1 fails while worker-0 is in-flight")

    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=_InFlightCompletesDispatcher(), ctx=ctx)

    assert result.status is RunStatus.PAUSED
    snap = result.pause_snapshot
    assert snap is not None and snap.fan_out_resume is not None
    by_index = {b.branch_index: b for b in snap.fan_out_resume.branches}
    # worker-0 was cancelled-but-completed → terminal `completed` WITH its output
    # captured (the fix); without it `output` would be None and resume would drop it.
    assert by_index[0].terminal_status == "completed"
    assert by_index[0].output == {"role": "worker-0", "in_flight_completed": True}


def test_resume_orchestrator_identity_mismatch_fails_closed() -> None:
    """Codex [P2] — the orchestrator's output is recovered + its dispatch skipped,
    so a snapshot whose `orchestrator_step_id` does NOT match the re-supplied
    `steps[0]` (a renamed/reordered orchestrator, same worker shape) fails CLOSED
    rather than applying stale orchestrator output to a different body."""
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator", "stale": True},
            orchestrator_step_id="renamed-orchestrator",  # body has "orchestrator"
            branches=(),
            worker_count=2,
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2), dispatcher=_CountingDispatcher(), ctx=ctx, pause_snapshot_input=snapshot
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "orchestrator-identity-mismatch" in result.fail_class


# ---------------------------------------------------------------------------
# B-HIERARCHICAL-PAUSE (R-FS-1) — recursive child PAUSE: capture + resume re-entry.
#
# The discriminating full-chain witness (`[[full-chain-witness-not-half-proofs]]`):
# a grandchild step that COMPLETED before the child paused is NOT re-executed when
# the parent resumes — the child re-enters at ITS cursor (counter == 1), NOT a fresh
# re-dispatch (which would make counter == 2). The child is a REAL recursive
# `execute_workflow` fan-out; only the SUB_AGENT_DISPATCH dispatcher is a faithful
# double of `RuntimeSubAgentDispatcher` (raise SubAgentChildPausedError on a child
# PAUSED + forward `step_context.child_resume_snapshot` as the child's
# `pause_snapshot_input`) — that runtime seam is unit-proven in
# `harness-runtime/tests/test_lifecycle_sub_agent_dispatch.py`. The INFERENCE-child
# real-provider e2e is blocked by a pre-existing runtime sync/async deadlock
# (`.harness/runtime_defect_sub_agent_inference_child_loop_bridge_deadlock.md`), so
# this declarative-only witness is the non-deadlocking proof of the recursion.
# ---------------------------------------------------------------------------

# Mutable holder (a list, not an ALL_CAPS module constant — pyright would flag a
# reassigned ALL_CAPS name as reportConstantRedefinition; Codex [P1]).
_grandchild0_dispatches = [0]


class _GrandchildDispatcher:
    """Child fan-out grandchild dispatcher: grandchild-0 completes (incrementing a
    module counter + setting a gate); grandchild-1 waits then FAILS → the child fan-out
    PAUSES with grandchild-0 terminal+recovered. Deterministic (the _GatedFailDispatcher
    shape)."""

    def __init__(self) -> None:
        self._gate = threading.Event()

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        if sid == "child-orch":
            return {"role": "child-orch"}
        if sid == "grandchild-0":
            _grandchild0_dispatches[0] += 1
            self._gate.set()
            return {"role": "grandchild-0", "done": True}
        assert self._gate.wait(timeout=10.0), "grandchild-0 never completed"
        raise RuntimeError("grandchild-1 fails (after grandchild-0 completed) → child pauses")


def _child_steps() -> list[WorkflowStep]:
    return [
        WorkflowStep(
            step_id=StepID("child-orch"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": "child-orch"},
        ),
        WorkflowStep(
            step_id=StepID("grandchild-0"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 0},
        ),
        WorkflowStep(
            step_id=StepID("grandchild-1"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 1},
        ),
    ]


class _FaithfulSubAgentDispatcher:
    """A faithful double of `RuntimeSubAgentDispatcher` for the B-HIERARCHICAL-PAUSE
    seam: dispatches a REAL child `execute_workflow`, reading
    `step_context.child_resume_snapshot` to thread the child's resume snapshot (so a
    resumed child re-enters at its cursor), and RAISING `SubAgentChildPausedError`
    (carrying the child's PauseSnapshot) when the child returns PAUSED — exactly what
    the runtime dispatcher does at `sub_agent_dispatch.py`."""

    def __init__(self, *, child_dispatcher: _GrandchildDispatcher) -> None:
        self._child_dispatcher = child_dispatcher
        self.child_calls = 0
        self.received_resume: list[Any] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        self.child_calls += 1
        child_resume = getattr(step_context, "child_resume_snapshot", None)
        self.received_resume.append(child_resume)
        child_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
        child_result = execute_workflow(
            _manifest("wf-child", TopologyPattern.ORCHESTRATOR_WORKERS),
            _child_steps(),
            run_id="child-run",
            ctx=child_ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_registry(self._child_dispatcher),
            pause_snapshot_input=child_resume,
        )
        if child_result.status is RunStatus.PAUSED:
            assert child_result.pause_snapshot is not None
            raise SubAgentChildPausedError(
                child_workflow_id="wf-child", child_snapshot=child_result.pause_snapshot
            )
        # SUCCESS / PARTIAL / DRAINED → success-equivalent (mirrors the dispatcher's
        # non-FAILED, non-PAUSED handling): return the child's state as the worker output.
        return dict(child_result.final_state or child_result.partial_state or {})


class _ParentRegistry:
    """Routes DECLARATIVE_STEP (the parent orchestrator) to a simple echo and
    SUB_AGENT_DISPATCH (the recursive worker) to the faithful sub-agent double."""

    def __init__(self, *, sub_agent: _FaithfulSubAgentDispatcher) -> None:
        self._sub_agent = sub_agent
        self._echo = _CountingDispatcher()

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.SUB_AGENT_DISPATCH:
            return cast(StepDispatcher, self._sub_agent)
        if step_kind is StepKind.DECLARATIVE_STEP:
            return cast(StepDispatcher, self._echo)
        raise StepKindDispatcherNotBoundError(step_kind)


def _parent_steps() -> list[WorkflowStep]:
    return [
        WorkflowStep(
            step_id=StepID("parent-orch"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": "parent-orch"},
        ),
        WorkflowStep(
            step_id=StepID("sub-worker"),
            step_kind=StepKind.SUB_AGENT_DISPATCH,
            step_payload={"child_workflow_id": "wf-child"},
        ),
    ]


def test_hierarchical_child_pause_resume_does_not_reexecute_grandchild() -> None:
    """THE discriminating witness — a grandchild completed INSIDE the child before the
    pause is NOT re-executed on resume (the child re-enters at its cursor, not fresh).

    First run: parent HIERARCHICAL → SUB_AGENT worker → REAL child fan-out; grandchild-0
    completes (counter → 1), grandchild-1 fails → child PAUSES → the worker raises
    SubAgentChildPausedError → the parent captures the child's snapshot into
    `paused_child_branches` + PAUSES. Resume: the parent re-dispatches the worker WITH
    the child snapshot → the child re-enters at its cursor → grandchild-0 is
    terminal-skipped (counter STAYS 1), proving non-re-execution. A broken re-entry
    (re-dispatch fresh) would make counter == 2."""
    _grandchild0_dispatches[0] = 0
    child_dispatcher = _GrandchildDispatcher()
    sub_agent = _FaithfulSubAgentDispatcher(child_dispatcher=child_dispatcher)
    parent_registry = cast(StepDispatcherRegistry, _ParentRegistry(sub_agent=sub_agent))

    # ---- First run: parent pauses on the recursive child PAUSE.
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = execute_workflow(
        _manifest("wf-parent", TopologyPattern.HIERARCHICAL_DELEGATION),
        _parent_steps(),
        run_id="parent-run",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=parent_registry,
    )
    assert paused.status is RunStatus.PAUSED, f"parent must pause; got {paused.status}"
    assert _grandchild0_dispatches[0] == 1, "grandchild-0 ran exactly once on the first pass"
    snap = paused.pause_snapshot
    assert snap is not None and snap.fan_out_resume is not None
    pcb = snap.fan_out_resume.paused_child_branches
    assert len(pcb) == 1, "the SUB_AGENT worker's child paused → exactly one paused-child branch"
    assert pcb[0].step_id == "sub-worker"
    # The captured child_snapshot is the child fan-out's own resumable snapshot.
    assert pcb[0].child_snapshot.fan_out_resume is not None
    # Hash covers the nested child cursor (a tampered child snapshot fails parent resume).
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        fan_out_resume=snap.fan_out_resume,
    )

    # ---- Resume: the child re-enters at its cursor — grandchild-0 is NOT re-executed.
    ctx2 = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    resumed = execute_workflow(
        _manifest("wf-parent", TopologyPattern.HIERARCHICAL_DELEGATION),
        _parent_steps(),
        run_id="parent-run",
        ctx=ctx2,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=parent_registry,
        pause_snapshot_input=snap,
    )
    assert resumed.status is not RunStatus.PAUSED
    # THE load-bearing assertion: the child re-entered at its cursor (terminal-skipped
    # grandchild-0), so it was NOT re-executed. Fresh re-dispatch would give 2.
    assert _grandchild0_dispatches[0] == 1, (
        f"grandchild-0 was RE-EXECUTED on resume (count={_grandchild0_dispatches[0]}) — the child "
        f"was re-dispatched FRESH instead of re-entering at its cursor (broken resume re-entry)"
    )
    # The worker WAS re-dispatched on resume (re-entry IS a dispatch, just at the cursor).
    assert sub_agent.child_calls == 2
    # Discriminating: the FIRST child dispatch got no resume snapshot (fresh); the SECOND
    # (resume) received the child's snapshot threaded via child_resume_snapshot — so the
    # non-re-execution is genuine re-entry, not a coincidental skip.
    assert sub_agent.received_resume[0] is None
    assert sub_agent.received_resume[1] is not None
    assert sub_agent.received_resume[1].fan_out_resume is not None


class _PausingChildDispatcher:
    """A child fan-out grandchild dispatcher whose grandchild-1 always fails → the child
    fan-out PAUSES (grandchild-0 completes terminal+output). Distinct gates per child so
    the parent's two SUB_AGENT workers pause in a controlled order."""

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        if sid == "child-orch" or sid == "grandchild-0":
            return {"role": sid}
        raise RuntimeError("grandchild-1 fails → child pauses")


class _OrderedPausingSubAgentDispatcher:
    """Two SUB_AGENT workers, each dispatching a REAL child fan-out that PAUSES. worker
    `sub-0` raises its SubAgentChildPausedError FIRST (sets a gate); `sub-1` waits the
    gate then raises — so worker-0's raise cancels worker-1 while worker-1's own child
    pause is draining in-flight, exercising the CANCELLATION path (Codex [P1]). Both
    paused children MUST survive into `paused_child_branches` (without the fix the
    cancelled worker-1 is recorded terminal `completed` + its snapshot DROPPED)."""

    def __init__(self) -> None:
        self._gate = threading.Event()

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        child_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
        child_result = execute_workflow(
            _manifest(f"wf-child-{sid}", TopologyPattern.ORCHESTRATOR_WORKERS),
            _child_steps(),
            run_id=f"child-run-{sid}",
            ctx=child_ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_registry(_PausingChildDispatcher()),
        )
        assert child_result.status is RunStatus.PAUSED and child_result.pause_snapshot is not None
        if sid == "sub-0":
            self._gate.set()  # let worker-1 proceed AFTER worker-0 has its pause ready
        else:
            # worker-1: wait until worker-0 raised (→ cancels this branch) so this
            # branch's pause drains in-flight under the shield → CancelledError path.
            assert self._gate.wait(timeout=10.0)
        raise SubAgentChildPausedError(
            child_workflow_id=f"wf-child-{sid}", child_snapshot=child_result.pause_snapshot
        )


class _TwoSubAgentRegistry:
    def __init__(self, *, sub_agent: _OrderedPausingSubAgentDispatcher) -> None:
        self._sub_agent = sub_agent
        self._echo = _CountingDispatcher()

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.SUB_AGENT_DISPATCH:
            return cast(StepDispatcher, self._sub_agent)
        if step_kind is StepKind.DECLARATIVE_STEP:
            return cast(StepDispatcher, self._echo)
        raise StepKindDispatcherNotBoundError(step_kind)


def test_cancellation_race_captures_paused_child_among_failing_siblings() -> None:
    """B-HIERARCHICAL-PAUSE (Codex [P1]) — when one paused-child worker raises and the
    TaskGroup cancels a SIBLING whose own child also paused in-flight, the cancelled
    sibling's child PAUSE lands in `inflight.exception()` (the shielded drain suppresses
    it + re-raises CancelledError). Both paused children MUST survive into
    `paused_child_branches` — the cancelled one is NOT recorded as a terminal `completed`
    branch (which would drop its snapshot on resume)."""
    sub_agent = _OrderedPausingSubAgentDispatcher()
    registry = cast(StepDispatcherRegistry, _TwoSubAgentRegistry(sub_agent=sub_agent))
    parent_steps = [
        WorkflowStep(
            step_id=StepID("parent-orch"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": "parent-orch"},
        ),
        WorkflowStep(
            step_id=StepID("sub-0"), step_kind=StepKind.SUB_AGENT_DISPATCH, step_payload={}
        ),
        WorkflowStep(
            step_id=StepID("sub-1"), step_kind=StepKind.SUB_AGENT_DISPATCH, step_payload={}
        ),
    ]
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = execute_workflow(
        _manifest("wf-parent-2", TopologyPattern.HIERARCHICAL_DELEGATION),
        parent_steps,
        run_id="parent-run-2",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=registry,
    )
    assert result.status is RunStatus.PAUSED
    snap = result.pause_snapshot
    assert snap is not None and snap.fan_out_resume is not None
    pcb_indices = {p.branch_index for p in snap.fan_out_resume.paused_child_branches}
    terminal_indices = {b.branch_index for b in snap.fan_out_resume.branches}
    # BOTH paused children captured — neither lost to a terminal `completed` branch.
    assert pcb_indices == {0, 1}, (
        f"a paused child was DROPPED in the cancellation race: paused={pcb_indices}, "
        f"terminal={terminal_indices}"
    )
    assert terminal_indices.isdisjoint(pcb_indices)


# ---------------------------------------------------------------------------
# B-FANOUT-PAUSE-SYNTHESIS — synthesis-bearing ORCHESTRATOR_WORKERS pause-resume.
# (HIERARCHICAL_DELEGATION reuses `_execute_orchestrator_workers` + `FanOutResumeState`
# + the SAME `execute_workflow(pause_snapshot_input=...)` entry the child re-enters on,
# so these FanOut witnesses cover the HIERARCHICAL top-level + child-level paths too.)
# ---------------------------------------------------------------------------


def test_resume_with_matching_synthesis_fresh_dispatches_succeeds() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS full-chain (ORCHESTRATOR_WORKERS) — a synthesis-bearing
    fan-out PAUSE is now RESUMABLE. The snapshot recovers the orchestrator output + carries
    the synthesis identity (`synthesis_step_id="synthesis"`); resume material-diffs it
    (match), re-dispatches the (absent) workers, then FRESH-dispatches the synthesis over
    the worker siblings post-barrier (it never ran on a pause → effect-free, first-and-only).
    Load-bearing: the aggregate is the SYNTHESIZED output (NOT the orchestrator+workers
    fold), the synthesis dispatched EXACTLY ONCE, and the orchestrator was NOT re-dispatched
    (recovered)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator", "recovered": True},
            orchestrator_step_id="orchestrator",
            branches=(),  # both workers re-dispatchable
            worker_count=2,
            synthesis_step_id="synthesis",
        )
    )
    dispatcher = _SynthDispatcher()
    result = execute_workflow(
        _manifest(),
        [*_steps(2), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(StepDispatcherRegistry, _SynthRegistry(dispatcher)),
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.SUCCESS
    # SYNTHESIZED output, NOT the fold — `from` carries the branch-index-ordered WORKER
    # siblings (0, 1); the orchestrator (steps[0]) is NOT a sibling.
    assert result.final_state == {"synthesized": True, "from": (0, 1)}
    assert dispatcher.dispatched.count("synthesis") == 1
    # Workers re-dispatched; the orchestrator was recovered (NOT re-dispatched).
    assert {s for s in dispatcher.dispatched if s.startswith("worker")} == {
        "worker-0",
        "worker-1",
    }
    assert "orchestrator" not in dispatcher.dispatched


def test_resume_synthesis_added_fails_closed_fanout() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS material-diff (ADDED, ORCHESTRATOR_WORKERS) — a snapshot
    captured WITHOUT a synthesis but resumed against a body that NOW carries one fails
    closed (the synthesis was added between pause and resume)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    snapshot = _captured_snapshot(
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator"},
            orchestrator_step_id="orchestrator",
            branches=(),
            worker_count=2,
        )
    )
    result = _run(
        steps=[*_steps(2), _synthesis_step()],
        dispatcher=_CountingDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert result.fail_class.startswith("post-join-synthesis-resume-material-diff:")


def test_synthesis_absent_fanout_snapshot_byte_compat_hash() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS byte-compat (FanOutResumeState) — a synthesis-ABSENT fan-out
    snapshot (`synthesis_step_id=None`, no `paused_child_branches`) hashes byte-identically
    to the pre-arc shape: `_compute_snapshot_hash` DROPS the `synthesis_step_id` key (next to
    the existing `paused_child_branches` drop) when None, so every old durable
    ORCHESTRATOR_WORKERS snapshot still validates."""
    import hashlib
    import json

    state_summary, _ = _pause_context_reader()
    fan = FanOutResumeState(
        orchestrator_output={"role": "orchestrator"},
        orchestrator_step_id="orchestrator",
        branches=(),
        worker_count=2,
    )  # synthesis_step_id + paused_child_branches both default-empty
    got = _compute_snapshot_hash(
        workflow_id="wf-fp",
        run_id="run-1",
        step_index=0,
        state_summary=state_summary,
        fan_out_resume=fan,
    )
    # The pre-arc canonical serialization — FanOut carrier with NEITHER `synthesis_step_id`
    # NOR `paused_child_branches` keys (both dropped when empty).
    canonical = {
        "workflow_id": "wf-fp",
        "run_id": "run-1",
        "step_index": 0,
        "state_summary": state_summary.model_dump(mode="json"),
        "fan_out_resume": {
            "orchestrator_output": {"role": "orchestrator"},
            "orchestrator_step_id": "orchestrator",
            "branches": [],
            "worker_count": 2,
        },
    }
    expected = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert got == expected


# ---------------------------------------------------------------------------
# B-FANOUT-PAUSE-SYNTHESIS × HIERARCHICAL — a REAL nested round-trip (advisor Item 1).
# A synthesis-bearing CHILD fan-out snapshot embedded in a parent's paused_child_branches:
# proves (1) the nested child synthesis identity survives a REAL protocol capture, (2) the
# parent hash recomputes consistently over the nested carrier (the recursive `synthesis_step_id`
# strip — Codex #1 was exactly a nested-HIERARCHICAL hash bug), and (3) on parent resume the
# child re-enters `execute_workflow(pause_snapshot_input=...)`, MY entry guard material-diffs the
# child synthesis (matches), and the child reaches SUCCESS + FRESH-dispatches its synthesis once.
# ---------------------------------------------------------------------------


class _ChildSynthDispatcher:
    """Child fan-out dispatcher with a terminal synthesis: `child-orch` + workers echo;
    `child-synthesis` returns a DISTINCT marker + counts its dispatches (so the
    fresh-dispatched-exactly-once claim is checkable)."""

    def __init__(self) -> None:
        self.synth_dispatches = 0

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        if step.step_kind is StepKind.POST_JOIN_SYNTHESIS:
            self.synth_dispatches += 1
            sibs = tuple(i for i, _ in (getattr(step_context, "sibling_outputs", None) or ()))
            return {"child_synthesized": True, "from": sibs}
        return {"role": str(step.step_id)}


class _ChildSynthRegistry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind not in (StepKind.DECLARATIVE_STEP, StepKind.POST_JOIN_SYNTHESIS):
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _child_steps_with_synthesis() -> list[WorkflowStep]:
    return [
        WorkflowStep(
            step_id=StepID("child-orch"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": "child-orch"},
        ),
        WorkflowStep(
            step_id=StepID("child-worker-0"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 0},
        ),
        WorkflowStep(
            step_id=StepID("child-worker-1"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 1},
        ),
        WorkflowStep(
            step_id=StepID("child-synthesis"),
            step_kind=StepKind.POST_JOIN_SYNTHESIS,
            step_payload={"messages": [], "params": {"max_tokens": 64}},
        ),
    ]


class _SynthChildSubAgentDispatcher:
    """Faithful sub-agent double that re-enters a SYNTHESIS-bearing child fan-out, threading
    `step_context.child_resume_snapshot` as the child's `pause_snapshot_input` (exactly the
    runtime `sub_agent_dispatch.py` seam) so the child re-enters at its cursor."""

    def __init__(self, *, child_dispatcher: _ChildSynthDispatcher) -> None:
        self._child_dispatcher = child_dispatcher
        self.received_resume: list[Any] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        child_resume = getattr(step_context, "child_resume_snapshot", None)
        self.received_resume.append(child_resume)
        child_ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
        child_result = execute_workflow(
            _manifest("wf-child", TopologyPattern.ORCHESTRATOR_WORKERS),
            _child_steps_with_synthesis(),
            run_id="child-run",
            ctx=child_ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=cast(
                StepDispatcherRegistry, _ChildSynthRegistry(self._child_dispatcher)
            ),
            pause_snapshot_input=child_resume,
        )
        if child_result.status is RunStatus.PAUSED:
            assert child_result.pause_snapshot is not None
            raise SubAgentChildPausedError(
                child_workflow_id="wf-child", child_snapshot=child_result.pause_snapshot
            )
        return dict(child_result.final_state or child_result.partial_state or {})


class _SynthParentRegistry:
    def __init__(self, *, sub_agent: _SynthChildSubAgentDispatcher) -> None:
        self._sub_agent = sub_agent
        self._echo = _CountingDispatcher()

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.SUB_AGENT_DISPATCH:
            return cast(StepDispatcher, self._sub_agent)
        if step_kind is StepKind.DECLARATIVE_STEP:
            return cast(StepDispatcher, self._echo)
        raise StepKindDispatcherNotBoundError(step_kind)


def test_hierarchical_child_synthesis_real_nested_round_trip() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS × HIERARCHICAL real nested round-trip (advisor Item 1)."""
    # A synthesis-bearing CHILD snapshot — captured through the REAL protocol (hash-valid).
    # `branches=()` → resume re-dispatches both child workers → SUCCESS → the synthesis fires
    # (a real failure-pause always leaves a terminal-errored branch → degraded → fold, so the
    # SUCCESS+synthesis path is the no-errored-branch shape, as for the top-level full-chain).
    child_snap = asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id="wf-child",
            run_id="child-run",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            fan_out_resume=FanOutResumeState(
                orchestrator_output={"role": "child-orch", "recovered": True},
                orchestrator_step_id="child-orch",
                branches=(),
                worker_count=2,
                synthesis_step_id="child-synthesis",
            ),
        )
    )
    # A parent snapshot embedding the child as a paused-child branch (the sub-worker, ordinal 0).
    parent_fan = FanOutResumeState(
        orchestrator_output={"role": "parent-orch", "recovered": True},
        orchestrator_step_id="parent-orch",
        branches=(),
        worker_count=1,
        paused_child_branches=(
            PausedChildBranchResumeState(
                branch_index=0, step_id="sub-worker", child_snapshot=child_snap
            ),
        ),
    )
    parent_snap = asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id="wf-parent",
            run_id="parent-run",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            fan_out_resume=parent_fan,
        )
    )
    # (1) REAL nested capture: the child's synthesis identity survives inside the parent snapshot.
    assert parent_snap.fan_out_resume is not None
    nested = parent_snap.fan_out_resume.paused_child_branches[0].child_snapshot
    assert nested.fan_out_resume is not None
    assert nested.fan_out_resume.synthesis_step_id == "child-synthesis"
    # (2) Parent hash byte-compat over the NESTED carrier: the recompute COVERS the nested
    # (non-None) synthesis identity AND strips the parent's own None — `snapshot_hash` (computed
    # at capture) recomputes identically. With a top-level-only drop this would have diverged
    # (Codex #1). The recursive strip leaves the nested `synthesis_step_id: null`-free.
    assert parent_snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=parent_snap.workflow_id,
        run_id=parent_snap.run_id,
        step_index=parent_snap.step_index,
        state_summary=parent_snap.state_summary,
        fan_out_resume=parent_snap.fan_out_resume,
    )
    # (3) REAL parent resume → the child re-enters `execute_workflow` at its cursor, MY entry
    # guard material-diffs the child synthesis (matches "child-synthesis"), the child reaches
    # SUCCESS and FRESH-dispatches its synthesis EXACTLY ONCE.
    child_dispatcher = _ChildSynthDispatcher()
    sub_agent = _SynthChildSubAgentDispatcher(child_dispatcher=child_dispatcher)
    parent_registry = cast(StepDispatcherRegistry, _SynthParentRegistry(sub_agent=sub_agent))
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    resumed = execute_workflow(
        _manifest("wf-parent", TopologyPattern.HIERARCHICAL_DELEGATION),
        _parent_steps(),
        run_id="parent-run",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=parent_registry,
        pause_snapshot_input=parent_snap,
    )
    assert resumed.status is RunStatus.SUCCESS, f"{resumed.status} / {resumed.fail_class}"
    # The child re-entered WITH its snapshot (genuine re-entry, not a fresh run).
    assert sub_agent.received_resume[-1] is not None
    assert sub_agent.received_resume[-1].fan_out_resume is not None
    assert sub_agent.received_resume[-1].fan_out_resume.synthesis_step_id == "child-synthesis"
    # The child synthesis FRESH-dispatched exactly once (effect-free, first-and-only).
    assert child_dispatcher.synth_dispatches == 1


# ---------------------------------------------------------------------------
# B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — the ORCHESTRATOR_WORKERS analogue: a WORKER
# whose OWN dispatch raises the runtime effect fence composes that ambiguous-pause
# THROUGH the worker barrier to a genuine PAUSE carrying `effect_fence_paused_branches`,
# and resume re-enters the worker with the operator's key-bound resolution. The
# real-fence witness is the REAL `_execute_orchestrator_workers` TaskGroup+shield; the
# error is name-matched (harness-cp cannot import harness-runtime).
# ---------------------------------------------------------------------------


class EffectFenceAmbiguousUncommittedError(Exception):
    """Test-local stand-in for the runtime `effect_fence.EffectFenceAmbiguousUncommittedError`
    (C-RT-31 §14.22) — the driver name-matches `type(exc).__name__`, so a same-named local class
    with the `idempotency_key` attribute is the faithful CP-side witness."""

    def __init__(self, *, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"ambiguous (key={idempotency_key!r})")


class _OrchestratorFenceAmbiguousDispatcher:
    """Deterministic: orchestrator returns immediately; worker-0 completes (sets a gate);
    worker-1 waits on the gate THEN raises the effect-fence ambiguous error — so worker-0 is
    terminal BEFORE worker-1's fence-pause halts the barrier. Records each dispatch's threaded
    `effect_fence_resolution` (the resume producer-half witness)."""

    def __init__(self, *, fence_key: str = "fence-key-worker-1") -> None:
        self._gate = threading.Event()
        self._fence_key = fence_key
        self.dispatched: list[str] = []
        self.seen_resolution: dict[str, Any] = {}

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        self.seen_resolution[step_id] = getattr(step_context, "effect_fence_resolution", None)
        if step_id == "orchestrator":
            return {"role": "orchestrator"}
        if step_id == "worker-0":
            self._gate.set()
            return {"role": "worker-0", "echoed": dict(step.step_payload)}
        assert self._gate.wait(timeout=10.0), "worker-0 never completed"
        raise EffectFenceAmbiguousUncommittedError(idempotency_key=self._fence_key)


class _OrchestratorResumeRecordingDispatcher:
    """Resume-side recording dispatcher: orchestrator + worker-0 recovered (skipped); records the
    threaded `effect_fence_resolution` per dispatch then SUCCEEDS (no gate, no raise)."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.seen_resolution: dict[str, Any] = {}

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        self.seen_resolution[step_id] = getattr(step_context, "effect_fence_resolution", None)
        return {"role": step_id, "echoed": dict(step.step_payload)}


class _HolderWithResolution:
    """Stand-in `ResumeContextHolder` — `peek()` returns a ResumeContext carrying the operator's
    effect-fence resolution (NON-consuming, the production peek contract)."""

    def __init__(self, resolution: EffectFenceResolution) -> None:
        self._rc = ResumeContext(effect_fence_resolution=resolution)
        self.peeked = 0

    def peek(self) -> ResumeContext:
        self.peeked += 1
        return self._rc


def test_orchestrator_worker_effect_fence_ambiguous_composes_through_barrier_to_pause() -> None:
    """REAL-FENCE WITNESS (PAUSE half, ORCHESTRATOR_WORKERS): a worker whose OWN dispatch raises
    the effect-fence ambiguous error composes through the REAL `_execute_orchestrator_workers`
    TaskGroup+shield to a genuine PAUSE carrying `effect_fence_paused_branches` (worker-1 + its
    held reserve key), DISJOINT from the terminal `branches` (worker-0 recovered) and the
    orchestrator (recovered)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=_OrchestratorFenceAmbiguousDispatcher(), ctx=ctx)

    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    # Labeled EFFECT_FENCE_AMBIGUOUS so the operator surface knows to supply a resolution.
    assert snap.pause_reason is WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS
    fr = snap.fan_out_resume
    assert fr is not None
    assert fr.orchestrator_output == {"role": "orchestrator"}
    # worker-1 (branch ordinal 1) is the disjoint effect-fence-paused disposition.
    assert {b.branch_index for b in fr.branches} == {0}  # only worker-0 terminal
    efp = fr.effect_fence_paused_branches
    assert len(efp) == 1
    assert efp[0] == EffectFencePausedBranchResumeState(
        branch_index=1,
        step_id="worker-1",
        step_kind="declarative-step",
        idempotency_key="fence-key-worker-1",
    )
    restored = PauseSnapshot.model_validate(snap.model_dump(mode="json"))
    assert restored == snap


def test_orchestrator_worker_effect_fence_resume_threads_key_bound_resolution() -> None:
    """REAL-FENCE WITNESS (resume half, ORCHESTRATOR_WORKERS): resuming re-enters ONLY the
    fence-paused worker (orchestrator + worker-0 recovered-skipped), threading the operator's
    `EffectFenceResolution` key-bound to THAT worker's held reserve."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.fan_out_resume is not None
    efp = snap.fan_out_resume.effect_fence_paused_branches
    assert len(efp) == 1
    key = efp[0].idempotency_key

    holder = _HolderWithResolution(EffectFenceResolution.RE_FIRE)
    rec = _OrchestratorResumeRecordingDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.SUCCESS
    # orchestrator + worker-0 recovered-skipped; only worker-1 re-dispatched WITH the directive.
    assert "orchestrator" not in rec.dispatched
    assert "worker-0" not in rec.dispatched
    assert "worker-1" in rec.dispatched
    threaded = rec.seen_resolution["worker-1"]
    assert threaded is not None
    assert threaded.resolution is EffectFenceResolution.RE_FIRE
    assert threaded.idempotency_key == key
    assert holder.peeked >= 1  # PEEKED (non-consuming), not consumed


class EffectFenceAbortedError(Exception):
    """Test-local stand-in for the runtime `effect_fence.EffectFenceAbortedError` (operator ABORT
    applied at the fence gate)."""


class _OrchestratorAbortOnResolutionDispatcher:
    """Resume-side: orchestrator + worker-0 recovered-skipped; the re-entered fence-paused worker
    RAISES `EffectFenceAbortedError` when its threaded directive is ABORT (the runtime applying the
    operator's choice) — witnessing ABORT → terminal FAILED, not a re-pause."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        directive = getattr(step_context, "effect_fence_resolution", None)
        if directive is not None and directive.resolution is EffectFenceResolution.ABORT:
            raise EffectFenceAbortedError(f"operator aborted {step_id}")
        return {"role": step_id, "echoed": dict(step.step_payload)}


def test_orchestrator_worker_effect_fence_resume_abort_is_terminal_failed() -> None:
    """Codex [P1] regression (ORCHESTRATOR_WORKERS): resuming an effect-fence-paused worker with
    ABORT yields a TERMINAL `RunStatus.FAILED`, NOT a re-pause — even on the cascade_policy=pause
    tier."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.fan_out_resume is not None

    holder = _HolderWithResolution(EffectFenceResolution.ABORT)
    rec = _OrchestratorAbortOnResolutionDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED
    assert "orchestrator-workers-effect-fence-aborted" in (result.fail_class or "")
    assert result.pause_snapshot is None  # terminal — NOT a re-pause
    assert "worker-1" in rec.dispatched  # the aborted worker DID re-dispatch


def test_orchestrator_worker_effect_fence_resume_changed_kind_fails_closed() -> None:
    """Codex [P1] R2 regression (ORCHESTRATOR_WORKERS): an effect-fence-paused worker re-supplied
    at the SAME step_id but a CHANGED step_kind on resume FAILS CLOSED (the resolution would not
    reach the fence). The live-pause analogue of the crash-resume changed-kind guard."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.fan_out_resume is not None

    # Resume with worker-1 CHANGED from declarative-step → inference-step (same step_id).
    changed = [
        WorkflowStep(
            step_id=StepID("orchestrator"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"role": "orchestrator"},
        ),
        WorkflowStep(
            step_id=StepID("worker-0"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 0},
        ),
        WorkflowStep(
            step_id=StepID("worker-1"), step_kind=StepKind.INFERENCE_STEP, step_payload={"index": 1}
        ),
    ]
    holder = _HolderWithResolution(EffectFenceResolution.SKIP_AS_FIRED)
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=changed,
        dispatcher=_OrchestratorResumeRecordingDispatcher(),
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED
    assert "effect-fence-paused-kind-changed" in (result.fail_class or "")


# ---------------------------------------------------------------------------
# B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING — the ORCHESTRATOR's OWN
# (steps[0]) dispatch raises the effect-fence ambiguous error → composed to a §26.2 pause
# (Part 2) carrying the new `orchestrator_effect_fence_resume` carrier; resume re-dispatches
# the orchestrator WITH the operator's key-bound EffectFenceResolution threaded onto its
# context (Part 3, the NEW resume→orchestrator application site). Real-fence witnesses through
# the REAL `_execute_orchestrator_workers` sequential orchestrator-dispatch path.
# ---------------------------------------------------------------------------


class _OrchestratorSelfFenceAmbiguousDispatcher:
    """The ORCHESTRATOR's OWN dispatch raises the effect-fence ambiguous error on its FIRST dispatch
    (no resolution threaded — the effect-bearing orchestrator maybe-ran). On RESUME a directive IS
    threaded, and this dispatcher APPLIES it (the runtime fence's role): ABORT → raise
    `EffectFenceAbortedError`; RE_FIRE (or any non-abort) → a fresh success. Records each dispatch's
    threaded `effect_fence_resolution` (the resume producer-half witness)."""

    def __init__(self, *, fence_key: str = "fence-key-orchestrator") -> None:
        self._fence_key = fence_key
        self.dispatched: list[str] = []
        self.seen_resolution: dict[str, Any] = {}

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        directive = getattr(step_context, "effect_fence_resolution", None)
        self.seen_resolution[step_id] = directive
        if step_id == "orchestrator":
            if directive is None:
                raise EffectFenceAmbiguousUncommittedError(idempotency_key=self._fence_key)
            if directive.resolution is EffectFenceResolution.ABORT:
                raise EffectFenceAbortedError(f"operator aborted {step_id}")
            return {"role": "orchestrator", "refired": True}
        return {"role": step_id, "echoed": dict(step.step_payload)}


def test_orchestrator_self_effect_fence_ambiguous_composes_to_pause() -> None:
    """REAL-FENCE WITNESS (PAUSE half): the ORCHESTRATOR's OWN dispatch raises the effect-fence
    ambiguous error → `_execute_orchestrator_workers` composes a §26.2 EFFECT_FENCE_AMBIGUOUS pause
    carrying the new `orchestrator_effect_fence_resume` (held key + orchestrator step_id/step_kind).
    NOTHING ran (no `fan_out_resume`); the snapshot is hash-valid + round-trips."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(), ctx=ctx)

    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    assert snap.pause_reason is WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS
    # The orchestrator paused BEFORE any worker / its own capture → no fan_out_resume.
    assert snap.fan_out_resume is None
    oefr = snap.orchestrator_effect_fence_resume
    assert oefr == OrchestratorEffectFencePausedResumeState(
        idempotency_key="fence-key-orchestrator",
        step_id="orchestrator",
        step_kind="declarative-step",
    )
    # Hash-valid (covers the new carrier) + byte round-trips.
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        orchestrator_effect_fence_resume=oefr,
    )
    restored = PauseSnapshot.model_validate(snap.model_dump(mode="json"))
    assert restored == snap


def test_orchestrator_self_effect_fence_resume_re_fire_recovers() -> None:
    """REAL-FENCE WITNESS (resume RE_FIRE): resuming an orchestrator effect-fence pause re-dispatches
    the orchestrator WITH the operator's RE_FIRE directive key-bound to its reserve, then the workers
    fan out fresh → SUCCESS. The directive is threaded onto the orchestrator's context (the NEW
    resume→orchestrator application site)."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.orchestrator_effect_fence_resume is not None
    key = snap.orchestrator_effect_fence_resume.idempotency_key

    holder = _HolderWithResolution(EffectFenceResolution.RE_FIRE)
    rec = _OrchestratorSelfFenceAmbiguousDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2), dispatcher=rec, ctx=cast(DriverContext, ctx_obj), pause_snapshot_input=snap
    )

    assert result.status is RunStatus.SUCCESS
    # The orchestrator re-dispatched WITH the RE_FIRE directive key-bound to its reserve.
    assert "orchestrator" in rec.dispatched
    threaded = rec.seen_resolution["orchestrator"]
    assert threaded is not None
    assert threaded.resolution is EffectFenceResolution.RE_FIRE
    assert threaded.idempotency_key == key
    assert holder.peeked >= 1  # PEEKED (non-consuming)


def test_orchestrator_self_effect_fence_resume_abort_is_terminal_failed() -> None:
    """REAL-FENCE WITNESS (resume ABORT): an ABORT directive threaded to the orchestrator re-dispatch
    raises `EffectFenceAbortedError` (the runtime applying the operator's choice) → the generic
    orchestrator-dispatch except returns terminal FAILED, NOT a re-pause."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.orchestrator_effect_fence_resume is not None

    holder = _HolderWithResolution(EffectFenceResolution.ABORT)
    rec = _OrchestratorSelfFenceAmbiguousDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2), dispatcher=rec, ctx=cast(DriverContext, ctx_obj), pause_snapshot_input=snap
    )

    assert result.status is RunStatus.FAILED
    assert result.pause_snapshot is None  # terminal — NOT a re-pause
    assert "orchestrator" in rec.dispatched  # the orchestrator DID re-dispatch (then aborted)


def test_orchestrator_self_effect_fence_resume_skip_as_fired_rejected() -> None:
    """REAL-FENCE WITNESS (resume SKIP_AS_FIRED REJECTED): SKIP_AS_FIRED is rejected at the CP resume
    site for an orchestrator (its empty output would silently structure a degenerate fan-out
    aggregate — no-silent-failure). The orchestrator is NOT re-dispatched; the run FAILS loud."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.orchestrator_effect_fence_resume is not None

    holder = _HolderWithResolution(EffectFenceResolution.SKIP_AS_FIRED)
    rec = _OrchestratorSelfFenceAmbiguousDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2), dispatcher=rec, ctx=cast(DriverContext, ctx_obj), pause_snapshot_input=snap
    )

    assert result.status is RunStatus.FAILED
    assert "skip-as-fired-unsupported" in (result.fail_class or "")
    assert rec.dispatched == []  # rejected BEFORE any dispatch — never re-dispatched


def test_orchestrator_self_effect_fence_resume_changed_orchestrator_fails_closed() -> None:
    """REAL-FENCE WITNESS (changed-orchestrator guard): an orchestrator re-supplied on resume with a
    CHANGED step_kind (same step_id) FAILS CLOSED — threading the resolution would reach the WRONG
    (or no) fence and silently abandon the original ambiguous effect."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.orchestrator_effect_fence_resume is not None

    # Resume with the orchestrator CHANGED declarative-step → inference-step (same step_id).
    changed = [
        WorkflowStep(
            step_id=StepID("orchestrator"),
            step_kind=StepKind.INFERENCE_STEP,
            step_payload={"role": "orchestrator"},
        ),
        WorkflowStep(
            step_id=StepID("worker-0"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 0},
        ),
        WorkflowStep(
            step_id=StepID("worker-1"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 1},
        ),
    ]
    holder = _HolderWithResolution(EffectFenceResolution.RE_FIRE)
    rec = _OrchestratorSelfFenceAmbiguousDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=changed, dispatcher=rec, ctx=cast(DriverContext, ctx_obj), pause_snapshot_input=snap
    )

    assert result.status is RunStatus.FAILED
    assert "changed-orchestrator" in (result.fail_class or "")
    assert rec.dispatched == []  # fail-closed BEFORE any re-dispatch


def test_orchestrator_self_effect_fence_no_protocol_fails_closed() -> None:
    """Part-2 gating: the orchestrator raises the fence error but NO PauseResumeProtocol is bound →
    the pause cannot be composed → fall through to terminal FAILED (the pre-arc fail-closed; resume
    would advertise a resumability `api.resume` cannot honor)."""
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.pause_resume_protocol = None  # type: ignore[assignment]
    result = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, ctx_obj),
    )
    assert result.status is RunStatus.FAILED
    assert result.pause_snapshot is None


def test_orchestrator_self_effect_fence_resume_empty_body_fails_closed() -> None:
    """Empty-body guard (out-of-family Codex [P2], codex-vs-main): an orchestrator fence pause
    resumed with the body CHANGED to EMPTY (`steps=[]`) FAILS CLOSED — the empty-steps SUCCESS fast
    path must NOT silently abandon the unresolved ambiguous orchestrator effect + the operator's
    resolution (the changed-orchestrator guard reads `steps[0]`, which an empty body lacks)."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.orchestrator_effect_fence_resume is not None

    holder = _HolderWithResolution(EffectFenceResolution.RE_FIRE)
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=[],  # body changed to empty between pause and resume
        dispatcher=_OrchestratorSelfFenceAmbiguousDispatcher(),
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )
    assert result.status is RunStatus.FAILED
    assert "changed-orchestrator" in (result.fail_class or "")
    assert result.pause_snapshot is None


class _OrchestratorSelfFenceSynthDispatcher:
    """Orchestrator raises the fence ambiguous error on its FIRST dispatch; on RESUME (a directive
    threaded) it re-fires; workers echo; the terminal POST_JOIN_SYNTHESIS composes. Witnesses an
    orchestrator fence pause in a SYNTHESIS-bearing workflow."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step.step_kind is StepKind.POST_JOIN_SYNTHESIS:
            return {"synthesized": True}
        if step_id == "orchestrator":
            directive = getattr(step_context, "effect_fence_resolution", None)
            if directive is None:
                raise EffectFenceAmbiguousUncommittedError(idempotency_key="fence-key-orchestrator")
            return {"role": "orchestrator", "refired": True}
        return {"role": step_id, "echoed": dict(step.step_payload)}


def test_orchestrator_self_effect_fence_resume_synthesis_bearing_recovers() -> None:
    """REAL-FENCE WITNESS (synthesis-bearing, out-of-family Codex [P2]): an orchestrator fence pause
    in an ORCHESTRATOR_WORKERS workflow that ALSO carries a terminal POST_JOIN_SYNTHESIS step
    RESUMES — the synthesis material-diff is SKIPPED (the orchestrator paused BEFORE everything, so
    nothing ran + no synthesis identity was captured; the orchestrator + workers + synthesis all
    re-dispatch fresh on RE_FIRE). Before the [P2] fix the absent captured synthesis identity falsely
    rejected the unchanged synthesis-bearing body as a 'removed' material diff."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    paused = execute_workflow(
        _manifest(),
        [*_steps(2), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry, _SynthRegistry(_OrchestratorSelfFenceSynthDispatcher())
        ),
    )
    assert paused.status is RunStatus.PAUSED
    snap = paused.pause_snapshot
    assert snap is not None and snap.orchestrator_effect_fence_resume is not None

    holder = _HolderWithResolution(EffectFenceResolution.RE_FIRE)
    rec = _OrchestratorSelfFenceSynthDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = execute_workflow(
        _manifest(),
        [*_steps(2), _synthesis_step()],
        run_id="run-1",
        ctx=cast(DriverContext, ctx_obj),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(StepDispatcherRegistry, _SynthRegistry(rec)),
        pause_snapshot_input=snap,
    )
    assert result.status is RunStatus.SUCCESS  # NOT rejected by the synthesis material-diff
    # Everything re-dispatched fresh (orchestrator re-fired + workers + synthesis once).
    assert "orchestrator" in rec.dispatched
    assert rec.dispatched.count("synthesis") == 1


# ---------------------------------------------------------------------------
# B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION — two workers fence-pause in ONE barrier;
# resume resolves them DIFFERENTLY via the `effect_fence_resolutions` per-key map
# (idempotency_key -> EffectFenceResolution), the single field staying the uniform default.
# Real-fence witness: the REAL `_execute_orchestrator_workers` TaskGroup+shield.
# ---------------------------------------------------------------------------


class _OrchestratorTwoFenceDispatcher:
    """Orchestrator completes; BOTH workers raise the effect-fence ambiguous error with DISTINCT
    keys, synchronized on a barrier so both are in-flight BEFORE either raises → TWO
    `effect_fence_paused_branches` in one pause (the per-branch-distinct precondition)."""

    def __init__(self) -> None:
        self._barrier = threading.Barrier(2, timeout=10.0)
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id == "orchestrator":
            return {"role": "orchestrator"}
        self._barrier.wait()
        raise EffectFenceAmbiguousUncommittedError(idempotency_key=f"fence-key-{step_id}")


class _HolderWithResolutions:
    """Stand-in `ResumeContextHolder` — `peek()` returns a ResumeContext carrying a per-key
    `effect_fence_resolutions` map (B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION) + an optional
    uniform `effect_fence_resolution` default."""

    def __init__(
        self,
        resolutions: dict[str, EffectFenceResolution],
        *,
        uniform: EffectFenceResolution | None = None,
    ) -> None:
        self._rc = ResumeContext(
            effect_fence_resolution=uniform,
            effect_fence_resolutions=resolutions,
        )
        self.peeked = 0

    def peek(self) -> ResumeContext:
        self.peeked += 1
        return self._rc


class _OrchestratorPartialResumeDispatcher:
    """Resume-side (partial-map iterative witness): a worker WITH a threaded directive resolves
    (success); a worker WITHOUT one (unanswered → INERT) RE-RAISES the fence — modelling the
    runtime's INERT re-pause — so the unanswered worker re-pauses while the answered one resolves
    terminal."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.seen_resolution: dict[str, Any] = {}

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        directive = getattr(step_context, "effect_fence_resolution", None)
        self.seen_resolution[step_id] = directive
        if step_id == "orchestrator":
            return {"role": "orchestrator"}
        if directive is None:
            raise EffectFenceAmbiguousUncommittedError(idempotency_key=f"fence-key-{step_id}")
        return {"role": step_id, "echoed": dict(step.step_payload)}


def _orchestrator_two_fence_pause() -> PauseSnapshot:
    """Drive a real ORCHESTRATOR_WORKERS pause with BOTH workers effect-fence-paused; return the
    snapshot (the per-branch-distinct precondition)."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_OrchestratorTwoFenceDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    assert paused.status is RunStatus.PAUSED
    snap = paused.pause_snapshot
    assert snap is not None and snap.fan_out_resume is not None
    assert len(snap.fan_out_resume.effect_fence_paused_branches) == 2
    return snap


def test_orchestrator_per_branch_distinct_resolutions() -> None:
    """REAL-FENCE WITNESS (ORCHESTRATOR_WORKERS, per-branch-DISTINCT): two workers fence-pause in
    one barrier; resume resolves worker-0 SKIP_AS_FIRED + worker-1 RE_FIRE via the per-key
    `effect_fence_resolutions` map — each worker re-dispatched with ITS OWN key-bound resolution,
    the capability the single uniform field could not express."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.SKIP_AS_FIRED,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _OrchestratorResumeRecordingDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.SUCCESS
    w0 = rec.seen_resolution["worker-0"]
    w1 = rec.seen_resolution["worker-1"]
    assert w0 is not None and w0.resolution is EffectFenceResolution.SKIP_AS_FIRED
    assert w0.idempotency_key == key_by_index[0]
    assert w1 is not None and w1.resolution is EffectFenceResolution.RE_FIRE
    assert w1.idempotency_key == key_by_index[1]


def test_orchestrator_per_branch_map_overrides_uniform_default() -> None:
    """The per-key map OVERRIDES the uniform `effect_fence_resolution` default per branch: worker-0
    (in the map) gets SKIP_AS_FIRED; worker-1 (absent from the map) falls back to the uniform
    RE_FIRE default — the `default + per-key override` composition."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {key_by_index[0]: EffectFenceResolution.SKIP_AS_FIRED},
        uniform=EffectFenceResolution.RE_FIRE,
    )
    rec = _OrchestratorResumeRecordingDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.SUCCESS
    assert rec.seen_resolution["worker-0"].resolution is EffectFenceResolution.SKIP_AS_FIRED
    assert rec.seen_resolution["worker-1"].resolution is EffectFenceResolution.RE_FIRE  # fallback


def test_orchestrator_partial_map_unanswered_worker_re_pauses_iteratively() -> None:
    """Partial-map iterative composability: a map answering ONLY worker-0 (no uniform default) →
    worker-0 resolves terminal, worker-1 (unanswered → INERT) re-pauses carrying its residual. The
    NEW snapshot holds ONLY the still-unanswered worker-1, so a subsequent resume can answer it."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    # Answer ONLY worker-0; worker-1 left unanswered with NO uniform fallback.
    holder = _HolderWithResolutions({key_by_index[0]: EffectFenceResolution.SKIP_AS_FIRED})
    rec = _OrchestratorPartialResumeDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.PAUSED
    snap2 = result.pause_snapshot
    assert snap2 is not None and snap2.fan_out_resume is not None
    efp2 = snap2.fan_out_resume.effect_fence_paused_branches
    assert len(efp2) == 1
    assert efp2[0].branch_index == 1  # only worker-1 still paused
    assert efp2[0].idempotency_key == key_by_index[1]
    # worker-0 got its SKIP directive (answered); worker-1 got None (unanswered → re-paused).
    assert rec.seen_resolution["worker-0"].resolution is EffectFenceResolution.SKIP_AS_FIRED
    assert rec.seen_resolution["worker-1"] is None


def test_orchestrator_no_map_uniform_default_applies_to_all() -> None:
    """Backward-compat: with NO map (the v1.65 shape), the single uniform `effect_fence_resolution`
    applies to BOTH fence-paused workers — byte-identical to the pre-arc behavior."""
    snap = _orchestrator_two_fence_pause()
    holder = _HolderWithResolution(EffectFenceResolution.RE_FIRE)  # single field only, no map
    rec = _OrchestratorResumeRecordingDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.SUCCESS
    assert rec.seen_resolution["worker-0"].resolution is EffectFenceResolution.RE_FIRE
    assert rec.seen_resolution["worker-1"].resolution is EffectFenceResolution.RE_FIRE


class _OrchestratorAbortGuardDispatcher:
    """Resume-side abort-guard witness: an ABORT directive raises EffectFenceAbortedError; a
    RE_FIRE / SKIP directive FIRES (records the branch in `fired`); a None directive (a suppressed
    sibling) RE-RAISES the ambiguous fence (re-pause, no fire). Witnesses that under a mixed
    {ABORT, RE_FIRE} map the RE_FIRE sibling does NOT fire before the ABORT fails the run."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.fired: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id == "orchestrator":
            return {"role": "orchestrator"}
        directive = getattr(step_context, "effect_fence_resolution", None)
        if directive is None:
            raise EffectFenceAmbiguousUncommittedError(idempotency_key=f"fence-key-{step_id}")
        if directive.resolution is EffectFenceResolution.ABORT:
            raise EffectFenceAbortedError(f"operator aborted {step_id}")
        self.fired.append(step_id)  # RE_FIRE / SKIP would fire the effect
        return {"role": step_id, "echoed": dict(step.step_payload)}


def test_orchestrator_mixed_abort_map_suppresses_sibling_refire() -> None:
    """Codex [P1] (ORCHESTRATOR_WORKERS): a mixed map {worker-0: ABORT, worker-1: RE_FIRE} must NOT
    fire the RE_FIRE sibling before the ABORT fails the run — ABORT stays run-level-terminal
    (v1.65 §1(b)). The RE_FIRE sibling's directive is SUPPRESSED (re-pauses INERT, no fire); the
    run FAILs. Per-branch-SCOPED abort (fire survivors anyway) is the registered follow-on."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED
    assert "orchestrator-workers-effect-fence-aborted" in (result.fail_class or "")
    assert result.pause_snapshot is None  # terminal — NOT a re-pause
    assert "worker-1" not in rec.fired  # the RE_FIRE sibling was SUPPRESSED — did NOT fire


# ---------------------------------------------------------------------------
# B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (ORCHESTRATOR_WORKERS) — the per-branch-SCOPED
# abort (`ABORT_BRANCH`): fail JUST one worker, let the vouched-for siblings FIRE, fold survivors
# per cascade_policy. The symmetric witness of the parallelization peer case (same shared sites).
# ---------------------------------------------------------------------------


def test_orchestrator_scoped_abort_fires_vouched_sibling() -> None:
    """CRUX contrasting baseline (ORCHESTRATOR_WORKERS; inverse of
    test_orchestrator_mixed_abort_map_suppresses_sibling_refire): a mixed map
    {worker-0: ABORT_BRANCH, worker-1: RE_FIRE} fails JUST worker-0 (never re-dispatched →
    at-most-once) and FIRES the vouched-for RE_FIRE sibling → the run folds the survivor → PARTIAL
    (NOT the run-level-ABORT FAILED)."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.PARTIAL  # survivor folded, NOT the run-level-ABORT FAILED
    # PARTIAL carries fail_class=None like every degraded PARTIAL (the aborted worker is a degraded
    # terminal non-contributor; run-result provenance is FAILED-only — see the all-abort test).
    assert result.fail_class is None
    assert "worker-1" in rec.fired  # the vouched-for RE_FIRE sibling FIRED (NOT suppressed)
    assert "worker-0" not in rec.dispatched  # the scoped-abort worker was NEVER re-dispatched


def test_orchestrator_all_scoped_abort_fails_not_vacuous_partial() -> None:
    """All-abort guard (advisor watchpoint #1; ORCHESTRATOR_WORKERS): when EVERY fence-paused worker
    is scoped-aborted, branch_plan is empty and NO worker survived → the run is FAILED, NOT the
    vacuous PARTIAL the empty-`branch_plan` short-circuit's `_degraded` would otherwise return."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.ABORT_BRANCH,
        }
    )
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED  # NO survivor → FAILED, not a vacuous PARTIAL
    assert "orchestrator-workers-effect-fence-branch-aborted" in (result.fail_class or "")
    assert "worker-0" not in rec.dispatched  # neither scoped-abort worker was re-dispatched
    assert "worker-1" not in rec.dispatched


def test_orchestrator_scoped_abort_iterative_repause() -> None:
    """Iterative re-pause (advisor watchpoint #4; ORCHESTRATOR_WORKERS): a map answering ONLY
    worker-0 (ABORT_BRANCH) while worker-1 is left unresolved → worker-0 finalizes as a TERMINAL
    branch (next resume SKIPS it) and worker-1 re-pauses INERT (carried forward as still
    fence-paused)."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions({key_by_index[0]: EffectFenceResolution.ABORT_BRANCH})
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.PAUSED
    snap2 = result.pause_snapshot
    assert snap2 is not None and snap2.fan_out_resume is not None
    # worker-0 (scoped-aborted) is now a TERMINAL branch — a later resume SKIPS it (never re-fires).
    assert 0 in {b.branch_index for b in snap2.fan_out_resume.branches}
    # worker-1 (unresolved) re-paused INERT — still fence-paused, carried forward.
    assert {b.branch_index for b in snap2.fan_out_resume.effect_fence_paused_branches} == {1}
    assert "worker-0" not in rec.dispatched  # the scoped-abort worker was NEVER re-dispatched


def test_orchestrator_mixed_run_abort_and_scoped_abort_deterministic() -> None:
    """advisor [P1] (precedence; ORCHESTRATOR_WORKERS): a mixed map {worker-0: ABORT, worker-1:
    ABORT_BRANCH} — run-level ABORT dominates (the run FAILs), but the scoped-abort worker-1 MUST be
    recorded DETERMINISTICALLY (excluded from re-dispatch), NOT nulled by the run-level-ABORT
    suppression and re-dispatched into the ABORT race. Witnesses the interception-BEFORE-suppression
    ordering: worker-1 is NEVER dispatched."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT,
            key_by_index[1]: EffectFenceResolution.ABORT_BRANCH,
        }
    )
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED  # run-level ABORT dominates
    assert "orchestrator-workers-effect-fence-aborted" in (result.fail_class or "")
    assert "worker-0" in rec.dispatched  # the ABORT worker re-dispatched → raised → FAILED
    assert "worker-1" not in rec.dispatched  # the scoped-abort worker deterministically EXCLUDED


def test_orchestrator_scoped_abort_under_cascade_cancel_fails() -> None:
    """Codex [P1] (CASCADE_CANCEL tier; ORCHESTRATOR_WORKERS): a MIXED scoped-abort + surviving
    worker resumed under MULTI_TENANT_COMPLIANCE (CascadePolicy.CASCADE_CANCEL) must FAIL — NOT a
    SUCCESS with the surviving worker as final_state. The cascade-cancel block returns before the
    §25.15.1 degraded fold, so the scoped-abort guard must fire on this tier too."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,  # → CascadePolicy.CASCADE_CANCEL
    )

    assert result.status is RunStatus.FAILED  # NOT a SUCCESS hiding the scoped-abort
    assert "orchestrator-workers-effect-fence-branch-aborted" in (result.fail_class or "")


def test_orchestrator_scoped_abort_under_proceed_rejected_requires_strict_tier() -> None:
    """Codex [P2] (PROCEED tier; ORCHESTRATOR_WORKERS, MIXED): an effect-fence pause resumed under
    SOLO_DEVELOPER (CascadePolicy.PROCEED) with a surviving worker is rejected FAIL-CLOSED with
    `...-requires-strict-tier` (the existing guard, branch_plan non-empty) — scoped-abort recording
    SKIPPED (fail-closed precedes durable writes)."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
        persona_tier=PersonaTier.SOLO_DEVELOPER,  # → CascadePolicy.PROCEED
    )

    assert result.status is RunStatus.FAILED
    assert "orchestrator-workers-effect-fence-resume-requires-strict-tier" in (
        result.fail_class or ""
    )
    assert "worker-0" not in rec.dispatched  # no dispatch (rejected before the barrier)


def test_orchestrator_all_scoped_abort_under_proceed_requires_strict_tier() -> None:
    """Codex [P2] (PROCEED tier; ORCHESTRATOR_WORKERS, ALL-abort): an all-scoped-abort PROCEED resume
    empties branch_plan → the `not branch_plan` short-circuit (which returns BEFORE the existing
    strict-tier guard). The early gate there must report `...-requires-strict-tier` (NOT the
    scoped-abort fail_class nor a SUCCESS), and NO scoped-abort durable write happened."""
    snap = _orchestrator_two_fence_pause()
    key_by_index = {
        b.branch_index: b.idempotency_key for b in snap.fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.ABORT_BRANCH,
        }
    )
    rec = _OrchestratorAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
        persona_tier=PersonaTier.SOLO_DEVELOPER,  # → CascadePolicy.PROCEED
    )

    assert result.status is RunStatus.FAILED
    assert "orchestrator-workers-effect-fence-resume-requires-strict-tier" in (
        result.fail_class or ""
    )
