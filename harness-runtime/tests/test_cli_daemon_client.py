"""Tests for U-RT-108 — daemon-client mode (``harness run <file> --daemon``).

Maps to acceptance criteria 1–7 at runtime plan v2.31 §1.8. The daemon-client
mode connects to a running ``harness daemon`` via Unix-socket transport
(MCP streamable-HTTP over uds via httpx.AsyncHTTPTransport) and invokes the
``run_workflow`` MCP tool with the workflow_id-as-path semantics ratified at
``.harness/class_1_fork_u_rt_107_daemon_run_workflow_signature_underspec.md``
Reading (A) 2026-05-28.

Strategy:
- AC #1/#4 — mocked ``_daemon_client_dispatch`` returning synthetic CP RunResult
  dicts; verify exit-code mapping (SUCCESS → 0; DRAINED/FAILED → 1).
- AC #2 — verify ``--socket-path`` flag override + presence in help.
- AC #3 — socket path absent → exit 4 + RT-FAIL-CLI-DAEMON-CONNECTION.
- AC #5/#6/#7 — semantic equivalence + SIGINT graceful disconnect + concurrent
  clients — deferred to U-RT-109 e2e per the L9-undecies precedent (real
  daemon + real MCP client + real workflow execution at cluster terminus).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import harness_runtime.cli.app as _ensure_import
import pytest
from typer.testing import CliRunner

_cli_app_mod = sys.modules["harness_runtime.cli.app"]
assert _ensure_import is not None

from harness_runtime.cli.app import (
    EXIT_BOOTSTRAP_ERROR,
    EXIT_SUCCESS,
    EXIT_WORKFLOW_FAIL,
    app,
)

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


_VALID_YAML = """\
version: 1
workflow:
  workflow_id: "wf-cli-daemon-client"
  workload_class: "software-engineering"
  persona_tier: "solo-developer"
  engine_class: "pure-pattern-no-engine"
  topology_pattern: "evaluator-optimizer"
default_model_binding:
  provider: "anthropic"
  model: "claude-opus-4-7"
steps:
  - step_id: "s1"
    step_kind: "inference-step"
    step_payload: {}
"""


def _write_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "wf.yaml"
    path.write_text(_VALID_YAML, encoding="utf-8")
    return path


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any] | None = None,
    raises: BaseException | None = None,
) -> dict[str, Any]:
    """Install a fake ``_daemon_client_dispatch`` that returns ``payload``."""
    captured: dict[str, Any] = {}

    async def _fake(*, workflow_file: Path, socket_path: Path) -> dict[str, Any]:
        captured["workflow_file"] = workflow_file
        captured["socket_path"] = socket_path
        if raises is not None:
            raise raises
        return (
            payload
            if payload is not None
            else {
                "status": "success",
                "workflow_id": "wf-cli-daemon-client",
                "run_id": "abc123",
            }
        )

    monkeypatch.setattr(_cli_app_mod, "_daemon_client_dispatch", _fake)
    return captured


# ---------------------------------------------------------------------------
# AC #1 — happy path SUCCESS → exit 0
# ---------------------------------------------------------------------------


def test_ac1_daemon_client_success_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "daemon.sock"
    socket_path.touch()  # CLI presence-check requires socket to exist
    captured = _stub_dispatch(monkeypatch, payload={"status": "success"})
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app,
        ["run", str(manifest), "--daemon", "--socket-path", str(socket_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.stdout + result.stderr
    assert captured["workflow_file"] == manifest
    assert captured["socket_path"] == socket_path
    out = _plain(result.stdout)
    assert "success" in out


# ---------------------------------------------------------------------------
# AC #2 — --socket-path override + help
# ---------------------------------------------------------------------------


def test_ac2_socket_path_flag_appears_in_run_help() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    out = _plain(result.stdout)
    assert "--socket-path" in out


def test_ac2_socket_path_override_threads_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "custom.sock"
    socket_path.touch()
    captured = _stub_dispatch(monkeypatch, payload={"status": "success"})
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app,
        ["run", str(manifest), "--daemon", "--socket-path", str(socket_path)],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert captured["socket_path"] == socket_path


@pytest.mark.asyncio
async def test_dispatch_uses_loopback_nominal_url_for_uds_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UDS transport is the real connection path; the URL must still carry
    an ASGI-accepted Host header to avoid HTTP 421 from the daemon app."""
    import mcp
    import mcp.client.streamable_http as streamable_http_mod

    captured: dict[str, Any] = {}

    class _FakeStreamableHTTPClient:
        async def __aenter__(self) -> tuple[object, object, Any]:
            return object(), object(), lambda: None

        async def __aexit__(self, *args: object) -> None:
            return None

    def _fake_streamable_http_client(
        url: str,
        *,
        http_client: Any = None,
        terminate_on_close: bool = True,
    ) -> _FakeStreamableHTTPClient:
        captured["url"] = url
        captured["http_client"] = http_client
        captured["terminate_on_close"] = terminate_on_close
        return _FakeStreamableHTTPClient()

    class _FakeClientSession:
        def __init__(self, read_stream: object, write_stream: object) -> None:
            captured["session_streams"] = (read_stream, write_stream)

        async def __aenter__(self) -> _FakeClientSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def initialize(self) -> None:
            captured["initialized"] = True

        async def call_tool(self, name: str, arguments: dict[str, str]) -> Any:
            captured["tool_name"] = name
            captured["tool_arguments"] = arguments
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text='{"status":"success","workflow_id":"wf"}')],
            )

    monkeypatch.setattr(
        streamable_http_mod,
        "streamable_http_client",
        _fake_streamable_http_client,
    )
    monkeypatch.setattr(mcp, "ClientSession", _FakeClientSession)

    workflow = _write_yaml(tmp_path)
    socket_path = tmp_path / "daemon.sock"
    payload = await _cli_app_mod._daemon_client_dispatch(
        workflow_file=workflow,
        socket_path=socket_path,
    )

    assert payload["status"] == "success"
    assert captured["url"] == _cli_app_mod._DAEMON_CLIENT_STREAMABLE_HTTP_URL
    assert captured["url"] == "http://127.0.0.1/mcp"
    assert captured["http_client"] is not None
    assert captured["tool_name"] == "run_workflow"
    assert captured["tool_arguments"] == {"workflow_id": str(workflow)}


