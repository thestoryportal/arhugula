"""Regression tests for Claude-to-Codex workflow parity."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / ".codex" / "hooks" / "codex_hook_adapter.py"
RUNTIME_WITNESS = ROOT / "tools" / "codex_hook_runtime_witness.py"
# This module exercises normal adapters even when invoked from an isolated reviewer.
os.environ.pop("HARNESS_CODEX_REVIEW_ISOLATED", None)


def test_live_runtime_witness_is_provider_free_and_operator_runnable() -> None:
    source = RUNTIME_WITNESS.read_text(encoding="utf-8")
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")

    assert 'base_url = "http://127.0.0.1:' in source
    assert "requires_openai_auth = false" in source
    assert '"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"' in source
    assert 'for tool in ("Bash", "apply_patch")' in source
    assert "codex-hook-runtime-witness:" in justfile
    assert "/usr/bin/python3 tools/codex_hook_runtime_witness.py" in justfile


def _runtime_witness_module():
    spec = importlib.util.spec_from_file_location(
        "codex_hook_runtime_witness_test", RUNTIME_WITNESS
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_runtime_witness_reports_empty_hook_stream(tmp_path: Path) -> None:
    witness = _runtime_witness_module()
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no hook events"):
        witness._assert_witness(tmp_path, events_path, 0)


def test_live_runtime_witness_retries_directory_not_empty_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    witness = _runtime_witness_module()
    calls = 0
    real_rmtree = witness.shutil.rmtree

    def racing_rmtree(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", path)
        real_rmtree(path)

    monkeypatch.setattr(witness.shutil, "rmtree", racing_rmtree)
    witness._remove_temp_tree(tmp_path, attempts=2, delay=0)

    assert calls == 2
    assert not tmp_path.exists()


def _valid_runtime_witness_fixture(tmp_path: Path) -> tuple[Any, Path, list[dict[str, str]]]:
    witness = _runtime_witness_module()
    events = [
        {"hook_event_name": "SessionStart", "session_id": "one"},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "one"},
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "one"},
        {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "session_id": "one"},
        {"hook_event_name": "PostToolUse", "tool_name": "apply_patch", "session_id": "one"},
        {"hook_event_name": "Stop", "session_id": "one"},
        {"hook_event_name": "SessionEnd", "session_id": "one"},
    ]
    (tmp_path / "shell-marker.txt").write_text("codex-shell-witness", encoding="utf-8")
    (tmp_path / "patch-marker.txt").write_text("codex-patch-witness\n", encoding="utf-8")
    return witness, tmp_path / "events.jsonl", events


def _write_runtime_events(path: Path, events: list[dict[str, str]]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_live_runtime_witness_accepts_complete_fixture(tmp_path: Path) -> None:
    witness, events_path, events = _valid_runtime_witness_fixture(tmp_path)
    _write_runtime_events(events_path, events)

    assert witness._assert_witness(tmp_path, events_path, 3)["status"] == "PASS"


@pytest.mark.parametrize("missing", ["SessionStart", "Stop", "SessionEnd"])
def test_live_runtime_witness_rejects_each_missing_lifecycle_event(
    tmp_path: Path, missing: str
) -> None:
    witness, events_path, events = _valid_runtime_witness_fixture(tmp_path)
    _write_runtime_events(
        events_path, [event for event in events if event["hook_event_name"] != missing]
    )

    with pytest.raises(RuntimeError):
        witness._assert_witness(tmp_path, events_path, 3)


@pytest.mark.parametrize(
    ("phase", "tool"),
    [(phase, tool) for phase in ("PreToolUse", "PostToolUse") for tool in ("Bash", "apply_patch")],
)
def test_live_runtime_witness_rejects_each_missing_tool_phase(
    tmp_path: Path, phase: str, tool: str
) -> None:
    witness, events_path, events = _valid_runtime_witness_fixture(tmp_path)
    _write_runtime_events(
        events_path,
        [
            event
            for event in events
            if (event["hook_event_name"], event.get("tool_name")) != (phase, tool)
        ],
    )

    with pytest.raises(RuntimeError, match="missing tool hook dispatch"):
        witness._assert_witness(tmp_path, events_path, 3)


def test_live_runtime_witness_rejects_wrong_request_count(tmp_path: Path) -> None:
    witness, events_path, events = _valid_runtime_witness_fixture(tmp_path)
    _write_runtime_events(events_path, events)

    with pytest.raises(RuntimeError, match="expected 3"):
        witness._assert_witness(tmp_path, events_path, 2)


@pytest.mark.parametrize("marker", ["shell-marker.txt", "patch-marker.txt"])
def test_live_runtime_witness_rejects_each_missing_effect(tmp_path: Path, marker: str) -> None:
    witness, events_path, events = _valid_runtime_witness_fixture(tmp_path)
    _write_runtime_events(events_path, events)
    (tmp_path / marker).unlink()

    with pytest.raises((RuntimeError, FileNotFoundError)):
        witness._assert_witness(tmp_path, events_path, 3)


def test_live_runtime_witness_rejects_multiple_session_identities(tmp_path: Path) -> None:
    witness, events_path, events = _valid_runtime_witness_fixture(tmp_path)
    events[-1]["session_id"] = "two"
    _write_runtime_events(events_path, events)

    with pytest.raises(RuntimeError, match="multiple sessions"):
        witness._assert_witness(tmp_path, events_path, 3)


def _adapter_module():
    spec = importlib.util.spec_from_file_location("codex_hook_adapter_test", ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != "posix", reason="process-group witness requires POSIX")
def test_hook_adapter_timeout_terminates_descendant_process_tree(tmp_path: Path) -> None:
    adapter = _adapter_module()
    adapter.TERMINATION_GRACE_SECONDS = 0.2
    child_pid_file = tmp_path / "child.pid"
    env = os.environ.copy()
    env["CHILD_PID_FILE"] = str(child_pid_file)

    proc = adapter.run_bounded(
        [
            "/bin/sh",
            "-c",
            (
                '(trap "" TERM; sleep 30) & child=$!; '
                'printf "%s" "$child" > "$CHILD_PID_FILE"; wait "$child"'
            ),
        ],
        cwd=tmp_path,
        timeout=2,
        env=env,
    )

    assert proc.returncode == 124
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"hook timeout left descendant alive: pid={child_pid}")


@pytest.mark.skipif(os.name != "posix", reason="process-group witness requires POSIX")
def test_hook_adapter_success_terminates_background_descendant(tmp_path: Path) -> None:
    adapter = _adapter_module()
    adapter.TERMINATION_GRACE_SECONDS = 0.2
    child_pid_file = tmp_path / "successful-child.pid"
    env = os.environ.copy()
    env["CHILD_PID_FILE"] = str(child_pid_file)

    proc = adapter.run_bounded(
        [
            "/bin/sh",
            "-c",
            (
                '(trap "" TERM; sleep 30) & child=$!; '
                'printf "%s" "$child" > "$CHILD_PID_FILE"; exit 0'
            ),
        ],
        cwd=tmp_path,
        timeout=5,
        env=env,
    )

    assert proc.returncode == 0
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"successful hook left descendant alive: pid={child_pid}")


def test_hook_adapter_interrupt_terminates_detached_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter_module()
    adapter.TERMINATION_GRACE_SECONDS = 0

    class InterruptingProcess:
        pid = 434343
        returncode: int | None = None

        def wait(self, timeout: float) -> int:
            _ = timeout
            if self.returncode is None:
                raise KeyboardInterrupt
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    process = InterruptingProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        adapter.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)) if sent_signal != 0 else None,
    )

    with pytest.raises(KeyboardInterrupt):
        adapter.run_bounded(["hook"], cwd=tmp_path, timeout=30, env=os.environ.copy())

    assert signals == [
        (process.pid, adapter.signal.SIGTERM),
        (process.pid, adapter.signal.SIGKILL),
    ]
    assert process.returncode == -15


def _commands(event: str, matcher: str) -> list[str]:
    payload = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    return [
        hook["command"]
        for group in payload["hooks"].get(event, [])
        if group.get("matcher", "") == matcher
        for hook in group["hooks"]
    ]


def _event_commands(event: str) -> list[str]:
    payload = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    return [hook["command"] for group in payload["hooks"][event] for hook in group["hooks"]]


@pytest.mark.parametrize(
    ("event", "matcher", "fragments"),
    [
        (
            "SessionStart",
            "startup|resume|clear|compact",
            ["codex-session-start.sh"],
        ),
        (
            "PreToolUse",
            "Bash|apply_patch|Edit|Write",
            ["pre_tool_use_policy.py"],
        ),
        (
            "PreToolUse",
            "Bash",
            [
                "precmd-clear-cache.sh",
                "codex_hook_adapter.py pre-commit",
            ],
        ),
        ("PreToolUse", "*", ["permission-guard.sh"]),
        (
            "PermissionRequest",
            "*",
            ["permission_request.py", "permission-guard.sh"],
        ),
        ("PreCompact", "*", ["precompact-checkpoint.sh"]),
        ("PostCompact", "*", ["postcompact-reinject.sh"]),
        ("SessionEnd", "*", ["codex-session-end.sh"]),
        (
            "Stop",
            "",
            ["stop_gate.py", "stop-gate.sh", "git-arc-guard.sh", "stop-loop.sh"],
        ),
        ("SubagentStart", "*", ["subagent-validate.sh"]),
        ("SubagentStop", "*", ["subagent-validate.sh"]),
        (
            "UserPromptSubmit",
            "",
            ["prompt-context.sh", "skill-activation-check.sh", "prompt-lint.sh"],
        ),
        (
            "PostToolUse",
            "Bash",
            ["roadmap-audit/post-merge-refresh.sh"],
        ),
        ("PostToolUse", "*", ["codex_hook_adapter.py post-tool-use"]),
    ],
)
def test_codex_hooks_cover_supported_claude_lifecycle(
    event: str, matcher: str, fragments: list[str]
) -> None:
    commands = _commands(event, matcher)
    for fragment in fragments:
        assert any(fragment in command.replace('"', "") for command in commands), (
            event,
            fragment,
            commands,
        )


# Claude-runtime-only hook helpers, exempt from the Codex mirror by explicit decision
# (operator-approved 2026-08-16). These inject retrieval context into Claude's own prompt
# and session lifecycle — `UserPromptSubmit` context packs, `PostToolUse` savings
# accounting, end-of-turn card rebuilds. Codex has no equivalent injection point, so a
# mirrored command would have nothing to bind to; the Codex side receives the same
# guidance declaratively through the marker-fenced Graft block in `AGENTS.md` instead.
#
# The exemption is deliberately NOT a skip. Each exempt command must still reference a
# helper that exists AND is git-tracked: `.claude/settings.json` shipping a reference to
# an untracked local file is precisely the defect this exemption was written alongside —
# every hook would fail MODULE_NOT_FOUND on a clean checkout while this suite stayed green.
_CLAUDE_ONLY_HELPER_DIR = ".claude/helpers/"


def _adapter_claude_only_hooks() -> dict[str, str]:
    """`CLAUDE_ONLY_HOOKS` as the adapter module defines it (imported, not regexed -- the
    adapter's import is side-effect-free; `main()` runs only under `__main__`)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("codex_hook_adapter", ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hooks = getattr(module, "CLAUDE_ONLY_HOOKS", {})
    assert isinstance(hooks, dict)
    return dict(hooks)


def _claude_only_helper(command: str) -> str | None:
    """Repo-relative helper path if `command` is an exempt Claude-only hook, else None."""
    marker = f"{_CLAUDE_ONLY_HELPER_DIR}"
    if marker not in command:
        return None
    tail = command.split(marker, 1)[1]
    name = tail.split('"', 1)[0].split()[0]
    return f"{_CLAUDE_ONLY_HELPER_DIR}{name}"


def _assert_claude_only_helper_is_tracked(command: str) -> None:
    relative = _claude_only_helper(command)
    assert relative is not None
    path = ROOT / relative
    assert path.exists(), f"exempt hook references a missing helper: {relative}"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, (
        f"exempt hook references an UNTRACKED helper: {relative}. A clean checkout would "
        f"run node against a missing module on every hook fire."
    )


def test_codex_hook_map_tracks_every_canonical_claude_hook_command() -> None:
    claude = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))["hooks"]
    codex = json.loads((ROOT / ".codex/hooks.json").read_text(encoding="utf-8"))["hooks"]
    adapter = ADAPTER.read_text(encoding="utf-8")
    codex_commands = "\n".join(
        hook["command"] for groups in codex.values() for group in groups for hook in group["hooks"]
    )
    wrappers = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            ROOT / "tools" / "hooks" / "codex-session-start.sh",
            ROOT / "tools" / "hooks" / "codex-session-end.sh",
        ]
        if path.exists()
    )
    implementation = f"{codex_commands}\n{adapter}\n{wrappers}"
    wrapper_targets = {
        "tools/roadmap-audit/session-start.sh": "roadmap-audit/session-start.sh",
        "tools/hooks/loop-gc.sh": "loop-gc.sh",
        "tools/hooks/session-end-cleanup.sh": "session-end-cleanup.sh",
    }

    # U-SR-09: a hook the adapter declares CLAUDE_ONLY (with its reason) is asserted ABSENT
    # from the Codex map -- the exemption is data the adapter carries, not a silent skip.
    claude_only = _adapter_claude_only_hooks()
    for relative, reason in claude_only.items():
        assert reason.strip(), f"CLAUDE_ONLY_HOOKS[{relative!r}] carries no reason"
        assert (ROOT / relative).exists(), f"CLAUDE_ONLY_HOOKS names a missing hook: {relative}"
        assert relative not in codex_commands, f"{relative} is CLAUDE_ONLY yet mirrored"

    assert set(claude) - set(codex) == {"PostToolUseFailure", "StopFailure"}
    for groups in claude.values():
        for group in groups:
            for hook in group["hooks"]:
                command = hook["command"]
                prefix = "${CLAUDE_PROJECT_DIR}/"
                if _claude_only_helper(command) is not None:
                    _assert_claude_only_helper_is_tracked(command)
                elif command.startswith(prefix):
                    relative = command.removeprefix(prefix)
                    if relative in wrapper_targets:
                        assert wrapper_targets[relative] in wrappers
                    elif relative in claude_only:
                        continue
                    else:
                        assert relative in implementation
                else:
                    assert command == "uv run pyright && git rev-parse --show-toplevel"
                    assert '["uv", "run", "pyright"]' in adapter
                    assert '["git", "rev-parse", "--show-toplevel"]' in adapter


