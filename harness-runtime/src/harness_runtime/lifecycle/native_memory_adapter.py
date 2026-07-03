"""Anthropic native Memory tool adapter over the canonical memory store."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final, cast

from harness_is.memory_observability import (
    MemoryTelemetryOperationName,
    classify_memory_failure,
    memory_telemetry_span,
)
from harness_is.memory_operation_ledger import (
    MemoryOperationEngineClass,
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
)
from harness_is.memory_policy import AccessDecision, CaptureDecision, MemoryPolicyResolver
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    RedactionState,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, Identifier

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
)

__all__ = ["CanonicalNativeMemoryToolBackend", "normalize_native_memory_path"]


_MEMORIES_SCOPE: Final[str] = "/memories/"
_TEXT_ENCODING: Final[str] = "utf-8"
_SCHEMA_VERSION: Final[str] = "native-memory-adapter/v1"
_TOOL_NAME: Final[str] = "anthropic_memory"


@dataclass(frozen=True)
class _ValidatedMemoryPath:
    external_path: str
    relative_path: str


@dataclass(frozen=True)
class _NativeMemoryState:
    record: MemoryStoreRecord
    content: bytes | None
    deleted: bool


class CanonicalNativeMemoryToolBackend:
    """Canonical-store implementation of the Anthropic Memory callback protocol."""

    def __init__(
        self,
        *,
        store: CanonicalMemoryStore,
        policy_resolver: MemoryPolicyResolver,
        scope: MemoryScope,
        actor: Actor,
        run_id: str,
        step_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cli_profile: str | None = None,
        policy_ref: str | None = None,
        engine_class: MemoryOperationEngineClass | None = None,
        procedural_snapshot_ref: str | None = None,
        clock: Callable[[], datetime] | None = None,
        tracer_provider: object | None = None,
    ) -> None:
        self._store = store
        self._policy_resolver = policy_resolver
        self._scope = scope
        self._actor = actor
        self._run_id = run_id
        self._step_id = step_id
        self._provider = provider
        self._model = model
        self._cli_profile = cli_profile
        self._policy_ref = policy_ref
        self._engine_class = engine_class
        self._procedural_snapshot_ref = procedural_snapshot_ref
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tracer_provider = tracer_provider
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def view(self, path: str) -> bytes:
        try:
            validated = self._validate_observed_path(path)
            self._require_native_access()
            async with self._locks[validated.external_path]:
                state = self._require_existing_state(validated, command="view")
                self._require_retrieval_allowed(state.record, validated)
                content = state.content
                if content is None:
                    raise MemoryCallbackIOError(f"view({path!r}) failed: not found")
                self._append_native_adapter_call(
                    command="view",
                    validated=validated,
                    record=state.record,
                    timestamp=self._clock(),
                )
                return content
        except Exception as exc:
            self._emit_native_failure_span(path=path, exc=exc)
            raise

    async def create(self, path: str, content: bytes) -> None:
        try:
            validated = self._validate_observed_path(path)
            self._require_native_access()
            self._require_capture_allowed()
            async with self._locks[validated.external_path]:
                record = self._write_tool_event(
                    command="create",
                    validated=validated,
                    content=content,
                    deleted=False,
                    prior_state=self._latest_state(validated),
                )
                self._append_native_adapter_call(
                    command="create",
                    validated=validated,
                    record=record,
                    timestamp=record.envelope.created_at,
                )
        except Exception as exc:
            self._emit_native_failure_span(path=path, exc=exc)
            raise

    async def migrate_from_callback(
        self,
        path: str,
        content: bytes,
        *,
        migration_id: str,
        source_backend_name: str,
    ) -> MemoryID:
        """Write a callback-backed memory item into the canonical memory root.

        U-MEM-23 keeps migration explicit by recording command ``migrate`` in
        the canonical tool event while reusing the C-MEM-15 native adapter
        durable operation ledger.
        """

        try:
            validated = self._validate_observed_path(path)
            self._require_native_access()
            self._require_capture_allowed()
            async with self._locks[validated.external_path]:
                record = self._write_tool_event(
                    command="migrate",
                    validated=validated,
                    content=content,
                    deleted=False,
                    prior_state=self._latest_state(validated),
                    migration_id=migration_id,
                    migration_source_backend=source_backend_name,
                )
                self._append_native_adapter_call(
                    command="migrate",
                    validated=validated,
                    record=record,
                    timestamp=record.envelope.created_at,
                )
                return record.envelope.memory_id
        except Exception as exc:
            self._emit_native_failure_span(path=path, exc=exc)
            raise

    async def delete(self, path: str) -> None:
        try:
            validated = self._validate_observed_path(path)
            self._require_native_access()
            self._require_capture_allowed()
            async with self._locks[validated.external_path]:
                state = self._latest_state(validated)
                if state is None or state.deleted:
                    return
                self._require_retrieval_allowed(state.record, validated)
                record = self._write_tool_event(
                    command="delete",
                    validated=validated,
                    content=None,
                    deleted=True,
                    prior_state=state,
                )
                self._append_native_adapter_call(
                    command="delete",
                    validated=validated,
                    record=record,
                    timestamp=record.envelope.created_at,
                )
        except Exception as exc:
            self._emit_native_failure_span(path=path, exc=exc)
            raise

    async def str_replace(self, path: str, old: str, new: str) -> None:
        try:
            validated = self._validate_observed_path(path)
            self._require_native_access()
            self._require_capture_allowed()
            async with self._locks[validated.external_path]:
                state = self._require_existing_state(validated, command="str_replace")
                self._require_retrieval_allowed(state.record, validated)
                content = _state_text(state, command="str_replace", path=path)
                if old not in content:
                    raise MemoryCallbackIOError(
                        f"str_replace({path!r}): substring {old!r} not found"
                    )
                replaced = content.replace(old, new).encode(_TEXT_ENCODING)
                record = self._write_tool_event(
                    command="str_replace",
                    validated=validated,
                    content=replaced,
                    deleted=False,
                    prior_state=state,
                )
                self._append_native_adapter_call(
                    command="str_replace",
                    validated=validated,
                    record=record,
                    timestamp=record.envelope.created_at,
                )
        except Exception as exc:
            self._emit_native_failure_span(path=path, exc=exc)
            raise

    async def insert(self, path: str, line: int, content: str) -> None:
        try:
            validated = self._validate_observed_path(path)
            self._require_native_access()
            self._require_capture_allowed()
            async with self._locks[validated.external_path]:
                state = self._require_existing_state(validated, command="insert")
                self._require_retrieval_allowed(state.record, validated)
                existing = _state_text(state, command="insert", path=path)
                lines = existing.splitlines(keepends=True)
                if line < 1 or line > len(lines) + 1:
                    raise MemoryCallbackIOError(
                        f"insert({path!r}, line={line}): out of range (1..{len(lines) + 1})"
                    )
                lines.insert(line - 1, content)
                replaced = "".join(lines).encode(_TEXT_ENCODING)
                record = self._write_tool_event(
                    command="insert",
                    validated=validated,
                    content=replaced,
                    deleted=False,
                    prior_state=state,
                )
                self._append_native_adapter_call(
                    command="insert",
                    validated=validated,
                    record=record,
                    timestamp=record.envelope.created_at,
                )
        except Exception as exc:
            self._emit_native_failure_span(path=path, exc=exc)
            raise

    def _validate_observed_path(self, path: str) -> _ValidatedMemoryPath:
        return _validate_memory_path(path)

    def _require_native_access(self) -> None:
        access = self._policy_resolver.resolve_native_memory()
        if access.access_decision is not AccessDecision.NATIVE_PROVIDER:
            raise MemoryCallbackIOError("native memory policy denies adapter access")

    def _require_capture_allowed(self) -> None:
        capture = self._policy_resolver.resolve_capture()
        if capture.capture_decision is CaptureDecision.DENY:
            raise MemoryCallbackIOError("capture policy denies native memory write")

    def _require_retrieval_allowed(
        self,
        record: MemoryStoreRecord,
        validated: _ValidatedMemoryPath,
    ) -> None:
        if record.envelope.redaction_state is not RedactionState.ACTIVE:
            raise MemoryCallbackIOError(f"memory path {validated.external_path!r} is unavailable")
        access = self._policy_resolver.resolve_retrieval(
            record_kind=record.envelope.kind,
            record_scope=record.envelope.scope,
            requested_scope=self._scope,
        )
        if access.access_decision is AccessDecision.DENY:
            raise MemoryCallbackIOError(
                f"retrieval policy denies memory path {validated.external_path!r}"
            )

    def _require_existing_state(
        self,
        validated: _ValidatedMemoryPath,
        *,
        command: str,
    ) -> _NativeMemoryState:
        state = self._latest_state(validated)
        if state is None or state.deleted:
            raise MemoryCallbackIOError(f"{command}({validated.external_path!r}) failed: not found")
        return state

    def _latest_state(self, validated: _ValidatedMemoryPath) -> _NativeMemoryState | None:
        for entry in reversed(self._store.read_memory_operations()):
            if entry.operation_kind is not MemoryOperationKind.NATIVE_ADAPTER_CALL:
                continue
            for memory_ref in reversed(entry.memory_refs):
                record = self._read_tool_event_record(memory_ref, run_id=entry.run_id)
                if record is None:
                    continue
                if record.envelope.scope != self._scope:
                    continue
                if record.content.get("event_type") != "native_memory_adapter":
                    continue
                if record.content.get("memory_path") != validated.external_path:
                    continue
                return _NativeMemoryState(
                    record=record,
                    content=_content_from_record(record),
                    deleted=record.content.get("deleted") is True,
                )
        return None

    def _read_tool_event_record(
        self,
        memory_ref: MemoryID,
        *,
        run_id: str | None,
    ) -> MemoryStoreRecord | None:
        if run_id is None:
            return None
        try:
            return self._store.read_record(
                memory_ref,
                MemoryRecordKind.TOOL_EVENT,
                run_id=run_id,
                audit_mode=True,
            )
        except Exception:
            return None

    def _write_tool_event(
        self,
        *,
        command: str,
        validated: _ValidatedMemoryPath,
        content: bytes | None,
        deleted: bool,
        prior_state: _NativeMemoryState | None,
        migration_id: str | None = None,
        migration_source_backend: str | None = None,
    ) -> MemoryStoreRecord:
        timestamp = self._clock()
        content_sha256 = hashlib.sha256(content).hexdigest() if content is not None else None
        record_content: dict[str, object] = {
            "event_type": "native_memory_adapter",
            "run_id": self._run_id,
            "step_id": self._step_id,
            "tool_name": _TOOL_NAME,
            "command": command,
            "memory_path": validated.external_path,
            "relative_path": validated.relative_path,
            "content_b64": (
                base64.b64encode(content).decode("ascii") if content is not None else None
            ),
            "content_sha256": content_sha256,
            "content_bytes": len(content) if content is not None else 0,
            "deleted": deleted,
            "provider": self._provider,
            "model": self._model,
            "cli_profile": self._cli_profile,
            "policy_ref": self._policy_ref,
        }
        if migration_id is not None:
            record_content["migration_id"] = migration_id
        if migration_source_backend is not None:
            record_content["migration_source_backend"] = migration_source_backend
        content_hash = compute_memory_content_hash(record_content)
        memory_id = derive_memory_id(MemoryTier.EPISODIC, MemoryRecordKind.TOOL_EVENT, content_hash)
        record = MemoryStoreRecord(
            envelope=MemoryRecordEnvelope(
                memory_id=memory_id,
                schema_version=_SCHEMA_VERSION,
                tier=MemoryTier.EPISODIC,
                kind=MemoryRecordKind.TOOL_EVENT,
                created_at=timestamp,
                updated_at=None,
                source_refs=(
                    SourceRef(
                        ref_type=SourceRefType.TOOL_EVENT,
                        ref=_source_ref(command=command, path=validated.external_path),
                        content_hash=content_hash,
                    ),
                ),
                scope=self._scope,
                content_hash=content_hash,
                supersedes=(
                    (prior_state.record.envelope.memory_id,) if prior_state is not None else ()
                ),
                redaction_state=RedactionState.ACTIVE,
            ),
            content=record_content,
        )
        self._store.write_record(record)
        return record

    def _append_native_adapter_call(
        self,
        *,
        command: str,
        validated: _ValidatedMemoryPath,
        record: MemoryStoreRecord,
        timestamp: datetime,
    ) -> None:
        operation_ordinal = len(self._store.read_memory_operations())
        event_hash = _hash_json(
            {
                "command": command,
                "memory_path": validated.external_path,
                "memory_ref": str(record.envelope.memory_id),
                "content_hash": record.envelope.content_hash.hex(),
                "policy_ref": self._policy_ref,
                "run_id": self._run_id,
                "step_id": self._step_id,
                "operation_ordinal": operation_ordinal,
            }
        )
        self._store.append_memory_operation(
            MemoryOperationPayload(
                action_id=Identifier(f"native-adapter-call:{event_hash[:32]}"),
                idempotency_key=Identifier(f"native-adapter-call:{event_hash}"),
                actor=self._actor,
                timestamp=timestamp,
                operation_kind=MemoryOperationKind.NATIVE_ADAPTER_CALL,
                operation_projection=MemoryOperationProjection.NONE,
                run_id=self._run_id,
                step_id=self._step_id,
                provider=self._provider,
                model=self._model,
                cli_profile=self._cli_profile,
                engine_class=self._engine_class,
                memory_refs=(record.envelope.memory_id,),
                policy_ref=self._policy_ref,
                procedural_snapshot_ref=self._procedural_snapshot_ref,
            )
        )
        self._emit_native_success_span(validated=validated)

    def _emit_native_success_span(self, *, validated: _ValidatedMemoryPath) -> None:
        with memory_telemetry_span(
            self._tracer_provider,
            tracer_name="harness.runtime.native_memory_adapter",
            operation_name=MemoryTelemetryOperationName.NATIVE_ADAPTER_CALL,
            operation_kind=MemoryOperationKind.NATIVE_ADAPTER_CALL.value,
            path=validated.external_path,
            provider=self._provider,
            model=self._model,
            cli_profile=self._cli_profile,
            policy_decision="allowed",
            record_count=1,
        ):
            pass

    def _emit_native_failure_span(self, *, path: str, exc: BaseException) -> None:
        with memory_telemetry_span(
            self._tracer_provider,
            tracer_name="harness.runtime.native_memory_adapter",
            operation_name=MemoryTelemetryOperationName.NATIVE_ADAPTER_CALL,
            operation_kind=MemoryOperationKind.NATIVE_ADAPTER_CALL.value,
            path=path,
            provider=self._provider,
            model=self._model,
            cli_profile=self._cli_profile,
            policy_decision="failed",
            record_count=0,
            failure_class=classify_memory_failure(exc),
        ):
            pass


def _validate_memory_path(path: str) -> _ValidatedMemoryPath:
    if not path.startswith(_MEMORIES_SCOPE):
        raise MemoryPathViolationError(f"path {path!r} not prefixed with {_MEMORIES_SCOPE!r}")

    relative = path[len(_MEMORIES_SCOPE) :]
    if not relative:
        raise MemoryPathViolationError(
            f"path {path!r} resolves to /memories/ directory itself; expected file path"
        )
    if relative.startswith("/"):
        raise MemoryPathViolationError(f"path {path!r} double-slash after /memories/ scope")

    relative_parts = PurePosixPath(relative).parts
    if ".." in relative_parts:
        raise MemoryPathViolationError(f"path {path!r} contains path-traversal segment '..'")

    normalized_relative = PurePosixPath(relative).as_posix()
    return _ValidatedMemoryPath(
        external_path=f"{_MEMORIES_SCOPE}{normalized_relative}",
        relative_path=normalized_relative,
    )


def normalize_native_memory_path(path: str) -> str:
    """Return the canonical `/memories/...` path used by native adapters."""

    return _validate_memory_path(path).external_path


def _state_text(state: _NativeMemoryState, *, command: str, path: str) -> str:
    if state.content is None:
        raise MemoryCallbackIOError(f"{command}({path!r}) failed: not found")
    try:
        return state.content.decode(_TEXT_ENCODING)
    except UnicodeDecodeError as exc:
        raise MemoryCallbackIOError(f"{command}({path!r}) read failed: {exc}") from exc


def _content_from_record(record: MemoryStoreRecord) -> bytes | None:
    raw = record.content.get("content_b64")
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    try:
        return base64.b64decode(raw.encode("ascii"), validate=True)
    except Exception:
        return None


def _source_ref(*, command: str, path: str) -> str:
    digest = _stable_digest(f"{command}\0{path}")
    return f"native-memory:{command}:{digest[:32]}"


def _stable_digest(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _hash_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable_mapping(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _jsonable_mapping(value: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, item in value.items():
        normalized[str(key)] = _jsonable(item)
    return normalized


def _jsonable(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        return _jsonable_mapping(cast("Mapping[str, object]", value))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_jsonable(item) for item in cast("Sequence[object]", value)]
    return str(value)
