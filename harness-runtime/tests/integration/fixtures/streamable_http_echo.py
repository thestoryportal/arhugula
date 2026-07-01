"""Spawn helper for the streamable-HTTP echo MCP fixture (B-MCP-HOST-REMOTE-TRANSPORT).

Runs `mcp_echo_server_http.py` as a subprocess on a free `127.0.0.1` port and yields
the MCP endpoint URL, with hardened readiness + teardown so the e2e is non-flaky:

- **Free-port allocation** in the parent (bind `:0`, read, close) → passed to the child.
  If the (tiny) bind-close-reuse race loses the port, the child fails to bind and dies;
  the readiness poll detects the dead subprocess and fails fast with its stderr.
- **Endpoint-level readiness** — polls `http://127.0.0.1:<port>/mcp` (not bare TCP-accept)
  until the ASGI app answers with *any* HTTP status (the streamable-HTTP endpoint returns
  4xx to a plain GET — that 4xx still proves the server is up), aborting early if the
  subprocess exits.
- **Guaranteed teardown** — terminate + bounded wait + kill fallback, so no orphaned
  uvicorn worker survives a test (failed or not).
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import httpx

_HTTP_ECHO_FIXTURE = (Path(__file__).parent / "mcp_echo_server_http.py").resolve()


def _free_port() -> int:
    """Allocate a free 127.0.0.1 port (bind :0, read, release for the child to claim)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, proc: subprocess.Popen[bytes], *, timeout: float) -> None:
    """Poll `url` until the server answers any HTTP status, or fail fast.

    A connection error means not-yet-listening (retry); any HTTP response (incl. 4xx)
    means the ASGI app is up. If the subprocess has already exited, abort with its
    captured output rather than spinning until timeout.
    """
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = b""
            if proc.stdout is not None:
                out = proc.stdout.read() or b""
            raise RuntimeError(
                f"streamable-HTTP echo fixture exited early (code {proc.returncode}) "
                f"before readiness:\n{out.decode(errors='replace')}"
            )
        try:
            resp = httpx.get(url, timeout=1.0)
            _ = resp.status_code  # any HTTP status ⇒ the ASGI app is serving
            return
        except httpx.HTTPError as exc:  # not listening yet
            last_err = exc
            time.sleep(0.1)
    raise RuntimeError(
        f"streamable-HTTP echo fixture not ready at {url} within {timeout}s "
        f"(last error: {last_err!r})"
    )


@contextlib.contextmanager
def streamable_http_echo_server(*, ready_timeout: float = 25.0) -> Generator[str, None, None]:
    """Yield the MCP endpoint URL of a running streamable-HTTP echo server subprocess.

    The server is torn down (terminate → wait → kill) on context exit, including on
    test failure inside the `with` block.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(_HTTP_ECHO_FIXTURE), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        _wait_ready(url, proc, timeout=ready_timeout)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
