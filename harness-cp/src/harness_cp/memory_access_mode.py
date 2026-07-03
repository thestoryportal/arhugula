"""Provider memory access-mode selection - U-MEM-12."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from harness_is.cli_profile import CliProfile
from harness_is.memory_policy import AccessDecision, MemoryPolicyDocument
from harness_is.memory_record_envelope import MemoryScope
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain, ProviderFamily
from harness_cp.provider_capabilities import reflect_provider_capabilities


class MemoryAccessMode(StrEnum):
    """C-MEM-13 provider memory access modes."""

    NATIVE_PROVIDER_MEMORY = "native_provider_memory"
    STANDARD_MEMORY_TOOLS = "standard_memory_tools"
    PROMPT_EXTENSION_PACKET = "prompt_extension_packet"
    NO_MEMORY_ACCESS = "no_memory_access"


class MemoryAccessModeDenialReason(StrEnum):
    """Explicit ledgerable denial reasons for memory access-mode selection."""

    POLICY_DENIED = "policy_denied"
    TOKEN_BUDGET_EMPTY = "token_budget_empty"
    EXTERNAL_CLI_AUTH_UNAVAILABLE = "external_cli_auth_unavailable"
    EXTERNAL_CLI_ROUTE_MISMATCH = "external_cli_route_mismatch"
    NO_SUPPORTED_MODE = "no_supported_mode"


class MemoryProviderCapabilities(BaseModel):
    """Memory-specific projection of provider capability reflection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    supports_native_memory: bool
    supports_standard_memory_tools: bool
    supports_prompt_extension_packet: bool

    @field_validator("provider", "model")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not value:
            raise ValueError("provider capability fields cannot be empty")
        return value


class ExternalCliRoute(BaseModel):
    """External CLI route metadata used after provider construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    external_cli_kind: str
    command_name: str
    auth_check_passed: bool
    optional: bool
    degradation_allowed: bool

    @field_validator("provider_name", "external_cli_kind", "command_name")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not value:
            raise ValueError("external CLI route fields cannot be empty")
        return value

    @property
    def route_ref(self) -> str:
        """Stable token-free route reference for traces and ledgers."""

        return f"{self.external_cli_kind}:{self.command_name}"


class MemoryAccessModeRequest(BaseModel):
    """Inputs for C-MEM-13 memory access-mode selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_binding: ModelBinding
    fallback_chain: FallbackChain
    cli_profile: CliProfile
    workflow_policy: MemoryPolicyDocument
    step_policy: MemoryPolicyDocument | None = None
    token_budget: int = Field(ge=0)
    record_scope: MemoryScope
    provider_capabilities: MemoryProviderCapabilities | None = None
    external_cli_route: ExternalCliRoute | None = None

    @model_validator(mode="after")
    def _capabilities_match_binding(self) -> MemoryAccessModeRequest:
        if self.provider_capabilities is None:
            return self
        if (
            self.provider_capabilities.provider != self.model_binding.provider
            or self.provider_capabilities.model != self.model_binding.model
        ):
            raise ValueError("provider_capabilities must match model_binding")
        return self