def test_parity_doc_hook_counts_are_computed_not_hand_maintained() -> None:
    """U-CTX-09: `.codex/notes/claude-codex-parity.md`'s hook/matcher counts must track
    `.claude/settings.json` by construction — a hand-maintained figure silently drifts the
    moment a matcher is added or removed (as the now-removed dead `Bash(git commit*)`
    matcher's departure would have, had this assertion not existed). Event types = top-
    level hook lifecycle keys; matcher groups = total `{"matcher": ..., "hooks": [...]}`
    entries across every event; command handlers = total individual `"type": "command"`
    hook objects across every matcher group."""
    claude = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))["hooks"]
    event_types = len(claude)
    matcher_groups = sum(len(groups) for groups in claude.values())
    command_handlers = sum(
        1
        for groups in claude.values()
        for group in groups
        for hook in group["hooks"]
        if hook.get("type") == "command"
    )

    parity = (ROOT / ".codex" / "notes" / "claude-codex-parity.md").read_text(encoding="utf-8")
    match = re.search(
        r"(\d+) event types, (\d+) matcher groups, and (\d+) command handlers",
        parity,
    )
    assert match is not None, "claude-codex-parity.md no longer states the hook-count claim"
    doc_events, doc_matchers, doc_commands = (int(g) for g in match.groups())

    assert doc_events == event_types, (doc_events, event_types)
    assert doc_matchers == matcher_groups, (doc_matchers, matcher_groups)
    assert doc_commands == command_handlers, (doc_commands, command_handlers)


