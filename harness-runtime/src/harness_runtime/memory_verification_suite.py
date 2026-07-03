"""U-MEM-24 C-MEM-20 memory verification evidence matrix.

This module is intentionally declarative: it names the provider-free checks
that close each C-MEM-20 requirement and keeps live provider or external CLI
auth checks behind explicit gates. It does not execute paid provider calls.
U-MEM-25 uses the same matrix as its closeout evidence index.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from harness_cp.memory_access_mode import MemoryAccessMode
from harness_is.cli_profile import CliProfileKind


class VerificationEvidenceLane(StrEnum):
    """Evidence lanes for C-MEM-20 verification."""

    DETERMINISTIC = "deterministic"
    LIVE_CREDENTIAL_GATED = "live_credential_gated"


@dataclass(frozen=True, slots=True)
class VerificationTestSelector:
    """One provider-free pytest selector contributing to C-MEM-20 evidence."""

    file_path: str
    node_id: str | None = None
    lane: VerificationEvidenceLane = VerificationEvidenceLane.DETERMINISTIC
    provider_free: bool = True

    @property
    def pytest_selector(self) -> str:
        """Return a pytest-compatible selector string."""

        if self.node_id is None:
            return self.file_path
        return f"{self.file_path}::{self.node_id}"


@dataclass(frozen=True, slots=True)
class LiveCredentialGate:
    """One optional live check that must stay outside provider-free tests."""

    gate_id: str
    title: str
    required_operator_surface: str
    deterministic_absence_probe: str
    resume_command: str
    lane: VerificationEvidenceLane = VerificationEvidenceLane.LIVE_CREDENTIAL_GATED
    required_secret_names: tuple[str, ...] = ()
    required_auth_boundaries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationRequirement:
    """A C-MEM-20 required verification item and its evidence."""

    requirement_id: str
    title: str
    contract_refs: tuple[str, ...]
    deterministic_selectors: tuple[VerificationTestSelector, ...]
    live_gates: tuple[LiveCredentialGate, ...] = ()


@dataclass(frozen=True, slots=True)
class AccessModeVerificationScenario:
    """Provider access-mode scenario required by U-MEM-24."""

    scenario_id: str
    requirement_id: str
    access_mode: MemoryAccessMode
    provider: str
    model: str
    deterministic_selector: VerificationTestSelector
    live_gate_id: str | None = None


@dataclass(frozen=True, slots=True)
class CliProfileVerificationScenario:
    """CLI profile resolution scenario required by U-MEM-24."""

    profile_kind: CliProfileKind
    provider: str
    route_ref: str | None
    deterministic_selector: VerificationTestSelector


@dataclass(frozen=True, slots=True)
class ExternalCliRoutingScenario:
    """External CLI route with deterministic fake and live-auth split."""

    route_ref: str
    profile_kind: CliProfileKind
    provider: str
    deterministic_subprocess_selector: VerificationTestSelector
    live_gate_id: str


@dataclass(frozen=True, slots=True)
class MemoryVerificationMatrix:
    """Complete U-MEM-24 evidence index for C-MEM-20."""

    unit_ref: str
    contract_ref: str
    requirements: tuple[VerificationRequirement, ...]

    def deterministic_selectors(self) -> tuple[VerificationTestSelector, ...]:
        """Return unique provider-free selectors in declaration order."""

        selectors: list[VerificationTestSelector] = []
        seen: set[str] = set()
        for requirement in self.requirements:
            for selector in requirement.deterministic_selectors:
                if selector.pytest_selector in seen:
                    continue
                seen.add(selector.pytest_selector)
                selectors.append(selector)
        return tuple(selectors)


def _selector(file_path: str, node_id: str | None = None) -> VerificationTestSelector:
    return VerificationTestSelector(file_path=file_path, node_id=node_id)


_C_MEM_20 = "C-MEM-20"
_U_MEM_24 = "U-MEM-24"

_RECORD_SCHEMA = _selector("harness-is/tests/test_memory_record_envelope.py")
_CLI_SCHEMA = _selector("harness-is/tests/test_cli_profile.py")
_POLICY_SCHEMA = _selector("harness-is/tests/test_memory_policy.py")
_PATH_REGISTRY = _selector("harness-is/tests/test_memory_path_registry.py")
_OPERATION_LEDGER = _selector("harness-is/tests/test_memory_operation_ledger.py")
_RETRIEVAL = _selector("harness-is/tests/test_memory_retrieval.py")
_RETRIEVAL_INDEX = _selector("harness-is/tests/test_memory_retrieval_index.py")
_REDACTION = _selector("harness-is/tests/test_memory_redaction.py")
_ACCESS_MODE = _selector("harness-cp/tests/test_memory_access_mode.py")
_MEMORY_CONTEXT = _selector("harness-runtime/tests/test_memory_context.py")
_MEMORY_TOOLS = _selector("harness-runtime/tests/test_memory_tool_executor.py")
_NATIVE_ADAPTER = _selector("harness-runtime/tests/test_native_memory_adapter.py")
_CLI_LOADING = _selector("harness-runtime/tests/test_cli_profile_loading.py")
_COMPACTION = _selector("harness-runtime/tests/test_memory_compaction_safety.py")
_DURABILITY = _selector("harness-runtime/tests/test_memory_engine_durability.py")
_PROMOTION_CANDIDATES = _selector("harness-runtime/tests/test_memory_promotion_candidates.py")
_PROMOTION_REVIEW = _selector("harness-runtime/tests/test_memory_promotion_review.py")
_TOOL_CONTRACTS = _selector("harness-as/tests/test_memory_tool_contracts.py")


LIVE_CREDENTIAL_GATES: tuple[LiveCredentialGate, ...] = (
    LiveCredentialGate(
        gate_id="live-anthropic-native-memory",
        title="Anthropic native Memory behavior against the hosted provider",
        required_secret_names=("ANTHROPIC_API_KEY",),
        required_operator_surface="codex-credential-gate:U-MEM-24:anthropic-native-memory",
        deterministic_absence_probe="provider-free native adapter callback tests pass locally",
        resume_command=(
            "rtk env UV_CACHE_DIR=/tmp/arhugula-uv-cache uv run pytest "
            "harness-runtime/tests/integration/test_u_mem_24_live_memory.py "
            "-m e2e -k anthropic_native_memory"
        ),
    ),
    LiveCredentialGate(
        gate_id="live-claude-code-cli-auth",
        title="Claude Code external CLI route with authenticated local session",
        required_auth_boundaries=("claude-code external CLI session auth",),
        required_operator_surface="codex-credential-gate:U-MEM-24:claude-code-cli-auth",
        deterministic_absence_probe="subprocess-fake route resolves claude-code:claude",
        resume_command=(
            "rtk env UV_CACHE_DIR=/tmp/arhugula-uv-cache uv run pytest "
            "harness-runtime/tests/integration/test_u_mem_24_live_cli_routes.py "
            "-m e2e -k claude_code"
        ),
    ),
    LiveCredentialGate(
        gate_id="live-codex-cli-auth",
        title="Codex external CLI route with authenticated local session",
        required_auth_boundaries=("codex external CLI session auth",),
        required_operator_surface="codex-credential-gate:U-MEM-24:codex-cli-auth",
        deterministic_absence_probe="subprocess-fake route resolves codex:codex",
        resume_command=(
            "rtk env UV_CACHE_DIR=/tmp/arhugula-uv-cache uv run pytest "
            "harness-runtime/tests/integration/test_u_mem_24_live_cli_routes.py "
            "-m e2e -k codex"
        ),
    ),
    LiveCredentialGate(
        gate_id="live-antigravity-cli-auth",
        title="Antigravity external CLI route with authenticated local session",
        required_auth_boundaries=("antigravity external CLI session auth",),
        required_operator_surface="codex-credential-gate:U-MEM-24:antigravity-cli-auth",
        deterministic_absence_probe="subprocess-fake route resolves antigravity:antigravity",
        resume_command=(
            "rtk env UV_CACHE_DIR=/tmp/arhugula-uv-cache uv run pytest "
            "harness-runtime/tests/integration/test_u_mem_24_live_cli_routes.py "
            "-m e2e -k antigravity"
        ),
    ),
    LiveCredentialGate(
        gate_id="live-gemini-legacy-cli-auth",
        title="Legacy Gemini external CLI route with authenticated local session",
        required_auth_boundaries=("legacy gemini external CLI session auth",),
        required_operator_surface="codex-credential-gate:U-MEM-24:gemini-legacy-cli-auth",
        deterministic_absence_probe="subprocess-fake route resolves gemini:gemini",
        resume_command=(
            "rtk env UV_CACHE_DIR=/tmp/arhugula-uv-cache uv run pytest "
            "harness-runtime/tests/integration/test_u_mem_24_live_cli_routes.py "
            "-m e2e -k gemini_legacy"
        ),
    ),
    LiveCredentialGate(
        gate_id="live-generic-command-cli-auth",
        title="Generic-command external CLI route with operator-declared auth boundary",
        required_auth_boundaries=("operator-declared generic external CLI auth",),
        required_operator_surface="codex-credential-gate:U-MEM-24:generic-command-cli-auth",
        deterministic_absence_probe="subprocess-fake route resolves generic-command:custom",
        resume_command=(
            "rtk env UV_CACHE_DIR=/tmp/arhugula-uv-cache uv run pytest "
            "harness-runtime/tests/integration/test_u_mem_24_live_cli_routes.py "
            "-m e2e -k generic_command"
        ),
    ),
)


_LIVE_GATES_BY_ID = {gate.gate_id: gate for gate in LIVE_CREDENTIAL_GATES}


C_MEM_20_REQUIREMENT_IDS: tuple[str, ...] = (
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
)


ACCESS_MODE_VERIFICATION_SCENARIOS: tuple[AccessModeVerificationScenario, ...] = (
    AccessModeVerificationScenario(
        scenario_id="anthropic-native-memory",
        requirement_id="native_anthropic_adapter",
        access_mode=MemoryAccessMode.NATIVE_PROVIDER_MEMORY,
        provider="anthropic",
        model="claude-haiku-4-5",
        deterministic_selector=_MEMORY_CONTEXT,
        live_gate_id="live-anthropic-native-memory",
    ),
    AccessModeVerificationScenario(
        scenario_id="non-native-standard-tools",
        requirement_id="standard_memory_tools",
        access_mode=MemoryAccessMode.STANDARD_MEMORY_TOOLS,
        provider="openai",
        model="gpt-5",
        deterministic_selector=_MEMORY_TOOLS,
    ),
    AccessModeVerificationScenario(
        scenario_id="prompt-extension-fallback",
        requirement_id="prompt_packet_fallback",
        access_mode=MemoryAccessMode.PROMPT_EXTENSION_PACKET,
        provider="generic-command",
        model="local-model",
        deterministic_selector=_MEMORY_CONTEXT,
    ),
    AccessModeVerificationScenario(
        scenario_id="policy-denied-no-memory",
        requirement_id="cross_scope_cross_tenant_denial",
        access_mode=MemoryAccessMode.NO_MEMORY_ACCESS,
        provider="openai",
        model="gpt-5",
        deterministic_selector=_ACCESS_MODE,
    ),
)


CLI_PROFILE_VERIFICATION_SCENARIOS: tuple[CliProfileVerificationScenario, ...] = (
    CliProfileVerificationScenario(
        profile_kind=CliProfileKind.GENERIC,
        provider="openai",
        route_ref=None,
        deterministic_selector=_CLI_LOADING,
    ),
    CliProfileVerificationScenario(
        profile_kind=CliProfileKind.CLAUDE_CODE,
        provider="claude_code",
        route_ref="claude-code:claude",
        deterministic_selector=_CLI_LOADING,
    ),
    CliProfileVerificationScenario(
        profile_kind=CliProfileKind.CODEX,
        provider="codex",
        route_ref="codex:codex",
        deterministic_selector=_CLI_LOADING,
    ),
    CliProfileVerificationScenario(
        profile_kind=CliProfileKind.ANTIGRAVITY,
        provider="antigravity",
        route_ref="antigravity:antigravity",
        deterministic_selector=_CLI_LOADING,
    ),
    CliProfileVerificationScenario(
        profile_kind=CliProfileKind.GEMINI_LEGACY,
        provider="gemini_legacy",
        route_ref="gemini:gemini",
        deterministic_selector=_CLI_LOADING,
    ),
    CliProfileVerificationScenario(
        profile_kind=CliProfileKind.CUSTOM,
        provider="generic-command",
        route_ref="generic-command:custom",
        deterministic_selector=_CLI_LOADING,
    ),
)


EXTERNAL_CLI_ROUTING_SCENARIOS: tuple[ExternalCliRoutingScenario, ...] = (
    ExternalCliRoutingScenario(
        route_ref="claude-code:claude",
        profile_kind=CliProfileKind.CLAUDE_CODE,
        provider="claude_code",
        deterministic_subprocess_selector=_CLI_LOADING,
        live_gate_id="live-claude-code-cli-auth",
    ),
    ExternalCliRoutingScenario(
        route_ref="codex:codex",
        profile_kind=CliProfileKind.CODEX,
        provider="codex",
        deterministic_subprocess_selector=_CLI_LOADING,
        live_gate_id="live-codex-cli-auth",
    ),
    ExternalCliRoutingScenario(
        route_ref="antigravity:antigravity",
        profile_kind=CliProfileKind.ANTIGRAVITY,
        provider="antigravity",
        deterministic_subprocess_selector=_CLI_LOADING,
        live_gate_id="live-antigravity-cli-auth",
    ),
    ExternalCliRoutingScenario(
        route_ref="gemini:gemini",
        profile_kind=CliProfileKind.GEMINI_LEGACY,
        provider="gemini_legacy",
        deterministic_subprocess_selector=_CLI_LOADING,
        live_gate_id="live-gemini-legacy-cli-auth",
    ),
    ExternalCliRoutingScenario(
        route_ref="generic-command:custom",
        profile_kind=CliProfileKind.CUSTOM,
        provider="generic-command",
        deterministic_subprocess_selector=_CLI_LOADING,
        live_gate_id="live-generic-command-cli-auth",
    ),
)


def memory_verification_matrix() -> MemoryVerificationMatrix:
    """Return the complete U-MEM-24/C-MEM-20 provider-free evidence matrix."""

    return MemoryVerificationMatrix(
        unit_ref=_U_MEM_24,
        contract_ref=_C_MEM_20,
        requirements=(
            VerificationRequirement(
                requirement_id="schema_validation",
                title="Schema validation for every memory record and policy carrier",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(
                    _RECORD_SCHEMA,
                    _CLI_SCHEMA,
                    _POLICY_SCHEMA,
                    _OPERATION_LEDGER,
                    _TOOL_CONTRACTS,
                ),
            ),
            VerificationRequirement(
                requirement_id="path_traversal_rejection",
                title="Path registry traversal rejection",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_PATH_REGISTRY, _NATIVE_ADAPTER),
            ),
            VerificationRequirement(
                requirement_id="append_only_ledger_hash_chain",
                title="Append-only memory-operation ledger and hash-chain validation",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_OPERATION_LEDGER,),
            ),
            VerificationRequirement(
                requirement_id="concurrent_writer_no_fork",
                title="Concurrent writer tests proving ledger streams do not fork",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_OPERATION_LEDGER,),
            ),
            VerificationRequirement(
                requirement_id="promotion_policy",
                title="Promotion policy tests including preference promotion",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_PROMOTION_CANDIDATES, _PROMOTION_REVIEW),
            ),
            VerificationRequirement(
                requirement_id="memory_poisoning",
                title="Model-authored memory cannot become injectable without approval",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(
                    _PROMOTION_CANDIDATES,
                    _PROMOTION_REVIEW,
                    _MEMORY_CONTEXT,
                    _MEMORY_TOOLS,
                ),
            ),
            VerificationRequirement(
                requirement_id="compaction_safety",
                title="Compaction safety with durable candidate disposition",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_COMPACTION,),
            ),
            VerificationRequirement(
                requirement_id="retrieval_determinism",
                title="Retrieval determinism for fixed store, policy, and request",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_RETRIEVAL, _RETRIEVAL_INDEX),
            ),
            VerificationRequirement(
                requirement_id="cross_scope_cross_tenant_denial",
                title="Cross-project, workflow, tenant, provider-family, and CLI denial",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_RETRIEVAL, _POLICY_SCHEMA, _ACCESS_MODE),
            ),
            VerificationRequirement(
                requirement_id="prompt_packet_fallback",
                title="Prompt packet fallback for providers without native memory",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_ACCESS_MODE, _MEMORY_CONTEXT),
            ),
            VerificationRequirement(
                requirement_id="standard_memory_tools",
                title="Standard memory tools on tool-capable non-native provider paths",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_ACCESS_MODE, _MEMORY_TOOLS, _TOOL_CONTRACTS),
            ),
            VerificationRequirement(
                requirement_id="native_anthropic_adapter",
                title="Anthropic native adapter compatibility with /memories behavior",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_ACCESS_MODE, _NATIVE_ADAPTER),
                live_gates=(_LIVE_GATES_BY_ID["live-anthropic-native-memory"],),
            ),
            VerificationRequirement(
                requirement_id="cli_profile_resolution",
                title="CLI profiles for generic, Claude Code, Codex, Antigravity, Gemini, custom",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_CLI_SCHEMA, _CLI_LOADING),
                live_gates=tuple(
                    _LIVE_GATES_BY_ID[scenario.live_gate_id]
                    for scenario in EXTERNAL_CLI_ROUTING_SCENARIOS
                ),
            ),
            VerificationRequirement(
                requirement_id="engine_class_durability",
                title="Engine-class durability for all closed engine classes",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_DURABILITY, _OPERATION_LEDGER),
            ),
            VerificationRequirement(
                requirement_id="redaction_tombstone_exclusion",
                title="Redaction and tombstone exclusion from packets and tools",
                contract_refs=(_C_MEM_20,),
                deterministic_selectors=(_REDACTION, _RETRIEVAL, _MEMORY_TOOLS),
            ),
        ),
    )


__all__ = [
    "ACCESS_MODE_VERIFICATION_SCENARIOS",
    "CLI_PROFILE_VERIFICATION_SCENARIOS",
    "C_MEM_20_REQUIREMENT_IDS",
    "EXTERNAL_CLI_ROUTING_SCENARIOS",
    "LIVE_CREDENTIAL_GATES",
    "AccessModeVerificationScenario",
    "CliProfileVerificationScenario",
    "ExternalCliRoutingScenario",
    "LiveCredentialGate",
    "MemoryVerificationMatrix",
    "VerificationEvidenceLane",
    "VerificationRequirement",
    "VerificationTestSelector",
    "memory_verification_matrix",
]
