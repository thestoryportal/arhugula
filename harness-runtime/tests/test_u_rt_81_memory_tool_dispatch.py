"""U-RT-81 — C-RT-15 §14.5.1 callback-injection composer-step tests.

ACs per ``Implementation_Plan_Harness_Runtime_v2_15.md`` §1 U-RT-81 (revised
at v2.15 per F2-02 + F2-03 + F2-04 absorptions). Spec contract:
``Spec_Harness_Runtime_v1.md`` v1.17 §14.5.1 (Memory tool storage-backend
callback binding) + AS spec v1.5 §14.7 `memory.*` 6-attribute namespace +
§14.8 sampling-row for `memory.operation` spans + §14.12.4 fail-class
taxonomy.

Mechanism: β (harness-authored inner loop) per plan v2.15 adjacent-defect (ii)
fallback — empirical SDK check confirms `client.beta.messages.tool_runner`
swallows exceptions inside the callback loop (`_beta_runner.py:644-665`),
which would mask AC #5/#6 typed fail-class propagation under mechanism α.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier
from harness_core.identity import StepID
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher
from harness_runtime.lifecycle.memory_tool_dispatch import (
    MEMORY_COMMAND_KIND,
    MEMORY_TOOL_TYPE,
    derive_context_editing_active,
    execute_with_memory_callbacks,
    step_has_memory_tool,
)
from harness_runtime.lifecycle.memory_tool_filesystem import (
    LocalFilesystemMemoryToolBackend,
)
from harness_runtime.lifecycle.memory_tool_registry import MemoryToolRegistry
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers.
# ---------------------------------------------------------------------------


@pytest.fixture
def tracer_provider_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    """OTel tracer provider wired to an in-memory exporter for span assertions."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _registry(backend: Any) -> MemoryToolRegistry:
    return MemoryToolRegistry(
        backend=backend,
        configured_backend=MemoryToolStorageBackend.FILESYSTEM,
    )


def _binding(provider: str = "anthropic", model: str = "claude-sonnet-4-5") -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-001",
        model_binding=ModelBinding(provider=provider, model=model),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="test"),
        parent_entry_hash="0" * 64,
        parent_idempotency_key="idem-key-001",
        tenant_id=None,
        step_index=0,
    )


def _step_with_memory_tool() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-mem"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "test"}],
            "tools": [{"type": MEMORY_TOOL_TYPE, "name": "memory"}],
            "params": {"max_tokens": 1024},
        },
    )


def _step_without_memory_tool() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-plain"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "test"}],
            "tools": None,
            "params": {"max_tokens": 1024},
        },
    )


class _FakeAnthropicResponse:
    """Minimal Anthropic Message-shaped response for inner-loop tests."""

    def __init__(
        self,
        *,
        content: list[dict[str, Any]],
        stop_reason: str = "end_turn",
        input_tokens: int = 5,
        output_tokens: int = 7,
        msg_id: str = "msg-test",
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.id = msg_id
        self.usage = type(
            "_U",
            (),
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
            },
        )()

    def model_dump(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "stop_reason": self.stop_reason,
            "id": self.id,
        }


