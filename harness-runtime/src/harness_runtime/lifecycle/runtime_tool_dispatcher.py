"""U-RT-67 — `RuntimeToolDispatcher.dispatch()` body + sandbox span emission.

Per `Spec_Harness_Runtime_v1.md` v1.13 §14.9.1 RuntimeToolDispatcher
dispatch surface + §14.9.2 per-step invocation discipline + §14.9.4 span
emission + §14.9.5 failure-mode taxonomy.

Per `Implementation_Plan_Harness_Runtime_v2_11.md` §1 U-RT-67 (5 ACs).

The dispatcher satisfies the `AsyncStepDispatcher` Protocol
(`sync_dispatcher_facade.py`). Stage 5 wraps it with
`RetryBreakerFallbackDispatcher` (the spec calls for the same registry
key shape as `"llm_dispatch"`; the wrap is materialized at U-RT-68 +
governed by the Class-1 fork resolution recorded at
`.harness/class_1_fork_u_rt_68_retry_wrap_shape_gap.md`).

Composition surface:
- Consumes `MCPClientHost` (U-RT-63) → resolves ToolContract from
  `tool_registry` + invokes `call_tool`.
- Consumes `PerServerTrustEvaluator` (U-CP-68) → per-dispatch gate per
  spec §14.9.2 invariant 2 (no caching).
- Consumes `MCPClientNamespaceEmitter` (U-CP-69) → mutates the
  `mcp.tool.call` span with the 7-attribute `mcp.*` namespace.
- Operator supplies `sandbox_decision_resolver` at __init__ — the
  per-dispatch policy that selects sandbox tier + provider + tech (the
  spec defers to implementation discretion at §14.9.7; the dispatcher
  enforces tier-floor against `ToolContract.minimum_tier` but does not
  derive the tier itself).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import jsonschema
from harness_as.sandbox_fail_class import (
    MCPInvocationFailClass,
    project_mcp_to_sandbox_fail_class,
)
from harness_as.sandbox_tier import SandboxTier
from harness_as.secret_fail_class import SecretFailClass
from harness_as.secret_fetch_audit import SecretFetchEvent
from harness_as.tool_contract import SecretAllowlistEntry, ToolContract
from harness_cp.engine_class import EngineClass
from harness_cp.mcp_client_namespace_emitter import (
    MCPClientNamespaceEmitter,
)
from harness_cp.pause_resume_protocol_types import EffectFenceResolution
from harness_cp.per_server_trust_evaluator import PerServerTrustEvaluator
from harness_cp.per_server_trust_types import (
    MCPPrimitive,
    TrustPolicy,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Identifier

from harness_runtime.config.provider_secrets import (
    SecretAllowlistDeniedError,
    SecretResolutionError,
)
from harness_runtime.lifecycle.cost_record_sink import SupportsCostRecordAppend
from harness_runtime.lifecycle.effect_fence import (
    EffectFenceAbortedError,
    EffectFenceAmbiguousUncommittedError,
    EffectFenceProtocol,
)
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost

if TYPE_CHECKING:
    # Type-only: the multi-server routing carriers (U-RT-127/128). Imported
    # under TYPE_CHECKING + used via `cast("ServerName", …)` string forward-refs
    # at runtime to avoid the `lifecycle.runtime_tool_dispatcher → types →
    # lifecycle.*` runtime import cycle (the C-RT-04 `Any`-narrowing pattern).
    from harness_runtime.types import ServerName, ToolName

__all__ = [
    "MCPHostUnreachableError",
    "MCPToolNameCollisionError",
    "RuntimeToolDispatcher",
    "SandboxDecisionResolver",
    "SandboxDispatchDecision",
    "SandboxDriverUnavailableError",
    "SandboxTierFloorViolationError",
    "ToolContractUnknownError",
    "ToolExecutionDriver",
    "ToolInvocationProtocolError",
    "ToolInvocationSchemaViolationError",
    "ToolInvocationTimeoutError",
    "ToolInvocationTrustViolationError",
]


# B-EFFECT-FENCE-DURABLE-AUTO (runtime spec §14.22.7) — the durable-execution
# engine classes whose resume RE-DISPATCHES uncommitted steps, so a crash-then-
# resume can re-fire a non-idempotent effect. Under these the effect fence
# auto-activates (no operator `effect_fencing=True` opt-in needed). PURE_PATTERN_
# NO_ENGINE is EXCLUDED — it is the "no-engine" pipeline-automation baseline (the
# spec's "non-durable run" carve-out, kept fence-free by default; an operator may
# still opt in explicitly via `RuntimeConfig.effect_fencing`). Excluding it is a
# strict improvement over the pre-arc all-opt-in default (no regression: the 4
# real durable engines move opt-in → auto-on; pure-pattern is unchanged). The
# spec defines "durable" as "resume re-dispatches uncommitted steps" + names
# this as impl-discretion; this is that reading.
_DURABLE_AUTO_FENCE_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.SAVE_POINT_CHECKPOINT,
        EngineClass.EVENT_SOURCED_REPLAY,
        EngineClass.WAL_SEGMENT,
        EngineClass.RECONCILER_LOOP,
    }
)


# --- Sandbox decision carrier + resolver -----------------------------------


@dataclass(frozen=True)
class SandboxDispatchDecision:
    """Operator-resolved sandbox dispatch policy carrier per spec §14.9.4 +
    C-AS-15 §15 sandbox.* 7-attribute namespace.

    The dispatcher enforces tier-floor (`tier >= contract.minimum_tier`) and
    emits the 7 attributes from this carrier; it does NOT derive the
    decision. The derivation is the `SandboxDecisionResolver` contract at
    spec §14.9.8 (NOT §14.9.7 — that section defers only emitter-mutation /
    idempotency / health-check; phantom cite corrected at v1.41).
    """

    tier: SandboxTier
    tech: str  # e.g., "linux-namespaces" / "docker" / "firecracker"
    provider: str  # e.g., "host" / "container-d" / "fly-machines"
    assigned_tier_reason: str  # human-readable cause of tier assignment
    cost_tier_overhead_ms: int  # estimated per-tier startup overhead


SandboxDecisionResolver = Callable[[ToolContract, WorkflowStep], SandboxDispatchDecision]
"""Operator-supplied resolver mapping (contract, step) → dispatch decision.

