"""Typer parent app for the operator-facing `harness` CLI.

C-RT-29 contract surface (runtime spec v1.35 §14.18). U-RT-102 lands the
scaffolding; U-RT-106 lands the concrete one-shot ``harness run <file>`` body
per spec v1.35 §14.18.1 + admissibility check at dispatch site per spec v1.36
§14.18.4 + plan v2.32 §2.

Concrete subcommand bodies for daemon mode land downstream:
- ``harness run <file> --daemon``  → U-RT-108 (daemon-client mode)
- ``harness daemon``               → U-RT-107 (daemon entrypoint)
"""

from __future__ import annotations

import asyncio
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.engine_class_candidate import ENGINE_CLASS_CANDIDATES

from harness_runtime.config_source import (
    RuntimeConfigLoadError,
    RuntimeConfigSource,
)
from harness_runtime.lifecycle.workflow_manifest_loader import (
    LoadedWorkflow,
    ManifestAdmissibilityError,
    WorkflowManifestLoader,
    WorkflowManifestLoadError,
)


class OutputFormat(StrEnum):
    """`harness run --output` format selector per Q-F at G1 ratification."""

    text = "text"
    json = "json"


# Exit code mapping per runtime spec v1.35 §14.18.2 strict 5-level shape.
EXIT_SUCCESS = 0
EXIT_WORKFLOW_FAIL = 1
EXIT_MANIFEST_ERROR = 2
EXIT_CONFIG_ERROR = 3
EXIT_BOOTSTRAP_ERROR = 4


app = typer.Typer(
    name="harness",
    help="Multi-LLM Agent Harness operator-facing CLI",
    no_args_is_help=True,
    add_completion=False,
)


def _build_cli_overrides(*, tenant_id: str | None) -> dict[str, Any]:
    """Compose CLI overrides for :class:`RuntimeConfigSource.load`.

    ``--provider`` + ``--model`` override the workflow's ``default_model_binding``
    (workflow-level, applied at workflow projection) — NOT ``RuntimeConfig``.
    Only config-level CLI flags flow into ``cli_overrides``.
    """
    overrides: dict[str, Any] = {}
    if tenant_id is not None:
        overrides["tenant_id"] = tenant_id
    return overrides


def _apply_workflow_overrides(
    workflow: LoadedWorkflow,
    *,
    provider: str | None,
    model: str | None,
) -> LoadedWorkflow:
    """Apply CLI ``--provider`` / ``--model`` overrides to the workflow.

    Each override replaces the corresponding field of
    ``workflow.default_model_binding``. When neither flag is set, the workflow
    is returned unchanged.
    """
    if provider is None and model is None:
        return workflow
    current = workflow.default_model_binding
    new_binding = ModelBinding(
        provider=provider if provider is not None else current.provider,
        model=model if model is not None else current.model,
    )
    return workflow.model_copy(update={"default_model_binding": new_binding})


def _check_engine_class_admissibility(workflow: LoadedWorkflow, *, config: Any) -> None:
    """Verify the manifest engine_class is admissible for the deployment surface.

    Plan v2.32 §2 NEW AC #4 — admissibility deferred from U-RT-104 loader site
    to U-RT-106 dispatch site per spec v1.36 §14.19.2 row 7 + §14.19.4
    invariant 2. ``engine_class`` MUST appear in the candidate_set of the
    :data:`ENGINE_CLASS_CANDIDATES` entry whose ``deployment_surface`` matches
    ``config.deployment_surface``.
    """
    engine_class = workflow.manifest_entry.engine_class
    surface = config.deployment_surface
    candidate = next(
        (c for c in ENGINE_CLASS_CANDIDATES if c.deployment_surface == surface),
        None,
    )
    if candidate is None:
        raise ManifestAdmissibilityError(
            f"no candidate set declared for deployment_surface={surface!r}",
            source="(dispatch)",
        )
    if engine_class not in candidate.candidate_set:
        raise ManifestAdmissibilityError(
            f"engine_class={engine_class.value!r} is not admissible for "
            f"deployment_surface={surface.value!r}; "
            f"candidate_set={sorted(c.value for c in candidate.candidate_set)!r}",
            source="(dispatch)",
        )


