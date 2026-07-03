"""Tests for U-MEM-24 - C-MEM-20 cross-provider and CLI verification suite."""

from __future__ import annotations

from pathlib import Path

from harness_cp.memory_access_mode import MemoryAccessMode
from harness_is.cli_profile import CliProfileKind
from harness_runtime.memory_verification_suite import (
    ACCESS_MODE_VERIFICATION_SCENARIOS,
    C_MEM_20_REQUIREMENT_IDS,
    CLI_PROFILE_VERIFICATION_SCENARIOS,
    EXTERNAL_CLI_ROUTING_SCENARIOS,
    LIVE_CREDENTIAL_GATES,
    VerificationEvidenceLane,
    memory_verification_matrix,
)

_WORKTREE_ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_C_MEM_20_REQUIREMENTS = {
    "schema_validation",
    "path_traversal_rejection",
    "append_only_ledger_hash_chain",
    "concurrent_writer_no_fork",
    "promotion_policy",
    "memory_poisoning",
    "compaction_safety",
    "retrieval_determinism",
    "cross_scope_cross_tenant_denial",
    "prompt_packet_fallback",
    "standard_memory_tools",
    "native_anthropic_adapter",
    "cli_profile_resolution",
    "engine_class_durability",
    "redaction_tombstone_exclusion",
}


def test_c_mem_20_matrix_covers_every_required_verification_item() -> None:
    matrix = memory_verification_matrix()
    requirements_by_id = {item.requirement_id: item for item in matrix.requirements}

    assert set(C_MEM_20_REQUIREMENT_IDS) == _EXPECTED_C_MEM_20_REQUIREMENTS
    assert set(requirements_by_id) == _EXPECTED_C_MEM_20_REQUIREMENTS
    assert matrix.unit_ref == "U-MEM-24"
    assert matrix.contract_ref == "C-MEM-20"

    for requirement in matrix.requirements:
        assert "C-MEM-20" in requirement.contract_refs
        assert requirement.deterministic_selectors
        assert all(
            selector.lane is VerificationEvidenceLane.DETERMINISTIC
            for selector in requirement.deterministic_selectors
        )
        assert all(selector.provider_free for selector in requirement.deterministic_selectors)


def test_deterministic_selectors_point_at_existing_provider_free_tests() -> None:
    matrix = memory_verification_matrix()

    for selector in matrix.deterministic_selectors():
        assert selector.provider_free is True
        assert selector.lane is VerificationEvidenceLane.DETERMINISTIC
        assert (_WORKTREE_ROOT / selector.file_path).exists(), selector.pytest_selector
        assert "integration/test_r830_memory_tool_s3_live_e2e.py" not in selector.file_path
        assert "integration/test_track_b_e2e.py" not in selector.file_path


def test_access_mode_scenarios_cover_native_tools_prompt_and_denial() -> None:
    scenarios_by_mode = {
        scenario.access_mode: scenario for scenario in ACCESS_MODE_VERIFICATION_SCENARIOS
    }

    assert set(scenarios_by_mode) == {
        MemoryAccessMode.NATIVE_PROVIDER_MEMORY,
        MemoryAccessMode.STANDARD_MEMORY_TOOLS,
        MemoryAccessMode.PROMPT_EXTENSION_PACKET,
        MemoryAccessMode.NO_MEMORY_ACCESS,
    }
    assert scenarios_by_mode[MemoryAccessMode.NATIVE_PROVIDER_MEMORY].provider == "anthropic"
    assert (
        scenarios_by_mode[MemoryAccessMode.STANDARD_MEMORY_TOOLS].requirement_id
        == "standard_memory_tools"
    )
    assert (
        scenarios_by_mode[MemoryAccessMode.PROMPT_EXTENSION_PACKET].requirement_id
        == "prompt_packet_fallback"
    )
    assert (
        scenarios_by_mode[MemoryAccessMode.NO_MEMORY_ACCESS].requirement_id
        == "cross_scope_cross_tenant_denial"
    )


def test_cli_profile_scenarios_cover_all_specified_profiles() -> None:
    scenarios_by_kind = {
        scenario.profile_kind: scenario for scenario in CLI_PROFILE_VERIFICATION_SCENARIOS
    }

    assert set(scenarios_by_kind) == set(CliProfileKind)
    assert scenarios_by_kind[CliProfileKind.GENERIC].provider == "openai"
    assert scenarios_by_kind[CliProfileKind.CLAUDE_CODE].route_ref == "claude-code:claude"
    assert scenarios_by_kind[CliProfileKind.CODEX].route_ref == "codex:codex"
    assert scenarios_by_kind[CliProfileKind.ANTIGRAVITY].route_ref == ("antigravity:antigravity")
    assert scenarios_by_kind[CliProfileKind.GEMINI_LEGACY].route_ref == "gemini:gemini"
    assert scenarios_by_kind[CliProfileKind.CUSTOM].route_ref == "generic-command:custom"


def test_external_cli_routes_separate_fake_subprocess_from_live_credential_gates() -> None:
    gates_by_id = {gate.gate_id: gate for gate in LIVE_CREDENTIAL_GATES}
    scenarios_by_route = {
        scenario.route_ref: scenario for scenario in EXTERNAL_CLI_ROUTING_SCENARIOS
    }

    assert set(scenarios_by_route) == {
        "claude-code:claude",
        "codex:codex",
        "antigravity:antigravity",
        "gemini:gemini",
        "generic-command:custom",
    }

    for scenario in EXTERNAL_CLI_ROUTING_SCENARIOS:
        fake_selector = scenario.deterministic_subprocess_selector
        assert fake_selector.lane is VerificationEvidenceLane.DETERMINISTIC
        assert fake_selector.provider_free is True
        assert scenario.live_gate_id in gates_by_id

        live_gate = gates_by_id[scenario.live_gate_id]
        assert live_gate.lane is VerificationEvidenceLane.LIVE_CREDENTIAL_GATED
        assert live_gate.required_operator_surface
        assert live_gate.required_secret_names or live_gate.required_auth_boundaries
        assert "sk-" not in live_gate.resume_command
