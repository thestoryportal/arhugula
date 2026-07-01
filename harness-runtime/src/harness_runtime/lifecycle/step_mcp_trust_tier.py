"""Per-step MCP-trust-tier resolver — R-FS-1 `B-TOOL-GATE`.

Per CP spec **v1.35 §19.1.2** (the Producer ¶) + runtime **§14.8.2 step-4c**: the
runtime HITL gate composer populates `GateLevelInput.mcp_trust_tier` from the
**resolved owning MCP host's** declared trust — the per-server `MCPTrustTier`
exposed at `MCPClientHost.trust_tier` (§27.8 identity-by-ordinal projection,
landed at U-RT-129/B2) — for the host that owns the step's `tool_id` via the
v1.51 §14.9.10 tool→server routing index (U-RT-128).

**Why this arc exists (the load-bearing finding).** U-RT-131 (B2-impl-3) retired
the harmful `hitl_gate_composer.py` `LEVEL_0_REFUSE_REMOTE` constant in favour of
the L3 *no-floor* default at the only gate sites that existed then — the
**host-less** `PRE_ACTION` (inference) + `SUB_AGENT_BOUNDARY` (sub-agent)
composers (`stage_5_loop_init.py`). `TOOL_STEP`s dispatch through
`runtime_tool_dispatcher.py`, which composed **no** HITL gate, so the §19.1.2
Producer ¶ had no gate site with an owning MCP host to populate. `B-TOOL-GATE`
adds the tool-step gate site (a third `RuntimeHITLGateComposer` wrapping the tool
dispatcher at the registry binding) and **this** resolver is its
`mcp_trust_tier_resolver` — making the §19.1.2 MCP-trust gate axis **non-vacuous
at the tool-step gate, at parity with the inference / sub-agent gates**: when the
gate fires, an L0-trust server's tool floors its gate to `DENY`, an L3 to `AUTO`
(`max()`-no-floor, blast/persona decide).

**Scope honesty — what this arc does NOT do.** The wrap-time HITL gate is
*placement-driven*: the composer fires only when `step.hitl_placements` is
non-empty. At HEAD, `hitl_placements` is declared at the WORKFLOW level
(`WorkflowManifestEntry.hitl_placements`) and is **never bound onto the per-step
`WorkflowStep`** the driver dispatches (which is frozen + `extra="forbid"`), so
NO wrap-time gate — inference, sub-agent, OR this tool gate — actually fires
through the real `WorkflowManifestLoader` → driver path. That per-step
`hitl_placements` producer is a **PRE-EXISTING gap shared by all wrap-time gate
sites** (not introduced here; the existing inference/sub-agent gate e2e tests
attach placements via a `_StepWithPlacements` test proxy for the same reason).
B-TOOL-GATE delivers the *tool-gate-site + the resolved-host MCP-trust feed* (the
§19.1.2-scoped half); production gate-firing for all wrap-time gates awaits that
shared producer, registered as a forward arc — NOT built here.

**Impl-against-cleared-spec.** The composer input is already spec-named
(§14.8.2 step-4c `mcp_server_trust_tier`; §19.1.2 Producer ¶); `MCPTrustTier`,
`GateLevelInput.mcp_trust_tier`, and `MCP_TRUST_GATE_LEVEL_FLOOR` all exist. This
mints **no** new contract surface — it is the resolved-owning-host producer the
spec already describes (runtime plan v2.48 §6 O-RT-7 item 2 — Rec: BUILD).

**Host-scan, not a dispatcher-held index (mirrors the sibling
`make_step_blast_radius_resolver`).** The resolver takes `ctx`, not the
dispatcher, so the dispatcher's `routing_index` is not reachable here. It scans
`ctx.mcp_client_hosts[*].tool_registry` for `tool_id` and returns the owning
host's `trust_tier`. The `RT-FAIL-MCP-TOOL-NAME-COLLISION` bootstrap guarantee (a
`tool_id` advertised by ≥2 hosts aborts startup) makes the tool resolve in **at
most one** host, so the host the gate scores is provably the host the dispatcher
routes to (no gate-L3-but-dispatch-L0 hole). The per-host registries stay the
single source of truth; this is a synchronized read, never a second authority.

**Fail-soft (deliberate, distinct from the blast resolver's fail-safe raise).**
An unresolvable `tool_id` (missing / non-str / not registered) returns `None`
→ the composer falls back to the L3 *no-floor* default. This is safe because:
(i) the `mcp_trust` axis is a `max()` **floor** — a missing floor can only
*under*-gate, never bypass another axis; and (ii) the sibling
`blast_radius_resolver` runs FIRST at step-4c (`hitl_gate_composer.py`) and
**raises** `StepBlastRadiusResolutionError` on the very same unresolvable
`tool_id`, so this resolver is never reached for an unclassifiable tool step.
Returning `None` (not raising) avoids a redundant second fail-safe while keeping
the under-gate impossible.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.workflow_driver_types import StepKind, WorkflowStep

if TYPE_CHECKING:  # pragma: no cover — type-only import (avoid runtime cycle)
    from harness_runtime.types import HarnessContext, ServerName

__all__ = [
    "make_step_mcp_trust_tier_resolver",
    "resolve_step_mcp_trust_tier",
]


def resolve_step_mcp_trust_tier(step: WorkflowStep, ctx: HarnessContext) -> MCPTrustTier | None:
    """Resolve a TOOL_STEP's owning-MCP-host trust tier, or `None`.

    Returns the `MCPTrustTier` of the host whose `tool_registry` owns the step's
    `tool_id` (CP spec §19.1.2 Producer ¶). Returns `None` for a non-TOOL_STEP, a
    missing / non-str `tool_id`, or a `tool_id` registered in no configured host
    (fail-soft — see the module docstring). A `None` result feeds the L3 no-floor
    default at the composer (the MCP-trust axis contributes no gate floor).
    """
    if step.step_kind is not StepKind.TOOL_STEP:
        return None
    tool_id = step.step_payload.get("tool_id")
    if not isinstance(tool_id, str) or not tool_id:
        return None
    hosts: dict[ServerName, Any] = getattr(ctx, "mcp_client_hosts", None) or {}
    for host in hosts.values():
        registry = getattr(host, "tool_registry", None)
        if registry is None:
            continue
        try:
            contract = registry.get(tool_id)
        except KeyError:
            # Real ToolRegistry raises ToolNameNotRegisteredError (a KeyError) on
            # a miss — try the next host (collision guarantee ⇒ at most one match).
            continue
        if contract is not None:
            trust_tier = getattr(host, "trust_tier", None)
            return trust_tier if isinstance(trust_tier, MCPTrustTier) else None
    return None


def make_step_mcp_trust_tier_resolver(
    ctx: HarnessContext,
) -> Callable[[WorkflowStep], MCPTrustTier | None]:
    """Build a `(step) -> MCPTrustTier | None` resolver closure capturing `ctx`.

    Mirrors the `make_step_blast_radius_resolver(ctx)` stage-5 closure precedent
    (`step_blast_radius.py`): the tool-step HITL gate composer holds this closure
    as instance state (`mcp_trust_tier_resolver`; no `ctx` field on the composer)
    and invokes it per step at the §14.8.2 step-4c gate-input composition. Bound
    at bootstrap stage-5 LOOP_INIT, where `ctx.mcp_client_hosts` is available.
    """

    def _resolver(step: WorkflowStep) -> MCPTrustTier | None:
        return resolve_step_mcp_trust_tier(step, ctx)

    return _resolver
