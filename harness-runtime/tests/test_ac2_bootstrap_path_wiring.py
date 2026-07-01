"""AC#2-closing arc (spec v1.41 §14.9.8, Reading B) — bootstrap path wiring.

CI-green coverage for the gaps that the skipif-gated echo-MCP-via-`api.run` e2e
exercises but cannot prove in CI (it needs a live provider per Gap D):

  - Gap B: stage-3a calls `host.start()` when `mcp_clients` is non-empty
    (and does NOT for the empty-sentinel host).
  - Gap F: `shutdown()` drains a started host in `mcp_client_hosts` (and skips
    an unstarted / absent one).
  - MCPClientConfig per-server sandbox-policy fields (defaults + custom).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_core import ClientName
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.config.sandbox_defaults import resolve_effective_sandbox_defaults
from harness_runtime.types import (
    CollectorConfig,
    MCPClientConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


def _client(**overrides: Any) -> MCPClientConfig:
    base: dict[str, Any] = dict(
        client_name=ClientName("echo-server"),
        transport=MCPTransport.STDIO,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="stdio:///bin/echo",
    )
    base.update(overrides)
    return MCPClientConfig(**base)


def _config(mcp_clients: list[MCPClientConfig]) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=mcp_clients,
    )


# ---------------------------------------------------------------------------
# MCPClientConfig per-server sandbox-policy fields (Gap C config surface).
# ---------------------------------------------------------------------------


def test_sandbox_policy_field_defaults_are_none_and_resolve_surface_aware() -> None:
    """v1.43 §14.9.9 / fork §7.1 (Reading A+) — the per-server sandbox-default fields
    default to None and resolve through the deployment-surface-aware policy. A bare
    local-development config resolves to honest TIER_1_PROCESS with coherent
    minimum_tier + sandbox_tier (no spurious §14.9.4 floor violation); the in-process
    out-of-box default. (Supersedes the v1.41 static-TIER_2 default — operator-ratified
    2026-06-11 Reading A+.)"""
    c = _client()
    assert c.default_sandbox_tier is None
    assert c.default_minimum_tier is None
    assert c.default_sandbox_tech is None
    assert c.default_sandbox_provider is None
    assert c.sandbox_driver is None

    eff = resolve_effective_sandbox_defaults(c, DeploymentSurface.LOCAL_DEVELOPMENT)
    assert eff.sandbox_tier is SandboxTier.TIER_1_PROCESS
    assert eff.minimum_tier is SandboxTier.TIER_1_PROCESS
    assert eff.sandbox_tech == "host-process"
    assert eff.sandbox_provider == "host"


def test_sandbox_policy_fields_operator_overridable() -> None:
    c = _client(
        default_minimum_tier=SandboxTier.TIER_1_PROCESS,
        default_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        default_sandbox_tech="host-process",
        default_sandbox_provider="host",
    )
    assert c.default_sandbox_tier is SandboxTier.TIER_1_PROCESS
    assert c.default_sandbox_tech == "host-process"


# ---------------------------------------------------------------------------
# Gap B — stage-3a starts the host when a server is configured.
# ---------------------------------------------------------------------------


class _FakeHost:
    def __init__(self, *, server_name: str) -> None:
        self.server_name = server_name
        self.started = False
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.started = True


async def _run_stage_3a(
    monkeypatch: pytest.MonkeyPatch, cfg: RuntimeConfig, host: _FakeHost
) -> None:
    import harness_runtime.bootstrap.stage_3a_cp_clients as stage_3a

    class _StubStage:
        providers: dict[str, Any] = {}

    async def _stub_providers(*_a: Any, **_k: Any) -> _StubStage:
        return _StubStage()

    async def _stub_host(*_a: Any, **_k: Any) -> dict[str, _FakeHost]:
        # U-RT-126 reshape: the factory returns `dict[ServerName, MCPClientHost]`
        # keyed on server_name; stage_3a starts each host.
        return {host.server_name: host}

    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext

    builder = _MutableHarnessContext()
    builder.keyring_resolver = object()  # type: ignore[assignment]
    monkeypatch.setattr(stage_3a, "materialize_provider_clients_stage", _stub_providers)
    monkeypatch.setattr(stage_3a, "materialize_mcp_client_host_stage", _stub_host)
    await stage_3a.execute(builder, cfg, WorkloadClass.SOFTWARE_ENGINEERING)


@pytest.mark.asyncio
async def test_stage_3a_starts_host_when_server_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(server_name="echo-server")
    await _run_stage_3a(monkeypatch, _config([_client()]), host)
    assert host.start_calls == 1
    assert host.started is True


@pytest.mark.asyncio
async def test_stage_3a_does_not_start_empty_sentinel_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(server_name="<empty-sentinel>")
    await _run_stage_3a(monkeypatch, _config([]), host)
    assert host.start_calls == 0
    assert host.started is False


@pytest.mark.asyncio
async def test_stage_3a_drains_started_hosts_when_a_later_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-server (U-RT-126): if a LATER host's start() fails mid-loop, the
    hosts already started are drained — CP_CLIENTS never completes, so
    `_rollback_cp_clients` never fires for them and they would otherwise leak.
    Fail-closed teardown of the partial-start prefix."""
    import harness_runtime.bootstrap.stage_3a_cp_clients as stage_3a
    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext

    class _Host:
        def __init__(self, *, server_name: str, fail_start: bool = False) -> None:
            self.server_name = server_name
            self._fail_start = fail_start
            self.started = False
            self.shutdown_calls = 0

        async def start(self) -> None:
            if self._fail_start:
                raise RuntimeError(f"start boom: {self.server_name}")
            self.started = True

        async def shutdown(self) -> None:
            self.shutdown_calls += 1

    host_a = _Host(server_name="a")
    host_b = _Host(server_name="b", fail_start=True)

    class _StubStage:
        providers: dict[str, Any] = {}

    async def _stub_providers(*_a: Any, **_k: Any) -> _StubStage:
        return _StubStage()

    async def _stub_hosts(*_a: Any, **_k: Any) -> dict[str, _Host]:
        return {"a": host_a, "b": host_b}

    monkeypatch.setattr(stage_3a, "materialize_provider_clients_stage", _stub_providers)
    monkeypatch.setattr(stage_3a, "materialize_mcp_client_host_stage", _stub_hosts)
    builder = _MutableHarnessContext()
    builder.keyring_resolver = object()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="start boom"):
        await stage_3a.execute(
            builder, _config([_client(), _client()]), WorkloadClass.SOFTWARE_ENGINEERING
        )
    assert host_a.started is True
    assert host_a.shutdown_calls == 1  # already-started host drained on the abort
    assert host_b.shutdown_calls == 0  # never fully started → not drained


