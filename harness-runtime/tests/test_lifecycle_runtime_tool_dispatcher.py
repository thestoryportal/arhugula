"""U-RT-67 — `RuntimeToolDispatcher.dispatch()` body + sandbox span emission.

Per `Implementation_Plan_Harness_Runtime_v2_11.md` §1 U-RT-67 (5 ACs):
1. Dispatch resolves ToolContract; raises RT-FAIL-TOOL-CONTRACT-UNKNOWN on miss
2. Per-server-trust evaluation invoked; raises RT-FAIL-TOOL-INVOCATION-TRUST-VIOLATION on deny
3. sandbox.* 7-attribute namespace emitted on sandbox.enter per C-AS-15 §15
4. mcp.* 7-attribute namespace emitted on mcp.tool.call per C-AS-14 §14.3
5. Schema validation both directions; raises RT-FAIL-TOOL-INVOCATION-SCHEMA-VIOLATION on breach
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_as.secret_fail_class import SecretFailClass
from harness_as.secret_fetch import SecretRef, SecretScope
from harness_as.secret_fetch_audit import SecretFetchEvent, compose_secret_fetch_audit_entry
from harness_as.tool_contract import SecretAllowlistEntry, ToolContract
from harness_core import ClientName, PersonaTier
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.cp_shared_types import MCPTrustTier, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.mcp_client_namespace_emitter import (
    MCPClientNamespaceEmitter,
    MCPServerInfo,
)
from harness_cp.per_server_trust_evaluator import PerServerTrustEvaluator
from harness_cp.per_server_trust_types import (
    TierDerivationRule,
    TrustPolicy,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_write import WriteResult
from harness_runtime.bootstrap.factories.mcp_client_host_factory import (
    _build_default_policy_converter,
)
from harness_runtime.config.provider_secrets import SecretResolutionError
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    MCPHostUnreachableError,
    RuntimeToolDispatcher,
    SandboxDispatchDecision,
    SandboxTierFloorViolationError,
    ToolContractUnknownError,
    ToolExecutionDriver,
    ToolInvocationProtocolError,
    ToolInvocationSchemaViolationError,
    ToolInvocationTimeoutError,
    ToolInvocationTrustViolationError,
)
from harness_runtime.types import MCPClientConfig
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------- fixtures + helpers ---------------------------------------------


def _build_fastmcp_server(register_echo: bool = True, *, fired: list[str] | None = None) -> FastMCP:
    server = FastMCP(name="dispatcher-test-srv")
    if register_echo:

        @server.tool(description="echo")
        def echo(message: str) -> str:
            if fired is not None:
                fired.append(message)  # count every REAL fire (at-most-once witness)
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


def _make_tool_converter(*, idempotent: bool = False):
    def convert(tool):
        return ToolContract(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema or {"type": "object"},
            output_schema={"type": "object"},  # empty → no validation
            minimum_tier=SandboxTier.TIER_1_PROCESS,
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
            idempotent=idempotent,
        )

    return convert


def _make_secret_tool_converter(*required_secrets: SecretAllowlistEntry):
    def convert(tool):
        return ToolContract(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema or {"type": "object"},
            output_schema={"type": "object"},
            minimum_tier=SandboxTier.TIER_1_PROCESS,
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
            required_secrets=required_secrets,
        )

    return convert


async def _build_started_host(
    register_echo: bool = True,
    *,
    idempotent: bool = False,
    tool_contract_converter_override: Any = None,
    fired: list[str] | None = None,
) -> MCPClientHost:
    server = _build_fastmcp_server(register_echo=register_echo, fired=fired)
    host = MCPClientHost(
        transport="stdio",
        server_name="dispatcher-test-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        # `tool_contract_converter_override` lets a test drive the PRODUCTION
        # converter (`_build_default_policy_converter`) through the real fence —
        # the full-chain witness for the discovered-tool default_idempotent path.
        tool_contract_converter=(
            tool_contract_converter_override
            if tool_contract_converter_override is not None
            else _make_tool_converter(idempotent=idempotent)
        ),
        session_context_factory=_build_session_factory(server),
        auth_present=False,
    )
    await host.start()
    return host


async def _build_started_secret_host(*required_secrets: SecretAllowlistEntry) -> MCPClientHost:
    server = _build_fastmcp_server(register_echo=True)
    host = MCPClientHost(
        transport="stdio",
        server_name="dispatcher-test-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_make_secret_tool_converter(*required_secrets),
        session_context_factory=_build_session_factory(server),
        auth_present=False,
    )
    await host.start()
    return host


def _make_step_context(
    run_engine_class: EngineClass | None = None,
) -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-1",
        parent_action_id="workflow:wf-1:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="run-idem-key-abc",
        tenant_id=None,
        step_index=0,
        run_engine_class=run_engine_class,
    )


def _make_step(tool_id: str, tool_args: dict | None = None) -> WorkflowStep:
    return WorkflowStep(
        step_id="step-1",
        step_kind=StepKind.TOOL_STEP,
        step_payload={"tool_id": tool_id, "tool_args": tool_args or {}},
    )


def _make_binding(
    engine_class: EngineClass = EngineClass.PURE_PATTERN_NO_ENGINE,
) -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-1",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=engine_class,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _make_trust_policy(*, deny: list[str] | None = None) -> TrustPolicy:
    return TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        # Explicitly allow the test server so unknown-server-tier-floor
        # doesn't fire under CONSERVATIVE; deny_list still takes precedence
        # per spec §27.6 inv 3.
        allow_list=frozenset({"dispatcher-test-srv"}),
        deny_list=frozenset(deny or []),
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


def _low_tier_sandbox_resolver(_contract, _step):
    """Resolves below the contract's TIER_1 floor — never used unless
    contract is TIER_2+."""
    return SandboxDispatchDecision(
        tier=SandboxTier.TIER_1_PROCESS,
        tech="host",
        provider="host",
        assigned_tier_reason="tier-1-pinned",
        cost_tier_overhead_ms=10,
    )


class _InjectedExecutionDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[SandboxDispatchDecision, str, dict, str]] = []

    async def call_tool(
        self,
        *,
        mcp_client_host: MCPClientHost,
        sandbox_decision: SandboxDispatchDecision,
        tool_id: str,
        tool_args: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        self.calls.append((sandbox_decision, tool_id, dict(tool_args), idempotency_key))
        return {
            "content": [{"type": "text", "text": f"driver:{tool_args['message']}"}],
            "isError": False,
            "structuredContent": {"provider": sandbox_decision.provider},
        }


@dataclass(frozen=True)
class _ResolvedSecretForAudit:
    ref: SecretRef
    secret_last_rotated_at: str
    backend: str = "test-secret-backend"
    cache_tier_overhead_ms: int = 7
    policy_access_decision_reason: str = "permitted"


class _MetadataSecretResolver:
    def __init__(self, *results: _ResolvedSecretForAudit) -> None:
        self._results = {result.ref.name: result for result in results}
        self.calls: list[tuple[str, SecretScope, SandboxTier, ToolContract | None]] = []

    def resolve_with_audit_metadata(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> _ResolvedSecretForAudit:
        self.calls.append((name, scope, tier, tool))
        return self._results[name]


class _FailingSecretResolver:
    def resolve_with_audit_metadata(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> _ResolvedSecretForAudit:
        _ = scope, tier, tool
        raise SecretResolutionError(SecretFailClass.SECRET_UNAVAILABLE, name)


class _CapturingSecretAuditEmitter:
    def __init__(self) -> None:
        self.events: list[SecretFetchEvent] = []

    def emit(self, event: SecretFetchEvent) -> WriteResult:
        self.events.append(event)
        return WriteResult.APPENDED


class _DedupSecretAuditEmitter:
    def __init__(self) -> None:
        self.events: list[SecretFetchEvent] = []
        self._seen_idempotency_keys: set[str] = set()

    def emit(self, event: SecretFetchEvent) -> WriteResult:
        entry = compose_secret_fetch_audit_entry(event, None)
        if entry.idempotency_key in self._seen_idempotency_keys:
            return WriteResult.IDEMPOTENT_NOOP
        self._seen_idempotency_keys.add(entry.idempotency_key)
        self.events.append(event)
        return WriteResult.APPENDED


# ---------- R-CXA-1 — workflow-time secret-fetch AS→IS producer ------------


@pytest.mark.asyncio
async def test_secret_fetch_producer_fires_at_workflow_step() -> None:
    """R-CXA-1: required_secrets resolve at TOOL_STEP time and emit AS→IS audit."""
    scope = SecretScope(name="prod")
    required = SecretAllowlistEntry(name="api-token", scope=scope)
    ref = SecretRef(name="api-token", scope=scope, tier=SandboxTier.TIER_2_CONTAINER)
    resolver = _MetadataSecretResolver(
        _ResolvedSecretForAudit(
            ref=ref,
            secret_last_rotated_at="2026-06-08T00:00:00+00:00",
        )
    )
    audit = _CapturingSecretAuditEmitter()
    host = await _build_started_secret_host(required)
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        provider_secret_resolver=resolver,
        secret_fetch_audit_emitter=audit.emit,
    )

    try:
        result = await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "secret"}),
            step_context=_make_step_context(),
        )
    finally:
        await host.shutdown()

    assert result["tool_id"] == "echo"
    assert len(resolver.calls) == 1
    name, resolved_scope, resolved_tier, tool = resolver.calls[0]
    assert name == "api-token"
    assert resolved_scope == scope
    assert resolved_tier is SandboxTier.TIER_2_CONTAINER
    assert tool is not None
    assert tool.required_secrets == (required,)

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.thread_id == "wf-1"
    assert event.step_id == "step-1"
    assert event.actor == _make_step_context().parent_actor
    assert event.secret_name == "api-token"
    assert event.secret_scope == scope
    assert event.secret_last_rotated_at == "2026-06-08T00:00:00+00:00"


@pytest.mark.asyncio
async def test_secret_fetch_event_fields_non_hollow() -> None:
    """R-CXA-1: rotation metadata changes the structure-not-content fingerprint."""
    scope = SecretScope(name="prod")
    required = SecretAllowlistEntry(name="api-token", scope=scope)
    audit = _CapturingSecretAuditEmitter()
    resolver = _MetadataSecretResolver(
        _ResolvedSecretForAudit(
            ref=SecretRef(
                name="api-token",
                scope=scope,
                tier=SandboxTier.TIER_2_CONTAINER,
            ),
            secret_last_rotated_at="2026-06-08T00:00:00+00:00",
        )
    )
    host = await _build_started_secret_host(required)
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        provider_secret_resolver=resolver,
        secret_fetch_audit_emitter=audit.emit,
    )

    try:
        await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "first"}),
            step_context=_make_step_context(),
        )
    finally:
        await host.shutdown()

    event = audit.events[0]
    assert event.secret_scope.name == "prod"
    assert event.secret_last_rotated_at != ""
    same_secret_rotated = event.model_copy(
        update={"secret_last_rotated_at": "2026-06-09T00:00:00+00:00"}
    )
    first_entry = compose_secret_fetch_audit_entry(event, None)
    second_entry = compose_secret_fetch_audit_entry(same_secret_rotated, None)
    assert first_entry.response_hash != second_entry.response_hash


@pytest.mark.asyncio
async def test_secret_fetch_replay_idempotent_noop() -> None:
    """R-CXA-1: replay of the same workflow-step secret fetch does not duplicate."""
    scope = SecretScope(name="prod")
    required = SecretAllowlistEntry(name="api-token", scope=scope)
    resolver = _MetadataSecretResolver(
        _ResolvedSecretForAudit(
            ref=SecretRef(
                name="api-token",
                scope=scope,
                tier=SandboxTier.TIER_2_CONTAINER,
            ),
            secret_last_rotated_at="2026-06-08T00:00:00+00:00",
        )
    )
    audit = _DedupSecretAuditEmitter()
    host = await _build_started_secret_host(required)
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        provider_secret_resolver=resolver,
        secret_fetch_audit_emitter=audit.emit,
    )

    try:
        for message in ("first", "replay"):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": message}),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()

    assert len(resolver.calls) == 2
    assert len(audit.events) == 1


@pytest.mark.asyncio
async def test_secret_fetch_span_co_emitted() -> None:
    """R-CXA-1: successful fetch emits the structure-only secret.fetch span."""
    scope = SecretScope(name="prod")
    required = SecretAllowlistEntry(name="api-token", scope=scope)
    resolver = _MetadataSecretResolver(
        _ResolvedSecretForAudit(
            ref=SecretRef(
                name="api-token",
                scope=scope,
                tier=SandboxTier.TIER_2_CONTAINER,
            ),
            secret_last_rotated_at="2026-06-08T00:00:00+00:00",
            backend="gcp-secret-manager",
            cache_tier_overhead_ms=11,
            policy_access_decision_reason="permitted",
        )
    )
    audit = _CapturingSecretAuditEmitter()
    exporter, provider = _otel_setup()
    host = await _build_started_secret_host(required)
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        provider_secret_resolver=resolver,
        secret_fetch_audit_emitter=audit.emit,
        tracer_provider=provider,
    )

    try:
        await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "span"}),
            step_context=_make_step_context(),
        )
    finally:
        await host.shutdown()

    secret_span = next(s for s in exporter.get_finished_spans() if s.name == "secret.fetch")
    attrs = dict(secret_span.attributes or {})
    assert attrs["secret.name"] == "api-token"
    assert attrs["secret.scope"] == "prod"
    assert attrs["secret.backend"] == "gcp-secret-manager"
    assert attrs["secret.cache.tier_overhead_ms"] == 11
    assert attrs["secret.policy.access_decision_reason"] == "permitted"
    assert "secret.fail.class" not in attrs
    assert not any("sk-" in str(value) for value in attrs.values())


@pytest.mark.asyncio
async def test_failed_fetch_emits_fail_class() -> None:
    """R-CXA-1: failed fetch emits secret.fail.class on secret.fetch span."""
    scope = SecretScope(name="prod")
    required = SecretAllowlistEntry(name="api-token", scope=scope)
    exporter, provider = _otel_setup()
    host = await _build_started_secret_host(required)
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        provider_secret_resolver=_FailingSecretResolver(),
        secret_fetch_audit_emitter=_CapturingSecretAuditEmitter().emit,
        tracer_provider=provider,
    )

    try:
        with pytest.raises(SecretResolutionError):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "fail"}),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()

    secret_span = next(s for s in exporter.get_finished_spans() if s.name == "secret.fetch")
    attrs = dict(secret_span.attributes or {})
    assert attrs["secret.name"] == "api-token"
    assert attrs["secret.scope"] == "prod"
    assert attrs["secret.fail.class"] == "secret_unavailable"
    assert not any("sk-" in str(value) for value in attrs.values())


# ---------- AC #1 — tool-contract resolution + unknown failure -------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_id_raises_contract_unknown() -> None:
    host = await _build_started_host()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
    )
    try:
        with pytest.raises(ToolContractUnknownError, match="not-registered"):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("not-registered"),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_dispatch_missing_tool_id_in_payload_raises() -> None:
    host = await _build_started_host()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
    )
    try:
        step = WorkflowStep(
            step_id="step-x",
            step_kind=StepKind.TOOL_STEP,
            step_payload={},  # no tool_id
        )
        with pytest.raises(ToolContractUnknownError, match="missing or non-str"):
            await dispatcher.dispatch(_make_binding(), step, step_context=_make_step_context())
    finally:
        await host.shutdown()


# ---------- AC #2 — trust violation ----------------------------------------


@pytest.mark.asyncio
async def test_dispatch_trust_violation_raises() -> None:
    host = await _build_started_host()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(deny=["dispatcher-test-srv"]),
        sandbox_decision_resolver=_good_sandbox_resolver,
    )
    try:
        with pytest.raises(ToolInvocationTrustViolationError, match="explicit_deny|EXPLICIT_DENY"):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "hi"}),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()


# ---------- AC #3+4 — sandbox.* + mcp.* span emission ----------------------


@pytest.mark.asyncio
async def test_dispatch_emits_sandbox_and_mcp_spans() -> None:
    host = await _build_started_host()
    # Wire up an in-memory OTel exporter.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=provider,
    )
    try:
        result = await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "spans!"}),
            step_context=_make_step_context(),
        )
        assert result["tool_id"] == "echo"
        spans = exporter.get_finished_spans()
        names = {span.name for span in spans}
        assert "tool.dispatch" in names
        assert "sandbox.enter" in names
        assert "mcp.tool.call" in names
        assert "sandbox.exit" in names

        # sandbox.enter span carries the 7-attribute set (minus
        # sandbox.tier_escalation event which is conditional).
        sandbox_enter = next(s for s in spans if s.name == "sandbox.enter")
        attrs = dict(sandbox_enter.attributes or {})
        assert attrs["sandbox.tier"] == "tier-2-container"
        assert attrs["sandbox.tech"] == "linux-namespaces"
        assert attrs["sandbox.provider"] == "container-d"
        assert attrs["sandbox.policy.assigned_tier_reason"] == "default-from-test"
        assert attrs["sandbox.cost.tier_overhead_ms"] == 120
        assert "sandbox.fail.class" in attrs

        # mcp.tool.call span carries the 7-attribute mcp.* namespace.
        mcp_call = next(s for s in spans if s.name == "mcp.tool.call")
        mcp_attrs = dict(mcp_call.attributes or {})
        assert mcp_attrs["mcp.server.name"] == "dispatcher-test-srv"
        assert mcp_attrs["mcp.server.trust_tier"] == "level-2-sandbox-all"
        assert mcp_attrs["mcp.protocol_version"] == "2025-06-18"
        assert mcp_attrs["mcp.transport"] == "stdio"
        assert mcp_attrs["mcp.auth_present"] is False
        assert mcp_attrs["mcp.primitive.kind"] == "tool"
        assert isinstance(mcp_attrs["mcp.primitive.signature.sha256"], str)
        assert len(mcp_attrs["mcp.primitive.signature.sha256"]) == 64
    finally:
        await host.shutdown()


# ---------- AC #5 — schema violation ---------------------------------------


@pytest.mark.asyncio
async def test_dispatch_schema_violation_raises() -> None:
    """Output schema validation fires on mismatch."""
    server = _build_fastmcp_server()

    def strict_converter(tool):
        return ToolContract(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema or {"type": "object"},
            # Strict output schema — require a "must_be_present" field.
            output_schema={
                "type": "object",
                "required": ["must_be_present"],
                "properties": {"must_be_present": {"type": "string"}},
            },
            minimum_tier=SandboxTier.TIER_1_PROCESS,
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
        )

    host = MCPClientHost(
        transport="stdio",
        server_name="dispatcher-test-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=strict_converter,
        session_context_factory=_build_session_factory(server),
    )
    await host.start()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
    )
    try:
        with pytest.raises(ToolInvocationSchemaViolationError, match="output_schema validation"):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "x"}),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()


# ---------- tier-floor violation -------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_tier_floor_violation_raises() -> None:
    """When the resolved tier is below contract.minimum_tier, raise."""
    server = _build_fastmcp_server()

    def high_floor_converter(tool):
        return ToolContract(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema or {"type": "object"},
            output_schema={},
            minimum_tier=SandboxTier.TIER_3_MICROVM,  # high floor
            blast_radius_tier=BlastRadiusTier.EXTERNAL_REVERSIBLE,
        )

    host = MCPClientHost(
        transport="stdio",
        server_name="dispatcher-test-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=high_floor_converter,
        session_context_factory=_build_session_factory(server),
    )
    await host.start()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_low_tier_sandbox_resolver,  # tier 1 < tier 3
    )
    try:
        with pytest.raises(
            SandboxTierFloorViolationError,
            match="tier_3_microvm|tier-3-microvm",
        ):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "x"}),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()


# ---------- happy path: end-to-end --------------------------------------


@pytest.mark.asyncio
async def test_dispatch_happy_path_returns_step_output() -> None:
    host = await _build_started_host()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
    )
    try:
        result = await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "hello"}),
            step_context=_make_step_context(),
        )
        assert result["tool_id"] == "echo"
        assert result["sandbox_tier"] == "tier-2-container"
        # TrustDecisionReason values are snake_case; both default + explicit
        # allow are permitted outcomes.
        assert result["trust_decision_reason"] in {
            "default_allow",
            "explicit_allow",
        }
        # Idempotency key is deterministic over (parent_key, step_id, tool_id).
        assert isinstance(result["idempotency_key"], str)
        assert len(result["idempotency_key"]) == 64  # sha256 hex
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_dispatch_uses_injected_tool_execution_driver() -> None:
    """R-410 seam: after sandbox decision resolution, execution is delegated
    to the configured driver instead of being hard-wired to MCPClientHost."""
    host = await _build_started_host()
    driver: ToolExecutionDriver = _InjectedExecutionDriver()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tool_execution_driver=driver,
    )
    try:
        result = await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "via-driver"}),
            step_context=_make_step_context(),
        )
        response = result["response"]
        assert response["content"][0]["text"] == "driver:via-driver"

        assert isinstance(driver, _InjectedExecutionDriver)
        assert len(driver.calls) == 1
        sandbox_decision, tool_id, tool_args, idempotency_key = driver.calls[0]
        assert sandbox_decision.tier is SandboxTier.TIER_2_CONTAINER
        assert sandbox_decision.provider == "container-d"
        assert tool_id == "echo"
        assert tool_args == {"message": "via-driver"}
        assert isinstance(idempotency_key, str)
        assert len(idempotency_key) == 64
    finally:
        await host.shutdown()


# ---------- default sandbox resolver loud-on-misconfig ---------------------


@pytest.mark.asyncio
async def test_default_sandbox_resolver_raises() -> None:
    host = await _build_started_host()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        # NOT passing sandbox_decision_resolver — should hit default.
    )
    try:
        with pytest.raises(LookupError, match="default SandboxDecisionResolver"):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "x"}),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()


# ---------- AS spec v1.6 §15.9 dual-attribute emission discipline ---------


def _otel_setup() -> tuple[InMemorySpanExporter, TracerProvider]:
    """Standalone in-memory OTel exporter + provider for dispatcher tests."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


