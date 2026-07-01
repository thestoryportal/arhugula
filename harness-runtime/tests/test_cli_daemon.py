"""Tests for U-RT-107 — daemon entrypoint (``harness daemon``).

Maps to acceptance criteria 1–8 at runtime plan v2.31 §1.7. Mechanism α
(uvicorn + uds + ``streamable_http_app``) per the post-ratification apply
arc 2026-05-28 of the U-RT-107 Class 1 fork at
``.harness/class_1_fork_u_rt_107_daemon_run_workflow_signature_underspec.md``
(Reading A + Q2=i + Q3=a + Q4=a).

Strategy:
- structural unit tests for helpers (``_looks_like_manifest_path`` +
  ``_default_daemon_socket_path``)
- mocked bootstrap + uvicorn tests for drain / startup-failure paths
- 1 subprocess smoke test verifying real daemon binding + SIGTERM shutdown

AC #5 (concurrent multi-client) + AC #6 (sequential single-client repeat)
+ AC #8 (PID file end-to-end) are deferred to U-RT-109 e2e per
``[[verification-shape-sharpened-grep-vs-e2e]]`` + L9-undecies precedent
(structural binding verified here; full e2e against real MCP client over
Unix-socket lands at cluster terminus).
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


from typing import Any

import harness_runtime.cli.app as _ensure_import
import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from typer.testing import CliRunner

_cli_app_mod = sys.modules["harness_runtime.cli.app"]
assert _ensure_import is not None

from harness_runtime.cli.app import (
    EXIT_CONFIG_ERROR,
    DaemonStartupError,
    _default_daemon_socket_path,  # pyright: ignore[reportPrivateUsage]
    app,
)
from harness_runtime.config_source import RuntimeConfigLoadError
from harness_runtime.lifecycle.mcp_server import (
    _looks_like_manifest_path,  # pyright: ignore[reportPrivateUsage]
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

runner = CliRunner()


def _daemon_subprocess_smoke_socket_path() -> Path:
    return Path("/tmp") / f"h-daemon-{os.getpid()}-{time.monotonic_ns()}.sock"


def _runtime_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


# ---------------------------------------------------------------------------
# AC #3 + discriminator — run_workflow handler workflow_id-as-path widening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("workflow_id", "expected"),
    [
        ("workflow-key", False),
        ("wf-min", False),
        ("wf.yaml", True),
        ("wf.yml", True),
        ("wf.toml", True),
        ("/abs/path/wf.yaml", True),
        ("rel/path/wf", True),  # slash makes it a path
        ("wf.YAML", True),  # case-insensitive
        ("wf.unknown", False),
        ("", False),
    ],
)
def test_looks_like_manifest_path_discriminator(workflow_id: str, expected: bool) -> None:
    """U-RT-62 handler workflow_id discriminator per fork doc Q2=(i)."""
    assert _looks_like_manifest_path(workflow_id) is expected


# ---------------------------------------------------------------------------
# AC #1/#2 — socket path default + override
# ---------------------------------------------------------------------------


def test_ac1_default_socket_path_is_pid_independent_well_known() -> None:
    """Per Class 1 fork resolution Reading A at
    ``.harness/class_1_fork_daemon_default_socket_path_pid_mismatch.md``
    (operator-ratified 2026-05-29 probe-v4 finding): the default socket path
    is a single well-known location so that ``harness daemon`` and
    ``harness run --daemon`` resolve to the same socket without
    operator-supplied ``--socket-path`` on either side. Previously the path
    embedded ``os.getpid()``, which structurally cannot coordinate daemon
    and client because they are different processes.
    """
    path = _default_daemon_socket_path()
    assert path.name == "harness-daemon.sock"
    # PID-independence: invocations from any process resolve identically.
    assert str(os.getpid()) not in path.name
    assert path.parent.exists()


def test_ac1_default_socket_path_matches_across_simulated_processes() -> None:
    """The whole point of Reading A: two callers in different processes
    compute the same path. Simulate by calling twice (same process is the
    structurally weaker check; if they match here they trivially match
    across processes because the function takes no PID-coupled input).
    """
    a = _default_daemon_socket_path()
    b = _default_daemon_socket_path()
    assert a == b


def test_ac1_daemon_subprocess_smoke_socket_path_stays_short() -> None:
    path = _daemon_subprocess_smoke_socket_path()

    assert path.parent == Path("/tmp")
    assert len(str(path)) < 100


def test_ac2_socket_path_flag_appears_in_help() -> None:
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    out = _plain(result.stdout)
    assert "--socket-path" in out


# ---------------------------------------------------------------------------
# AC #4 — drained_flag → shutdown propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_drained_flag_triggers_uvicorn_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `ctx.drained_flag` fires, uvicorn.serve() is requested to exit."""
    fake_ctx_state: dict[str, Any] = {}
    drained = asyncio.Event()

    class _FakeCtx:
        def __init__(self) -> None:
            self.drained_flag = drained
            self.mcp_server = _FakeMCPServer()

    class _FakeMCPServer:
        def __init__(self) -> None:
            self._state = fake_ctx_state
            self.server = _FakeFastMCP()

    class _FakeFastMCP:
        def streamable_http_app(self) -> Any:
            return object()

    fake_ctx = _FakeCtx()

    async def _fake_bootstrap(*args: Any, **kwargs: Any) -> Any:
        return fake_ctx

    shutdown_called: list[Any] = []

    async def _fake_shutdown(ctx: Any, *, timeout: float = 30.0) -> Any:
        shutdown_called.append(ctx)
        return object()

    serve_calls: list[bool] = []

    class _FakeUvicornServer:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False
            self.force_exit = False

        async def serve(self) -> None:
            serve_calls.append(True)
            while not self.should_exit:
                await asyncio.sleep(0.01)

    class _FakeUvicornConfig:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.uds = kwargs.get("uds")

    fake_uvicorn = type("uvicorn", (), {})()
    fake_uvicorn.Server = _FakeUvicornServer  # type: ignore[attr-defined]
    fake_uvicorn.Config = _FakeUvicornConfig  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    import harness_runtime.bootstrap as _bootstrap_mod

    monkeypatch.setattr(_bootstrap_mod, "run_bootstrap", _fake_bootstrap)
    _shutdown_mod = sys.modules["harness_runtime.shutdown"]
    monkeypatch.setattr(_shutdown_mod, "shutdown", _fake_shutdown)

    socket_path = tmp_path / "ac4.sock"

    async def _trigger_drain_soon() -> None:
        await asyncio.sleep(0.05)
        drained.set()

    asyncio.create_task(_trigger_drain_soon())
    await _cli_app_mod._daemon_main(
        runtime_config=_runtime_config(tmp_path),
        socket_path=socket_path,
    )
    assert len(serve_calls) == 1
    assert len(shutdown_called) == 1
    assert shutdown_called[0] is fake_ctx
    # Socket file should be cleaned up.
    assert not socket_path.exists()


