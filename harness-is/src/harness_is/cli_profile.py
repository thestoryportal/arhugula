"""CLI profile schema - U-MEM-05.

Implements C-MEM-16's provider-neutral CLI profile vocabulary and validation
substrate. This module declares profile data only; runtime CLI loading and
deployed external-provider route mapping remain port-gated follow-on work.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CliProfileKind(StrEnum):
    """CLI profile kinds declared by C-MEM-16."""

    GENERIC = "generic"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    GEMINI_LEGACY = "gemini_legacy"
    CUSTOM = "custom"


class CliImportPolicy(StrEnum):
    """External CLI memory import policies declared by C-MEM-16."""

    DENY = "deny"
    READ_ONLY = "read_only"
    LEDGERED_IMPORT = "ledgered_import"
    BIDIRECTIONAL_SYNC = "bidirectional_sync"


class CliInstructionSourceKind(StrEnum):
    """Instruction/progress source families usable by explicit CLI policy."""

    PROJECT_INSTRUCTION = "project_instruction"
    USER_INSTRUCTION = "user_instruction"
    PROGRESS_STATE = "progress_state"
    CUSTOM = "custom"


class CliMemorySourceKind(StrEnum):
    """External memory source families usable by explicit CLI policy."""

    EXTERNAL_FILE = "external_file"
    EXTERNAL_DIRECTORY = "external_directory"
    CLAUDE_PROGRESS = "claude_progress"
    CODEX_LOCAL_MEMORY = "codex_local_memory"
    CUSTOM = "custom"


class CliProviderBinding(BaseModel):
    """Existing external CLI provider identity binding carried as data only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    external_cli_kind: str
    command_name: str
    auth_boundary: str | None = None

    @field_validator("provider_name", "external_cli_kind", "command_name", "auth_boundary")
    @classmethod
    def _non_empty_optional_string(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("CLI provider binding fields cannot be empty")
        return value


class CliInstructionSource(BaseModel):
    """One explicit instruction/progress source allowed by a CLI profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_kind: CliInstructionSourceKind
    path: str
    required: bool = False

    @field_validator("source_id", "path")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not value:
            raise ValueError("CLI instruction source fields cannot be empty")
        return value


class CliMemorySource(BaseModel):
    """One explicit external memory source allowed by a CLI profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_kind: CliMemorySourceKind
    path: str
    allow_mutation: bool = False

    @field_validator("source_id", "path")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not value:
            raise ValueError("CLI memory source fields cannot be empty")
        return value


def _empty_instruction_sources() -> tuple[CliInstructionSource, ...]:
    return ()


def _empty_memory_sources() -> tuple[CliMemorySource, ...]:
    return ()


def _empty_capability_flags() -> tuple[str, ...]:
    return ()


_SOURCE_REQUIRED_KINDS = {
    CliProfileKind.CLAUDE_CODE,
    CliProfileKind.CODEX,
}


class CliProfile(BaseModel):
    """C-MEM-16 CLI profile schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cli-profile/v1"] = "cli-profile/v1"
    profile_id: str
    kind: CliProfileKind
    provider_name: str | None = None
    external_cli_kind: str | None = None
    command_name: str | None = None
    auth_boundary: str | None = None
    instruction_sources: tuple[CliInstructionSource, ...] = Field(
        default_factory=_empty_instruction_sources
    )
    external_memory_sources: tuple[CliMemorySource, ...] = Field(
        default_factory=_empty_memory_sources
    )
    capability_flags: tuple[str, ...] = Field(default_factory=_empty_capability_flags)
    import_policy: CliImportPolicy = CliImportPolicy.DENY

    @field_validator(
        "profile_id",
        "provider_name",
        "external_cli_kind",
        "command_name",
        "auth_boundary",
        mode="after",
    )
    @classmethod
    def _non_empty_optional_string(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("CLI profile string fields cannot be empty")
        return value

    @field_validator("capability_flags")
    @classmethod
    def _capability_flags_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not flag for flag in value):
            raise ValueError("CLI profile capability flags cannot be empty")
        return value

    @model_validator(mode="after")
    def _validate_profile(self) -> Self:
        binding_fields = (self.provider_name, self.external_cli_kind, self.command_name)
        if self.kind is CliProfileKind.GENERIC and any(
            (
                self.provider_name,
                self.external_cli_kind,
                self.command_name,
                self.auth_boundary,
                self.instruction_sources,
                self.external_memory_sources,
            )
        ):
            raise ValueError("generic profile cannot bind CLI-specific sources or providers")
        if any(binding_fields) and not all(binding_fields):
            raise ValueError("CLI provider identity binding requires provider, kind, and command")
        if self.kind in _SOURCE_REQUIRED_KINDS and not (
            self.instruction_sources or self.external_memory_sources
        ):
            raise ValueError(f"{self.kind.value} requires explicit source declarations")
        if external_memory_source_mutation_allowed(self) and (
            self.import_policy is not CliImportPolicy.BIDIRECTIONAL_SYNC
        ):
            raise ValueError("external memory source mutation requires bidirectional_sync policy")
        return self


BUILT_IN_CLI_PROVIDER_BINDINGS: Mapping[CliProfileKind, CliProviderBinding] = MappingProxyType(
    {
        CliProfileKind.CLAUDE_CODE: CliProviderBinding(
            provider_name="claude_code",
            external_cli_kind="claude-code",
            command_name="claude",
            auth_boundary="external_cli_session",
        ),
        CliProfileKind.CODEX: CliProviderBinding(
            provider_name="codex",
            external_cli_kind="codex",
            command_name="codex",
            auth_boundary="external_cli_session",
        ),
        CliProfileKind.ANTIGRAVITY: CliProviderBinding(
            provider_name="antigravity",
            external_cli_kind="antigravity",
            command_name="antigravity",
            auth_boundary="external_cli_session",
        ),
        CliProfileKind.GEMINI_LEGACY: CliProviderBinding(
            provider_name="gemini_legacy",
            external_cli_kind="gemini",
            command_name="gemini",
            auth_boundary="external_cli_session",
        ),
        CliProfileKind.CUSTOM: CliProviderBinding(
            provider_name="generic-command",
            external_cli_kind="generic-command",
            command_name="custom",
            auth_boundary="operator_declared",
        ),
    }
)
"""Immutable built-in provider bindings for CLI profile provenance."""


def external_memory_source_mutation_allowed(profile: CliProfile) -> bool:
    """Return whether any declared external source asks to mutate its origin."""

    return any(source.allow_mutation for source in profile.external_memory_sources)


DEFAULT_GENERIC_CLI_PROFILE = CliProfile(
    profile_id="profile:generic",
    kind=CliProfileKind.GENERIC,
)
"""Default generic CLI profile with no CLI-specific assumptions."""


def built_in_cli_profile(
    kind: CliProfileKind,
    *,
    profile_id: str | None = None,
    instruction_sources: tuple[CliInstructionSource, ...] = (),
    external_memory_sources: tuple[CliMemorySource, ...] = (),
    capability_flags: tuple[str, ...] = (),
    import_policy: CliImportPolicy = CliImportPolicy.DENY,
) -> CliProfile:
    """Build a profile from the immutable built-in identity binding."""

    if kind is CliProfileKind.GENERIC:
        return CliProfile(
            profile_id=profile_id or "profile:generic",
            kind=kind,
            capability_flags=capability_flags,
            import_policy=import_policy,
        )
    binding = BUILT_IN_CLI_PROVIDER_BINDINGS[kind]
    return CliProfile(
        profile_id=profile_id or f"profile:{kind.value}",
        kind=kind,
        instruction_sources=instruction_sources,
        external_memory_sources=external_memory_sources,
        capability_flags=capability_flags,
        import_policy=import_policy,
        **binding.model_dump(),
    )


__all__ = [
    "BUILT_IN_CLI_PROVIDER_BINDINGS",
    "DEFAULT_GENERIC_CLI_PROFILE",
    "CliImportPolicy",
    "CliInstructionSource",
    "CliInstructionSourceKind",
    "CliMemorySource",
    "CliMemorySourceKind",
    "CliProfile",
    "CliProfileKind",
    "CliProviderBinding",
    "built_in_cli_profile",
    "external_memory_source_mutation_allowed",
]
