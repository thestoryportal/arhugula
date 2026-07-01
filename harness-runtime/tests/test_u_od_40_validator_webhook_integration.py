"""U-OD-40 AC #5 integration — 1 validator dispatch + 1 webhook dispatch → 2 cost records.

End-to-end exercise of:
1. ConcreteValidatorFramework wired with CostAttributingValidatorHook via
   the v1.24 factory mechanism (a) signature widening — calling evaluate()
   triggers the hook → cost-attribution chain → 1 audit-ledger entry.
2. WebhookDeliveryComposer inline-wrap — calling deliver_webhook() triggers
   the best-effort cost-attribution → 1 audit-ledger entry.

Both surfaces share a single shared `_RecordingAuditWriter` to verify the
final cardinality assertion (2 audit entries; one per surface).

Authority: U-OD-40 AC #5 + CP spec v1.24 §28.10 + OD spec v1.8 §C-OD-26.2.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core.identity import StepID
from harness_cp.hitl_timeout_degradation import WebhookConfig, WebhookPayload
from harness_cp.sub_agent_gate_level_descent import GateLevel
from harness_cp.validator_framework import ConcreteValidatorFramework
from harness_cp.validator_framework_types import (
    ValidatorOutcome,
    ValidatorResult,
)
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_od.rate_table_types import RateTable, WebhookRate
from harness_runtime.lifecycle.cost_attribution import RuntimeCostAttributionChain
from harness_runtime.lifecycle.cost_attribution_validator_dispatch import (
    CostAttributingValidatorHook,
)
from harness_runtime.lifecycle.webhook_delivery_composer import (
    WebhookDeliveryComposer,
)


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.appended: list[tuple[str | None, object]] = []

    def append(self, tenant_id: str | None, audit_entry: object) -> object:
        self.appended.append((tenant_id, audit_entry))
        return "appended"


class _FixedValidator:
    def __init__(self, result: ValidatorResult) -> None:
        self._result = result

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorResult:
        return self._result


def _make_rate_table() -> RateTable:
    return RateTable(
        version="2026-05-28-integration",
        providers={},
        tool_rates={},
        webhook_rate=WebhookRate(
            flat_per_attempt=Decimal("0.01"),
            plus_egress=False,
        ),
        cpu_rate_per_ms=Decimal("0.001"),
        egress_rate_per_byte=Decimal("0"),
    )


def _make_step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-1"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={},
    )


def _make_step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-integration",
        parent_action_id="workflow:wf-integration:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="integration-test"),
        parent_entry_hash="",
        parent_idempotency_key="parent-idem-1",
        tenant_id=None,
        step_index=0,
    )


@pytest.mark.asyncio
async def test_one_validator_plus_one_webhook_produces_two_cost_records() -> None:
    """U-OD-40 AC #5 — 1 validator + 1 webhook → 2 cost-records.

    Shared `_RecordingAuditWriter` receives both audit-ledger entries;
    asserts cardinality + per-entry action_id-prefix discrimination.
    """
    rate_table = _make_rate_table()
    cost_chain = RuntimeCostAttributionChain()
    audit_writer = _RecordingAuditWriter()

    # --- Validator surface ---
    step = _make_step()
    ctx = _make_step_context()
    validator = _FixedValidator(
        ValidatorResult(
            outcome=ValidatorOutcome.PASS,
            fail_class=None,
            fail_detail_hash=None,
        )
    )
    hook = CostAttributingValidatorHook(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
    )
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )
    await fw.evaluate(step, {}, step_context=ctx)

    # --- Webhook surface ---
    # Mock httpx client that returns 200 OK — exercises success path of
    # deliver_webhook + triggers cost-attribution at the end.
    class _MockResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _MockAsyncClient:
        def __init__(self) -> None:
            pass

        async def __aenter__(self) -> _MockAsyncClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(200)

    composer = WebhookDeliveryComposer(
        retry_max_attempts=1,
        http_client_factory=lambda: _MockAsyncClient(),  # type: ignore[arg-type]
        sleep_fn=lambda _s: asyncio.sleep(0),
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        workflow_id="wf-integration",
        parent_action_id="hitl:wf-integration:gate:0",
        parent_idempotency_key="parent-idem-1",
    )

    webhook_config = WebhookConfig(
        webhook_id="test-webhook",
        endpoint_url="https://ops.example.com/hitl",
        timeout=30.0,
        degradation_mode="proceed",
    )
    webhook_payload = WebhookPayload(
        approval_id="approval-1",
        idempotency_key="webhook-1",
        gate_evaluation_ref="gate-eval-1",
        payload_body={"summary": "approval needed"},
    )

    await composer.deliver_webhook(webhook_config, webhook_payload, idempotency_key="webhook-1")

    # --- Assertions ---
    assert len(audit_writer.appended) == 2, (
        f"expected 2 audit-ledger entries (1 validator + 1 webhook); "
        f"got {len(audit_writer.appended)}"
    )

    # Both entries route via cost: action_id prefix (CXA v2.9 §0.3 row 8 /
    # OD spec v1.10 §C-OD-26.6.1 step 2).
    for _tenant_id, audit_entry in audit_writer.appended:
        attrs = audit_entry.payload.audit_namespace_attrs
        assert attrs["audit.cp.action_id"].startswith("cost:"), (
            f"audit entry must use cost: action_id prefix; got {attrs['audit.cp.action_id']!r}"
        )
        assert attrs["audit.cp.response"] == "cost_attributed"

    # Verify per-surface action_id correlation:
    action_ids = sorted(
        e[1].payload.audit_namespace_attrs["audit.cp.action_id"] for e in audit_writer.appended
    )
    # Validator: cost:wf-integration:workflow:wf-integration:step:0
    # Webhook:   cost:wf-integration:hitl:wf-integration:gate:0
    assert any("workflow:" in a for a in action_ids), (
        f"validator audit entry expected with workflow: parent_action_id; "
        f"got action_ids={action_ids}"
    )
    assert any("hitl:" in a for a in action_ids), (
        f"webhook audit entry expected with hitl: parent_action_id; got action_ids={action_ids}"
    )


@pytest.mark.asyncio
async def test_validator_and_webhook_append_to_run_scoped_cost_sink() -> None:
    """R-FS-1 arc CA — the validator hook + webhook composer each append their
    returned `SpanCostRecord` into the shared run-scoped `cost_record_sink` (the
    same list `_build_run_result` rolls up into `RunResult.cost_attribution`,
    runtime spec v1.53 §9 C-RT-09). Wiring-by-execution for two of the four
    per-dispatch cost wrappers (LLM in test_ca_cost_aggregate.py; tool in the
    tool-dispatcher cost test)."""
    rate_table = _make_rate_table()
    cost_chain = RuntimeCostAttributionChain()
    audit_writer = _RecordingAuditWriter()
    # The single run-scoped accumulator threaded into BOTH surfaces.
    sink: list[Any] = []

    # --- Validator surface (sink threaded in) ---
    step = _make_step()
    ctx = _make_step_context()
    validator = _FixedValidator(
        ValidatorResult(outcome=ValidatorOutcome.PASS, fail_class=None, fail_detail_hash=None)
    )
    hook = CostAttributingValidatorHook(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        cost_record_sink=sink,
    )
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )
    await fw.evaluate(step, {}, step_context=ctx)

    assert len(sink) == 1, "validator dispatch must append exactly one cost record"

    # --- Webhook surface (same sink threaded in) ---
    class _MockResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _MockAsyncClient:
        async def __aenter__(self) -> _MockAsyncClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(200)

    composer = WebhookDeliveryComposer(
        retry_max_attempts=1,
        http_client_factory=lambda: _MockAsyncClient(),  # type: ignore[arg-type]
        sleep_fn=lambda _s: asyncio.sleep(0),
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        cost_record_sink=sink,
        workflow_id="wf-integration",
        parent_action_id="hitl:wf-integration:gate:0",
        parent_idempotency_key="parent-idem-1",
    )
    webhook_config = WebhookConfig(
        webhook_id="test-webhook",
        endpoint_url="https://ops.example.com/hitl",
        timeout=30.0,
        degradation_mode="proceed",
    )
    webhook_payload = WebhookPayload(
        approval_id="approval-1",
        idempotency_key="webhook-1",
        gate_evaluation_ref="gate-eval-1",
        payload_body={"summary": "approval needed"},
    )
    await composer.deliver_webhook(webhook_config, webhook_payload, idempotency_key="webhook-1")

    # Both surfaces appended into the one shared run-scoped accumulator.
    assert len(sink) == 2, "validator + webhook each append one record to the shared sink"
    # v1.30 — the dispatch type now lives in `dispatch_kind` (provider_discriminator
    # is None per-dispatch). StrEnum sorts/compares by value.
    dispatch_kinds = sorted(r.dispatch_kind.value for r in sink)
    assert dispatch_kinds == ["validator", "webhook"]
    assert all(r.provider_discriminator is None for r in sink)
