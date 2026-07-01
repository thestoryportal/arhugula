"""Tests for subscription-backed external CLI provider adapters (R-CLI-1)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from harness_runtime.lifecycle.external_cli_provider import (
    CLIProcessResult,
    ExternalCLICommandError,
    ExternalCLINotAuthenticatedError,
    ExternalCLIProcessTimeout,
    RecordingSubprocessRunner,
    construct_claude_code_cli_adapter,
)
from harness_runtime.types import ExternalCLIProviderConfig


@dataclass
class _FakeRunner:
    results: list[CLIProcessResult]
    calls: list[tuple[tuple[str, ...], str, float]]

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: str,
        timeout_seconds: float,
    ) -> CLIProcessResult:
        self.calls.append((argv, stdin, timeout_seconds))
        return self.results.pop(0)


def _config(**overrides: object) -> ExternalCLIProviderConfig:
    return ExternalCLIProviderConfig(
        provider="claude_code",
        kind="claude-code",
        command="claude",
        timeout_seconds=42.0,
        **overrides,
    )


@pytest.mark.asyncio
async def test_construct_claude_adapter_checks_auth_without_token_access() -> None:
    runner = _FakeRunner(
        results=[CLIProcessResult(exit_code=0, stdout='{"loggedIn": true}', stderr="")],
        calls=[],
    )

    adapter = await construct_claude_code_cli_adapter(_config(), runner=runner)

    assert adapter.provider_name == "claude_code"
    assert runner.calls == [
        (("claude", "auth", "status", "--json"), "", 42.0),
    ]


@pytest.mark.asyncio
async def test_construct_claude_adapter_rejects_unauthenticated_cli() -> None:
    runner = _FakeRunner(
        results=[CLIProcessResult(exit_code=0, stdout='{"loggedIn": false}', stderr="")],
        calls=[],
    )

    with pytest.raises(ExternalCLINotAuthenticatedError):
        await construct_claude_code_cli_adapter(_config(), runner=runner)


@pytest.mark.asyncio
async def test_claude_dispatch_uses_argv_and_stdin_for_text_only_prompt() -> None:
    runner = _FakeRunner(
        results=[
            CLIProcessResult(exit_code=0, stdout='{"loggedIn": true}', stderr=""),
            CLIProcessResult(exit_code=0, stdout='{"result": "OK"}', stderr=""),
        ],
        calls=[],
    )
    adapter = await construct_claude_code_cli_adapter(_config(), runner=runner)

    result = await adapter.dispatch_text(model="sonnet", prompt="Reply OK")

    assert result.text == "OK"
    argv, stdin, timeout = runner.calls[1]
    assert argv == (
        "claude",
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
        "sonnet",
    )
    assert stdin == "Reply OK"
    assert timeout == 42.0


@pytest.mark.asyncio
async def test_claude_dispatch_surfaces_nonzero_exit_with_stderr() -> None:
    runner = _FakeRunner(
        results=[
            CLIProcessResult(exit_code=0, stdout='{"loggedIn": true}', stderr=""),
            CLIProcessResult(exit_code=2, stdout="", stderr="boom"),
        ],
        calls=[],
    )
    adapter = await construct_claude_code_cli_adapter(_config(), runner=runner)

    with pytest.raises(ExternalCLICommandError, match="boom"):
        await adapter.dispatch_text(model="sonnet", prompt="Reply OK")


@pytest.mark.asyncio
async def test_recording_runner_never_accepts_shell_execution() -> None:
    runner = RecordingSubprocessRunner(
        [CLIProcessResult(exit_code=0, stdout='{"loggedIn": true}', stderr="")]
    )

    await runner.run(("claude", "auth", "status", "--json"), stdin="", timeout_seconds=1.0)

    assert runner.calls == [(("claude", "auth", "status", "--json"), "", 1.0)]


@pytest.mark.asyncio
async def test_claude_dispatch_timeout_is_typed() -> None:
    class _TimeoutRunner:
        async def run(
            self,
            argv: tuple[str, ...],
            *,
            stdin: str,
            timeout_seconds: float,
        ) -> CLIProcessResult:
            _ = argv, stdin, timeout_seconds
            raise ExternalCLIProcessTimeout("claude", 1.0)

    adapter = await construct_claude_code_cli_adapter(
        _config(auth_check=False),
        runner=_TimeoutRunner(),
    )

    with pytest.raises(ExternalCLIProcessTimeout):
        await adapter.dispatch_text(model="sonnet", prompt="Reply OK")