# ---------------------------------------------------------------------------
# AC #7 — daemon startup failure → RT-FAIL-CLI-DAEMON-CONNECTION → exit 4
# ---------------------------------------------------------------------------


def test_ac7_bootstrap_failure_raises_daemon_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BootstrapFailure inside daemon_main is wrapped as DaemonStartupError."""
    from harness_runtime.bootstrap import BootstrapFailure
    from harness_runtime.types import BootstrapStage

    async def _failing_bootstrap(*args: Any, **kwargs: Any) -> Any:
        raise BootstrapFailure(BootstrapStage.LOOP_INIT, RuntimeError("synthetic boot failure"))

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _failing_bootstrap)
    with pytest.raises(DaemonStartupError) as excinfo:
        asyncio.run(
            _cli_app_mod._daemon_main(
                runtime_config=_runtime_config(tmp_path),
                socket_path=tmp_path / "ac7.sock",
            )
        )
    assert "bootstrap failure" in str(excinfo.value)
    assert excinfo.value.FAIL_CLASS == "RT-FAIL-CLI-DAEMON-CONNECTION"


@pytest.mark.asyncio
async def test_ac7_uvicorn_serve_failure_raises_daemon_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uvicorn startup/bind failure is retrieved and surfaced as CLI failure."""
    fake_ctx_state: dict[str, Any] = {}
    drained = asyncio.Event()

    class _FakeCtx:
        def __init__(self) -> None:
            self.drained_flag = drained
            self.mcp_server = _FakeMCPServer()

    class _FakeMCPServer:
        def __init__(self) -> None:
            self._state = fake_ctx_state
            self.server = _FakeFastMCP()

    class _FakeFastMCP:
        def streamable_http_app(self) -> Any:
            return object()

    fake_ctx = _FakeCtx()

    async def _fake_bootstrap(*args: Any, **kwargs: Any) -> Any:
        return fake_ctx

    shutdown_called: list[Any] = []

    async def _fake_shutdown(ctx: Any, *, timeout: float = 30.0) -> Any:
        shutdown_called.append(ctx)
        return object()

    class _FailingUvicornServer:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False
            self.force_exit = False

        async def serve(self) -> None:
            raise OSError("AF_UNIX path too long")

    class _FakeUvicornConfig:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.uds = kwargs.get("uds")

    fake_uvicorn = type("uvicorn", (), {})()
    fake_uvicorn.Server = _FailingUvicornServer  # type: ignore[attr-defined]
    fake_uvicorn.Config = _FakeUvicornConfig  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    import harness_runtime.bootstrap as _bootstrap_mod

    monkeypatch.setattr(_bootstrap_mod, "run_bootstrap", _fake_bootstrap)
    _shutdown_mod = sys.modules["harness_runtime.shutdown"]
    monkeypatch.setattr(_shutdown_mod, "shutdown", _fake_shutdown)

    with pytest.raises(DaemonStartupError) as excinfo:
        await _cli_app_mod._daemon_main(
            runtime_config=_runtime_config(tmp_path),
            socket_path=tmp_path / "too-long.sock",
        )

    assert "failed to bind Unix-socket" in str(excinfo.value)
    assert "AF_UNIX path too long" in str(excinfo.value)
    assert shutdown_called == [fake_ctx]


