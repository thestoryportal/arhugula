"""Sandbox tier→driver selection contract — runtime spec v1.43 §14.9.9.

Covers the R-CC-1 arc #1 fix (`.harness/class_1_fork_sandbox_tier_driver_selection_silent_in_process.md`,
Reading A+): the resolved sandbox tier now selects the matching execution driver
(FR-1), or fails loud (FR-2) — it is NEVER silently executed in-process under a
`> TIER_1_PROCESS` claim. Two layers tested here without a live substrate:

- the deployment-surface-aware default policy + three-field coherence
  (`resolve_effective_sandbox_defaults`);
- the tier→driver selection registry + the fail-loud branches
  (`_select_tool_execution_driver`) — the discriminating-power / contrasting-baseline
  test (same no-driver input; TIER_1 → in-process driver, TIER_2 → RAISE).
"""

from __future__ import annotations

import pytest
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_core import ClientName
from harness_core.deployment_surface import DeploymentSurface
from harness_runtime.bootstrap.factories.runtime_tool_dispatcher_factory import (
    _select_tool_execution_driver,
)
from harness_runtime.config.sandbox_defaults import (
    compose_transport_floor,
    resolve_effective_sandbox_defaults,
    resolve_per_tool_sandbox_defaults,
)
from harness_runtime.lifecycle.docker_tool_execution_driver import (
    DockerToolRunnerExecutionDriver,
    GVisorRunscToolRunnerExecutionDriver,
)
from harness_runtime.lifecycle.e2b_tool_execution_driver import (
    E2BManagedFullVMToolRunnerExecutionDriver,
)
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    MCPHostToolExecutionDriver,
    SandboxDriverUnavailableError,
)
from harness_runtime.types import MCPClientConfig, SandboxDriverConfig


def _client(**overrides: object) -> MCPClientConfig:
    base: dict[str, object] = dict(
        client_name=ClientName("echo-server"),
        transport=MCPTransport.STDIO,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="stdio:///bin/echo",
    )
    base.update(overrides)
    return MCPClientConfig(**base)  # type: ignore[arg-type]


_DRIVER_CFG = SandboxDriverConfig(command=("python", "runner.py"), image="echo:latest")


