"""Tests for U-MEM-05 - CLI profile schema (C-MEM-16)."""

from __future__ import annotations

import importlib

import pytest


def _cli_profile_module():
    return importlib.import_module("harness_is.cli_profile")


def _instruction_source(module: object):
    return module.CliInstructionSource(
        source_id="project-agents",
        source_kind=module.CliInstructionSourceKind.PROJECT_INSTRUCTION,
        path="AGENTS.md",
        required=True,
    )


def _codex_binding() -> dict[str, str]:
    return {
        "provider_name": "codex",
        "external_cli_kind": "codex",
        "command_name": "codex",
        "auth_boundary": "external_cli_session",
    }


def _claude_binding() -> dict[str, str]:
    return {
        "provider_name": "claude_code",
        "external_cli_kind": "claude-code",
        "command_name": "claude",
        "auth_boundary": "external_cli_session",
    }


def test_cli_profile_vocabularies_match_c_mem_16() -> None:
    m = _cli_profile_module()

    assert {kind.value for kind in m.CliProfileKind} == {
        "generic",
        "claude_code",
        "codex",
        "antigravity",
        "gemini_legacy",
        "custom",
    }
    assert {policy.value for policy in m.CliImportPolicy} == {
        "deny",
        "read_only",
        "ledgered_import",
        "bidirectional_sync",
    }


def test_cli_profile_document_declares_c_mem_16_fields() -> None:
    m = _cli_profile_module()

    assert set(m.CliProfile.model_fields) == {
        "schema_version",
        "profile_id",
        "kind",
        "provider_name",
        "external_cli_kind",
        "command_name",
        "auth_boundary",
        "instruction_sources",
        "external_memory_sources",
        "capability_flags",
        "import_policy",
    }
    assert set(m.CliInstructionSource.model_fields) == {
        "source_id",
        "source_kind",
        "path",
        "required",
    }
    assert set(m.CliMemorySource.model_fields) == {
        "source_id",
        "source_kind",
        "path",
        "allow_mutation",
    }


def test_generic_profile_has_no_cli_specific_assumptions() -> None:
    m = _cli_profile_module()

    profile = m.CliProfile(profile_id="profile:generic", kind=m.CliProfileKind.GENERIC)

    assert profile.provider_name is None
    assert profile.external_cli_kind is None
    assert profile.command_name is None
    assert profile.auth_boundary is None
    assert profile.instruction_sources == ()
    assert profile.external_memory_sources == ()
    assert profile.import_policy is m.CliImportPolicy.DENY

    with pytest.raises(ValueError, match="generic profile cannot bind"):
        m.CliProfile(
            profile_id="profile:generic-with-command",
            kind=m.CliProfileKind.GENERIC,
            command_name="codex",
        )


def test_claude_code_and_codex_require_explicit_source_declarations() -> None:
    m = _cli_profile_module()

    with pytest.raises(ValueError, match="requires explicit source declarations"):
        m.CliProfile(
            profile_id="profile:claude",
            kind=m.CliProfileKind.CLAUDE_CODE,
            **_claude_binding(),
        )

    with pytest.raises(ValueError, match="requires explicit source declarations"):
        m.CliProfile(
            profile_id="profile:codex",
            kind=m.CliProfileKind.CODEX,
            **_codex_binding(),
        )

    codex_profile = m.CliProfile(
        profile_id="profile:codex",
        kind=m.CliProfileKind.CODEX,
        instruction_sources=(_instruction_source(m),),
        import_policy=m.CliImportPolicy.READ_ONLY,
        **_codex_binding(),
    )

    assert codex_profile.instruction_sources[0].path == "AGENTS.md"
    assert codex_profile.import_policy is m.CliImportPolicy.READ_ONLY


def test_builtin_profiles_declare_identity_without_provider_order() -> None:
    m = _cli_profile_module()

    assert set(m.BUILT_IN_CLI_PROVIDER_BINDINGS) == {
        m.CliProfileKind.CLAUDE_CODE,
        m.CliProfileKind.CODEX,
        m.CliProfileKind.ANTIGRAVITY,
        m.CliProfileKind.GEMINI_LEGACY,
        m.CliProfileKind.CUSTOM,
    }
    assert m.BUILT_IN_CLI_PROVIDER_BINDINGS[m.CliProfileKind.CLAUDE_CODE].model_dump() == {
        "provider_name": "claude_code",
        "external_cli_kind": "claude-code",
        "command_name": "claude",
        "auth_boundary": "external_cli_session",
    }
    assert m.BUILT_IN_CLI_PROVIDER_BINDINGS[m.CliProfileKind.CODEX].model_dump() == {
        "provider_name": "codex",
        "external_cli_kind": "codex",
        "command_name": "codex",
        "auth_boundary": "external_cli_session",
    }
    assert (
        m.BUILT_IN_CLI_PROVIDER_BINDINGS[m.CliProfileKind.CUSTOM].provider_name == "generic-command"
    )
    assert (
        m.BUILT_IN_CLI_PROVIDER_BINDINGS[m.CliProfileKind.CUSTOM].external_cli_kind
        == "generic-command"
    )
    assert "provider_order" not in m.CliProfile.model_fields
    assert "fallback_chain" not in m.CliProfile.model_fields
    assert "routing_priority" not in m.CliProfile.model_fields


def test_external_memory_source_mutation_requires_bidirectional_import_policy() -> None:
    m = _cli_profile_module()
    source = m.CliMemorySource(
        source_id="codex-local-memory",
        source_kind=m.CliMemorySourceKind.CODEX_LOCAL_MEMORY,
        path=".codex/memories",
        allow_mutation=True,
    )

    with pytest.raises(ValueError, match="bidirectional_sync"):
        m.CliProfile(
            profile_id="profile:codex",
            kind=m.CliProfileKind.CODEX,
            instruction_sources=(_instruction_source(m),),
            external_memory_sources=(source,),
            import_policy=m.CliImportPolicy.READ_ONLY,
            **_codex_binding(),
        )

    profile = m.CliProfile(
        profile_id="profile:codex",
        kind=m.CliProfileKind.CODEX,
        instruction_sources=(_instruction_source(m),),
        external_memory_sources=(source,),
        import_policy=m.CliImportPolicy.BIDIRECTIONAL_SYNC,
        **_codex_binding(),
    )

    assert m.external_memory_source_mutation_allowed(profile) is True