async def _dispatch_with_failing_call_tool(
    exc: Exception,
) -> tuple[InMemorySpanExporter, Exception]:
    """Build a real fastmcp-backed dispatcher, then patch host.call_tool to
    raise `exc` post-MCP-span-open. Returns the OTel exporter + the
    raised exception for caller assertions."""
    host = await _build_started_host()

    async def _failing_call_tool(*_args, **_kwargs):
        raise exc

    # Patch the bound method on this host instance.
    host.call_tool = _failing_call_tool  # type: ignore[method-assign]

    exporter, provider = _otel_setup()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=provider,
    )
    raised: Exception | None = None
    try:
        try:
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "x"}),
                step_context=_make_step_context(),
            )
        except Exception as e:
            raised = e
    finally:
        await host.shutdown()
    assert raised is not None
    return exporter, raised


def _violation_attrs(exporter: InMemorySpanExporter) -> dict[str, object]:
    spans = exporter.get_finished_spans()
    violation = next((s for s in spans if s.name == "sandbox.violation"), None)
    assert violation is not None, (
        f"expected sandbox.violation span, got names={[s.name for s in spans]}"
    )
    return dict(violation.attributes or {})


@pytest.mark.asyncio
async def test_dispatch_transport_failure_emits_sandbox_violation_dual_attrs() -> None:
    """§15.9 row 2 — MCPHostUnreachableError → mcp.fail.class=transport;
    projected sandbox.fail.class=exit_nonzero per §15.10 row 1."""
    exporter, raised = await _dispatch_with_failing_call_tool(
        MCPHostUnreachableError("host unreachable")
    )
    assert isinstance(raised, MCPHostUnreachableError)
    attrs = _violation_attrs(exporter)
    assert attrs["mcp.fail.class"] == "transport"
    assert attrs["sandbox.fail.class"] == "exit_nonzero"
    # AS spec v1.6 §15.6 row 1: sandbox.violation carries parent idempotency_key
    idem = attrs.get("idempotency_key")
    assert isinstance(idem, str) and len(idem) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_dispatch_protocol_error_emits_sandbox_violation_dual_attrs() -> None:
    """ToolInvocationProtocolError → mcp.fail.class=protocol_error;
    projected sandbox.fail.class=exit_nonzero per §15.10 row 2."""
    exporter, raised = await _dispatch_with_failing_call_tool(
        ToolInvocationProtocolError("malformed")
    )
    assert isinstance(raised, ToolInvocationProtocolError)
    attrs = _violation_attrs(exporter)
    assert attrs["mcp.fail.class"] == "protocol_error"
    assert attrs["sandbox.fail.class"] == "exit_nonzero"
    idem = attrs.get("idempotency_key")
    assert isinstance(idem, str) and len(idem) == 64