def _contract(**overrides: object) -> object:
    from harness_as.tool_contract import ToolContract

    base: dict[str, object] = dict(
        name="t",
        description="d",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
    )
    base.update(overrides)
    return ToolContract(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_per_tool_sandbox_defaults — B6 Slice 2 per-tool resolution (§14.9.11).
# ---------------------------------------------------------------------------


def test_per_tool_read_only_stdio_floors_to_tier3() -> None:
    """A READ_ONLY tool on a STDIO host resolves to TIER_3 (sandbox_tier_floor row 3),
    subsuming B6 Slice 1's transport floor per-tool; the reason names the per-tool cause."""
    client = _client()  # STDIO, L1, READ_ONLY
    surface = resolve_effective_sandbox_defaults(client, DeploymentSurface.LOCAL_DEVELOPMENT)
    eff = resolve_per_tool_sandbox_defaults(
        _contract(), client, DeploymentSurface.LOCAL_DEVELOPMENT, surface
    )
    assert eff.sandbox_tier is SandboxTier.TIER_3_MICROVM
    assert eff.assigned_tier_reason == "per-tool-sandbox-floor: t → tier-3-microvm (C-AS-02 §2.3)"


def test_per_tool_forcing_computer_use_resolves_tier4() -> None:
    """A `forces_computer_use` tool resolves to TIER_4 (sandbox_tier_floor row 1) regardless
    of its low blast radius — the per-tool forcing row now reachable (was unreachable pre-B6-S2)."""
    client = _client()
    surface = resolve_effective_sandbox_defaults(client, DeploymentSurface.LOCAL_DEVELOPMENT)
    eff = resolve_per_tool_sandbox_defaults(
        _contract(forces_computer_use=True),
        client,
        DeploymentSurface.LOCAL_DEVELOPMENT,
        surface,
    )
    assert eff.sandbox_tier is SandboxTier.TIER_4_FULL_VM


def test_per_tool_surface_default_floors_low_per_tool_tier_in_production() -> None:
    """The §14.9.8 deployment-surface default is preserved as a floor: a READ_ONLY tool on a
    REMOTE L1 host (per-tool floor TIER_1, no STDIO row) in managed-cloud is held at the
    surface default TIER_2 — production never drops below its fail-safe-high default."""
    client = _client(
        transport=MCPTransport.STREAMABLE_HTTP_L1_PINNED,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
    )
    surface = resolve_effective_sandbox_defaults(client, DeploymentSurface.MANAGED_CLOUD)
    assert surface.sandbox_tier is SandboxTier.TIER_2_CONTAINER
    eff = resolve_per_tool_sandbox_defaults(
        _contract(), client, DeploymentSurface.MANAGED_CLOUD, surface
    )
    assert eff.sandbox_tier is SandboxTier.TIER_2_CONTAINER


# ---------------------------------------------------------------------------
# resolve_effective_sandbox_defaults — surface-aware default policy (Reading A+).
# ---------------------------------------------------------------------------


def test_bare_local_development_resolves_honest_tier_1() -> None:
    """A bare local-development config → honest TIER_1_PROCESS, coherent
    minimum_tier + sandbox_tier (no spurious floor violation), in-process out-of-box."""
    eff = resolve_effective_sandbox_defaults(_client(), DeploymentSurface.LOCAL_DEVELOPMENT)
    assert eff.sandbox_tier is SandboxTier.TIER_1_PROCESS
    assert eff.minimum_tier is SandboxTier.TIER_1_PROCESS
    assert eff.sandbox_tech == "host-process"
    assert eff.sandbox_provider == "host"


@pytest.mark.parametrize(
    "surface",
    [DeploymentSurface.SELF_HOSTED_SERVER, DeploymentSurface.MANAGED_CLOUD],
)
def test_bare_production_surfaces_resolve_fail_safe_high(surface: DeploymentSurface) -> None:
    """Bare self-hosted / managed-cloud configs → fail-safe-high TIER_2_CONTAINER
    (which FR-2(i) refuses to run unless a driver is configured)."""
    eff = resolve_effective_sandbox_defaults(_client(), surface)
    assert eff.sandbox_tier is SandboxTier.TIER_2_CONTAINER
    assert eff.minimum_tier is SandboxTier.TIER_2_CONTAINER
    assert eff.sandbox_tech == "container"


def test_explicit_operator_values_override_surface_policy() -> None:
    """An explicit per-server tier/tech/provider overrides the surface-aware default."""
    eff = resolve_effective_sandbox_defaults(
        _client(
            default_sandbox_tier=SandboxTier.TIER_3_MICROVM,
            default_minimum_tier=SandboxTier.TIER_3_MICROVM,
            default_sandbox_tech="gvisor",
            default_sandbox_provider="runsc",
        ),
        DeploymentSurface.LOCAL_DEVELOPMENT,  # would default TIER_1 — overridden
    )
    assert eff.sandbox_tier is SandboxTier.TIER_3_MICROVM
    assert eff.minimum_tier is SandboxTier.TIER_3_MICROVM
    assert eff.sandbox_tech == "gvisor"


# ---------------------------------------------------------------------------
# _select_tool_execution_driver — tier→driver registry (FR-1) + fail-loud (FR-2).
# ---------------------------------------------------------------------------


def test_tier_1_selects_in_process_host_driver_without_config() -> None:
    """TIER_1_PROCESS needs no driver config — the in-process host driver."""
    driver = _select_tool_execution_driver(tier=SandboxTier.TIER_1_PROCESS, driver_config=None)
    assert isinstance(driver, MCPHostToolExecutionDriver)


def test_tier_2_selects_docker_driver_when_configured() -> None:
    driver = _select_tool_execution_driver(
        tier=SandboxTier.TIER_2_CONTAINER, driver_config=_DRIVER_CFG
    )
    assert isinstance(driver, DockerToolRunnerExecutionDriver)
    # Delivered-tier == resolved-tier invariant (the driver self-guards on it).
    assert driver.required_tier is SandboxTier.TIER_2_CONTAINER


def test_tier_3_selects_gvisor_driver_when_configured() -> None:
    driver = _select_tool_execution_driver(
        tier=SandboxTier.TIER_3_MICROVM, driver_config=_DRIVER_CFG
    )
    assert isinstance(driver, GVisorRunscToolRunnerExecutionDriver)
    assert driver.required_tier is SandboxTier.TIER_3_MICROVM
    assert driver.runtime == "runsc"


def test_tier_4_selects_e2b_driver_when_configured() -> None:
    driver = _select_tool_execution_driver(
        tier=SandboxTier.TIER_4_FULL_VM,
        driver_config=SandboxDriverConfig(command=("python", "runner.py")),
    )
    assert isinstance(driver, E2BManagedFullVMToolRunnerExecutionDriver)
    assert driver.required_tier is SandboxTier.TIER_4_FULL_VM


# --- FR-2(i) fail-loud — the contrasting-baseline / discriminating-power test ---


def test_tier_2_without_any_driver_config_fails_loud_not_in_process() -> None:
    """THE security fix: a TIER_2_CONTAINER claim with no driver configured RAISES
    `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE` — it does NOT silently fall through to the
    in-process driver (the pre-v1.43 defect). Discriminating power: same no-driver
    input as the TIER_1 case above, opposite outcome."""
    with pytest.raises(SandboxDriverUnavailableError):
        _select_tool_execution_driver(tier=SandboxTier.TIER_2_CONTAINER, driver_config=None)


@pytest.mark.parametrize(
    "tier",
    [SandboxTier.TIER_2_CONTAINER, SandboxTier.TIER_3_MICROVM],
)
def test_container_tiers_without_image_fail_loud(tier: SandboxTier) -> None:
    """A container/microVM tier with a driver config missing `image` fails loud —
    a partially-configured driver is non-delivery, not a silent downgrade."""
    cfg = SandboxDriverConfig(command=("python", "runner.py"))  # no image
    with pytest.raises(SandboxDriverUnavailableError):
        _select_tool_execution_driver(tier=tier, driver_config=cfg)


# ---------------------------------------------------------------------------
# compose_transport_floor — R-FS-1 B6 Slice 1 (runtime spec v1.54 §14.9.8):
# the per-MCP-transport floor (ADR-D2 §1.3) composed into the resolved tier,
# still per-server-uniform. STDIO → ≥ TIER_3; remote L2 → TIER_4; remote L1/L3
# + non-floor cases → unchanged; minimum_tier never raised; REFUSE (L0) → raise.
# ---------------------------------------------------------------------------


def _composed(client: MCPClientConfig, surface: DeploymentSurface) -> object:
    return compose_transport_floor(resolve_effective_sandbox_defaults(client, surface), client)


def test_stdio_floors_local_dev_tier1_to_tier3() -> None:
    """The B6 behavioral change: a STDIO server at local-development (surface
    default TIER_1) floors to TIER_3 (ADR-D2 §1.3), with tech/provider re-derived."""
    eff = _composed(_client(transport=MCPTransport.STDIO), DeploymentSurface.LOCAL_DEVELOPMENT)
    assert eff.sandbox_tier is SandboxTier.TIER_3_MICROVM  # type: ignore[attr-defined]
    assert eff.sandbox_tech == "gvisor"  # type: ignore[attr-defined]
    assert eff.sandbox_provider == "runsc"  # type: ignore[attr-defined]
    # Security telemetry honesty: the reason records the transport floor, not the default.
    assert "per-mcp-transport-floor" in eff.assigned_tier_reason  # type: ignore[attr-defined]
    assert "ADR-D2 §1.3" in eff.assigned_tier_reason  # type: ignore[attr-defined]


def test_stdio_floor_leaves_minimum_tier_unchanged() -> None:
    """The transport floor raises the DELIVERED tier (`sandbox_tier`), NOT the
    tool's REQUIRED floor (`minimum_tier`) — `resolved.tier >= minimum_tier` stays
    satisfied by the raised tier; the converter's minimum_tier is untouched."""
    base = resolve_effective_sandbox_defaults(
        _client(transport=MCPTransport.STDIO), DeploymentSurface.LOCAL_DEVELOPMENT
    )
    composed = compose_transport_floor(base, _client(transport=MCPTransport.STDIO))
    assert base.minimum_tier is SandboxTier.TIER_1_PROCESS
    assert composed.minimum_tier is SandboxTier.TIER_1_PROCESS  # UNCHANGED
    assert composed.sandbox_tier is SandboxTier.TIER_3_MICROVM  # delivered raised


def test_stdio_floors_self_hosted_tier2_to_tier3() -> None:
    """STDIO floor (TIER_3) composes by max() with the self-hosted surface default
    (TIER_2) → TIER_3."""
    eff = _composed(_client(transport=MCPTransport.STDIO), DeploymentSurface.SELF_HOSTED_SERVER)
    assert eff.sandbox_tier is SandboxTier.TIER_3_MICROVM  # type: ignore[attr-defined]


def test_remote_l2_floors_to_tier4() -> None:
    """A remote L2 (sandbox-all) server floors to TIER_4 (ADR-D2 §1.3 / C-AS-10
    §10.1 — `max(TIER_4, blast_radius_floor)`)."""
    eff = _composed(
        _client(
            transport=MCPTransport.STREAMABLE_HTTP_L2_SANDBOX,
            trust_level=MCPServerTrustLevel.L2_SANDBOX_ALL,
        ),
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert eff.sandbox_tier is SandboxTier.TIER_4_FULL_VM  # type: ignore[attr-defined]
    assert eff.sandbox_tech == "firecracker"  # type: ignore[attr-defined]


def test_remote_l1_readonly_floor_does_not_raise_tier() -> None:
    """A remote L1 READ_ONLY server floors to `blast_radius_floor(READ_ONLY)` =
    TIER_1, which does NOT exceed the local-dev surface default (TIER_1) — the
    composed tier is unchanged (no spurious raise for non-STDIO low-blast servers)."""
    eff = _composed(
        _client(
            transport=MCPTransport.STREAMABLE_HTTP_L1_PINNED,
            trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        ),
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert eff.sandbox_tier is SandboxTier.TIER_1_PROCESS  # type: ignore[attr-defined]
    assert eff.sandbox_tech == "host-process"  # type: ignore[attr-defined]
    # No floor raise → the per-server-default reason is retained (not floor-attributed).
    assert eff.assigned_tier_reason == "per-server-default-sandbox-policy"  # type: ignore[attr-defined]


def test_operator_tech_override_survives_floor_raise() -> None:
    """When the floor raises the tier, an explicit operator-supplied tech/provider
    still wins over the auto-derived per-tier label (the operator's label is theirs)."""
    eff = _composed(
        _client(
            transport=MCPTransport.STDIO,
            default_sandbox_tech="custom-tech",
            default_sandbox_provider="custom-provider",
        ),
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert eff.sandbox_tier is SandboxTier.TIER_3_MICROVM  # type: ignore[attr-defined]  # floor still raises
    assert eff.sandbox_tech == "custom-tech"  # type: ignore[attr-defined]  # operator override survives
    assert eff.sandbox_provider == "custom-provider"  # type: ignore[attr-defined]


def test_remote_l0_floor_refuse_raises_defensively() -> None:
    """A remote L0 (refuse) server's floor is REFUSE (not a tier). It is unreachable
    in production (L0 is refused at registration), so `compose_transport_floor` raises
    a defensive fail-closed error rather than returning a tier-less decision."""
    client = _client(
        transport=MCPTransport.STREAMABLE_HTTP_L0_REFUSE,
        trust_level=MCPServerTrustLevel.L0_REFUSE_REMOTE,
    )
    base = resolve_effective_sandbox_defaults(client, DeploymentSurface.LOCAL_DEVELOPMENT)
    with pytest.raises(RuntimeError, match="TRANSPORT-FLOOR-REFUSE"):
        compose_transport_floor(base, client)