class _FakeMessages:
    """Records messages.create calls; yields scripted responses."""

    def __init__(self, responses: list[_FakeAnthropicResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeAnthropicResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessages: no more scripted responses")
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


class _FakeAdapter:
    def __init__(self, messages: _FakeMessages) -> None:
        self.client = _FakeClient(messages)


# ---------------------------------------------------------------------------
# Unit-level helpers.
# ---------------------------------------------------------------------------


def test_step_has_memory_tool_detects_memory_20250818() -> None:
    assert step_has_memory_tool(({"type": MEMORY_TOOL_TYPE, "name": "memory"},)) is True


def test_step_has_memory_tool_returns_false_for_absent_or_other() -> None:
    assert step_has_memory_tool(None) is False
    assert step_has_memory_tool(()) is False
    assert step_has_memory_tool(({"type": "function", "name": "other"},)) is False


def test_memory_command_kind_lock_per_ac_3() -> None:
    """AC #3 — callback→kind bijection LOCKED at plan-body."""
    assert MEMORY_COMMAND_KIND == {
        "view": "read",
        "create": "write",
        "str_replace": "update",
        "insert": "update",
        "delete": "delete",
    }
    assert (
        "list" not in MEMORY_COMMAND_KIND.values()
        or sum(1 for v in MEMORY_COMMAND_KIND.values() if v == "list") == 0
    )


def test_derive_context_editing_active_true_on_clear_tool_uses_memory_excluded() -> None:
    params = {
        "context_management": {
            "edits": [
                {
                    "type": "clear_tool_uses_20250919",
                    "exclude_tools": ["memory"],
                }
            ]
        }
    }
    assert derive_context_editing_active(params) is True


def test_derive_context_editing_active_false_on_absent_or_wrong_shape() -> None:
    assert derive_context_editing_active({}) is False
    assert derive_context_editing_active({"context_management": None}) is False
    assert derive_context_editing_active({"context_management": {"edits": []}}) is False
    # exclude_tools doesn't contain "memory"
    assert (
        derive_context_editing_active(
            {
                "context_management": {
                    "edits": [
                        {
                            "type": "clear_tool_uses_20250919",
                            "exclude_tools": ["files"],
                        }
                    ]
                }
            }
        )
        is False
    )


# ---------------------------------------------------------------------------
# AC #2 — Callback wiring: tool_use(create) → backend.create invoked with args.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_wiring_create_invokes_backend(tmp_path: Path) -> None:
    backend = LocalFilesystemMemoryToolBackend(root=tmp_path)
    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu-1",
                        "name": "memory",
                        "input": {
                            "command": "create",
                            "path": "/memories/notes.txt",
                            "file_text": "hello",
                        },
                    }
                ],
                stop_reason="tool_use",
            ),
            _FakeAnthropicResponse(
                content=[{"type": "text", "text": "done"}],
                stop_reason="end_turn",
            ),
        ]
    )
    adapter = _FakeAdapter(msgs)
    tracer = TracerProvider().get_tracer("test")

    result = await execute_with_memory_callbacks(
        adapter=adapter,
        model="claude-sonnet-4-5",
        messages_create_kwargs={"messages": [{"role": "user", "content": "hi"}]},
        backend=backend,
        backend_enum=MemoryToolStorageBackend.FILESYSTEM,
        tracer=tracer,
        context_editing_active=False,
    )

    assert (tmp_path / "notes.txt").read_text() == "hello"
    # Final response has stop_reason="end_turn".
    assert result.stop_reason == "end_turn"  # type: ignore[attr-defined]
    # Two messages.create calls — initial + post-tool-result.
    assert len(msgs.calls) == 2


# ---------------------------------------------------------------------------
# AC #3 — memory.* namespace emission + locked kind bijection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_emits_memory_operation_span_with_write_kind(
    tmp_path: Path,
    tracer_provider_with_exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    tp, exporter = tracer_provider_with_exporter
    backend = LocalFilesystemMemoryToolBackend(root=tmp_path)
    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu-c",
                        "name": "memory",
                        "input": {
                            "command": "create",
                            "path": "/memories/foo",
                            "file_text": "hi",
                        },
                    }
                ],
                stop_reason="tool_use",
            ),
            _FakeAnthropicResponse(
                content=[{"type": "text", "text": "ok"}], stop_reason="end_turn"
            ),
        ]
    )
    adapter = _FakeAdapter(msgs)

    await execute_with_memory_callbacks(
        adapter=adapter,
        model="claude-sonnet-4-5",
        messages_create_kwargs={"messages": [{"role": "user", "content": "x"}]},
        backend=backend,
        backend_enum=MemoryToolStorageBackend.FILESYSTEM,
        tracer=tp.get_tracer("test"),
        context_editing_active=False,
    )

    memory_spans = [s for s in exporter.get_finished_spans() if s.name == "memory.operation"]
    assert len(memory_spans) == 1
    attrs = dict(memory_spans[0].attributes or {})
    assert attrs["memory.operation.kind"] == "write"
    assert attrs["memory.path"] == "/memories/foo"
    assert attrs["memory.backend"] == "filesystem"
    assert attrs["memory.bytes_written"] == len(b"hi")
    assert attrs["memory.context_editing_active"] is False
    assert attrs["memory.operation.name"] == "native_adapter_call"
    assert attrs["memory.provider"] == "anthropic"
    assert attrs["memory.model"] == "claude-sonnet-4-5"
    assert attrs["memory.policy.decision"] == "allowed"
    assert attrs["memory.record_count"] == 1
    # Audit-floor commitment per AS §14.8 — mutation kind head-sampled at 1.0.
    assert attrs["sampling.head_rate"] == 1.0