# ---------------------------------------------------------------------------
# Adjacent — config load failure surfaces at CLI layer as exit 3
# ---------------------------------------------------------------------------


def test_config_load_failure_exits_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_load(
        cls: Any,
        config_file: Path | None = None,
        cli_overrides: dict[str, Any] | None = None,
    ) -> RuntimeConfig:
        raise RuntimeConfigLoadError("synthetic test failure", source="test")

    monkeypatch.setattr(_cli_app_mod.RuntimeConfigSource, "load", classmethod(_fake_load))
    result = runner.invoke(app, ["daemon"])
    assert result.exit_code == EXIT_CONFIG_ERROR, result.stdout + result.stderr
    assert "RT-FAIL-CLI-CONFIG-LOAD" in result.stderr


# ---------------------------------------------------------------------------
# AC #1 e2e — subprocess smoke: daemon starts, binds socket, exits on SIGTERM
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason=(
        "Daemon subprocess e2e requires ANTHROPIC_API_KEY to advance past "
        "stage 3a CP_CLIENTS. With the env-var fallback at "
        "`KeyringSecretResolver._lookup` (per "
        "`.harness/binding_fix_keyring_resolver_env_var_fallback.md` + "
        "ADR-F5 v1.1 §(b)(i) headless-mode framing), the daemon catches up "
        "to ANTHROPIC_API_KEY without a keyring entry. Gating matches "
        "mech-β AC #1 precedent. Subprocess will run ping against the real "
        "Anthropic API once the key is present."
    ),
)
def test_ac1_e2e_daemon_subprocess_binds_socket_and_shuts_down(
    tmp_path: Path,
) -> None:
    """End-to-end: launch real daemon subprocess, verify socket binding,
    send SIGTERM, verify exit 0 + socket cleanup.

    Composes a minimal ``harness.toml`` with all 4 PathClass path-binding
    entries that bootstrap stage IS-1 requires (per
    ``[[finding-bootstrap-stage-is-1-requires-skills-path-binding]]``
    Resolution reading (D-test)). Anthropic provider is required
    (``anthropic_optional=False``); the env-var fallback at the keyring
    resolver lets the test source the key from ``ANTHROPIC_API_KEY`` per
    ADR-F5 §(b)(i) headless-mode framing.
    """
    socket_path = _daemon_subprocess_smoke_socket_path()
    repo_root = tmp_path
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    routing_manifest = tmp_path / "routing_manifest"
    routing_manifest.mkdir()
    state_ledger = tmp_path / "state_ledger"
    state_ledger.mkdir()
    config_file = tmp_path / "harness.toml"

    def _binding(pc: str, path: Path) -> str:
        return (
            "[[runtime.path_bindings.raw_entries]]\n"
            f'path_class = "{pc}"\n'
            'workflow_class = "software-engineering"\n'
            'deployment_surface = "local-development"\n'
            f'path = "{path}"\n\n'
        )

    config_file.write_text(
        "[runtime]\n"
        # Anthropic is required so the env-fallback path is exercised.
        # ANTHROPIC_API_KEY is sourced from the test env per the skipif gate.
        "anthropic_optional = false\n"
        "openai_optional = true\n"
        "ollama_optional = true\n"
        "\n"
        "[runtime.otel]\n"
        'otlp_endpoint = "http://localhost:4318"\n'
        "\n"
        "[runtime.routing_manifest]\n"
        "manifest_version = 1\n"
        "per_role_bindings = {}\n"
        "per_workload_overrides = {}\n"
        "retry_policies = {}\n"
        "fallback_chains = [\n"
        '  { primary = { provider = "anthropic", model = "claude-haiku-4-5", '
        'family = "anthropic" }, same_family = [], cross_family = [] },\n'
        "]\n"
        "\n"
        + _binding("SKILLS", skills_dir)
        + _binding("PROMPTS", prompts_dir)
        + _binding("ROUTING_MANIFEST", routing_manifest)
        + _binding("STATE_LEDGER", state_ledger)
    )
    env = {
        **os.environ,
        "HARNESS_DEPLOYMENT_SURFACE": "local-development",
        "HARNESS_REPOSITORY_ROOT": str(repo_root),
        "HARNESS_DEFAULT_TOPOLOGY": "single-threaded-linear",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
    env.pop("OPENAI_API_KEY", None)
    cmd = [
        sys.executable,
        "-m",
        "harness_runtime.cli",
        "daemon",
        "--config",
        str(config_file),
        "--socket-path",
        str(socket_path),
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Wait for socket binding (up to 10s).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if socket_path.exists():
                break
            if proc.poll() is not None:
                stdout = proc.stdout.read().decode() if proc.stdout else ""
                stderr = proc.stderr.read().decode() if proc.stderr else ""
                pytest.fail(
                    f"daemon exited before binding socket (code={proc.returncode}): "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
            time.sleep(0.1)
        else:
            proc.send_signal(signal.SIGKILL)
            pytest.fail(f"socket {socket_path} did not appear within 10s")

        # Send SIGTERM and verify clean exit.
        proc.send_signal(signal.SIGTERM)
        exit_code = proc.wait(timeout=15.0)
        assert exit_code == 0, (
            f"daemon exited non-zero: code={exit_code} "
            f"stderr={proc.stderr.read().decode() if proc.stderr else ''!r}"
        )
        assert not socket_path.exists(), "socket file not cleaned up"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5.0)
        socket_path.unlink(missing_ok=True)
