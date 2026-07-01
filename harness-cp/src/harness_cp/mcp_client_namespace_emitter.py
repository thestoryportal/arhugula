"""C-CP-27 MCPClientNamespaceEmitter — `mcp.*` 7-attribute namespace emission
at the H_T-as-MCP-client tool-invocation site.

U-CP-69 — fifth unit of cluster 10-CP-C. Implements
`MCPClientNamespaceEmitter.emit_mcp_call_span(span, server_name, primitive,
signature_hash) -> None` per CP spec v1.10 §27.1 + producer-side mutation
discipline per C-AS-14 §14.3 / AS spec v1.4 §14.3 producer-site reference
note.

**7 attributes mutated on the `mcp.tool.call` span site** per C-AS-14 §14.3
verbatim:

| Attribute                          | Source                          |
|------------------------------------|---------------------------------|
| `mcp.server.name`                  | call-site arg `server_name`     |
| `mcp.server.trust_tier`            | per-server info lookup          |
| `mcp.protocol_version`             | per-server info lookup          |
| `mcp.transport`                    | per-server info lookup          |
| `mcp.auth_present`                 | per-server info lookup          |
| `mcp.primitive.kind`               | call-site arg `primitive`       |
| `mcp.primitive.signature.sha256`   | call-site arg `signature_hash`  |

**§27.3 stage placement.** Instantiated at Stage 3a alongside `MCPClientHost`
(runtime spec v1.13 §14.9.3); bound to `ctx.mcp_client_namespace_emitter`.
At Stage 5 the `RuntimeToolDispatcher` (runtime spec v1.13 §14.9) invokes
`ctx.mcp_client_namespace_emitter.emit_mcp_call_span(...)` DURING dispatch
(step 7) to mutate the `mcp.tool.call` span context opened upstream.

**Impl-discretion FACTOR-OUT for per-server runtime info** (mirrors the
U-CP-68 `TierResolver` precedent):

- `MCPServerInfo` — 4-field envelope (transport / protocol_version /
  auth_present / trust_tier) returned by the operator-supplied lookup
- `MCPServerInfoLookup = Callable[[str], MCPServerInfo]` — injected at
  `__init__`; operator wiring decides whether to source from static bootstrap
  config OR dynamic per-server trust evaluation OR a combination

Default lookup raises `LookupError` (no production fallback; operator MUST
inject the lookup at bootstrap per §27.3 stage 3a).

**Transport values.** Per AC #2: `stdio` / `streamable_http` / `sse` (3
transports per runtime spec v1.13 §14.9 / `MCPClientHost`). The AS spec
v1.4 §14.3 enumerates only `stdio` / `streamable_http` (the third `sse`
transport surfaces via the runtime composer extension); the emitter passes
through the operator-supplied string verbatim without enum-validating it
here.

**`mcp.auth_present` discipline.** Per AC #3: `False` on STDIO; per-server
transport-config-driven elsewhere. The emitter is agnostic — it emits the
bool value the lookup returns. Operator wiring at bootstrap enforces the
STDIO-implies-False discipline.

**Authority.** CP spec v1.10 §27.1 + §27.3 (NEW C-CP-27); AS spec v1.4
§14.3 producer-site reference note ("MCPClientNamespaceEmitter per CP §27
owns producer-side mutation"); plan unit U-CP-69 (CP plan v2.15 §1 cluster
10-CP-C, preserved at v2.17).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.per_server_trust_types import MCPPrimitive

# ---------------------------------------------------------------------------
# Span attribute name constants (Pattern-P1 alignment with C-AS-14 §14.3)
# ---------------------------------------------------------------------------

ATTR_MCP_SERVER_NAME: Final[str] = "mcp.server.name"
ATTR_MCP_SERVER_TRUST_TIER: Final[str] = "mcp.server.trust_tier"
ATTR_MCP_PROTOCOL_VERSION: Final[str] = "mcp.protocol_version"
ATTR_MCP_TRANSPORT: Final[str] = "mcp.transport"
ATTR_MCP_AUTH_PRESENT: Final[str] = "mcp.auth_present"
ATTR_MCP_PRIMITIVE_KIND: Final[str] = "mcp.primitive.kind"
ATTR_MCP_PRIMITIVE_SIGNATURE_SHA256: Final[str] = "mcp.primitive.signature.sha256"

MCP_CALL_SPAN_NAME: Final[str] = "mcp.tool.call"
"""Span site mutated by the emitter per C-AS-14 §14.3 + AS v1.4 §14.3
producer-site note."""


# ---------------------------------------------------------------------------
# MCPServerInfo carrier + lookup callable
# ---------------------------------------------------------------------------


class MCPServerInfo(BaseModel):
    """Per-server runtime info envelope consumed by the emitter at call-time.

    Sourced via the operator-supplied `MCPServerInfoLookup` injected at
    `MCPClientNamespaceEmitter.__init__`. The operator decides how to compose
    the fields — typically from static bootstrap server-registry config
    combined with dynamic per-server trust evaluation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: str
    """Transport identifier — `"stdio"` | `"streamable_http"` | `"sse"`
    (AC #2 3-transport set; passed through verbatim, not enum-validated)."""

    protocol_version: str
    """MCP protocol version (e.g., `"2025-06-18"`)."""

    auth_present: bool
    """Whether authentication is present. AC #3: False on STDIO;
    transport-config-driven elsewhere."""

    trust_tier: MCPTrustTier
    """Operator-configured / dynamically-resolved tier for this server."""


