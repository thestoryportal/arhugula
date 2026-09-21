"""The installed entrypoint must expose failures to its parent process."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--help"], 0),
        (["--not-a-harness-option"], 3),
        (["daemon", "--config", "missing.toml"], 3),
        (["inspect", "--ledger-path", "missing.jsonl"], 2),
    ],
)
def test_cli_process_exit_contract(tmp_path: Path, arguments: list[str], expected: int) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from harness_runtime.cli import main; main()", *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == expected, result.stdout + result.stderr
