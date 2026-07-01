"""U-OD-39 — cost-attribution at tool dispatch site tests.

ACs per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-39:
  #1 Cost-attribution invoked on every tool dispatch (success + failure)
  #2 Tool-rate resolution per ToolRate.cost_kind formulas:
     flat_per_invocation → cost = rate
     per_input_byte → cost = rate × len(canonical_json(tool_args))
     per_output_byte → cost = rate × len(canonical_json(response))
     All arithmetic in Decimal per §C-OD-28.4 invariant 2.
  #3 mcp.tool.call cost piggybacks on parent tool.dispatch (per §C-OD-26.2)
  #4 Cost-record attached + audit-ledger entry written
  #5 Integration test: 1 tool call → 1 cost-record per each of 3 cost_kind
     values exercised + cost arithmetic verified Decimal-precision-preserving

AC #3 is verified at the production binding-arc (invocation site at
RuntimeToolDispatcher.dispatch); at the module-layer this test file
verifies that 1 invocation produces 1 cost-record per the helper contract
(no separate mcp.tool.call invocation here — that's the dispatcher-side
concern at U-OD-39 production binding arc to follow).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from harness_od.rate_table_types import RateTable, ToolRate, WebhookRate
from harness_runtime.lifecycle.cost_attribution import RuntimeCostAttributionChain
from harness_runtime.lifecycle.cost_attribution_tool_dispatch import (
    ToolRateMissingError,
    _canonical_json_byte_length,
    _compute_tool_cost,
    _resolve_tool_rate,
    attribute_tool_dispatch_cost,
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


def _make_rate_table(tool_rates: dict[str, ToolRate]) -> RateTable:
    """Minimal RateTable with operator-supplied tool_rates + zero-providers.

    Constructor needs all 5 fields. Providers/cpu_rate_per_ms/egress_rate_per_byte
    are not exercised at tool-dispatch unit tests; webhook_rate is required
    by RateTable schema but not consumed by tool path.
    """
    return RateTable(
        version="2026-05-28-test",
        providers={},
        tool_rates=tool_rates,
        webhook_rate=WebhookRate(flat_per_attempt=Decimal("0"), plus_egress=False),
        cpu_rate_per_ms=Decimal("0"),
        egress_rate_per_byte=Decimal("0"),
    )


@pytest.fixture
def cost_chain() -> RuntimeCostAttributionChain:
    return RuntimeCostAttributionChain()


@pytest.fixture
def audit_writer() -> _RecordingAuditWriter:
    return _RecordingAuditWriter()


# ---------------------------------------------------------------------------
# AC #2 — Three cost_kind formula branches
# ---------------------------------------------------------------------------


def test_cost_kind_flat_per_invocation() -> None:
    """flat_per_invocation: cost = rate (constant per invocation)."""
    rate = ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.005"))
    cost = _compute_tool_cost(
        rate, tool_args={"a": 1, "b": "long string ignored"}, response={"x": 999}
    )
    assert cost == Decimal("0.005")


def test_cost_kind_per_input_byte() -> None:
    """per_input_byte: cost = rate × len(canonical_json(tool_args))."""
    rate = ToolRate(cost_kind="per_input_byte", rate=Decimal("0.001"))
    tool_args = {"key": "value"}
    cost = _compute_tool_cost(rate, tool_args=tool_args, response={"any": "ignored"})
    # canonical_json({"key": "value"}, sort_keys, separators=(",", ":")) =
    # '{"key":"value"}' = 15 bytes
    expected = Decimal("0.001") * Decimal(15)
    assert cost == expected
    assert _canonical_json_byte_length(tool_args) == 15


def test_cost_kind_per_output_byte() -> None:
    """per_output_byte: cost = rate × len(canonical_json(response))."""
    rate = ToolRate(cost_kind="per_output_byte", rate=Decimal("0.002"))
    response = {"result": [1, 2, 3]}
    cost = _compute_tool_cost(rate, tool_args={"any": "ignored"}, response=response)
    # canonical_json({"result": [1, 2, 3]}, sort, sep) = '{"result":[1,2,3]}' = 18 bytes
    expected = Decimal("0.002") * Decimal(18)
    assert cost == expected
    assert _canonical_json_byte_length(response) == 18


def test_decimal_precision_preserved() -> None:
    """§C-OD-28.4 invariant 2: all arithmetic in Decimal; no float coercion."""
    # 17 sig-digit rate × 33-byte payload — float would lose precision here.
    rate = ToolRate(cost_kind="per_input_byte", rate=Decimal("0.12345678901234567"))
    payload = {"deep": "x" * 20}  # canonical_json = '{"deep":"xxxxxxxxxxxxxxxxxxxx"}' = 31 bytes
    cost = _compute_tool_cost(rate, tool_args=payload, response={})
    assert isinstance(cost, Decimal)
    # 31-byte payload * 0.12345678901234567 = 3.82716045938... preserved at Decimal precision
    expected = Decimal("0.12345678901234567") * Decimal(31)
    assert cost == expected


# ---------------------------------------------------------------------------
# Canonical-JSON byte-length convention
# ---------------------------------------------------------------------------


def test_canonical_json_byte_length_sorts_keys() -> None:
    """sort_keys ensures deterministic byte count regardless of dict ordering."""
    # Two equivalent payloads with different insertion order
    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}
    assert _canonical_json_byte_length(payload_a) == _canonical_json_byte_length(payload_b)


def test_canonical_json_byte_length_minimal_whitespace() -> None:
    """separators=(",", ":") produces minimal-whitespace JSON form."""
    payload = {"a": 1}
    # Default json.dumps would produce '{"a": 1}' = 8 bytes (with space after :)
    # Canonical form is '{"a":1}' = 7 bytes
    assert _canonical_json_byte_length(payload) == 7


def test_canonical_json_byte_length_unicode_uses_utf8() -> None:
    """UTF-8 byte count, not character count, for multibyte payloads."""
    # "café" → c-a-f-é where é is 2 bytes in UTF-8
    payload = {"name": "café"}
    # canonical: '{"name":"café"}' — json.dumps escapes by default,
    # but len(encode("utf-8")) returns the actual byte count of the escaped form
    serialized = '{"name":"caf\\u00e9"}'
    assert _canonical_json_byte_length(payload) == len(serialized.encode("utf-8"))


# ---------------------------------------------------------------------------
# AC #1 (helper-layer) + AC #5 — full chain returns attached record + 1 audit write
# ---------------------------------------------------------------------------


def test_attribute_tool_dispatch_cost_returns_attached_record(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """Helper returns idempotency-key-bearing SpanCostRecord for caller-side
    OTel attribute emission per AC #4."""
    rate_table = _make_rate_table(
        {"echo": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.005"))}
    )
    attached = attribute_tool_dispatch_cost(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        tool_id="echo",
        tool_args={"input": "hi"},
        response={"output": "hi"},
        span_id="abcdef0123456789",
        idempotency_key="tool-idem-1",
        parent_idempotency_key="parent-idem-1",
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
    )
    assert attached.span_id == "abcdef0123456789"
    assert attached.idempotency_key == "parent-idem-1"  # joins to parent
    assert attached.provider_discriminator is None  # v1.30 — no chain-level family tag
    assert attached.dispatch_kind == "tool"  # v1.30 — the PER_DISPATCH_KIND key
    assert attached.gen_ai_provider_name == "tool:echo"
    assert attached.gen_ai_request_model == ""
    assert attached.total_cost == pytest.approx(0.005, rel=1e-9)


def test_attribute_tool_dispatch_cost_writes_audit_entry(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """AC #4 — Cost-record attached + audit-ledger entry written.
    AC #5 — 1 tool call → 1 cost-record + 1 audit-ledger entry."""
    rate_table = _make_rate_table(
        {"echo": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.005"))}
    )
    attribute_tool_dispatch_cost(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        tool_id="echo",
        tool_args={"input": "hi"},
        response={"output": "hi"},
        span_id="0011223344556677",
        idempotency_key="tool-idem-1",
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
    # Per OD spec v1.10 §C-OD-26.6.1 step 2 canonical pattern:
    # cost:<workflow_id>:<step_action_id> + response=cost_attributed
    assert attrs["audit.cp.action_id"] == "cost:test-wf:workflow:test-wf:step:0"
    assert attrs["audit.cp.response"] == "cost_attributed"


def test_three_cost_kind_branches_produce_three_distinct_audit_writes(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """AC #5 integration shape: 3 tool calls (one per cost_kind) →
    3 distinct cost-records + 3 audit entries."""
    rate_table = _make_rate_table(
        {
            "tool_flat": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("1")),
            "tool_input": ToolRate(cost_kind="per_input_byte", rate=Decimal("0.1")),
            "tool_output": ToolRate(cost_kind="per_output_byte", rate=Decimal("0.01")),
        }
    )
    common = dict(
        rate_table=rate_table,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        tool_args={"a": 1},
        response={"b": 2},
        idempotency_key="any",
        parent_idempotency_key="parent",
        workflow_id="wf",
        parent_action_id="aid",
    )
    r_flat = attribute_tool_dispatch_cost(tool_id="tool_flat", span_id="aaa1", **common)
    r_input = attribute_tool_dispatch_cost(tool_id="tool_input", span_id="aaa2", **common)
    r_output = attribute_tool_dispatch_cost(tool_id="tool_output", span_id="aaa3", **common)
    # 3 distinct cost values from 3 cost_kind formulas
    assert r_flat.total_cost == pytest.approx(1.0, rel=1e-9)
    # canonical_json({"a":1}) = '{"a":1}' = 7 bytes
    assert r_input.total_cost == pytest.approx(0.1 * 7, rel=1e-9)
    # canonical_json({"b":2}) = '{"b":2}' = 7 bytes
    assert r_output.total_cost == pytest.approx(0.01 * 7, rel=1e-9)
    # 3 audit entries appended
    assert len(audit_writer.appended) == 3
    # Each dispatch_kind is "tool" (v1.30 — provider_discriminator is None per-dispatch)
    for r in (r_flat, r_input, r_output):
        assert r.provider_discriminator is None
        assert r.dispatch_kind == "tool"


# ---------------------------------------------------------------------------
# §C-OD-28.2 default fail-closed — unknown tool_id raises
# ---------------------------------------------------------------------------


def test_resolve_tool_rate_raises_on_unknown_tool() -> None:
    rate_table = _make_rate_table(
        {"echo": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.005"))}
    )
    with pytest.raises(ToolRateMissingError) as exc_info:
        _resolve_tool_rate(rate_table, "unknown_tool")
    assert "CP-FAIL-RATE-TABLE-MISSING" in str(exc_info.value)
    assert "unknown_tool" in str(exc_info.value)


def test_attribute_tool_dispatch_cost_propagates_rate_missing_error(
    cost_chain: RuntimeCostAttributionChain,
    audit_writer: _RecordingAuditWriter,
) -> None:
    """Caller-side production binding wraps in best-effort exception swallowing
    (mirror of llm_dispatch.py:934 precedent). At the helper layer, the
    error propagates per §C-OD-28.2 default fail-closed."""
    rate_table = _make_rate_table({})  # empty tool_rates
    with pytest.raises(ToolRateMissingError):
        attribute_tool_dispatch_cost(
            rate_table=rate_table,
            cost_chain=cost_chain,
            audit_writer=audit_writer,
            tool_id="missing_tool",
            tool_args={},
            response={},
            span_id="aaa",
            idempotency_key="any",
            parent_idempotency_key="parent",
            workflow_id="wf",
            parent_action_id="aid",
        )
    # No audit-ledger entry on rate-resolution failure (fail-closed semantics)
    assert audit_writer.appended == []
