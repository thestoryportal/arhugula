"""U-RT-129 — D3 identity-by-ordinal `MCPServerTrustLevel → MCPTrustTier`
trust-telemetry projection (retire the constant-collapse stub).

Spec contract: CP spec **v1.34 §27.8** (the identity-by-ordinal trust
*telemetry* projection). Plan: runtime v2.47 §2.6 U-RT-129. Exercised
**by execution** through the real stage-3a factory `materialize_mcp_client_host_stage`
(not the private `_trust_tier_from_level` directly), so the test proves the
production path projects the declared trust, not a hardcoded value.

The prior MVP stub returned `LEVEL_0_REFUSE_REMOTE` for every server,
flattening all per-server trust telemetry to the most-restrictive tier;
U-RT-129 makes it a faithful `L_k → LEVEL_k` identity (the two enums are
the same closed 4-value set per C-AS-10 §10.3 / CP §27.8). The projection
is TELEMETRY-ONLY — `host.trust_tier` feeds the `mcp.server.trust_tier`
span attribute, NOT the dispatch trust gate (which keys on `server_name`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_core import ClientName
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.bootstrap.factories.mcp_client_host_factory import (
    materialize_mcp_client_host_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    MCPClientConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# The full identity-by-ordinal mapping the projection must realize (CP §27.8):
# the AS-side level L_k maps to the same-ordinal CP-side tier LEVEL_k.
_IDENTITY_PAIRS: list[tuple[MCPServerTrustLevel, MCPTrustTier]] = [
    (MCPServerTrustLevel.L0_REFUSE_REMOTE, MCPTrustTier.LEVEL_0_REFUSE_REMOTE),
    (MCPServerTrustLevel.L1_SIGNED_PINNED, MCPTrustTier.LEVEL_1_SIGNED_PINNED),
    (MCPServerTrustLevel.L2_SANDBOX_ALL, MCPTrustTier.LEVEL_2_SANDBOX_ALL),
    (MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT, MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT),
]


def _config_with_trust(level: MCPServerTrustLevel) -> RuntimeConfig:
    """A single-server config declaring the given per-server `trust_level`."""
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[
            MCPClientConfig(
                client_name=ClientName("trust-test"),
                transport=MCPTransport.STDIO,
                trust_level=level,
                blast_radius=BlastRadiusTier.READ_ONLY,
                connection_url="stdio:///bin/echo",
            )
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("level", "expected_tier"), _IDENTITY_PAIRS)
async def test_factory_projects_trust_level_identity_by_ordinal(
    level: MCPServerTrustLevel, expected_tier: MCPTrustTier
) -> None:
    """A host declaring level `L_k` reports `trust_tier == LEVEL_k` (identity).

    This is the U-RT-129 AC: each of the 4 tiers projects faithfully through
    the production factory — no longer collapsed to the constant `LEVEL_0`.
    """
    hosts = await materialize_mcp_client_host_stage(_config_with_trust(level))
    host = next(iter(hosts.values()))
    assert host.trust_tier is expected_tier


@pytest.mark.asyncio
async def test_l3_server_no_longer_reports_the_l0_constant() -> None:
    """Regression guard on the stub retirement — an `L3_ALLOW_WITH_AUDIT`
    server reports `LEVEL_3_ALLOW_WITH_AUDIT`, NOT the prior constant
    `LEVEL_0_REFUSE_REMOTE` the collapse-stub returned for every server.
    """
    hosts = await materialize_mcp_client_host_stage(
        _config_with_trust(MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT)
    )
    host = next(iter(hosts.values()))
    assert host.trust_tier is MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT
    assert host.trust_tier is not MCPTrustTier.LEVEL_0_REFUSE_REMOTE


@pytest.mark.asyncio
async def test_trust_tier_distinct_across_tiers_non_vacuous() -> None:
    """Non-vacuous tier-distinctness — an L0 server and an L3 server compose
    DIFFERENT `trust_tier` telemetry from the SAME factory path (proving the
    projection carries real per-server signal, not a uniform constant table).
    """
    l0_host = next(
        iter(
            (
                await materialize_mcp_client_host_stage(
                    _config_with_trust(MCPServerTrustLevel.L0_REFUSE_REMOTE)
                )
            ).values()
        )
    )
    l3_host = next(
        iter(
            (
                await materialize_mcp_client_host_stage(
                    _config_with_trust(MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT)
                )
            ).values()
        )
    )
    assert l0_host.trust_tier is not l3_host.trust_tier
    assert l0_host.trust_tier is MCPTrustTier.LEVEL_0_REFUSE_REMOTE
    assert l3_host.trust_tier is MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT
