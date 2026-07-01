"""R-820 provider-free Managed Agents contract helpers.

This module supplies:

- small provider-neutral agent session records,
- a protocol plus an Anthropic SDK adapter for Managed Agents sessions, and
- a `managed_agents.runtime` span helper carrying the AS `managed_agents.*`
  namespace.

Cite-bind: this is the carrier formalized by **C-RT-28** (`Spec_Harness_Runtime_v1.md`
§14.20; R-FS-1 arc M) and the substitution **H_T-AS-8f** (`managed_agents.*`
namespace; SUBSTANTIVE_RETIRED at R-820). The production-wiring surface (the
`ManagedAgentsStepDispatcher` + stage-5 factory) lives at
`managed_agents_dispatch.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast, runtime_checkable

__all__ = [
    "ANTHROPIC_MANAGED_AGENTS_BETA",
    "AnthropicManagedAgentsClient",
    "ManagedAgentEvent",
    "ManagedAgentSession",
    "ManagedAgentSessionStatus",
    "ManagedAgentsClientProtocol",
    "managed_agents_runtime_span",
]


ANTHROPIC_MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
"""Anthropic Managed Agents beta header value used by the R-820 live path."""


class ManagedAgentSessionStatus(StrEnum):
    """Provider-neutral session lifecycle states for a managed agent run."""

    CREATED = "created"
    IDLE = "idle"
    RUNNING = "running"
    RESCHEDULING = "rescheduling"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class ManagedAgentSession:
    """Provider-neutral metadata for a Managed Agents session."""

    session_id: str
    agent_id: str
    environment_id: str
    status: ManagedAgentSessionStatus
    runtime_ms: int
    billable_seconds: float


@dataclass(frozen=True, slots=True)
class ManagedAgentEvent:
    """Provider-neutral event emitted to or from a Managed Agents session."""

    event_type: str
    payload: Mapping[str, Any]


@runtime_checkable
class ManagedAgentsClientProtocol(Protocol):
    """Minimal async port for a provider-backed Managed Agents adapter.

    `@runtime_checkable` (method-only Protocol) so it can serve as the
    `HarnessContext.managed_agents_client` Pydantic field type (C-RT-28 §14.20.1)
    — mirrors the `ValidatorFramework` runtime_checkable-Protocol field pattern.
    """

    async def create_session(
        self,
        *,
        agent_id: str,
        environment_id: str,
        title: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ManagedAgentSession: ...

    async def send_event(
        self,
        *,
        session_id: str,
        event: ManagedAgentEvent,
    ) -> ManagedAgentEvent: ...

    async def retrieve_session(self, *, session_id: str) -> ManagedAgentSession: ...

    async def cancel_session(self, *, session_id: str) -> ManagedAgentSession: ...


def _read_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, Any], value)
        return mapping.get(field, default)
    return getattr(value, field, default)


def _to_payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True)
        if isinstance(dumped, Mapping):
            return cast(Mapping[str, Any], dumped)
    return {"value": repr(value)}


def _status_from_anthropic(status: Any) -> ManagedAgentSessionStatus:
    status_value = str(status or "").lower()
    return {
        "idle": ManagedAgentSessionStatus.IDLE,
        "running": ManagedAgentSessionStatus.RUNNING,
        "rescheduling": ManagedAgentSessionStatus.RESCHEDULING,
        "terminated": ManagedAgentSessionStatus.TERMINATED,
    }.get(status_value, ManagedAgentSessionStatus.FAILED)


def _session_from_anthropic(value: Any) -> ManagedAgentSession:
    stats = _read_field(value, "stats", {})
    agent = _read_field(value, "agent", {})
    active_seconds = _read_field(stats, "active_seconds", 0.0) or 0.0
    return ManagedAgentSession(
        session_id=str(_read_field(value, "id", "")),
        agent_id=str(_read_field(agent, "id", "")),
        environment_id=str(_read_field(value, "environment_id", "")),
        status=_status_from_anthropic(_read_field(value, "status")),
        runtime_ms=int(float(active_seconds) * 1000),
        billable_seconds=float(active_seconds),
    )


class AnthropicManagedAgentsClient:
    """Async wrapper around Anthropic's beta Managed Agents sessions API.

    The Python SDK methods are synchronous at the current lockfile version, so
    this adapter runs SDK calls in a worker thread while preserving the async
    runtime port used by the harness.
    """

    def __init__(self, *, client: Any) -> None:
        self._client = client

    async def create_session(
        self,
        *,
        agent_id: str,
        environment_id: str,
        title: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ManagedAgentSession:
        def _create() -> Any:
            resolved_metadata: dict[str, str] = dict(metadata or {})
            return self._client.beta.sessions.create(
                agent=agent_id,
                environment_id=environment_id,
                title=title,
                metadata=resolved_metadata,
                betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            )

        return _session_from_anthropic(await asyncio.to_thread(_create))

    async def send_event(
        self,
        *,
        session_id: str,
        event: ManagedAgentEvent,
    ) -> ManagedAgentEvent:
        payload = dict(event.payload)
        event_body: dict[str, Any] = {"type": event.event_type, **payload}

        def _send() -> Any:
            return self._client.beta.sessions.events.send(
                session_id,
                events=[event_body],
                betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            )

        response = await asyncio.to_thread(_send)
        data = _read_field(response, "data")
        if isinstance(data, list) and data:
            returned: Any = cast(list[Any], data)[0]
            returned_type = str(_read_field(returned, "type", event.event_type))
            return ManagedAgentEvent(event_type=returned_type, payload=_to_payload(returned))
        return event

    async def retrieve_session(self, *, session_id: str) -> ManagedAgentSession:
        def _retrieve() -> Any:
            return self._client.beta.sessions.retrieve(
                session_id,
                betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            )

        return _session_from_anthropic(await asyncio.to_thread(_retrieve))

    async def cancel_session(self, *, session_id: str) -> ManagedAgentSession:
        def _interrupt_and_archive() -> Any:
            self._client.beta.sessions.events.send(
                session_id,
                events=[{"type": "user.interrupt"}],
                betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            )
            return self._client.beta.sessions.archive(
                session_id,
                betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            )

        session = _session_from_anthropic(await asyncio.to_thread(_interrupt_and_archive))
        return ManagedAgentSession(
            session_id=session.session_id,
            agent_id=session.agent_id,
            environment_id=session.environment_id,
            status=ManagedAgentSessionStatus.CANCELED,
            runtime_ms=session.runtime_ms,
            billable_seconds=session.billable_seconds,
        )


@asynccontextmanager
async def managed_agents_runtime_span(
    *,
    tracer: Any,
    session: ManagedAgentSession | None = None,
    session_id: str | None = None,
    runtime_ms: int | None = None,
    billable_seconds: float | None = None,
) -> AsyncGenerator[Any, None]:
    """Open a `managed_agents.runtime` span with AS namespace attributes.

    The helper accepts either a full `ManagedAgentSession` or explicit fields.
    This lets future live adapters emit metadata directly from provider session
    responses while provider-free tests can exercise telemetry without SDK calls.
    """

    resolved_session_id = session.session_id if session is not None else session_id
    resolved_runtime_ms = session.runtime_ms if session is not None else runtime_ms
    resolved_billable_seconds = (
        session.billable_seconds if session is not None else billable_seconds
    )

    with tracer.start_as_current_span("managed_agents.runtime") as span:
        if resolved_runtime_ms is not None:
            span.set_attribute("managed_agents.runtime_ms", resolved_runtime_ms)
        if resolved_session_id is not None:
            span.set_attribute("managed_agents.session_id", resolved_session_id)
        if resolved_billable_seconds is not None:
            span.set_attribute("managed_agents.billable_seconds", resolved_billable_seconds)
        yield span
