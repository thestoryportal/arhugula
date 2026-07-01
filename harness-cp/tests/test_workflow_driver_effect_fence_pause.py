"""B-EFFECT-FENCE-HITL-ROUTE (R-FS-1) — the driver routes the runtime effect-fence
ambiguous signal to a §26.2 EFFECT_FENCE_AMBIGUOUS PAUSE (protocol bound) / FAILED
(unbound).

The runtime effect fence (§14.22) raises `EffectFenceAmbiguousUncommittedError` at the
tool sink when a lost-reserve re-dispatch finds NO captured output (a crash in the
fire→capture window). The workflow driver name-matches it (harness-cp cannot import
harness-runtime per the workspace dependency graph, mirroring HITLPauseRequestedSignal)
at the LINEAR / TOOL_STEP step-dispatch boundary and routes it:

  * bound `pause_resume_protocol` → RunStatus.PAUSED, pause_reason EFFECT_FENCE_AMBIGUOUS,
    fail_class None, a hash-valid PauseSnapshot (resumable);
  * unbound → RunStatus.FAILED (behaviorally equivalent to the pre-v1.72 fail-closed:
    FAILED, NO auto-re-fire — only the fail_class STRING differs, owned here).

The "no auto-re-fire" at-most-once half is the dispatcher-level witness in
test_effect_fence.py (suppress vs ambiguous-raise); this is the driver-level routing half.
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
from harness_cp.pause_resume_protocol import (
    PauseResumeProtocol,
    PauseResumeProtocolEventKind,
    _compute_snapshot_hash,
)
from harness_cp.pause_resume_protocol_types import (
    EffectFenceResolution,
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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-effect-fence-pause")
_ANCHOR = "0" * 64  # constant MVP pause-context anchor (no material diff on resume)
_WF = "wf-ef-pause"


class EffectFenceAmbiguousUncommittedError(Exception):
    """A local stand-in for the runtime exception the fence raises — the driver
    name-matches on `type(exc).__name__`, so a same-named class triggers the
    branch without importing harness-runtime (the very dependency-graph constraint
    the production name-match exists for). Carries `idempotency_key` like the real
    one so the driver's carrier-population (`effect_fence_resume`) is witnessed."""

    def __init__(self, message: str = "", *, idempotency_key: str = "") -> None:
        self.idempotency_key = idempotency_key
        super().__init__(message or "effect-fence: ambiguous (no captured output)")


class EffectFenceAbortedError(Exception):
    """Local stand-in for the runtime ABORT-resolution error. The driver does NOT
    name-match it (it falls through to the generic FAILED mapping), so this just
    needs to be a same-named non-transient Exception to witness ABORT → FAILED."""

    def __init__(self, *, idempotency_key: str = "") -> None:
        self.idempotency_key = idempotency_key
        super().__init__("effect-fence: operator ABORT")


def _manifest() -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=_WF,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _step(name: str) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(name),
        step_kind=StepKind.TOOL_STEP,
        step_payload={"tool_id": "do_effect", "tool_args": {"message": name}},
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


class _Ctx:
    """Driver context; `pause_resume_protocol` bound iff `with_protocol`."""

    def __init__(self, *, ledger: Any, emitter: _Emitter, with_protocol: bool) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = emitter
        self.drained_flag = asyncio.Event()
        self.pause_requested_flag = asyncio.Event()
        self.pause_resume_protocol = _protocol() if with_protocol else None
        self.ledger_reader = None
        self.tracer_provider = NoOpTracerProvider()
        self.validator_framework = None
        self.tenant_id = None
        self.inter_step_output_channel = None


class _Registry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.TOOL_STEP:
            return self._dispatcher
        raise StepKindDispatcherNotBoundError(step_kind)


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _Registry(dispatcher))


class _FenceAmbiguousDispatcher:
    """Succeeds on every step except `raise_on`, which raises the (name-matched)
    effect-fence ambiguous error — modeling a lost-reserve re-dispatch whose prior
    attempt captured no output. Records dispatched step_ids (the no-extra-dispatch
    witness)."""

    def __init__(self, *, raise_on: str) -> None:
        self._raise_on = raise_on
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id == self._raise_on:
            raise EffectFenceAmbiguousUncommittedError(
                "effect-fence: reserved + no captured output (ambiguous)",
                idempotency_key=f"fence-key-{step_id}",
            )
        return {"tool_id": "do_effect", "response": {"echoed": step_id}}


def _run(
    *, dispatcher: StepDispatcher, ctx: DriverContext, pause_snapshot_input: Any = None
) -> Any:
    return execute_workflow(
        _manifest(),
        [_step("s0"), _step("s1")],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
        pause_snapshot_input=pause_snapshot_input,
    )