def _run_adapter(
    mode: str,
    payload: dict[str, object],
    *,
    cwd: Path,
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(ADAPTER), mode],
        cwd=cwd,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )


def test_post_tool_adapter_captures_nonzero_bash_result(tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "session-a",
        "tool_name": "Bash",
        "tool_response": {"exit_code": 1, "output": "failed"},
    }

    first = _run_adapter("post-tool-use", payload, cwd=tmp_path)
    second = _run_adapter("post-tool-use", payload, cwd=tmp_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    rows = [
        json.loads(line)
        for line in (tmp_path / ".harness" / "session-issues.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["event"] for row in rows] == [
        "PostToolUseFailure",
        "PostToolUseFailure",
    ]
    context = json.loads(second.stdout)["hookSpecificOutput"]
    assert context["hookEventName"] == "PostToolUse"
    assert "recurring failure" in context["additionalContext"]


def test_post_tool_adapter_does_not_misclassify_textual_success(tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__example__read",
        "tool_response": "model-facing successful text",
    }

    proc = _run_adapter("post-tool-use", payload, cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert not (tmp_path / ".harness" / "session-issues.jsonl").exists()


def test_post_tool_adapter_preserves_underlying_hook_failure_message() -> None:
    adapter = _adapter_module()

    assert adapter.additional_context({"systemMessage": "underlying hook failed"}) == (
        "underlying hook failed"
    )


def test_post_tool_adapter_lints_python_files_from_apply_patch(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("print('x')\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ruff = bin_dir / "ruff"
    ruff.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = check ]; then echo 'sample.py:1:1: E999 broken'; exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    ruff.chmod(0o755)
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Update File: sample.py\n@@\n"},
        "tool_response": {"status": "completed"},
    }

    proc = _run_adapter(
        "post-tool-use",
        payload,
        cwd=tmp_path,
        path=f"{bin_dir}:{os.environ['PATH']}",
    )

    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]
    assert context["hookEventName"] == "PostToolUse"
    assert "ruff findings on sample.py" in context["additionalContext"]


@pytest.mark.parametrize("command", ["git commit -m test", "rtk git commit -m test"])
def test_pre_commit_adapter_blocks_when_pyright_fails(tmp_path: Path, command: str) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\necho 'pyright failed' >&2\nexit 1\n", encoding="utf-8")
    uv.chmod(0o755)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(tmp_path),
    }

    proc = _run_adapter(
        "pre-commit",
        payload,
        cwd=tmp_path,
        path=f"{bin_dir}:{os.environ['PATH']}",
    )

    assert proc.returncode == 2
    assert "pyright failed" in proc.stderr


def test_pre_commit_adapter_uses_repo_safe_uv_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf \'%s\' "${UV_CACHE_DIR:-}" > "$PWD/uv-cache-observed"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m test"},
        "cwd": str(tmp_path),
    }
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)

    proc = _run_adapter(
        "pre-commit",
        payload,
        cwd=tmp_path,
        path=f"{bin_dir}:{os.environ['PATH']}",
    )

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "uv-cache-observed").read_text(encoding="utf-8") == (
        "/tmp/arhugula-uv-cache"
    )