# ---------------------------------------------------------------------------
# AC #4 — read + insert + update + delete branches emit correct kinds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,kind,input_extras",
    [
        ("view", "read", {}),
        ("str_replace", "update", {"old_str": "hello", "new_str": "world"}),
        ("insert", "update", {"insert_line": 1, "insert_text": "x\n"}),
        ("delete", "delete", {}),
    ],
)
async def test_each_command_emits_correct_kind(
    tmp_path: Path,
    tracer_provider_with_exporter: tuple[TracerProvider, InMemorySpanExporter],
    command: str,
    kind: str,
    input_extras: dict[str, Any],
) -> None:
    tp, exporter = tracer_provider_with_exporter
    backend = LocalFilesystemMemoryToolBackend(root=tmp_path)
    # Pre-create the file for view/str_replace/insert/delete commands.
    (tmp_path / "foo").write_text("hello")

    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu",
                        "name": "memory",
                        "input": {
                            "command": command,
                            "path": "/memories/foo",
                            **input_extras,
                        },
                    }
                ],
                stop_reason="tool_use",
            ),
            _FakeAnthropicResponse(
                content=[{"type": "text", "text": "ok"}], stop_reason="end_turn"
            ),
        ]
    )
    adapter = _FakeAdapter(msgs)

    await execute_with_memory_callbacks(
        adapter=adapter,
        model="claude-sonnet-4-5",
        messages_create_kwargs={"messages": [{"role": "user", "content": "x"}]},
        backend=backend,
        backend_enum=MemoryToolStorageBackend.FILESYSTEM,
        tracer=tp.get_tracer("test"),
        context_editing_active=False,
    )

    memory_spans = [s for s in exporter.get_finished_spans() if s.name == "memory.operation"]
    assert len(memory_spans) == 1
    attrs = dict(memory_spans[0].attributes or {})
    assert attrs["memory.operation.kind"] == kind


# ---------------------------------------------------------------------------
# AC #5 — RT-FAIL-MEMORY-CALLBACK-IO propagation.
# ---------------------------------------------------------------------------


class _IOFailingBackend:
    async def view(self, path: str) -> bytes:
        return b""

    async def create(self, path: str, content: bytes) -> None:
        raise MemoryCallbackIOError(f"create({path!r}) simulated I/O failure")

    async def delete(self, path: str) -> None:
        pass

    async def str_replace(self, path: str, old: str, new: str) -> None:
        pass

    async def insert(self, path: str, line: int, content: str) -> None:
        pass


@pytest.mark.asyncio
async def test_callback_io_error_propagates_verbatim() -> None:
    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu",
                        "name": "memory",
                        "input": {
                            "command": "create",
                            "path": "/memories/x",
                            "file_text": "hi",
                        },
                    }
                ],
                stop_reason="tool_use",
            )
        ]
    )
    adapter = _FakeAdapter(msgs)
    tracer = TracerProvider().get_tracer("test")

    with pytest.raises(MemoryCallbackIOError, match="simulated I/O failure"):
        await execute_with_memory_callbacks(
            adapter=adapter,
            model="claude-sonnet-4-5",
            messages_create_kwargs={"messages": []},
            backend=_IOFailingBackend(),
            backend_enum=MemoryToolStorageBackend.FILESYSTEM,
            tracer=tracer,
            context_editing_active=False,
        )


# ---------------------------------------------------------------------------
# AC #6 — RT-FAIL-MEMORY-PATH-VIOLATION propagation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_violation_propagates_verbatim(tmp_path: Path) -> None:
    # Real LocalFilesystemMemoryToolBackend raises MemoryPathViolationError on
    # an out-of-scope path; the inner loop must surface it verbatim.
    backend = LocalFilesystemMemoryToolBackend(root=tmp_path)
    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu",
                        "name": "memory",
                        "input": {
                            "command": "view",
                            "path": "/etc/passwd",  # not /memories/
                        },
                    }
                ],
                stop_reason="tool_use",
            )
        ]
    )
    adapter = _FakeAdapter(msgs)
    tracer = TracerProvider().get_tracer("test")

    with pytest.raises(MemoryPathViolationError):
        await execute_with_memory_callbacks(
            adapter=adapter,
            model="claude-sonnet-4-5",
            messages_create_kwargs={"messages": []},
            backend=backend,
            backend_enum=MemoryToolStorageBackend.FILESYSTEM,
            tracer=tracer,
            context_editing_active=False,
        )