Per spec §14.9.8 (Reading B, v1.41): the runtime dispatcher enforces
invariants; the resolver carries the deployment-specific tier-assignment
policy. The bootstrap stage-5 factory supplies a per-server default-policy
resolver; the default resolver here raises on production misconfig for
operator-hand-built dispatchers (mirrors U-CP-68 `_default_tier_resolver` +
U-CP-69 `_default_info_lookup` loud-on-misconfig discipline).
"""


def _default_sandbox_decision_resolver(
    _contract: ToolContract, _step: WorkflowStep
) -> SandboxDispatchDecision:
    raise LookupError(
        "default SandboxDecisionResolver invoked — operator must supply a "
        "sandbox_decision_resolver at RuntimeToolDispatcher.__init__ that "
        "maps (tool_contract, step) to a SandboxDispatchDecision carrying "
        "the per-deployment tier + tech + provider + assigned_tier_reason + "
        "cost_tier_overhead_ms per C-AS-15 §15"
    )


class ToolExecutionDriver(Protocol):
    """Per-dispatch tool execution mechanism selected after sandbox policy.

    The dispatcher owns contract/trust checks, sandbox span emission, and
    response validation. The execution driver owns the actual mechanism that
    runs the tool call. The default driver preserves the pre-R-410 behavior by
    delegating to `MCPClientHost.call_tool`; container/microVM/full-VM drivers
    can replace that mechanism without changing the dispatcher surface.
    """

    async def call_tool(
        self,
        *,
        mcp_client_host: MCPClientHost,
        sandbox_decision: SandboxDispatchDecision,
        tool_id: str,
        tool_args: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Execute one tool call and return the dispatcher response mapping."""
        ...


