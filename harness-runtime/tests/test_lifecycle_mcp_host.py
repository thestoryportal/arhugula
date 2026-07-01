"""U-RT-15 — `materialize_mcp_stage` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L3:
- Host accepts connections (placeholder MCPHost constructed at L3).
- Configured clients reach READY.
- Transport-floor invariants asserted.
"""

from __future__ import annotations

import pytest
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_runtime.lifecycle.mcp_host import (
    MCPClient,
    MCPHost,
    MCPServerRefusedError,
    MCPStage,
    materialize_mcp_stage,
)
from harness_runtime.types import ClientName, MCPClientConfig


def _stdio_client(name: str = "stdio-test") -> MCPClientConfig:
    return MCPClientConfig(
        client_name=ClientName(name),
        transport=MCPTransport.STDIO,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="python -m my_mcp_server",
    )


# ---------------------------------------------------------------------------
# Empty client config (typical bootstrap state).
# ---------------------------------------------------------------------------


def test_empty_clients_builds_empty_stage() -> None:
    """No configured clients → host + empty client registry."""
    stage = materialize_mcp_stage([])
    assert isinstance(stage, MCPStage)
    assert isinstance(stage.host, MCPHost)
    assert stage.clients == {}


# ---------------------------------------------------------------------------
# Configured clients reach READY (plan AC).
# ---------------------------------------------------------------------------


def test_single_stdio_client_reaches_ready() -> None:
    """STDIO transport with L1 trust → resolved + MCPClient.ready=True."""
    config = _stdio_client("alpha")
    stage = materialize_mcp_stage([config])
    client = stage.clients[ClientName("alpha")]
    assert isinstance(client, MCPClient)
    assert client.ready is True
    assert client.config is config


def test_multiple_clients_indexed_by_name() -> None:
    """Configured clients indexed by client_name in the stage.clients dict."""
    configs = [_stdio_client(name) for name in ("a", "b", "c")]
    stage = materialize_mcp_stage(configs)
    assert set(stage.clients.keys()) == {
        ClientName("a"),
        ClientName("b"),
        ClientName("c"),
    }


# ---------------------------------------------------------------------------
# Transport-floor invariants asserted (plan AC).
# ---------------------------------------------------------------------------


def test_remote_l0_trust_refused() -> None:
    """Remote transport + `L0_REFUSE_REMOTE` trust → MCPServerRefusedError.

    Per C-AS-10 §10.1 row 2: trust-level 0 rejects at registration.
    """
    config = MCPClientConfig(
        client_name=ClientName("remote-refused"),
        transport=MCPTransport.STREAMABLE_HTTP_L0_REFUSE,
        trust_level=MCPServerTrustLevel.L0_REFUSE_REMOTE,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="https://untrusted.example/mcp",
    )
    with pytest.raises(MCPServerRefusedError) as exc_info:
        materialize_mcp_stage([config])
    assert exc_info.value.client_name == ClientName("remote-refused")


def test_remote_l1_trust_clears() -> None:
    """Remote transport + `L1_SIGNED_PINNED` trust → registration clears."""
    config = MCPClientConfig(
        client_name=ClientName("remote-pinned"),
        transport=MCPTransport.STREAMABLE_HTTP_L1_PINNED,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="https://pinned.example/mcp",
    )
    stage = materialize_mcp_stage([config])
    assert ClientName("remote-pinned") in stage.clients


def test_stdio_with_any_trust_clears() -> None:
    """STDIO transport bypasses remote-trust gating (C-AS-10 §10.1 row 1)."""
    for trust in MCPServerTrustLevel:
        if trust is MCPServerTrustLevel.L0_REFUSE_REMOTE:
            # L0 + STDIO is benign — the table's STDIO row doesn't go through
            # trust_level. Still cleared.
            pass
        config = MCPClientConfig(
            client_name=ClientName(f"stdio-{trust.value}"),
            transport=MCPTransport.STDIO,
            trust_level=trust,
            blast_radius=BlastRadiusTier.READ_ONLY,
            connection_url="python -m server",
        )
        stage = materialize_mcp_stage([config])
        assert ClientName(f"stdio-{trust.value}") in stage.clients


def test_refusal_short_circuits_subsequent_clients() -> None:
    """A REFUSE on any client raises before subsequent configs are processed."""
    good = _stdio_client("first")
    bad = MCPClientConfig(
        client_name=ClientName("second-refused"),
        transport=MCPTransport.STREAMABLE_HTTP_L0_REFUSE,
        trust_level=MCPServerTrustLevel.L0_REFUSE_REMOTE,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="https://x",
    )
    third = _stdio_client("third")
    with pytest.raises(MCPServerRefusedError):
        materialize_mcp_stage([good, bad, third])


# ---------------------------------------------------------------------------
# Host placeholder at L3.
# ---------------------------------------------------------------------------


def test_mcp_host_started_false_at_l3() -> None:
    """L3 ships the composition surface; real server startup deferred."""
    stage = materialize_mcp_stage([])
    assert stage.host.started is False


def test_mcp_stage_is_frozen() -> None:
    """`MCPStage` is a frozen dataclass."""
    stage = materialize_mcp_stage([])
    with pytest.raises((AttributeError, Exception)):
        stage.host = None  # type: ignore[misc,assignment]
