"""B-FANOUT-PAUSE-PARALLELIZATION (R-FS-1) — resumable `cascade_policy=pause` for the
PARALLELIZATION (peer fan-out) topology.

Materializes the cleared CP spec §25.15.1 `pause → PAUSED` row ("composes with
C-CP-26 PauseResumeProtocol + C-RT-35 `api.resume`") for PARALLELIZATION, flipping
the interim `parallelization-pause-resume-not-yet-materialized` FAILED to a genuine
resumable PAUSED — the `_execute_orchestrator_workers` (U-CP-88 / B-FANOUT-PAUSE)
shape applied PARALLELIZATION-shaped: NO orchestrator `steps[0]`, every step is a
PEER branch (indexed over `steps`), so the resume state is `PeerFanOutResumeState`
(branches + branch_count), NOT the orchestrator-bearing `FanOutResumeState`.

The honest bar (no false-`PAUSED`): a PAUSED is returned ONLY when a
`pause_resume_protocol` is bound so a `PeerFanOutResumeState`-bearing `PauseSnapshot`
can actually be captured, and `api.resume` (via the real `execute_workflow(
pause_snapshot_input=...)` entry-point resume detection — the exact path the runtime
`api.resume` drives) genuinely re-enters the strategy: terminal branches are SKIPPED
(§25.15.2 obligation 7, outputs recovered), the not-yet-dispatched ones re-dispatched.

Prerequisite: B-PARALLELIZATION-CASCADE (closed) built the cascade_policy harvest
this resume builds on (PARALLELIZATION had NO cascade machinery before that arc).

Authority: `Spec_Control_Plane_v1_44.md` §1 (PeerFanOutResumeState) + §2 (§25.15.1
PARALLELIZATION materialization note); `pause_resume_protocol_types.py` C-CP-26.
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
from harness_cp.pause_resume_protocol import (
    PauseResumeProtocol,
    _compute_snapshot_hash,
    _strip_default_fanout_resume_fields,
)
from harness_cp.pause_resume_protocol_types import (
    EffectFencePausedBranchResumeState,
    EffectFenceResolution,
    EffectFenceResumeState,
    EvaluatorOptimizerResumeState,
    FanOutBranchResumeState,
    FanOutResumeState,
    HandoffResumeState,
    PauseSnapshot,
    PeerFanOutResumeState,
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
    _DriverStrategyStatus,
    _resume_carrier_topology_mismatch,
    _synthesis_resume_material_diff,
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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-par-pause")
_PAUSE_TIER = PersonaTier.TEAM_BINDING  # → cascade_policy = pause
_ANCHOR = "0" * 64  # constant MVP pause-context anchor (no material diff on resume)


def _manifest(
    workflow_id: str = "wf-pp", persona_tier: PersonaTier = _PAUSE_TIER
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.PARALLELIZATION,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _steps(n_branches: int) -> list[WorkflowStep]:
    """A PEER fan-out — every step IS a branch (NO orchestrator `steps[0]`)."""
    return [
        WorkflowStep(
            step_id=StepID(f"branch-{i}"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": i},
        )
        for i in range(n_branches)
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
    """MVP constant-sentinel reader: empty StateSummary + a constant anchor →
    resume detects no material diff → admits."""
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
    """Driver context WITH a bound `pause_resume_protocol` (the pause/resume opt-in)
    so the peer fan-out `pause` branch can capture a snapshot + return PAUSED, and
    `execute_workflow(pause_snapshot_input=...)` entry-point resume detection can
    validate + admit a resume. `procedural_tier_snapshot_resolver` absent → the
    R-003 sidecar stays None."""

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
    """Echoes `{role, echoed}`; records every dispatched step_id (so a resume can
    assert which branches were re-dispatched vs terminal-skipped). A step_id in
    `fail_step_ids` raises (the cascade trigger)."""

    def __init__(self, *, fail_step_ids: set[str] | None = None) -> None:
        self._fail = fail_step_ids or set()
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id in self._fail:
            raise RuntimeError(f"simulated branch failure at {step_id}")
        return {"role": step_id, "echoed": dict(step.step_payload)}


class _SynthDispatcher:
    """B-FANOUT-PAUSE-SYNTHESIS — handles BOTH peer branches (`DECLARATIVE_STEP`,
    echoing `{role, echoed}`) AND the terminal `POST_JOIN_SYNTHESIS` step (returning a
    DISTINCT `{synthesized, from}` marker so a test can prove the run's aggregate is the
    SYNTHESIZED output, NOT the deterministic `{branch_outputs}` fold). Records every
    dispatched step_id so the synthesis-dispatched-exactly-once + branches-re-dispatched
    claims are checkable."""

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
    """Routes BOTH `DECLARATIVE_STEP` (branches) and `POST_JOIN_SYNTHESIS` (the
    post-barrier synthesis) to one `_SynthDispatcher`."""

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


class _GatedFailDispatcher:
    """Forces a DETERMINISTIC all-terminal pause: branch-0 completes cleanly and
    sets a gate; branch-1 waits on that gate THEN fails. So both branches reach a
    terminal disposition (branch-0 `completed`+output / branch-1 ran-and-errored
    `completed`/no-output) BEFORE the barrier resolves — no not-yet-dispatched
    (cancelled) branch, no timing race."""

    def __init__(self) -> None:
        self._gate = threading.Event()
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id == "branch-0":
            self._gate.set()
            return {"role": "branch-0", "echoed": dict(step.step_payload)}
        # branch-1: wait until branch-0 has completed, then fail (the trigger).
        assert self._gate.wait(timeout=10.0), "branch-0 never completed"
        raise RuntimeError("simulated branch-1 failure (after branch-0 completed)")


def _run(
    *,
    steps: list[WorkflowStep],
    dispatcher: StepDispatcher,
    ctx: DriverContext,
    pause_snapshot_input: PauseSnapshot | None = None,
    workflow_id: str = "wf-pp",
    persona_tier: PersonaTier = _PAUSE_TIER,
) -> Any:
    return execute_workflow(
        _manifest(workflow_id, persona_tier),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
        pause_snapshot_input=pause_snapshot_input,
    )


def _captured_snapshot(
    *, peer_fan_out_resume: PeerFanOutResumeState, workflow_id: str = "wf-pp"
) -> PauseSnapshot:
    """A hash-valid peer fan-out snapshot, captured through the real protocol (NOT a
    hand-mutated model) — the exact shape a prior `pause` halt would produce."""
    return asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id=workflow_id,
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            peer_fan_out_resume=peer_fan_out_resume,
        )
    )


# ---------------------------------------------------------------------------
# Capture — a real peer fan-out pause returns PAUSED + a peer-aware snapshot
# ---------------------------------------------------------------------------


def test_pause_with_protocol_returns_paused_with_peer_snapshot() -> None:
    """TEAM persona → pause, protocol bound: branch-1 fails (after branch-0
    completes) → the run PAUSES (not the interim FAILED) with a hash-valid
    `PauseSnapshot` carrying a `PeerFanOutResumeState` (NO orchestrator; the
    terminal branches + branch-0's recovered output)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=_GatedFailDispatcher(), ctx=ctx)

    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    # PARALLELIZATION sets `peer_fan_out_resume`, NEVER the orchestrator-bearing one.
    assert snap.fan_out_resume is None
    pr = snap.peer_fan_out_resume
    assert pr is not None
    assert pr.branch_count == 2
    by_index = {b.branch_index: b for b in pr.branches}
    # branch-0 completed cleanly → terminal + its output recovered into the snapshot.
    assert by_index[0].terminal_status == "completed"
    assert by_index[0].step_id == "branch-0"  # identity captured for resume validation
    assert by_index[0].output == {"role": "branch-0", "echoed": {"index": 0}}
    # branch-1 ran-and-errored → terminal `completed` (dispatch-boundary), no output.
    assert by_index[1].terminal_status == "completed"
    assert by_index[1].step_id == "branch-1"
    assert by_index[1].output is None
    # The snapshot is hash-valid (covers peer_fan_out_resume).
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        peer_fan_out_resume=pr,
    )


def test_pause_emits_resumption_not_workflow_start_on_resume() -> None:
    """The resume envelope emits RESUMPTION (the terminal branches already ran in
    the original envelope), not a second WORKFLOW_START."""
    emitter = _Emitter()
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=emitter))
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="branch-0",
                    terminal_status="completed",
                    output={"role": "branch-0"},
                ),
            ),  # branch-1 absent → re-dispatchable
            branch_count=2,
        )
    )
    _run(steps=_steps(2), dispatcher=_CountingDispatcher(), ctx=ctx, pause_snapshot_input=snapshot)
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    assert WorkflowEventClass.WORKFLOW_START not in emitter.emits


# ---------------------------------------------------------------------------
# Resume — the real `execute_workflow(pause_snapshot_input=...)` witness
# ---------------------------------------------------------------------------


def test_resume_skips_terminal_recovers_outputs_and_redispatches_rest() -> None:
    """THE WITNESS — through the real `execute_workflow(pause_snapshot_input=...)`
    entry-point resume detection (the path `api.resume` drives):
      (1) the terminal branch (branch-0) is NOT re-dispatched (obligation 7),
      (2) the not-yet-dispatched branch (branch-1) IS re-dispatched,
      (3) the aggregate fuses the RECOVERED branch-0 output + the FRESH branch-1
          output → SUCCESS."""
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="branch-0",
                    terminal_status="completed",
                    output={"role": "branch-0", "recovered": True},
                ),
            ),  # branch-1 ABSENT → left re-dispatchable
            branch_count=2,
        )
    )
    dispatcher = _CountingDispatcher()
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=dispatcher, ctx=ctx, pause_snapshot_input=snapshot)

    assert result.status is RunStatus.SUCCESS
    # (1): the terminal branch-0 was NOT re-dispatched.
    assert "branch-0" not in dispatcher.dispatched
    # (2): only the re-dispatchable branch-1 ran on resume.
    assert dispatcher.dispatched == ["branch-1"]
    # (3): the aggregate's branch_outputs fuse recovered (branch-0) + fresh (branch-1).
    assert result.final_state is not None
    assert result.final_state["branch_outputs"]["branch-0"] == {
        "role": "branch-0",
        "recovered": True,
    }
    assert result.final_state["branch_outputs"]["branch-1"] == {
        "role": "branch-1",
        "echoed": {"index": 1},
    }


def test_resume_all_terminal_with_a_failed_branch_is_partial_not_silent_success() -> None:
    """Real pause → real resume round-trip (the GatedFail all-terminal pause): both
    branches terminal at pause (branch-0 completed, branch-1 FAILED) → resume
    re-dispatches NOTHING and surfaces **PARTIAL** (degraded), NOT a bare silent
    SUCCESS dropping the failure — the silent-degradation class this arc forecloses.
    branch-0's output is recovered; the failed branch-1 contributes nothing + is not
    re-fired (obligation 7 + at-most-once)."""
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
    # The salvaged aggregate is on partial_state; branch-0 recovered, branch-1 gone.
    assert result.partial_state is not None
    assert "branch-0" in result.partial_state["branch_outputs"]
    assert "branch-1" not in result.partial_state["branch_outputs"]


# ---------------------------------------------------------------------------
# Negative controls + integrity + backward-compat
# ---------------------------------------------------------------------------


def test_snapshot_hash_covers_peer_fan_out_resume_tamper_rejected() -> None:
    """Integrity: a snapshot whose recovered branch output is TAMPERED (without
    re-hashing) is REJECTED at resume → FAILED + CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION
    (no silent-tamper gap on the data the resumed aggregate trusts)."""
    good = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="branch-0",
                    terminal_status="completed",
                    output={"amount": 100},
                ),
            ),
            branch_count=2,
        )
    )
    # Tamper the recovered output, keeping the STALE hash → corruption.
    tampered = good.model_copy(
        update={
            "peer_fan_out_resume": good.peer_fan_out_resume.model_copy(  # type: ignore[union-attr]
                update={
                    "branches": (
                        FanOutBranchResumeState(
                            branch_index=0,
                            step_id="branch-0",
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
    recovered branch-0 output) re-dispatches BOTH branches and branch-0's output is
    the FRESH one — proving the recovered output in the snapshot is what populates
    the aggregate, not an incidental re-run."""
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(),  # nothing recovered → both branches re-dispatchable
            branch_count=2,
        )
    )
    dispatcher = _CountingDispatcher()
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=dispatcher, ctx=ctx, pause_snapshot_input=snapshot)
    assert result.status is RunStatus.SUCCESS
    # BOTH branches re-dispatched (no terminal skip); branch-0's output is the FRESH
    # echo (no "recovered" marker), proving the recovered-output path is the only
    # source of a recovered value (vs. this incidental re-run).
    assert set(dispatcher.dispatched) == {"branch-0", "branch-1"}
    assert result.final_state is not None
    assert result.final_state["branch_outputs"]["branch-0"] == {
        "role": "branch-0",
        "echoed": {"index": 0},
    }


def test_resume_with_matching_synthesis_fresh_dispatches_succeeds() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS full-chain (PARALLELIZATION) — a synthesis-bearing peer
    fan-out PAUSE is now RESUMABLE. The snapshot carries the synthesis identity
    (`synthesis_step_id="synthesis"`); resume material-diffs it against the re-supplied
    terminal synthesis (match), recovers/re-dispatches the branches, then FRESH-dispatches
    the synthesis post-barrier (it never ran on a pause → effect-free, first-and-only).
    The load-bearing assertions: the aggregate is the SYNTHESIZED output (NOT the
    deterministic `{branch_outputs}` fold), and the synthesis dispatched EXACTLY ONCE."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(), branch_count=2, synthesis_step_id="synthesis"
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
    # The aggregate is the SYNTHESIZED output, NOT the deterministic fold — a fold-fallback
    # bug (synthesis silently skipped) would yield `{"branch_outputs": ...}` and pass a bare
    # "resume succeeds" test. `from` carries the branch-index-ordered sibling step_ids.
    assert result.final_state == {"synthesized": True, "from": (0, 1)}
    # The synthesis dispatched EXACTLY ONCE (first-and-only — no replay, no double-dispatch).
    assert dispatcher.dispatched.count("synthesis") == 1
    # Both branches were re-dispatched (nothing recovered in this snapshot).
    assert {s for s in dispatcher.dispatched if s.startswith("branch")} == {
        "branch-0",
        "branch-1",
    }


def test_resume_synthesis_added_fails_closed() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS material-diff (ADDED) — a snapshot captured WITHOUT a
    synthesis (`synthesis_step_id=None`) but resumed against a body that NOW carries a
    terminal synthesis fails closed: the synthesis was added between pause and resume, so a
    fresh dispatch would compose an aggregate the original run never produced. Fail-closed
    BEFORE any branch/synthesis dispatch (the original [P1] reject posture preserved as a
    typed material-diff)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(branches=(), branch_count=2)
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


def test_resume_synthesis_removed_fails_closed() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS material-diff (REMOVED) — a snapshot that CAPTURED a
    synthesis (`synthesis_step_id="synthesis"`) but resumed against a body with NO terminal
    synthesis fails closed rather than silently yielding the deterministic fold. This is the
    silent-DROP case a check nested inside the placement block would structurally miss (the
    resumed body has no synthesis position to trigger it)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(), branch_count=2, synthesis_step_id="synthesis"
        )
    )
    result = _run(
        steps=_steps(2),  # NO synthesis step on resume
        dispatcher=_CountingDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert result.fail_class.startswith("post-join-synthesis-resume-material-diff:")


def test_resume_synthesis_changed_step_id_fails_closed() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS material-diff (CHANGED) — a snapshot that captured
    `synthesis_step_id="synthesis-a"` but resumed against a body whose terminal synthesis is
    `synthesis-b` fails closed: a same-position rename is a body change, so fresh-dispatching
    the renamed synthesis would compose a divergent aggregate."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(), branch_count=2, synthesis_step_id="synthesis-a"
        )
    )
    result = _run(
        steps=[*_steps(2), _synthesis_step("synthesis-b")],
        dispatcher=_CountingDispatcher(),
        ctx=ctx,
        pause_snapshot_input=snapshot,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert result.fail_class.startswith("post-join-synthesis-resume-material-diff:")


def test_synthesis_absent_peer_snapshot_byte_compat_hash() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS byte-compat (PeerFanOutResumeState) — a synthesis-ABSENT
    peer snapshot (`synthesis_step_id=None`) hashes byte-identically to the pre-arc shape:
    `_compute_snapshot_hash` DROPS the `synthesis_step_id` key from the canonical
    serialization when None, so every old durable PARALLELIZATION snapshot still validates.
    `PeerFanOutResumeState` had NO drop before this field, so this guards the freshly-added
    drop."""
    import hashlib
    import json

    state_summary, _ = _pause_context_reader()
    peer = PeerFanOutResumeState(branches=(), branch_count=2)  # synthesis_step_id defaults None
    got = _compute_snapshot_hash(
        workflow_id="wf-pp",
        run_id="run-1",
        step_index=0,
        state_summary=state_summary,
        peer_fan_out_resume=peer,
    )
    # The pre-arc canonical serialization — peer carrier with NO `synthesis_step_id` key.
    canonical = {
        "workflow_id": "wf-pp",
        "run_id": "run-1",
        "step_index": 0,
        "state_summary": state_summary.model_dump(mode="json"),
        "peer_fan_out_resume": {"branches": [], "branch_count": 2},
    }
    expected = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert got == expected


def test_synthesis_material_diff_helper_covers_both_carriers() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS — direct unit coverage of `_synthesis_resume_material_diff`
    over BOTH resume carriers and all identity-diff directions. Includes the HIERARCHICAL
    case: a HIERARCHICAL child re-enters via `execute_workflow(pause_snapshot_input=...)`
    against a `FanOutResumeState`-bearing child snapshot, so the FanOut branch of this helper
    IS the child-level guard."""

    def _peer(synthesis_step_id: str | None) -> PauseSnapshot:
        return _captured_snapshot(
            peer_fan_out_resume=PeerFanOutResumeState(
                branches=(), branch_count=1, synthesis_step_id=synthesis_step_id
            )
        )

    def _fanout(synthesis_step_id: str | None) -> PauseSnapshot:
        fan = FanOutResumeState(
            orchestrator_output={},
            orchestrator_step_id="orch",
            branches=(),
            worker_count=1,
            synthesis_step_id=synthesis_step_id,
        )
        return asyncio.run(
            _protocol().capture_pause_snapshot(
                workflow_id="wf-pp",
                run_id="run-1",
                step_index=0,
                pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
                fan_out_resume=fan,
            )
        )

    no_synth = _steps(1)
    with_synth = [*_steps(1), _synthesis_step("synthesis")]
    with_other = [*_steps(1), _synthesis_step("other")]
    _ORCH = _DriverStrategyStatus.ORCHESTRATOR_WORKERS
    _PAR = _DriverStrategyStatus.PARALLELIZATION
    # Each builder is read under the strategy whose carrier it populates: `_peer` (a
    # PeerFanOutResumeState snapshot) under PARALLELIZATION, `_fanout` (a FanOutResumeState
    # snapshot) under ORCHESTRATOR_WORKERS. HIERARCHICAL_DELEGATION reuses the FanOut carrier,
    # so `_fanout` under HIERARCHICAL is the child-level guard.
    for build, strat in ((_peer, _PAR), (_fanout, _ORCH)):
        # both-present match → None (OK)
        assert _synthesis_resume_material_diff(build("synthesis"), with_synth, strat) is None
        # both-absent → None (a non-synthesis fan-out resume, unchanged)
        assert _synthesis_resume_material_diff(build(None), no_synth, strat) is None
        # added (captured None, resumed present) → fail
        assert _synthesis_resume_material_diff(build(None), with_synth, strat) is not None
        # removed (captured present, resumed absent) → fail
        assert _synthesis_resume_material_diff(build("synthesis"), no_synth, strat) is not None
        # changed step_id → fail
        assert _synthesis_resume_material_diff(build("synthesis"), with_other, strat) is not None
    # HIERARCHICAL_DELEGATION reads the FanOut carrier (the child-level guard path).
    assert (
        _synthesis_resume_material_diff(
            _fanout("synthesis"), with_synth, _DriverStrategyStatus.HIERARCHICAL_DELEGATION
        )
        is None
    )
    # CARRIER/TOPOLOGY MISMATCH (out-of-family Codex [P1]) — a synthesis-bearing snapshot
    # captured under one carrier but resumed under the OTHER topology fails closed: the
    # strategy's EXPECTED carrier is absent → captured None → differs from the present
    # resumed synthesis. A `peer` snapshot resumed as ORCHESTRATOR_WORKERS, and a `fanout`
    # snapshot resumed as PARALLELIZATION — both reject (would otherwise run the fan-out fresh).
    assert _synthesis_resume_material_diff(_peer("synthesis"), with_synth, _ORCH) is not None
    assert _synthesis_resume_material_diff(_fanout("synthesis"), with_synth, _PAR) is not None


def test_strip_none_synthesis_step_id_recurses_into_nested_child_snapshots() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS byte-compat (NESTED, out-of-family Codex [P1]) — the hash
    drop must RECURSE: a HIERARCHICAL `paused_child_branches` cursor serializes a child
    `PauseSnapshot` (with its own `fan_out_resume`) inside the parent carrier's `model_dump`,
    so a nested `synthesis_step_id: null` must be stripped too — a top-level-only drop would
    change the recomputed hash of valid pre-existing HIERARCHICAL parent snapshots. This
    asserts the recursion strips None at EVERY depth while KEEPING a synthesis-bearing id."""
    tree: dict[str, Any] = {
        "synthesis_step_id": None,  # top-level (parent carrier)
        "branches": [],
        "paused_child_branches": [
            {
                "branch_index": 0,
                "step_id": "worker-0",
                "child_snapshot": {
                    "run_id": "child",
                    "fan_out_resume": {
                        "synthesis_step_id": None,  # nested child carrier — the [P1] leak
                        "branches": [],
                    },
                    "peer_fan_out_resume": {
                        "synthesis_step_id": "kept-grandchild",  # non-None → KEPT
                    },
                },
            }
        ],
    }
    _strip_default_fanout_resume_fields(tree)
    assert "synthesis_step_id" not in tree
    child = tree["paused_child_branches"][0]["child_snapshot"]
    assert "synthesis_step_id" not in child["fan_out_resume"]
    # a present (synthesis-bearing) id at ANY depth is hash-covered → KEPT.
    assert child["peer_fan_out_resume"]["synthesis_step_id"] == "kept-grandchild"


def test_strip_preserves_user_synthesis_step_id_in_recovered_output() -> None:
    """B-FANOUT-PAUSE-SYNTHESIS — the strip is PATH-AWARE (out-of-family Codex [P2]): it
    touches ONLY the carrier's OWN structural `synthesis_step_id`, NOT a user-data key that
    happens to be named `synthesis_step_id` inside recovered output (`orchestrator_output` /
    `branches[].output`). Such a user key MUST stay hash-covered — a blanket recursive walk
    would strip it (breaking byte-compat for old snapshots + leaving it uncovered for new
    ones). Witness: a recovered `orchestrator_output` with `synthesis_step_id: None` changes
    the snapshot hash (it is covered), while the carrier's own default-None field does not."""
    state_summary, _ = _pause_context_reader()

    def _hash(orchestrator_output: dict[str, Any]) -> str:
        fan = FanOutResumeState(
            orchestrator_output=orchestrator_output,
            orchestrator_step_id="orch",
            branches=(),
            worker_count=1,
        )
        return _compute_snapshot_hash(
            workflow_id="wf-pp",
            run_id="run-1",
            step_index=0,
            state_summary=state_summary,
            fan_out_resume=fan,
        )

    # A user-data `synthesis_step_id` key in recovered output is HASH-COVERED → its presence
    # changes the hash (the path-aware strip never reaches into orchestrator_output).
    assert _hash({"synthesis_step_id": None, "data": 1}) != _hash({"data": 1})
    # And it survives in the strip output (not silently removed).
    dumped = FanOutResumeState(
        orchestrator_output={"synthesis_step_id": None, "data": 1},
        orchestrator_step_id="orch",
        branches=(),
        worker_count=1,  # carrier synthesis_step_id defaults None
    ).model_dump(mode="json")
    _strip_default_fanout_resume_fields(dumped)
    assert "synthesis_step_id" not in dumped  # the CARRIER's own field is stripped
    assert "synthesis_step_id" in dumped["orchestrator_output"]  # the USER key is preserved


def test_strip_none_orchestrator_effect_fence_resume_recurses_into_nested_child_snapshots() -> None:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING byte-compat (NESTED) — the new
    `orchestrator_effect_fence_resume` is a PauseSnapshot-level field, so a HIERARCHICAL
    `paused_child_branches[].child_snapshot` serializes it (as null) inside the parent carrier's
    `model_dump`. The recursive strip drops it when None at EVERY depth, so a pre-arc nested
    HIERARCHICAL snapshot re-hashes byte-identically; a non-null carrier (a real nested orchestrator
    fence pause) is KEPT (hash-covered). Mirrors the synthesis nested-strip [P1] for the new field."""
    tree: dict[str, Any] = {
        "branches": [],
        "paused_child_branches": [
            {
                "branch_index": 0,
                "step_id": "worker-0",
                "child_snapshot": {
                    "run_id": "child",
                    "orchestrator_effect_fence_resume": None,  # nested PauseSnapshot field — drop
                    "fan_out_resume": {"branches": []},
                },
            },
            {
                "branch_index": 1,
                "step_id": "worker-1",
                "child_snapshot": {
                    "run_id": "grandchild",
                    # a REAL nested orchestrator fence pause → non-None → hash-COVERED → KEPT.
                    "orchestrator_effect_fence_resume": {
                        "idempotency_key": "k",
                        "step_id": "orch",
                        "step_kind": "tool-step",
                    },
                },
            },
        ],
    }
    _strip_default_fanout_resume_fields(tree)
    c0 = tree["paused_child_branches"][0]["child_snapshot"]
    assert "orchestrator_effect_fence_resume" not in c0  # None → stripped (byte-compat)
    c1 = tree["paused_child_branches"][1]["child_snapshot"]
    assert c1["orchestrator_effect_fence_resume"]["idempotency_key"] == "k"  # non-None → KEPT


def test_orchestrator_effect_fence_resume_absent_does_not_change_hash() -> None:
    """Top-level byte-compat — passing `orchestrator_effect_fence_resume=None` (or omitting it)
    yields the SAME hash as a pre-arc snapshot (the conditional include in `_compute_snapshot_hash`),
    so every pre-existing snapshot validates unchanged."""
    state_summary, _ = _pause_context_reader()
    base = dict(workflow_id="wf-pp", run_id="run-1", step_index=0, state_summary=state_summary)
    assert _compute_snapshot_hash(**base) == _compute_snapshot_hash(
        **base, orchestrator_effect_fence_resume=None
    )


def _captured_with(**carrier: Any) -> PauseSnapshot:
    """A hash-valid snapshot carrying exactly one topology resume carrier (or none),
    captured through the real protocol — the exact shape a prior `pause` halt under that
    topology would produce. `**carrier` is one of `fan_out_resume=` / `peer_fan_out_resume=`
    / `handoff_resume=` / `evaluator_optimizer_resume=` / `effect_fence_resume=`, or empty
    (a plain linear `step_index`-only pause)."""
    return asyncio.run(
        _protocol().capture_pause_snapshot(
            workflow_id="wf-pp",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
            **carrier,
        )
    )


def _a_fan_out(synthesis_step_id: str | None = None) -> FanOutResumeState:
    return FanOutResumeState(
        orchestrator_output={},
        orchestrator_step_id="orch",
        branches=(),
        worker_count=1,
        synthesis_step_id=synthesis_step_id,
    )


def _manifest_topology(
    topology: TopologyPattern, workflow_id: str = "wf-pp"
) -> WorkflowManifestEntry:
    """`_manifest` with an arbitrary topology — for the carrier/topology-mismatch
    by-execution tests that resume a foreign carrier under each resuming strategy."""
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=_PAUSE_TIER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=topology,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _run_topology(
    *,
    topology: TopologyPattern,
    steps: list[WorkflowStep],
    dispatcher: StepDispatcher,
    ctx: DriverContext,
    pause_snapshot_input: PauseSnapshot,
) -> Any:
    return execute_workflow(
        _manifest_topology(topology),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
        pause_snapshot_input=pause_snapshot_input,
    )


def test_resume_carrier_topology_mismatch_predicate_full_matrix() -> None:
    """B-FANOUT-RESUME-CARRIER-TOPOLOGY-MISMATCH — the general carrier↔strategy predicate
    fails closed for EVERY populated topology resume carrier resumed under a strategy that
    does NOT read it, and admits the matching carrier. This generalizes the
    B-FANOUT-PAUSE-SYNTHESIS synthesis-only guard to all carriers, synthesis-bearing or NOT
    (the v1.58 §1 "non-synthesis ... unchanged" carve-out is now closed)."""
    _LIN = _DriverStrategyStatus.LINEAR_INLINE
    _PAR = _DriverStrategyStatus.PARALLELIZATION
    _ORCH = _DriverStrategyStatus.ORCHESTRATOR_WORKERS
    _HIER = _DriverStrategyStatus.HIERARCHICAL_DELEGATION
    _HAND = _DriverStrategyStatus.DECENTRALIZED_HANDOFF
    _EO = _DriverStrategyStatus.EVALUATOR_OPTIMIZER
    all_strategies = frozenset({_LIN, _PAR, _ORCH, _HIER, _HAND, _EO})

    # (carrier-bearing snapshot, the strategies that READ it). Every other strategy = mismatch.
    cases: list[tuple[PauseSnapshot, frozenset[_DriverStrategyStatus]]] = [
        (_captured_with(fan_out_resume=_a_fan_out()), frozenset({_ORCH, _HIER})),
        (
            _captured_with(peer_fan_out_resume=PeerFanOutResumeState(branches=(), branch_count=1)),
            frozenset({_PAR}),
        ),
        (
            _captured_with(handoff_resume=HandoffResumeState(completed_stages=(), stage_count=1)),
            frozenset({_HAND}),
        ),
        (
            _captured_with(
                evaluator_optimizer_resume=EvaluatorOptimizerResumeState(completed_steps=())
            ),
            frozenset({_EO}),
        ),
        (
            _captured_with(effect_fence_resume=EffectFenceResumeState(idempotency_key="k")),
            frozenset({_LIN}),
        ),
    ]
    for snap, readers in cases:
        for strat in all_strategies:
            diff = _resume_carrier_topology_mismatch(snap, strat)
            if strat in readers:
                assert diff is None, (snap, strat)
            else:
                assert diff is not None and diff.startswith("resume-carrier-topology-mismatch:")

    # A carrier-less snapshot (a plain linear step_index resume) is NEVER a mismatch.
    bare = _captured_with()
    for strat in all_strategies:
        assert _resume_carrier_topology_mismatch(bare, strat) is None

    # The generalization subsumes the synthesis-bearing case (the only one guarded before
    # this arc): a fan-out+synthesis snapshot resumed as PARALLELIZATION still fails closed
    # on carrier-populated alone, independent of the synthesis identity.
    synth_fan = _captured_with(fan_out_resume=_a_fan_out("synthesis"))
    assert _resume_carrier_topology_mismatch(synth_fan, _PAR) is not None
    assert _resume_carrier_topology_mismatch(synth_fan, _ORCH) is None


def test_resume_foreign_carrier_under_parallelization_fails_closed_zero_dispatch() -> None:
    """B-FANOUT-RESUME-CARRIER-TOPOLOGY-MISMATCH integration (by-execution) — resuming the
    PARALLELIZATION strategy against a snapshot that populated a DIFFERENT topology's carrier
    fails closed BEFORE any dispatch (zero branches re-run), proving the guard prevents the
    fresh-run re-dispatch of effect-bearing branches. Covers every foreign (non-peer)
    carrier, incl. the previously-unguarded NON-synthesis fan-out carrier."""
    foreign: list[PauseSnapshot] = [
        _captured_with(fan_out_resume=_a_fan_out()),  # ORCHESTRATOR carrier (non-synthesis)
        _captured_with(handoff_resume=HandoffResumeState(completed_stages=(), stage_count=1)),
        _captured_with(
            evaluator_optimizer_resume=EvaluatorOptimizerResumeState(completed_steps=())
        ),
        _captured_with(effect_fence_resume=EffectFenceResumeState(idempotency_key="k")),
    ]
    for snap in foreign:
        dispatcher = _CountingDispatcher()
        ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
        result = _run(steps=_steps(2), dispatcher=dispatcher, ctx=ctx, pause_snapshot_input=snap)
        assert result.status is RunStatus.FAILED
        assert result.fail_class is not None
        assert result.fail_class.startswith("resume-carrier-topology-mismatch:")
        assert dispatcher.dispatched == []  # at-most-once: nothing re-dispatched fresh


def test_resume_peer_carrier_under_foreign_topology_fails_closed_zero_dispatch() -> None:
    """The reverse angle — a PARALLELIZATION (`peer_fan_out_resume`) snapshot resumed under
    every OTHER strategy fails closed BEFORE any dispatch. Proves the entry guard fires for
    EACH resuming strategy (not only PARALLELIZATION reading a foreign carrier), so the
    generalization is exercised end-to-end for orchestrator / hierarchical / handoff /
    evaluator-optimizer / linear resumes too."""
    peer_snap = _captured_with(
        peer_fan_out_resume=PeerFanOutResumeState(branches=(), branch_count=1)
    )
    for topology in (
        TopologyPattern.ORCHESTRATOR_WORKERS,
        TopologyPattern.HIERARCHICAL_DELEGATION,
        TopologyPattern.DECENTRALIZED_HANDOFF,
        TopologyPattern.EVALUATOR_OPTIMIZER,
        TopologyPattern.SINGLE_THREADED_LINEAR,
    ):
        dispatcher = _CountingDispatcher()
        ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
        result = _run_topology(
            topology=topology,
            steps=_steps(2),
            dispatcher=dispatcher,
            ctx=ctx,
            pause_snapshot_input=peer_snap,
        )
        assert result.status is RunStatus.FAILED, topology
        assert result.fail_class is not None, topology
        assert result.fail_class.startswith("resume-carrier-topology-mismatch:"), topology
        assert dispatcher.dispatched == [], topology


def test_resume_branch_count_mismatch_fails_closed() -> None:
    """Material-diff guard: a snapshot captured with branch_count=3 but resumed
    against a 2-branch body fails CLOSED (the recovered ordinals no longer map to
    these steps — a changed body) rather than re-dispatching a mismatched set."""
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(branches=(), branch_count=3)
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2), dispatcher=_CountingDispatcher(), ctx=ctx, pause_snapshot_input=snapshot
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "branch-count-mismatch" in result.fail_class


def test_resume_branch_identity_mismatch_fails_closed() -> None:
    """A valid (same branch_count) snapshot whose recovered branch `step_id` does
    NOT match the re-supplied body (a branch rename / reorder) fails CLOSED rather
    than silently attributing the recovered output to the wrong step. The hash is
    valid (captured for the renamed id), so this is caught by the in-strategy
    identity guard, not the snapshot_hash."""
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="renamed-branch",  # the body has "branch-0" at index 0
                    terminal_status="completed",
                    output={"stale": True},
                ),
            ),
            branch_count=2,
        )
    )
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(
        steps=_steps(2), dispatcher=_CountingDispatcher(), ctx=ctx, pause_snapshot_input=snapshot
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "branch-identity-mismatch" in result.fail_class


def test_resume_redispatch_failing_branch_re_pauses_with_unioned_branches() -> None:
    """A re-dispatched branch failing AGAIN under `pause` re-PAUSES with a snapshot
    whose `branches` UNION the prior-recovered + this-round-terminal sets. branch-0
    recovered; branch-1 fails on re-dispatch → the new snapshot carries BOTH
    (branch-0's recovered output carried forward + branch-1 newly terminal)."""
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="branch-0",
                    terminal_status="completed",
                    output={"role": "branch-0", "recovered": True},
                ),
            ),  # branch-1 + branch-2 absent → re-dispatchable
            branch_count=3,
        )
    )
    dispatcher = _CountingDispatcher(fail_step_ids={"branch-1"})
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(3), dispatcher=dispatcher, ctx=ctx, pause_snapshot_input=snapshot)

    # A re-dispatched branch failed under pause (protocol bound) → re-PAUSED.
    assert result.status is RunStatus.PAUSED
    new_snap = result.pause_snapshot
    assert new_snap is not None and new_snap.peer_fan_out_resume is not None
    by_index = {b.branch_index: b for b in new_snap.peer_fan_out_resume.branches}
    # UNION: the prior-recovered branch-0 (carried forward, output preserved) +
    # the newly-terminal branch-1 (failed this round).
    assert 0 in by_index and 1 in by_index
    assert by_index[0].output == {"role": "branch-0", "recovered": True}
    assert by_index[1].output is None  # ran-and-errored → no output
    # branch-0 was NOT re-dispatched (terminal-skipped); branch-1 WAS (and failed).
    assert "branch-0" not in dispatcher.dispatched
    assert "branch-1" in dispatcher.dispatched


def test_pause_captures_in_flight_sibling_completed_output() -> None:
    """A sibling IN-FLIGHT when the barrier cancels it (because another branch
    failed) runs to completion under the shield; its successful OUTPUT must be
    captured into the snapshot (else resume skips it as terminal + drops the
    output). branch-0 is mid-dispatch (a brief sleep) when branch-1 fails →
    branch-0 completes under the shield → its output is recovered."""
    import time

    class _InFlightCompletesDispatcher:
        def __init__(self) -> None:
            self._started = threading.Event()

        def dispatch(
            self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            sid = str(step.step_id)
            if sid == "branch-0":
                self._started.set()
                time.sleep(0.05)  # in-flight when branch-1 fails; completes under the shield
                return {"role": "branch-0", "in_flight_completed": True}
            assert self._started.wait(timeout=10.0), "branch-0 never started"
            raise RuntimeError("branch-1 fails while branch-0 is in-flight")

    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=_InFlightCompletesDispatcher(), ctx=ctx)

    assert result.status is RunStatus.PAUSED
    snap = result.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    by_index = {b.branch_index: b for b in snap.peer_fan_out_resume.branches}
    # branch-0 was cancelled-but-completed → terminal `completed` WITH its output
    # captured; without it `output` would be None and resume would drop it.
    assert by_index[0].terminal_status == "completed"
    assert by_index[0].output == {"role": "branch-0", "in_flight_completed": True}


def test_peer_snapshot_hash_byte_identical_backward_compat() -> None:
    """Backward-compat: a snapshot with NO `peer_fan_out_resume` (and no
    `fan_out_resume`) hashes byte-identically to the pre-B-FANOUT-PAUSE formula
    (each key is added to the canonical dict ONLY when present) → existing durable
    snapshots still validate."""
    summary = _pause_context_reader()[0]
    with_field = _compute_snapshot_hash(
        workflow_id="wf",
        run_id="r",
        step_index=0,
        state_summary=summary,
        peer_fan_out_resume=None,
    )
    legacy_canonical_hash = _compute_snapshot_hash(
        workflow_id="wf", run_id="r", step_index=0, state_summary=summary
    )
    assert with_field == legacy_canonical_hash


def test_peer_snapshot_survives_json_roundtrip() -> None:
    """Durable-store fidelity: the peer fan-out snapshot round-trips through
    model_dump(mode="json") → model_validate (the JournalWorkflowPauseStore path)
    with `peer_fan_out_resume` intact AND the hash still valid."""
    snapshot = _captured_snapshot(
        peer_fan_out_resume=PeerFanOutResumeState(
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="branch-0",
                    terminal_status="completed",
                    output={"k": "v"},
                ),
                FanOutBranchResumeState(
                    branch_index=1, step_id="branch-1", terminal_status="timed_out", output=None
                ),
            ),
            branch_count=3,
        )
    )
    restored = PauseSnapshot.model_validate(snapshot.model_dump(mode="json"))
    assert restored == snapshot
    assert restored.peer_fan_out_resume is not None
    assert restored.snapshot_hash == _compute_snapshot_hash(
        workflow_id=restored.workflow_id,
        run_id=restored.run_id,
        step_index=restored.step_index,
        state_summary=restored.state_summary,
        peer_fan_out_resume=restored.peer_fan_out_resume,
    )


# ---------------------------------------------------------------------------
# B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — a peer branch whose OWN dispatch raises the
# runtime effect fence COMPOSES that ambiguous-pause THROUGH the fan-out barrier:
# the run PAUSES with `effect_fence_paused_branches` populated, and resume re-enters
# the branch with the operator's key-bound `EffectFenceResolution`. The real-fence
# witness is the REAL `_execute_parallelization` TaskGroup+shield concurrency
# machinery; the error is name-matched (harness-cp cannot import harness-runtime, the
# same test-local pattern as the linear `test_workflow_driver_effect_fence_pause.py`).
# ---------------------------------------------------------------------------


class EffectFenceAmbiguousUncommittedError(Exception):
    """Test-local stand-in for the runtime `effect_fence.EffectFenceAmbiguousUncommittedError`
    (C-RT-31 §14.22). The driver name-matches `type(exc).__name__` (harness-cp cannot import
    harness-runtime), so a same-named local class with the `idempotency_key` attribute is the
    faithful CP-side witness — exercised through the REAL fan-out concurrency machinery."""

    def __init__(self, *, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"ambiguous (key={idempotency_key!r})")


class _FenceAmbiguousBranchDispatcher:
    """Deterministic all-terminal-or-fence-paused peer fan-out: branch-0 completes cleanly
    and sets a gate; branch-1 waits on that gate THEN raises the effect-fence ambiguous error.
    So branch-0 reaches a terminal `completed`+output BEFORE branch-1's fence-pause halts the
    barrier (no not-yet-dispatched / cancelled branch, no timing race). On RESUME (branch-0
    terminal-skipped) it records each dispatched step's threaded `effect_fence_resolution`."""

    def __init__(self, *, fence_key: str = "fence-key-branch-1") -> None:
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
        if step_id == "branch-0":
            self._gate.set()
            return {"role": "branch-0", "echoed": dict(step.step_payload)}
        # branch-1: wait until branch-0 has completed, then raise the fence-ambiguous error.
        assert self._gate.wait(timeout=10.0), "branch-0 never completed"
        raise EffectFenceAmbiguousUncommittedError(idempotency_key=self._fence_key)


