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

from harness_runtime.types import (
    ExternalCLIPromptTransport,
    ExternalCLIProviderConfig,
    ExternalCLIProviderKind,
    ExternalCLIResponseFormat,
)

__all__ = [
    "AntigravityCLIAdapter",
    "AsyncioSubprocessRunner",
    "CLIProcessResult",
    "ClaudeCodeCLIAdapter",
    "CodexCLIAdapter",
    "ExternalCLICommandError",
    "ExternalCLINotAuthenticatedError",
    "ExternalCLIOutputError",
    "ExternalCLIProcessTimeout",
    "ExternalCLIProviderError",
    "ExternalCLISubprocessRunner",
    "ExternalCLITextResult",
    "GeminiCLIAdapter",
    "GenericCommandCLIAdapter",
    "RecordingSubprocessRunner",
    "construct_antigravity_cli_adapter",
    "construct_claude_code_cli_adapter",
    "construct_codex_cli_adapter",
    "construct_external_cli_adapter",
    "construct_gemini_cli_adapter",
    "construct_generic_command_cli_adapter",
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


@dataclass(slots=True)
class CodexCLIAdapter:
    provider_name: str
    command: str
    timeout_seconds: float
    runner: ExternalCLISubprocessRunner
    kind: str = "codex"
    _closed: bool = False

    async def aclose(self) -> None:
        self._closed = True

    async def dispatch_text(self, *, model: str, prompt: str) -> ExternalCLITextResult:
        result = await self.runner.run(
            _codex_inference_argv(self.command, model),
            stdin=prompt,
            timeout_seconds=self.timeout_seconds,
        )
        _raise_for_nonzero(self.command, result)
        events = _parse_json_lines(result.stdout, "Codex inference response")
        text = _extract_jsonl_text_result(events, "Codex inference response")
        return ExternalCLITextResult(
            text=text,
            exit_code=result.exit_code,
            raw_response={"events": events},
        )


@dataclass(slots=True)
class AntigravityCLIAdapter:
    provider_name: str
    command: str
    timeout_seconds: float
    runner: ExternalCLISubprocessRunner
    kind: str = "antigravity"
    _closed: bool = False

    async def aclose(self) -> None:
        self._closed = True

    async def dispatch_text(self, *, model: str, prompt: str) -> ExternalCLITextResult:
        result = await self.runner.run(
            _antigravity_inference_argv(
                self.command,
                model,
                prompt,
                timeout_seconds=self.timeout_seconds,
            ),
            stdin="",
            timeout_seconds=self.timeout_seconds,
        )
        _raise_for_nonzero(self.command, result)
        text, raw_response = _parse_response_by_format(
            result.stdout,
            ExternalCLIResponseFormat.TEXT,
            "Antigravity inference response",
        )
        return ExternalCLITextResult(
            text=text,
            exit_code=result.exit_code,
            raw_response=raw_response,
        )


@dataclass(slots=True)
class GeminiCLIAdapter:
    provider_name: str
    command: str
    timeout_seconds: float
    runner: ExternalCLISubprocessRunner
    kind: str = "gemini"
    _closed: bool = False

    async def aclose(self) -> None:
        self._closed = True

    async def dispatch_text(self, *, model: str, prompt: str) -> ExternalCLITextResult:
        result = await self.runner.run(
            _gemini_inference_argv(self.command, model, prompt),
            stdin="",
            timeout_seconds=self.timeout_seconds,
        )
        _raise_for_nonzero(self.command, result)
        text, raw_response = _parse_response_by_format(
            result.stdout,
            ExternalCLIResponseFormat.TEXT,
            "Gemini inference response",
        )
        return ExternalCLITextResult(
            text=text,
            exit_code=result.exit_code,
            raw_response=raw_response,
        )


@dataclass(slots=True)
class GenericCommandCLIAdapter:
    provider_name: str
    command: str
    args: tuple[str, ...]
    response_format: ExternalCLIResponseFormat
    prompt_transport: ExternalCLIPromptTransport
    timeout_seconds: float
    runner: ExternalCLISubprocessRunner
    kind: str = "generic-command"
    _closed: bool = False

    async def aclose(self) -> None:
        self._closed = True

    async def dispatch_text(self, *, model: str, prompt: str) -> ExternalCLITextResult:
        prompt_in_argv = self.prompt_transport is ExternalCLIPromptTransport.ARG
        argv = (
            self.command,
            *_render_argv_templates(
                self.args,
                model=model,
                prompt=prompt,
                prompt_in_argv=prompt_in_argv,
            ),
        )
        result = await self.runner.run(
            argv,
            stdin="" if prompt_in_argv else prompt,
            timeout_seconds=self.timeout_seconds,
        )
        _raise_for_nonzero(self.command, result)
        text, raw_response = _parse_response_by_format(
            result.stdout,
            self.response_format,
            "generic external CLI inference response",
        )
        return ExternalCLITextResult(
            text=text,
            exit_code=result.exit_code,
            raw_response=raw_response,
        )


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


def _codex_auth_argv(command: str) -> tuple[str, ...]:
    return (command, "login", "status")


def _codex_inference_argv(command: str, model: str) -> tuple[str, ...]:
    return (
        command,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-m",
        model,
        "-",
    )


def _antigravity_auth_argv(command: str) -> tuple[str, ...]:
    return (command, "models")


def _antigravity_inference_argv(
    command: str,
    model: str,
    prompt: str,
    *,
    timeout_seconds: float,
) -> tuple[str, ...]:
    return (
        command,
        "--print",
        prompt,
        "--model",
        model,
        "--print-timeout",
        _go_seconds_duration(timeout_seconds),
        "--sandbox",
    )


def _gemini_inference_argv(command: str, model: str, prompt: str) -> tuple[str, ...]:
    return (
        command,
        "--skip-trust",
        "-m",
        model,
        "-p",
        prompt,
    )


def _go_seconds_duration(seconds: float) -> str:
    return f"{seconds:.3g}s"


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


def _parse_json_lines(raw: str, label: str) -> tuple[Mapping[str, Any], ...]:
    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ExternalCLIOutputError(
                f"{label} line {line_number} was not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise ExternalCLIOutputError(f"{label} line {line_number} was not a JSON object")
        events.append(dict(cast(Mapping[str, Any], parsed)))
    if not events:
        raise ExternalCLIOutputError(f"{label} did not contain JSON events")
    return tuple(events)


def _extract_text_result(payload: Mapping[str, Any]) -> str:
    for key in ("result", "text", "response"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    raise ExternalCLIOutputError("external CLI JSON response did not contain a text result")


def _extract_jsonl_text_result(events: Sequence[Mapping[str, Any]], label: str) -> str:
    for event in reversed(events):
        event_type = event.get("type")
        if event_type == "agent_message":
            text = event.get("text")
            if isinstance(text, str):
                return text
        item = event.get("item")
        if isinstance(item, Mapping):
            item_payload = cast(Mapping[str, Any], item)
            text = item_payload.get("text")
            if item_payload.get("type") != "agent_message":
                text = None
            if isinstance(text, str):
                return text
        text = event.get("text")
        if isinstance(text, str) and event_type in {"message", "response"}:
            return text
    raise ExternalCLIOutputError(f"{label} did not contain an agent text result")


def _render_argv_templates(
    args: Sequence[str],
    *,
    model: str,
    prompt: str,
    prompt_in_argv: bool,
) -> tuple[str, ...]:
    rendered: list[str] = []
    for arg in args:
        if "{prompt}" in arg and not prompt_in_argv:
            raise ExternalCLIOutputError(
                "{prompt} template requires prompt_transport = \"arg\""
            )
        rendered.append(arg.replace("{model}", model).replace("{prompt}", prompt))
    return tuple(rendered)


def _parse_response_by_format(
    raw: str,
    response_format: ExternalCLIResponseFormat,
    label: str,
) -> tuple[str, Mapping[str, Any]]:
    if response_format is ExternalCLIResponseFormat.TEXT:
        text = raw.strip()
        if not text:
            raise ExternalCLIOutputError(f"{label} was empty")
        return text, {"text": text}
    if response_format is ExternalCLIResponseFormat.JSON:
        payload = _parse_json_object(raw, label)
        return _extract_text_result(payload), payload
    if response_format is ExternalCLIResponseFormat.JSONL:
        events = _parse_json_lines(raw, label)
        return _extract_jsonl_text_result(events, label), {"events": events}
    raise ExternalCLIOutputError(f"unsupported external CLI response format {response_format!r}")


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


async def _assert_codex_authenticated(
    config: ExternalCLIProviderConfig,
    runner: ExternalCLISubprocessRunner,
) -> None:
    result = await runner.run(
        _codex_auth_argv(config.command),
        stdin="",
        timeout_seconds=config.timeout_seconds,
    )
    if result.exit_code != 0:
        raise ExternalCLINotAuthenticatedError(
            config.provider,
            result.stderr.strip() or result.stdout.strip() or f"exit={result.exit_code}",
        )
    output = f"{result.stdout}\n{result.stderr}".lower()
    if "not logged" in output or "logged out" in output or "not authenticated" in output:
        raise ExternalCLINotAuthenticatedError(config.provider, output.strip())
    if "logged in" not in output and "authenticated" not in output:
        raise ExternalCLINotAuthenticatedError(
            config.provider,
            "could not confirm Codex login status",
        )


async def _assert_antigravity_authenticated(
    config: ExternalCLIProviderConfig,
    runner: ExternalCLISubprocessRunner,
) -> None:
    result = await runner.run(
        _antigravity_auth_argv(config.command),
        stdin="",
        timeout_seconds=config.timeout_seconds,
    )
    if result.exit_code != 0:
        raise ExternalCLINotAuthenticatedError(
            config.provider,
            result.stderr.strip() or result.stdout.strip() or f"exit={result.exit_code}",
        )
    if not result.stdout.strip():
        raise ExternalCLINotAuthenticatedError(
            config.provider,
            "could not confirm Antigravity models/auth status",
        )


async def _assert_configured_auth_command_succeeds(
    config: ExternalCLIProviderConfig,
    runner: ExternalCLISubprocessRunner,
    *,
    provider_label: str,
) -> None:
    if not config.auth_args:
        raise ExternalCLINotAuthenticatedError(
            config.provider,
            f"{provider_label} auth_check=true requires auth_args",
        )
    result = await runner.run(
        (config.command, *config.auth_args),
        stdin="",
        timeout_seconds=config.timeout_seconds,
    )
    if result.exit_code != 0:
        raise ExternalCLINotAuthenticatedError(
            config.provider,
            result.stderr.strip() or result.stdout.strip() or f"exit={result.exit_code}",
        )


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


async def construct_codex_cli_adapter(
    config: ExternalCLIProviderConfig,
    *,
    runner: ExternalCLISubprocessRunner | None = None,
) -> CodexCLIAdapter:
    if config.kind is not ExternalCLIProviderKind.CODEX:
        raise ValueError(f"unsupported Codex adapter kind: {config.kind}")
    process_runner = runner if runner is not None else AsyncioSubprocessRunner()
    if config.auth_check:
        await _assert_codex_authenticated(config, process_runner)
    return CodexCLIAdapter(
        provider_name=config.provider,
        command=config.command,
        timeout_seconds=config.timeout_seconds,
        runner=process_runner,
    )


async def construct_antigravity_cli_adapter(
    config: ExternalCLIProviderConfig,
    *,
    runner: ExternalCLISubprocessRunner | None = None,
) -> AntigravityCLIAdapter:
    if config.kind is not ExternalCLIProviderKind.ANTIGRAVITY:
        raise ValueError(f"unsupported Antigravity adapter kind: {config.kind}")
    process_runner = runner if runner is not None else AsyncioSubprocessRunner()
    if config.auth_check:
        await _assert_antigravity_authenticated(config, process_runner)
    return AntigravityCLIAdapter(
        provider_name=config.provider,
        command=config.command,
        timeout_seconds=config.timeout_seconds,
        runner=process_runner,
    )


async def construct_gemini_cli_adapter(
    config: ExternalCLIProviderConfig,
    *,
    runner: ExternalCLISubprocessRunner | None = None,
) -> GeminiCLIAdapter:
    if config.kind is not ExternalCLIProviderKind.GEMINI:
        raise ValueError(f"unsupported Gemini adapter kind: {config.kind}")
    process_runner = runner if runner is not None else AsyncioSubprocessRunner()
    if config.auth_check:
        await _assert_configured_auth_command_succeeds(
            config,
            process_runner,
            provider_label="Gemini CLI",
        )
    return GeminiCLIAdapter(
        provider_name=config.provider,
        command=config.command,
        timeout_seconds=config.timeout_seconds,
        runner=process_runner,
    )


async def construct_generic_command_cli_adapter(
    config: ExternalCLIProviderConfig,
    *,
    runner: ExternalCLISubprocessRunner | None = None,
) -> GenericCommandCLIAdapter:
    if config.kind is not ExternalCLIProviderKind.GENERIC_COMMAND:
        raise ValueError(f"unsupported generic command adapter kind: {config.kind}")
    process_runner = runner if runner is not None else AsyncioSubprocessRunner()
    if config.auth_check:
        await _assert_configured_auth_command_succeeds(
            config,
            process_runner,
            provider_label="generic external CLI",
        )
    return GenericCommandCLIAdapter(
        provider_name=config.provider,
        command=config.command,
        args=config.args,
        response_format=config.response_format,
        prompt_transport=config.prompt_transport,
        timeout_seconds=config.timeout_seconds,
        runner=process_runner,
    )


async def construct_external_cli_adapter(
    config: ExternalCLIProviderConfig,
    *,
    runner: ExternalCLISubprocessRunner | None = None,
) -> (
    ClaudeCodeCLIAdapter
    | CodexCLIAdapter
    | AntigravityCLIAdapter
    | GeminiCLIAdapter
    | GenericCommandCLIAdapter
):
    if config.kind is ExternalCLIProviderKind.CLAUDE_CODE:
        return await construct_claude_code_cli_adapter(config, runner=runner)
    if config.kind is ExternalCLIProviderKind.CODEX:
        return await construct_codex_cli_adapter(config, runner=runner)
    if config.kind is ExternalCLIProviderKind.ANTIGRAVITY:
        return await construct_antigravity_cli_adapter(config, runner=runner)
    if config.kind is ExternalCLIProviderKind.GEMINI:
        return await construct_gemini_cli_adapter(config, runner=runner)
    if config.kind is ExternalCLIProviderKind.GENERIC_COMMAND:
        return await construct_generic_command_cli_adapter(config, runner=runner)
    raise ValueError(f"unsupported external CLI provider kind: {config.kind}")