MCPServerInfoLookup = Callable[[str], MCPServerInfo]
"""Operator-supplied callable resolving server_name → MCPServerInfo at
emit-time. Injected at `MCPClientNamespaceEmitter.__init__`."""


def _default_info_lookup(server_name: str) -> MCPServerInfo:
    """Default lookup raises — operator MUST inject an info lookup at bootstrap.

    Mirrors the U-CP-68 `_default_tier_resolver` discipline: keep production
    misconfiguration loud rather than silently emitting plausible-looking
    placeholders that would contaminate the audit trail."""
    raise LookupError(
        f"MCPClientNamespaceEmitter requires an operator-supplied "
        f"MCPServerInfoLookup injected at __init__; no info for server "
        f"{server_name!r}. Bootstrap wiring per CP spec v1.10 §27.3 stage 3a."
    )


# ---------------------------------------------------------------------------
# Span-typing protocol (minimal — avoid hard otel-sdk dep)
# ---------------------------------------------------------------------------


class _SpanLike:  # pyright: ignore[reportUnusedClass] — documentation-only span-typing note
    """Structural duck-type alias — any object with `set_attribute(key, value)`
    works as a span at this emitter site.

    Typed as `Any` at the public signature to avoid coupling the CP-axis
    package to the OTel SDK; consumer (runtime composer) supplies the
    `opentelemetry.trace.Span` instance."""


# ---------------------------------------------------------------------------
# MCPClientNamespaceEmitter
# ---------------------------------------------------------------------------


class MCPClientNamespaceEmitter:
    """Emits the `mcp.*` 7-attribute namespace at the H_T-as-MCP-client
    tool-invocation site per C-AS-14 §14.3 + CP spec v1.10 §27.

    Producer-side mutation discipline (AS spec v1.4 §14.3 producer-site
    reference note): the canonical 7-attribute set is declared at AS §14.3;
    this emitter owns the runtime mutation of the `mcp.tool.call` span
    context.

    Instantiated at Stage 3a (per §27.3) alongside `MCPClientHost`; invoked
    by `RuntimeToolDispatcher` at Stage 5 step 7 during dispatch.
    """

    def __init__(
        self,
        *,
        info_lookup: MCPServerInfoLookup | None = None,
    ) -> None:
        """Construct emitter with operator-supplied per-server info lookup.

        :param info_lookup: callable resolving server_name → MCPServerInfo;
            defaults to `_default_info_lookup` which raises LookupError on
            production use (operator MUST inject at bootstrap)."""
        self._info_lookup: MCPServerInfoLookup = info_lookup or _default_info_lookup

    def emit_mcp_call_span(
        self,
        span: Any,
        server_name: str,
        primitive: MCPPrimitive,
        signature_hash: str,
    ) -> None:
        """Mutate `span` with the 7 `mcp.*` attributes per C-AS-14 §14.3.

        :param span: opened `mcp.tool.call` span context (typed Any to avoid
            OTel SDK coupling; any object with `set_attribute(key, value)`
            works)
        :param server_name: MCP server registry identifier
        :param primitive: MCPPrimitive enum value (tool / resource / prompt /
            sampling)
        :param signature_hash: per-primitive content-addressable sha256 hex
            (tool-poisoning detection per AS §14.3 row 7)
        """
        info = self._info_lookup(server_name)
        span.set_attribute(ATTR_MCP_SERVER_NAME, server_name)
        span.set_attribute(ATTR_MCP_SERVER_TRUST_TIER, info.trust_tier.value)
        span.set_attribute(ATTR_MCP_PROTOCOL_VERSION, info.protocol_version)
        span.set_attribute(ATTR_MCP_TRANSPORT, info.transport)
        span.set_attribute(ATTR_MCP_AUTH_PRESENT, info.auth_present)
        span.set_attribute(ATTR_MCP_PRIMITIVE_KIND, primitive.value)
        span.set_attribute(ATTR_MCP_PRIMITIVE_SIGNATURE_SHA256, signature_hash)
