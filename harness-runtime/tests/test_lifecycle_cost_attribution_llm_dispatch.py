"""U-OD-38 — cost-attribution at LLM dispatch site tests.

ACs per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-38:
  #1 Cost-attribution invoked on every LLM dispatch (success + failure paths)
  #2 Cost-record uses gen_ai.usage.input_tokens + output_tokens per GenAI
     semconv 1.41.0
  #3 Idempotency-key attached pre-audit-write
  #4 PRICE_TABLE_REF resolution failure falls back per Decision (raises per
     §C-OD-28.2 default fail-closed)
  #5 1 LLM call → 1 cost-record + 1 audit-ledger entry
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier, StepID
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
from harness_od.cost_record_otel_serializer import COST_ATTRIBUTED_DECIMAL_ATTR
from harness_od.rate_table_resolver import RateTableMissingError
from harness_od.rate_table_v1 import RATE_TABLE_V1
from harness_runtime.lifecycle.cost_attribution import RuntimeCostAttributionChain
from harness_runtime.lifecycle.cost_attribution_llm_dispatch import (
    attribute_llm_dispatch_cost,
)
from harness_runtime.lifecycle.llm_dispatch import (
    RuntimeLLMDispatcher,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RecordingAuditWriter:
    """Captures every audit-ledger append for AC #5 1-call-1-write assertion."""

    def __init__(self) -> None:
        self.appended: list[tuple[str | None, object]] = []

    def append(self, tenant_id: str | None, audit_entry: object) -> object:
        self.appended.append((tenant_id, audit_entry))
        return "appended"


@pytest.fixture
def cost_chain() -> RuntimeCostAttributionChain:
    return RuntimeCostAttributionChain()


@pytest.fixture
def audit_writer() -> _RecordingAuditWriter:
    return _RecordingAuditWriter()


# ---------------------------------------------------------------------------
# AC #1 + AC #2 — Cost-attribution invoked; cost-record uses usage attrs
# ---------------------------------------------------------------------------