@pytest.mark.asyncio
async def test_dispatch_timeout_emits_sandbox_violation_dual_attrs() -> None:
    """§15.9 row 4 — ToolInvocationTimeoutError → mcp.fail.class=timeout;
    projected sandbox.fail.class=timeout per §15.10 row 4 (value-name parity)."""
    exporter, raised = await _dispatch_with_failing_call_tool(
        ToolInvocationTimeoutError("call timed out")
    )
    assert isinstance(raised, ToolInvocationTimeoutError)
    attrs = _violation_attrs(exporter)
    assert attrs["mcp.fail.class"] == "timeout"
    assert attrs["sandbox.fail.class"] == "timeout"
    idem = attrs.get("idempotency_key")
    assert isinstance(idem, str) and len(idem) == 64


@pytest.mark.asyncio
async def test_dispatch_schema_violation_emits_sandbox_violation_dual_attrs() -> None:
    """§15.9 row 3 — jsonschema.ValidationError → mcp.fail.class=schema_violation;
    projected sandbox.fail.class=policy_override per §15.10 row 3 (HIGH stretch).

    Uses a real fastmcp server with strict output schema that the echo
    response will fail (the dispatcher catches `jsonschema.ValidationError`
    inside its own `_validate_response_schema` step).
    """
    server = _build_fastmcp_server()

    def strict_converter(tool):
        return ToolContract(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema or {"type": "object"},
            output_schema={
                "type": "object",
                "required": ["must_be_present"],
                "properties": {"must_be_present": {"type": "string"}},
            },
            minimum_tier=SandboxTier.TIER_1_PROCESS,
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
        )

    host = MCPClientHost(
        transport="stdio",
        server_name="dispatcher-test-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=strict_converter,
        session_context_factory=_build_session_factory(server),
    )
    await host.start()
    exporter, provider = _otel_setup()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=provider,
    )
    try:
        with pytest.raises(ToolInvocationSchemaViolationError):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "x"}),
                step_context=_make_step_context(),
            )
        attrs = _violation_attrs(exporter)
        assert attrs["mcp.fail.class"] == "schema_violation"
        assert attrs["sandbox.fail.class"] == "policy_override"
        idem = attrs.get("idempotency_key")
        assert isinstance(idem, str) and len(idem) == 64
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_dispatch_happy_path_emits_no_sandbox_violation() -> None:
    """Success path: sandbox.exit emits without a preceding sandbox.violation.

    Regression guard — the violation span only opens on the exception path
    per §15.9 emission discipline.
    """
    host = await _build_started_host()
    exporter, provider = _otel_setup()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=provider,
    )
    try:
        await dispatcher.dispatch(
            _make_binding(),
            _make_step("echo", {"message": "ok"}),
            step_context=_make_step_context(),
        )
        names = {s.name for s in exporter.get_finished_spans()}
        assert "sandbox.violation" not in names
        assert "sandbox.exit" in names
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_dispatch_sandbox_violation_idempotency_key_matches_parent_dispatch() -> None:
    """AS spec v1.6 §15.6 row 1 idempotency-key join — H_T-AS-5 retirement gate.

    The `sandbox.violation` event's `idempotency_key` MUST equal the value
    passed to the parent `mcp.tool.call` (`host.call_tool(..., key)`), so
    that cross-axis cost-attribution (D6) and engine event history (D1)
    can correlate the violation back to its parent dispatch.
    """
    host = await _build_started_host()
    captured: list[str] = []

    async def _capturing_failing_call_tool(_tool_id, _tool_args, key):
        captured.append(key)
        raise MCPHostUnreachableError("host unreachable")

    host.call_tool = _capturing_failing_call_tool  # type: ignore[method-assign]
    exporter, provider = _otel_setup()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        tracer_provider=provider,
    )
    try:
        with pytest.raises(MCPHostUnreachableError):
            await dispatcher.dispatch(
                _make_binding(),
                _make_step("echo", {"message": "x"}),
                step_context=_make_step_context(),
            )
    finally:
        await host.shutdown()
    assert len(captured) == 1
    attrs = _violation_attrs(exporter)
    assert attrs["idempotency_key"] == captured[0]