def test_pre_commit_adapter_blocks_outside_a_git_worktree(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m test"},
        "cwd": str(tmp_path),
    }

    proc = _run_adapter(
        "pre-commit",
        payload,
        cwd=tmp_path,
        path=f"{bin_dir}:{os.environ['PATH']}",
    )

    assert proc.returncode == 2
    assert "not a git repository" in proc.stderr


def test_isolated_review_session_start_skips_shared_context_checkpoint() -> None:
    env = os.environ.copy()
    env["HARNESS_CODEX_REVIEW_ISOLATED"] = "1"

    proc = subprocess.run(
        [sys.executable, str(ROOT / ".codex" / "hooks" / "session_start.py")],
        cwd=ROOT,
        input=json.dumps({"session_id": "review-session", "cwd": str(ROOT)}),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "Codex context guard" not in proc.stdout


def test_session_start_wrapper_registers_lease_before_posture(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    payload = {"session_id": "wrapper-session", "cwd": str(tmp_path)}
    env = os.environ.copy()
    env["HARNESS_CODEX_REVIEW_ISOLATED"] = "1"
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)

    start = subprocess.run(
        ["bash", str(ROOT / "tools" / "hooks" / "codex-session-start.sh")],
        cwd=tmp_path,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )

    assert start.returncode == 0, start.stderr
    assert start.stdout == ""
    leases = list((tmp_path / ".git" / "codex-worktree-sessions").rglob("*.lease"))
    assert [path.name for path in leases] == ["session-wrapper-session.lease"]

    end = subprocess.run(
        ["bash", str(ROOT / "tools" / "hooks" / "codex-session-end.sh")],
        cwd=tmp_path,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert end.returncode == 0, end.stderr
    assert not list((tmp_path / ".git" / "codex-worktree-sessions").rglob("*.lease"))


def test_session_start_bounds_advisory_hygiene(tmp_path: Path) -> None:
    hook_dir = tmp_path / "tools" / "hooks"
    roadmap_dir = tmp_path / "tools" / "roadmap-audit"
    posture_dir = tmp_path / ".codex" / "hooks"
    hook_dir.mkdir(parents=True)
    roadmap_dir.mkdir(parents=True)
    posture_dir.mkdir(parents=True)
    for name in ("codex-session-start.sh", "session-lease.sh", "lib.sh"):
        shutil.copy2(ROOT / "tools" / "hooks" / name, hook_dir / name)
    lib_path = hook_dir / "lib.sh"
    lib_source = lib_path.read_text(encoding="utf-8")
    assert "hook_bounded() {" in lib_source
    lib_path.write_text(
        lib_source.replace("hook_bounded() {", "_real_hook_bounded() {", 1)
        + """
hook_bounded() {
  if [ "${3##*/}" = "loop-gc.sh" ]; then
    printf '%s' "$1" > "$HYGIENE_BOUND_MARKER"
  fi
  _real_hook_bounded "$@"
}
""",
        encoding="utf-8",
    )
    (posture_dir / "session_start.py").write_text(
        "print('bounded posture')\n",
        encoding="utf-8",
    )
    (roadmap_dir / "session-start.sh").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' \'{"hookSpecificOutput":{"additionalContext":"roadmap"}}\'\n',
        encoding="utf-8",
    )
    hygiene_term_marker = tmp_path / "hygiene-term"
    hygiene_bound_marker = tmp_path / "hygiene-bound"
    (hook_dir / "loop-gc.sh").write_text(
        "#!/bin/sh\n"
        'trap \'printf term > "$HYGIENE_TERM_MARKER"; '
        'kill "$sleeper" 2>/dev/null; wait "$sleeper" 2>/dev/null; exit 0\' TERM\n'
        'sleep 30 &\nsleeper=$!\nwait "$sleeper"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    payload = json.dumps({"session_id": "bounded-hygiene", "cwd": str(tmp_path)})
    env = os.environ.copy()
    env.update(
        {
            "CLAUDE_PROJECT_DIR": str(tmp_path),
            "HARNESS_SESSION_START_HYGIENE_SECONDS": "1",
            "HYGIENE_BOUND_MARKER": str(hygiene_bound_marker),
            "HYGIENE_TERM_MARKER": str(hygiene_term_marker),
        }
    )

    proc = subprocess.run(
        ["bash", str(hook_dir / "codex-session-start.sh")],
        cwd=tmp_path,
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert hygiene_bound_marker.read_text(encoding="utf-8") == "1"
    assert hygiene_term_marker.read_text(encoding="utf-8") == "term"
    assert "bounded posture" in proc.stdout
    assert "roadmap" in proc.stdout


def test_registered_session_lifecycle_round_trips_active_lease(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "tools").symlink_to(ROOT / "tools", target_is_directory=True)
    payload = json.dumps({"session_id": "registered-round-trip", "cwd": str(tmp_path)})
    env = os.environ.copy()
    env.update(
        {
            "HARNESS_CODEX_REVIEW_ISOLATED": "1",
            "CLAUDE_PROJECT_DIR": str(tmp_path),
            "HOME": str(tmp_path / "home"),
        }
    )

    for command in _event_commands("SessionStart"):
        proc = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=tmp_path,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
        assert proc.returncode == 0, (command, proc.stderr)

    leases = list((tmp_path / ".git" / "codex-worktree-sessions").rglob("*.lease"))
    assert len(leases) == 1
    assert leases[0].read_text(encoding="utf-8").splitlines()[0] == "active"

    for command in _event_commands("SessionEnd"):
        proc = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=tmp_path,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
        assert proc.returncode == 0, (command, proc.stderr)

    assert not list((tmp_path / ".git" / "codex-worktree-sessions").rglob("*.lease"))


def test_session_lifecycle_wrappers_order_registration_and_release() -> None:
    start = (ROOT / "tools" / "hooks" / "codex-session-start.sh").read_text(encoding="utf-8")
    end = (ROOT / "tools" / "hooks" / "codex-session-end.sh").read_text(encoding="utf-8")

    assert start.index("lease_action start") < start.index("session_start.py")
    assert start.index("session_start.py") < start.index("roadmap-audit/session-start.sh")
    assert start.index("roadmap-audit/session-start.sh") < start.index("loop-gc.sh")
    assert start.index("loop-gc.sh") < start.rindex("lease_action activate")
    assert "lease_action end" in start
    assert end.index('session-lease.sh" end') < end.index("session-end-cleanup.sh")


def test_session_start_hygiene_is_report_only_and_never_reaps() -> None:
    hygiene = (ROOT / "tools" / "hooks" / "loop-gc.sh").read_text(encoding="utf-8")

    assert "loop_gc_worktrees report" in hygiene
    assert "loop_gc_worktrees reap" not in hygiene


def test_parity_regressions_are_blocking_locally_and_in_ci() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    test_steps = workflow["jobs"]["test"]["steps"]

    assert "codex-parity-check" in justfile
    assert "bash tools/codex-parity-check.sh" in justfile
    assert any(
        "bash tools/codex-parity-check.sh" in str(step.get("run") or "") for step in test_steps
    )
    assert any(
        str(step.get("uses") or "").startswith("actions/checkout@")
        and str((step.get("with") or {}).get("fetch-depth")) == "0"
        for step in test_steps
    )


def test_local_premerge_gates_match_ci_format_check() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")

    assert "fmt-check:\n    uv run ruff format --check ." in justfile
    assert "check: codex-sync lint fmt-check typecheck" in justfile
    assert "codex-check: codex-sync lint fmt-check typecheck" in justfile


def test_session_end_hook_uses_supported_timeout() -> None:
    payload = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    hooks = [hook for group in payload["hooks"]["SessionEnd"] for hook in group["hooks"]]

    assert len(hooks) == 1
    assert hooks[0]["timeout"] <= 3


def test_merge_gate_honors_operator_authorized_ten_round_checkpoint() -> None:
    # The 2026-08-01 operator decision set the period at ten rounds; C-HE-21 §1 (v1.5 X5,
    # ratified 2026-08-25) makes it a recorded-decision checkpoint, never an auto-stopping
    # cap (U-HE-39). All three carriers keep the period and the eleventh-disagreement
    # decision point; they must not describe it as a cap.
    for path in [
        ROOT / ".agents" / "skills" / "merge-gate" / "SKILL.md",
        ROOT / ".claude" / "skills" / "merge-gate" / "SKILL.md",
        ROOT / ".claude" / "skills" / "ship-pr" / "SKILL.md",
    ]:
        merge_gate = path.read_text(encoding="utf-8")
        assert "ten rounds" in merge_gate, path
        assert "eleventh" in merge_gate.lower(), path
        assert "disagreement" in merge_gate, path
        assert "recorded-decision checkpoint" in merge_gate, path
        # The noun alone survives "the checkpoint is a cap"; pin the negation (U-HE-39 lens r1).
        assert "not a cap" in merge_gate or "never a cap" in merge_gate, path
        assert "capped at ten" not in merge_gate.lower(), path
        assert "cap this at ten" not in merge_gate.lower(), path
        assert "cap automatic fix/re-gate at ten" not in merge_gate.lower(), path

    codex_merge_gate = (ROOT / ".agents" / "skills" / "merge-gate" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "just codex-hook-runtime-witness" in codex_merge_gate
    assert "both tool phase pairs" in codex_merge_gate


def test_every_shared_state_hook_is_inert_for_isolated_review(tmp_path: Path) -> None:
    shell_hooks = [
        ROOT / path
        for path in [
            "tools/hooks/git-arc-guard.sh",
            "tools/hooks/loop-gc.sh",
            "tools/hooks/permission-guard.sh",
            "tools/hooks/postcompact-reinject.sh",
            "tools/hooks/precmd-clear-cache.sh",
            "tools/hooks/precompact-checkpoint.sh",
            "tools/hooks/prompt-context.sh",
            "tools/hooks/prompt-lint.sh",
            "tools/hooks/session-end-cleanup.sh",
            "tools/hooks/skill-activation-check.sh",
            "tools/hooks/stop-gate.sh",
            "tools/hooks/stop-loop.sh",
            "tools/roadmap-audit/post-merge-refresh.sh",
            "tools/roadmap-audit/session-start.sh",
        ]
    ]
    python_hooks = [
        (ROOT / ".codex" / "hooks" / "codex_hook_adapter.py", ["post-tool-use"]),
        (ROOT / ".codex" / "hooks" / "session_start.py", []),
        (ROOT / ".codex" / "hooks" / "stop_gate.py", []),
    ]
    env = os.environ.copy()
    env.update(
        {
            "HARNESS_CODEX_REVIEW_ISOLATED": "1",
            "CLAUDE_PROJECT_DIR": str(tmp_path),
            "HOME": str(tmp_path / "home"),
        }
    )
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "isolated",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_response": {"exit_code": 1},
        }
    )

    for path in shell_hooks:
        assert "hook_review_isolated && exit 0" in path.read_text(encoding="utf-8")
        before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
        proc = subprocess.run(
            ["bash", str(path)],
            cwd=tmp_path,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=env,
        )
        after = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
        assert proc.returncode == 0, (path, proc.stderr)
        assert proc.stdout == "", path
        assert after == before, path

    for path, args in python_hooks:
        before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
        proc = subprocess.run(
            [sys.executable, str(path), *args],
            cwd=tmp_path,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=env,
        )
        after = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
        assert proc.returncode == 0, (path, proc.stderr)
        assert proc.stdout == "", path
        assert after == before, path


def test_every_tracked_claude_skill_has_a_codex_entrypoint() -> None:
    def declared_name(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                return line.removeprefix("name:").strip()
        raise AssertionError(f"missing skill name: {path}")

    claude_names = {
        declared_name(path)
        for path in (ROOT / ".claude" / "skills").rglob("SKILL.md")
        if subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path.relative_to(ROOT))],
            cwd=ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    }
    codex_names = {declared_name(path) for path in (ROOT / ".agents" / "skills").glob("*/SKILL.md")}

    assert claude_names <= codex_names


def test_every_non_native_skill_bridge_preserves_its_canonical_source_contract() -> None:
    def declared_name(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                return line.removeprefix("name:").strip()
        raise AssertionError(f"missing skill name: {path}")

    claude_skills = {
        declared_name(path): path
        for path in (ROOT / ".claude" / "skills").rglob("SKILL.md")
        if subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path.relative_to(ROOT))],
            cwd=ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    }
    codex_skills = {
        declared_name(path): path for path in (ROOT / ".agents" / "skills").glob("*/SKILL.md")
    }
    native_codex_workflows = {"merge-gate", "roadmap-continue", "ship-pr"}

    for name, canonical in claude_skills.items():
        if name in native_codex_workflows:
            continue
        bridge = codex_skills[name].read_text(encoding="utf-8")
        canonical_relative = str(canonical.relative_to(ROOT))
        assert canonical_relative in bridge, (name, canonical_relative)
        lowered = bridge.lower()
        assert "complete" in lowered, name
        assert "source of truth" in lowered or "full workflow" in lowered, name
        assert "runner translations only" in lowered or "translate only runner" in lowered, name


