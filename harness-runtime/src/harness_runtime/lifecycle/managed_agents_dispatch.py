"""C-RT-28 §14.20 — Managed Agents step dispatcher (R-FS-1 arc M).

Production-wiring surface over the R-820-built carrier `managed_agents.py`:
a `StepDispatcher` for `StepKind.MANAGED_AGENTS` (CP spec v1.39 §5.2/§25.2)
that executes a workflow step's body via a **vendor-run** Managed Agents
session (Anthropic `beta.sessions.*`) rather than a harness-orchestrated loop.

The dispatch is **async** (the client port is async) → it satisfies the
`AsyncStepDispatcher` Protocol and is bound to the CP driver's sync
`StepDispatcher` Protocol via `SyncDispatcherFacade` at stage 5 (the
C-RT-15 inner / C-RT-17 sub-agent precedent).

Architectural distinction from `SUB_AGENT_DISPATCH` (C-RT-17): the harness does
NOT orchestrate the agent loop here — the vendor does. No topology-admissibility
check, no `subagent.*`/`topology.*` spans, no child-manifest recursion. The
dispatcher creates the session, sends the step's event, polls to a terminal
status, emits the `managed_agents.runtime` span, and returns the outcome.

Per `Spec_Harness_Runtime_v1.md` §14.20 (C-RT-28) + the paired CP spec v1.39
`StepKind.MANAGED_AGENTS` extension (operator-ratified 2026-06-17, Option B).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from harness_cp.engine_class import EngineClass
from harness_cp.pause_resume_protocol_types import EffectFenceResolution
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep

from harness_runtime.lifecycle.effect_fence import (
    EffectFenceAbortedError,
    EffectFenceAmbiguousUncommittedError,
    EffectFenceProtocol,
)
from harness_runtime.lifecycle.managed_agents import (
    ManagedAgentEvent,
    ManagedAgentsClientProtocol,
    ManagedAgentSessionStatus,
    managed_agents_runtime_span,
)

__all__ = [
    "ManagedAgentsConfig",
    "ManagedAgentsSessionError",
    "ManagedAgentsStageMaterializeError",
    "ManagedAgentsStepDispatcher",
]


# The durable-execution engine classes that AUTO-activate the §14.22 effect fence (a
# crash-resume re-dispatches uncommitted steps under these). MIRRORS
# `runtime_tool_dispatcher._DURABLE_AUTO_FENCE_ENGINE_CLASSES` — defined locally (not
# imported) because importing the heavy tool-dispatcher module from here forms an import
# cycle through `harness_runtime.config.provider_secrets → harness_runtime.types`. A
# drift-guard test (`test_managed_agents_fence_gate_set_matches_tool_dispatcher`) asserts
# the two sets stay equal, so the duplication can never silently skew.
_DURABLE_AUTO_FENCE_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.SAVE_POINT_CHECKPOINT,
        EngineClass.EVENT_SOURCED_REPLAY,
        EngineClass.WAL_SEGMENT,
        EngineClass.RECONCILER_LOOP,
    }
)


# Terminal statuses (the poll loop stops on these); success subset returns a
# step output, the rest map to RT-FAIL-MANAGED-AGENTS-SESSION (§14.20.2 step 7).
_TERMINAL_STATUSES: frozenset[ManagedAgentSessionStatus] = frozenset(
    {
        ManagedAgentSessionStatus.IDLE,
        ManagedAgentSessionStatus.COMPLETED,
        ManagedAgentSessionStatus.FAILED,
        ManagedAgentSessionStatus.CANCELED,
        ManagedAgentSessionStatus.TERMINATED,
    }
)
_SUCCESS_STATUSES: frozenset[ManagedAgentSessionStatus] = frozenset(
    {ManagedAgentSessionStatus.IDLE, ManagedAgentSessionStatus.COMPLETED}
)


def _compose_managed_agents_idempotency_key(parent_idempotency_key: str, step_id: str) -> str:
    """Payload-independent effect-fence key for a MANAGED_AGENTS dispatch
    (B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL, R-FS-1).

    The tool key is `H(parent : step_id : tool_id)` (§14.9.7 recipe). This LEADS the digest
    with a constant ``managed_agents`` domain tag — `H(managed_agents : parent : step_id)`.
    The tag is DELIBERATELY in the LEADING slot, not the trailing tool-id slot: a trailing
    tag would be byte-identical to a TOOL_STEP whose `tool_id == "managed_agents"` at the
    same `(parent, step_id)` → a cross-sink fence collision (both dispatchers share
    `.harness/effect-fence`) — out-of-family Codex [P2]. Leading the tag makes the key
    disjoint from EVERY tool key by construction: a collision would require a tool's
    harness-composed `parent_idempotency_key` to begin with the literal ``managed_agents:``,
    which a run idempotency key never is. DELIBERATELY carries NO ``agent_id`` / payload
    component: a resumed agent-swap under the same ``step_id`` composes the SAME key → it is
    SUPPRESSED (the captured prior outcome is returned) rather than firing a SECOND billable
    vendor session — accepted-parity-or-stricter vs the cleared TOOL_STEP tool-swap path (CP
    spec v1.65 §3): per-key at-most-once is preserved."""
    digest = hashlib.sha256()
    digest.update(b"managed_agents")
    digest.update(b":")
    digest.update(parent_idempotency_key.encode("utf-8"))
    digest.update(b":")
    digest.update(step_id.encode("utf-8"))
    return digest.hexdigest()


def _skipped_as_fired_outcome(*, agent_id: str, environment_id: str) -> dict[str, Any]:
    """The EMPTY-shape outcome for a maybe-ran MANAGED_AGENTS branch the operator
    resolves SKIP_AS_FIRED (B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL): the vendor
    session FIRED but its outcome was lost in the create→capture crash window and is
    unrecoverable, so proceed WITHOUT re-dispatching a second billable session.

    Keeps the 6 success-outcome KEYS present (so an opaque fold / downstream consumer
    reading e.g. ``.get("status")`` never KeyErrors) with empty / zero values;
    ``status="skipped_as_fired"`` labels the cell. Parity with the TOOL_STEP SKIP_AS_FIRED
    empty-output return (runtime spec §14.22.9)."""
    return {
        "session_id": "",
        "agent_id": agent_id,
        "environment_id": environment_id,
        "status": "skipped_as_fired",
        "runtime_ms": 0,
        "billable_seconds": 0.0,
    }


@dataclass(frozen=True)
class ManagedAgentsConfig:
    """Operator opt-in marker + supplied client for managed-agents dispatch.

    Presence (non-``None`` at ``RuntimeConfig.managed_agents_config``) +
    ``DeploymentSurface.MANAGED_CLOUD`` signals operator opt-in (the
    H_T-AS-8f local-development exclusion remains TRUE — the dispatcher binds
    only on managed-cloud). ``client`` is the concrete
    ``ManagedAgentsClientProtocol`` implementation (e.g.
    ``AnthropicManagedAgentsClient`` over a live SDK client), supplied
    out-of-band at the operator-controlled bootstrap site — NO credentials are
    embedded in this config. Typed ``Any`` (instead of the structural
    ``ManagedAgentsClientProtocol | None``) so Pydantic v2 schema-generation at
    ``RuntimeConfig.managed_agents_config`` introspection succeeds (the
    runtime_checkable Protocol is duck-typed at dispatch, not validated at the
    dataclass layer) — the same pattern as ``SkillActivationHookConfig.hook``.

    ``step_timeout_seconds`` is the ``SyncDispatcherFacade``
    ``result_timeout_seconds`` bound used **only** for the
    ``StepKind.MANAGED_AGENTS`` facade binding — intentionally DECOUPLED from
    the shared ``RuntimeConfig.step_dispatch_timeout_seconds`` (30s default).
    A vendor-run managed-agents session runs **minutes**, not seconds; binding
    the facade to the shared 30s bound would fire ``RT-FAIL-STEP-DISPATCH-
    TIMEOUT`` at 30s (wrong fail class) while the vendor session keeps running,
    billable, never cancelled (the abandoned coroutine's poll loop never
    reaches its cancel-on-give-up path). The default 600s must exceed the
    per-step poll budget (``max_poll_attempts × poll_interval_seconds`` +
    create/send/retrieve latency headroom); the operator sizes both together.

    Per runtime spec v1.55 §14.20.1 / §14.20.3.
    """

    client: Any = None
    step_timeout_seconds: float = 600.0


class ManagedAgentsStageMaterializeError(Exception):
    """Stage-5 factory cannot construct the managed-agents dispatcher.

    Fail class ``RT-FAIL-MANAGED-AGENTS-STAGE-MATERIALIZE`` (permanent →
    bootstrap rollback per ADR-F4 v1.1 / C-RT-02). Raised when opted-in on
    ``MANAGED_CLOUD`` but the tracer provider is unbound at stage-5 entry, the
    operator-supplied client is absent, or dispatcher construction raises.
    Per runtime spec v1.55 §14.20.4.
    """


class ManagedAgentsSessionError(Exception):
    """A managed-agents dispatch did not reach a success terminal status.

    Fail class ``RT-FAIL-MANAGED-AGENTS-SESSION`` (step-failure; NOT a
    bootstrap fail). Raised on a terminal ``FAILED`` / ``CANCELED`` /
    ``TERMINATED`` status, poll-budget exhaustion, or a missing required
    ``step_payload`` input. The CP driver maps the type name to
    ``step-failure: RT-FAIL-MANAGED-AGENTS-SESSION`` per the §14.20.4 /
    §25.3.3.4 try/except discipline (name-match per the
    ``StepDispatchTimeoutError`` precedent — harness-cp cannot import from
    harness-runtime). Per runtime spec v1.55 §14.20.4.
    """


@dataclass(frozen=True)
class ManagedAgentsStepDispatcher:
    """Async ``StepDispatcher`` for ``StepKind.MANAGED_AGENTS`` (C-RT-28).

    Constructed at bootstrap stage 5 by
    ``materialize_managed_agents_dispatcher_stage`` (§14.20.3). Holds a
    ``ManagedAgentsClientProtocol`` + the ``ctx.tracer_provider`` (typed
    ``Any`` per the C-RT-04 OTel-SDK-type-deferral pattern). Bound to the CP
    driver's sync ``StepDispatcher`` Protocol via ``SyncDispatcherFacade``.
    """

    client: ManagedAgentsClientProtocol
    tracer_provider: Any
    effect_fence: EffectFenceProtocol | None = None
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL (R-FS-1, §14.22 C-RT-31) — the
    crash-atomic effect fence that makes the vendor-session effect (create + send)
    at-most-once across a crash-resume. ``None`` (non-fence bootstrap / unit construction)
    → no reserve, no claim → byte-identical to pre-arc. Stage-5 factory passes the shared
    ``RuntimeEffectFence`` (the SAME claim dir as the tool dispatcher)."""
    effect_fencing_explicit: bool = False
    """The operator's ``RuntimeConfig.effect_fencing`` opt-in. ``True`` → fence every
    managed-agents dispatch; ``False`` (default) → AUTO-fence only when the RUN's engine
    class is durable-execution (``step_context.run_engine_class`` ∈
    ``_DURABLE_AUTO_FENCE_ENGINE_CLASSES``) — the §14.22.7 per-run gate, mirroring the tool
    dispatcher so non-durable runs stay fence-free."""

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Run a managed-agents session for one ``MANAGED_AGENTS`` step.

        Per §14.20.2: read ``step_payload`` → create session → send the step
        event → poll to terminal → emit the ``managed_agents.runtime`` span →
        return the outcome mapping. Raises ``ManagedAgentsSessionError`` on a
        non-success terminal status / poll-budget exhaustion / missing input.

        B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL (R-FS-1): when an effect
        fence is bound + the per-run gate is open, the vendor-session effect
        (create + send) is wrapped in ONE crash-atomic claim keyed on
        ``(parent_idempotency_key, step_id)`` (a LEAF effect — no harness-side
        reconstruction), so a maybe-ran fan-out worker re-dispatched on a strict-tier
        crash-resume SUPPRESSES (returns the captured outcome) / PAUSES (ambiguous) /
        re-fires (claim absent) instead of creating a second billable session. Mirrors
        the §14.22 tool-dispatcher fence gate verbatim.
        """
        _ = binding  # binding not consumed by managed-agents dispatch (vendor-run loop)
        payload = step.step_payload

        agent_id = payload.get("agent_id")
        environment_id = payload.get("environment_id")
        if not agent_id or not environment_id:
            raise ManagedAgentsSessionError(
                "managed-agents step_payload requires non-empty 'agent_id' + "
                f"'environment_id' (got agent_id={agent_id!r}, "
                f"environment_id={environment_id!r})"
            )

        event = ManagedAgentEvent(
            event_type=str(payload.get("event_type", "user.message")),
            payload=dict(payload.get("event_payload", {})),
        )
        metadata_raw = payload.get("metadata")
        metadata: Mapping[str, str] | None = (
            cast("Mapping[str, str]", metadata_raw) if isinstance(metadata_raw, Mapping) else None
        )
        title_raw = payload.get("title")

        # --- Effect fence (B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL, R-FS-1) ----
        # At-most-once for the UNFENCED vendor-session effect (create_session +
        # send_event): a maybe-ran fan-out MANAGED_AGENTS worker re-dispatched on a
        # strict-tier crash-resume would otherwise create a SECOND billable session +
        # re-send the event. The managed-agents dispatch is a LEAF — one opaque vendor
        # effect returning an outcome mapping, NO harness-side reconstruction (unlike
        # SUB_AGENT_DISPATCH's recursive child) — so result-fidelity holds by
        # construction: a suppress folds the CAPTURED outcome verbatim. The §14.22 fence
        # gate is applied VERBATIM (one coarse claim around the whole dispatch); managed
        # agents is never `idempotent`, so there is no per-tool exemption.
        #
        # The whole gate is guarded on `self.effect_fence is not None`: a fence-bound
        # dispatcher ALWAYS receives a real `step_context` (the CP driver composes one at
        # every dispatch site), so reading `parent_idempotency_key` / `run_engine_class`
        # here is safe; an UNbound dispatcher (no fence) skips the block entirely (the
        # pre-arc byte-identical path, which a unit test may exercise with no context).
        idempotency_key: str | None = None
        fence_gate_open = False
        if self.effect_fence is not None:
            idempotency_key = _compose_managed_agents_idempotency_key(
                step_context.parent_idempotency_key, step.step_id
            )
            fence_gate_open = self.effect_fencing_explicit or (
                step_context.run_engine_class in _DURABLE_AUTO_FENCE_ENGINE_CLASSES
            )
            # A KEY-BOUND operator resolution the driver threaded onto the RESUMED step's
            # context — applied ONLY when it targets THIS dispatch's key (a stale
            # resolution can never mis-apply to a different fenced effect). `None` /
            # non-match → the fence behaves exactly as a naive resume (INERT re-pause).
            fence_resolution = (
                step_context.effect_fence_resolution.resolution
                if (
                    step_context.effect_fence_resolution is not None
                    and step_context.effect_fence_resolution.idempotency_key == idempotency_key
                )
                else None
            )
            if (
                fence_gate_open
                and fence_resolution is EffectFenceResolution.RE_FIRE
                and self.effect_fence.try_consume_refire(idempotency_key)
            ):
                # RE_FIRE — operator asserts the prior attempt did NOT fire; clear the held
                # claim so the `try_reserve` below WINS + fires fresh. The consume-once
                # `.refire` latch makes a retry/crash-resume of the re-fire LOSE → it falls
                # through to the suppress/ambiguous split, so the re-fire can never
                # double-create a vendor session.
                self.effect_fence.clear_claim(idempotency_key)
            if fence_gate_open and not self.effect_fence.try_reserve(idempotency_key):
                # The reserve was lost to a prior uncommitted attempt of THIS dispatch (a
                # crash-then-resume re-run of a maybe-ran branch). A key-bound
                # SKIP_AS_FIRED / ABORT resolution acts BEFORE the auto captured-output
                # split.
                if fence_resolution is EffectFenceResolution.SKIP_AS_FIRED:
                    return _skipped_as_fired_outcome(
                        agent_id=str(agent_id), environment_id=str(environment_id)
                    )
                if fence_resolution is EffectFenceResolution.ABORT:
                    # Operator cannot determine whether the session fired (or declines to
                    # proceed) → fail the run terminally (the driver name-matches
                    # `EffectFenceAbortedError` → FAILED); never re-fire, never proceed.
                    raise EffectFenceAbortedError(idempotency_key=idempotency_key)
                captured_output = self.effect_fence.read_output(idempotency_key)
                if captured_output is not None:
                    # Output present + valid → the vendor session demonstrably completed
                    # AND its outcome is in hand → suppress-and-continue: return the
                    # CAPTURED outcome verbatim (full result-fidelity), NEVER re-dispatch.
                    return dict(captured_output)
                # Output absent / corrupt → the crash fell in the create→capture window →
                # whether the vendor session fired is genuinely ambiguous → fail to the
                # operator (the driver routes to a §26.2 EFFECT_FENCE_AMBIGUOUS PAUSE when
                # a PauseResumeProtocol is bound, else FAILED). NEVER auto-re-fire.
                raise EffectFenceAmbiguousUncommittedError(idempotency_key=idempotency_key)

        session = await self.client.create_session(
            agent_id=str(agent_id),
            environment_id=str(environment_id),
            title=str(title_raw) if title_raw is not None else None,
            metadata=metadata,
        )
        await self.client.send_event(session_id=session.session_id, event=event)

        # Bounded poll to a terminal status (§14.20.7 — budget is impl
        # discretion; defaults bound the loop; the fake client in unit tests
        # returns terminal on the first retrieve so no sleep fires).
        max_poll_attempts = max(1, int(payload.get("max_poll_attempts", 30)))
        poll_interval = float(payload.get("poll_interval_seconds", 1.0))
        reached_terminal = False
        for attempt in range(max_poll_attempts):
            session = await self.client.retrieve_session(session_id=session.session_id)
            if session.status in _TERMINAL_STATUSES:
                reached_terminal = True
                break
            if attempt + 1 < max_poll_attempts:
                await asyncio.sleep(poll_interval)

        # §14.20.5 invariant 3 — emit the managed_agents.runtime span at every
        # dispatch (success or failure), carrying the final session metadata.
        tracer = self.tracer_provider.get_tracer("harness.runtime.managed_agents")
        async with managed_agents_runtime_span(tracer=tracer, session=session):
            pass

        if not reached_terminal:
            # §14.20.2 step 4 — the session is still running server-side and
            # billable; the harness has given up waiting. Best-effort cancel to
            # avoid orphaning a billable vendor session before raising. A cancel
            # failure must NOT mask the primary (budget-exhausted) error — it is
            # the load-bearing signal — so swallow only the cancel exception.
            try:
                await self.client.cancel_session(session_id=session.session_id)
            except Exception as cancel_exc:  # best-effort cleanup on give-up
                raise ManagedAgentsSessionError(
                    f"managed-agents session {session.session_id} did not reach a "
                    f"terminal status within {max_poll_attempts} polls "
                    f"(last status={session.status.value}); the best-effort cancel "
                    f"also failed ({cancel_exc!r}) — the session may be orphaned"
                ) from cancel_exc
            raise ManagedAgentsSessionError(
                f"managed-agents session {session.session_id} did not reach a "
                f"terminal status within {max_poll_attempts} polls "
                f"(last status={session.status.value}); cancelled to avoid "
                f"orphaning a billable session"
            )
        if session.status not in _SUCCESS_STATUSES:
            raise ManagedAgentsSessionError(
                f"managed-agents session {session.session_id} terminated "
                f"non-successfully (status={session.status.value})"
            )

        outcome = {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "environment_id": session.environment_id,
            "status": session.status.value,
            "runtime_ms": session.runtime_ms,
            "billable_seconds": session.billable_seconds,
        }
        # Capture AFTER a validated success (the non-success / poll-exhausted paths raised
        # above WITHOUT capturing → claim held + no output → a resume is genuinely
        # ambiguous → PAUSE). A present capture therefore always denotes a complete, valid
        # success the suppress path can fold verbatim (§14.22 capture-on-success-only).
        # `idempotency_key is not None` whenever `fence_gate_open` (both set together in
        # the bound-fence block above) — the explicit check satisfies the type narrowing.
        if self.effect_fence is not None and fence_gate_open and idempotency_key is not None:
            self.effect_fence.capture_output(idempotency_key, outcome)
        return outcome