# ---------------------------------------------------------------------------
# B-EFFECT-FENCE-DURABLE-AUTO (§14.22.7) — the effect fence auto-activates for a
# durable-execution engine class WITHOUT the operator `effect_fencing` opt-in,
# while a non-durable run stays fence-free. Witness by execution: dispatch the
# SAME step twice (same idempotency key) — a second reserve LOSES iff the fence
# is active (the at-most-once claim), so the 2nd dispatch raises iff the gate is
# open. (`tmp_path` gives the lazy claim dir.)
# ---------------------------------------------------------------------------


async def _dispatch_echo_twice_with_fence(
    *,
    run_engine_class: EngineClass | None,
    effect_fencing_explicit: bool,
    fence_dir: Any,
    binding_engine_class: EngineClass = EngineClass.PURE_PATTERN_NO_ENGINE,
    idempotent: bool = False,
    tool_contract_converter_override: Any = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], list[str]]:
    """Dispatch the same echo TOOL_STEP twice through a real fence; return the first
    result, the second result, and the tool-fire counter. The gate keys on
    `step_context.run_engine_class` (the RUN engine class); `binding_engine_class` is
    the INDEPENDENT per-step effective binding (the override channel) — they differ in
    the Codex [P2] regression witness. `idempotent` declares the echo tool's contract
    idempotent (B-EFFECT-FENCE-PER-TOOL) → the fence EXEMPTS it from the reserve.
    `tool_contract_converter_override` drives the PRODUCTION converter (full-chain).

    When the gate is ACTIVE, the first dispatch captures its output and the second
    SUPPRESS-AND-CONTINUEs (returns the captured output, NO re-fire) → `len(fired)==1`.
    When the gate is INACTIVE, the second dispatch RE-FIRES → `len(fired)==2`. (Neither
    raises — the first dispatch always captures, so the ambiguous→raise path is the
    separate absent/corrupt witness in test_effect_fence.py.)"""
    from harness_runtime.lifecycle.effect_fence import RuntimeEffectFence

    fired: list[str] = []
    host = await _build_started_host(
        idempotent=idempotent,
        tool_contract_converter_override=tool_contract_converter_override,
        fired=fired,
    )
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        effect_fence=RuntimeEffectFence(fence_dir=fence_dir / "effect-fence"),
        effect_fencing_explicit=effect_fencing_explicit,
    )
    binding = _make_binding(engine_class=binding_engine_class)
    ctx = _make_step_context(run_engine_class=run_engine_class)
    try:
        first = await dispatcher.dispatch(
            binding, _make_step("echo", {"message": "x"}), step_context=ctx
        )
        second = await dispatcher.dispatch(
            binding, _make_step("echo", {"message": "x"}), step_context=ctx
        )
    finally:
        await host.shutdown()
    return first, second, fired


