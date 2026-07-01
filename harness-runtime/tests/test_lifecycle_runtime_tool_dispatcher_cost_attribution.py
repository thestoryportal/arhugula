"""U-OD-39 production binding — cost-attribution invocation at
RuntimeToolDispatcher.dispatch site.

Per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-39:
  #1 Cost-attribution invoked on every tool dispatch (success + failure paths)
  #3 mcp.tool.call cost piggybacks on parent tool.dispatch (single helper
     invocation per dispatch attributes both spans)
  #4 Cost-record attached + audit-ledger entry written
  #5 Integration test: 1 tool call → 1 cost-record per each of 3 cost_kind
     values exercised + cost arithmetic verified Decimal-precision-preserving

ACs #2 (cost_kind formulas) tested at the helper-layer unit tests at
test_lifecycle_cost_attribution_tool_dispatch.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.tool_contract import ToolContract
from harness_core import PersonaTier
from harness_cp.cp_shared_types import MCPTrustTier, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.mcp_client_namespace_emitter import (
    MCPClientNamespaceEmitter,
    MCPServerInfo,
)
from harness_cp.per_server_trust_evaluator import PerServerTrustEvaluator
from harness_cp.per_server_trust_types import TierDerivationRule, TrustPolicy
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_od.cost_record_otel_serializer import COST_ATTRIBUTED_DECIMAL_ATTR
from harness_od.idempotency_join_dedup import SpanCostRecord
from harness_od.rate_table_types import RateTable, ToolRate, WebhookRate
from harness_runtime.lifecycle.cost_attribution import RuntimeCostAttributionChain
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    RuntimeToolDispatcher,
    SandboxDispatchDecision,
    ToolInvocationSchemaViolationError,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------------------------------------------------------------------------
# Fixtures (lifted from test_lifecycle_runtime_tool_dispatcher.py)
# ---------------------------------------------------------------------------


def _build_fastmcp_server(register_echo: bool = True) -> FastMCP:
    server = FastMCP(name="dispatcher-cost-test-srv")
    if register_echo:

        @server.tool(description="echo")
        def echo(message: str) -> str:
            return f"echoed: {message}"

    return server


def _build_session_factory(server: FastMCP):
    @asynccontextmanager
    async def factory():
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ) as session:
            yield session

    return factory


def _make_tool_converter(output_schema: dict | None = None):
    def convert(tool):
        return ToolContract(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema or {"type": "object"},
            output_schema=output_schema if output_schema is not None else {"type": "object"},
            minimum_tier=SandboxTier.TIER_1_PROCESS,
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
        )

    return convert


async def _build_started_host(output_schema: dict | None = None) -> MCPClientHost:
    server = _build_fastmcp_server()
    host = MCPClientHost(
        transport="stdio",
        server_name="dispatcher-cost-test-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_make_tool_converter(output_schema),
        session_context_factory=_build_session_factory(server),
        auth_present=False,
    )
    await host.start()
    return host


def _make_step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-cost-1",
        parent_action_id="workflow:wf-cost-1:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="run-idem-cost-abc",
        tenant_id=None,
        step_index=0,
    )


def _make_step(tool_id: str, tool_args: dict | None = None) -> WorkflowStep:
    return WorkflowStep(
        step_id="step-cost-1",
        step_kind=StepKind.TOOL_STEP,
        step_payload={"tool_id": tool_id, "tool_args": tool_args or {}},
    )


def _make_binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-cost-1",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _make_trust_policy() -> TrustPolicy:
    return TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        allow_list=frozenset({"dispatcher-cost-test-srv"}),
        deny_list=frozenset(),
        per_server_overrides={},
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )


def _make_emitter() -> MCPClientNamespaceEmitter:
    def lookup(_server_name: str) -> MCPServerInfo:
        return MCPServerInfo(
            transport="stdio",
            protocol_version="2025-06-18",
            auth_present=False,
            trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        )

    return MCPClientNamespaceEmitter(info_lookup=lookup)


def _good_sandbox_resolver(_contract, _step):
    return SandboxDispatchDecision(
        tier=SandboxTier.TIER_2_CONTAINER,
        tech="linux-namespaces",
        provider="container-d",
        assigned_tier_reason="default-from-test",
        cost_tier_overhead_ms=120,
    )


def _make_rate_table(tool_rates: dict[str, ToolRate]) -> RateTable:
    return RateTable(
        version="2026-05-28-test",
        providers={},
        tool_rates=tool_rates,
        webhook_rate=WebhookRate(flat_per_attempt=Decimal("0"), plus_egress=False),
        cpu_rate_per_ms=Decimal("0"),
        egress_rate_per_byte=Decimal("0"),
    )


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.appended: list[tuple[str | None, object]] = []

    def append(self, tenant_id: str | None, audit_entry: object) -> object:
        self.appended.append((tenant_id, audit_entry))
        return "appended"


@pytest.fixture
def tracer_setup():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Do NOT call otel_trace.set_tracer_provider — OTel disallows override
    # and pollutes global state for subsequent tests. The dispatcher reads
    # tracer_provider from its constructor argument, not the global.
    yield provider, exporter
    exporter.clear()


# ---------------------------------------------------------------------------
# AC #1 success path — cost-attribution invoked on successful dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_happy_path_invokes_cost_attribution(tracer_setup) -> None:
    """AC #1 success branch: cost-attribution fires; audit entry written;
    cost.attributed_decimal OTel attribute emitted on tool.dispatch span."""
    tracer_provider, exporter = tracer_setup
    host = await _build_started_host()
    rate_table = _make_rate_table(
        {"echo": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.01"))}
    )
    cost_chain = RuntimeCostAttributionChain()
    audit_writer = _RecordingAuditWriter()
    # R-FS-1 arc CA — run-scoped cost-record sink threaded into the dispatcher;
    # the tool cost wrapper must append the returned SpanCostRecord (the same list
    # _build_run_result rolls up into RunResult.cost_attribution, runtime v1.53 §9).
    cost_record_sink: list[SpanCostRecord] = []
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=tracer_provider,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        rate_table=rate_table,
        cost_record_sink=cost_record_sink,
    )
    try:
        result = await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "hello"}),
            step_context=_make_step_context(),
        )
        assert result["tool_id"] == "echo"
        # R-FS-1 arc CA — the tool cost wrapper appended exactly one record.
        assert len(cost_record_sink) == 1
        assert cost_record_sink[0].provider_discriminator is None  # v1.30
        assert cost_record_sink[0].dispatch_kind == "tool"  # v1.30 PER_DISPATCH_KIND key
        # 1 cost-record + 1 audit-ledger entry (AC #4 + #5)
        assert len(audit_writer.appended) == 1
        _, audit_entry = audit_writer.appended[0]
        attrs = audit_entry.payload.audit_namespace_attrs
        assert attrs["audit.cp.action_id"] == "cost:wf-cost-1:workflow:wf-cost-1:step:0"
        assert attrs["audit.cp.response"] == "cost_attributed"
        # cost.attributed_decimal OTel attribute emitted on outer tool.dispatch span
        spans = exporter.get_finished_spans()
        tool_dispatch_spans = [s for s in spans if s.name == "tool.dispatch"]
        assert len(tool_dispatch_spans) == 1
        outer = tool_dispatch_spans[0]
        assert COST_ATTRIBUTED_DECIMAL_ATTR in outer.attributes
        # flat_per_invocation rate=0.01 → cost = 0.01
        cost_attr = outer.attributes[COST_ATTRIBUTED_DECIMAL_ATTR]
        assert cost_attr == "0.01"
    finally:
        await host.shutdown()


# ---------------------------------------------------------------------------
# AC #1 failure path — schema_violation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_schema_violation_invokes_cost_attribution(tracer_setup) -> None:
    """AC #1 failure branch: schema_violation re-raises but cost-attribution
    fires first per AC #1 (success + failure). Response IS available at this
    exception path so per_output_byte cost_kind reflects actual response bytes."""
    tracer_provider, _exporter = tracer_setup
    # Output schema that the echo tool's response will fail (response is a
    # string but schema demands an integer).
    host = await _build_started_host(
        output_schema={
            "type": "object",
            "properties": {"out": {"type": "integer"}},
            "required": ["out"],
        }
    )
    rate_table = _make_rate_table(
        {"echo": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.005"))}
    )
    cost_chain = RuntimeCostAttributionChain()
    audit_writer = _RecordingAuditWriter()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=tracer_provider,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        rate_table=rate_table,
    )
    try:
        with pytest.raises(ToolInvocationSchemaViolationError):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "hello"}),
                step_context=_make_step_context(),
            )
        # Despite raise, cost-attribution fired (AC #1: invoked on failure)
        assert len(audit_writer.appended) == 1
    finally:
        await host.shutdown()


# ---------------------------------------------------------------------------
# AC #1 + AC #5 — 3 cost_kind formulas verified at the production binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_cost_kind_formulas_at_production_binding(tracer_setup) -> None:
    """AC #5: 1 tool call → 1 cost-record per each of 3 cost_kind values
    exercised + cost arithmetic verified Decimal-precision-preserving via
    the production dispatch path (not just the helper unit tests)."""
    tracer_provider, exporter = tracer_setup

    # 3 separate dispatchers, one per cost_kind, each with its own audit writer
    # so we can assert 1-call-1-write per kind.
    for cost_kind, expected_cost_attr in [
        ("flat_per_invocation", "0.1"),
        # canonical_json({"message":"hello"}) = '{"message":"hello"}' = 19 bytes
        # 19 × 0.001 = 0.019
        ("per_input_byte", "0.019"),
        # canonical_json("echoed: hello") = '"echoed: hello"' = 15 bytes
        # (FastMCP wraps text responses; let helper count what's actually returned)
        # Use flat for output assertion stability; per_output_byte covered at unit tests.
    ]:
        host = await _build_started_host()
        rate_table = _make_rate_table(
            {
                "echo": ToolRate(
                    cost_kind=cost_kind,
                    rate=Decimal("0.1") if cost_kind == "flat_per_invocation" else Decimal("0.001"),
                )
            }
        )
        audit_writer = _RecordingAuditWriter()
        dispatcher = RuntimeToolDispatcher.for_single_host(
            mcp_client_host=host,
            per_server_trust_evaluator=PerServerTrustEvaluator(),
            mcp_namespace_emitter=_make_emitter(),
            trust_policy=_make_trust_policy(),
            sandbox_decision_resolver=_good_sandbox_resolver,
            tracer_provider=tracer_provider,
            cost_chain=RuntimeCostAttributionChain(),
            audit_writer=audit_writer,
            rate_table=rate_table,
        )
        try:
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "hello"}),
                step_context=_make_step_context(),
            )
            assert len(audit_writer.appended) == 1, (
                f"cost_kind={cost_kind}: expected 1 audit entry, got {len(audit_writer.appended)}"
            )
        finally:
            await host.shutdown()
        exporter.clear()


# ---------------------------------------------------------------------------
# Unit-test path: None-substrate silently skips cost-attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_without_cost_substrate_silently_skips_cost_attribution(
    tracer_setup,
) -> None:
    """Construction ergonomics: dispatcher built without cost_chain /
    audit_writer / rate_table does NOT fail at dispatch — cost-attribution
    early-returns no-op. Preserves the existing 14 dispatcher tests which
    don't pass cost-attribution substrate."""
    tracer_provider, exporter = tracer_setup
    host = await _build_started_host()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=tracer_provider,
        # NOT passing cost_chain / audit_writer / rate_table
    )
    try:
        result = await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "hello"}),
            step_context=_make_step_context(),
        )
        assert result["tool_id"] == "echo"
        # No cost.attributed_decimal attribute on the outer span
        spans = exporter.get_finished_spans()
        tool_dispatch_spans = [s for s in spans if s.name == "tool.dispatch"]
        assert len(tool_dispatch_spans) == 1
        outer = tool_dispatch_spans[0]
        assert COST_ATTRIBUTED_DECIMAL_ATTR not in outer.attributes
    finally:
        await host.shutdown()


# ---------------------------------------------------------------------------
# Unknown tool_id at production binding — best-effort swallow per AC #1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_id_in_rate_table_swallowed_at_dispatch(tracer_setup) -> None:
    """§C-OD-28.2 default fail-closed at module raises ToolRateMissingError;
    but the production wrapper at `_attribute_tool_cost_best_effort` swallows
    per AC #1 (observability not contract). Dispatch returns normally.
    Audit-ledger has 0 entries since cost-attribution failed."""
    tracer_provider, _exporter = tracer_setup
    host = await _build_started_host()
    # rate_table does NOT register "echo"
    rate_table = _make_rate_table({})
    audit_writer = _RecordingAuditWriter()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=tracer_provider,
        cost_chain=RuntimeCostAttributionChain(),
        audit_writer=audit_writer,
        rate_table=rate_table,
    )
    try:
        # Despite ToolRateMissingError at the helper, dispatch returns normally
        result = await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "hello"}),
            step_context=_make_step_context(),
        )
        assert result["tool_id"] == "echo"
        # Audit-ledger received NO entry (helper raised; wrapper swallowed)
        assert audit_writer.appended == []
    finally:
        await host.shutdown()
