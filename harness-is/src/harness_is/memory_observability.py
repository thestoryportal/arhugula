"""Additive memory telemetry namespace - U-MEM-22, C-MEM-19.

The helper preserves the existing six `memory.*` attributes while adding the
C-MEM-19 observability attributes for memory capture, retrieval, packet
assembly, injection, promotion, native adapter calls, standard tool calls,
redaction, and denial.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from enum import StrEnum
from types import TracebackType
from typing import Any


class MemoryTelemetryOperationName(StrEnum):
    """Closed operation names required by C-MEM-19."""

    CAPTURE = "capture"
    RETRIEVAL = "retrieval"
    RANKING = "ranking"
    PACKET_ASSEMBLY = "packet_assembly"
    INJECTION = "injection"
    PROMOTION = "promotion"
    NATIVE_ADAPTER_CALL = "native_adapter_call"
    STANDARD_TOOL_CALL = "standard_tool_call"
    REDACTION = "redaction"
    TOMBSTONE = "tombstone"
    DENIAL = "denial"


class MemoryTelemetryFailureClass(StrEnum):
    """Failure classes required by C-MEM-19."""

    POLICY_DENIAL = "policy_denial"
    PATH_VIOLATION = "path_violation"
    IO_FAILURE = "io_failure"
    SERIALIZATION_FAILURE = "serialization_failure"
    PROVIDER_ADAPTER_FAILURE = "provider_adapter_failure"
    RETRIEVAL_EMPTY_RESULT = "retrieval_empty_result"


def memory_telemetry_span(
    tracer_provider: Any | None,
    *,
    tracer_name: str,
    span_name: str = "memory.operation",
    operation_name: MemoryTelemetryOperationName | str,
    operation_kind: str | None = None,
    tier: str | None = None,
    access_mode: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    cli_profile: str | None = None,
    policy_decision: str | None = None,
    packet_hash: str | None = None,
    record_count: int | None = None,
    failure_class: MemoryTelemetryFailureClass | str | None = None,
    path: str | None = None,
    backend: str | None = None,
    bytes_read: int | None = None,
    bytes_written: int | None = None,
    context_editing_active: bool | None = None,
) -> AbstractContextManager[Any]:
    """Return a span context manager or a no-op when no tracer is configured."""

    if tracer_provider is None:
        return nullcontext(None)
    tracer = tracer_provider.get_tracer(tracer_name)
    return _MemoryTelemetrySpanContext(
        span_cm=tracer.start_as_current_span(span_name),
        operation_name=operation_name,
        operation_kind=operation_kind,
        tier=tier,
        access_mode=access_mode,
        provider=provider,
        model=model,
        cli_profile=cli_profile,
        policy_decision=policy_decision,
        packet_hash=packet_hash,
        record_count=record_count,
        failure_class=failure_class,
        path=path,
        backend=backend,
        bytes_read=bytes_read,
        bytes_written=bytes_written,
        context_editing_active=context_editing_active,
    )


def set_memory_telemetry_attributes(
    span: Any,
    *,
    operation_name: MemoryTelemetryOperationName | str | None = None,
    operation_kind: str | None = None,
    tier: str | None = None,
    access_mode: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    cli_profile: str | None = None,
    policy_decision: str | None = None,
    packet_hash: str | None = None,
    record_count: int | None = None,
    failure_class: MemoryTelemetryFailureClass | str | None = None,
    path: str | None = None,
    backend: str | None = None,
    bytes_read: int | None = None,
    bytes_written: int | None = None,
    context_editing_active: bool | None = None,
) -> None:
    """Populate additive C-MEM-19 attributes on an open span."""

    if span is None:
        return
    if operation_name is not None:
        span.set_attribute("memory.operation.name", _string_value(operation_name))
    if operation_kind is not None:
        span.set_attribute("memory.operation.kind", operation_kind)
    if tier is not None:
        span.set_attribute("memory.tier", tier)
    if access_mode is not None:
        span.set_attribute("memory.access_mode", access_mode)
    if provider is not None:
        span.set_attribute("memory.provider", provider)
    if model is not None:
        span.set_attribute("memory.model", model)
    if cli_profile is not None:
        span.set_attribute("memory.cli_profile", cli_profile)
    if policy_decision is not None:
        span.set_attribute("memory.policy.decision", policy_decision)
    if packet_hash is not None:
        span.set_attribute("memory.packet_hash", packet_hash)
    if record_count is not None:
        span.set_attribute("memory.record_count", record_count)
    if failure_class is not None:
        span.set_attribute("memory.failure_class", _string_value(failure_class))
    if path is not None:
        span.set_attribute("memory.path", path)
    if backend is not None:
        span.set_attribute("memory.backend", backend)
    if bytes_read is not None:
        span.set_attribute("memory.bytes_read", bytes_read)
    if bytes_written is not None:
        span.set_attribute("memory.bytes_written", bytes_written)
    if context_editing_active is not None:
        span.set_attribute("memory.context_editing_active", context_editing_active)


def classify_memory_failure(exc: BaseException) -> MemoryTelemetryFailureClass:
    """Classify memory failures into the C-MEM-19 vocabulary."""

    message = str(exc).lower()
    exc_name = type(exc).__name__.lower()
    if "policy" in message or "denied" in message or "unavailable" in message:
        return MemoryTelemetryFailureClass.POLICY_DENIAL
    if "path" in message or "traversal" in message or "prefix" in message:
        return MemoryTelemetryFailureClass.PATH_VIOLATION
    if isinstance(exc, (OSError, IOError)) or "io" in exc_name:
        return MemoryTelemetryFailureClass.IO_FAILURE
    if "json" in exc_name or "serial" in message or "decode" in message:
        return MemoryTelemetryFailureClass.SERIALIZATION_FAILURE
    return MemoryTelemetryFailureClass.PROVIDER_ADAPTER_FAILURE


class _MemoryTelemetrySpanContext(AbstractContextManager[Any]):
    def __init__(
        self,
        *,
        span_cm: Any,
        operation_name: MemoryTelemetryOperationName | str,
        operation_kind: str | None,
        tier: str | None,
        access_mode: str | None,
        provider: str | None,
        model: str | None,
        cli_profile: str | None,
        policy_decision: str | None,
        packet_hash: str | None,
        record_count: int | None,
        failure_class: MemoryTelemetryFailureClass | str | None,
        path: str | None,
        backend: str | None,
        bytes_read: int | None,
        bytes_written: int | None,
        context_editing_active: bool | None,
    ) -> None:
        self._span_cm = span_cm
        self._attrs: dict[str, Any] = {
            "operation_name": operation_name,
            "operation_kind": operation_kind,
            "tier": tier,
            "access_mode": access_mode,
            "provider": provider,
            "model": model,
            "cli_profile": cli_profile,
            "policy_decision": policy_decision,
            "packet_hash": packet_hash,
            "record_count": record_count,
            "failure_class": failure_class,
            "path": path,
            "backend": backend,
            "bytes_read": bytes_read,
            "bytes_written": bytes_written,
            "context_editing_active": context_editing_active,
        }

    def __enter__(self) -> Any:
        span = self._span_cm.__enter__()
        set_memory_telemetry_attributes(span, **self._attrs)
        return span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self._span_cm.__exit__(exc_type, exc, traceback)


def _string_value(value: MemoryTelemetryOperationName | MemoryTelemetryFailureClass | str) -> str:
    if isinstance(value, StrEnum):
        return value.value
    return value


__all__ = [
    "MemoryTelemetryFailureClass",
    "MemoryTelemetryOperationName",
    "classify_memory_failure",
    "memory_telemetry_span",
    "set_memory_telemetry_attributes",
]
