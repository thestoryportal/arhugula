"""External local CLI provider adapters for subscription-backed inference.

R-CLI-1 deliberately uses the official local CLI as the auth boundary. The
runtime passes text over stdin and reads JSON over stdout; it never reads or
stores OAuth/session tokens and never invokes a shell.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from harness_runtime.types import ExternalCLIProviderConfig, ExternalCLIProviderKind

__all__ = [
    "AsyncioSubprocessRunner",
    "CLIProcessResult",
    "ClaudeCodeCLIAdapter",
    "ExternalCLICommandError",
    "ExternalCLINotAuthenticatedError",
    "ExternalCLIOutputError",
    "ExternalCLIProcessTimeout",
    "ExternalCLIProviderError",
    "ExternalCLISubprocessRunner",
    "ExternalCLITextResult",
    "RecordingSubprocessRunner",
    "construct_claude_code_cli_adapter",
    "construct_external_cli_adapter",
]


@dataclass(frozen=True, slots=True)
class CLIProcessResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class ExternalCLITextResult:
    text: str
    exit_code: int
    raw_response: Mapping[str, Any]


class ExternalCLIProviderError(Exception):
    """Base class for external CLI provider failures."""


class ExternalCLIProcessTimeout(ExternalCLIProviderError):  # noqa: N818
    """Raised when the CLI process exceeds its configured timeout."""

    def __init__(self, command: str, timeout_seconds: float) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"external CLI command {command!r} timed out after {timeout_seconds:.3g}s"
        )


class ExternalCLICommandError(ExternalCLIProviderError):
    """Raised when the CLI process exits nonzero or cannot be spawned."""

    def __init__(
        self,
        command: str,
        exit_code: int,
        stderr: str,
        *,
        stdout: str = "",
    ) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        detail = stderr.strip() or stdout.strip() or "no stderr/stdout"
        super().__init__(
            f"external CLI command {command!r} exited {exit_code}: {detail}"
        )


class ExternalCLIOutputError(ExternalCLIProviderError):
    """Raised when the CLI exits successfully but returns an unexpected payload."""


class ExternalCLINotAuthenticatedError(ExternalCLIProviderError):
    """Raised when the official CLI reports no authenticated local session."""

    def __init__(self, provider: str, detail: str) -> None:
        self.provider = provider
        self.detail = detail
        super().__init__(f"external CLI provider {provider!r} is not authenticated: {detail}")


class ExternalCLISubprocessRunner(Protocol):
    """Subprocess boundary for external CLI calls.

    The shape intentionally has no shell parameter; production uses argv-only
    `create_subprocess_exec`, and tests inject deterministic fakes.
    """

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: str,
        timeout_seconds: float,
    ) -> CLIProcessResult: ...


class AsyncioSubprocessRunner:
    """Production subprocess runner using argv-only execution."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: str,
        timeout_seconds: float,
    ) -> CLIProcessResult:
        if not argv:
            raise ExternalCLICommandError("", 127, "empty argv")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ExternalCLICommandError(argv[0], 127, str(exc)) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(stdin.encode("utf-8")),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ExternalCLIProcessTimeout(argv[0], timeout_seconds) from exc

        return CLIProcessResult(
            exit_code=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )


class RecordingSubprocessRunner:
    """Fake runner for tests that records argv/stdin/timeout calls."""

    def __init__(self, results: Sequence[CLIProcessResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[tuple[str, ...], str, float]] = []

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: str,
        timeout_seconds: float,
    ) -> CLIProcessResult:
        self.calls.append((argv, stdin, timeout_seconds))
        if not self._results:
            raise AssertionError("RecordingSubprocessRunner has no remaining results")
        return self._results.pop(0)


@dataclass(slots=True)
class ClaudeCodeCLIAdapter:
    provider_name: str
    command: str
    timeout_seconds: float
    runner: ExternalCLISubprocessRunner
    kind: str = "claude-code"
    _closed: bool = False

    async def aclose(self) -> None:
        self._closed = True

    async def dispatch_text(self, *, model: str, prompt: str) -> ExternalCLITextResult:
        result = await self.runner.run(
            _claude_inference_argv(self.command, model),
            stdin=prompt,
            timeout_seconds=self.timeout_seconds,
        )
        _raise_for_nonzero(self.command, result)
        payload = _parse_json_object(result.stdout, "Claude Code inference response")
        text = _extract_text_result(payload)
        return ExternalCLITextResult(text=text, exit_code=result.exit_code, raw_response=payload)


def _claude_auth_argv(command: str) -> tuple[str, ...]:
    return (command, "auth", "status", "--json")


def _claude_inference_argv(command: str, model: str) -> tuple[str, ...]:
    return (
        command,
        "--print",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--model",
        model,
    )


def _raise_for_nonzero(command: str, result: CLIProcessResult) -> None:
    if result.exit_code != 0:
        raise ExternalCLICommandError(
            command,
            result.exit_code,
            result.stderr,
            stdout=result.stdout,
        )


def _parse_json_object(raw: str, label: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExternalCLIOutputError(f"{label} was not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ExternalCLIOutputError(f"{label} was not a JSON object")
    return dict(cast(Mapping[str, Any], parsed))


def _extract_text_result(payload: Mapping[str, Any]) -> str:
    for key in ("result", "text", "response"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    raise ExternalCLIOutputError("Claude Code JSON response did not contain a text result")


async def _assert_claude_authenticated(
    config: ExternalCLIProviderConfig,
    runner: ExternalCLISubprocessRunner,
) -> None:
    result = await runner.run(
        _claude_auth_argv(config.command),
        stdin="",
        timeout_seconds=config.timeout_seconds,
    )
    if result.exit_code != 0:
        raise ExternalCLINotAuthenticatedError(
            config.provider,
            result.stderr.strip() or result.stdout.strip() or f"exit={result.exit_code}",
        )
    payload = _parse_json_object(result.stdout, "Claude Code auth status response")
    if payload.get("loggedIn") is not True:
        raise ExternalCLINotAuthenticatedError(config.provider, "loggedIn=false")


async def construct_claude_code_cli_adapter(
    config: ExternalCLIProviderConfig,
    *,
    runner: ExternalCLISubprocessRunner | None = None,
) -> ClaudeCodeCLIAdapter:
    if config.kind is not ExternalCLIProviderKind.CLAUDE_CODE:
        raise ValueError(f"unsupported Claude Code adapter kind: {config.kind}")
    process_runner = runner if runner is not None else AsyncioSubprocessRunner()
    if config.auth_check:
        await _assert_claude_authenticated(config, process_runner)
    return ClaudeCodeCLIAdapter(
        provider_name=config.provider,
        command=config.command,
        timeout_seconds=config.timeout_seconds,
        runner=process_runner,
    )


async def construct_external_cli_adapter(
    config: ExternalCLIProviderConfig,
    *,
    runner: ExternalCLISubprocessRunner | None = None,
) -> ClaudeCodeCLIAdapter:
    if config.kind is ExternalCLIProviderKind.CLAUDE_CODE:
        return await construct_claude_code_cli_adapter(config, runner=runner)
    raise ValueError(f"unsupported external CLI provider kind: {config.kind}")
