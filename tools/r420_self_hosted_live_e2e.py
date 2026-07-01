#!/usr/bin/env python3
"""Live R-420 SELF_HOSTED_SERVER local-stack e2e.

This command is intentionally not part of CI. It expects the operator-owned
local Docker Compose telemetry backend to be running and an OS keyring item to
exist for every configured provider-secret allowlist entry.

The exercise is no-paid by default: provider bootstrap uses local Ollama and
the daemon workflow dispatches a deterministic MCP echo tool.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harness_as.sandbox_tier import SandboxTier
from harness_runtime.config.provider_secrets import (
    SecretResolutionError,
    make_keyring_resolver,
)
from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW = ROOT / "deploy" / "self-hosted-local" / "r420-tool-echo.toml"
DEFAULT_SOCKET = Path("/tmp/harness-r420-self-hosted.sock")
SUCCESS_STATUSES = frozenset({"completed", "success"})


class LiveE2EError(RuntimeError):
    """Raised for a failed R-420 live gate."""


def _load_readiness_evaluator() -> Any:
    module_path = ROOT / "tools" / "self_hosted_readiness.py"
    spec = importlib.util.spec_from_file_location("r420_self_hosted_readiness", module_path)
    if spec is None or spec.loader is None:
        raise LiveE2EError(f"could not load readiness helper from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.evaluate_config


evaluate_config = _load_readiness_evaluator()


def _print_step(message: str) -> None:
    print(f"[r420-live] {message}", flush=True)


def _tail(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _probe_tcp(url: str, *, timeout_seconds: float) -> None:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise LiveE2EError(f"OTLP endpoint has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((parsed.hostname, port), timeout=timeout_seconds):
        return None


def _verify_keyring_entries(config_path: Path) -> None:
    config = RuntimeConfigSource.load(config_file=config_path)
    report = evaluate_config(config)
    if not report.ready:
        failed = "; ".join(
            f"{check.name}: {check.detail}" for check in report.checks if not check.ok
        )
        raise LiveE2EError(f"static readiness failed: {failed}")

    resolver = make_keyring_resolver(config.provider_secrets)
    if not config.provider_secrets.operator_allowlist:
        raise LiveE2EError("provider_secrets.operator_allowlist is empty")

    for entry in config.provider_secrets.operator_allowlist:
        try:
            resolver.resolve(entry.name, entry.scope, SandboxTier.TIER_1_PROCESS, tool=None)
        except SecretResolutionError as exc:
            raise LiveE2EError(
                f"keyring entry {entry.name!r} did not resolve through self-hosted-keyring"
            ) from exc

    _probe_tcp(config.otel.otlp_endpoint, timeout_seconds=5)


def _wait_for_socket(
    socket_path: Path,
    process: subprocess.Popen[str],
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise LiveE2EError(
                "daemon exited before binding socket\n"
                f"stdout:\n{_tail(stdout)}\n"
                f"stderr:\n{_tail(stderr)}"
            )
        if socket_path.exists():
            return
        time.sleep(0.2)
    raise LiveE2EError(f"daemon did not bind socket at {socket_path} within {timeout_seconds}s")


def _run_checked(
    command: Sequence[str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise LiveE2EError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{_tail(completed.stdout)}\n"
            f"stderr:\n{_tail(completed.stderr)}"
        )
    return completed


def _status_is_successful(payload: dict[str, object]) -> bool:
    return str(payload.get("status", "")).lower() in SUCCESS_STATUSES


def _run_daemon_e2e(
    *,
    config_path: Path,
    workflow_path: Path,
    socket_path: Path,
    startup_timeout_seconds: float,
    run_timeout_seconds: float,
) -> dict[str, object]:
    socket_path.unlink(missing_ok=True)
    daemon = subprocess.Popen(
        [
            "uv",
            "run",
            "harness",
            "daemon",
            "--config",
            str(config_path),
            "--socket-path",
            str(socket_path),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_socket(socket_path, daemon, startup_timeout_seconds)
        client = _run_checked(
            [
                "uv",
                "run",
                "harness",
                "run",
                str(workflow_path),
                "--daemon",
                "--socket-path",
                str(socket_path),
                "--output",
                "json",
            ],
            timeout_seconds=run_timeout_seconds,
        )
        try:
            payload = json.loads(client.stdout)
        except json.JSONDecodeError as exc:
            raise LiveE2EError(f"daemon client did not emit JSON: {client.stdout!r}") from exc
        if not _status_is_successful(payload):
            raise LiveE2EError(f"daemon workflow did not complete: {payload!r}")
        return payload
    finally:
        if daemon.poll() is None:
            daemon.send_signal(signal.SIGINT)
            try:
                daemon.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.communicate(timeout=5)
        socket_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="SELF_HOSTED_SERVER harness config")
    parser.add_argument(
        "--workflow",
        type=Path,
        default=DEFAULT_WORKFLOW,
        help=f"Workflow manifest to dispatch through the daemon (default: {DEFAULT_WORKFLOW})",
    )
    parser.add_argument(
        "--socket-path",
        type=Path,
        default=DEFAULT_SOCKET,
        help=f"Temporary daemon Unix socket path (default: {DEFAULT_SOCKET})",
    )
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument("--run-timeout", type=float, default=90.0)
    args = parser.parse_args(argv)

    config_path = args.config.resolve()
    workflow_path = args.workflow.resolve()
    try:
        _print_step("loading static SELF_HOSTED_SERVER config")
        _verify_keyring_entries(config_path)
        _print_step("static readiness, keyring resolution, and OTLP TCP probe passed")
        _print_step("starting daemon and dispatching tool workflow")
        payload = _run_daemon_e2e(
            config_path=config_path,
            workflow_path=workflow_path,
            socket_path=args.socket_path,
            startup_timeout_seconds=args.startup_timeout,
            run_timeout_seconds=args.run_timeout,
        )
    except (LiveE2EError, RuntimeConfigLoadError, OSError, subprocess.SubprocessError) as exc:
        print(f"R-420 live e2e failed: {exc}", file=sys.stderr)
        return 1

    _print_step(
        "completed: workflow="
        f"{payload.get('workflow_id')} status={payload.get('status')} "
        "cost=0 hosted-provider-calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
