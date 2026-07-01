"""Stage 3a factory — `materialize_mcp_client_host_stage(config) → MCPClientHost`.

Per `Spec_Harness_Runtime_v1.md` §14.9.3 stage-3a factory contract (added at
v1.15 per U-RT-68 fork Q2=B2 ratification; reshaped singular→mapping at v1.51
§14.9.10 D1 per the B2 multi-server reshape). Ingests
`config.mcp_clients: list[MCPClientConfig]` (per-server transport_config) and
constructs `MCPClientHost` instances. The factory return value — a
`dict[ServerName, MCPClientHost]` keyed on each host's `server_name` — is bound
to `ctx.mcp_client_hosts` (C-RT-04 v1.51+ field) by the stage 3a body.

The field is now a mapping (§4 C-RT-04 v1.51 / U-RT-125). The factory handles:

- 0 servers: returns an empty dict `{}` (§14.9.10 D1 "empty dict permitted
  when 0 servers configured" — no sentinel host). Tool-dispatch attempts then
  raise `RT-FAIL-TOOL-CONTRACT-UNKNOWN` (empty routing index) at dispatch.
- N servers: returns an N-entry dict, one `MCPClientHost` per `MCPClientConfig`
  entry keyed on its `server_name` (U-RT-126-full — retires the single-server
  `[0]`); subprocess spawn + protocol handshake + `list_tools` registry
  population happen at `start()`, which the stage 3a body invokes per-host
  after factory return.

The cross-host routing index + collision fail-class (U-RT-127) + the dispatch
tool→server resolution (U-RT-128) + the per-host sandbox (U-RT-130) live at the
stage-5 `runtime_tool_dispatcher_factory` (a SEPARATE factory per reshape-fork
F1-03). The §14.9.6 inv-1 per-host lifecycle reword ("each configured host
started exactly once") is realized by the stage-3a body's per-host start loop.

Per `Plan_Executability_Audit_v1.md` framework-pull discipline: no
framework adoption.
"""

from __future__ import annotations

import shlex
from typing import Any, Final, Literal

from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_as.tool_contract import ToolContract
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.cp_shared_types import MCPTrustTier

from harness_runtime.config.sandbox_defaults import resolve_effective_sandbox_defaults
from harness_runtime.lifecycle.mcp_client_host import (
    MCPClientHost,
    MCPToolContractConverter,
)
from harness_runtime.types import MCPClientConfig, RuntimeConfig, ServerName

# Re-declare the Literal type so this module is self-contained for the
# granular→coarse transport projection (the same Literal is declared at
# lifecycle.mcp_client_host as the host's connection-mechanism selector).
_MCPTransportLiteral = Literal["stdio", "streamable_http", "sse"]

__all__ = ["materialize_mcp_client_host_stage"]


_STDIO_URL_PREFIX = "stdio://"


def _coarse_transport(granular: MCPTransport) -> _MCPTransportLiteral:
    """Project the granular per-server `MCPTransport` onto the coarse
    connection-mechanism the host's transport selector consumes.

    `MCPClientConfig.transport` carries the granular `harness_as.discriminators.MCPTransport`
    (C-AS-10 §10.1: `stdio` + `streamable_http_l0..l3`), which encodes BOTH the
    connection mechanism AND the remote trust level. `MCPClientHost` keys only on
    the *mechanism* — how to open the connection (stdio subprocess vs streamable-HTTP
    client pool) — per its `_VALID_TRANSPORTS = {"stdio", "streamable_http", "sse"}`
    coarse selector (runtime spec §14.9.6 inv 5: "STDIO + HTTP + SSE all supported at
    v1"). The L-level is a *trust* dimension consumed elsewhere: the per-MCP-transport
    sandbox floor reads it via `MCPClientConfig.trust_level` (§14.9.8), and the dispatch
    trust gate keys on `server_name`. So all four `streamable_http_l*` values collapse to
    the single coarse `streamable_http` mechanism; `stdio` maps to `stdio`.

    Earlier landed code at the construction site passed `entry.transport.value` straight
    through a `cast` — correct for `stdio` (whose `.value` is the coarse `"stdio"`) but a
    latent defect for every remote value (`"streamable_http_l1"` ∉ `_VALID_TRANSPORTS` →
    `MCPClientHost.__init__` raised `ValueError: unknown MCP transport`). The gap was never
    exercised because no remote MCP host had been materialized through the factory
    (`B-MCP-HOST-REMOTE-TRANSPORT`). This projection restores the intended §14.9.6-inv-5
    behavior; no spec change, no host change, no contract widening.

    REFUSE (remote l0) is NOT guarded here — `mcp_transport_floor` owns REFUSE (one source
    of truth) and stage-2 `materialize_mcp_stage` raises `MCPServerRefusedError` on an l0
    config, aborting the bootstrap BEFORE this stage-3a factory runs (runtime spec §14.9.8
    + §14.9.10 D1). An l0 transport therefore never reaches here.
    """
    if granular is MCPTransport.STDIO:
        return "stdio"
    # All four streamable_http_l0..l3 share the streamable-HTTP connection mechanism;
    # the L-level (trust) is read by the sandbox floor + trust gate, not the host.
    return "streamable_http"


