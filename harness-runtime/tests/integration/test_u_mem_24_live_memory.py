"""U-MEM-24 live Anthropic native-memory confirmation.

This module is intentionally marked e2e and skip-gated on ANTHROPIC_API_KEY.
It exercises the hosted Anthropic Memory tool against the canonical native
adapter backend and then asserts the canonical memory-operation ledger, rather
than treating the live provider call as evidence by itself.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from anthropic import AsyncAnthropic
from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend
from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import MemoryOperationKind
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_policy import (
    AccessDecision,
    CaptureDecision,
    MemoryPolicyDocument,
    MemoryPolicyResolver,
    PromotionDecision,
    ReviewMode,
)
from harness_is.memory_record_envelope import MemoryRecordKind, MemoryScope, MemoryVisibility
from harness_is.memory_store import CanonicalMemoryStore
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.memory_tool_dispatch import execute_with_memory_callbacks
from harness_runtime.lifecycle.native_memory_adapter import CanonicalNativeMemoryToolBackend
from harness_runtime.lifecycle.providers import AnthropicAdapter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.getenv("ANTHROPIC_API_KEY"),
        reason="U-MEM-24 Anthropic native-memory live gate requires ANTHROPIC_API_KEY",
    ),
]

_MODEL = "claude-haiku-4-5"
_NOW = datetime(2026, 7, 3, 18, 0, 0, tzinfo=UTC)
_POLICY_REF = "policy:u-mem-24-live"
_MEMORY_TOOL_DEFINITION: dict[str, Any] = {"type": "memory_20250818", "name": "memory"}
_MEMORY_BETA_HEADER: dict[str, str] = {"anthropic-beta": "context-management-2025-06-27"}
_FIXTURE_PATH = "/memories/u-mem-24/live-native-memory.txt"
_FIXTURE_CONTENT = "U-MEM-24 live canonical native memory adapter confirmation."
_SYSTEM_PROMPT = (
    "You have access to a Memory tool. Use the Memory tool's create operation "
    f"exactly once to write the user's text to {_FIXTURE_PATH!r}. Do not call "
    "view, str_replace, insert, delete, or any non-memory tool. Pass the user's "
    "text as the file_text exactly, with no paraphrasing or extra whitespace. "
    "After the tool call succeeds, respond briefly."
)
_USER_MESSAGE = f"Save this exact text to memory: {_FIXTURE_CONTENT}"


def _scope() -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow="memory-substrate",
        workload_class="coding-arc",
        provider_family="anthropic",
        cli_profile="claude",
        visibility=MemoryVisibility.WORKFLOW,
    )


def _policy() -> MemoryPolicyDocument:
    return MemoryPolicyDocument(
        policy_id=_POLICY_REF,
        enabled=True,
        capture_decision=CaptureDecision.CAPTURE_FULL,
        promotion_decision=PromotionDecision.KEEP_EPISODIC,
        retrieval_access=AccessDecision.RETRIEVAL_ONLY,
        native_memory_access=AccessDecision.NATIVE_PROVIDER,
        review_mode=ReviewMode.OPERATOR_REQUIRED,
    )


@pytest.mark.asyncio
async def test_anthropic_native_memory_live_writes_canonical_adapter_ledger(
    tmp_path: Path,
) -> None:
    store = CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    tracer_provider = TracerProvider()
    exporter = InMemorySpanExporter()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    backend = CanonicalNativeMemoryToolBackend(
        store=store,
        policy_resolver=MemoryPolicyResolver(_policy()),
        scope=_scope(),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="u-mem-24-live"),
        run_id="run-u-mem-24-live",
        step_id="step-native-memory-live",
        provider="anthropic",
        model=_MODEL,
        cli_profile="claude",
        policy_ref=_POLICY_REF,
        clock=lambda: _NOW,
        tracer_provider=tracer_provider,
    )
    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    adapter = AnthropicAdapter(client=client, ping=lambda: client.models.list())
    try:
        await execute_with_memory_callbacks(
            adapter=adapter,
            model=_MODEL,
            messages_create_kwargs={
                "messages": [{"role": "user", "content": _USER_MESSAGE}],
                "max_tokens": 1024,
                "system": _SYSTEM_PROMPT,
                "tools": [_MEMORY_TOOL_DEFINITION],
                "extra_headers": _MEMORY_BETA_HEADER,
            },
            backend=backend,
            backend_enum=MemoryToolStorageBackend.OPERATOR_DEFINED,
            tracer=tracer_provider.get_tracer(__name__),
            context_editing_active=False,
            max_iterations=4,
        )
    finally:
        await adapter.aclose()

    operations = store.read_memory_operations()
    assert operations, "Anthropic Memory tool did not invoke the native adapter"
    assert all(
        entry.operation_kind is MemoryOperationKind.NATIVE_ADAPTER_CALL for entry in operations
    )

    matching_records = []
    for operation in operations:
        for memory_ref in operation.memory_refs:
            record = store.read_record(
                memory_ref,
                MemoryRecordKind.TOOL_EVENT,
                run_id="run-u-mem-24-live",
                audit_mode=True,
            )
            if record.content.get("memory_path") == _FIXTURE_PATH:
                matching_records.append(record)

    assert matching_records, f"no canonical TOOL_EVENT record for {_FIXTURE_PATH!r}"
    create_records = [r for r in matching_records if r.content.get("command") == "create"]
    assert create_records, f"no create record observed; commands={operations!r}"
    created = create_records[-1]
    assert (
        created.content["content_sha256"]
        == hashlib.sha256(_FIXTURE_CONTENT.encode("utf-8")).hexdigest()
    )
    assert base64.b64decode(str(created.content["content_b64"])).decode("utf-8") == _FIXTURE_CONTENT

    spans = [span for span in exporter.get_finished_spans() if span.name == "memory.operation"]
    assert spans
    assert any(
        dict(span.attributes or {}).get("memory.operation.name") == "native_adapter_call"
        for span in spans
    )