class MemoryAccessModeSelection(BaseModel):
    """Selected access mode plus explicit trace and denial state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_mode: MemoryAccessMode
    selected_provider: str
    selected_model: str
    selected_family: ProviderFamily
    fallback_primary: str
    cli_profile_ref: str
    external_cli_route_ref: str | None = None
    denial_reason: MemoryAccessModeDenialReason | None = None
    ledgerable_denial: bool = False
    decision_trace: tuple[str, ...]


def reflect_memory_provider_capabilities(
    model_binding: ModelBinding,
) -> MemoryProviderCapabilities:
    """Reflect native/tool/prompt memory capability for a model binding."""

    provider_capabilities = reflect_provider_capabilities(
        model_binding.provider,
        model_binding.model,
    )
    is_anthropic = model_binding.provider == "anthropic"
    return MemoryProviderCapabilities(
        provider=model_binding.provider,
        model=model_binding.model,
        supports_native_memory=is_anthropic,
        supports_standard_memory_tools=provider_capabilities.supports_tools and not is_anthropic,
        supports_prompt_extension_packet=True,
    )


def select_memory_access_mode(
    request: MemoryAccessModeRequest,
) -> MemoryAccessModeSelection:
    """Select the C-MEM-13 memory access mode without touching credentials."""

    capabilities = request.provider_capabilities or reflect_memory_provider_capabilities(
        request.model_binding
    )
    trace = _base_trace(request, capabilities)
    external_route_ref: str | None = None
    if request.external_cli_route is not None:
        route_result = _external_cli_route_trace(request.external_cli_route, request)
        external_route_ref = request.external_cli_route.route_ref
        trace.extend(route_result.trace)
        if route_result.denial_reason is not None:
            return _denied(
                request,
                trace=trace,
                external_cli_route_ref=external_route_ref,
                reason=route_result.denial_reason,
            )

    if _policy_denies(request.workflow_policy, request.step_policy):
        trace.append(MemoryAccessModeDenialReason.POLICY_DENIED.value)
        return _denied(
            request,
            trace=trace,
            external_cli_route_ref=external_route_ref,
            reason=MemoryAccessModeDenialReason.POLICY_DENIED,
        )

    if capabilities.supports_standard_memory_tools and _policy_allows_standard_tools(
        request.workflow_policy,
        request.step_policy,
    ):
        trace.append("standard_tools_policy_allowed")
        return _selected(
            request,
            trace=trace,
            external_cli_route_ref=external_route_ref,
            access_mode=MemoryAccessMode.STANDARD_MEMORY_TOOLS,
        )

    prompt_denial_reason: MemoryAccessModeDenialReason | None = None
    if capabilities.supports_prompt_extension_packet and _policy_allows_prompt_packet(
        request.workflow_policy,
        request.step_policy,
    ):
        if request.token_budget <= 0:
            trace.append(MemoryAccessModeDenialReason.TOKEN_BUDGET_EMPTY.value)
            prompt_denial_reason = MemoryAccessModeDenialReason.TOKEN_BUDGET_EMPTY
        else:
            trace.append("prompt_packet_policy_allowed")
            return _selected(
                request,
                trace=trace,
                external_cli_route_ref=external_route_ref,
                access_mode=MemoryAccessMode.PROMPT_EXTENSION_PACKET,
            )

    if capabilities.supports_native_memory and _policy_allows_native(
        request.workflow_policy,
        request.step_policy,
    ):
        trace.append("native_provider_policy_allowed")
        return _selected(
            request,
            trace=trace,
            external_cli_route_ref=external_route_ref,
            access_mode=MemoryAccessMode.NATIVE_PROVIDER_MEMORY,
        )

    if prompt_denial_reason is not None:
        return _denied(
            request,
            trace=trace,
            external_cli_route_ref=external_route_ref,
            reason=prompt_denial_reason,
        )

    trace.append(MemoryAccessModeDenialReason.NO_SUPPORTED_MODE.value)
    return _denied(
        request,
        trace=trace,
        external_cli_route_ref=external_route_ref,
        reason=MemoryAccessModeDenialReason.NO_SUPPORTED_MODE,
    )


class _ExternalRouteTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace: tuple[str, ...]
    denial_reason: MemoryAccessModeDenialReason | None = None


def _external_cli_route_trace(
    route: ExternalCliRoute,
    request: MemoryAccessModeRequest,
) -> _ExternalRouteTrace:
    trace = [f"external_cli_route:{route.route_ref}:auth_ok"]
    if route.provider_name != request.model_binding.provider:
        return _ExternalRouteTrace(
            trace=(f"external_cli_route:{route.route_ref}:provider_mismatch",),
            denial_reason=MemoryAccessModeDenialReason.EXTERNAL_CLI_ROUTE_MISMATCH,
        )
    if not route.auth_check_passed:
        disposition = "optional" if route.optional else "required"
        degradation = "degradable" if route.degradation_allowed else "hard"
        return _ExternalRouteTrace(
            trace=(
                f"external_cli_route:{route.route_ref}:auth_unavailable:{disposition}:{degradation}",
            ),
            denial_reason=MemoryAccessModeDenialReason.EXTERNAL_CLI_AUTH_UNAVAILABLE,
        )
    return _ExternalRouteTrace(trace=tuple(trace))


def _base_trace(
    request: MemoryAccessModeRequest,
    capabilities: MemoryProviderCapabilities,
) -> list[str]:
    return [
        f"provider:{request.model_binding.provider}",
        f"model:{request.model_binding.model}",
        f"fallback_primary:{_fallback_primary(request.fallback_chain)}",
        f"cli_profile:{request.cli_profile.profile_id}",
        f"record_scope:{request.record_scope.visibility.value}",
        f"capabilities:native={_bool_token(capabilities.supports_native_memory)}",
        f"capabilities:tools={_bool_token(capabilities.supports_standard_memory_tools)}",
        f"capabilities:prompt={_bool_token(capabilities.supports_prompt_extension_packet)}",
    ]


def _policy_denies(
    workflow_policy: MemoryPolicyDocument,
    step_policy: MemoryPolicyDocument | None,
) -> bool:
    return any(
        (not policy.enabled or policy.retrieval_access is AccessDecision.DENY)
        for policy in _policies(workflow_policy, step_policy)
    )


def _policy_allows_native(
    workflow_policy: MemoryPolicyDocument,
    step_policy: MemoryPolicyDocument | None,
) -> bool:
    return _all_policies_match(
        workflow_policy,
        step_policy,
        field_name="native_memory_access",
        expected=AccessDecision.NATIVE_PROVIDER,
    )


def _policy_allows_standard_tools(
    workflow_policy: MemoryPolicyDocument,
    step_policy: MemoryPolicyDocument | None,
) -> bool:
    return _all_policies_match(
        workflow_policy,
        step_policy,
        field_name="standard_tool_access",
        expected=AccessDecision.STANDARD_TOOLS,
    )


def _policy_allows_prompt_packet(
    workflow_policy: MemoryPolicyDocument,
    step_policy: MemoryPolicyDocument | None,
) -> bool:
    return _all_policies_match(
        workflow_policy,
        step_policy,
        field_name="injection_access",
        expected=AccessDecision.PROMPT_PACKET,
    )


def _all_policies_match(
    workflow_policy: MemoryPolicyDocument,
    step_policy: MemoryPolicyDocument | None,
    *,
    field_name: Literal[
        "injection_access",
        "native_memory_access",
        "standard_tool_access",
    ],
    expected: AccessDecision,
) -> bool:
    return all(
        getattr(policy, field_name) is expected
        for policy in _policies(workflow_policy, step_policy)
    )


def _policies(
    workflow_policy: MemoryPolicyDocument,
    step_policy: MemoryPolicyDocument | None,
) -> tuple[MemoryPolicyDocument, ...]:
    return (workflow_policy,) if step_policy is None else (workflow_policy, step_policy)


def _selected(
    request: MemoryAccessModeRequest,
    *,
    trace: list[str],
    external_cli_route_ref: str | None,
    access_mode: MemoryAccessMode,
) -> MemoryAccessModeSelection:
    return MemoryAccessModeSelection(
        access_mode=access_mode,
        selected_provider=request.model_binding.provider,
        selected_model=request.model_binding.model,
        selected_family=request.fallback_chain.primary.family,
        fallback_primary=_fallback_primary(request.fallback_chain),
        cli_profile_ref=request.cli_profile.profile_id,
        external_cli_route_ref=external_cli_route_ref,
        denial_reason=None,
        ledgerable_denial=False,
        decision_trace=tuple(trace),
    )


def _denied(
    request: MemoryAccessModeRequest,
    *,
    trace: list[str],
    external_cli_route_ref: str | None,
    reason: MemoryAccessModeDenialReason,
) -> MemoryAccessModeSelection:
    return MemoryAccessModeSelection(
        access_mode=MemoryAccessMode.NO_MEMORY_ACCESS,
        selected_provider=request.model_binding.provider,
        selected_model=request.model_binding.model,
        selected_family=request.fallback_chain.primary.family,
        fallback_primary=_fallback_primary(request.fallback_chain),
        cli_profile_ref=request.cli_profile.profile_id,
        external_cli_route_ref=external_cli_route_ref,
        denial_reason=reason,
        ledgerable_denial=True,
        decision_trace=tuple(trace),
    )


def _fallback_primary(fallback_chain: FallbackChain) -> str:
    return f"{fallback_chain.primary.provider}:{fallback_chain.primary.model}"


def _bool_token(value: bool) -> str:
    return "true" if value else "false"


__all__ = [
    "ExternalCliRoute",
    "MemoryAccessMode",
    "MemoryAccessModeDenialReason",
    "MemoryAccessModeRequest",
    "MemoryAccessModeSelection",
    "MemoryProviderCapabilities",
    "reflect_memory_provider_capabilities",
    "select_memory_access_mode",
]
