"""U-OD-40 — cost-attribution at validator.evaluate site tests.

ACs per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-40:
  #1 Validator cost uses CPU-meter (execution_time_ms × $/CPU_ms) per
     Decision 2.D5 RATIFIED.
  #3 Cost-record attached at span exit
  #4 Audit-ledger entry written

Per CP spec v1.24 §28.10 the helper is invoked from the
ValidatorPostEvaluateHook firing site at ConcreteValidatorFramework.evaluate
(post-construction pre-return; best-effort exception swallow).

AC #2 (webhook cost) + AC #5 (integration: 1 validator + 1 webhook →
2 cost-records) covered separately in the webhook test module + the
factory-binding integration test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from harness_od.rate_table_types import RateTable, WebhookRate
from harness_runtime.lifecycle.cost_attribution import RuntimeCostAttributionChain
from harness_runtime.lifecycle.cost_attribution_validator_dispatch import (
    _compute_validator_cost,
    attribute_validator_dispatch_cost,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.appended: list[tuple[str | None, object]] = []

    def append(self, tenant_id: str | None, audit_entry: object) -> object:
        self.appended.append((tenant_id, audit_entry))
        return "appended"


def _make_rate_table(cpu_rate_per_ms: Decimal) -> RateTable:
    """RateTable with operator-supplied cpu_rate_per_ms substrate."""
    return RateTable(
        version="2026-05-28-test",
        providers={},
        tool_rates={},
        webhook_rate=WebhookRate(flat_per_attempt=Decimal("0"), plus_egress=False),
        cpu_rate_per_ms=cpu_rate_per_ms,
        egress_rate_per_byte=Decimal("0"),
    )


@pytest.fixture
def cost_chain() -> RuntimeCostAttributionChain:
    return RuntimeCostAttributionChain()


@pytest.fixture
def audit_writer() -> _RecordingAuditWriter:
    return _RecordingAuditWriter()


# ---------------------------------------------------------------------------
# AC #1 — CPU-meter formula (Decision 2.D5 RATIFIED)
# ---------------------------------------------------------------------------


def test_compute_validator_cost_cpu_meter_integer_ms() -> None:
    """AC #1 — cost = cpu_rate_per_ms × execution_time_ms (integer ms case)."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0.001"))
    cost = _compute_validator_cost(rate_table, execution_time_ms=42.0)
    # 42 ms × 0.001 = 0.042
    assert cost == Decimal("0.042")


def test_compute_validator_cost_cpu_meter_fractional_ms() -> None:
    """AC #1 — sub-millisecond precision preserved via str(float) coercion."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0.005"))
    cost = _compute_validator_cost(rate_table, execution_time_ms=3.5)
    # 3.5 ms × 0.005 = 0.0175 (preserved as Decimal)
    assert cost == Decimal("0.0175")
    assert isinstance(cost, Decimal)


def test_compute_validator_cost_zero_elapsed() -> None:
    """Edge: zero elapsed time → zero cost (validator returns immediately)."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("1.0"))
    cost = _compute_validator_cost(rate_table, execution_time_ms=0.0)
    assert cost == Decimal("0.0")


def test_compute_validator_cost_zero_rate() -> None:
    """Edge: zero cpu_rate (free-tier) → zero cost regardless of elapsed."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0"))
    cost = _compute_validator_cost(rate_table, execution_time_ms=1000.0)
    assert cost == Decimal("0")


def test_compute_validator_cost_decimal_precision_preserved() -> None:
    """§C-OD-28.4 invariant 2 — Decimal-precision-preserving arithmetic."""
    # 17 sig-digit rate × fractional-ms elapsed — float would lose precision
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0.12345678901234567"))
    cost = _compute_validator_cost(rate_table, execution_time_ms=7.5)
    expected = Decimal("0.12345678901234567") * Decimal("7.5")
    assert cost == expected
    assert isinstance(cost, Decimal)


# ---------------------------------------------------------------------------
# AC #3 + AC #4 — full chain returns attached record + 1 audit write
# ---------------------------------------------------------------------------


def test_attribute_validator_dispatch_cost_returns_attached_record(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """Helper returns idempotency-key-bearing SpanCostRecord per AC #3."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0.01"))
    attached = attribute_validator_dispatch_cost(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        validator_id="schema-validator",
        execution_time_ms=10.0,
        span_id="abcdef0123456789",
        idempotency_key="validator-idem-1",
        parent_idempotency_key="parent-idem-1",
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
    )
    assert attached.span_id == "abcdef0123456789"
    assert attached.idempotency_key == "parent-idem-1"  # joins to parent
    assert attached.provider_discriminator is None  # v1.30 — no chain-level family tag
    assert attached.dispatch_kind == "validator"  # v1.30 — the PER_DISPATCH_KIND key
    assert attached.gen_ai_provider_name == "validator:schema-validator"
    assert attached.gen_ai_request_model == ""
    assert attached.total_cost == pytest.approx(0.1, rel=1e-9)  # 10 ms × 0.01
    assert attached.total_latency_ms == 10


def test_attribute_validator_dispatch_cost_writes_audit_entry(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """AC #4 — Cost-record attached + audit-ledger entry written."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0.001"))
    attribute_validator_dispatch_cost(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        validator_id="schema-validator",
        execution_time_ms=5.0,
        span_id="0011223344556677",
        idempotency_key="validator-idem-1",
        parent_idempotency_key="parent-1",
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
    )
    assert len(audit_writer.appended) == 1
    tenant_id, audit_entry = audit_writer.appended[0]
    assert tenant_id is None
    assert hasattr(audit_entry, "payload")
    assert hasattr(audit_entry, "entry_hash")
    attrs = audit_entry.payload.audit_namespace_attrs
    # cost: action_id prefix per CXA v2.9 §0.3 row 8 / OD spec v1.10 §C-OD-26.6.1 step 2
    assert attrs["audit.cp.action_id"] == "cost:test-wf:workflow:test-wf:step:0"
    assert attrs["audit.cp.response"] == "cost_attributed"


def test_attribute_validator_dispatch_cost_writes_with_tenant(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """Multi-tenant routing: tenant_id propagates to audit_writer.append."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0.001"))
    attribute_validator_dispatch_cost(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        validator_id="v1",
        execution_time_ms=1.0,
        span_id="x" * 16,
        idempotency_key="k",
        parent_idempotency_key="p",
        workflow_id="wf",
        parent_action_id="action",
        tenant_id="tenant-A",
    )
    assert audit_writer.appended[0][0] == "tenant-A"


def test_three_validator_dispatches_produce_three_audit_writes(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """3 sequential validator dispatches → 3 cost-records + 3 audit entries."""
    rate_table = _make_rate_table(cpu_rate_per_ms=Decimal("0.001"))
    for i in range(3):
        attribute_validator_dispatch_cost(
            rate_table=rate_table,
            cost_chain=cost_chain,
            audit_writer=audit_writer,
            validator_id=f"validator-{i}",
            execution_time_ms=float(i + 1),
            span_id=f"{i:016x}",
            idempotency_key=f"key-{i}",
            parent_idempotency_key=f"parent-{i}",
            workflow_id="wf",
            parent_action_id=f"workflow:wf:step:{i}",
        )
    assert len(audit_writer.appended) == 3
    # Each audit entry has distinct action_id (per-step parent_action_id)
    action_ids = [
        e[1].payload.audit_namespace_attrs["audit.cp.action_id"] for e in audit_writer.appended
    ]
    assert len(set(action_ids)) == 3