class _HolderWithResolution:
    """Stand-in `ResumeContextHolder` — `peek()` returns a ResumeContext carrying the
    operator's effect-fence resolution (NON-consuming, the production peek contract)."""

    def __init__(self, resolution: EffectFenceResolution) -> None:
        self._rc = ResumeContext(effect_fence_resolution=resolution)
        self.peeked = 0

    def peek(self) -> ResumeContext:
        self.peeked += 1
        return self._rc


class _ResumeRecordingDispatcher:
    """Resume-side recording dispatcher: records each dispatched step's threaded
    `step_context.effect_fence_resolution` then SUCCEEDS (no gate, no raise) — to witness
    that the driver THREADS the key-bound directive onto the re-entered fence-paused branch."""

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


def test_peer_branch_effect_fence_ambiguous_composes_through_barrier_to_pause() -> None:
    """REAL-FENCE WITNESS (PAUSE half): a peer branch whose OWN dispatch raises the
    effect-fence ambiguous error does NOT become a `completed` cascade branch — it
    composes through the REAL `_execute_parallelization` TaskGroup+shield to a genuine
    PAUSE carrying `effect_fence_paused_branches` (branch-1 + its held reserve key),
    DISJOINT from the terminal `branches` (branch-0 recovered). Proves the name-matched
    catch fires through the concurrency machinery (not ExceptionGroup-swallowed)."""
    ctx = cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter()))
    result = _run(steps=_steps(2), dispatcher=_FenceAmbiguousBranchDispatcher(), ctx=ctx)

    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    # Labeled EFFECT_FENCE_AMBIGUOUS so the operator surface knows to supply a resolution.
    assert snap.pause_reason is WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS
    pr = snap.peer_fan_out_resume
    assert pr is not None
    assert pr.branch_count == 2
    # branch-1 is the disjoint effect-fence-paused disposition (NOT a terminal branch).
    assert {b.branch_index for b in pr.branches} == {0}  # only branch-0 terminal
    efp = pr.effect_fence_paused_branches
    assert len(efp) == 1
    assert efp[0] == EffectFencePausedBranchResumeState(
        branch_index=1,
        step_id="branch-1",
        step_kind="declarative-step",
        idempotency_key="fence-key-branch-1",
    )
    # The snapshot is hash-valid (the carrier rides the snapshot hash, dropped-when-empty).
    restored = PauseSnapshot.model_validate(snap.model_dump(mode="json"))
    assert restored == snap