class MCPHostToolExecutionDriver:
    """Default tool execution driver: existing MCP host call path."""

    async def call_tool(
        self,
        *,
        mcp_client_host: MCPClientHost,
        sandbox_decision: SandboxDispatchDecision,
        tool_id: str,
        tool_args: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        _ = sandbox_decision
        return await mcp_client_host.call_tool(tool_id, tool_args, idempotency_key)


# --- Typed fail classes (spec §14.9.5) -------------------------------------


class ToolContractUnknownError(LookupError):
    """`RT-FAIL-TOOL-CONTRACT-UNKNOWN` typed carrier per spec §14.9.5.

    Raised when `step.step_payload["tool_id"]` is not registered in any
    configured host's `tool_registry` (absent from the routing index).
    Permanent — the driver maps to
    `step-failure: RT-FAIL-TOOL-CONTRACT-UNKNOWN: ...`.
    """


class MCPToolNameCollisionError(RuntimeError):
    """`RT-FAIL-MCP-TOOL-NAME-COLLISION` typed carrier per spec §14.9.10 D2
    (the 10th fail class — the §14.9.5 "8 new" + §14.9.9 "9th" counts preserved).

    Raised at the stage-5 routing-index build when a `tool_id` is advertised by
    ≥2 configured MCP hosts. Permanent — bootstrap aborts (detect-then-refuse;
    routing must stay deterministic + unambiguous). Server-qualified addressing
    (`server_name/tool_id`) to permit deliberate same-name tools is a registered
    forward item (reshape fork §6), not silently foreclosed.
    """


class ToolInvocationTrustViolationError(PermissionError):
    """`RT-FAIL-TOOL-INVOCATION-TRUST-VIOLATION` typed carrier per spec §14.9.5.

    Raised when `PerServerTrustEvaluator.evaluate()` returns
    `permitted=False`. Permanent — does not retry.
    """


class ToolInvocationTimeoutError(TimeoutError):
    """`RT-FAIL-TOOL-INVOCATION-TIMEOUT` typed carrier per spec §14.9.5.

    Raised when `MCPClientHost.call_tool()` exceeds the tool-contract
    timeout. Retryable per C-RT-16.
    """


class ToolInvocationProtocolError(RuntimeError):
    """`RT-FAIL-TOOL-INVOCATION-PROTOCOL-ERROR` typed carrier per spec §14.9.5.

    Raised when MCP protocol error from the underlying transport. Permanent.
    """


class ToolInvocationSchemaViolationError(ValueError):
    """`RT-FAIL-TOOL-INVOCATION-SCHEMA-VIOLATION` typed carrier per spec §14.9.5.

    Raised when `call_tool` response fails `ToolContract.output_schema`
    validation. Permanent.
    """


class MCPHostUnreachableError(RuntimeError):
    """`RT-FAIL-MCP-HOST-UNREACHABLE` typed carrier per spec §14.9.5.

    Raised when `MCPClientHost.health_check()` reports `alive=False`
    mid-dispatch. Transient — retryable per C-RT-16.
    """


class SandboxTierFloorViolationError(RuntimeError):
    """`RT-FAIL-SANDBOX-TIER-FLOOR-VIOLATION` typed carrier per spec §14.9.5.

    Raised when the resolved `SandboxDispatchDecision.tier` is below the
    `ToolContract.minimum_tier`. Permanent (policy breach).
    """


class SandboxDriverUnavailableError(RuntimeError):
    """`RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE` typed carrier per spec §14.9.9 (FR-2(i)).

    Raised at the stage-5 factory when the resolved per-server sandbox tier is
    `> TIER_1_PROCESS` but no execution driver is configured/registrable for it
    (`MCPClientConfig.sandbox_driver` absent, or missing a field the tier's driver
    requires). Permanent — bootstrap aborts. The factory MUST NOT fall through to
    the in-process `MCPHostToolExecutionDriver` (the silent-in-process defect this
    closes); a claimed isolation tier that cannot be delivered fails loud.
    """


# --- sandbox.* + tool.dispatch attribute name constants --------------------

ATTR_SANDBOX_TIER = "sandbox.tier"
ATTR_SANDBOX_TECH = "sandbox.tech"
ATTR_SANDBOX_PROVIDER = "sandbox.provider"
ATTR_SANDBOX_POLICY_ASSIGNED_TIER_REASON = "sandbox.policy.assigned_tier_reason"
ATTR_SANDBOX_COST_TIER_OVERHEAD_MS = "sandbox.cost.tier_overhead_ms"
ATTR_SANDBOX_FAIL_CLASS = "sandbox.fail.class"
ATTR_MCP_FAIL_CLASS = "mcp.fail.class"
ATTR_IDEMPOTENCY_KEY = "idempotency_key"

ATTR_TOOL_CONTRACT_NAME = "tool.contract.name"
ATTR_STEP_ID = "step.id"
ATTR_STEP_KIND = "step.step_kind"

ATTR_SECRET_NAME = "secret.name"
ATTR_SECRET_SCOPE = "secret.scope"
ATTR_SECRET_BACKEND = "secret.backend"
ATTR_SECRET_FAIL_CLASS = "secret.fail.class"
ATTR_SECRET_CACHE_TIER_OVERHEAD_MS = "secret.cache.tier_overhead_ms"
ATTR_SECRET_POLICY_ACCESS_DECISION_REASON = "secret.policy.access_decision_reason"


_SANDBOX_TIER_RANK: Mapping[SandboxTier, int] = {
    SandboxTier.TIER_1_PROCESS: 1,
    SandboxTier.TIER_2_CONTAINER: 2,
    SandboxTier.TIER_3_MICROVM: 3,
    SandboxTier.TIER_4_FULL_VM: 4,
}


class RuntimeToolDispatcher:
    """Per-step TOOL_STEP dispatcher (C-RT-19 §14.9.1 surface).

    Satisfies the `AsyncStepDispatcher` Protocol. Stage 5 binds via
    `ctx.tool_dispatcher`; the step-dispatcher registry resolves
    `TOOL_STEP → ctx.tool_dispatcher`.

    Per spec §14.9.6:
    - inv 3: trust evaluated every dispatch (no caching)
    - inv 4: schema validation at both directions
    - inv 6: no retry inside the dispatcher (handled at C-RT-16 wrap)
    """

    def __init__(
        self,
        *,
        mcp_client_hosts: dict[ServerName, MCPClientHost],
        routing_index: dict[ToolName, ServerName],
        per_server_trust_evaluator: PerServerTrustEvaluator,
        mcp_namespace_emitter: MCPClientNamespaceEmitter,
        trust_policy: TrustPolicy,
        sandbox_decision_resolvers: dict[ServerName, SandboxDecisionResolver] | None = None,
        tracer_provider: Any = None,
        cost_chain: Any = None,
        audit_writer: Any = None,
        rate_table: Any = None,
        cost_record_sink: SupportsCostRecordAppend | None = None,
        tool_execution_drivers: dict[ServerName, ToolExecutionDriver] | None = None,
        provider_secret_resolver: Any = None,
        secret_fetch_audit_emitter: Callable[[SecretFetchEvent], Any] | None = None,
        secret_fetch_backend: str = "provider-secret-resolver",
        effect_fence: EffectFenceProtocol | None = None,
        effect_fencing_explicit: bool = False,
    ) -> None:
        """Construct dispatcher with the cross-axis dependencies (multi-server).

        Per spec §14.9.10 D2 (multi-server reshape): the dispatcher holds N hosts
        keyed by `server_name` + a derived tool→server `routing_index`, and
        resolves each `TOOL_STEP`'s `tool_id` to its owning host (`tool_registry`,
        trust gate, sandbox resolver/driver, `call_tool`) per dispatch. For the
        single-host case (most unit tests) use `RuntimeToolDispatcher.for_single_host`.

        Parameters
        ----------
        mcp_client_hosts:
            U-RT-63/64/65/66 clients keyed by `server_name` (U-RT-125/126). The
            dispatcher resolves the owning host per `tool_id` via `routing_index`,
            then reads its `tool_registry` + invokes `call_tool`.
        routing_index:
            U-RT-127 derived tool→server lookup (`dict[ToolName, ServerName]`).
            A `tool_id` in no host's registry is absent here → dispatch raises
            `RT-FAIL-TOOL-CONTRACT-UNKNOWN`. The per-host registries stay the
            authority; this index is a synchronized lookup, never a 2nd authority.
        per_server_trust_evaluator:
            U-CP-68 evaluator. Invoked pre-call per spec §14.9.2 inv 2.
        mcp_namespace_emitter:
            U-CP-69 emitter. Mutates the `mcp.tool.call` span with the
            7-attribute `mcp.*` namespace per C-AS-14 §14.3.
        trust_policy:
            Immutable `TrustPolicy` loaded at bootstrap; passed to every
            `evaluate()` call (caching is FORBIDDEN per spec §14.9.2 inv 2
            since operators may revoke between dispatches).
        sandbox_decision_resolvers:
            Per-host operator-supplied resolvers keyed by `server_name`
            (U-RT-130). The resolved host's resolver returns the
            `SandboxDispatchDecision` per dispatch; a host absent from this map
            falls through to the default resolver, which RAISES on production
            misconfig (fail-loud, never silent in-process — FR-2).
        tool_execution_drivers:
            Per-host operator-supplied execution drivers keyed by `server_name`
            (U-RT-130 / R-410). A host absent from this map falls through to the
            default `MCPHostToolExecutionDriver` (the pre-R-410 host-call path).
        tracer_provider:
            OTel `TracerProvider`-shaped object (typed `Any` to avoid SDK
            coupling per C-RT-04 pattern). Used to open `tool.dispatch` +
            `sandbox.enter` + `mcp.tool.call` + `sandbox.exit` spans. If
            `None`, span emission is skipped (test-injection seam).
        cost_chain:
            U-OD-39 cost-attribution chain (`ctx.cost_chain`) materialized
            at stage 4 OD. Required at production construction (bootstrap
            stage 5); defaulted to None to preserve construction ergonomics
            for unit tests that only exercise the dispatch surface without
            cost-attribution. When any of cost_chain / audit_writer /
            rate_table is None, `_attribute_tool_cost_best_effort`
            early-returns (no-op).
        audit_writer:
            U-OD-39 audit-ledger writer (`ctx.audit_writer`) materialized at
            stage 4 OD. Same None-default discipline as `cost_chain`.
        rate_table:
            U-OD-39 PRICE_TABLE_REF (`RATE_TABLE_V1` at v1) materialized at
            stage 4 OD. Same None-default discipline as `cost_chain`.
        """
        self._mcp_client_hosts = mcp_client_hosts
        self._routing_index = routing_index
        self._trust_evaluator = per_server_trust_evaluator
        self._mcp_emitter = mcp_namespace_emitter
        self._trust_policy = trust_policy
        # Per-host sandbox resolvers/drivers (U-RT-130), resolved by the owning
        # `server_name` at dispatch. A host missing a resolver falls through to
        # `_default_sandbox_decision_resolver` (RAISES — fail-loud, never silent
        # under-sandbox); a host missing a driver falls through to the default
        # host-call driver.
        self._sandbox_resolvers: dict[ServerName, SandboxDecisionResolver] = (
            sandbox_decision_resolvers or {}
        )
        self._tracer_provider = tracer_provider
        # U-OD-39 cost-attribution substrate (per OD spec §C-OD-26.2 row
        # "tool.dispatch"). None-default preserves unit-test ergonomics;
        # production stage-5 binding sets all 3 substrates per
        # `materialize_runtime_tool_dispatcher_stage` kwargs.
        self._cost_chain = cost_chain
        self._audit_writer = audit_writer
        self._rate_table = rate_table
        # R-FS-1 arc CA — run-scoped cost-record sink (same list as
        # `ctx.cost_record_accumulator`, threaded by the stage-5 factory).
        # `_attribute_tool_cost_best_effort` appends each dispatch's returned
        # SpanCostRecord for `RunResult.cost_attribution` rollup (runtime v1.53 §9).
        self._cost_record_sink = cost_record_sink
        self._tool_execution_drivers: dict[ServerName, ToolExecutionDriver] = (
            tool_execution_drivers or {}
        )
        self._default_tool_execution_driver = MCPHostToolExecutionDriver()
        self._provider_secret_resolver = provider_secret_resolver
        self._secret_fetch_audit_emitter = secret_fetch_audit_emitter
        self._secret_fetch_backend = secret_fetch_backend
        # B-EFFECT-FENCE (runtime spec §14.22 C-RT-31) — at-most-once execution
        # of non-idempotent effects. `effect_fence` None → no reserve, no claim
        # files, byte-identical to pre-v1.60.
        self._effect_fence = effect_fence
        # B-EFFECT-FENCE-DURABLE-AUTO (runtime spec §14.22.7) — `effect_fencing_explicit`
        # is the operator's `RuntimeConfig.effect_fencing` opt-in: True → fence EVERY
        # tool step (the pre-v1.60 semantic); False → AUTO-fence only when the RUN
        # engine class (`step_context.run_engine_class` = `manifest_entry.engine_class`)
        # is a durable-execution engine (§14.22.7 "per-run reserve gate keyed on the
        # engine class"), keeping non-durable runs fence-free.
        # The stage-5 factory now constructs the fence UNCONDITIONALLY (its claim dir
        # is created lazily on first reserve, so a never-reserving run is dir-free),
        # so the per-run gate — not fence presence — decides whether a reserve fires.
        self._effect_fencing_explicit = effect_fencing_explicit

    @classmethod
    def for_single_host(
        cls,
        *,
        mcp_client_host: MCPClientHost,
        per_server_trust_evaluator: PerServerTrustEvaluator,
        mcp_namespace_emitter: MCPClientNamespaceEmitter,
        trust_policy: TrustPolicy,
        sandbox_decision_resolver: SandboxDecisionResolver | None = None,
        tracer_provider: Any = None,
        cost_chain: Any = None,
        audit_writer: Any = None,
        rate_table: Any = None,
        cost_record_sink: SupportsCostRecordAppend | None = None,
        tool_execution_driver: ToolExecutionDriver | None = None,
        provider_secret_resolver: Any = None,
        secret_fetch_audit_emitter: Callable[[SecretFetchEvent], Any] | None = None,
        secret_fetch_backend: str = "provider-secret-resolver",
        effect_fence: EffectFenceProtocol | None = None,
        effect_fencing_explicit: bool = False,
    ) -> RuntimeToolDispatcher:
        """Single-host convenience constructor — the degenerate 1-host case.

        Wraps `mcp_client_host` into the multi-server shape: a 1-entry
        `mcp_client_hosts` keyed on the host's `server_name`, a `routing_index`
        auto-built from that host's `tool_registry.names()` (every tool routes to
        the sole host), and 1-entry per-host resolver/driver maps when supplied.
        Used by the dispatch unit tests + any single-server caller; the
        production stage-5 factory uses the multi-server `__init__` directly.

        The host's registry MUST be populated (started) before this call — the
        index snapshots `tool_registry.names()` (the established test pattern is
        `host = await _build_started_*(...)` before construction).
        """
        server_name = cast("ServerName", mcp_client_host.server_name)
        routing_index: dict[ToolName, ServerName] = {
            name: server_name for name in mcp_client_host.tool_registry.names()
        }
        return cls(
            mcp_client_hosts={server_name: mcp_client_host},
            routing_index=routing_index,
            per_server_trust_evaluator=per_server_trust_evaluator,
            mcp_namespace_emitter=mcp_namespace_emitter,
            trust_policy=trust_policy,
            sandbox_decision_resolvers=(
                {server_name: sandbox_decision_resolver}
                if sandbox_decision_resolver is not None
                else None
            ),
            tracer_provider=tracer_provider,
            cost_chain=cost_chain,
            audit_writer=audit_writer,
            rate_table=rate_table,
            cost_record_sink=cost_record_sink,
            tool_execution_drivers=(
                {server_name: tool_execution_driver} if tool_execution_driver is not None else None
            ),
            provider_secret_resolver=provider_secret_resolver,
            secret_fetch_audit_emitter=secret_fetch_audit_emitter,
            secret_fetch_backend=secret_fetch_backend,
            effect_fence=effect_fence,
            effect_fencing_explicit=effect_fencing_explicit,
        )

    def _attribute_tool_cost_best_effort(
        self,
        *,
        outer_span: Any,
        tool_id: str,
        tool_args: Any,
        response: Any,
        idempotency_key: str,
        step_context: StepExecutionContext,
    ) -> None:
        """U-OD-39 best-effort cost-attribution invocation per §C-OD-26.1.

        Calls `attribute_tool_dispatch_cost`. On success, emits the
        cost.attributed_decimal OTel attribute on `outer_span` via U-OD-49
        string-form. On failure (rate-table missing OR upstream cost-chain
        failure), swallows the exception — cost-attribution is observability,
        not contract, and MUST NOT fail the dispatch per AC #1
        (invoked on every dispatch, success + failure paths).

        Mirror of `_invoke_cost_attribution` at `llm_dispatch.py`. Defer
        imports to method body to keep `runtime_tool_dispatcher.py`'s cold
        import surface narrow (cost-attribution path pulls OD + CXA types
        transitively).
        """
        from decimal import Decimal

        from harness_od.cost_record_otel_serializer import (
            COST_ATTRIBUTED_DECIMAL_ATTR,
            serialize_decimal_for_otel,
        )

        from harness_runtime.lifecycle.cost_attribution_tool_dispatch import (
            attribute_tool_dispatch_cost,
        )

        if self._cost_chain is None or self._audit_writer is None or self._rate_table is None:
            # Cost-attribution substrate not bound — unit-test path.
            return

        if outer_span is None:
            # No active span (test-injection seam with tracer_provider=None)
            # — cost-attribution requires a span_id for SpanCostRecord.
            return

        span_context = outer_span.get_span_context()
        span_id_hex = format(span_context.span_id, "016x")

        try:
            attached = attribute_tool_dispatch_cost(
                rate_table=self._rate_table,
                cost_chain=self._cost_chain,
                audit_writer=self._audit_writer,
                tool_id=tool_id,
                tool_args=tool_args,
                response=response,
                span_id=span_id_hex,
                idempotency_key=idempotency_key,
                parent_idempotency_key=step_context.parent_idempotency_key,
                workflow_id=step_context.workflow_id,
                parent_action_id=step_context.parent_action_id,
                tenant_id=None,
            )
        except Exception:
            # Cost-attribution is observability, not contract. Swallow.
            return

        # Emit cost.attributed_decimal OTel attribute via U-OD-49 string-form
        # preserving the float→Decimal serialization at the OTel boundary.
        outer_span.set_attribute(
            COST_ATTRIBUTED_DECIMAL_ATTR,
            serialize_decimal_for_otel(Decimal(str(attached.total_cost))),
        )

        # R-FS-1 arc CA — record into the run-scoped accumulator for the
        # `RunResult.cost_attribution` rollup (runtime spec v1.53 §9 C-RT-09).
        if self._cost_record_sink is not None:
            self._cost_record_sink.append(attached)

    def _emit_sandbox_violation(
        self,
        tracer: Any,
        mcp_fail_class: MCPInvocationFailClass,
        idempotency_key: str,
    ) -> None:
        """Open the `sandbox.violation` child span with fail-class + idempotency attrs.

        Per AS spec v1.6 §15.9 dual-attribute emission discipline: the
        `sandbox.violation` event carries BOTH `mcp.fail.class` (direct from
        §15.8 enum) AND `sandbox.fail.class` (F4 projected via §15.10).
        Per §15.6 row 1 idempotency-key join: the event also carries the
        parent `tool.call` `idempotency_key` as the cross-axis join key
        for cost-attribution (D6) and engine event history (D1).
        """
        if tracer is None:
            return
        projected = project_mcp_to_sandbox_fail_class(mcp_fail_class)
        with tracer.start_as_current_span("sandbox.violation") as span:
            _set(span, ATTR_MCP_FAIL_CLASS, mcp_fail_class.value)
            _set(span, ATTR_SANDBOX_FAIL_CLASS, projected.value)
            _set(span, ATTR_IDEMPOTENCY_KEY, idempotency_key)

    def _emit_secret_fetch_span(
        self,
        tracer: Any,
        *,
        required_secret: SecretAllowlistEntry,
        backend: str,
        cache_tier_overhead_ms: int,
        policy_access_decision_reason: str,
        fail_class: SecretFailClass | None = None,
    ) -> None:
        """Open the structure-only `secret.fetch` span for one required secret."""
        if tracer is None:
            return
        with tracer.start_as_current_span("secret.fetch") as span:
            _set(span, ATTR_SECRET_NAME, required_secret.name)
            _set(span, ATTR_SECRET_SCOPE, required_secret.scope.name)
            _set(span, ATTR_SECRET_BACKEND, backend)
            _set(span, ATTR_SECRET_CACHE_TIER_OVERHEAD_MS, cache_tier_overhead_ms)
            _set(
                span,
                ATTR_SECRET_POLICY_ACCESS_DECISION_REASON,
                policy_access_decision_reason,
            )
            if fail_class is not None:
                _set(span, ATTR_SECRET_FAIL_CLASS, fail_class.value)

    def _resolve_and_emit_required_secrets(
        self,
        *,
        contract: ToolContract,
        sandbox_decision: SandboxDispatchDecision,
        step: WorkflowStep,
        step_context: StepExecutionContext,
        tracer: Any,
    ) -> None:
        """Resolve `ToolContract.required_secrets` at the workflow TOOL_STEP site.

        R-CXA-1 producer: the dispatcher has the active workflow/step identity
        and the resolved sandbox tier, so this is the first non-hollow
        production call site for AS secret-fetch audit emission.
        """
        if not contract.required_secrets:
            return
        if self._provider_secret_resolver is None:
            raise SecretResolutionError(
                SecretFailClass.SECRET_UNAVAILABLE,
                "provider-secret-resolver",
            )
        if self._secret_fetch_audit_emitter is None:
            raise RuntimeError(
                "required_secrets cannot be resolved without secret_fetch_audit_emitter"
            )
        resolve_with_metadata = getattr(
            self._provider_secret_resolver,
            "resolve_with_audit_metadata",
            None,
        )
        if resolve_with_metadata is None:
            raise SecretResolutionError(
                SecretFailClass.SECRET_UNAVAILABLE,
                "provider-secret-resolver",
            )

        for required_secret in contract.required_secrets:
            try:
                resolved = resolve_with_metadata(
                    required_secret.name,
                    required_secret.scope,
                    sandbox_decision.tier,
                    tool=contract,
                )
                last_rotated_at = getattr(resolved, "secret_last_rotated_at", None)
                if not isinstance(last_rotated_at, str) or not last_rotated_at.strip():
                    raise SecretResolutionError(
                        SecretFailClass.SECRET_UNAVAILABLE,
                        required_secret.name,
                    )
            except SecretResolutionError as exc:
                self._emit_secret_fetch_span(
                    tracer,
                    required_secret=required_secret,
                    backend=self._secret_fetch_backend,
                    cache_tier_overhead_ms=0,
                    policy_access_decision_reason="resolution_failed",
                    fail_class=exc.fail_class,
                )
                raise
            except SecretAllowlistDeniedError as exc:
                self._emit_secret_fetch_span(
                    tracer,
                    required_secret=required_secret,
                    backend=self._secret_fetch_backend,
                    cache_tier_overhead_ms=0,
                    policy_access_decision_reason=exc.decision.value,
                    fail_class=SecretFailClass.SECRET_LOCKED,
                )
                raise

            event = SecretFetchEvent(
                secret_name=required_secret.name,
                secret_scope=required_secret.scope,
                secret_last_rotated_at=last_rotated_at,
                actor=step_context.parent_actor,
                timestamp=datetime.now(UTC),
                thread_id=Identifier(step_context.workflow_id),
                step_id=Identifier(step.step_id),
            )
            self._secret_fetch_audit_emitter(event)
            self._emit_secret_fetch_span(
                tracer,
                required_secret=required_secret,
                backend=str(getattr(resolved, "backend", self._secret_fetch_backend)),
                cache_tier_overhead_ms=int(getattr(resolved, "cache_tier_overhead_ms", 0)),
                policy_access_decision_reason=str(
                    getattr(resolved, "policy_access_decision_reason", "permitted")
                ),
                fail_class=None,
            )

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Invoke the TOOL_STEP body per spec §14.9.1 11-step sequence.

        Per the `AsyncStepDispatcher` Protocol the `step_context` carries
        per-step parent context; this dispatcher consumes
        `parent_idempotency_key` for idempotency-key composition (step 6).
        `binding` is reserved for future per-step override surfaces (e.g.,
        per-step trust-tier overrides at C-CP-19); the effect-fence gate (step 6b,
        B-EFFECT-FENCE-DURABLE-AUTO) keys on `step_context.run_engine_class` (the
        RUN engine class), NOT `binding.engine_class` (which carries a per-step
        override) — see the step-6b comment.

        :returns: `Mapping[str, Any]` (step body output) per
            `AsyncStepDispatcher` Protocol contract.
        """
        _ = binding  # reserved for future per-step override surfaces

        payload = step.step_payload
        tool_id_raw = payload.get("tool_id")
        if not isinstance(tool_id_raw, str) or not tool_id_raw:
            raise ToolContractUnknownError(
                f"TOOL_STEP payload missing or non-str 'tool_id' "
                f"(step_id={step.step_id!r}, payload_keys="
                f"{sorted(payload.keys())})"
            )
        tool_id: str = tool_id_raw
        tool_args_raw: Any = payload.get("tool_args")
        if tool_args_raw is None:
            tool_args: Mapping[str, Any] = {}
        elif isinstance(tool_args_raw, Mapping):
            tool_args = cast("Mapping[str, Any]", tool_args_raw)
        else:
            raise ToolContractUnknownError(
                f"TOOL_STEP 'tool_args' must be a mapping (got {type(tool_args_raw).__name__})"
            )

        # --- Step 0: resolve the owning host via the routing index (U-RT-128) -
        # The §14.9.10 D2 routing index maps each tool to its owning host's
        # `server_name`; a tool_id in no host's registry is absent from the index
        # → RT-FAIL-TOOL-CONTRACT-UNKNOWN. The resolved host's per-server
        # registry / trust / sandbox resolver / driver are then read below.
        server_name = self._routing_index.get(cast("ToolName", tool_id))
        if server_name is None:
            raise ToolContractUnknownError(
                f"RT-FAIL-TOOL-CONTRACT-UNKNOWN: tool_id={tool_id!r} in no "
                f"configured MCP host's registry (not in the routing index)"
            )
        host = self._mcp_client_hosts[server_name]
        sandbox_resolver = self._sandbox_resolvers.get(
            server_name, _default_sandbox_decision_resolver
        )
        tool_execution_driver = self._tool_execution_drivers.get(
            server_name, self._default_tool_execution_driver
        )

        # --- Step 1: resolve ToolContract from the resolved host's registry ---
        try:
            contract: ToolContract = host.tool_registry.get(
                tool_id  # type: ignore[arg-type]
            )
        except Exception as exc:
            raise ToolContractUnknownError(
                f"RT-FAIL-TOOL-CONTRACT-UNKNOWN: tool_id={tool_id!r} not "
                f"registered at MCPClientHost(server="
                f"{host.server_name!r})"
            ) from exc

        # --- Step 2: per-server-trust gate (no caching per inv 2) -----------
        trust_eval = await self._trust_evaluator.evaluate(
            host.server_name,
            MCPPrimitive.TOOL,
            contract,
            self._trust_policy,
        )
        if not trust_eval.permitted:
            raise ToolInvocationTrustViolationError(
                f"RT-FAIL-TOOL-INVOCATION-TRUST-VIOLATION: server="
                f"{host.server_name!r} tool={tool_id!r} "
                f"decision_reason={trust_eval.decision_reason.value}"
            )

        # --- Step 3-5: open tool.dispatch + sandbox.enter; tier-floor -------
        tracer = (
            self._tracer_provider.get_tracer("harness.runtime.tool_dispatch")
            if self._tracer_provider is not None
            else None
        )
        sandbox_decision = sandbox_resolver(contract, step)
        # Tier-floor enforcement per spec §14.9.6 inv 2.
        if _SANDBOX_TIER_RANK[sandbox_decision.tier] < _SANDBOX_TIER_RANK[contract.minimum_tier]:
            raise SandboxTierFloorViolationError(
                f"RT-FAIL-SANDBOX-TIER-FLOOR-VIOLATION: tool={tool_id!r} "
                f"resolved_tier={sandbox_decision.tier.value} "
                f"< minimum_tier={contract.minimum_tier.value}"
            )

        outer_span_cm = (
            tracer.start_as_current_span("tool.dispatch") if tracer is not None else _null_span_cm()
        )
        with outer_span_cm as outer_span:
            _set(outer_span, ATTR_STEP_ID, step.step_id)
            _set(outer_span, ATTR_STEP_KIND, step.step_kind.value)
            _set(outer_span, ATTR_TOOL_CONTRACT_NAME, contract.name)

            sandbox_enter_cm = (
                tracer.start_as_current_span("sandbox.enter")
                if tracer is not None
                else _null_span_cm()
            )
            with sandbox_enter_cm as sandbox_enter_span:
                _set(sandbox_enter_span, ATTR_SANDBOX_TIER, sandbox_decision.tier.value)
                _set(sandbox_enter_span, ATTR_SANDBOX_TECH, sandbox_decision.tech)
                _set(sandbox_enter_span, ATTR_SANDBOX_PROVIDER, sandbox_decision.provider)
                _set(
                    sandbox_enter_span,
                    ATTR_SANDBOX_POLICY_ASSIGNED_TIER_REASON,
                    sandbox_decision.assigned_tier_reason,
                )
                _set(
                    sandbox_enter_span,
                    ATTR_SANDBOX_COST_TIER_OVERHEAD_MS,
                    sandbox_decision.cost_tier_overhead_ms,
                )
                _set(sandbox_enter_span, ATTR_SANDBOX_FAIL_CLASS, "")

            self._resolve_and_emit_required_secrets(
                contract=contract,
                sandbox_decision=sandbox_decision,
                step=step,
                step_context=step_context,
                tracer=tracer,
            )

            # --- Step 6: compose idempotency key (per parent step) ----------
            idempotency_key = _compose_idempotency_key(
                step_context.parent_idempotency_key, step.step_id, tool_id
            )

            # --- Step 6b: effect fence (B-EFFECT-FENCE, §14.22 C-RT-31) ------
            # At-most-once EXECUTION: crash-atomically claim this effect's
            # idempotency_key BEFORE `call_tool` fires. The FIRST dispatch wins
            # (fires); any re-dispatch of the SAME effect — a crash-then-resume
            # re-run of an effected-but-uncommitted step (the
            # `_determine_resume_at` prefix-skip protects only COMMITTED steps),
            # or an in-process `RetryBreakerToolDispatcher` retry — LOSES and
            # fail-closes to §22.1 HITL rather than re-fire the non-idempotent
            # effect.
            #
            # B-EFFECT-FENCE-DURABLE-AUTO (§14.22.7) — the fence is now constructed
            # unconditionally (lazy claim dir), so a PER-RUN gate decides whether a
            # reserve fires: the operator's explicit `effect_fencing=True` (fence
            # every step) OR an AUTO-activation when this RUN's engine class is a
            # durable-execution engine (a crash-resume re-dispatches uncommitted
            # steps there). Non-durable runs without the opt-in skip the reserve →
            # no claim file → byte-identical to pre-v1.60.
            #
            # Keys on `step_context.run_engine_class` (the WORKFLOW/RUN engine class,
            # `manifest_entry.engine_class`), NOT `binding.engine_class`: the latter
            # resolves a per-step `StepOverride.engine_class`, so a per-step override
            # to a non-durable class on a DURABLE workflow would wrongly skip the fence
            # for a step the RUN still resumes + re-dispatches (Codex [P2]). What
            # governs resume is the run engine class. `None` (unset / non-driver path)
            # → not in the durable set → gate closed (safe default).
            #
            # B-EFFECT-FENCE-PER-TOOL (AS spec C-AS-03 §3.1 v1.12 / runtime spec
            # §14.22.7) — a tool whose contract declares `idempotent=True` is EXEMPT
            # from the reserve even when the gate is open: re-executing it produces no
            # additional external effect, so it fires + is safely retryable (the
            # interim fence over-fenced ALL tools + over-conservatively fail-closed an
            # idempotent tool's transient retry). Default `idempotent=False` → fenced
            # (byte-identical to pre-v1.12). The strict tool-intrinsic all-invocations
            # semantic is the contract author's assertion (AS §3.1); the harness keys
            # the exemption off it, the conservative default protecting undeclared tools.
            _fence_gate_open = (
                self._effect_fencing_explicit
                or (step_context.run_engine_class in _DURABLE_AUTO_FENCE_ENGINE_CLASSES)
            ) and not contract.idempotent
            # B-EFFECT-FENCE-PAUSE-RESOLUTION (§14.22.9) — a KEY-BOUND operator resolution
            # the driver threaded onto the RESUMED step's context. Bind it to THIS
            # dispatch's effect: apply ONLY when the directive's `idempotency_key` equals
            # the recomputed key (so a stale resolution can never mis-apply to a different
            # fenced step — correctness-by-construction). `None` / non-match → the fence
            # behaves exactly as pre-v1.73 (the INERT re-pause is preserved).
            _fence_resolution = (
                step_context.effect_fence_resolution.resolution
                if (
                    step_context.effect_fence_resolution is not None
                    and step_context.effect_fence_resolution.idempotency_key == idempotency_key
                )
                else None
            )
            if (
                self._effect_fence is not None
                and _fence_gate_open
                and _fence_resolution is EffectFenceResolution.RE_FIRE
                and self._effect_fence.try_consume_refire(idempotency_key)
            ):
                # RE_FIRE — the operator asserts the prior attempt did NOT fire (it
                # claimed the reserve, then crashed before firing). Clear the held claim
                # so the `try_reserve` below WINS and fires the effect FRESH: a FIRST-and-
                # only execution, still at-most-once from the true state of the world.
                # Done before the reserve so the normal step-7 `call_tool` + post-fire
                # `capture_output` + success-path emission all run unchanged.
                #
                # CONSUME-ONCE (Codex [P1]): the clear is gated on `try_consume_refire`,
                # a durable one-way latch. Only the FIRST RE_FIRE attempt wins it + clears;
                # a `RetryBreakerToolDispatcher` retry of the re-fire (reusing the same
                # `step_context`) — or a crash-then-resume after the re-fire began — LOSES
                # the latch → does NOT clear → `try_reserve` below loses to the re-fire's
                # own held claim → the normal read_output split (suppress-if-captured /
                # ambiguous-PAUSE) applies. So a retryable error during the re-fire can
                # NEVER clear-and-double-fire the non-idempotent effect.
                self._effect_fence.clear_claim(idempotency_key)
            if (
                self._effect_fence is not None
                and _fence_gate_open
                and not self._effect_fence.try_reserve(idempotency_key)
            ):
                # The reserve was lost to a prior uncommitted attempt of THIS effect (a
                # crash-then-resume re-run of an effected-but-uncommitted step, or an
                # in-process retry). (RE_FIRE was handled above — it cleared the claim, so
                # `try_reserve` WON and we are not here.)
                #
                # B-EFFECT-FENCE-PAUSE-RESOLUTION (§14.22.9) — a key-bound SKIP_AS_FIRED /
                # ABORT resolution acts BEFORE the auto captured-output split:
                if _fence_resolution is EffectFenceResolution.SKIP_AS_FIRED:
                    # Operator asserts the effect FIRED; the prior attempt's output was
                    # lost in the fire→capture crash window and is unrecoverable → proceed
                    # with EMPTY output, NEVER re-fire. Balance the pre-fence
                    # `sandbox.enter` with `sandbox.exit` (the suppress-and-continue
                    # precedent); no `mcp.tool.call` span (no effect fired this dispatch).
                    sandbox_exit_cm = (
                        tracer.start_as_current_span("sandbox.exit")
                        if tracer is not None
                        else _null_span_cm()
                    )
                    with sandbox_exit_cm as sandbox_exit_span:
                        _set(sandbox_exit_span, ATTR_SANDBOX_FAIL_CLASS, "")
                    return {
                        "tool_id": tool_id,
                        "response": {},
                        "idempotency_key": idempotency_key,
                        "trust_decision_reason": trust_eval.decision_reason.value,
                        "sandbox_tier": sandbox_decision.tier.value,
                    }
                if _fence_resolution is EffectFenceResolution.ABORT:
                    # Operator cannot determine whether the effect fired (or declines to
                    # proceed) → fail the run terminally (the driver name-matches
                    # `EffectFenceAbortedError` → FAILED); never re-fire, never proceed.
                    raise EffectFenceAbortedError(idempotency_key=idempotency_key)
                # B-EFFECT-FENCE-HITL-ROUTE (§14.22 two-case split) — no resolution (a
                # naive resume, or the first ambiguous pause). Split on whether the prior
                # attempt captured a validated output:
                captured_output = self._effect_fence.read_output(idempotency_key)
                if captured_output is not None:
                    # OUTPUT PRESENT + valid → the effect demonstrably completed AND
                    # its result is in hand → suppress-and-continue: return the
                    # captured output, NEVER re-fire `call_tool`. The metadata fields
                    # are recomputed THIS dispatch (trust/sandbox resolved pre-fence),
                    # so the wrapper shape is byte-identical to a fresh success return.
                    # Emit `sandbox.exit` (fail_class="") to BALANCE the `sandbox.enter`
                    # already emitted pre-fence (the success-path step 9-10 emission) —
                    # a suppress is a successful dispatch, so its sandbox lifecycle must
                    # close; no `mcp.tool.call` span (no effect was re-fired). [Codex P2]
                    sandbox_exit_cm = (
                        tracer.start_as_current_span("sandbox.exit")
                        if tracer is not None
                        else _null_span_cm()
                    )
                    with sandbox_exit_cm as sandbox_exit_span:
                        _set(sandbox_exit_span, ATTR_SANDBOX_FAIL_CLASS, "")
                    return {
                        "tool_id": tool_id,
                        "response": captured_output,
                        "idempotency_key": idempotency_key,
                        "trust_decision_reason": trust_eval.decision_reason.value,
                        "sandbox_tier": sandbox_decision.tier.value,
                    }
                # OUTPUT ABSENT or CORRUPT → the crash fell in the fire→capture window
                # → whether the effect fired is genuinely ambiguous → fail to the
                # operator (the driver routes to a §26.2 EFFECT_FENCE_AMBIGUOUS PAUSE
                # when a PauseResumeProtocol is bound, else FAILED). NEVER auto-re-fire.
                raise EffectFenceAmbiguousUncommittedError(idempotency_key=idempotency_key)

            # --- Step 7: invoke + mcp.tool.call span emission ---------------
            # Per AS spec v1.6 §15.9 dual-attribute emission: any MCP-protocol
            # exception opens a `sandbox.violation` child span carrying BOTH
            # `mcp.fail.class` (§15.8 direct) AND `sandbox.fail.class` (F4
            # projected via §15.10) before re-raise. The producer-side
            # mapping owned at this dispatcher per runtime spec §14.9.
            mcp_call_cm = (
                tracer.start_as_current_span("mcp.tool.call")
                if tracer is not None
                else _null_span_cm()
            )
            try:
                with mcp_call_cm as mcp_call_span:
                    # Mutate span with the 7-attribute mcp.* namespace.
                    signature_hash = _compute_primitive_signature_hash(
                        contract.name, contract.input_schema, contract.output_schema
                    )
                    if mcp_call_span is not None:
                        self._mcp_emitter.emit_mcp_call_span(
                            mcp_call_span,
                            host.server_name,
                            MCPPrimitive.TOOL,
                            signature_hash,
                        )
                    response = await tool_execution_driver.call_tool(
                        mcp_client_host=host,
                        sandbox_decision=sandbox_decision,
                        tool_id=tool_id,
                        tool_args=tool_args,
                        idempotency_key=idempotency_key,
                    )
            except ToolInvocationTimeoutError:
                self._emit_sandbox_violation(
                    tracer, MCPInvocationFailClass.TIMEOUT, idempotency_key
                )
                # U-OD-39 AC #1: cost-attribution invoked on every dispatch
                # (success + failure paths). Failure-path response=None;
                # per_output_byte cost_kind degrades to len('"null"')=6 bytes.
                self._attribute_tool_cost_best_effort(
                    outer_span=outer_span,
                    tool_id=tool_id,
                    tool_args=tool_args,
                    response=None,
                    idempotency_key=idempotency_key,
                    step_context=step_context,
                )
                raise
            except ToolInvocationProtocolError:
                self._emit_sandbox_violation(
                    tracer, MCPInvocationFailClass.PROTOCOL_ERROR, idempotency_key
                )
                self._attribute_tool_cost_best_effort(
                    outer_span=outer_span,
                    tool_id=tool_id,
                    tool_args=tool_args,
                    response=None,
                    idempotency_key=idempotency_key,
                    step_context=step_context,
                )
                raise
            except MCPHostUnreachableError:
                self._emit_sandbox_violation(
                    tracer, MCPInvocationFailClass.TRANSPORT, idempotency_key
                )
                self._attribute_tool_cost_best_effort(
                    outer_span=outer_span,
                    tool_id=tool_id,
                    tool_args=tool_args,
                    response=None,
                    idempotency_key=idempotency_key,
                    step_context=step_context,
                )
                raise

            # --- Step 8: response schema validation -------------------------
            try:
                _validate_response_schema(response, contract.output_schema)
            except jsonschema.ValidationError as exc:
                self._emit_sandbox_violation(
                    tracer, MCPInvocationFailClass.SCHEMA_VIOLATION, idempotency_key
                )
                # U-OD-39 AC #1: cost-attribution on schema-violation failure.
                # Response IS available here (validation failed AFTER call),
                # so per_output_byte cost_kind reflects actual response bytes.
                self._attribute_tool_cost_best_effort(
                    outer_span=outer_span,
                    tool_id=tool_id,
                    tool_args=tool_args,
                    response=response,
                    idempotency_key=idempotency_key,
                    step_context=step_context,
                )
                raise ToolInvocationSchemaViolationError(
                    f"RT-FAIL-TOOL-INVOCATION-SCHEMA-VIOLATION: tool="
                    f"{tool_id!r} response failed output_schema validation: "
                    f"{exc.message}"
                ) from exc

            # --- Step 9-10: sandbox.exit span (success path) ----------------
            sandbox_exit_cm = (
                tracer.start_as_current_span("sandbox.exit")
                if tracer is not None
                else _null_span_cm()
            )
            with sandbox_exit_cm as sandbox_exit_span:
                _set(sandbox_exit_span, ATTR_SANDBOX_FAIL_CLASS, "")

            # --- U-OD-39 cost-attribution at success path -------------------
            # AC #1 success branch + AC #3 mcp.tool.call piggyback (1 helper
            # invocation per dispatch attributes entire tool-dispatch surface
            # including nested mcp.tool.call span per §C-OD-26.2 table).
            self._attribute_tool_cost_best_effort(
                outer_span=outer_span,
                tool_id=tool_id,
                tool_args=tool_args,
                response=response,
                idempotency_key=idempotency_key,
                step_context=step_context,
            )

            # --- Step 10b: effect-fence output capture (B-EFFECT-FENCE-HITL-ROUTE)
            # Crash-atomically record the validated output keyed on the same
            # `idempotency_key` as the reserve — post-fire, post-validation,
            # post-cost-attribution, BEFORE the step commits. Gated on the SAME
            # `_fence_gate_open` as the reserve (an idempotent / non-durable
            # un-fenced dispatch captures nothing → byte-identical to pre-v1.72).
            # Reaching here with the gate open implies THIS dispatch won the reserve
            # (a lost reserve returned/raised at step 6b), so the capture is the
            # winner's. "captured ⟹ cost already attributed (above)" → a re-dispatch
            # that suppress-and-continues skips cost-attribution exactly once.
            if self._effect_fence is not None and _fence_gate_open:
                self._effect_fence.capture_output(idempotency_key, response)

            # --- Step 11: wrap response + return ----------------------------
            return {
                "tool_id": tool_id,
                "response": response,
                "idempotency_key": idempotency_key,
                "trust_decision_reason": trust_eval.decision_reason.value,
                "sandbox_tier": sandbox_decision.tier.value,
            }


# --- private helpers -------------------------------------------------------


def _set(span: Any, key: str, value: Any) -> None:
    """Conditional set_attribute (null-span tolerant)."""
    if span is None:
        return
    span.set_attribute(key, value)


class _NullSpanContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: Any) -> None:
        return None


def _null_span_cm() -> _NullSpanContext:
    return _NullSpanContext()


def _compose_idempotency_key(parent_idempotency_key: str, step_id: str, tool_id: str) -> str:
    """Per spec §14.9.7 suggested recipe — sha256 over parent key + step + tool."""
    digest = hashlib.sha256()
    digest.update(parent_idempotency_key.encode("utf-8"))
    digest.update(b":")
    digest.update(step_id.encode("utf-8"))
    digest.update(b":")
    digest.update(tool_id.encode("utf-8"))
    return digest.hexdigest()


def _compute_primitive_signature_hash(
    name: str,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> str:
    """Per C-AS-14 §14.3 tool-poisoning detection: sha256 over the
    primitive's name + input schema + output schema (sorted JSON)."""
    import json

    digest = hashlib.sha256()
    digest.update(name.encode("utf-8"))
    digest.update(b"|")
    digest.update(json.dumps(input_schema, sort_keys=True).encode("utf-8"))
    digest.update(b"|")
    digest.update(json.dumps(output_schema, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _validate_response_schema(
    response: Mapping[str, Any], output_schema: Mapping[str, Any]
) -> None:
    """Validate `response` against JSON Schema `output_schema`.

    Empty schema ({}) bypasses validation. Per spec §14.9.2 inv 4 the
    dispatcher validates BOTH directions; input validation is the
    operator's responsibility at the workflow-driver layer (the dispatcher
    enforces post-call shape only — the inbound `tool_args` already
    crossed the workflow boundary).
    """
    if not output_schema:
        return
    jsonschema.validate(instance=dict(response), schema=dict(output_schema))


# `time` imported above is intentional — reserved for future per-attempt
# latency emission at the C-RT-16 wrap layer per spec §14.9.7 deferral.
_ = time
