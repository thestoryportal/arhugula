"""Tests for U-RT-102 — operator-facing CLI scaffolding (C-RT-29 / spec v1.35 §14.18).

Maps to acceptance criteria 1–7 at runtime plan v2.31 §1.2.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest
from harness_runtime.cli import main
from harness_runtime.cli.app import app
from typer.testing import CliRunner

runner = CliRunner()

# Typer renders help text via Rich, which interleaves ANSI escape codes inside
# words (e.g. `--config` → `\x1b[1;36m-\x1b[0m\x1b[1;36m-config\x1b[0m`).
# Stripping escapes restores the literal text for substring assertions.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


# AC #1 — extended at PR #84 Reading A apply: parent app now registers all
# 4 subcommands per spec §13.4 + §14.18.1 5-subcommand promise (Track A
# admin: inspect + shutdown; Track B operator: run + daemon).
def test_harness_top_help_lists_all_four_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = _plain(result.stdout)
    for subcommand in ("run", "daemon", "inspect", "shutdown"):
        assert subcommand in out, f"parent app help missing {subcommand!r}"


# PR #84 Reading A — `harness inspect --help` delegates to admin module's
# argparse parser (typer auto-help disabled via help_option_names=[]).
def test_harness_inspect_help_delegates_to_admin_argparse() -> None:
    result = runner.invoke(app, ["inspect", "--help"])
    assert result.exit_code == 0
    out = _plain(result.output)
    # argparse help shows the admin module's actual flag inventory.
    for flag in ("--ledger-path", "--collector-path", "--last-n", "--json"):
        assert flag in out, (
            f"`harness inspect --help` missing {flag!r} — "
            "delegation to admin/inspect.py main() may have regressed"
        )


# PR #84 Reading A — `harness shutdown --help` delegates to admin module's
# argparse parser.
def test_harness_shutdown_help_delegates_to_admin_argparse() -> None:
    result = runner.invoke(app, ["shutdown", "--help"])
    assert result.exit_code == 0
    out = _plain(result.output)
    for flag in ("--pidfile-path", "--wait", "--json"):
        assert flag in out, (
            f"`harness shutdown --help` missing {flag!r} — "
            "delegation to admin/shutdown_cli.py main() may have regressed"
        )


# PR #84 Reading A — `harness inspect <path>` actually runs the admin body
# against a real ledger fixture, verifying the wrapper is not just plumbing
# help but the full body.
def test_harness_inspect_runs_admin_body_against_real_ledger(
    tmp_path: Path,
) -> None:
    # Minimal ledger fixture — genesis entry with valid F2 hash chain.
    import hashlib
    import json

    entry = {
        "action_id": "test:probe",
        "idempotency_key": "k" * 64,
        "actor": {"actor_class": "agent", "actor_id": "test"},
        "response_hash": hashlib.sha256(b"r").hexdigest(),
        "timestamp": "2026-05-29T00:00:00+00:00",
        "prior_event_hash": "0" * 64,
    }
    ledger_path = tmp_path / "state.jsonl"
    ledger_path.write_text(json.dumps(entry) + "\n")

    result = runner.invoke(app, ["inspect", "--ledger-path", str(ledger_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total_entries"] == 1
    assert payload["entries"][0]["action_id"] == "test:probe"


# AC #2
def test_harness_run_help_shows_flag_inventory() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    out = _plain(result.stdout)
    # Positional + 6 declared flags per spec §14.18.1.
    assert "WORKFLOW_FILE" in out or "workflow_file" in out.lower()
    for flag in ("--config", "--daemon", "--output", "--provider", "--model", "--tenant-id"):
        assert flag in out, f"missing flag {flag} in `harness run --help`"


# AC #3
def test_harness_daemon_help_shows_flag_inventory() -> None:
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    assert "--config" in _plain(result.stdout)


# AC #4 — SUPERSEDED at U-RT-106 (one-shot mode body landed at runtime plan
# v2.31 §1.6 / v2.32 §2). The U-RT-102 stub message is no longer emitted by
# `harness run <file>`; the concrete one-shot body + the 11 ACs at
# `test_cli_one_shot.py` supersede this scaffolding-stage assertion. The
# `harness run --daemon` flag remains a stub (U-RT-108) and is covered at
# U-RT-108 landing.


# AC #5 — SUPERSEDED at U-RT-107 (daemon entrypoint body landed at runtime
# plan v2.31 §1.7). The U-RT-102 stub message is no longer emitted by
# `harness daemon`; the concrete daemon body + 8 ACs at `test_cli_daemon.py`
# supersede this scaffolding-stage assertion.


# AC #6 — Track A admin stubs are PRESERVED VERBATIM under `harness-inspect` +
# `harness-shutdown` standalone binaries (spec v1.35 §13.4 + §14.18.6).
def test_harness_inspect_and_shutdown_remain_standalone_binaries() -> None:
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["harness-inspect"] == "harness_runtime.admin.inspect:main"
    assert scripts["harness-shutdown"] == "harness_runtime.admin.shutdown_cli:main"
    # And the operator-facing parent dispatcher lives alongside, not as a replacement.
    assert scripts["harness"] == "harness_runtime.cli:main"


# AC #7
def test_unknown_flag_exits_code_3_with_arg_invalid_fail_class(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown flag → Click UsageError → RT-FAIL-CLI-ARG-INVALID → exit 3."""
    monkeypatch.setattr(sys, "argv", ["harness", "run", "--no-such-flag", "wf.yaml"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 3
    captured = capsys.readouterr()
    assert "RT-FAIL-CLI-ARG-INVALID" in captured.err
