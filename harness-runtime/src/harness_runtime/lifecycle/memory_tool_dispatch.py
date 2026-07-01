"""U-RT-81 — C-RT-15 §14.5.1 callback-injection composer-step helpers.

Per `Spec_Harness_Runtime_v1.md` v1.17 §14.5.1 (Memory tool storage-backend
callback binding) + AS spec v1.5 §14.7 `memory.*` 6-attribute namespace +
§14.8 sampling-row for `memory.operation` spans.

Mechanism choice: **β harness-authored inner loop** per plan v2.15 §1 U-RT-81
adjacent-defect (ii) fallback. Empirical SDK check (2026-05-23):
`anthropic.lib.tools._beta_runner.py:644-665` shows `client.beta.messages.
tool_runner(...).until_done()` catches both `ToolError` and bare `Exception`
inside the per-callback loop and renders them as `tool_result` blocks with
`is_error: True`. SDK swallow → mechanism α cannot satisfy AC #5/#6 typed
fail-class propagation. Mechanism β preserves verbatim exception propagation
through the loop boundary — `MemoryPathViolationError` /
`MemoryCallbackIOError` reach the C-RT-15 dispatcher's caller (the driver
`try/except` at `workflow_driver.py:618-635`) per spec §14.5.1 step 5.

Architecture:

- `step_has_memory_tool(payload_tools)` — detection helper checking the
  `tools` list for an element with `type == "memory_20250818"`.
- `MEMORY_COMMAND_KIND` — locked bijection (per plan AC #3) mapping each
  SDK Memory tool command name to the `memory.operation.kind` enum value.
  The spec-prose enum value `"list"` is dead at v1.17 (NO command emits it).
- `derive_context_editing_active(payload_params)` — reads
  `payload.params["context_management"]` for `clear_tool_uses_20250919`
  + `exclude_tools: ["memory"]` shape per AS spec §14.7 row 6.
- `execute_with_memory_callbacks(adapter, model, kwargs, ...)` — async
  inner loop wrapping `messages.create`. Iterates while
  `stop_reason == "tool_use"`, executes harness Protocol callbacks for
  each memory `tool_use` content block, emits one `memory.operation` span
  per callback, builds `tool_result` content blocks, re-dispatches.

Cost-attribution accuracy note (Class 3 adjacent-defect — NOT patched here):
the harness inner loop calls `messages.create` N times until a non-tool-use
response; the existing `_attribute_cost_best_effort(...)` at the dispatcher
only attributes the FINAL response. Intermediate-turn token usage is
under-reported. Future arc owes either (i) aggregate `usage` across loop
iterations, or (ii) emit one cost record per inner `messages.create`. Out
of scope for this arc per FM-2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
    MemoryToolStorageBackendProtocol,
)

__all__ = [
    "MEMORY_COMMAND_KIND",
    "MEMORY_TOOL_TYPE",
    "derive_context_editing_active",
    "execute_with_memory_callbacks",
    "step_has_memory_tool",
]


MEMORY_TOOL_TYPE: Final[str] = "memory_20250818"
"""Anthropic Memory tool client-side type per ADR-D3 v1.2 §1.1 #11 +
runtime spec v1.17 §14.5.1 step 1 detection contract."""


MEMORY_COMMAND_KIND: Final[Mapping[str, str]] = {
    "view": "read",
    "create": "write",
    "str_replace": "update",
    "insert": "update",
    "delete": "delete",
}
"""Locked bijection per plan v2.15 §1 U-RT-81 AC #3 — maps the 5 harness
Protocol callbacks to the `memory.operation.kind` enum value per AS spec
v1.5 §14.7 row 1. The spec-prose enum value `"list"` is dead at v1.17
(NO callback emits it; future spec revision MAY add a `list` callback per
plan v2.15 adjacent-defect (ii))."""


_HEAD_SAMPLED_KINDS: Final[frozenset[str]] = frozenset({"write", "update", "delete"})
"""Audit-floor `kind` values per AS spec v1.5 §14.8 + ADR-D3 v1.2 §1.8.1.
The runtime composer marks the span attribute; downstream OD SpanProcessor
+ Sampler honor the head=1.0 commitment per AS §14.8 audit-floor table."""


def step_has_memory_tool(payload_tools: Sequence[Mapping[str, Any]] | None) -> bool:
    """Return True iff `payload_tools` contains an Anthropic Memory tool
    definition per ADR-D3 v1.2 §1.1 #11.

    Per spec §14.5.1 step 1: composer iterates `step.step_payload.tools`
    and checks for tool element with `type == "memory_20250818"`. If absent,
    the dispatch proceeds without memory-tool wiring per the unchanged
    §14.5 step 4. If present, the dispatcher routes through
    `execute_with_memory_callbacks`.
    """
    if payload_tools is None:
        return False
    for tool in payload_tools:
        if tool.get("type") == MEMORY_TOOL_TYPE:
            return True
    return False


def derive_context_editing_active(payload_params: Mapping[str, Any]) -> bool:
    """Return True iff the parent LLM inference span step has Anthropic context
    management `clear_tool_uses_20250919` edit excluding `"memory"`.

    Per AS spec v1.7 §14.7 row 6: `memory.context_editing_active` is True
    iff the parent (the LLM inference span per AS §14.1 alias-term; runtime
    span-name format owned by OD spec v1.12 §C-OD-04 §4.1) uses
    `clear_tool_uses_20250919` with
    `exclude_tools: ["memory"]` per docs.claude.com [HIGH]. The directive
    rides in `payload.params["context_management"]` per Anthropic's
    `BetaContextManagementConfigParam` shape.

    Shape (per `anthropic.types.beta.BetaContextManagementConfigParam`):

        {"edits": [{"type": "clear_tool_uses_20250919",
                    "exclude_tools": ["memory", ...]}]}

    Permissive read: returns True if any edit element has the right type
    AND the exclude_tools list contains `"memory"`. Returns False on any
    shape mismatch (defensive — context_management is an SDK beta surface
    that may evolve; spec §14.7 row 6 reading is permissive about absent
    sub-keys).
    """
    cm_raw = payload_params.get("context_management")
    if not isinstance(cm_raw, Mapping):
        return False
    cm = cast(Mapping[str, Any], cm_raw)
    edits_raw = cm.get("edits")
    if not isinstance(edits_raw, Sequence):
        return False
    edits = cast(Sequence[Any], edits_raw)
    for edit_raw in edits:
        if not isinstance(edit_raw, Mapping):
            continue
        edit = cast(Mapping[str, Any], edit_raw)
        if edit.get("type") != "clear_tool_uses_20250919":
            continue
        excludes_raw = edit.get("exclude_tools")
        if isinstance(excludes_raw, Sequence):
            excludes = cast(Sequence[Any], excludes_raw)
            if "memory" in excludes:
                return True
    return False


# ---------------------------------------------------------------------------
# Memory-tool inner loop (mechanism β).
# ---------------------------------------------------------------------------


def _content_blocks(response: Any) -> Sequence[Any]:
    """Extract content blocks from an Anthropic `Message` response.

    Provider-SDK Pydantic models expose `.content` as a list of typed
    content blocks; test-fixture stubs use plain dicts. Tolerant of both.
    """
    content: Any = getattr(response, "content", None)
    if content is None and isinstance(response, Mapping):
        content = cast(Mapping[str, Any], response).get("content")
    return cast(Sequence[Any], content) if content else []


def _block_type(block: Any) -> str | None:
    if isinstance(block, Mapping):
        bt: Any = cast(Mapping[str, Any], block).get("type")
    else:
        bt = getattr(block, "type", None)
    return bt if isinstance(bt, str) else None


def _block_attr(block: Any, name: str) -> Any:
    if isinstance(block, Mapping):
        return cast(Mapping[str, Any], block).get(name)
    return getattr(block, name, None)


def _command_field(command_input: Mapping[str, Any], field: str) -> Any:
    """Read a field from a Memory tool command input dict.

    Per `BetaMemoryTool20250818Command` shape: each command carries a
    `command` discriminator + command-specific fields (`path` for all,
    plus `file_text` / `old_str` / `new_str` / `insert_line` / `insert_text`
    per command type).
    """
    return command_input.get(field)


def _stop_reason(response: Any) -> str | None:
    if isinstance(response, Mapping):
        sr: Any = cast(Mapping[str, Any], response).get("stop_reason")
    else:
        sr = getattr(response, "stop_reason", None)
    return sr if isinstance(sr, str) else None


async def _invoke_protocol_callback(
    backend: MemoryToolStorageBackendProtocol,
    command_name: str,
    command_input: Mapping[str, Any],
) -> tuple[str, int | None, int | None]:
    """Invoke the harness Protocol method matching `command_name`.

    Returns `(tool_result_content, bytes_read, bytes_written)` for the
    `tool_result` content block + `memory.bytes_read` / `memory.bytes_written`
    span attribute emission per AS spec §14.7 rows 4-5.

    Raises `MemoryPathViolationError` / `MemoryCallbackIOError` per the
    Protocol contract — propagated VERBATIM through the inner loop to
    the C-RT-15 dispatcher boundary per spec §14.5.1 step 5.

    Per plan AC #3 callback→kind bijection lock: the 5 SDK Memory tool
    commands map to the 5 Protocol methods (`view` / `create` /
    `str_replace` / `insert` / `delete`). The SDK's 6th command `rename`
    is NOT in the harness Protocol per spec §14.12.1; treated as a
    structural-decline per FM-2 (returns an error tool_result without
    invoking any Protocol method).
    """
    path_raw = _command_field(command_input, "path")
    path = path_raw if isinstance(path_raw, str) else ""

    if command_name == "view":
        content = await backend.view(path)
        return (content.decode("utf-8", errors="replace"), len(content), None)
    if command_name == "create":
        body_raw = _command_field(command_input, "file_text")
        body = body_raw if isinstance(body_raw, str) else ""
        body_bytes = body.encode("utf-8")
        await backend.create(path, body_bytes)
        return (f"created {path}", None, len(body_bytes))
    if command_name == "str_replace":
        old_raw = _command_field(command_input, "old_str")
        new_raw = _command_field(command_input, "new_str")
        old_s = old_raw if isinstance(old_raw, str) else ""
        new_s = new_raw if isinstance(new_raw, str) else ""
        await backend.str_replace(path, old_s, new_s)
        return (f"replaced in {path}", None, len(new_s.encode("utf-8")))
    if command_name == "insert":
        line_raw = _command_field(command_input, "insert_line")
        text_raw = _command_field(command_input, "insert_text")
        line = int(line_raw) if isinstance(line_raw, int) else 1
        text = text_raw if isinstance(text_raw, str) else ""
        await backend.insert(path, line, text)
        return (f"inserted at {path}:{line}", None, len(text.encode("utf-8")))
    if command_name == "delete":
        await backend.delete(path)
        return (f"deleted {path}", None, None)

    # `rename` (6th SDK command) + any unknown future command: structural-
    # decline per FM-2 — return error tool_result without invoking any
    # Protocol method. Avoids leaking SDK-only commands as silent
    # Protocol extensions per X-AL-3.
    return (
        f"Memory tool command {command_name!r} not implemented by harness "
        f"storage-backend protocol (v1.17 §14.12.1 enumerates 5 callbacks: "
        f"view / create / str_replace / insert / delete)",
        None,
        None,
    )


def _emit_memory_operation_span(
    tracer: Any,
    *,
    kind: str,
    path: str,
    backend_enum: MemoryToolStorageBackend,
    bytes_read: int | None,
    bytes_written: int | None,
    context_editing_active: bool,
) -> Any:
    """Open a `memory.operation` span carrying the AS spec §14.7
    6-attribute namespace.

    Returns the OTel `Span` context manager so the caller can wrap the
    Protocol callback invocation. Sets all 6 attributes pre-yield; the
    head-sampling decision (head=1.0 at mutation kinds per AS §14.8
    audit-floor) is realized by setting the `sampling.head_rate` attribute
    that downstream Sampler honors — the actual sampling decision is the
    OD-side `harness_od.tail_keep_sampler` responsibility per ADR-D3 v1.2
    §1.8.1 audit-floor commitment.
    """
    span_cm = tracer.start_as_current_span("memory.operation")
    return _MemoryOperationSpanContext(
        span_cm=span_cm,
        kind=kind,
        path=path,
        backend_enum=backend_enum,
        bytes_read=bytes_read,
        bytes_written=bytes_written,
        context_editing_active=context_editing_active,
    )


class _MemoryOperationSpanContext:
    """Context manager wrapping the OTel span CM with attribute population.

    `__enter__` opens the span and sets all 6 AS spec §14.7 attributes
    pre-yield. The `bytes_read` / `bytes_written` may be `None` on
    callback failure paths where the callback bails before computing
    the value; the span still emits with the partial attribute set.
    """

    def __init__(
        self,
        *,
        span_cm: Any,
        kind: str,
        path: str,
        backend_enum: MemoryToolStorageBackend,
        bytes_read: int | None,
        bytes_written: int | None,
        context_editing_active: bool,
    ) -> None:
        self._span_cm = span_cm
        self._kind = kind
        self._path = path
        self._backend_enum = backend_enum
        self._bytes_read = bytes_read
        self._bytes_written = bytes_written
        self._context_editing_active = context_editing_active
        self._span: Any = None

    def __enter__(self) -> Any:
        self._span = self._span_cm.__enter__()
        # 6-attribute namespace per AS spec v1.5 §14.7.
        self._span.set_attribute("memory.operation.kind", self._kind)
        self._span.set_attribute("memory.path", self._path)
        self._span.set_attribute("memory.backend", self._backend_enum.value)
        if self._bytes_read is not None:
            self._span.set_attribute("memory.bytes_read", self._bytes_read)
        if self._bytes_written is not None:
            self._span.set_attribute("memory.bytes_written", self._bytes_written)
        self._span.set_attribute("memory.context_editing_active", self._context_editing_active)
        # Audit-floor commitment per AS spec §14.8 + ADR-D3 §1.8.1 — mutation
        # kinds head-sampled at 1.0. The attribute is read by the OD-side
        # Sampler; the span emission lifecycle here is unaffected.
        if self._kind in _HEAD_SAMPLED_KINDS:
            self._span.set_attribute("sampling.head_rate", 1.0)
        return self._span

    def update_bytes(self, *, read: int | None, written: int | None) -> None:
        """Set bytes_read / bytes_written attributes post-callback when the
        callback's return shape determines the values (e.g., `view`'s
        bytes-read is the length of the returned content)."""
        if self._span is None:
            return
        if read is not None:
            self._span.set_attribute("memory.bytes_read", read)
        if written is not None:
            self._span.set_attribute("memory.bytes_written", written)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._span_cm.__exit__(exc_type, exc, tb)


async def execute_with_memory_callbacks(
    *,
    adapter: Any,
    model: str,
    messages_create_kwargs: Mapping[str, Any],
    backend: MemoryToolStorageBackendProtocol,
    backend_enum: MemoryToolStorageBackend,
    tracer: Any,
    context_editing_active: bool,
    max_iterations: int = 16,
) -> Mapping[str, Any]:
    """Harness-authored inner loop (mechanism β) wrapping `messages.create`.

    Per spec §14.5.1 step 3 + plan v2.15 §1 U-RT-81 — calls `messages.create`,
    inspects response for `tool_use` blocks invoking the Memory tool,
    executes the matching Protocol callback under a `memory.operation` span,
    appends the `tool_result` content block to messages, re-dispatches.
    Returns the final response (the first response whose `stop_reason` is
    not `tool_use`).

    Per spec §14.5.1 step 5 + §14.12.4: `MemoryPathViolationError` and
    `MemoryCallbackIOError` raised by the backend Protocol callback
    propagate VERBATIM through the loop — the driver `try/except` at
    `workflow_driver.py:618-635` catches and maps to `step-failure:
    RT-FAIL-MEMORY-{PATH-VIOLATION,CALLBACK-IO}: {exc}` per C-CP-25
    §25.3.3.4 contract.

    `max_iterations` bounds the loop depth as a defensive cap against
    runaway tool-use sequences; matches the spirit of the SDK's
    `tool_runner(max_iterations=...)` param. Default 16 mirrors common
    Anthropic SDK fixture values.
    """
    messages = list(messages_create_kwargs.get("messages", []))
    other_kwargs: dict[str, Any] = {
        k: v for k, v in messages_create_kwargs.items() if k != "messages"
    }

    response: Mapping[str, Any] | Any = None
    for _ in range(max_iterations):
        response = await adapter.client.messages.create(
            model=model, messages=messages, **other_kwargs
        )

        if _stop_reason(response) != "tool_use":
            # Final response — return mapping form (caller pipeline coerces
            # any Pydantic response via `_response_to_mapping` separately).
            return cast(Mapping[str, Any], response)

        # Append the assistant message verbatim so the next request carries
        # the tool_use blocks the model is responding to.
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": list(_content_blocks(response)),
        }
        messages.append(assistant_message)

        # Collect tool_result blocks for ALL tool_use blocks invoking the
        # Memory tool in this turn. Non-memory tool_use blocks are ignored
        # here (other composers / future arcs may handle them); the harness
        # surfaces a generic "tool not handled" tool_result so the SDK loop
        # doesn't stall on an unanswered tool_use.
        tool_results: list[dict[str, Any]] = []
        for block in _content_blocks(response):
            if _block_type(block) != "tool_use":
                continue
            tool_use_id = _block_attr(block, "id")
            tool_name = _block_attr(block, "name")
            command_input_raw = _block_attr(block, "input")
            if not isinstance(command_input_raw, Mapping):
                command_input_raw = {}
            command_input = cast(Mapping[str, Any], command_input_raw)

            if tool_name != "memory":
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": (
                            f"Tool {tool_name!r} not handled by Memory tool "
                            f"composer-step (C-RT-15 §14.5.1)"
                        ),
                        "is_error": True,
                    }
                )
                continue

            command_name_raw = command_input.get("command")
            command_name = command_name_raw if isinstance(command_name_raw, str) else ""
            kind = MEMORY_COMMAND_KIND.get(command_name, command_name or "unknown")
            path_arg = command_input.get("path")
            path_str = path_arg if isinstance(path_arg, str) else ""

            # Open span; invoke Protocol; on harness typed exceptions
            # propagate verbatim through the loop boundary per spec §14.5.1
            # step 5. Span attributes are best-effort populated on entry
            # (bytes counters may update post-callback for `view`).
            span_ctx = _emit_memory_operation_span(
                tracer,
                kind=kind,
                path=path_str,
                backend_enum=backend_enum,
                bytes_read=None,
                bytes_written=None,
                context_editing_active=context_editing_active,
            )
            with span_ctx:
                content, bytes_read, bytes_written = await _invoke_protocol_callback(
                    backend, command_name, command_input
                )
                span_ctx.update_bytes(read=bytes_read, written=bytes_written)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                    }
                )

        messages.append({"role": "user", "content": tool_results})

    # max_iterations exhausted — return the last response (best-effort; this
    # mirrors the SDK runner's behavior when its own `max_iterations` is hit).
    return cast(Mapping[str, Any], response)


# Re-export typed exceptions so callers (llm_dispatch.py) can catch the
# 2 harness-typed exceptions at a single import surface.
__all__ = [
    "MEMORY_COMMAND_KIND",
    "MEMORY_TOOL_TYPE",
    "MemoryCallbackIOError",
    "MemoryPathViolationError",
    "derive_context_editing_active",
    "execute_with_memory_callbacks",
    "step_has_memory_tool",
]