# ---------------------------------------------------------------------------
# AC #3 — daemon not running → RT-FAIL-CLI-DAEMON-CONNECTION → exit 4
# ---------------------------------------------------------------------------


def test_ac3_socket_absent_exits_four_with_daemon_connection_fail_class(
    tmp_path: Path,
) -> None:
    nonexistent = tmp_path / "no-such.sock"
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app, ["run", str(manifest), "--daemon", "--socket-path", str(nonexistent)]
    )
    assert result.exit_code == EXIT_BOOTSTRAP_ERROR, result.stdout + result.stderr
    assert "RT-FAIL-CLI-DAEMON-CONNECTION" in result.stderr


def test_ac3_connection_error_during_dispatch_exits_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "daemon.sock"
    socket_path.touch()
    from harness_runtime.cli.app import DaemonStartupError

    _stub_dispatch(
        monkeypatch,
        raises=DaemonStartupError("synthetic mid-dispatch connection failure"),
    )
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app, ["run", str(manifest), "--daemon", "--socket-path", str(socket_path)]
    )
    assert result.exit_code == EXIT_BOOTSTRAP_ERROR, result.stdout + result.stderr
    assert "RT-FAIL-CLI-DAEMON-CONNECTION" in result.stderr


# ---------------------------------------------------------------------------
# AC #4 — RunResult propagation: SUCCESS → 0, FAILED → 1, DRAINED → 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        ("success", EXIT_SUCCESS),
        ("drained", EXIT_WORKFLOW_FAIL),
        ("failed", EXIT_WORKFLOW_FAIL),
        ("partial", EXIT_WORKFLOW_FAIL),
        ("pending", EXIT_WORKFLOW_FAIL),
    ],
)
def test_ac4_cp_status_to_exit_code_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_exit: int,
) -> None:
    socket_path = tmp_path / "daemon.sock"
    socket_path.touch()
    _stub_dispatch(monkeypatch, payload={"status": status, "workflow_id": "wf"})
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app, ["run", str(manifest), "--daemon", "--socket-path", str(socket_path)]
    )
    assert result.exit_code == expected_exit, f"status={status!r}: {result.stdout + result.stderr}"


# ---------------------------------------------------------------------------
# Adjacent — --output=json emits raw JSON from daemon payload
# ---------------------------------------------------------------------------


def test_output_json_emits_raw_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    socket_path = tmp_path / "daemon.sock"
    socket_path.touch()
    payload = {"status": "success", "workflow_id": "wf-json", "run_id": "xyz"}
    _stub_dispatch(monkeypatch, payload=payload)
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app,
        [
            "run",
            str(manifest),
            "--daemon",
            "--socket-path",
            str(socket_path),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed == payload


# ---------------------------------------------------------------------------
# Adjacent — workflow_id-as-path discriminator threaded correctly
# ---------------------------------------------------------------------------


def test_dispatch_invoked_with_full_manifest_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-RT-107 fork Reading (A): workflow_id passed to daemon is the
    full manifest path (path-input branch triggers handler-side load)."""
    socket_path = tmp_path / "daemon.sock"
    socket_path.touch()
    captured = _stub_dispatch(monkeypatch, payload={"status": "success"})
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app, ["run", str(manifest), "--daemon", "--socket-path", str(socket_path)]
    )
    assert result.exit_code == EXIT_SUCCESS
    # The dispatch helper receives the workflow_file as a Path; the helper
    # body stringifies it for the tool call. Verify the path round-trips.
    assert captured["workflow_file"] == manifest
    from harness_runtime.lifecycle.mcp_server import (
        _looks_like_manifest_path,  # pyright: ignore[reportPrivateUsage]
    )

    assert _looks_like_manifest_path(str(manifest))
