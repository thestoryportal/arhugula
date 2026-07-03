"""Tests for U-MEM-12 - provider memory access-mode selection."""

from __future__ import annotations

import pytest
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.memory_access_mode import (
    ExternalCliRoute,
    MemoryAccessMode,
    MemoryAccessModeDenialReason,
    MemoryAccessModeRequest,
    MemoryProviderCapabilities,
    reflect_memory_provider_capabilities,
    select_memory_access_mode,
)
from harness_is.cli_profile import (
    DEFAULT_GENERIC_CLI_PROFILE,
    CliInstructionSource,
    CliInstructionSourceKind,
    CliProfileKind,
    built_in_cli_profile,
)
from harness_is.memory_policy import (
    AccessDecision,
    MemoryPolicyDocument,
    ReviewMode,
)
from harness_is.memory_record_envelope import MemoryScope, MemoryVisibility


def _scope() -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow="memory-substrate",
        workload_class="coding-arc",
        provider_family="anthropic",
        cli_profile="codex",
        visibility=MemoryVisibility.WORKFLOW,
    )


def _policy(**overrides: object) -> MemoryPolicyDocument:
    payload = {
        "policy_id": "policy:u-mem-12-test",
        "enabled": True,
        "retrieval_access": AccessDecision.RETRIEVAL_ONLY,
        "injection_access": AccessDecision.PROMPT_PACKET,
        "review_mode": ReviewMode.AUTOMATIC,
        **overrides,
    }
    return MemoryPolicyDocument(**payload)


def _chain(provider: str, model: str, family: ProviderFamily) -> FallbackChain:
    return FallbackChain(
        primary=ProviderCandidate(provider=provider, model=model, family=family),
        same_family=(),
        cross_family=(),
        terminal=None,
    )


def _request(
    *,
    provider: str,
    model: str,
    family: ProviderFamily,
    policy: MemoryPolicyDocument | None = None,
    token_budget: int = 1024,
    capabilities: MemoryProviderCapabilities | None = None,
    external_cli_route: ExternalCliRoute | None = None,
) -> MemoryAccessModeRequest:
    return MemoryAccessModeRequest(
        model_binding=ModelBinding(provider=provider, model=model),
        fallback_chain=_chain(provider, model, family),
        cli_profile=DEFAULT_GENERIC_CLI_PROFILE,
        workflow_policy=policy or _policy(),
        step_policy=None,
        token_budget=token_budget,
        record_scope=_scope(),
        provider_capabilities=capabilities,
        external_cli_route=external_cli_route,
    )


def test_memory_access_mode_vocabulary_matches_c_mem_13() -> None:
    assert {mode.value for mode in MemoryAccessMode} == {
        "native_provider_memory",
        "standard_memory_tools",
        "prompt_extension_packet",
        "no_memory_access",
    }


def test_anthropic_selects_native_memory_when_policy_allows() -> None:
    result = select_memory_access_mode(
        _request(
            provider="anthropic",
            model="claude-opus-4-7",
            family=ProviderFamily.ANTHROPIC,
            policy=_policy(native_memory_access=AccessDecision.NATIVE_PROVIDER),
        )
    )

    assert result.access_mode is MemoryAccessMode.NATIVE_PROVIDER_MEMORY
    assert result.denial_reason is None
    assert result.ledgerable_denial is False
    assert result.selected_provider == "anthropic"
    assert "native_provider_policy_allowed" in result.decision_trace


def test_native_memory_does_not_require_prompt_injection_access() -> None:
    result = select_memory_access_mode(
        _request(
            provider="anthropic",
            model="claude-opus-4-7",
            family=ProviderFamily.ANTHROPIC,
            policy=_policy(
                injection_access=AccessDecision.DENY,
                native_memory_access=AccessDecision.NATIVE_PROVIDER,
            ),
        )
    )

    assert result.access_mode is MemoryAccessMode.NATIVE_PROVIDER_MEMORY
    assert result.denial_reason is None


def test_non_native_tool_provider_selects_standard_memory_tools() -> None:
    result = select_memory_access_mode(
        _request(
            provider="openai",
            model="gpt-5",
            family=ProviderFamily.OPENAI,
            policy=_policy(standard_tool_access=AccessDecision.STANDARD_TOOLS),
        )
    )

    assert result.access_mode is MemoryAccessMode.STANDARD_MEMORY_TOOLS
    assert result.denial_reason is None
    assert "standard_tools_policy_allowed" in result.decision_trace


def test_standard_memory_tools_do_not_require_prompt_injection_access() -> None:
    result = select_memory_access_mode(
        _request(
            provider="openai",
            model="gpt-5",
            family=ProviderFamily.OPENAI,
            policy=_policy(
                injection_access=AccessDecision.DENY,
                standard_tool_access=AccessDecision.STANDARD_TOOLS,
            ),
        )
    )

    assert result.access_mode is MemoryAccessMode.STANDARD_MEMORY_TOOLS
    assert result.denial_reason is None