def _build_transport_config(transport: _MCPTransportLiteral, connection_url: str) -> dict[str, Any]:
    """Translate MCPClientConfig.connection_url into the transport_config
    shape MCPClientHost consumes per its per-transport context contracts.

    The host docstrings at `_stdio_connection_context` / `_http_connection_context`
    / `_sse_connection_context` enumerate the required keys per transport:

      - stdio  → `command` (REQUIRED str), `args` (list[str], default []),
                 `env`/`cwd` (deferred — not supplied from MCPClientConfig at v1.17)
      - streamable_http / sse → `url` (REQUIRED str)

    The factory satisfies these contracts. Earlier landed code at this site
    passed `{"connection_url": ...}` unconditionally, which the host rejects
    at `host.start()` time with `ValueError("STDIO transport_config requires
    str 'command'")` — surfaced at U-RT-86 e2e pre-flight check. The fix is
    a factory-side translation; no spec change, no host change.

    Stdio `connection_url` shape: `'stdio://<command> [args...]'` per the
    URL-scheme + shell-style-argv convention at the existing factory test
    fixture `_stdio_client(connection_url="stdio:///bin/echo")`. The prefix
    is stripped and the remainder shlex-split into (command, args).
    """
    if transport == "stdio":
        if not connection_url.startswith(_STDIO_URL_PREFIX):
            raise ValueError(
                f"stdio connection_url must start with {_STDIO_URL_PREFIX!r}; "
                f"got {connection_url!r}"
            )
        remainder = connection_url[len(_STDIO_URL_PREFIX) :]
        parts = shlex.split(remainder)
        if not parts:
            raise ValueError(
                f"stdio connection_url has no command after "
                f"{_STDIO_URL_PREFIX!r}: {connection_url!r}"
            )
        return {"command": parts[0], "args": parts[1:]}
    # streamable_http / sse — host's _http_connection_context /
    # _sse_connection_context consume `url` per their docstrings.
    return {"url": connection_url}


def _build_default_policy_converter(
    entry: MCPClientConfig, deployment_surface: DeploymentSurface
) -> MCPToolContractConverter:
    """Build a Reading-B default-policy `MCPToolContractConverter` (spec v1.40
    §14.9.3).

    The converter stamps every tool discovered from this server with the
    operator-declared per-server defaults (`entry.default_minimum_tier` +
    `entry.default_blast_radius`), mapping an advertised MCP `Tool`
    (name / description / inputSchema) → AS `ToolContract`. This replaces the
    raise-on-every-call `_default_tool_contract_converter` that the host would
    otherwise use, making a TOOL_STEP dispatchable through the operator
    `api.run` path (closes `.harness/class_1_fork_tool_step_no_operator_supplied_converter.md`
    Reading B).

    The `tool` argument is an `mcp.types.Tool` (typed `Any` per the
    `MCPToolContractConverter = Callable[[Any], ToolContract]` alias);
    `description` may be `None` and `inputSchema` is a JSON-schema dict.
    """
    # Reconcile the per-server minimum_tier through the deployment-surface-aware
    # default policy (runtime spec v1.43 §14.9.9 + fork §7.1) so a bare config's
    # minimum_tier stays coherent with the stage-5 resolved sandbox_tier — the
    # §14.9.4 tier-floor never spuriously violates (three-field reconciliation).
    minimum_tier: SandboxTier = resolve_effective_sandbox_defaults(
        entry, deployment_surface
    ).minimum_tier
    blast_radius_tier: BlastRadiusTier = entry.default_blast_radius

    def convert(tool: Any) -> ToolContract:
        input_schema: dict[str, object] = tool.inputSchema or {"type": "object"}
        return ToolContract(
            name=tool.name,
            description=tool.description or "",
            input_schema=input_schema,
            output_schema={"type": "object"},
            minimum_tier=minimum_tier,
            blast_radius_tier=blast_radius_tier,
            # B6 Slice 2 (runtime spec v1.56 §14.9.11): stamp the per-server default
            # ToolMetadata forcing discriminators so the per-tool resolver's C-AS-02 §2.3
            # forcing rows (1-2) + row 7 are reachable on the production MCP-discovered path
            # — not just for manually-built contracts. MCP advertisements carry no forcing
            # semantics, so the per-server default is the Reading-B policy source.
            forces_computer_use=entry.default_forces_computer_use,
            forces_code_execution=entry.default_forces_code_execution,
            is_deterministic_inhouse=entry.default_is_deterministic_inhouse,
            # B-EFFECT-FENCE-PER-TOOL (AS spec C-AS-03 §3.1 v1.12 / runtime §14.22.7):
            # stamp the per-server idempotency default so the effect fence can exempt
            # declared-idempotent discovered tools (MCP advertisements carry no
            # idempotency semantics → the per-server default is the policy source).
            idempotent=entry.default_idempotent,
        )

    return convert


