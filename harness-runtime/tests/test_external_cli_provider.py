"""Tests for subscription-backed external CLI provider adapters (R-CLI-1)."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from harness_runtime.lifecycle import external_cli_provider
from harness_runtime.lifecycle.external_cli_provider import (
    AsyncioSubprocessRunner,
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


_READY_THEN_SLEEP = (
    "import os, sys, time\n"
    "ready = sys.argv[1]\n"
    "with open(ready + '.tmp', 'w') as fh:\n"
    "    fh.write(str(os.getpid()))\n"
    "os.replace(ready + '.tmp', ready)\n"
    "time.sleep(60)\n"
)


class _SpawnRecorder:
    """Wraps asyncio.create_subprocess_exec to expose the spawned process."""

    def __init__(self, *, limit: int | None = None) -> None:
        self.processes: list[asyncio.subprocess.Process] = []
        self._real = asyncio.create_subprocess_exec
        self._limit = limit

    async def __call__(self, *args: object, **kwargs: object) -> asyncio.subprocess.Process:
        if self._limit is not None:
            # A small StreamReader limit makes the reader pause as soon as
            # unconsumed output piles up, so buffer pressure is deterministic.
            kwargs["limit"] = self._limit
        process = await self._real(*args, **kwargs)  # type: ignore[arg-type]
        self.processes.append(process)
        return process


async def _reap_leftovers(recorder: _SpawnRecorder) -> None:
    for process in recorder.processes:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), timeout=10.0)


async def _wait_for_ready(ready: Path, task: asyncio.Task[CLIProcessResult]) -> None:
    deadline = time.monotonic() + 10.0
    while not ready.exists():
        if task.done():
            task.result()
            raise AssertionError("runner finished before child signalled readiness")
        if time.monotonic() > deadline:
            raise AssertionError("child never signalled readiness")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_asyncio_runner_cancellation_reaps_direct_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _SpawnRecorder()
    monkeypatch.setattr(external_cli_provider.asyncio, "create_subprocess_exec", recorder)
    ready = tmp_path / "ready"
    task = asyncio.ensure_future(
        AsyncioSubprocessRunner().run(
            (sys.executable, "-c", _READY_THEN_SLEEP, str(ready)),
            stdin="prompt",
            timeout_seconds=60.0,
        )
    )
    try:
        await _wait_for_ready(ready, task)
        assert len(recorder.processes) == 1
        process = recorder.processes[0]
        assert int(ready.read_text()) == process.pid
        assert process.returncode is None

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()
        assert process.returncode is not None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await _reap_leftovers(recorder)


@pytest.mark.asyncio
async def test_asyncio_runner_timeout_is_typed_and_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _SpawnRecorder()
    monkeypatch.setattr(external_cli_provider.asyncio, "create_subprocess_exec", recorder)
    try:
        with pytest.raises(ExternalCLIProcessTimeout):
            await AsyncioSubprocessRunner().run(
                (sys.executable, "-c", _READY_THEN_SLEEP, str(tmp_path / "ready")),
                stdin="prompt",
                timeout_seconds=0.5,
            )
        assert len(recorder.processes) == 1
        assert recorder.processes[0].returncode is not None
    finally:
        await _reap_leftovers(recorder)


@pytest.mark.asyncio
async def test_asyncio_runner_returns_stdout_stderr_and_exit_code() -> None:
    script = (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write('out:' + data)\n"
        "sys.stderr.write('err')\n"
        "sys.exit(int(sys.argv[1]))\n"
    )
    runner = AsyncioSubprocessRunner()

    ok = await runner.run((sys.executable, "-c", script, "0"), stdin="hi", timeout_seconds=30.0)
    assert ok == CLIProcessResult(exit_code=0, stdout="out:hi", stderr="err")

    failed = await runner.run((sys.executable, "-c", script, "3"), stdin="hi", timeout_seconds=30.0)
    assert failed == CLIProcessResult(exit_code=3, stdout="out:hi", stderr="err")


@pytest.mark.asyncio
async def test_asyncio_runner_missing_executable_is_command_error(tmp_path: Path) -> None:
    missing = str(tmp_path / "definitely-missing-cli")

    with pytest.raises(ExternalCLICommandError) as exc_info:
        await AsyncioSubprocessRunner().run((missing,), stdin="", timeout_seconds=5.0)

    assert exc_info.value.exit_code == 127


_READY_THEN_FLOOD = (
    "import os, sys\n"
    "fd = int(sys.argv[1])\n"
    "ready = sys.argv[2]\n"
    "with open(ready + '.tmp', 'w') as fh:\n"
    "    fh.write(str(os.getpid()))\n"
    "os.replace(ready + '.tmp', ready)\n"
    "chunk = b'x' * 65536\n"
    "while True:\n"
    "    os.write(fd, chunk)\n"
)
_FLOOD_STREAM_LIMIT = 1024
_BOUNDED_FINISH_SECONDS = 5.0


async def _drain_and_reap_flood(
    recorder: _SpawnRecorder, task: asyncio.Task[CLIProcessResult]
) -> None:
    """Always unblock a stuck runner (even on RED) by draining its pipes."""
    for process in recorder.processes:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if not task.done():
            await asyncio.wait_for(process.communicate(), timeout=10.0)
    if not task.done():
        task.cancel()
    await asyncio.wait([task], timeout=10.0)
    if task.done() and not task.cancelled():
        task.exception()  # Mark retrieved; assertions report the real outcome.
    await _reap_leftovers(recorder)


@pytest.mark.asyncio
@pytest.mark.parametrize(("fd", "stream_name"), [(1, "stdout"), (2, "stderr")])
async def test_asyncio_runner_cancellation_drains_high_output_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fd: int, stream_name: str
) -> None:
    recorder = _SpawnRecorder(limit=_FLOOD_STREAM_LIMIT)
    monkeypatch.setattr(external_cli_provider.asyncio, "create_subprocess_exec", recorder)
    ready = tmp_path / "ready"
    task = asyncio.ensure_future(
        AsyncioSubprocessRunner().run(
            (sys.executable, "-c", _READY_THEN_FLOOD, str(fd), str(ready)),
            stdin="prompt",
            timeout_seconds=60.0,
        )
    )
    try:
        await _wait_for_ready(ready, task)
        await asyncio.sleep(0.05)  # Let the child fill the pipe.
        assert len(recorder.processes) == 1
        process = recorder.processes[0]
        assert process.returncode is None

        task.cancel()
        done, _ = await asyncio.wait([task], timeout=_BOUNDED_FINISH_SECONDS)

        assert task in done, f"runner hung after cancellation with {stream_name} flooding"
        assert task.cancelled()
        assert process.returncode is not None
    finally:
        await _drain_and_reap_flood(recorder, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(("fd", "stream_name"), [(1, "stdout"), (2, "stderr")])
async def test_asyncio_runner_timeout_drains_high_output_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fd: int, stream_name: str
) -> None:
    recorder = _SpawnRecorder(limit=_FLOOD_STREAM_LIMIT)
    monkeypatch.setattr(external_cli_provider.asyncio, "create_subprocess_exec", recorder)
    task = asyncio.ensure_future(
        AsyncioSubprocessRunner().run(
            (sys.executable, "-c", _READY_THEN_FLOOD, str(fd), str(tmp_path / "ready")),
            stdin="prompt",
            timeout_seconds=0.5,
        )
    )
    try:
        done, _ = await asyncio.wait([task], timeout=_BOUNDED_FINISH_SECONDS)

        assert task in done, f"runner hung after timeout with {stream_name} flooding"
        assert isinstance(task.exception(), ExternalCLIProcessTimeout)
        assert len(recorder.processes) == 1
        assert recorder.processes[0].returncode is not None
    finally:
        await _drain_and_reap_flood(recorder, task)