def test_ambiguous_with_protocol_returns_paused_with_effect_fence_reason() -> None:
    """Bound protocol: the ambiguous signal at s1 (after s0 completes) routes to a
    resumable §26.2 EFFECT_FENCE_AMBIGUOUS PAUSE — NOT FAILED, and (the at-most-once
    half) the dispatcher is not re-invoked beyond the failing dispatch."""
    dispatcher = _FenceAmbiguousDispatcher(raise_on="s1")
    ctx = cast(
        DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=True)
    )
    result = _run(dispatcher=dispatcher, ctx=ctx)

    assert result.status is RunStatus.PAUSED
    assert result.fail_class is None
    snap = result.pause_snapshot
    assert snap is not None
    assert snap.pause_reason is WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS
    # Paused AT s1 (index 1); the completed prefix is s0 (index 0).
    assert snap.step_index == 1
    assert result.terminal_step_index == 0
    # B-EFFECT-FENCE-PAUSE-RESOLUTION — the driver populates the effect-fence resume
    # carrier from the runtime error's idempotency_key, so a later resume can key-bind
    # the operator's resolution to THIS held reserve.
    assert snap.effect_fence_resume is not None
    assert snap.effect_fence_resume.idempotency_key == "fence-key-s1"
    # Hash-valid (resumable — the U-CP-64 resume guard recomputes this), and the hash
    # COVERS the effect_fence_resume carrier (a tampered key fails the resume recompute).
    assert snap.snapshot_hash == _compute_snapshot_hash(
        workflow_id=snap.workflow_id,
        run_id=snap.run_id,
        step_index=snap.step_index,
        state_summary=snap.state_summary,
        effect_fence_resume=snap.effect_fence_resume,
    )
    # No re-dispatch: s0 + the single failing s1 dispatch only.
    assert dispatcher.dispatched == ["s0", "s1"]


def test_ambiguous_without_protocol_returns_failed_not_paused() -> None:
    """Unbound protocol (the conservative opt-out default): the ambiguous signal
    falls through to FAILED — behaviorally equivalent to the pre-v1.72 fail-closed
    (FAILED, no auto-re-fire). Only the fail_class STRING differs (owned here): it
    names the effect-fence ambiguous error, not the old reserved-uncommitted one."""
    dispatcher = _FenceAmbiguousDispatcher(raise_on="s1")
    ctx = cast(
        DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=False)
    )
    result = _run(dispatcher=dispatcher, ctx=ctx)

    assert result.status is RunStatus.FAILED
    assert result.pause_snapshot is None
    assert result.fail_class is not None
    assert "EffectFenceAmbiguousUncommittedError" in result.fail_class
    assert result.terminal_step_index == 1
    # Still no auto-re-fire — the failing dispatch is the last invocation.
    assert dispatcher.dispatched == ["s0", "s1"]