@pytest.mark.asyncio
async def test_effect_fence_auto_activates_for_durable_run_without_optin(tmp_path: Any) -> None:
    """A durable RUN engine class (EVENT_SOURCED_REPLAY) + `effect_fencing_explicit=
    False` → the fence AUTO-activates: the re-dispatch of the same effect LOSES the
    claim and SUPPRESS-AND-CONTINUEs (returns the captured output, no re-fire) →
    the tool body fires EXACTLY ONCE."""
    first, second, fired = await _dispatch_echo_twice_with_fence(
        run_engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        effect_fencing_explicit=False,
        fence_dir=tmp_path,
    )
    assert first["tool_id"] == "echo"
    assert fired == ["x"]  # at-most-once — the re-dispatch suppressed, never re-fired
    assert second["response"] == first["response"]  # suppress returned the captured output


@pytest.mark.asyncio
async def test_effect_fence_skips_non_durable_run_without_optin(tmp_path: Any) -> None:
    """A non-durable RUN engine class (PURE_PATTERN_NO_ENGINE) + `effect_fencing_
    explicit=False` → NO auto-fence: both dispatches RE-FIRE (the gate stays closed,
    no claim — the spec's 'non-durable runs fence-free' carve-out)."""
    first, _second, fired = await _dispatch_echo_twice_with_fence(
        run_engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        effect_fencing_explicit=False,
        fence_dir=tmp_path,
    )
    assert first["tool_id"] == "echo"
    assert fired == ["x", "x"]  # fence-free → double-fire (no at-most-once claim)


