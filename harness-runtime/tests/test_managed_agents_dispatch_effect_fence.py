"""B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL (R-FS-1) — effect-fence witnesses
for the MANAGED_AGENTS vendor-session dispatch.

The §14.20 managed-agents dispatch performs an UNFENCED vendor-session effect
(create_session + send_event). Until this arc a maybe-ran fan-out MANAGED_AGENTS worker
re-dispatched on a strict-tier crash-resume created a SECOND billable session. The arc
wraps the dispatch in the §14.22 effect fence keyed on (parent_idempotency_key, step_id).

These are DISPATCHER-LEVEL witnesses through the REAL `RuntimeEffectFence` (on-disk
O_EXCL claims under tmp) + a COUNTING fake vendor client (per the #746 lesson — fake the
vendor PORT, not the dispatcher), asserting `create_session` is called AT MOST ONCE across
a crash-resume, plus the full disposition matrix (non-success / poll-exhausted leave the
fence in the ambiguous state; SKIP_AS_FIRED / ABORT / RE_FIRE resolutions; payload-
independent agent-swap suppression; durability across a fresh fence instance).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import StepID
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.pause_resume_protocol_types import (
    EffectFenceResolution,
    EffectFenceResolutionDirective,
)
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.effect_fence import (
    EffectFenceAbortedError,
    EffectFenceAmbiguousUncommittedError,
    RuntimeEffectFence,
)
from harness_runtime.lifecycle.managed_agents import (
    ManagedAgentEvent,
    ManagedAgentSession,
    ManagedAgentSessionStatus,
)
from harness_runtime.lifecycle.managed_agents_dispatch import (
    ManagedAgentsSessionError,
    ManagedAgentsStepDispatcher,
    _compose_managed_agents_idempotency_key,
)
from opentelemetry.sdk.trace import TracerProvider


class _CountingClient:
    """Counting fake `ManagedAgentsClientProtocol` — records every create_session /
    send_event / cancel_session so a test can assert at-most-once at the vendor sink."""

    def __init__(
        self,
        *,
        retrieve_statuses: list[ManagedAgentSessionStatus],
        cancel_raises: bool = False,
    ) -> None:
        self._statuses = retrieve_statuses
        self._retrieve_calls = 0
        self._cancel_raises = cancel_raises
        self.created: list[tuple[str, str]] = []
        self.sent: list[ManagedAgentEvent] = []
        self.cancelled: list[str] = []

    async def create_session(
        self,
        *,
        agent_id: str,
        environment_id: str,
        title: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ManagedAgentSession:
        _ = (title, metadata)
        self.created.append((agent_id, environment_id))
        return ManagedAgentSession(
            session_id="session_test",
            agent_id=agent_id,
            environment_id=environment_id,
            status=ManagedAgentSessionStatus.CREATED,
            runtime_ms=0,
            billable_seconds=0.0,
        )

    async def send_event(self, *, session_id: str, event: ManagedAgentEvent) -> ManagedAgentEvent:
        _ = session_id
        self.sent.append(event)
        return event

    async def retrieve_session(self, *, session_id: str) -> ManagedAgentSession:
        idx = min(self._retrieve_calls, len(self._statuses) - 1)
        self._retrieve_calls += 1
        return ManagedAgentSession(
            session_id=session_id,
            agent_id="agent_test",
            environment_id="env_test",
            status=self._statuses[idx],
            runtime_ms=1250,
            billable_seconds=1.25,
        )

    async def cancel_session(self, *, session_id: str) -> ManagedAgentSession:
        self.cancelled.append(session_id)
        if self._cancel_raises:
            raise RuntimeError("vendor cancel failed")
        return ManagedAgentSession(
            session_id=session_id,
            agent_id="agent_test",
            environment_id="env_test",
            status=ManagedAgentSessionStatus.CANCELED,
            runtime_ms=1500,
            billable_seconds=1.5,
        )


_PARENT_KEY = "run-idem-managed-fence"
_STEP_ID = "step-managed-0"


def _fence(tmp_path: Path) -> RuntimeEffectFence:
    return RuntimeEffectFence(fence_dir=tmp_path / ".harness" / "effect-fence")


def _dispatcher(
    client: _CountingClient,
    fence: RuntimeEffectFence | None,
    *,
    explicit: bool = False,
) -> ManagedAgentsStepDispatcher:
    return ManagedAgentsStepDispatcher(
        client=cast(Any, client),
        tracer_provider=TracerProvider(),
        effect_fence=fence,
        effect_fencing_explicit=explicit,
    )


def _step(payload: dict[str, Any] | None = None) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(_STEP_ID),
        step_kind=StepKind.MANAGED_AGENTS,
        step_payload=payload or {"agent_id": "agent_test", "environment_id": "env_test"},
    )


def _ctx(
    *,
    run_engine_class: EngineClass | None = EngineClass.EVENT_SOURCED_REPLAY,
    resolution: EffectFenceResolution | None = None,
    resolution_key: str | None = None,
) -> StepExecutionContext:
    directive = (
        EffectFenceResolutionDirective(
            resolution=resolution,
            idempotency_key=resolution_key
            or _compose_managed_agents_idempotency_key(_PARENT_KEY, _STEP_ID),
        )
        if resolution is not None
        else None
    )
    return StepExecutionContext(
        workflow_id="wf-managed-fence",
        parent_action_id="workflow:wf-managed-fence:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        parent_idempotency_key=_PARENT_KEY,
        tenant_id=None,
        step_index=0,
        run_engine_class=run_engine_class,
        effect_fence_resolution=directive,
    )


_KEY = _compose_managed_agents_idempotency_key(_PARENT_KEY, _STEP_ID)
_IDLE = [ManagedAgentSessionStatus.IDLE]


@pytest.mark.asyncio
async def test_first_dispatch_reserves_fires_and_captures(tmp_path: Path) -> None:
    """Gate open + claim absent → fires the vendor session ONCE + captures the outcome."""
    fence = _fence(tmp_path)
    client = _CountingClient(retrieve_statuses=_IDLE)
    out = await _dispatcher(client, fence).dispatch(cast(Any, None), _step(), step_context=_ctx())
    assert out["status"] == "idle"
    assert client.created == [("agent_test", "env_test")]
    assert fence.read_output(_KEY) == dict(out)


@pytest.mark.asyncio
async def test_maybe_ran_resume_suppresses_returns_captured_no_second_session(
    tmp_path: Path,
) -> None:
    """The at-most-once witness: a captured branch re-dispatched on resume SUPPRESSES —
    returns the CAPTURED outcome verbatim (full result-fidelity), create_session NOT
    called a second time."""
    fence = _fence(tmp_path)
    client = _CountingClient(retrieve_statuses=_IDLE)
    first = await _dispatcher(client, fence).dispatch(cast(Any, None), _step(), step_context=_ctx())
    # Re-dispatch the SAME effect (a crash-then-resume re-run of a maybe-ran branch).
    second = await _dispatcher(client, fence).dispatch(
        cast(Any, None), _step(), step_context=_ctx()
    )
    assert second == first  # the captured outcome, folded verbatim
    assert len(client.created) == 1  # vendor session fired EXACTLY ONCE across resume
    assert len(client.sent) == 1


@pytest.mark.asyncio
async def test_maybe_ran_no_capture_raises_ambiguous(tmp_path: Path) -> None:
    """Reserve held by a prior attempt that crashed in the create→capture window (no
    output) → a resume is genuinely ambiguous → raise (driver routes to §26.2 PAUSE);
    create_session NEVER auto-re-fires."""
    fence = _fence(tmp_path)
    fence.try_reserve(_KEY)  # prior attempt claimed, then crashed before capture
    client = _CountingClient(retrieve_statuses=_IDLE)
    with pytest.raises(EffectFenceAmbiguousUncommittedError):
        await _dispatcher(client, fence).dispatch(cast(Any, None), _step(), step_context=_ctx())
    assert client.created == []  # no second billable session


@pytest.mark.asyncio
async def test_skip_as_fired_returns_empty_keyed_outcome_no_session(tmp_path: Path) -> None:
    """SKIP_AS_FIRED resolution on a held reserve → an EMPTY-but-fully-keyed outcome
    (never None, so an opaque fold never KeyErrors), create_session NOT called. This is
    the advisor-flagged fold cell."""
    fence = _fence(tmp_path)
    fence.try_reserve(_KEY)
    client = _CountingClient(retrieve_statuses=_IDLE)
    out = await _dispatcher(client, fence).dispatch(
        cast(Any, None),
        _step(),
        step_context=_ctx(resolution=EffectFenceResolution.SKIP_AS_FIRED),
    )
    assert out == {
        "session_id": "",
        "agent_id": "agent_test",
        "environment_id": "env_test",
        "status": "skipped_as_fired",
        "runtime_ms": 0,
        "billable_seconds": 0.0,
    }
    assert client.created == []


@pytest.mark.asyncio
async def test_abort_resolution_raises_aborted(tmp_path: Path) -> None:
    """ABORT resolution on a held reserve → raise EffectFenceAbortedError (driver → FAILED);
    never proceed, never re-fire."""
    fence = _fence(tmp_path)
    fence.try_reserve(_KEY)
    client = _CountingClient(retrieve_statuses=_IDLE)
    with pytest.raises(EffectFenceAbortedError):
        await _dispatcher(client, fence).dispatch(
            cast(Any, None), _step(), step_context=_ctx(resolution=EffectFenceResolution.ABORT)
        )
    assert client.created == []


@pytest.mark.asyncio
async def test_re_fire_resolution_clears_and_fires_fresh(tmp_path: Path) -> None:
    """RE_FIRE resolution on a held reserve → clear the claim → fire the vendor session
    FRESH (operator asserts the prior attempt did NOT fire) → captures."""
    fence = _fence(tmp_path)
    fence.try_reserve(_KEY)
    client = _CountingClient(retrieve_statuses=_IDLE)
    out = await _dispatcher(client, fence).dispatch(
        cast(Any, None), _step(), step_context=_ctx(resolution=EffectFenceResolution.RE_FIRE)
    )
    assert out["status"] == "idle"
    assert client.created == [("agent_test", "env_test")]  # fired fresh
    assert fence.read_output(_KEY) == dict(out)


@pytest.mark.asyncio
async def test_non_success_terminal_raises_without_capture_then_ambiguous(tmp_path: Path) -> None:
    """The advisor-flagged disposition cell: a session that reaches a NON-success terminal
    (FAILED) raises WITHOUT capturing → the claim is held + no output → a subsequent
    resume is ambiguous (NOT a false suppress of a failed outcome)."""
    fence = _fence(tmp_path)
    client = _CountingClient(retrieve_statuses=[ManagedAgentSessionStatus.FAILED])
    with pytest.raises(ManagedAgentsSessionError):
        await _dispatcher(client, fence).dispatch(cast(Any, None), _step(), step_context=_ctx())
    assert fence.read_output(_KEY) is None  # failure did NOT capture
    # A resume of the same maybe-ran branch is ambiguous, never a false suppress.
    client2 = _CountingClient(retrieve_statuses=_IDLE)
    with pytest.raises(EffectFenceAmbiguousUncommittedError):
        await _dispatcher(client2, fence).dispatch(cast(Any, None), _step(), step_context=_ctx())
    assert client2.created == []


@pytest.mark.asyncio
async def test_poll_exhausted_cancels_raises_without_capture(tmp_path: Path) -> None:
    """Poll-budget exhaustion cancels the still-running session + raises WITHOUT capture →
    the claim is held + no output → a resume is ambiguous."""
    fence = _fence(tmp_path)
    client = _CountingClient(retrieve_statuses=[ManagedAgentSessionStatus.RUNNING])
    with pytest.raises(ManagedAgentsSessionError):
        await _dispatcher(client, fence).dispatch(
            cast(Any, None),
            _step(
                {
                    "agent_id": "agent_test",
                    "environment_id": "env_test",
                    "max_poll_attempts": 2,
                    "poll_interval_seconds": 0.0,
                }
            ),
            step_context=_ctx(),
        )
    assert client.cancelled == ["session_test"]
    assert fence.read_output(_KEY) is None


@pytest.mark.asyncio
async def test_gate_closed_non_durable_fires_every_dispatch(tmp_path: Path) -> None:
    """Negative control — a NON-durable run engine class + no explicit opt-in → the gate is
    closed → no reserve → the fence is inert → create_session fires on EVERY dispatch."""
    fence = _fence(tmp_path)
    client = _CountingClient(retrieve_statuses=_IDLE * 2)
    ctx = _ctx(run_engine_class=EngineClass.PURE_PATTERN_NO_ENGINE)
    await _dispatcher(client, fence).dispatch(cast(Any, None), _step(), step_context=ctx)
    await _dispatcher(client, fence).dispatch(cast(Any, None), _step(), step_context=ctx)
    assert len(client.created) == 2  # fence inert on a non-durable run
    assert fence.read_output(_KEY) is None


@pytest.mark.asyncio
async def test_explicit_opt_in_fences_even_non_durable(tmp_path: Path) -> None:
    """`effect_fencing_explicit=True` activates the fence even on a non-durable run — the
    operator opt-in path (mirrors the tool dispatcher)."""
    fence = _fence(tmp_path)
    client = _CountingClient(retrieve_statuses=_IDLE)
    ctx = _ctx(run_engine_class=EngineClass.PURE_PATTERN_NO_ENGINE)
    await _dispatcher(client, fence, explicit=True).dispatch(
        cast(Any, None), _step(), step_context=ctx
    )
    # Re-dispatch suppresses despite the non-durable class (explicit opt-in won).
    await _dispatcher(client, fence, explicit=True).dispatch(
        cast(Any, None), _step(), step_context=ctx
    )
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_durable_across_fresh_fence_instance(tmp_path: Path) -> None:
    """The claim + capture are ON-DISK → a FRESH RuntimeEffectFence pointing at the same
    dir (a 'process restart') still suppresses the re-dispatch."""
    client = _CountingClient(retrieve_statuses=_IDLE)
    await _dispatcher(client, _fence(tmp_path)).dispatch(
        cast(Any, None), _step(), step_context=_ctx()
    )
    # Fresh fence instance, same dir — durable suppress.
    second = await _dispatcher(client, _fence(tmp_path)).dispatch(
        cast(Any, None), _step(), step_context=_ctx()
    )
    assert second["status"] == "idle"
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_agent_swap_under_same_step_id_suppressed(tmp_path: Path) -> None:
    """The fence key is payload-INDEPENDENT (no agent_id) → a resumed agent-swap under the
    same step_id composes the SAME key → it is SUPPRESSED (returns the prior captured
    outcome), NOT a second billable session. Stricter than the TOOL_STEP tool-swap (which
    keys on tool_id) — accepted-parity-or-stricter, never a double-fire."""
    fence = _fence(tmp_path)
    client = _CountingClient(retrieve_statuses=_IDLE)
    first = await _dispatcher(client, fence).dispatch(
        cast(Any, None),
        _step({"agent_id": "agent_A", "environment_id": "env_test"}),
        step_context=_ctx(),
    )
    # Resume with a SWAPPED agent_id under the same step_id.
    second = await _dispatcher(client, fence).dispatch(
        cast(Any, None),
        _step({"agent_id": "agent_B", "environment_id": "env_test"}),
        step_context=_ctx(),
    )
    assert second == first  # suppressed — the original agent_A outcome
    assert client.created == [("agent_A", "env_test")]  # agent_B never fired


def test_managed_agents_key_disjoint_from_tool_key_collision() -> None:
    """Regression guard (out-of-family Codex [P2]) — the managed-agents fence key must NOT
    collide with a TOOL_STEP key whose `tool_id == "managed_agents"` at the same
    (parent, step_id). Both dispatchers share `.harness/effect-fence`, so a collision would
    cross-contaminate claim/output state. The leading-domain-tag composition guarantees
    disjointness by construction."""
    from harness_runtime.lifecycle.runtime_tool_dispatcher import _compose_idempotency_key

    managed_key = _compose_managed_agents_idempotency_key(_PARENT_KEY, _STEP_ID)
    tool_key_named_managed_agents = _compose_idempotency_key(
        _PARENT_KEY, _STEP_ID, "managed_agents"
    )
    assert managed_key != tool_key_named_managed_agents


def test_durable_auto_fence_set_matches_tool_dispatcher() -> None:
    """Drift guard — the locally-defined `_DURABLE_AUTO_FENCE_ENGINE_CLASSES` (defined
    locally to avoid the import cycle through runtime_tool_dispatcher) must stay equal to
    the tool dispatcher's source-of-truth set, so the two fence gates can never skew."""
    from harness_runtime.lifecycle import managed_agents_dispatch, runtime_tool_dispatcher

    assert (
        managed_agents_dispatch._DURABLE_AUTO_FENCE_ENGINE_CLASSES
        == runtime_tool_dispatcher._DURABLE_AUTO_FENCE_ENGINE_CLASSES
    )