def _emit_run_result(result: Any, *, output: OutputFormat) -> None:
    """Emit ``RunResult`` to stdout per ``--output`` mode."""
    if output is OutputFormat.json:
        typer.echo(result.model_dump_json())
        return
    # text mode — operator-readable summary.
    typer.echo(f"status:    {result.status}")
    typer.echo(f"workflow:  {result.workflow_id}")
    typer.echo(f"ledger:    {result.audit_ledger_head_hash}")
    if result.failure_cause is not None:
        typer.echo(f"failure:   {result.failure_cause.runtime_fail_class}", err=True)
        typer.echo(f"detail:    {result.failure_cause.detail}", err=True)


def _print_fail_class(fail_class: str, detail: str) -> None:
    """Emit ``RT-FAIL-*`` fail class + detail to stderr per spec §14.18.4."""
    typer.echo(f"{fail_class}: {detail}", err=True)


_DAEMON_CLIENT_STREAMABLE_HTTP_URL = "http://127.0.0.1/mcp"


async def _daemon_client_dispatch(
    *,
    workflow_file: Path,
    socket_path: Path,
) -> dict[str, Any]:
    """Connect to the running daemon via Unix-socket; invoke run_workflow.

    Per `.harness/class_1_fork_u_rt_107_daemon_run_workflow_signature_
    underspec.md` Reading (A) + Q2=(i) ratification: the daemon's
    ``run_workflow`` tool accepts the manifest filesystem path as the
    ``workflow_id`` argument; the daemon-side handler discriminates path-vs-
    registry-key and loads the manifest on path-input.

    Transport: MCP streamable-HTTP over Unix-socket via a custom httpx
    client factory injecting :class:`httpx.AsyncHTTPTransport(uds=...)`. The
    Unix-socket transport controls the connection path; the nominal URL uses a
    loopback host so ASGI host validation accepts the HTTP ``Host`` header. The
    path component matches FastMCP's default ``streamable_http_path = "/mcp"``.

    Returns
    -------
    dict[str, Any]
        Raw JSON dict returned by the ``run_workflow`` tool (CP RunResult
        ``model_dump(mode='json')`` shape per U-RT-62 handler line ~242).
    """
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    http_client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0))

    try:
        async with streamable_http_client(
            _DAEMON_CLIENT_STREAMABLE_HTTP_URL,
            http_client=http_client,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tool_result = await session.call_tool(
                    "run_workflow",
                    {"workflow_id": str(workflow_file)},
                )
    except OSError as exc:
        raise DaemonStartupError(f"failed to connect to daemon at {socket_path}: {exc}") from exc
    finally:
        await http_client.aclose()

    if tool_result.isError:
        # The handler raised; surface the textual error to operator.
        text_block = tool_result.content[0] if tool_result.content else None
        detail = getattr(text_block, "text", "unknown tool error")
        raise DaemonStartupError(f"daemon-side run_workflow failed: {detail}")
    if not tool_result.content:
        raise DaemonStartupError("daemon-side run_workflow returned empty content")

    import json

    text_block = tool_result.content[0]
    payload_text = getattr(text_block, "text", None)
    if payload_text is None:
        raise DaemonStartupError(
            f"daemon-side run_workflow returned non-text content: {type(text_block).__name__}"
        )
    parsed = json.loads(payload_text)
    if not isinstance(parsed, dict):
        raise DaemonStartupError(
            f"daemon-side run_workflow returned non-dict payload: {type(parsed).__name__}"
        )
    result: dict[str, Any] = {str(k): v for k, v in parsed.items()}  # type: ignore[reportUnknownVariableType,reportUnknownMemberType]
    return result


# CP RunStatus → CLI exit code mapping (mirror of api.py:_CP_TO_RT_STATUS).
# CP statuses: 'success' / 'drained' / 'failed' / 'partial' / 'pending'.
_CP_STATUS_TO_EXIT_CODE: dict[str, int] = {
    "success": EXIT_SUCCESS,
    "drained": EXIT_WORKFLOW_FAIL,
    "failed": EXIT_WORKFLOW_FAIL,
    "partial": EXIT_WORKFLOW_FAIL,
    "pending": EXIT_WORKFLOW_FAIL,
}


def _emit_daemon_run_result(payload: dict[str, Any], *, output: OutputFormat) -> None:
    """Emit the daemon-side CP RunResult payload per ``--output`` mode."""
    if output is OutputFormat.json:
        import json

        typer.echo(json.dumps(payload))
        return
    typer.echo(f"status:    {payload.get('status', 'unknown')}")
    typer.echo(f"workflow:  {payload.get('workflow_id', '-')}")
    if payload.get("fail_class"):
        typer.echo(f"fail:      {payload['fail_class']}", err=True)


@app.command("run")
def run_command(
    workflow_file: Annotated[
        Path,
        typer.Argument(
            help="Workflow manifest path (.yaml / .yml / .toml)",
            show_default=False,
        ),
    ],
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Override default harness.toml config path"),
    ] = None,
    daemon: Annotated[
        bool,
        typer.Option("--daemon", help="Daemon-client mode (sibling to one-shot default)"),
    ] = False,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="RunResult output format"),
    ] = OutputFormat.text,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Override default_model_binding.provider"),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override default_model_binding.model"),
    ] = None,
    tenant_id: Annotated[
        str | None,
        typer.Option("--tenant-id", help="Override RuntimeConfig.tenant_id"),
    ] = None,
    socket_path: Annotated[
        Path | None,
        typer.Option(
            "--socket-path",
            help="Unix-socket path of the daemon (only with --daemon)",
        ),
    ] = None,
) -> None:
    """Invoke a workflow (one-shot, or daemon-client when ``--daemon`` is set)."""
    if daemon:
        # --- Daemon-client mode (U-RT-108 per plan v2.31 §1.8) -----------
        resolved_socket = socket_path if socket_path is not None else _default_daemon_socket_path()
        if not resolved_socket.exists():
            _print_fail_class(
                "RT-FAIL-CLI-DAEMON-CONNECTION",
                f"socket path {resolved_socket} does not exist (daemon not running?)",
            )
            raise typer.Exit(code=EXIT_BOOTSTRAP_ERROR)
        try:
            payload = asyncio.run(
                _daemon_client_dispatch(workflow_file=workflow_file, socket_path=resolved_socket)
            )
        except DaemonStartupError as exc:
            _print_fail_class(exc.FAIL_CLASS, str(exc))
            raise typer.Exit(code=EXIT_BOOTSTRAP_ERROR) from exc
        _emit_daemon_run_result(payload, output=output)
        status = str(payload.get("status", "")).lower()
        raise typer.Exit(code=_CP_STATUS_TO_EXIT_CODE.get(status, EXIT_WORKFLOW_FAIL))

    # --- Stage 1: config load (RT-FAIL-CLI-CONFIG-LOAD → exit 3) ----------
    cli_overrides = _build_cli_overrides(tenant_id=tenant_id)
    try:
        runtime_config = RuntimeConfigSource.load(config_file=config, cli_overrides=cli_overrides)
    except RuntimeConfigLoadError as exc:
        _print_fail_class(exc.FAIL_CLASS, exc.reason)
        raise typer.Exit(code=EXIT_CONFIG_ERROR) from exc

    # --- Stage 2: manifest load (RT-FAIL-CLI-MANIFEST-* → exit 2) ---------
    try:
        workflow = WorkflowManifestLoader.load_workflow(workflow_file)
    except WorkflowManifestLoadError as exc:
        _print_fail_class(exc.FAIL_CLASS, exc.reason)
        raise typer.Exit(code=EXIT_MANIFEST_ERROR) from exc

    # --- Stage 3: workflow-level overrides (--provider / --model) ---------
    workflow = _apply_workflow_overrides(workflow, provider=provider, model=model)

    # --- Stage 4: admissibility check at dispatch site (NEW AC #4) --------
    try:
        _check_engine_class_admissibility(workflow, config=runtime_config)
    except ManifestAdmissibilityError as exc:
        _print_fail_class(exc.FAIL_CLASS, exc.reason)
        raise typer.Exit(code=EXIT_MANIFEST_ERROR) from exc

    # --- Stage 5: dispatch via api.run (synchronous one-shot) -------------
    # Lazy import to keep the CLI cold-path import budget small: api.py pulls
    # bootstrap + shutdown transitively, and `harness --help` should not pay
    # that cost.
    from harness_runtime.api import run as _api_run
    from harness_runtime.bootstrap import BootstrapFailure

    try:
        run_result = asyncio.run(_api_run(workflow, config=runtime_config))
    except BootstrapFailure as exc:
        _print_fail_class("RT-FAIL-BOOTSTRAP", str(exc))
        raise typer.Exit(code=EXIT_BOOTSTRAP_ERROR) from exc

    # --- Stage 6: emit RunResult + exit per §14.18.2 ----------------------
    _emit_run_result(run_result, output=output)
    if run_result.status == "completed":
        raise typer.Exit(code=EXIT_SUCCESS)
    # status ∈ {"drained", "failed"} → exit 1
    raise typer.Exit(code=EXIT_WORKFLOW_FAIL)