def test_attribute_llm_dispatch_cost_returns_attached_record_for_anthropic(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    attached = attribute_llm_dispatch_cost(
        rate_table=RATE_TABLE_V1,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        provider_name="anthropic",
        model="claude-haiku-4-5",
        span_id="0123456789abcdef",
        parent_idempotency_key="parent-idem-1",
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
        input_tokens=1000,
        output_tokens=500,
    )
    assert attached.span_id == "0123456789abcdef"
    assert attached.idempotency_key == "parent-idem-1"  # AC #3
    assert attached.gen_ai_provider_name == "anthropic"
    assert attached.gen_ai_request_model == "claude-haiku-4-5"
    assert attached.provider_discriminator is None  # v1.30 — no chain-level family tag
    assert attached.dispatch_kind == "llm"  # v1.30 — the PER_DISPATCH_KIND key
    # AC #2 — cost uses usage attrs; per claude-haiku-4-5 override $1/MTok in + $5/MTok out:
    # cost = 1000 * (1.00 / 1e6) + 500 * (5.00 / 1e6) = 0.001 + 0.0025 = 0.0035
    assert attached.total_cost == pytest.approx(0.0035, rel=1e-6)


def test_attribute_llm_dispatch_cost_writes_audit_entry(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """AC #5 — 1 LLM call → 1 cost-record + 1 audit-ledger entry."""
    attribute_llm_dispatch_cost(
        rate_table=RATE_TABLE_V1,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        provider_name="anthropic",
        model="claude-haiku-4-5",
        span_id="abcdef0123456789",
        parent_idempotency_key="parent-1",
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
        input_tokens=100,
        output_tokens=50,
    )
    assert len(audit_writer.appended) == 1
    tenant_id, audit_entry = audit_writer.appended[0]
    assert tenant_id is None
    # Audit entry shape per cp_audit_to_od_audit converter
    assert hasattr(audit_entry, "payload")
    assert hasattr(audit_entry, "entry_hash")
    attrs = audit_entry.payload.audit_namespace_attrs
    # Per OD spec v1.10 §C-OD-26.6.1 step 2 canonical pattern:
    # cost:<workflow_id>:<step_action_id>; response=cost_attributed
    assert attrs["audit.cp.action_id"] == "cost:test-wf:workflow:test-wf:step:0"
    assert attrs["audit.cp.response"] == "cost_attributed"


# ---------------------------------------------------------------------------
# AC #3 — Idempotency key attached pre-audit-write
# ---------------------------------------------------------------------------


def test_idempotency_key_attached_before_audit_write(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """Cost-record idempotency_key must be the parent's join key per
    C-IS-05 / C-OD-14 §14.4 BEFORE the audit-ledger write."""
    attached = attribute_llm_dispatch_cost(
        rate_table=RATE_TABLE_V1,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        provider_name="openai",
        model="gpt-5",
        span_id="ffff000011112222",
        parent_idempotency_key="WORKFLOW-PARENT-KEY-42",
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
        input_tokens=10,
        output_tokens=5,
    )
    # Parent's idempotency_key is attached on the returned record
    assert attached.idempotency_key == "WORKFLOW-PARENT-KEY-42"
    # AND the audit ledger received exactly 1 entry
    assert len(audit_writer.appended) == 1


# ---------------------------------------------------------------------------
# AC #4 — PRICE_TABLE_REF resolution failure raises (fail-closed default)
# ---------------------------------------------------------------------------


def test_unknown_provider_raises_rate_table_missing(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """Per §C-OD-28.2: resolution failure raises CP-FAIL-RATE-TABLE-MISSING
    by default (fail-closed); operator may flip to fail-open via bootstrap
    config (not at v1 scope)."""
    with pytest.raises(RateTableMissingError) as exc_info:
        attribute_llm_dispatch_cost(
            rate_table=RATE_TABLE_V1,
            cost_chain=cost_chain,
            audit_writer=audit_writer,
            provider_name="cohere",  # not in RATE_TABLE_V1
            model="command-r",
            span_id="0000000000000000",
            parent_idempotency_key="parent-x",
            workflow_id="test-wf",
            parent_action_id="workflow:test-wf:step:0",
            input_tokens=1,
            output_tokens=1,
        )
    assert "CP-FAIL-RATE-TABLE-MISSING" in str(exc_info.value)
    # No audit entry written on failure
    assert audit_writer.appended == []


# ---------------------------------------------------------------------------
# Provider coverage — all 3 ADR-F1 providers
# ---------------------------------------------------------------------------


def test_cost_attribution_works_for_all_3_adr_f1_providers(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    for provider in ("anthropic", "openai", "ollama"):
        attribute_llm_dispatch_cost(
            rate_table=RATE_TABLE_V1,
            cost_chain=cost_chain,
            audit_writer=audit_writer,
            provider_name=provider,
            model=f"test-{provider}-model",
            span_id=f"{'a' * 16}",
            parent_idempotency_key=f"parent-{provider}",
            workflow_id="test-wf",
            parent_action_id="workflow:test-wf:step:0",
            input_tokens=100,
            output_tokens=50,
        )
    assert len(audit_writer.appended) == 3


# ---------------------------------------------------------------------------
# AC #1 — End-to-end via RuntimeLLMDispatcher with cost-attribution wiring
# ---------------------------------------------------------------------------


_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-cost-attr")


class _FakeAnthropicAdapter:
    """Mock anthropic adapter returning a fixed usage shape."""

    def __init__(self) -> None:
        self.client = MagicMock()
        usage = MagicMock(
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        response = MagicMock(usage=usage, id="msg_test_001")
        response.model_dump = lambda: {"id": "msg_test_001", "content": []}

        async def _create(model: str, **kwargs: Any) -> Any:
            return response

        self.client.messages.create = _create


def test_end_to_end_dispatch_emits_cost_attribution_audit_entry(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """AC #1 + AC #5 end-to-end: RuntimeLLMDispatcher.dispatch on a mocked
    anthropic provider produces exactly 1 audit-ledger entry."""
    import asyncio

    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": _FakeAnthropicAdapter()},
        tracer_provider=tp,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        rate_table=RATE_TABLE_V1,
    )
    binding = StepEffectiveBinding(
        step_id="step-0",
        model_binding=_DEFAULT_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    step = WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "params": {"max_tokens": 1},
        },
    )
    step_context = StepExecutionContext(
        workflow_id="wf",
        parent_action_id="workflow:wf:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=_ACTOR,
        parent_entry_hash="",
        parent_idempotency_key="parent-e2e-key",
        tenant_id=None,
        step_index=0,
    )
    asyncio.run(dispatcher.dispatch(binding, step, step_context=step_context))

    # AC #5 — exactly 1 audit-ledger entry written
    assert len(audit_writer.appended) == 1

    # cost.attributed_decimal OTel attribute emitted on the dispatch span
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    cost_attr = (spans[0].attributes or {}).get(COST_ATTRIBUTED_DECIMAL_ATTR)
    assert cost_attr is not None
    assert isinstance(cost_attr, str)
    # Round-trip the Decimal-form string
    recovered = Decimal(cost_attr)
    # haiku rates: 1000 * 1.00/1e6 + 500 * 5.00/1e6 = 0.0035
    assert recovered == pytest.approx(Decimal("0.0035"), rel=Decimal("1e-6"))


def test_dispatcher_without_cost_substrate_silently_skips_cost_attribution() -> None:
    """Backward-compat: dispatcher constructed without cost_chain/audit_writer/
    rate_table proceeds without cost-attribution (unit-test ergonomics)."""
    import asyncio

    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": _FakeAnthropicAdapter()},
        tracer_provider=tp,
        # cost-attribution substrate omitted
    )
    binding = StepEffectiveBinding(
        step_id="step-0",
        model_binding=_DEFAULT_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    step = WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "params": {"max_tokens": 1},
        },
    )
    step_context = StepExecutionContext(
        workflow_id="wf",
        parent_action_id="workflow:wf:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=_ACTOR,
        parent_entry_hash="",
        parent_idempotency_key="parent-x",
        tenant_id=None,
        step_index=0,
    )
    # Should complete without raising
    asyncio.run(dispatcher.dispatch(binding, step, step_context=step_context))


_ = Mapping