def _build_host(entry: MCPClientConfig, deployment_surface: DeploymentSurface) -> MCPClientHost:
    """Construct one `MCPClientHost` from a single `MCPClientConfig` entry.

    `entry.transport` is an `harness_as.discriminators.MCPTransport` StrEnum
    (granular: `stdio` + `streamable_http_l0..l3`); `_coarse_transport` projects
    it onto the coarse connection-mechanism the host consumes (`stdio` /
    `streamable_http`). The `server_name` is set from `entry.client_name` (the
    `ServerName`/`ClientName` same-value-today property per spec §14.9.10).
    """
    transport_value: _MCPTransportLiteral = _coarse_transport(entry.transport)
    return MCPClientHost(
        transport=transport_value,
        server_name=entry.client_name,
        trust_tier=_trust_tier_from_level(entry.trust_level),
        transport_config=_build_transport_config(transport_value, entry.connection_url),
        tool_contract_converter=_build_default_policy_converter(entry, deployment_surface),
    )


async def materialize_mcp_client_host_stage(
    config: RuntimeConfig,
) -> dict[ServerName, MCPClientHost]:
    """Construct the stage 3a `MCPClientHost` mapping from operator-supplied config.

    Per spec §14.9.3 stage-3a factory contract + **§14.9.10 D1** (v1.51
    multi-server reshape) + AC #1/AC #2/AC #5 at U-RT-73/126.

    Materializes ONE `MCPClientHost` per `config.mcp_clients` entry
    (U-RT-126-full — retires the single-server `[0]`), keyed on each host's
    `server_name`. The stage 3a body starts each host afterward (subprocess
    spawn / HTTP connect / SSE stream + `list_tools` registry population per
    transport). An empty config yields an empty dict `{}` (§14.9.10 D1 "empty
    dict permitted when 0 servers configured") — no sentinel host; a TOOL_STEP
    then raises `RT-FAIL-TOOL-CONTRACT-UNKNOWN` (empty routing index) at dispatch.

    Returns
    -------
    dict[ServerName, MCPClientHost]
        An N-entry mapping of unstarted hosts keyed on `server_name`
        (`{}` when 0 servers configured).
    """
    hosts: dict[ServerName, MCPClientHost] = {}
    for entry in config.mcp_clients:
        host = _build_host(entry, config.deployment_surface)
        server_name = ServerName(host.server_name)
        if server_name in hosts:
            # Fail-loud detect-then-refuse: a duplicate `server_name` (i.e. two
            # entries sharing a `client_name`) cannot satisfy §14.9.10 D1's "one
            # host per entry, keyed by server_name" — the dict would silently drop
            # the earlier host (so stage 3a would start only the last). Caught
            # here at materialize (pre-start) → no started-host leak.
            raise ValueError(
                f"duplicate MCP server_name {server_name!r} in config.mcp_clients "
                f"— each configured server must have a unique client_name "
                f"(spec §14.9.10 D1: one MCPClientHost per entry, keyed by server_name)"
            )
        hosts[server_name] = host
    return hosts


_TRUST_LEVEL_TO_TIER: Final[dict[MCPServerTrustLevel, MCPTrustTier]] = {
    MCPServerTrustLevel.L0_REFUSE_REMOTE: MCPTrustTier.LEVEL_0_REFUSE_REMOTE,
    MCPServerTrustLevel.L1_SIGNED_PINNED: MCPTrustTier.LEVEL_1_SIGNED_PINNED,
    MCPServerTrustLevel.L2_SANDBOX_ALL: MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT: MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
}
"""Identity-by-ordinal projection table (CP spec v1.34 §27.8 / U-RT-129).

The AS-side `MCPServerTrustLevel` and CP-side `MCPTrustTier` are the SAME
closed 4-value set — `MCPTrustTier` is a "byte-exact factor-out of the
AS-owned value set" (C-AS-10 §10.3 / CP §27.8), so identity (`L_k → LEVEL_k`)
is the unique faithful realization. Total over the closed enum.
"""


def _trust_tier_from_level(level: MCPServerTrustLevel) -> MCPTrustTier:
    """Project the per-`MCPClientConfig` `trust_level` (AS-side enum) onto
    the per-`MCPClientHost` `trust_tier` (CP-side enum), identity-by-ordinal.

    Per CP spec **v1.34 §27.8** (U-RT-129): the projection is a faithful
    `L_k → LEVEL_k` identity over the shared closed 4-value set (the prior
    constant-collapse stub returned `LEVEL_0_REFUSE_REMOTE` regardless,
    flattening every server's trust telemetry to the most-restrictive tier).

    **TELEMETRY-ONLY.** The result populates `MCPHostHealth.trust_tier` → the
    `mcp.server.trust_tier` span attribute (`mcp_client_namespace_emitter`).
    It does NOT feed the dispatch trust gate — that keys on `server_name` via
    the per-server `TrustPolicy`/`per_server_trust_evaluator.evaluate`, a path
    byte-unchanged by this projection. No transport-aware clamp here: transport
    severity is owned by the per-transport sandbox floor (a clamp would be a
    one-source-of-truth violation; the narrow `level`-only signature is the
    positive evidence transport belongs to the floor, not the projection).
    """
    return _TRUST_LEVEL_TO_TIER[level]