def test_peer_branch_effect_fence_resume_threads_key_bound_resolution() -> None:
    """REAL-FENCE WITNESS (resume half): resuming an effect-fence-paused peer fan-out
    re-enters ONLY the fence-paused branch (branch-0 terminal-skipped), threading the
    operator's `EffectFenceResolution` key-bound to THAT branch's held reserve. The
    dispatcher APPLYING the resolution (RE_FIRE/SKIP/ABORT) is proven by the runtime
    `test_effect_fence.py` witnesses; this is the CP producer half."""
    # First: pause at branch-1, populating the carrier with the key.
    paused = _run(
        steps=_steps(2),
        dispatcher=_FenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    efp = snap.peer_fan_out_resume.effect_fence_paused_branches
    assert len(efp) == 1
    key = efp[0].idempotency_key

    # Resume: a holder carrying SKIP_AS_FIRED + a recording dispatcher; branch-1 re-dispatched.
    holder = _HolderWithResolution(EffectFenceResolution.SKIP_AS_FIRED)
    rec = _ResumeRecordingDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.SUCCESS
    # branch-0 terminal-skipped on resume; only branch-1 re-dispatched WITH the directive.
    assert "branch-0" not in rec.dispatched
    assert "branch-1" in rec.dispatched
    threaded = rec.seen_resolution["branch-1"]
    assert threaded is not None
    assert threaded.resolution is EffectFenceResolution.SKIP_AS_FIRED
    assert threaded.idempotency_key == key
    assert holder.peeked >= 1  # the holder was PEEKED (non-consuming), not consumed


class EffectFenceAbortedError(Exception):
    """Test-local stand-in for the runtime `effect_fence.EffectFenceAbortedError` — raised when
    the operator resolved an effect-fence pause with ABORT and the tool dispatcher applies it."""


class _AbortOnResolutionDispatcher:
    """Resume-side dispatcher that RAISES the test-local `EffectFenceAbortedError` when it sees an
    ABORT directive threaded on the re-entered branch (simulating the runtime fence applying the
    operator's ABORT) — so the driver's ABORT → terminal FAILED routing is witnessed."""

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


def test_peer_branch_effect_fence_resume_abort_is_terminal_failed_not_repause() -> None:
    """Codex [P1] regression: resuming an effect-fence-paused peer fan-out with an ABORT
    resolution yields a TERMINAL `RunStatus.FAILED` (the operator gave up), NOT a re-pause —
    even on the TEAM (cascade_policy=pause) tier where an ordinary branch failure WOULD pause."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_FenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None

    holder = _HolderWithResolution(EffectFenceResolution.ABORT)
    rec = _AbortOnResolutionDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED
    assert "parallelization-effect-fence-aborted" in (result.fail_class or "")
    assert result.pause_snapshot is None  # terminal — NOT a re-pause
    assert "branch-1" in rec.dispatched  # the aborted branch DID re-dispatch


def test_peer_branch_effect_fence_resume_changed_kind_fails_closed() -> None:
    """Codex [P1] R2 regression: an effect-fence-paused peer captured at one kind, then re-supplied
    at the SAME step_id but a CHANGED step_kind on resume, FAILS CLOSED — threading the resolution
    into a different-kind dispatcher would not reach the tool fence (the original effect would be
    silently abandoned). The live-pause analogue of the crash-resume changed-kind guard."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_FenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None

    # Resume with branch-1 CHANGED from declarative-step → inference-step (same step_id).
    changed = [
        WorkflowStep(
            step_id=StepID("branch-0"),
            step_kind=StepKind.DECLARATIVE_STEP,
            step_payload={"index": 0},
        ),
        WorkflowStep(
            step_id=StepID("branch-1"), step_kind=StepKind.INFERENCE_STEP, step_payload={"index": 1}
        ),
    ]
    holder = _HolderWithResolution(EffectFenceResolution.SKIP_AS_FIRED)
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=changed,
        dispatcher=_ResumeRecordingDispatcher(),
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED
    assert "effect-fence-paused-kind-changed" in (result.fail_class or "")


def test_peer_effect_fence_resume_under_proceed_tier_fails_closed() -> None:
    """Codex [P2] R3 regression: an effect-fence pause captured under a strict (pause) tier, then
    RESUMED under a manifest/persona that now resolves to CascadePolicy.PROCEED, FAILS CLOSED — the
    PROCEED path has no pause/resolution handling, so honoring the resume there would degrade the
    operator's ABORT / the at-most-once re-pause to a silent PARTIAL. The fence resume requires a
    strict tier."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_FenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None

    holder = _HolderWithResolution(EffectFenceResolution.SKIP_AS_FIRED)
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=_ResumeRecordingDispatcher(),
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
        persona_tier=PersonaTier.SOLO_DEVELOPER,  # → CascadePolicy.PROCEED
    )

    assert result.status is RunStatus.FAILED
    assert "effect-fence-resume-requires-strict-tier" in (result.fail_class or "")
    assert result.pause_snapshot is None  # NOT a silent PARTIAL


# ---------------------------------------------------------------------------
# B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION (PARALLELIZATION) — two peers fence-pause in
# one barrier; resume resolves them DIFFERENTLY via the `effect_fence_resolutions` per-key map.
# The symmetric witness of the ORCHESTRATOR_WORKERS case (same shared resolver, peer site).
# ---------------------------------------------------------------------------


class _TwoFenceAmbiguousBranchDispatcher:
    """Both peers raise the effect-fence ambiguous error with DISTINCT keys, synchronized on a
    barrier so BOTH are in-flight before either raises → TWO `effect_fence_paused_branches` in
    one pause (the per-branch-distinct precondition)."""

    def __init__(self) -> None:
        self._barrier = threading.Barrier(2, timeout=10.0)
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        self._barrier.wait()
        raise EffectFenceAmbiguousUncommittedError(idempotency_key=f"fence-key-{step_id}")


class _HolderWithResolutions:
    """Stand-in holder whose `peek()` returns a ResumeContext carrying a per-key
    `effect_fence_resolutions` map (B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION)."""

    def __init__(self, resolutions: dict[str, EffectFenceResolution]) -> None:
        self._rc = ResumeContext(effect_fence_resolutions=resolutions)
        self.peeked = 0

    def peek(self) -> ResumeContext:
        self.peeked += 1
        return self._rc


def test_peer_per_branch_distinct_resolutions() -> None:
    """REAL-FENCE WITNESS (PARALLELIZATION, per-branch-DISTINCT): two peers fence-pause in one
    barrier; resume resolves branch-0 SKIP_AS_FIRED + branch-1 RE_FIRE via the per-key
    `effect_fence_resolutions` map — each peer re-dispatched through the REAL
    `_execute_parallelization` with ITS OWN key-bound resolution."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    assert paused.status is RunStatus.PAUSED
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    efp = snap.peer_fan_out_resume.effect_fence_paused_branches
    assert len(efp) == 2
    key_by_index = {b.branch_index: b.idempotency_key for b in efp}

    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.SKIP_AS_FIRED,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _ResumeRecordingDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.SUCCESS
    b0 = rec.seen_resolution["branch-0"]
    b1 = rec.seen_resolution["branch-1"]
    assert b0 is not None and b0.resolution is EffectFenceResolution.SKIP_AS_FIRED
    assert b0.idempotency_key == key_by_index[0]
    assert b1 is not None and b1.resolution is EffectFenceResolution.RE_FIRE
    assert b1.idempotency_key == key_by_index[1]


class _PeerAbortGuardDispatcher:
    """Resume-side abort-guard witness (PARALLELIZATION): an ABORT directive raises
    EffectFenceAbortedError; a RE_FIRE / SKIP directive FIRES (records in `fired`); a None directive
    (a suppressed sibling) RE-RAISES the ambiguous fence (re-pause, no fire)."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.fired: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        directive = getattr(step_context, "effect_fence_resolution", None)
        if directive is None:
            raise EffectFenceAmbiguousUncommittedError(idempotency_key=f"fence-key-{step_id}")
        if directive.resolution is EffectFenceResolution.ABORT:
            raise EffectFenceAbortedError(f"operator aborted {step_id}")
        self.fired.append(step_id)
        return {"role": step_id, "echoed": dict(step.step_payload)}


def test_peer_mixed_abort_map_suppresses_sibling_refire() -> None:
    """Codex [P1] (PARALLELIZATION): a mixed map {branch-0: ABORT, branch-1: RE_FIRE} must NOT fire
    the RE_FIRE sibling before the ABORT fails the run — ABORT stays run-level-terminal. The RE_FIRE
    sibling's directive is SUPPRESSED (re-pauses INERT, no fire); the run FAILs."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    key_by_index = {
        b.branch_index: b.idempotency_key
        for b in snap.peer_fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _PeerAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED
    assert "parallelization-effect-fence-aborted" in (result.fail_class or "")
    assert result.pause_snapshot is None
    assert "branch-1" not in rec.fired  # the RE_FIRE sibling was SUPPRESSED — did NOT fire


# ---------------------------------------------------------------------------
# B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (PARALLELIZATION) — the per-branch-SCOPED abort
# (`ABORT_BRANCH`): fail JUST one peer, let the vouched-for siblings FIRE, fold survivors per
# cascade_policy. The exact inverse of the run-level `ABORT` (which suppresses ALL siblings).
# ---------------------------------------------------------------------------


def test_peer_scoped_abort_fires_vouched_sibling() -> None:
    """CRUX contrasting baseline (the inverse of test_peer_mixed_abort_map_suppresses_sibling_refire):
    a mixed map {branch-0: ABORT_BRANCH, branch-1: RE_FIRE} fails JUST branch-0 (never re-dispatched
    → at-most-once: its ambiguous effect is never re-fired) and FIRES the vouched-for RE_FIRE sibling
    → the run folds the survivor → PARTIAL (NOT the run-FAILED that run-level ABORT forces)."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    key_by_index = {
        b.branch_index: b.idempotency_key
        for b in snap.peer_fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _PeerAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.PARTIAL  # survivor folded, NOT the run-level-ABORT FAILED
    # PARTIAL carries fail_class=None like every degraded PARTIAL (the aborted peer is a degraded
    # terminal non-contributor; run-result provenance is FAILED-only — see the all-abort test).
    assert result.fail_class is None
    assert "branch-1" in rec.fired  # the vouched-for RE_FIRE sibling FIRED (NOT suppressed)
    assert "branch-0" not in rec.dispatched  # the scoped-abort peer was NEVER re-dispatched


def test_peer_all_scoped_abort_fails_not_vacuous_partial() -> None:
    """All-abort guard (advisor watchpoint #1): when EVERY fence-paused peer is scoped-aborted there
    is NO surviving contributor → the run is FAILED, NOT the vacuous PARTIAL the degraded check would
    otherwise return with zero survivors. Neither peer is re-dispatched."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    key_by_index = {
        b.branch_index: b.idempotency_key
        for b in snap.peer_fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.ABORT_BRANCH,
        }
    )
    rec = _PeerAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED  # NO survivor → FAILED, not a vacuous PARTIAL
    assert "parallelization-effect-fence-branch-aborted" in (result.fail_class or "")
    assert rec.dispatched == []  # neither scoped-abort peer was re-dispatched


