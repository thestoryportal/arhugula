"""Runtime CLI profile loading - U-MEM-18."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Self

from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain
from harness_cp.memory_access_mode import ExternalCliRoute
from harness_is.cli_profile import (
    BUILT_IN_CLI_PROVIDER_BINDINGS,
    DEFAULT_GENERIC_CLI_PROFILE,
    CliImportPolicy,
    CliInstructionSource,
    CliInstructionSourceKind,
    CliMemorySource,
    CliMemorySourceKind,
    CliProfile,
    CliProfileKind,
    built_in_cli_profile,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CliProfileResolutionError(ValueError):
    """Raised when CLI profile loading would violate route or source policy."""


def _empty_instruction_sources() -> tuple[CliInstructionSource, ...]:
    return ()


def _empty_memory_sources() -> tuple[CliMemorySource, ...]:
    return ()


def _empty_capability_flags() -> tuple[str, ...]:
    return ()


class CliProfileResolutionRequest(BaseModel):
    """Inputs for resolving the active runtime CLI profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    model_binding: ModelBinding
    fallback_chain: FallbackChain
    profile_kind: CliProfileKind | None = None
    profile: CliProfile | None = None
    external_cli_route: ExternalCliRoute | None = None
    instruction_sources: tuple[CliInstructionSource, ...] = Field(
        default_factory=_empty_instruction_sources
    )
    external_memory_sources: tuple[CliMemorySource, ...] = Field(
        default_factory=_empty_memory_sources
    )
    capability_flags: tuple[str, ...] = Field(default_factory=_empty_capability_flags)
    import_policy: CliImportPolicy = CliImportPolicy.DENY
    profile_id: str | None = None
    instruction_root: Path | None = None

    @field_validator("profile_id")
    @classmethod
    def _non_empty_profile_id(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("profile_id cannot be empty")
        return value

    @model_validator(mode="after")
    def _one_profile_source(self) -> Self:
        if self.profile is not None and self.profile_kind is not None:
            raise ValueError("profile and profile_kind are mutually exclusive")
        return self


class LoadedCliInstructionSource(BaseModel):
    """One declared instruction source loaded under the active profile policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_kind: CliInstructionSourceKind
    path: str
    required: bool
    content: str
    content_sha256: str


class CliExternalMemorySourceAccess(BaseModel):
    """Read/import/mutation guard decision for one external CLI memory source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_kind: CliMemorySourceKind
    path: str
    read_allowed: bool
    import_allowed: bool
    mutation_allowed: bool
    import_policy: CliImportPolicy
    denied_reason: str | None = None


class CliProfileResolution(BaseModel):
    """Resolved CLI profile plus provenance consumed by capture/retrieval/injection."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    profile: CliProfile
    model_binding: ModelBinding
    fallback_chain: FallbackChain
    external_cli_route: ExternalCliRoute | None = None
    loaded_instruction_sources: tuple[LoadedCliInstructionSource, ...] = ()
    external_memory_access: tuple[CliExternalMemorySourceAccess, ...] = ()


def resolve_cli_profile(request: CliProfileResolutionRequest) -> CliProfileResolution:
    """Resolve the runtime CLI profile without changing provider or fallback order."""

    _validate_route_matches_binding(request)
    profile = _resolve_profile(request)
    _validate_profile_matches_route(profile, request.external_cli_route)
    return CliProfileResolution(
        profile=profile,
        model_binding=request.model_binding,
        fallback_chain=request.fallback_chain,
        external_cli_route=request.external_cli_route,
        loaded_instruction_sources=_load_instruction_sources(
            profile,
            instruction_root=request.instruction_root,
        ),
        external_memory_access=_external_memory_access(profile),
    )


def _resolve_profile(request: CliProfileResolutionRequest) -> CliProfile:
    if request.profile is not None:
        return request.profile
    kind = request.profile_kind or _profile_kind_from_route(request.external_cli_route)
    if kind is CliProfileKind.GENERIC:
        if request.instruction_sources or request.external_memory_sources:
            raise CliProfileResolutionError("generic profile cannot bind CLI-specific sources")
        if (
            not request.profile_id
            and not request.capability_flags
            and request.import_policy is CliImportPolicy.DENY
        ):
            return DEFAULT_GENERIC_CLI_PROFILE
    return built_in_cli_profile(
        kind,
        profile_id=request.profile_id,
        instruction_sources=request.instruction_sources,
        external_memory_sources=request.external_memory_sources,
        capability_flags=request.capability_flags,
        import_policy=request.import_policy,
    )


def _profile_kind_from_route(route: ExternalCliRoute | None) -> CliProfileKind:
    if route is None:
        return CliProfileKind.GENERIC
    for kind, binding in BUILT_IN_CLI_PROVIDER_BINDINGS.items():
        if kind is CliProfileKind.CUSTOM:
            if (
                route.provider_name == binding.provider_name
                and route.external_cli_kind == binding.external_cli_kind
            ):
                return kind
            continue
        if (
            route.provider_name == binding.provider_name
            and route.external_cli_kind == binding.external_cli_kind
            and route.command_name == binding.command_name
        ):
            return kind
    raise CliProfileResolutionError(
        f"external CLI route {route.route_ref!r} is not a known CLI profile route"
    )


def _validate_route_matches_binding(request: CliProfileResolutionRequest) -> None:
    route = request.external_cli_route
    if route is None:
        return
    if route.provider_name != request.model_binding.provider:
        raise CliProfileResolutionError(
            "external CLI route does not match selected provider "
            f"{request.model_binding.provider!r}"
        )


def _validate_profile_matches_route(
    profile: CliProfile,
    route: ExternalCliRoute | None,
) -> None:
    if route is None or profile.kind is CliProfileKind.GENERIC:
        return
    expected = (profile.provider_name, profile.external_cli_kind, profile.command_name)
    observed = (route.provider_name, route.external_cli_kind, route.command_name)
    if expected != observed:
        raise CliProfileResolutionError(
            f"CLI profile {profile.profile_id!r} does not match active route {route.route_ref!r}"
        )


def _load_instruction_sources(
    profile: CliProfile,
    *,
    instruction_root: Path | None,
) -> tuple[LoadedCliInstructionSource, ...]:
    if instruction_root is None:
        return ()

    root = instruction_root.resolve(strict=False)
    loaded: list[LoadedCliInstructionSource] = []
    for source in profile.instruction_sources:
        source_path = _resolve_declared_source_path(root, source)
        if not source_path.exists():
            if source.required:
                raise CliProfileResolutionError(
                    f"required instruction source {source.path!r} does not exist"
                )
            continue
        if not source_path.is_file():
            raise CliProfileResolutionError(
                f"instruction source {source.path!r} is not a regular file"
            )
        content = source_path.read_text(encoding="utf-8")
        loaded.append(
            LoadedCliInstructionSource(
                source_id=source.source_id,
                source_kind=source.source_kind,
                path=source.path,
                required=source.required,
                content=content,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(loaded)


def _resolve_declared_source_path(root: Path, source: CliInstructionSource) -> Path:
    declared_path = Path(source.path)
    source_path = declared_path if declared_path.is_absolute() else root / declared_path
    resolved = source_path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise CliProfileResolutionError(
            f"instruction source {source.path!r} escapes instruction root"
        )
    return resolved


def _external_memory_access(
    profile: CliProfile,
) -> tuple[CliExternalMemorySourceAccess, ...]:
    return tuple(
        _memory_source_access(source, profile.import_policy)
        for source in profile.external_memory_sources
    )


def _memory_source_access(
    source: CliMemorySource,
    import_policy: CliImportPolicy,
) -> CliExternalMemorySourceAccess:
    read_allowed = import_policy in {
        CliImportPolicy.READ_ONLY,
        CliImportPolicy.LEDGERED_IMPORT,
        CliImportPolicy.BIDIRECTIONAL_SYNC,
    }
    import_allowed = import_policy in {
        CliImportPolicy.LEDGERED_IMPORT,
        CliImportPolicy.BIDIRECTIONAL_SYNC,
    }
    mutation_allowed = import_policy is CliImportPolicy.BIDIRECTIONAL_SYNC and source.allow_mutation
    return CliExternalMemorySourceAccess(
        source_id=source.source_id,
        source_kind=source.source_kind,
        path=source.path,
        read_allowed=read_allowed,
        import_allowed=import_allowed,
        mutation_allowed=mutation_allowed,
        import_policy=import_policy,
        denied_reason=None if read_allowed else "import_policy_deny",
    )


__all__ = [
    "CliExternalMemorySourceAccess",
    "CliProfileResolution",
    "CliProfileResolutionError",
    "CliProfileResolutionRequest",
    "LoadedCliInstructionSource",
    "resolve_cli_profile",
]