@pytest.mark.asyncio
async def test_effect_fence_explicit_optin_fences_non_durable_run(tmp_path: Any) -> None:
    """The operator's explicit `effect_fencing=True` fences EVERY tool step (the
    pre-v1.60 blanket semantic), even a non-durable run — the re-dispatch suppresses."""
    first, second, fired = await _dispatch_echo_twice_with_fence(
        run_engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        effect_fencing_explicit=True,
        fence_dir=tmp_path,
    )
    assert first["tool_id"] == "echo"
    assert fired == ["x"]  # at-most-once — the re-dispatch suppressed
    assert second["response"] == first["response"]


@pytest.mark.asyncio
async def test_effect_fence_gates_on_run_not_per_step_override(tmp_path: Any) -> None:
    """Codex [P2] regression — a DURABLE run (run_engine_class=WAL_SEGMENT) with a
    per-step `StepOverride.engine_class=PURE_PATTERN_NO_ENGINE` (the binding's
    effective class) STILL fences: the gate keys on the RUN engine class (which
    governs resume/re-dispatch), NOT the per-step effective binding. Without the fix
    the per-step override would wrongly skip the fence → a double-fire window."""
    first, _second, fired = await _dispatch_echo_twice_with_fence(
        run_engine_class=EngineClass.WAL_SEGMENT,  # the RUN is durable
        binding_engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,  # a per-step override
        effect_fencing_explicit=False,
        fence_dir=tmp_path,
    )
    assert first["tool_id"] == "echo"
    assert fired == ["x"]  # at-most-once — gate keyed on the RUN class, fence active


