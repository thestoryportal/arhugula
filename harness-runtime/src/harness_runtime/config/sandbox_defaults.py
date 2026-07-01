"""Deployment-surface-aware per-server sandbox default policy (Reading A+).

Per runtime spec v1.43 §14.9.9 + fork
`.harness/class_1_fork_sandbox_tier_driver_selection_silent_in_process.md` §7.1.
When a per-server `MCPClientConfig` leaves a sandbox default field unset
(`None`), the effective default is keyed on `RuntimeConfig.deployment_surface`:

- `local-development` → honest `TIER_1_PROCESS` in-process (no lie, runs out-of-box;
  the in-process host driver needs no substrate).
- `self-hosted-server` / `managed-cloud` → fail-safe-high `TIER_2_CONTAINER` (which
  FR-2(i) fail-loud at the stage-5 factory refuses to run unless a `sandbox_driver`
  is configured).

This is a *floor-safe* default per the floor-verification at fork §7: the ADR-D2
§1.1 / C-AS-09 §9.1 cells are raise-only floors, so a surface-keyed default only
ever sits at or above the matrix floor, and the §14.9.4 tier-floor check is the
safety net against silent under-sandboxing.

The single source of truth for BOTH the stage-3a converter (`minimum_tier`) and the
stage-5 resolver/driver-selection (`sandbox_tier`/`tech`/`provider`) — so the three
`default_*` fields stay coherent and a bare config never spuriously trips the
floor check (the three-field-incoherence catch recorded at fork §7.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harness_as.mcp_transport_floor import mcp_transport_floor
from harness_as.sandbox_tier import SandboxTier, is_tier_at_or_above
from harness_as.sandbox_tier_floor import (
    MCPServer,
    SandboxTierFloorOutcome,
    ToolMetadata,
    sandbox_tier_floor,
)
from harness_as.tool_contract import ToolContract
from harness_core.deployment_surface import DeploymentSurface

if TYPE_CHECKING:
    from harness_runtime.types import MCPClientConfig

__all__ = [
    "EffectiveSandboxDefaults",
    "compose_transport_floor",
    "resolve_effective_sandbox_defaults",
    "resolve_per_tool_sandbox_defaults",
    "surface_default_sandbox_tier",
]


# Per-tier (tech, provider) labels emitted on the `sandbox.enter` span when the
# operator leaves them unset. Impl-discretion (§14.9.9 Deferred — "the registry
# names families, not classes"). NOTE (adversarial-review F1-02): these are
# placeholder labels that do NOT yet match the ADR-D2 §1.7 canonical `sandbox.*`
# namespace (where `sandbox.tech` = the tier-mechanism class e.g. `microvm`, and
# `sandbox.provider` = the provider+tech tuple e.g. `e2b_firecracker`). Aligning
# them is deferred to the future observability-hygiene arc alongside the §1.7
# provider-class reconciliation; the operator may override per-server today.
_TIER_TECH_PROVIDER: dict[SandboxTier, tuple[str, str]] = {
    SandboxTier.TIER_1_PROCESS: ("host-process", "host"),
    SandboxTier.TIER_2_CONTAINER: ("container", "docker"),
    SandboxTier.TIER_3_MICROVM: ("gvisor", "runsc"),
    SandboxTier.TIER_4_FULL_VM: ("firecracker", "e2b"),
}


#: Default `assigned_tier_reason` when the surface-aware per-server policy (not a
#: transport floor) determines the tier.
_PER_SERVER_DEFAULT_REASON = "per-server-default-sandbox-policy"


@dataclass(frozen=True)
class EffectiveSandboxDefaults:
    """The reconciled per-server sandbox defaults after applying the
    deployment-surface-aware policy to any `None` `MCPClientConfig` fields."""

    minimum_tier: SandboxTier
    sandbox_tier: SandboxTier
    sandbox_tech: str
    sandbox_provider: str
    assigned_tier_reason: str = _PER_SERVER_DEFAULT_REASON
    """Human-readable cause of the resolved `sandbox_tier` — emitted on the
    `sandbox.enter` span (C-AS-15 §15). The surface-aware default policy sets the
    per-server-default reason; `compose_transport_floor` overrides it with a
    floor-aware reason when the per-MCP-transport floor raises the tier (B6 Slice 1
    — security telemetry honesty: the span says WHY the isolation was applied)."""


def surface_default_sandbox_tier(deployment_surface: DeploymentSurface) -> SandboxTier:
    """The Reading-A+ default sandbox tier for a deployment surface (used when the
    operator leaves the per-server tier unset)."""
    if deployment_surface is DeploymentSurface.LOCAL_DEVELOPMENT:
        # Honest in-process default — the in-process host driver needs no substrate,
        # so a bare local-dev config runs out-of-box, truthfully labeled TIER_1.
        return SandboxTier.TIER_1_PROCESS
    # self-hosted-server / managed-cloud: fail-safe-high. FR-2(i) refuses to run
    # unless a sandbox_driver is configured — production never runs silently
    # unsandboxed.
    return SandboxTier.TIER_2_CONTAINER


def resolve_effective_sandbox_defaults(
    entry: MCPClientConfig, deployment_surface: DeploymentSurface
) -> EffectiveSandboxDefaults:
    """Resolve the per-server sandbox defaults, applying the surface-aware policy to
    any unset (`None`) field and keeping the three tier-bearing fields coherent.

    Explicit operator values always override the policy. When `default_minimum_tier`
    / `default_sandbox_tier` are both `None`, they resolve to the SAME surface-derived
    tier — so the §14.9.4 tier-floor check (`resolved.tier >= contract.minimum_tier`)
    never spuriously violates on a bare config.
    """
    surface_tier = surface_default_sandbox_tier(deployment_surface)

    sandbox_tier = (
        entry.default_sandbox_tier if entry.default_sandbox_tier is not None else surface_tier
    )
    minimum_tier = (
        entry.default_minimum_tier if entry.default_minimum_tier is not None else surface_tier
    )

    default_tech, default_provider = _TIER_TECH_PROVIDER[sandbox_tier]
    sandbox_tech = (
        entry.default_sandbox_tech if entry.default_sandbox_tech is not None else default_tech
    )
    sandbox_provider = (
        entry.default_sandbox_provider
        if entry.default_sandbox_provider is not None
        else default_provider
    )

    return EffectiveSandboxDefaults(
        minimum_tier=minimum_tier,
        sandbox_tier=sandbox_tier,
        sandbox_tech=sandbox_tech,
        sandbox_provider=sandbox_provider,
    )


def compose_transport_floor(
    effective: EffectiveSandboxDefaults, entry: MCPClientConfig
) -> EffectiveSandboxDefaults:
    """Compose the ADR-D2 §1.3 per-MCP-transport sandbox-tier floor into the resolved
    `sandbox_tier` — R-FS-1 B6 Slice 1, runtime spec v1.54 §14.9.8 "Per-MCP-transport
    floor composition".

    Raises the resolved `sandbox_tier` to `max(effective.sandbox_tier,
    mcp_transport_floor(transport, trust_level, blast_radius).tier)` (U-AS-13 / C-AS-10
    §10.1): STDIO → `max(TIER_3_MICROVM, blast_radius_floor)`; remote L2 →
    `max(TIER_4_FULL_VM, blast_radius_floor)`; remote L1/L3 + non-remote →
    `blast_radius_floor`. When the floor raises the tier, `sandbox_tech` / `sandbox_provider`
    are re-derived for the raised tier (operator-supplied overrides still win); they are
    the `sandbox.enter` span labels, kept coherent with the delivered tier.

    **Per-server-uniform PRESERVED** — `mcp_transport_floor` is fed the per-host
    `MCPClientConfig.blast_radius` (one blast input per host ⇒ one tier per host). The
    per-TOOL `sandbox_tier_floor` per-cell composition (rows 1-2 forcing + rows 7-10
    per-tool blast) is the distinct future arc B6 Slice 2 (`B-PER-TOOL-SANDBOX-TIER`).

    `minimum_tier` is UNCHANGED — the transport floor raises what the deployment
    *delivers* (`sandbox_tier`), not what the tool *requires* (`minimum_tier`); the
    §14.9.4 tier-floor check (`resolved.tier >= contract.minimum_tier`) stays satisfied
    by the raised tier.

    A floor `REFUSE` (remote L0) is **unreachable** here — L0-remote is refused at
    registration (`materialize_mcp_stage`, §14.9.10 D1) before the stage-5 resolver loop
    that is this function's only caller — so it is a defensive fail-closed raise, never a
    silent tier substitution (`SandboxDispatchDecision.tier` is non-optional).
    """
    floor = mcp_transport_floor(entry.transport, entry.trust_level, entry.blast_radius)
    if floor.outcome is SandboxTierFloorOutcome.REFUSE:
        raise RuntimeError(
            f"RT-FAIL-SANDBOX-TRANSPORT-FLOOR-REFUSE: MCP server "
            f"{entry.client_name!r} (transport={entry.transport.value}, "
            f"trust_level={entry.trust_level.value}) resolves to a transport-floor "
            f"REFUSE at the stage-5 sandbox resolver — an L0-remote server must be "
            f"refused at registration (materialize_mcp_stage); reaching this site is a "
            f"bootstrap-ordering invariant violation"
        )
    assert floor.tier is not None, "RESOLVED SandboxTierFloorResult always carries a tier"

    # Only a STRICT raise is floor-attributed. When the floor equals (or sits below) the
    # surface-aware default, the default decision stands UNCHANGED — including its
    # `assigned_tier_reason` (no raise happened, so the floor is not the cause). Using a
    # reflexive `>=` here would mis-attribute an equal-tier default to the floor.
    raised = (
        is_tier_at_or_above(floor.tier, effective.sandbox_tier)
        and floor.tier is not effective.sandbox_tier
    )
    if not raised:
        return effective

    default_tech, default_provider = _TIER_TECH_PROVIDER[floor.tier]
    return EffectiveSandboxDefaults(
        minimum_tier=effective.minimum_tier,
        sandbox_tier=floor.tier,
        sandbox_tech=(
            entry.default_sandbox_tech if entry.default_sandbox_tech is not None else default_tech
        ),
        sandbox_provider=(
            entry.default_sandbox_provider
            if entry.default_sandbox_provider is not None
            else default_provider
        ),
        # Security telemetry honesty: the `sandbox.enter` span records that the
        # per-MCP-transport floor (ADR-D2 §1.3) — not the per-server default — drove
        # the (raised) tier.
        assigned_tier_reason=(
            f"per-mcp-transport-floor: {entry.transport.value} → {floor.tier.value} (ADR-D2 §1.3)"
        ),
    )


def resolve_per_tool_sandbox_defaults(
    contract: ToolContract,
    entry: MCPClientConfig,
    deployment_surface: DeploymentSurface,
    surface_default: EffectiveSandboxDefaults,
) -> EffectiveSandboxDefaults:
    """Resolve the PER-TOOL sandbox decision — R-FS-1 B6 Slice 2, runtime spec v1.56
    §14.9.11 (the per-server-uniform→per-tool lift; `B-PER-TOOL-SANDBOX-TIER`).

    Computes `max(surface_default.sandbox_tier, sandbox_tier_floor(...).tier)` per
    `(contract, host)`: the §14.9.8 deployment-surface default (`surface_default`) is the
    floor; the full 10-row `sandbox_tier_floor` (C-AS-02 §2.3) supplies the per-tool
    forcing rows (1-2 — `forces_computer_use`/`forces_code_execution` → TIER_4), the
    transport/trust rows (3-6 — **subsuming** B6 Slice 1's per-host `compose_transport_floor`
    per-tool, STDIO→TIER_3), and the per-tool blast rows (7-10 — keyed on the tool's own
    `blast_radius_tier`, not the host's). This is why the per-tool path consumes
    `resolve_effective_sandbox_defaults` (the surface default) and NOT `compose_transport_floor`
    (whose host-blast transport floor `sandbox_tier_floor` rows 3-6 already subsume per-tool).

    `minimum_tier` is UNCHANGED — the per-tool floor raises what the dispatch *delivers*
    (`sandbox_tier`), not what the tool *requires* (`minimum_tier`); the §14.9.4 tier-floor
    check (`resolved.tier >= contract.minimum_tier`) reads `minimum_tier` separately.

    A floor `REFUSE` (remote L0) is **unreachable** — L0-remote is refused at registration
    (`materialize_mcp_stage`, §14.9.10 D1) before the stage-5 resolver loop — so it is a
    defensive fail-closed raise, never a silent tier substitution.
    """
    floor = sandbox_tier_floor(
        ToolMetadata(
            is_deterministic_inhouse=contract.is_deterministic_inhouse,
            forces_computer_use=contract.forces_computer_use,
            forces_code_execution=contract.forces_code_execution,
        ),
        deployment_surface,
        contract.blast_radius_tier,
        entry.transport,
        MCPServer(server_id=entry.client_name, trust_level=entry.trust_level),
    )
    if floor.outcome is SandboxTierFloorOutcome.REFUSE:
        raise RuntimeError(
            f"RT-FAIL-SANDBOX-PER-TOOL-FLOOR-REFUSE: tool {contract.name!r} on MCP server "
            f"{entry.client_name!r} resolves to a per-tool sandbox_tier_floor REFUSE — an "
            f"L0-remote server must be refused at registration before the stage-5 resolver; "
            f"reaching this site is a bootstrap-ordering invariant violation (runtime §14.9.11)"
        )
    assert floor.tier is not None, "RESOLVED SandboxTierFloorResult always carries a tier"

    # max(surface default, per-tool floor). When the surface default is STRICTLY higher it
    # stands UNCHANGED (incl. its tech/provider/reason); otherwise the per-tool floor drives
    # the tier and the span labels/reason reflect the per-tool cause (security telemetry honesty).
    if (
        is_tier_at_or_above(surface_default.sandbox_tier, floor.tier)
        and surface_default.sandbox_tier is not floor.tier
    ):
        return surface_default

    default_tech, default_provider = _TIER_TECH_PROVIDER[floor.tier]
    return EffectiveSandboxDefaults(
        minimum_tier=surface_default.minimum_tier,
        sandbox_tier=floor.tier,
        sandbox_tech=(
            entry.default_sandbox_tech if entry.default_sandbox_tech is not None else default_tech
        ),
        sandbox_provider=(
            entry.default_sandbox_provider
            if entry.default_sandbox_provider is not None
            else default_provider
        ),
        assigned_tier_reason=(
            f"per-tool-sandbox-floor: {contract.name} → {floor.tier.value} (C-AS-02 §2.3)"
        ),
    )