def _default_daemon_socket_path() -> Path:
    """Default Unix-socket path for the daemon — single well-known path so that
    ``harness daemon`` and ``harness run --daemon`` resolve to the same socket
    without operator-supplied ``--socket-path`` on either side.

    Resolves to ``${tempfile.gettempdir()}/harness-daemon.sock`` (typically
    ``/tmp/harness-daemon.sock`` on macOS/Linux). Multi-daemon-per-host operator
    scenarios still require explicit ``--socket-path`` on at least one side per
    runtime spec v1.39 §14.18.1; the single-daemon-per-host default Just Works.

    Per Class 1 fork resolution Reading A at
    ``.harness/class_1_fork_daemon_default_socket_path_pid_mismatch.md``
    (operator-ratified 2026-05-29). Previously namespaced by ``os.getpid()``
    which structurally cannot coordinate daemon and client (different
    processes compute different paths).
    """
    import tempfile

    return Path(tempfile.gettempdir()) / "harness-daemon.sock"


class DaemonStartupError(RuntimeError):
    """Raised when the daemon entrypoint fails to bind / start the server.

    Maps to CLI fail-class ``RT-FAIL-CLI-DAEMON-CONNECTION`` → exit code 4
    per runtime spec v1.35 §14.18.4.
    """

    FAIL_CLASS: str = "RT-FAIL-CLI-DAEMON-CONNECTION"