@pytest.mark.asyncio
async def test_effect_fence_exempts_idempotent_tool_under_durable_run(tmp_path: Any) -> None:
    """B-EFFECT-FENCE-PER-TOOL (runtime §14.22.7 / AS C-AS-03 §3.1 v1.12) — a tool whose
    contract declares `idempotent=True` is NOT reserved even under a durable run that
    auto-activates the fence: both dispatches of the same effect RE-FIRE (no at-most-once
    claim), so an idempotent tool is safely retryable. Contrast: the default
    (idempotent=False) durable run fences (test_effect_fence_auto_activates_*)."""
    first, _second, fired = await _dispatch_echo_twice_with_fence(
        run_engine_class=EngineClass.EVENT_SOURCED_REPLAY,  # durable → fence active
        effect_fencing_explicit=False,
        fence_dir=tmp_path,
        idempotent=True,  # but the tool is declared idempotent → exempt
    )
    assert first["tool_id"] == "echo"
    assert fired == ["x", "x"]  # exempt → no fence claim → safely re-fired


@pytest.mark.asyncio
async def test_effect_fence_exempts_idempotent_tool_under_explicit_optin(tmp_path: Any) -> None:
    """The idempotent exemption is fence-active-reason-agnostic: even the operator's
    explicit `effect_fencing=True` (the pre-v1.60 blanket "fence every step") does NOT
    fence a declared-idempotent tool — there is no effect to double-fire. The exemption
    only ever applies to EXPLICITLY-declared-idempotent tools (default False stays fenced,
    test_effect_fence_explicit_optin_fences_non_durable_run)."""
    first, _second, fired = await _dispatch_echo_twice_with_fence(
        run_engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,  # non-durable
        effect_fencing_explicit=True,  # operator forces the fence on
        fence_dir=tmp_path,
        idempotent=True,  # but idempotent → still exempt
    )
    assert first["tool_id"] == "echo"
    assert fired == ["x", "x"]  # exempt → re-fired (no claim)