# ---------------------------------------------------------------------------
# AC #7 — Secret-redaction invariant: memory.* span has memory.path (structure)
# but no memory.content (content bytes).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_span_emits_path_not_content(
    tmp_path: Path,
    tracer_provider_with_exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    tp, exporter = tracer_provider_with_exporter
    backend = LocalFilesystemMemoryToolBackend(root=tmp_path)
    secret = "TOPSECRET-12345"
    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu",
                        "name": "memory",
                        "input": {
                            "command": "create",
                            "path": "/memories/secrets",
                            "file_text": secret,
                        },
                    }
                ],
                stop_reason="tool_use",
            ),
            _FakeAnthropicResponse(
                content=[{"type": "text", "text": "ok"}], stop_reason="end_turn"
            ),
        ]
    )
    adapter = _FakeAdapter(msgs)

    await execute_with_memory_callbacks(
        adapter=adapter,
        model="claude-sonnet-4-5",
        messages_create_kwargs={"messages": []},
        backend=backend,
        backend_enum=MemoryToolStorageBackend.FILESYSTEM,
        tracer=tp.get_tracer("test"),
        context_editing_active=False,
    )

    memory_spans = [s for s in exporter.get_finished_spans() if s.name == "memory.operation"]
    assert len(memory_spans) == 1
    attrs = dict(memory_spans[0].attributes or {})
    assert "memory.path" in attrs
    assert "memory.content" not in attrs
    # The secret content MUST NOT appear in any span attribute value (per AS §14.9).
    for v in attrs.values():
        assert secret not in str(v), f"secret leaked into span attribute: {v!r}"


# ---------------------------------------------------------------------------
# AC #8 — Backwards-compat for non-memory dispatches: existing §14.5 path
# unchanged when memory tool absent in step_payload.tools.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_memory_dispatch_skips_memory_branch() -> None:
    """RuntimeLLMDispatcher.dispatch with no memory tool in payload.tools
    calls messages.create exactly once (no inner loop)."""
    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[{"type": "text", "text": "hello"}],
                stop_reason="end_turn",
            )
        ]
    )
    adapter = _FakeAdapter(msgs)
    tp = TracerProvider()

    # Construct dispatcher with registry+surface bound — the detect-memory
    # branch should still take the no-memory path when payload.tools doesn't
    # contain memory_20250818.
    backend = LocalFilesystemMemoryToolBackend(root=Path("/tmp"))
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        memory_tool_registry=_registry(backend),
        deployment_surface=object(),  # sentinel — never resolved on no-memory path
    )

    await dispatcher.dispatch(_binding(), _step_without_memory_tool(), step_context=_step_context())

    assert len(msgs.calls) == 1


# ---------------------------------------------------------------------------
# AC #1 — Detect-memory-tool branch routes through the helper.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_dispatch_routes_through_inner_loop(tmp_path: Path) -> None:
    """RuntimeLLMDispatcher.dispatch with memory_20250818 in payload.tools
    routes through `execute_with_memory_callbacks` (verified via inner-loop
    behavior: 2 messages.create calls, one tool_use turn + one final)."""
    backend = LocalFilesystemMemoryToolBackend(root=tmp_path)
    msgs = _FakeMessages(
        [
            _FakeAnthropicResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu",
                        "name": "memory",
                        "input": {
                            "command": "create",
                            "path": "/memories/note",
                            "file_text": "x",
                        },
                    }
                ],
                stop_reason="tool_use",
            ),
            _FakeAnthropicResponse(
                content=[{"type": "text", "text": "done"}],
                stop_reason="end_turn",
            ),
        ]
    )
    adapter = _FakeAdapter(msgs)
    tp = TracerProvider()

    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        memory_tool_registry=_registry(backend),
        deployment_surface=object(),
    )

    result = await dispatcher.dispatch(
        _binding(), _step_with_memory_tool(), step_context=_step_context()
    )

    assert len(msgs.calls) == 2
    assert isinstance(result, Mapping)


# ---------------------------------------------------------------------------
# AC #9 — Importable + pyright strict pass (importable verified by imports
# above; pyright strict run separately).
# ---------------------------------------------------------------------------


def test_module_importable() -> None:
    from harness_runtime.lifecycle import memory_tool_dispatch

    assert callable(memory_tool_dispatch.execute_with_memory_callbacks)
    assert callable(memory_tool_dispatch.step_has_memory_tool)
    assert callable(memory_tool_dispatch.derive_context_editing_active)