class _RecordingCpIsWiring:
    """Records the PAUSE_CAPTURED CP→IS emission the driver fires alongside the PAUSE
    (production binds this via the U-RT-111 wiring; the default `_Ctx` leaves it absent
    so the emission is opt-in)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, int]] = []

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
        self.calls.append((protocol_event_kind, event_sequence_id))


def test_ambiguous_pause_emits_cp_is_pause_captured_with_effect_fence_event_kind() -> None:
    """When `cp_is_wiring` IS bound (the production path), the ambiguous PAUSE also
    fires a `PAUSE_CAPTURED` CP→IS audit entry with `event_sequence_id = (step_index
    << 2) | 3` — the effect-fence disambiguator (=3), distinct from the drain-flag (=1)
    and HITL-signal (=2) paths at the same step_index."""
    wiring = _RecordingCpIsWiring()
    ctx_obj = _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=True)
    ctx_obj.cp_is_wiring = wiring  # type: ignore[attr-defined]
    result = _run(
        dispatcher=_FenceAmbiguousDispatcher(raise_on="s1"), ctx=cast(DriverContext, ctx_obj)
    )

    assert result.status is RunStatus.PAUSED
    # s1 is step_index 1 → (1 << 2) | 3 == 7; kind PAUSE_CAPTURED.
    assert wiring.calls == [(PauseResumeProtocolEventKind.PAUSE_CAPTURED, (1 << 2) | 3)]


def test_ambiguous_pause_resume_re_pauses_until_resolution_follow_on() -> None:
    """INTERIM (documented, not a silent surprise): the resume-side RESOLUTION of an
    ambiguous effect-fence pause — clear-claim-to-re-fire / skip-as-fired / abort — is
    the registered follow-on `B-EFFECT-FENCE-PAUSE-RESOLUTION`. Until it lands, a naive
    `api.resume` re-enters the same step with the fence claim still held + still no
    captured output → the dispatcher re-raises → an identical re-PAUSE (same reason,
    valid snapshot). This is INERT, not a busy-loop: resume is caller-initiated
    (`api.resume` / `execute_workflow(pause_snapshot_input=...)`), never auto-driven —
    the paused run sits until a caller resumes. This arc ships suppress-and-continue +
    the captured, operator-SURFACED pause; the operator-side RESOLUTION is the follow-on."""
    paused = _run(
        dispatcher=_FenceAmbiguousDispatcher(raise_on="s1"),
        ctx=cast(
            DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=True)
        ),
    )
    assert paused.status is RunStatus.PAUSED
    snap = paused.pause_snapshot
    assert snap is not None

    # Naive resume with the same still-ambiguous dispatcher → identical re-pause.
    repaused = _run(
        dispatcher=_FenceAmbiguousDispatcher(raise_on="s1"),
        ctx=cast(
            DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=True)
        ),
        pause_snapshot_input=snap,
    )
    assert repaused.status is RunStatus.PAUSED
    assert repaused.pause_snapshot is not None
    assert repaused.pause_snapshot.pause_reason is WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS


# ---------- B-EFFECT-FENCE-PAUSE-RESOLUTION driver witnesses ----------------


class _FenceAbortDispatcher:
    """Raises the (name-unmatched) ABORT error at `raise_on` — modeling the dispatcher
    after an operator ABORT resolution. Records dispatched step_ids."""

    def __init__(self, *, raise_on: str) -> None:
        self._raise_on = raise_on
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.dispatched.append(step_id)
        if step_id == self._raise_on:
            raise EffectFenceAbortedError(idempotency_key=f"fence-key-{step_id}")
        return {"tool_id": "do_effect", "response": {"echoed": step_id}}


def test_abort_resolution_maps_to_failed_not_paused() -> None:
    """ABORT: the dispatcher raises `EffectFenceAbortedError` (after the operator
    resolved ABORT). The driver does NOT route it to a PAUSE — it falls through to the
    generic FAILED mapping (terminal; never re-fire), even with a bound protocol."""
    dispatcher = _FenceAbortDispatcher(raise_on="s1")
    ctx = cast(
        DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=True)
    )
    result = _run(dispatcher=dispatcher, ctx=ctx)

    assert result.status is RunStatus.FAILED
    assert result.pause_snapshot is None
    assert result.fail_class is not None
    assert "EffectFenceAbortedError" in result.fail_class
    assert dispatcher.dispatched == ["s0", "s1"]  # no auto-re-fire


class _HolderWithResolution:
    """Stand-in `ResumeContextHolder` — `peek()` returns a ResumeContext carrying the
    operator's effect-fence resolution (NON-consuming, the production peek contract)."""

    def __init__(self, resolution: EffectFenceResolution) -> None:
        self._rc = ResumeContext(effect_fence_resolution=resolution)
        self.peeked = 0

    def peek(self) -> ResumeContext:
        self.peeked += 1
        return self._rc


class _RecordingResolutionDispatcher:
    """Records the `step_context.effect_fence_resolution` each dispatch received, then
    SUCCEEDS — to witness that the driver THREADS the key-bound directive onto the
    resumed step (the producer half of the full chain; the dispatcher APPLYING it is
    proven by the runtime witnesses)."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, Any]] = []

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        step_id = str(step.step_id)
        self.seen.append((step_id, getattr(step_context, "effect_fence_resolution", None)))
        return {"tool_id": "do_effect", "response": {"echoed": step_id}}


def test_resume_threads_key_bound_resolution_to_resumed_step() -> None:
    """Full-chain producer half: on resume of an effect-fence pause, the driver PEEKS
    the holder (non-consuming) + key-binds the operator's resolution to the snapshot's
    `effect_fence_resume.idempotency_key` + threads it onto the RESUMED step's context
    ONLY. (The dispatcher applying it — RE_FIRE/SKIP/ABORT — is proven by the
    test_effect_fence.py runtime witnesses.)"""
    # First: pause at s1, populating the carrier with the key.
    paused = _run(
        dispatcher=_FenceAmbiguousDispatcher(raise_on="s1"),
        ctx=cast(
            DriverContext, _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=True)
        ),
    )
    snap = paused.pause_snapshot
    assert snap is not None and snap.effect_fence_resume is not None
    key = snap.effect_fence_resume.idempotency_key

    # Resume: a holder carrying RE_FIRE + a recording dispatcher.
    holder = _HolderWithResolution(EffectFenceResolution.RE_FIRE)
    rec = _RecordingResolutionDispatcher()
    ctx_obj = _Ctx(ledger=_RecordingLedger(), emitter=_Emitter(), with_protocol=True)
    ctx_obj.resume_context_holder = holder  # type: ignore[attr-defined]
    result = _run(dispatcher=rec, ctx=cast(DriverContext, ctx_obj), pause_snapshot_input=snap)

    assert result.status is RunStatus.SUCCESS
    # Resume re-entered at s1; the directive was threaded onto THAT step only, key-bound.
    assert len(rec.seen) == 1  # only the resumed step (s1) dispatched
    seen_step_id, threaded = rec.seen[0]
    assert seen_step_id == "s1"
    assert threaded is not None
    assert threaded.resolution is EffectFenceResolution.RE_FIRE
    assert threaded.idempotency_key == key
    assert holder.peeked == 1  # peeked (not consumed) — HITL composer's one-shot intact
