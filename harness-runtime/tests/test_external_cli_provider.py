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
    construct_antigravity_cli_adapter,
    construct_claude_code_cli_adapter,
    construct_codex_cli_adapter,
    construct_gemini_cli_adapter,
    construct_generic_command_cli_adapter,
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


def _provider_config(
    provider: str,
    kind: str,
    command: str,
    **overrides: object,
) -> ExternalCLIProviderConfig:
    return ExternalCLIProviderConfig(
        provider=provider,
        kind=kind,
        command=command,
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


@pytest.mark.asyncio
async def test_construct_codex_adapter_checks_login_status_without_token_access() -> None:
    runner = _FakeRunner(
        results=[CLIProcessResult(exit_code=0, stdout="Logged in using ChatGPT\n", stderr="")],
        calls=[],
    )

    adapter = await construct_codex_cli_adapter(
        _provider_config("codex", "codex", "codex"),
        runner=runner,
    )

    assert adapter.provider_name == "codex"
    assert runner.calls == [(("codex", "login", "status"), "", 42.0)]


@pytest.mark.asyncio
async def test_codex_dispatch_uses_ephemeral_jsonl_stdin_and_extracts_agent_message() -> None:
    runner = _FakeRunner(
        results=[
            CLIProcessResult(exit_code=0, stdout="Logged in using ChatGPT\n", stderr=""),
            CLIProcessResult(
                exit_code=0,
                stdout=(
                    '{"type":"thread.started","thread_id":"t"}\n'
                    '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":1}}\n'
                ),
                stderr="",
            ),
        ],
        calls=[],
    )
    adapter = await construct_codex_cli_adapter(
        _provider_config("codex", "codex", "codex"),
        runner=runner,
    )

    result = await adapter.dispatch_text(model="gpt-5", prompt="Reply OK")

    assert result.text == "OK"
    argv, stdin, timeout = runner.calls[1]
    assert argv == (
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-m",
        "gpt-5",
        "-",
    )
    assert stdin == "Reply OK"
    assert timeout == 42.0


@pytest.mark.asyncio
async def test_construct_antigravity_adapter_checks_models_without_token_access() -> None:
    runner = _FakeRunner(
        results=[
            CLIProcessResult(
                exit_code=0,
                stdout="Gemini 3.5 Flash (Low)\nClaude Sonnet 4.6 (Thinking)\n",
                stderr="",
            )
        ],
        calls=[],
    )

    adapter = await construct_antigravity_cli_adapter(
        _provider_config("antigravity", "antigravity", "agy"),
        runner=runner,
    )

    assert adapter.provider_name == "antigravity"
    assert runner.calls == [(("agy", "models"), "", 42.0)]


@pytest.mark.asyncio
async def test_antigravity_dispatch_uses_print_mode_and_extracts_stdout() -> None:
    runner = _FakeRunner(
        results=[
            CLIProcessResult(exit_code=0, stdout="Gemini 3.5 Flash (Low)\n", stderr=""),
            CLIProcessResult(exit_code=0, stdout="OK\n", stderr=""),
        ],
        calls=[],
    )
    adapter = await construct_antigravity_cli_adapter(
        _provider_config("antigravity", "antigravity", "agy"),
        runner=runner,
    )

    result = await adapter.dispatch_text(
        model="Gemini 3.5 Flash (Low)",
        prompt="Reply OK",
    )

    assert result.text == "OK"
    argv, stdin, timeout = runner.calls[1]
    assert argv == (
        "agy",
        "--print",
        "Reply OK",
        "--model",
        "Gemini 3.5 Flash (Low)",
        "--print-timeout",
        "42s",
        "--sandbox",
    )
    assert stdin == ""
    assert timeout == 42.0


@pytest.mark.asyncio
async def test_gemini_dispatch_uses_headless_text_prompt_skip_trust_and_extracts_stdout() -> None:
    runner = _FakeRunner(
        results=[CLIProcessResult(exit_code=0, stdout="OK\n", stderr="")],
        calls=[],
    )
    adapter = await construct_gemini_cli_adapter(
        _provider_config("gemini", "gemini", "gemini", auth_check=False),
        runner=runner,
    )

    result = await adapter.dispatch_text(model="gemini-2.5-flash", prompt="Reply OK")

    assert result.text == "OK"
    assert runner.calls == [
        (
            (
                "gemini",
                "--skip-trust",
                "-m",
                "gemini-2.5-flash",
                "-p",
                "Reply OK",
            ),
            "",
            42.0,
        )
    ]


@pytest.mark.asyncio
async def test_generic_command_adapter_uses_configured_templates_and_stdin() -> None:
    runner = _FakeRunner(
        results=[
            CLIProcessResult(exit_code=0, stdout="authenticated\n", stderr=""),
            CLIProcessResult(exit_code=0, stdout='{"response": "OK"}', stderr=""),
        ],
        calls=[],
    )
    adapter = await construct_generic_command_cli_adapter(
        _provider_config(
            "local_llm",
            "generic-command",
            "my-llm",
            args=("--model", "{model}", "--json"),
            auth_args=("auth", "status"),
            response_format="json",
        ),
        runner=runner,
    )

    result = await adapter.dispatch_text(model="demo-model", prompt="Reply OK")

    assert result.text == "OK"
    assert runner.calls == [
        (("my-llm", "auth", "status"), "", 42.0),
        (("my-llm", "--model", "demo-model", "--json"), "Reply OK", 42.0),
    ]
