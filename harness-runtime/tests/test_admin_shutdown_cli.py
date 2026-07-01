"""U-RT-48 — `harness-shutdown` admin CLI tests.

ACs per spec §13 C-RT-13:
- Reads pidfile.
- Sends SIGTERM.
- Optional `--wait` polls for exit.
- Stale pidfile / signal-denied surface as RT-FAIL-ADMIN-PIDFILE (exit 2).
- Read-only invariant: no writes to ledger/sqlite/config.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from harness_runtime.admin.pidfile import write_pidfile
from harness_runtime.admin.shutdown_cli import build_parser, main

# ---------------------------------------------------------------------------
# Parser.
# ---------------------------------------------------------------------------


def test_parser_defaults() -> None:
    ns = build_parser().parse_args([])
    assert ns.pidfile_path == Path(".harness/runtime.pid")
    assert ns.wait == 0.0
    assert ns.json is False


def test_parser_flags() -> None:
    ns = build_parser().parse_args(["--pidfile-path", "/tmp/p.pid", "--wait", "2.5", "--json"])
    assert ns.pidfile_path == Path("/tmp/p.pid")
    assert ns.wait == 2.5
    assert ns.json is True


# ---------------------------------------------------------------------------
# Happy path — SIGTERM sent (monkeypatched os.kill).
# ---------------------------------------------------------------------------


def _signal_spy(monkeypatch: pytest.MonkeyPatch, *, alive: bool = True) -> list[tuple[int, int]]:
    """Replace os.kill with a spy. `alive=True` makes the liveness probe pass."""
    calls: list[tuple[int, int]] = []

    def _spy(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == 0 and not alive:
            raise ProcessLookupError(f"no such pid: {pid}")

    monkeypatch.setattr(os, "kill", _spy)
    return calls


def test_shutdown_cli_sends_sigterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pidfile = tmp_path / "runtime.pid"
    write_pidfile(pidfile, 11111)
    calls = _signal_spy(monkeypatch, alive=True)

    code = main(["--pidfile-path", str(pidfile)])

    assert code == 0
    out = capsys.readouterr().out
    assert "signaled" in out
    # The CLI should have made a liveness probe (sig 0) and a SIGTERM.
    assert (11111, 0) in calls
    assert (11111, int(signal.SIGTERM)) in calls


def test_shutdown_cli_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pidfile = tmp_path / "runtime.pid"
    write_pidfile(pidfile, 22222)
    _signal_spy(monkeypatch, alive=True)

    code = main(["--pidfile-path", str(pidfile), "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert code == 0
    assert payload["pid"] == 22222
    assert payload["status"] == "signaled"
    assert "SIGTERM" in payload["detail"]


# ---------------------------------------------------------------------------
# Failure paths — RT-FAIL-ADMIN-PIDFILE (exit 2).
# ---------------------------------------------------------------------------


def test_shutdown_cli_missing_pidfile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--pidfile-path", str(tmp_path / "missing.pid")])
    assert code == 2
    err = capsys.readouterr().err
    assert "RT-FAIL-ADMIN-PIDFILE" in err


def test_shutdown_cli_unparseable_pidfile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pidfile = tmp_path / "junk.pid"
    pidfile.write_text("not-a-number\n")

    code = main(["--pidfile-path", str(pidfile)])
    assert code == 2
    err = capsys.readouterr().err
    assert "RT-FAIL-ADMIN-PIDFILE" in err


def test_shutdown_cli_stale_pidfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PID exists in pidfile but the process is gone — `os.kill(pid, 0)` raises."""
    pidfile = tmp_path / "runtime.pid"
    write_pidfile(pidfile, 99999)
    _signal_spy(monkeypatch, alive=False)

    code = main(["--pidfile-path", str(pidfile)])

    assert code == 2
    err = capsys.readouterr().err
    assert "stale" in err
    assert "99999" in err


def test_shutdown_cli_signal_permission_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pidfile = tmp_path / "runtime.pid"
    write_pidfile(pidfile, 77777)

    def _spy(pid: int, sig: int) -> None:
        if sig == 0:
            return  # alive
        raise PermissionError("not allowed")

    monkeypatch.setattr(os, "kill", _spy)

    code = main(["--pidfile-path", str(pidfile)])

    assert code == 2
    err = capsys.readouterr().err
    assert "signal delivery denied" in err