async def _daemon_main(
    *,
    runtime_config: Any,
    socket_path: Path,
) -> None:
    """Daemon entrypoint body — bootstrap, serve on Unix-socket, shutdown.

    Per `.harness/class_1_fork_u_rt_107_daemon_run_workflow_signature_
    underspec.md` Reading (A) ratification 2026-05-28: workflow_id-as-path
    widening at U-RT-62's `run_workflow` handler. Daemon mode reuses the
    existing tool surface VERBATIM at the wire-level signature; the handler
    body discriminates registry-key vs filesystem path on the input.

    Mechanism α (recommended default): uvicorn serving FastMCP's
    `streamable_http_app()` over Unix-socket via `uvicorn.Config(uds=...)`.
    Per-session ctx isolation (spec §14.18.5 spec-MUST) is NOT addressed at
    this MVP — the post-bootstrap `HarnessContext` at `_state['_harness_ctx']`
    is single-shared across concurrent invocations. Concurrent invariant
    (AC #5) deferred to U-RT-109 e2e per `[[verification-shape-sharpened-
    grep-vs-e2e]]` + L9-undecies precedent.
    """
    import uvicorn
    from harness_core.workload_class import WorkloadClass

    from harness_runtime.bootstrap import BootstrapFailure, run_bootstrap
    from harness_runtime.shutdown import shutdown as _shutdown

    # Bootstrap stage 0..9 — constructs the FastMCP server (stage 2) and
    # installs SIGINT/SIGTERM signal handlers (stage 7). The signal handlers
    # set `ctx.drained_flag`; we await that event to break the serve loop.
    try:
        ctx = await run_bootstrap(runtime_config, workload_class=WorkloadClass.SOFTWARE_ENGINEERING)
    except BootstrapFailure as exc:
        raise DaemonStartupError(f"bootstrap failure during daemon startup: {exc}") from exc

    try:
        # Bind the post-bootstrap HarnessContext on the MCP server's state
        # dict; the run_workflow tool handler reads from this key. The
        # `HarnessMCPServer` Protocol at `types.py:551` declares the abstract
        # shape; the concrete dataclass at `lifecycle/mcp_server.py:80` carries
        # `server` + `_state`. Cast through `Any` to access the concrete
        # surface; bootstrap stage 2 guarantees `ctx.mcp_server is not None`.
        from typing import cast as _cast

        concrete_server: Any = _cast(Any, ctx.mcp_server)
        concrete_server._state["_harness_ctx"] = ctx

        # Construct uvicorn server bound to the Unix-socket. FastMCP's
        # `streamable_http_app()` returns a Starlette app exposing the MCP
        # streamable-HTTP transport per the mcp-python-sdk default.
        fastmcp: Any = concrete_server.server
        starlette_app: Any = fastmcp.streamable_http_app()
        uv_config = uvicorn.Config(
            starlette_app,
            uds=str(socket_path),
            log_level="warning",
            lifespan="on",
        )
        uv_server = uvicorn.Server(uv_config)

        # Serve until `ctx.drained_flag` fires (bootstrap stage 7 wires SIGINT
        # / SIGTERM → drained_flag.set()). We race the serve task against the
        # drained_flag wait; the first to complete cancels the other.
        serve_task = asyncio.create_task(uv_server.serve(), name="uvicorn-serve")
        drain_task = asyncio.create_task(ctx.drained_flag.wait(), name="daemon-drain-wait")
        try:
            done, pending = await asyncio.wait(
                {serve_task, drain_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if drain_task in done:
                # Drain signal received — request uvicorn shutdown.
                uv_server.should_exit = True
                try:
                    await asyncio.wait_for(serve_task, timeout=10.0)
                except TimeoutError:
                    uv_server.force_exit = True
                    await serve_task
            else:
                # uvicorn exited first (e.g., bind failure or external stop).
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass
                try:
                    await serve_task
                except OSError as exc:
                    raise DaemonStartupError(
                        f"failed to bind Unix-socket {socket_path}: {exc}"
                    ) from exc
                except Exception as exc:
                    raise DaemonStartupError(f"daemon server exited with error: {exc}") from exc
            for task in pending:
                if not task.done():
                    task.cancel()
        except OSError as exc:
            raise DaemonStartupError(f"failed to bind Unix-socket {socket_path}: {exc}") from exc
    finally:
        await _shutdown(ctx)
        # Best-effort cleanup of the socket file. A single non-retried unlink on
        # the shutdown path; the harness commits to asyncio (not trio/anyio) per
        # Target Stack Commitment §5.1, so the ASYNC240 trio.Path/anyio.path
        # remedy is out-of-stack and the blocking cost here is negligible.
        try:
            socket_path.unlink(missing_ok=True)  # noqa: ASYNC240
        except OSError:
            pass


@app.command("daemon")
def daemon_command(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Override default harness.toml config path"),
    ] = None,
    socket_path: Annotated[
        Path | None,
        typer.Option("--socket-path", help="Unix-socket path for the daemon"),
    ] = None,
) -> None:
    """Start the harness daemon (FastMCP server, Unix-socket transport).

    Per runtime spec v1.35 §14.18.1 + Q-K=(c) Unix-socket transport. Bootstraps
    the harness, binds the FastMCP server's streamable-HTTP app to a Unix
    domain socket, and serves until SIGINT/SIGTERM triggers drain. Reuses the
    existing U-RT-62 `run_workflow` MCP tool (PRESERVED VERBATIM at wire-level
    signature; handler-internal discriminator added per the U-RT-107 Class 1
    fork Reading (A) ratification).
    """
    # --- Stage 1: config load (RT-FAIL-CLI-CONFIG-LOAD → exit 3) -----------
    try:
        runtime_config = RuntimeConfigSource.load(config_file=config)
    except RuntimeConfigLoadError as exc:
        _print_fail_class(exc.FAIL_CLASS, exc.reason)
        raise typer.Exit(code=EXIT_CONFIG_ERROR) from exc

    resolved_socket = socket_path if socket_path is not None else _default_daemon_socket_path()

    # --- Stage 2: daemon serve (RT-FAIL-CLI-DAEMON-CONNECTION → exit 4) ----
    try:
        asyncio.run(_daemon_main(runtime_config=runtime_config, socket_path=resolved_socket))
    except DaemonStartupError as exc:
        _print_fail_class(exc.FAIL_CLASS, str(exc))
        raise typer.Exit(code=EXIT_BOOTSTRAP_ERROR) from exc

    raise typer.Exit(code=EXIT_SUCCESS)


# --- Track A admin subcommand registration (PR #84 Reading A apply) --------
#
# Spec §13.4 + §14.18.1 declare 5-subcommand parent dispatcher; standalone
# `harness-inspect` + `harness-shutdown` binaries at [project.scripts] are
# PRESERVED VERBATIM (operator muscle memory). The 2 subcommands here are
# pass-through wrappers — they forward all extra args (including --help) to
# the admin modules' argparse-based `main(argv)` entrypoints unchanged.
#
# Discipline preserved: admin modules stay argparse-only per spec §13
# "no click/typer" footer at their docstrings. The parent-app wrappers
# are typer-typed (consistent with run + daemon siblings) but delegate
# without translating flag shapes.


@app.command(
    "inspect",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        # Disable typer's auto-help so --help flows through to the admin
        # module's argparse parser (which shows the real flag inventory).
        "help_option_names": [],
    },
    help="Read-only summary of state ledger + collector traces.",
    short_help="Read-only state inspection (delegates to harness-inspect).",
)
def inspect_command(ctx: typer.Context) -> None:
    """Pass-through wrapper for the Track A `harness-inspect` admin stub.

    Forwards all extra args verbatim to
    `harness_runtime.admin.inspect:main`. See `harness inspect --help` (which
    routes via the admin module's argparse parser) for the flag inventory.
    """
    from harness_runtime.admin import inspect as _inspect_admin

    raise typer.Exit(code=_inspect_admin.main(ctx.args))


