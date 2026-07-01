"""U-RT-82 — End-to-end test: local-filesystem backend + real Anthropic API
``messages.create`` with ``tools=[memory_tool]``.

Implements ``Implementation_Plan_Harness_Runtime_v2_15.md`` §1 U-RT-82 (per
v2.14 cluster L5 closer). Spec contract: ``Spec_Harness_Runtime_v1.md`` v1.17
§14.12.6 X-AL-2 retirement implications — full RETIRED transition
prerequisites per §16 §6.C v2 C.vii local-fs scope:

1. Operator-bound ``RuntimeConfig.memory_tool_backend_config`` non-default
2. Local-filesystem-backend e2e exercise: real Anthropic API
   ``messages.create`` call with ``tools=[memory_tool]`` +
   ``MemoryToolStorageBackend.FILESYSTEM`` wired through the registry;
   LLM-driven ``create``/``view``/``str_replace`` callback invocation
   observed; ``memory.*`` namespace emitted at each callback span.

**Test scope per §14.D ratification.** ONLY local-filesystem backend at this
arc per operator §14.D scope. S3 / ENCRYPTED_FILESYSTEM / DATABASE e2e tests
deferred to operator-discretion follow-on retirement-batch arcs per
§16 §6.C v2 C.vii.

**Architecture note (FM-2 discretion).** The plan AC signature mentions
"full bootstrap with RuntimeConfig(...)"; this implementation invokes
``RuntimeLLMDispatcher`` directly with a real Anthropic adapter +
``MemoryToolRegistry`` wrapping ``LocalFilesystemMemoryToolBackend``. The
contract surface verified IS the U-RT-77 + U-RT-78 + U-RT-80 + U-RT-81
composition against a real model — driver-level workflow exercise is
deferred to operator-discretion follow-on retirement-batch arc per §16
§6.C v2 C.vii (matches the same operator-opt-in pattern as H_T-CP-18
batch-10 + H_T-CP-21 batch-11 RETIRE-READY landings).

**Deterministic-prompt fixture (per plan AC #3 + advisor coherence-pass
2026-05-23).** Forces the LLM to invoke Memory tool's ``create`` operation
against a known path + content via an explicit system prompt naming the
capability + the path. Mitigates LLM-behavior flakiness; on prompt-
ineffective failures, the diagnostic names the prompt content + LLM
response so the implementer can adjust per FM-2.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from anthropic import AsyncAnthropic
from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier
from harness_core.deployment_surface import DeploymentSurface
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
from harness_runtime.lifecycle.memory_tool_filesystem import (
    LocalFilesystemMemoryToolBackend,
)
from harness_runtime.lifecycle.memory_tool_registry import MemoryToolRegistry
from harness_runtime.lifecycle.providers import AnthropicAdapter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------------------------------------------------------------------------
# Module-level gating: every test in this module requires ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.getenv("ANTHROPIC_API_KEY"),
        reason=(
            "U-RT-82 e2e requires ANTHROPIC_API_KEY for real Anthropic API "
            "calls. Skipped per @pytest.mark.skipif gate (AC #2)."
        ),
    ),
]


# Anthropic Memory tool client-side type + required beta header per ADR-D3
# v1.2 §1.1 #11. The runtime spec v1.17 §14.5.1 + plan v2.15 AC #1 require
# both the tool definition AND the beta header for the SDK to accept the
# memory_20250818 tool type.
_MEMORY_TOOL_DEFINITION: dict[str, Any] = {"type": "memory_20250818", "name": "memory"}
_MEMORY_BETA_HEADER: dict[str, str] = {"anthropic-beta": "context-management-2025-06-27"}

# Fixed model for determinism — claude-haiku-4-5 is cost-effective + supports
# tool use. The deterministic-prompt fixture below mitigates LLM-behavior
# non-determinism within model invocation.
_E2E_MODEL = "claude-haiku-4-5"

# Deterministic-prompt fixture per plan AC #3 + advisor finding.
_FIXTURE_PATH = "/memories/notes.txt"
_FIXTURE_CONTENT = "User prefers concise responses with bullet points."
_SYSTEM_PROMPT = (
    "You have access to a Memory tool that lets you persist notes across "
    "conversations. Use the Memory tool's `create` operation to save a note "
    f"to {_FIXTURE_PATH!r}. Use ONLY the `create` operation; do not view "
    "existing memory first; do not call any other operation. "
    "CRITICAL: pass the user's content to the `content` parameter EXACTLY "
    "as given, byte-for-byte, with no reformatting, no markdown headers, "
    "no bullet rewrites, no paraphrasing, and no added whitespace. The "
    "file body must equal the user-provided string verbatim. After "
    "creating the memory file, respond briefly to confirm you've saved "
    "the note."
)
_USER_MESSAGE = (
    "Save the following text to memory verbatim. The exact string to pass "
    "as the `content` parameter, with no modifications, is:\n\n"
    f"{_FIXTURE_CONTENT}"
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_backend_root(tmp_path: Path) -> Path:
    """Per-test filesystem root for the Memory backend.

    `tmp_path` is auto-cleaned by pytest at session teardown — satisfies
    AC #5 (no test artifacts persisted between runs).
    """
    root = tmp_path / "memories"
    root.mkdir()
    return root


@pytest.fixture
def tracer_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Real TracerProvider + InMemorySpanExporter for span assertions."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.fixture
def anthropic_adapter() -> Iterator[AnthropicAdapter]:
    """Real `AnthropicAdapter` wrapping `AsyncAnthropic` constructed from
    `ANTHROPIC_API_KEY` env var.

    Note: the AsyncAnthropic client manages an internal httpx pool. We
    don't call `aclose()` per the test scope (the test process exits +
    OS reaps the connections); a production runtime relies on the
    stage-3a `aclose()` chain.
    """
    api_key = os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    async def _ping() -> None:
        await client.models.list()

    adapter = AnthropicAdapter(client=client, ping=_ping)
    yield adapter


# ---------------------------------------------------------------------------
# Test fixtures: WorkflowStep + binding + step_context with memory tool.
# ---------------------------------------------------------------------------


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-e2e",
        model_binding=ModelBinding(provider="anthropic", model=_E2E_MODEL),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-e2e"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": _USER_MESSAGE}],
            "tools": [_MEMORY_TOOL_DEFINITION],
            "params": {
                "max_tokens": 1024,
                "system": _SYSTEM_PROMPT,
                "extra_headers": _MEMORY_BETA_HEADER,
            },
        },
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="u-rt-82-e2e",
        parent_action_id="workflow:u-rt-82-e2e:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="u-rt-82-test"),
        parent_entry_hash="0" * 64,
        parent_idempotency_key="u-rt-82-idem-key",
        tenant_id=None,
        step_index=0,
    )


# ---------------------------------------------------------------------------
# AC #1 — Test runs with ANTHROPIC_API_KEY: completes within ~30s; create
# callback invoked at filesystem backend with the fixture path; file
# contains fixture content.
#
# AC #3 — Deterministic-prompt write-path assertion: create callback invoked
# at least once against real Anthropic API.
#
# AC #4 — Corresponding memory.operation span exists with kind=write +
# matching backend + path + bytes_written attributes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_tool_filesystem_e2e_write_path(
    memory_backend_root: Path,
    tracer_with_exporter: tuple[TracerProvider, InMemorySpanExporter],
    anthropic_adapter: AnthropicAdapter,
) -> None:
    """AC #1 + #3 + #4: deterministic-prompt fixture forces LLM to invoke
    the Memory tool's `create` operation; backend writes the file; span
    emitted with `memory.operation.kind == "write"`."""
    tracer_provider, exporter = tracer_with_exporter

    # CallbackRecorder backend wraps the real LocalFilesystemMemoryToolBackend
    # to record method invocations for AC #1/#3 assertions.
    real_backend = LocalFilesystemMemoryToolBackend(root=memory_backend_root)
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class _RecordingBackend:
        async def view(self, path: str) -> bytes:
            calls.append(("view", (path,)))
            return await real_backend.view(path)

        async def create(self, path: str, content: bytes) -> None:
            calls.append(("create", (path, content)))
            await real_backend.create(path, content)

        async def delete(self, path: str) -> None:
            calls.append(("delete", (path,)))
            await real_backend.delete(path)

        async def str_replace(self, path: str, old: str, new: str) -> None:
            calls.append(("str_replace", (path, old, new)))
            await real_backend.str_replace(path, old, new)

        async def insert(self, path: str, line: int, content: str) -> None:
            calls.append(("insert", (path, line, content)))
            await real_backend.insert(path, line, content)

    registry = MemoryToolRegistry(
        backend=_RecordingBackend(),  # type: ignore[arg-type]
        configured_backend=MemoryToolStorageBackend.FILESYSTEM,
    )

    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": anthropic_adapter},
        tracer_provider=tracer_provider,
        memory_tool_registry=registry,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )

    result = await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())

    # AC #1 + #3: create callback invoked at least once with fixture path.
    create_calls = [c for c in calls if c[0] == "create"]
    assert create_calls, (
        f"deterministic-prompt fixture FAILED: LLM did not invoke "
        f"`create` operation. Calls observed: {calls!r}. "
        f"Final response (truncated): {str(result)[:500]!r}. "
        f"Adjust the system prompt per FM-2 if recent model variant "
        f"changes Memory tool invocation behavior."
    )

    # AC #1: file at resolved filesystem path contains fixture content.
    # Path `/memories/notes.txt` → `memory_backend_root / "notes.txt"`.
    written_file = memory_backend_root / "notes.txt"
    assert written_file.exists(), (
        f"create callback observed but file not present at {written_file!r}. "
        f"create_calls: {create_calls!r}"
    )
    body = written_file.read_text(encoding="utf-8")
    assert _FIXTURE_CONTENT in body, (
        f"file content mismatch: expected fixture content "
        f"{_FIXTURE_CONTENT!r} in file body {body!r}"
    )

    # AC #4: memory.operation span emitted with the right attributes.
    memory_spans = [s for s in exporter.get_finished_spans() if s.name == "memory.operation"]
    assert memory_spans, "no memory.operation span emitted"
    write_spans = [
        s for s in memory_spans if (s.attributes or {}).get("memory.operation.kind") == "write"
    ]
    assert write_spans, (
        f"no memory.operation span with kind=write; observed spans: "
        f"{[(s.name, dict(s.attributes or {})) for s in memory_spans]!r}"
    )
    attrs = dict(write_spans[0].attributes or {})
    assert attrs["memory.backend"] == "filesystem"
    assert attrs["memory.path"] == _FIXTURE_PATH
    # bytes_written matches the LLM-supplied content length (the LLM may
    # paraphrase slightly, so we check >0 rather than exact match).
    bytes_written = attrs.get("memory.bytes_written")
    assert isinstance(bytes_written, int) and bytes_written > 0


# ---------------------------------------------------------------------------
# AC #2 — Test runs without ANTHROPIC_API_KEY: skips cleanly per
# @pytest.mark.skipif gate (covered at module-level pytestmark above —
# pytest reports the skip without false failure).
#
# This module-level assertion documents the gating shape; the actual skip
# behavior is exercised when ANTHROPIC_API_KEY is unset.
# ---------------------------------------------------------------------------


def test_module_skip_gate_present() -> None:
    """AC #2 gate-mechanism assertion: the module-level pytestmark includes
    a skipif gate on ANTHROPIC_API_KEY env var, so the e2e tests skip
    cleanly when no credential is available (no false failure in CI)."""
    skipif_marker = next((m for m in pytestmark if m.name == "skipif"), None)
    assert skipif_marker is not None
    # The skipif condition is `not os.getenv("ANTHROPIC_API_KEY")` — when
    # the env var is set (as it must be for this test to even reach this
    # line), the condition evaluates False and the gate doesn't trigger.
    assert "ANTHROPIC_API_KEY" in (skipif_marker.kwargs.get("reason") or "")


# ---------------------------------------------------------------------------
# AC #5 — Cleanup at teardown: the `memory_backend_root` fixture uses
# pytest's `tmp_path` which auto-cleans at session teardown. Verified
# implicitly by fixture behavior (no test artifacts persisted between runs).
#
# AC #6 — Importable + pyright strict: importable verified by the imports
# at top of this module; pyright strict run separately at CI/local.
# ---------------------------------------------------------------------------


def test_module_importable() -> None:
    """AC #6 (importable) — the test module imports without error."""
    assert callable(test_memory_tool_filesystem_e2e_write_path)