@pytest.mark.parametrize(
    "path",
    [
        ".harness/memory/semantic/index.jsonl",
        ".impeccable/live/config.json",
        "tools/dashboard/public/index.html",
        "tools/dashboard/.DS_Store",
    ],
)
def test_local_skill_and_runtime_state_does_not_dirty_root(path: str) -> None:
    proc = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, check=False)
    assert proc.returncode == 0, path


# R-CTX-1 D2 (U-CTX-16) removed these 9 skills entirely — nothing tracked remains from
# which to DERIVE this roster, so they are pinned literals (see the completeness
# assertion in the test below, which fails loudly the moment the 21-total drifts).
_REMOVED_DESIGN_SKILLS = ("frontend-design", "impeccable", "taste-skill", "ui-ux-pro-max")
_REMOVED_TRACKED_BMAD_SKILLS = (
    "bmad-agent-pm",
    "bmad-prd",
    "bmad-prfaq",
    "bmad-product-brief",
    "bmad-technical-research",
)


def _removed_local_bmad_skill_names() -> tuple[str, ...]:
    """The 12 machine-local bmad-* skill names R-CTX-1 R3 (U-CTX-17) newly gitignores,
    DERIVED from the .gitignore R3 block rather than hardcoded — a future edit to that
    block is picked up automatically instead of silently drifting from this test."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    return tuple(
        sorted(re.findall(r"^\.claude/skills/(bmad-[a-z0-9-]+)/$", gitignore, re.MULTILINE))
    )


def test_r_ctx_1_removed_skill_roster_is_21_and_absent_from_tracked_state() -> None:
    """R-CTX-1 D2 + R3 catalog-assertion test for the 21 skills removed from this repo:
    4 operator-installed design skills + 5 tracked bmad-* skills DELETED at U-CTX-16,
    plus 12 machine-local bmad-* skills newly GITIGNORED at U-CTX-17.

    Per plan errata E7 this asserts against TRACKED state only — whether a gitignored
    local payload directory still exists on disk in the root checkout is an
    ORCHESTRATOR post-merge acceptance step, not this CI test's job.
    """
    local_bmad = _removed_local_bmad_skill_names()
    assert len(local_bmad) == 12, local_bmad  # completeness assertion: catches .gitignore drift

    roster = (*_REMOVED_DESIGN_SKILLS, *_REMOVED_TRACKED_BMAD_SKILLS, *local_bmad)
    assert len(roster) == 21, roster
    assert len(set(roster)) == len(roster), "duplicate name across the three removed-skill rosters"

    for name in roster:
        claude_side = subprocess.run(
            ["git", "ls-files", f".claude/skills/{name}/"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        agents_side = subprocess.run(
            ["git", "ls-files", f".agents/skills/{name}/"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert claude_side.stdout == "", (name, "still tracked under .claude/skills/")
        assert agents_side.stdout == "", (name, "still tracked under .agents/skills/")


def test_codex_shipping_skills_encode_current_review_and_ci_fixed_point() -> None:
    combined = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in [
            ".agents/skills/codex-autonomous-loop/SKILL.md",
            ".agents/skills/roadmap-continue/SKILL.md",
            ".agents/skills/ship-pr/SKILL.md",
            ".agents/skills/merge-gate/SKILL.md",
        ]
    )
    for required in [
        "just gemini-review",
        "Antigravity",
        "three-lens",
        "base `main` HEAD",
        "stale prior",
        "main CI",
        "context-save",
    ]:
        assert required in combined

    merge_gate = (ROOT / ".agents/skills/merge-gate/SKILL.md").read_text(encoding="utf-8")
    assert "--sandbox read-only" in merge_gate
    assert "-s read-only" not in merge_gate


def test_roadmap_continue_carriers_document_span_emission_parity() -> None:
    # U-HE-50 (C-HE-27 §5 X6a), codex u-he-50 r3: BOTH runner carriers must document
    # the same emission surface — the logged wrapper (automatic verify edges) plus the
    # interim session-emitted absorb/edit/verify_unavailable block — or a supported
    # runner's arcs leave C-HE-27 spans null. The interim block is deleted with the
    # B-218 PR from both carriers together.
    for path in [
        ".claude/skills/roadmap-continue/SKILL.md",
        ".agents/skills/roadmap-continue/SKILL.md",
    ]:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "review-with-failover-logged" in text, path
        assert "--phase absorb --edge start" in text, path
        assert "--phase edit --edge start" in text, path
        assert "verify_unavailable" in text, path
        assert "B-218" in text, path
    # codex r6: the Codex runner's unlogged gemini rounds have NO wrapper emitter,
    # so its carrier must hold the executable verify commands itself and point at
    # the single definition home (the Claude ship-pr block) — keyword presence
    # alone would stay green with the sentence gutted.
    codex_carrier = (ROOT / ".agents/skills/roadmap-continue/SKILL.md").read_text(encoding="utf-8")
    assert "--phase verify --edge start" in codex_carrier
    assert ".claude/skills/ship-pr/SKILL.md" in codex_carrier


def test_roadmap_continue_carriers_wire_the_selection_time_disjointness_gate() -> None:
    # U-HE-36 (C-HE-13 §5), codex u-he-36 r1 P1: BOTH runner carriers must run the same
    # guard-allowlisted gate between `selectable` and `reserve`, or a lane launched through
    # the Codex bridge bypasses the selection fence entirely.
    gate = "uv run python tools/arc_disjoint_check.py check --candidate HEAD"
    for path in [
        ".claude/skills/roadmap-continue/SKILL.md",
        ".agents/skills/roadmap-continue/SKILL.md",
    ]:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert gate in text, path
        assert text.index("reservations.py selectable") < text.index(gate), path
        assert text.index(gate) < text.index("reservations.py reserve "), path
    # codex u-he-36 r6: the Codex carrier's loop-state start must follow the reservation —
    # `codex-autonomous-arc` binds .harness/codex_loop_state.json to the arc.
    codex = (ROOT / ".agents/skills/roadmap-continue/SKILL.md").read_text(encoding="utf-8")
    assert codex.index("reservations.py reserve ") < codex.index(
        "just codex-autonomous-arc <arc-id>` in that worktree"
    )
    # B-228 (codex r2/r6/r7): the SAME gate runs again at ship pre-flight in both carriers,
    # when the candidate carries the arc's commits.
    for path in [".claude/skills/ship-pr/SKILL.md", ".agents/skills/ship-pr/SKILL.md"]:
        assert gate in (ROOT / path).read_text(encoding="utf-8"), path


def test_forward_profile_template_preserves_current_codex_home_and_review_boundary() -> None:
    profile = (ROOT / ".codex" / "notes" / "arhugula-forward.config.toml.example").read_text(
        encoding="utf-8"
    )
    parity = (ROOT / ".codex" / "notes" / "claude-codex-parity.md").read_text(encoding="utf-8")

    assert 'approval_policy = "on-request"' in profile
    assert 'approvals_reviewer = "auto_review"' in profile
    assert 'sandbox_mode = "danger-full-access"' in profile
    assert "CODEX_HOME" not in profile
    assert "codex --profile arhugula-forward" in parity
    assert "managed requirements" in parity
    assert "leaving the worktree" in parity
    assert "~/.codex-arhugula/CODEX_HOME/" in parity
    assert "overlays" in parity


def test_controller_and_implementer_profiles_pin_parity_models() -> None:
    forward = (ROOT / ".codex" / "notes" / "arhugula-forward.config.toml.example").read_text(
        encoding="utf-8"
    )
    implementer = (
        ROOT / ".codex" / "notes" / "arhugula-implementer.config.toml.example"
    ).read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    loop = (ROOT / ".agents" / "skills" / "codex-autonomous-loop" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert 'model = "gpt-5.6-sol"' in forward
    assert 'model_reasoning_effort = "high"' in forward
    assert 'model = "gpt-5.6-terra"' in implementer
    assert 'model_reasoning_effort = "high"' in implementer
    for text in (agents, loop):
        assert "Fable 5" in text
        assert "Opus 5" in text
        assert "gpt-5.6-sol" in text
        assert "gpt-5.6-terra" in text
        assert "arhugula-implementer" in text

    brief = (ROOT / ".codex" / "notes" / "leg-brief-template.md").read_text(encoding="utf-8")
    assert "codex exec --profile arhugula-implementer" in brief
    assert "gpt-5.6-sol" in brief
    assert "gpt-5.6-terra" in brief
    assert "gpt-5.6-codex" not in brief


def test_reconciled_hardening_docs_do_not_preserve_superseded_gc_or_path_claims() -> None:
    review = (
        ROOT / ".harness" / "hardening-workflow" / "hook-advisor-workflow-review.md"
    ).read_text(encoding="utf-8")
    inventory = (
        ROOT / ".harness" / "hardening-workflow" / "inventory-hooks-skills-disciplines.md"
    ).read_text(encoding="utf-8")

    assert "it reaps at the *next* session's **SessionStart**" not in review
    assert "tools/loop/run.sh" not in inventory
    assert "tools/loop/defer.sh" not in inventory
    assert "tools/loop/halt.sh" not in inventory
    assert "## B. LOOP RUNNER (`tools/04-loop/`)" in inventory


def test_antigravity_review_is_read_only_and_uses_writable_operational_log() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    recipe = justfile.split("gemini-review base='main' outcome_json='':", 1)[1].split(
        "\n_require-antigravity:", 1
    )[0]
    reviewer = (ROOT / "tools" / "agy_review.py").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "tools/agy_review.py" in recipe
    assert "--mode" in reviewer and '"plan"' in reviewer
    assert 'Path(scratch) / "route.log"' in reviewer
    assert "/tmp/arhugula-agy-review.log" not in reviewer
    assert "GEMINI_API_KEY" in reviewer and "GOOGLE_API_KEY" in reviewer
    assert "VERDICT: APPROVE" in reviewer and "VERDICT: BLOCK" in reviewer
    assert "standing authorization" in agents.lower()


def test_arc_metrics_capture_is_mirrored_across_both_ship_carriers() -> None:
    """A Codex-shipped arc must reach the ledger too, or the baseline omits it.

    `.agents/skills/ship-pr` is Codex-native rather than a bridge, so wiring the
    capture into only the Claude carrier would silently drop every Codex-run arc
    from the B-170 cohort comparison.
    """
    for path in [
        ROOT / ".agents" / "skills" / "ship-pr" / "SKILL.md",
        ROOT / ".claude" / "skills" / "ship-pr" / "SKILL.md",
    ]:
        body = path.read_text(encoding="utf-8")
        # Scope every assertion to the arc-metrics SECTION. Both files discuss
        # merges and branches at length elsewhere, so an unscoped substring
        # check would pass on prose that has nothing to do with this protocol.
        marker = "arc-metrics"
        start = body.lower().index(marker)
        section = body[start:]
        # B-230 Task 3: the queue command rides the ONE close-out call, `just arc-close`
        # (exit report, then `arc-metrics queue` with the tail forwarded verbatim), so
        # the fenced command to inspect is that call — exactly one per carrier.
        assert "just arc-close" in section, path
        # C-HE-25 X6e activation (codex u-he-48 r1/r2): inspect the fenced
        # COMMAND, not the surrounding prose -- explanatory paragraphs also say
        # "--transcript", so a prose-level search stays green with the flag
        # deleted from the command. The Claude carrier must pass the flag; the
        # Codex carrier must instead NAME its exclusion (rollout transcripts
        # under ~/.codex/sessions are a shape arc_cost.py does not parse).
        # Fences are balanced, so the odd-indexed split segments are the fenced code;
        # the prose around them names the call too and must not count.
        fenced = body.split("```")[1::2]
        blocks = [b for b in fenced if "just arc-close" in b]
        assert len(blocks) == 1, path
        if ".claude" in str(path):
            assert "--transcript" in blocks[0], path
        else:
            assert "--transcript" not in blocks[0], path
            assert "~/.codex/sessions" in section, path
            assert "OMIT `--transcript`" in section, path
        assert "just arc-metrics drain" in section, path
        # the two properties a carrier must not silently drop
        assert "outside" in section.lower(), path  # queue lives out of the repo
        assert "merged history" in section.lower(), path  # release point, not any merge


def _skill_description(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("description:"):
            return line.removeprefix("description:").strip()
    raise AssertionError(f"missing description: {path}")


def test_defect_class_preflight_description_contract() -> None:
    """The consumer-inventory trigger lives in the frontmatter description,
    which is the discovery surface for BOTH runners and is capped fail-closed
    at 1024 chars by the AS-axis skill_frontmatter_validator. Pin all three
    properties so a wording revert, a cap breach, or canonical/mirror drift
    goes red instead of silently dropping the authoring-time pause."""
    canonical = _skill_description(
        ROOT / ".claude" / "skills" / "defect-class-preflight" / "SKILL.md"
    )
    mirror = _skill_description(ROOT / ".agents" / "skills" / "defect-class-preflight" / "SKILL.md")
    assert canonical == mirror
    assert len(canonical) <= 1024
    assert "new consumer of an existing data surface" in canonical
    assert "BEFORE the consumer is written" in canonical


def test_project_mcp_context7_registration_is_locked_local() -> None:
    """Supply-chain witness for the context7 registration the preflight
    skill's external-SDK rung names. The .mcp.json entry must run
    `npx --no-install` (resolves ONLY the locked local installation —
    never a registry fetch of unattested bytes), and the repository
    lockfile must bind the exact version WITH an integrity digest, so the
    bytes a trusted session executes are the bytes this repo reviewed.
    Server liveness is witnessed at registration time, not in CI."""
    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    context7 = mcp["mcpServers"]["context7"]
    assert context7["command"] == "npx"
    assert context7["args"][0] == "--no-install"
    assert "context7-mcp" in context7["args"]
    assert not any("-y" == arg for arg in context7["args"])

    manifest = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"]["@upstash/context7-mcp"] == "4.0.3"
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    locked = lock["packages"]["node_modules/@upstash/context7-mcp"]
    assert locked["version"] == "4.0.3"
    assert locked["integrity"].startswith("sha512-")


def test_merge_gate_carriers_wire_arc_id_and_adjudication() -> None:
    """U-HE-47 codex r4 P2×2: the Codex carrier's emit line must pass the reservation
    --arc-id (its omission defaults gate rows to pr-<N> and silently voids the X6d
    same-arc unique-catch join), and BOTH carriers must instruct the absorption-step
    adjudication with their own runner's absorber identity — reverting either
    instruction must red this suite, not just leave the helper tests green."""
    codex = (ROOT / ".agents" / "skills" / "merge-gate" / "SKILL.md").read_text(encoding="utf-8")
    claude = (ROOT / ".claude" / "skills" / "merge-gate" / "SKILL.md").read_text(encoding="utf-8")

    assert "just merge-gate-emit --pr <N> --arc-id <arc-id> --lens <id>" in codex
    assert "--arc-id <arc-id>" in claude

    # B-230 Task 5: BOTH carriers record the three verdicts in one call and name the
    # per-lens form only as the repair path — reverting either carrier to three calls
    # must red this suite.
    emit_all = "just merge-gate-emit-all --pr <"
    for text, carrier in ((codex, "codex"), (claude, "claude")):
        assert emit_all in text, carrier
        assert "--concurrency-json" in text and "--witness-json" in text, carrier
        assert "never" in text and "re-running `emit-all`" in text, carrier

    # the documented command MUST carry the HARNESS_ARC_ID= prefix (r5 P2): the guard
    # auto-allows only the prefixed form, so a bare instruction strands headless at ask
    adjudicate_cmd = (
        "HARNESS_ARC_ID=<arc-id> just merge-gate-adjudicate "
        "--finding-id <id> --disposition accepted|rejected"
    )
    assert adjudicate_cmd in codex
    assert "--actor codex_absorber" in codex
    assert adjudicate_cmd in claude
    assert "--actor claude_absorber" in claude

    preflight = (ROOT / ".claude" / "skills" / "defect-class-preflight" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    bridge = (ROOT / ".agents" / "skills" / "defect-class-preflight" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "HARNESS_ARC_ID=<arc-id> just" in preflight
    assert "merge-gate-adjudicate --finding-id <id>" in preflight
    assert "codex_absorber" in bridge  # the runner translation of <runner>_absorber