@app.command(
    "shutdown",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    },
    help="Signal a running harness instance to shut down gracefully.",
    short_help="Daemon shutdown (delegates to harness-shutdown).",
)
def shutdown_command(ctx: typer.Context) -> None:
    """Pass-through wrapper for the Track A `harness-shutdown` admin stub.

    Forwards all extra args verbatim to
    `harness_runtime.admin.shutdown_cli:main`. See `harness shutdown --help`
    (which routes via the admin module's argparse parser) for the flag
    inventory.
    """
    from harness_runtime.admin import shutdown_cli as _shutdown_admin

    raise typer.Exit(code=_shutdown_admin.main(ctx.args))


# Click UsageError exits with code 2 by default. Per runtime spec v1.35
# §14.18.4 + §14.18.2, CLI arg-parse failures map to RT-FAIL-CLI-ARG-INVALID
# → exit code 3. We discriminate UsageError (arg-parse) from legitimate
# command-body exits (e.g. manifest-error → exit 2) by running Click in
# ``standalone_mode=False`` so each exception type surfaces directly.
_ARG_INVALID_EXIT_CODE = 3
_ARG_INVALID_FAIL_CLASS = "RT-FAIL-CLI-ARG-INVALID"


def main() -> None:
    """Top-level entrypoint mapped at ``[project.scripts] harness``."""
    # Typer vendors Click internally as ``typer._click``; the exception
    # classes raised at parse / dispatch time are the vendored ones, NOT
    # ``click.exceptions.*`` from the top-level click package. Catch the
    # vendored variants so arg-parse errors (UsageError / NoSuchOption /
    # MissingParameter / ...) route through the RT-FAIL-CLI-ARG-INVALID
    # remap consistently.
    from typer._click.exceptions import (
        ClickException as _ClickException,
    )
    from typer._click.exceptions import (
        Exit as _Exit,
    )
    from typer._click.exceptions import (
        UsageError as _UsageError,
    )

    try:
        app(standalone_mode=False)
    except _UsageError as exc:
        # Click's default formatter writes "Usage: ..." + "Error: ..." to
        # stderr. Mirror that, then append the fail-class line per spec.
        exc.show()
        print(_ARG_INVALID_FAIL_CLASS, file=sys.stderr)
        raise SystemExit(_ARG_INVALID_EXIT_CODE) from exc
    except _Exit as exc:
        # typer.Exit(code=N) → propagates as Click Exit; preserve N verbatim.
        raise SystemExit(exc.exit_code) from exc
    except _ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
