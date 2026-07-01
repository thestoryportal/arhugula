"""`harness-shutdown` admin CLI — signal-running-instance (U-RT-48; C-RT-13).

Per `Spec_Harness_Runtime_v1.md` v1.1 §13:

> `harness-shutdown` (signal-running-instance).
> - Reads pidfile (location resolved via PATH_CLASS_REGISTRY; suggest
>   `.harness/runtime.pid`).
> - Sends `SIGTERM` to the pid.
> - Optionally waits for process exit with `--wait <seconds>` (default:
>   no wait).
> - Exits 0 on signal delivery success; nonzero on pidfile-missing or
>   signal-delivery error.
> - The receiving harness instance's signal handler is responsible for
>   the actual drain → shutdown sequence (per C-RT-10 + C-RT-11).

**Read-only invariant** (C-RT-13 invariant #2): `harness-shutdown` MUST
NOT touch state ledger / collector sqlite / configuration files. It
only reads the pidfile and emits a signal. Tested via sentinel
monkeypatch on `Path.open` + `os.open`.

**Fork extension** (`[[fork-u-rt-44-workflow-loop-drain]]`): `--wait`
polls the target process for exit via `os.kill(pid, 0)`. The
**receiving** harness's signal handler at HEAD only sets `drained_flag`
(U-RT-44); it does NOT call `shutdown()` and the CP workflow loop drain
is STRUCK. Until the fork lands a CP workflow loop + signal-handler
→ shutdown chain, `--wait` will time out against a running harness
(exit 3). The CLI mechanics are correct; the round-trip completes when
the fork resolves.

**Framework discipline** (spec §13): argparse stdlib; no click/typer.

**Pidfile path resolution**: spec says "via PATH_CLASS_REGISTRY". At
HEAD `PATH_CLASS_REGISTRY` has 4 values (no PIDFILE class) per
`[[fork-trace-storage-pathclass-gap]]`. CLI takes `--pidfile-path`
(default `.harness/runtime.pid` CWD-relative).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from harness_runtime.admin.pidfile import PidfileError, read_pidfile

__all__ = ["build_parser", "main"]


# ---------------------------------------------------------------------------
# Defaults + exit codes.
# ---------------------------------------------------------------------------


_DEFAULT_PIDFILE_PATH = Path(".harness/runtime.pid")
_POLL_INTERVAL_SECONDS = 0.1

_EXIT_OK = 0
_EXIT_ADMIN_PIDFILE = 2  # RT-FAIL-ADMIN-PIDFILE per C-RT-13
_EXIT_WAIT_TIMEOUT = 3


# ---------------------------------------------------------------------------
# argparse.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Factored for unit-testability."""
    parser = argparse.ArgumentParser(
        prog="harness-shutdown",
        description=(
            "Signal a running harness instance to drain (Track A admin "
            "stub per Spec_Harness_Runtime_v1.md §13 C-RT-13)."
        ),
    )
    parser.add_argument(
        "--pidfile-path",
        type=Path,
        default=_DEFAULT_PIDFILE_PATH,
        help=(
            f"Path to the pidfile (default: {_DEFAULT_PIDFILE_PATH}). "
            "Written by the receiving harness at stage 7 INGRESS_ACCEPT."
        ),
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help=(
            "Seconds to wait for the receiving process to exit. Default 0 "
            "(no wait). Polled at 100ms intervals via os.kill(pid, 0). "
            "NOTE: at HEAD, the receiving signal handler only sets "
            "drained_flag; the in-flight drain primitive is STRUCK per "
            "fork-u-rt-44-workflow-loop-drain. Until the fork resolves, "
            "--wait against a real harness will time out (exit 3)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output. Default is human-readable.",
    )
    return parser


# ---------------------------------------------------------------------------
# Liveness + signal delivery.
# ---------------------------------------------------------------------------


def _is_alive(pid: int) -> bool:
    """Return True iff `pid` is currently a live process.

    Uses `os.kill(pid, 0)` — POSIX existence probe. Returns False on
    `ProcessLookupError` (ESRCH) or `PermissionError` ambiguity (PID
    exists but caller lacks signal permission; for our purposes treat
    as not-our-process and surface as stale).
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def _send_sigterm(pid: int) -> bool:
    """Send SIGTERM to `pid`. Returns True on success, False on failure."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _wait_for_exit(pid: int, timeout_seconds: float) -> bool:
    """Poll `pid` for exit. Returns True if exited within `timeout_seconds`."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return not _is_alive(pid)


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------


def _emit(
    *,
    json_mode: bool,
    pidfile_path: Path,
    pid: int | None,
    status: str,
    detail: str,
    to_stderr: bool = False,
) -> None:
    # Resolve sys.stdout/stderr at call time (NOT def-time default) so
    # pytest's capsys monkey-patch is honored.
    stream = sys.stderr if to_stderr else sys.stdout
    if json_mode:
        payload = {
            "pidfile_path": str(pidfile_path),
            "pid": pid,
            "status": status,
            "detail": detail,
        }
        stream.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        stream.write(f"harness-shutdown: {status} — {detail}\n")


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`harness-shutdown` entry point.

    Returns the process exit code:
    - 0 (EXIT_OK) — signal delivered (and optionally process exited within
      --wait).
    - 2 (EXIT_ADMIN_PIDFILE) — RT-FAIL-ADMIN-PIDFILE: pidfile missing,
      unparseable, PID stale (not running), or signal delivery denied.
    - 3 (EXIT_WAIT_TIMEOUT) — --wait expired with PID still alive.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    pidfile_path: Path = args.pidfile_path
    wait_seconds: float = args.wait
    json_mode: bool = args.json

    # Step 1: read pidfile.
    try:
        pid = read_pidfile(pidfile_path)
    except PidfileError as exc:
        _emit(
            json_mode=json_mode,
            pidfile_path=pidfile_path,
            pid=None,
            status="RT-FAIL-ADMIN-PIDFILE",
            detail=str(exc),
            to_stderr=True,
        )
        return _EXIT_ADMIN_PIDFILE

    # Step 2: liveness probe (stale detection).
    if not _is_alive(pid):
        _emit(
            json_mode=json_mode,
            pidfile_path=pidfile_path,
            pid=pid,
            status="RT-FAIL-ADMIN-PIDFILE",
            detail=f"stale: pid {pid} not running",
            to_stderr=True,
        )
        return _EXIT_ADMIN_PIDFILE

    # Step 3: send SIGTERM.
    if not _send_sigterm(pid):
        _emit(
            json_mode=json_mode,
            pidfile_path=pidfile_path,
            pid=pid,
            status="RT-FAIL-ADMIN-PIDFILE",
            detail=f"signal delivery denied for pid {pid}",
            to_stderr=True,
        )
        return _EXIT_ADMIN_PIDFILE

    # Step 4: optional wait for exit.
    if wait_seconds > 0:
        exited = _wait_for_exit(pid, wait_seconds)
        if not exited:
            _emit(
                json_mode=json_mode,
                pidfile_path=pidfile_path,
                pid=pid,
                status="wait-timeout",
                detail=(
                    f"SIGTERM delivered to pid {pid}; process still alive "
                    f"after {wait_seconds}s wait. See "
                    f"fork-u-rt-44-workflow-loop-drain — the receiving "
                    f"signal handler at HEAD only sets drained_flag."
                ),
            )
            return _EXIT_WAIT_TIMEOUT
        _emit(
            json_mode=json_mode,
            pidfile_path=pidfile_path,
            pid=pid,
            status="exited",
            detail=f"pid {pid} exited within wait budget",
        )
        return _EXIT_OK

    _emit(
        json_mode=json_mode,
        pidfile_path=pidfile_path,
        pid=pid,
        status="signaled",
        detail=f"SIGTERM delivered to pid {pid}",
    )
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover — invoked via console_script
    raise SystemExit(main())