def test_provider_without_tools_selects_prompt_extension_packet() -> None:
    result = select_memory_access_mode(
        _request(
            provider="minimal",
            model="plain-text",
            family=ProviderFamily.LOCAL_OPEN_WEIGHT,
            capabilities=MemoryProviderCapabilities(
                provider="minimal",
                model="plain-text",
                supports_native_memory=False,
                supports_standard_memory_tools=False,
                supports_prompt_extension_packet=True,
            ),
            policy=_policy(injection_access=AccessDecision.PROMPT_PACKET),
        )
    )

    assert result.access_mode is MemoryAccessMode.PROMPT_EXTENSION_PACKET
    assert result.denial_reason is None
    assert "prompt_packet_policy_allowed" in result.decision_trace


def test_provider_capability_override_must_match_binding() -> None:
    with pytest.raises(ValueError, match="provider_capabilities must match model_binding"):
        _request(
            provider="openai",
            model="gpt-5",
            family=ProviderFamily.OPENAI,
            capabilities=MemoryProviderCapabilities(
                provider="minimal",
                model="plain-text",
                supports_native_memory=False,
                supports_standard_memory_tools=False,
                supports_prompt_extension_packet=True,
            ),
        )


def test_policy_denial_is_explicit_and_ledgerable() -> None:
    result = select_memory_access_mode(
        _request(
            provider="openai",
            model="gpt-5",
            family=ProviderFamily.OPENAI,
            policy=MemoryPolicyDocument(policy_id="policy:disabled", enabled=False),
        )
    )

    assert result.access_mode is MemoryAccessMode.NO_MEMORY_ACCESS
    assert result.denial_reason is MemoryAccessModeDenialReason.POLICY_DENIED
    assert result.ledgerable_denial is True
    assert "policy_denied" in result.decision_trace


def test_external_cli_route_fields_participate_in_selection() -> None:
    profile = built_in_cli_profile(
        CliProfileKind.CODEX,
        instruction_sources=(
            CliInstructionSource(
                source_id="agents",
                source_kind=CliInstructionSourceKind.PROJECT_INSTRUCTION,
                path="AGENTS.md",
            ),
        ),
    )
    route = ExternalCliRoute(
        provider_name="codex",
        external_cli_kind="codex",
        command_name="codex",
        auth_check_passed=True,
        optional=False,
        degradation_allowed=False,
    )

    result = select_memory_access_mode(
        MemoryAccessModeRequest(
            model_binding=ModelBinding(provider="codex", model="gpt-5"),
            fallback_chain=_chain("codex", "gpt-5", ProviderFamily.OPENAI),
            cli_profile=profile,
            workflow_policy=_policy(injection_access=AccessDecision.PROMPT_PACKET),
            step_policy=None,
            token_budget=512,
            record_scope=_scope(),
            provider_capabilities=MemoryProviderCapabilities(
                provider="codex",
                model="gpt-5",
                supports_native_memory=False,
                supports_standard_memory_tools=False,
                supports_prompt_extension_packet=True,
            ),
            external_cli_route=route,
        )
    )

    assert result.access_mode is MemoryAccessMode.PROMPT_EXTENSION_PACKET
    assert result.external_cli_route_ref == "codex:codex"
    assert "external_cli_route:codex:codex:auth_ok" in result.decision_trace


def test_optional_external_cli_auth_failure_denies_memory_without_tokens() -> None:
    route = ExternalCliRoute(
        provider_name="codex",
        external_cli_kind="codex",
        command_name="codex",
        auth_check_passed=False,
        optional=True,
        degradation_allowed=True,
    )

    result = select_memory_access_mode(
        _request(
            provider="codex",
            model="gpt-5",
            family=ProviderFamily.OPENAI,
            external_cli_route=route,
        )
    )

    assert result.access_mode is MemoryAccessMode.NO_MEMORY_ACCESS
    assert result.denial_reason is MemoryAccessModeDenialReason.EXTERNAL_CLI_AUTH_UNAVAILABLE
    assert result.ledgerable_denial is True
    assert result.external_cli_route_ref == "codex:codex"


def test_reflect_memory_provider_capabilities_is_deterministic() -> None:
    first = reflect_memory_provider_capabilities(ModelBinding(provider="openai", model="gpt-5"))
    second = reflect_memory_provider_capabilities(ModelBinding(provider="openai", model="gpt-5"))

    assert first == second
    assert first.supports_standard_memory_tools is True
    assert first.supports_native_memory is False