# ---------------------------------------------------------------------------
# --wait polling.
# ---------------------------------------------------------------------------


def test_shutdown_cli_wait_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--wait expires with the PID still alive → exit 3."""
    pidfile = tmp_path / "runtime.pid"
    write_pidfile(pidfile, 33333)
    _signal_spy(monkeypatch, alive=True)
    # Speed up the polling test: monkey-patch time.sleep to a no-op.
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # type: ignore[arg-type,misc]

    code = main(["--pidfile-path", str(pidfile), "--wait", "0.05"])

    assert code == 3
    out = capsys.readouterr().out
    assert "wait-timeout" in out
    assert "fork-u-rt-44-workflow-loop-drain" in out


def test_shutdown_cli_wait_observes_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PID stops being alive during the wait window → exit 0."""
    pidfile = tmp_path / "runtime.pid"
    write_pidfile(pidfile, 44444)

    call_count = {"n": 0}

    def _spy(pid: int, sig: int) -> None:
        call_count["n"] += 1
        if sig == 0 and call_count["n"] >= 3:
            # After the SIGTERM and the first poll, the process "exits."
            raise ProcessLookupError(f"no such pid: {pid}")

    monkeypatch.setattr(os, "kill", _spy)
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # type: ignore[arg-type,misc]

    code = main(["--pidfile-path", str(pidfile), "--wait", "5"])

    assert code == 0
    out = capsys.readouterr().out
    assert "exited" in out


# ---------------------------------------------------------------------------
# Read-only invariant (spec §13 invariant #2).
# ---------------------------------------------------------------------------


def test_shutdown_cli_does_not_open_anything_for_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel: only the pidfile may be read; no writes anywhere."""
    pidfile = tmp_path / "runtime.pid"
    write_pidfile(pidfile, 55555)
    _signal_spy(monkeypatch, alive=True)

    real_path_open: Any = Path.open
    real_os_open: Any = os.open
    write_attempts: list[str] = []

    def _spy_path_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(c in mode for c in ("w", "a", "x", "+")):
            write_attempts.append(f"Path.open({self}, mode={mode!r})")
        return real_path_open(self, mode, *args, **kwargs)

    def _spy_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        if flags & write_flags:
            write_attempts.append(f"os.open({path}, flags={flags})")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(Path, "open", _spy_path_open)
    monkeypatch.setattr(os, "open", _spy_os_open)

    code = main(["--pidfile-path", str(pidfile)])

    assert code == 0
    assert write_attempts == [], f"unexpected write attempts: {write_attempts}"


# ---------------------------------------------------------------------------
# Real-subprocess integration (one test).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics required")
def test_shutdown_cli_signals_real_subprocess(tmp_path: Path) -> None:
    """Fork a real subprocess and verify SIGTERM is delivered.

    Tests signal-delivery only (no `--wait`). `--wait` polling against a
    real subprocess hits a zombie-detection issue: `os.kill(zombie_pid, 0)`
    returns success on POSIX until the parent calls `wait()`. The wait
    semantics are exercised in `test_shutdown_cli_wait_observes_exit` via
    monkeypatched os.kill.
    """
    pidfile = tmp_path / "runtime.pid"
    ready_file = tmp_path / "handler.ready"
    script = (
        "import pathlib, signal, time, sys; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        f"pathlib.Path({str(ready_file)!r}).write_text('ready'); "
        "time.sleep(10)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        write_pidfile(pidfile, proc.pid)
        deadline = time.monotonic() + 2.0
        while not ready_file.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        assert ready_file.exists(), "subprocess did not install SIGTERM handler"

        # No --wait: signal-delivery only.
        code = main(["--pidfile-path", str(pidfile)])

        assert code == 0
        # Subprocess should exit cleanly (exit code 0 from sys.exit(0)) once
        # the parent test calls .wait() to reap it.
        return_code = proc.wait(timeout=2.0)
        assert return_code == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# pyproject scripts entry.
# ---------------------------------------------------------------------------


def test_pyproject_scripts_entry_present() -> None:
    here = Path(__file__).resolve()
    pyproject = here.parent.parent / "pyproject.toml"
    text = pyproject.read_text()
    assert "harness-shutdown" in text
    assert "harness_runtime.admin.shutdown_cli:main" in text