@pytest.mark.asyncio
async def test_effect_fence_exempts_idempotent_via_production_converter_full_chain(
    tmp_path: Any,
) -> None:
    """FULL-CHAIN witness (`[[full-chain-witness-not-half-proofs]]`, advisor pre-push
    catch) — the discovered-tool PRODUCTION path with NO proxy converter: a per-server
    `MCPClientConfig(default_idempotent=True)` drives the real
    `_build_default_policy_converter`, whose stamped `ToolContract.idempotent=True`
    reaches a REAL fence decision under a durable run → the tool is NOT reserved (both
    dispatches succeed). This closes the seam the unit witnesses leave open: production
    converter → host registry → fence (the over-applying direction = fence silently
    disabled = unsafe, so it must be witnessed end-to-end, not just at each half)."""
    entry = MCPClientConfig(
        client_name=ClientName("read-only-data-server"),
        transport=MCPTransport.STDIO,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="stdio:///bin/echo",
        default_idempotent=True,
    )
    production_converter = _build_default_policy_converter(
        entry, DeploymentSurface.LOCAL_DEVELOPMENT
    )
    first, _second, fired = await _dispatch_echo_twice_with_fence(
        run_engine_class=EngineClass.EVENT_SOURCED_REPLAY,  # durable → fence active
        effect_fencing_explicit=False,
        fence_dir=tmp_path,
        tool_contract_converter_override=production_converter,
    )
    assert first["tool_id"] == "echo"
    assert fired == ["x", "x"]  # production-stamped idempotent → exempt → re-fired end-to-end


@pytest.mark.asyncio
async def test_effect_fence_suppress_path_emits_sandbox_exit_no_mcp_call(tmp_path: Any) -> None:
    """[Codex P2] The suppress-and-continue early return BALANCES its sandbox lifecycle:
    the second (suppressed) dispatch emits `sandbox.enter` (pre-fence) AND `sandbox.exit`
    (the new suppress-path emission), but NO `mcp.tool.call` span (no effect re-fired) —
    so a suppressed dispatch is not recorded as an unmatched sandbox.enter."""
    from harness_runtime.lifecycle.effect_fence import RuntimeEffectFence

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    host = await _build_started_host()
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_make_emitter(),
        trust_policy=_make_trust_policy(),
        sandbox_decision_resolver=_good_sandbox_resolver,
        effect_fence=RuntimeEffectFence(fence_dir=tmp_path / "effect-fence"),
        effect_fencing_explicit=True,
        tracer_provider=provider,
    )
    binding = _make_binding()
    ctx = _make_step_context()
    try:
        await dispatcher.dispatch(binding, _make_step("echo", {"message": "x"}), step_context=ctx)
        exporter.clear()  # isolate the SECOND (suppressed) dispatch's spans
        suppressed = await dispatcher.dispatch(
            binding, _make_step("echo", {"message": "x"}), step_context=ctx
        )
    finally:
        await host.shutdown()
    assert suppressed["tool_id"] == "echo"  # suppress returned the captured output
    names = {span.name for span in exporter.get_finished_spans()}
    assert "sandbox.enter" in names  # emitted pre-fence
    assert "sandbox.exit" in names  # the new suppress-path balance emission
    assert "mcp.tool.call" not in names  # NO effect re-fired
