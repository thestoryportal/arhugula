"""U-MEM-24 live external CLI auth confirmations.

The runtime represents external CLI auth as explicit ``ExternalCliRoute``
carriers. These e2e tests bind real local CLI status probes to those carriers
without printing secrets or moving credential material.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess

import pytest
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain, ProviderCandidate, ProviderFamily
from harness_cp.memory_access_mode import ExternalCliRoute
from harness_is.cli_profile import (
    CliImportPolicy,
    CliInstructionSource,
    CliInstructionSourceKind,
    CliProfileKind,
)
from harness_runtime.cli_profile_loading import CliProfileResolutionRequest, resolve_cli_profile

pytestmark = pytest.mark.e2e


def _instruction_source() -> CliInstructionSource:
    return CliInstructionSource(
        source_id="project-instructions",
        source_kind=CliInstructionSourceKind.PROJECT_INSTRUCTION,
        path="AGENTS.md",
        required=True,
    )


def _chain(provider: str, family: ProviderFamily) -> FallbackChain:
    return FallbackChain(
        primary=ProviderCandidate(provider=provider, model="external-cli", family=family),
        same_family=(),
        cross_family=(),
        terminal=None,
    )


def _resolve_authenticated_route(
    *,
    kind: CliProfileKind,
    provider: str,
    external_cli_kind: str,
    command_name: str,
    family: ProviderFamily,
) -> ExternalCliRoute:
    route = ExternalCliRoute(
        provider_name=provider,
        external_cli_kind=external_cli_kind,
        command_name=command_name,
        auth_check_passed=True,
        optional=False,
        degradation_allowed=False,
    )
    resolved = resolve_cli_profile(
        CliProfileResolutionRequest(
            model_binding=ModelBinding(provider=provider, model="external-cli"),
            fallback_chain=_chain(provider, family),
            profile_kind=kind,
            external_cli_route=route,
            instruction_sources=(_instruction_source(),),
            import_policy=CliImportPolicy.READ_ONLY,
        )
    )
    assert resolved.profile.kind is kind
    assert resolved.external_cli_route == route
    return route


def _run_status(
    argv: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=True,
        timeout=20,
        env=env,
    )


def test_claude_code_cli_auth_confirms_claude_code_route() -> None:
    if shutil.which("claude") is None:
        pytest.skip("Claude Code CLI is not installed on PATH")

    result = _run_status(["claude", "auth", "status"])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        if result.returncode != 0:
            pytest.fail(f"claude auth status failed: {result.stderr or result.stdout}")
        raise
    if payload.get("loggedIn") is not True:
        pytest.skip("Claude Code CLI session auth is not logged in for this execution boundary")

    route = _resolve_authenticated_route(
        kind=CliProfileKind.CLAUDE_CODE,
        provider="claude_code",
        external_cli_kind="claude-code",
        command_name="claude",
        family=ProviderFamily.ANTHROPIC,
    )
    assert route.route_ref == "claude-code:claude"


def test_codex_cli_auth_confirms_codex_route() -> None:
    if shutil.which("codex") is None:
        pytest.skip("Codex CLI is not installed on PATH")

    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    result = _run_status(
        ["codex", "login", "status", "-c", "preferred_auth_method=chatgpt"],
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(f"codex login status failed: {result.stderr or result.stdout}")
    if "ChatGPT" not in f"{result.stdout}\n{result.stderr}":
        pytest.skip("Codex CLI is not logged in through the ChatGPT subscription boundary")

    route = _resolve_authenticated_route(
        kind=CliProfileKind.CODEX,
        provider="codex",
        external_cli_kind="codex",
        command_name="codex",
        family=ProviderFamily.OPENAI,
    )
    assert route.route_ref == "codex:codex"


def test_antigravity_cli_auth_confirms_antigravity_route() -> None:
    if shutil.which("antigravity") is None:
        pytest.skip("Antigravity CLI is not installed on PATH")
    pytest.skip("No non-secret Antigravity auth-status probe is declared for this host")


def test_gemini_legacy_cli_auth_confirms_gemini_legacy_route() -> None:
    if shutil.which("gemini") is None:
        pytest.skip("Gemini CLI is not installed on PATH")
    pytest.skip("No non-secret legacy Gemini auth-status probe is declared for this host")


def test_generic_command_cli_auth_confirms_operator_declared_route() -> None:
    command = os.getenv("U_MEM_24_GENERIC_COMMAND_AUTH_PROBE", "").strip()
    if not command:
        pytest.skip("U_MEM_24_GENERIC_COMMAND_AUTH_PROBE is not set")
    argv = shlex.split(command)
    executable = argv[0]
    if shutil.which(executable) is None:
        pytest.skip(f"generic auth probe executable {executable!r} is not installed on PATH")
    result = _run_status(argv)
    if result.returncode != 0:
        pytest.fail("generic command auth probe failed")

    route = _resolve_authenticated_route(
        kind=CliProfileKind.CUSTOM,
        provider="generic-command",
        external_cli_kind="generic-command",
        command_name="custom",
        family=ProviderFamily.LOCAL_OPEN_WEIGHT,
    )
    assert route.route_ref == "generic-command:custom"