# ---------------------------------------------------------------------------
# Gap F — shutdown() drains a started host.
# ---------------------------------------------------------------------------


class _FakeShutdownHost:
    def __init__(self, *, started: bool) -> None:
        self.started = started
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _shutdown_ctx(tmp_path: Path, host: object | None) -> Any:
    from types import SimpleNamespace

    class _Ctx:
        pass

    path = tmp_path / "state.jsonl"
    path.write_text("")
    ctx = _Ctx()
    ctx.tracer_provider = None  # type: ignore[attr-defined]
    ctx.ledger_writer = SimpleNamespace(handle=SimpleNamespace(canonical_path=path))  # type: ignore[attr-defined]
    ctx.drained_flag = asyncio.Event()  # type: ignore[attr-defined]
    ctx.collector_daemon = None  # type: ignore[attr-defined]
    ctx.providers = {}  # type: ignore[attr-defined]
    ctx.audit_writer = SimpleNamespace(read_all=lambda: [])  # type: ignore[attr-defined]
    if host is not None:
        # U-RT-125 reshape: ctx carries `mcp_client_hosts` as a dict keyed on
        # server_name; shutdown drains each host's connection.
        ctx.mcp_client_hosts = {"mcp-stub-server": host}  # type: ignore[attr-defined]
    return ctx


@pytest.mark.asyncio
async def test_shutdown_drains_started_host(tmp_path: Path) -> None:
    from harness_runtime.shutdown import shutdown

    host = _FakeShutdownHost(started=True)
    ctx = _shutdown_ctx(tmp_path, host)
    report = await shutdown(ctx)
    assert host.shutdown_calls == 1
    assert "mcp_client_host" not in report.failures


@pytest.mark.asyncio
async def test_shutdown_skips_unstarted_host(tmp_path: Path) -> None:
    from harness_runtime.shutdown import shutdown

    host = _FakeShutdownHost(started=False)
    ctx = _shutdown_ctx(tmp_path, host)
    await shutdown(ctx)
    assert host.shutdown_calls == 0


@pytest.mark.asyncio
async def test_shutdown_tolerates_absent_host(tmp_path: Path) -> None:
    from harness_runtime.shutdown import shutdown

    ctx = _shutdown_ctx(tmp_path, host=None)
    report = await shutdown(ctx)
    assert "mcp_client_host" not in report.failures