def test_peer_scoped_abort_iterative_repause() -> None:
    """Iterative re-pause (advisor watchpoint #4): a map answering ONLY branch-0 (ABORT_BRANCH) while
    branch-1 is left unresolved → branch-0 finalizes as a TERMINAL branch (next resume SKIPS it) and
    branch-1 re-pauses INERT (carried forward as still-fence-paused) — the operator can resolve the
    rest in a later resume."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    key_by_index = {
        b.branch_index: b.idempotency_key
        for b in snap.peer_fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions({key_by_index[0]: EffectFenceResolution.ABORT_BRANCH})
    rec = _PeerAbortGuardDispatcher()
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
    assert snap2 is not None and snap2.peer_fan_out_resume is not None
    # branch-0 (scoped-aborted) is now a TERMINAL branch — a later resume SKIPS it (never re-fires).
    assert 0 in {b.branch_index for b in snap2.peer_fan_out_resume.branches}
    # branch-1 (unresolved) re-paused INERT — still fence-paused, carried forward.
    assert {b.branch_index for b in snap2.peer_fan_out_resume.effect_fence_paused_branches} == {1}
    assert "branch-0" not in rec.dispatched  # the scoped-abort peer was NEVER re-dispatched


def test_peer_mixed_run_abort_and_scoped_abort_deterministic() -> None:
    """advisor [P1] (precedence): a mixed map {branch-0: ABORT, branch-1: ABORT_BRANCH} — run-level
    ABORT dominates (the run FAILs), but the scoped-abort branch-1 MUST be recorded DETERMINISTICALLY
    (excluded from re-dispatch), NOT nulled by the run-level-ABORT suppression and re-dispatched into
    the ABORT race. Witnesses the interception-BEFORE-suppression ordering: branch-1 is NEVER
    dispatched (before the fix it re-dispatched with a None directive)."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    key_by_index = {
        b.branch_index: b.idempotency_key
        for b in snap.peer_fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT,
            key_by_index[1]: EffectFenceResolution.ABORT_BRANCH,
        }
    )
    rec = _PeerAbortGuardDispatcher()
    ctx_obj = _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(
        steps=_steps(2),
        dispatcher=rec,
        ctx=cast(DriverContext, ctx_obj),
        pause_snapshot_input=snap,
    )

    assert result.status is RunStatus.FAILED  # run-level ABORT dominates
    assert "parallelization-effect-fence-aborted" in (result.fail_class or "")
    assert "branch-0" in rec.dispatched  # the ABORT branch re-dispatched → raised → FAILED
    assert "branch-1" not in rec.dispatched  # the scoped-abort branch deterministically EXCLUDED


def test_peer_scoped_abort_under_cascade_cancel_fails() -> None:
    """Codex [P1] (CASCADE_CANCEL tier): a scoped-abort resumed under MULTI_TENANT_COMPLIANCE
    (CascadePolicy.CASCADE_CANCEL) must FAIL — NOT a SUCCESS hiding the aborted branch. Per-branch
    isolation is incompatible with cascade-cancel-everything; the cascade-cancel block returns before
    the §25.15.1 degraded fold, so the scoped-abort guard must fire on this tier too."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    key_by_index = {
        b.branch_index: b.idempotency_key
        for b in snap.peer_fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions(
        {
            key_by_index[0]: EffectFenceResolution.ABORT_BRANCH,
            key_by_index[1]: EffectFenceResolution.RE_FIRE,
        }
    )
    rec = _PeerAbortGuardDispatcher()
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
    assert "parallelization-effect-fence-branch-aborted" in (result.fail_class or "")


def test_peer_scoped_abort_under_proceed_rejected_requires_strict_tier() -> None:
    """Codex [P2] (PROCEED tier): an effect-fence pause resumed under SOLO_DEVELOPER
    (CascadePolicy.PROCEED) is rejected FAIL-CLOSED with `...-requires-strict-tier` — and the
    scoped-abort durable recording is SKIPPED (it would otherwise persist a `completed` terminal for
    a resume that is then rejected → corrupt state). Fail-closed precedes durable writes."""
    paused = _run(
        steps=_steps(2),
        dispatcher=_TwoFenceAmbiguousBranchDispatcher(),
        ctx=cast(DriverContext, _CtxP(ledger=_RecordingLedger(), emitter=_Emitter())),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.peer_fan_out_resume is not None
    key_by_index = {
        b.branch_index: b.idempotency_key
        for b in snap.peer_fan_out_resume.effect_fence_paused_branches
    }
    holder = _HolderWithResolutions({key_by_index[0]: EffectFenceResolution.ABORT_BRANCH})
    rec = _PeerAbortGuardDispatcher()
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
    assert "effect-fence-resume-requires-strict-tier" in (result.fail_class or "")
    assert "branch-0" not in rec.dispatched  # no dispatch (rejected before the barrier)
