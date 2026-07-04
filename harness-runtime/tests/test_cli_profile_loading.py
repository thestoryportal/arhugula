"""Tests for U-MEM-18 - CLI profile loading."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_core import DeploymentSurface
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain, ProviderCandidate, ProviderFamily
from harness_cp.memory_access_mode import ExternalCliRoute, MemoryProviderCapabilities
from harness_is.cli_profile import (
    CliImportPolicy,
    CliInstructionSource,
    CliInstructionSourceKind,
    CliMemorySource,
    CliMemorySourceKind,
    CliProfile,
    CliProfileKind,
)
from harness_is.memory_operation_ledger import (
    MemoryOperationEngineClass,
    MemoryOperationKind,
)
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_policy import MemoryPolicyDocument, MemoryPolicyResolver
from harness_is.memory_record_envelope import (
    MemoryRecordKind,
    MemoryScope,
    MemoryVisibility,
)
from harness_is.memory_retrieval import MemoryRetriever
from harness_is.memory_retrieval_index import DerivedRetrievalIndexStore
from harness_is.memory_store import CanonicalMemoryStore
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.cli_profile_loading import (
    CliProfileResolutionError,
    CliProfileResolutionRequest,
    resolve_cli_profile,
)
from harness_runtime.memory_capture import EpisodicMemoryCapture, MemoryCaptureStatus
from harness_runtime.memory_context import (
    MemoryContextCompositionRequest,
    RuntimeMemoryContextComposer,
)

_NOW = datetime(2026, 7, 2, 21, 30, 0, tzinfo=UTC)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="codex")


def _binding(provider: str, model: str = "gpt-5") -> ModelBinding:
    return ModelBinding(provider=provider, model=model)


def _chain(
    provider: str,
    *,
    model: str = "gpt-5",
    family: ProviderFamily = ProviderFamily.OPENAI,
) -> FallbackChain:
    return FallbackChain(
        primary=ProviderCandidate(provider=provider, model=model, family=family),
        same_family=(
            ProviderCandidate(provider=provider, model=f"{model}-fallback", family=family),
        ),
        cross_family=(
            ProviderCandidate(
                provider="anthropic",
                model="claude-haiku-4-5",
                family=ProviderFamily.ANTHROPIC,
            ),
        ),
        terminal=None,
    )


def _route(
    provider_name: str,
    external_cli_kind: str,
    command_name: str,
) -> ExternalCliRoute:
    return ExternalCliRoute(
        provider_name=provider_name,
        external_cli_kind=external_cli_kind,
        command_name=command_name,
        auth_check_passed=True,
        optional=False,
        degradation_allowed=False,
    )


def _instruction_source(path: str = "AGENTS.md") -> CliInstructionSource:
    return CliInstructionSource(
        source_id="project-instructions",
        source_kind=CliInstructionSourceKind.PROJECT_INSTRUCTION,
        path=path,
        required=True,
    )


def _memory_source(*, allow_mutation: bool = False) -> CliMemorySource:
    return CliMemorySource(
        source_id="codex-local-memory",
        source_kind=CliMemorySourceKind.CODEX_LOCAL_MEMORY,
        path=".codex/memories",
        allow_mutation=allow_mutation,
    )


def test_generic_profile_resolves_without_cli_specific_files() -> None:
    binding = _binding("openai")
    fallback_chain = _chain("openai")

    result = resolve_cli_profile(
        CliProfileResolutionRequest(
            model_binding=binding,
            fallback_chain=fallback_chain,
        )
    )

    assert result.profile.kind is CliProfileKind.GENERIC
    assert result.profile.profile_id == "profile:generic"
    assert result.profile.instruction_sources == ()
    assert result.profile.external_memory_sources == ()
    assert result.loaded_instruction_sources == ()
    assert result.external_memory_access == ()
    assert result.external_cli_route is None
    assert result.model_binding == binding
    assert result.fallback_chain == fallback_chain


def test_generic_profile_rejects_declared_cli_sources() -> None:
    with pytest.raises(CliProfileResolutionError, match="generic profile cannot bind"):
        resolve_cli_profile(
            CliProfileResolutionRequest(
                model_binding=_binding("openai"),
                fallback_chain=_chain("openai"),
                profile_kind=CliProfileKind.GENERIC,
                instruction_sources=(_instruction_source(),),
            )
        )


@pytest.mark.parametrize(
    ("kind", "provider", "external_kind", "command", "family"),
    [
        (
            CliProfileKind.CLAUDE_CODE,
            "claude_code",
            "claude-code",
            "claude",
            ProviderFamily.ANTHROPIC,
        ),
        (CliProfileKind.CODEX, "codex", "codex", "codex", ProviderFamily.OPENAI),
        (
            CliProfileKind.ANTIGRAVITY,
            "antigravity",
            "antigravity",
            "antigravity",
            ProviderFamily.GOOGLE,
        ),
        (CliProfileKind.GEMINI_LEGACY, "gemini_legacy", "gemini", "gemini", ProviderFamily.GOOGLE),
        (
            CliProfileKind.CUSTOM,
            "generic-command",
            "generic-command",
            "custom",
            ProviderFamily.LOCAL_OPEN_WEIGHT,
        ),
    ],
)
def test_builtin_profiles_resolve_from_active_external_cli_route(
    kind: CliProfileKind,
    provider: str,
    external_kind: str,
    command: str,
    family: ProviderFamily,
) -> None:
    binding = _binding(provider)
    fallback_chain = _chain(provider, family=family)
    external_route = _route(provider, external_kind, command)

    result = resolve_cli_profile(
        CliProfileResolutionRequest(
            model_binding=binding,
            fallback_chain=fallback_chain,
            external_cli_route=external_route,
            instruction_sources=(_instruction_source(),),
            external_memory_sources=(_memory_source(),),
            import_policy=CliImportPolicy.LEDGERED_IMPORT,
        )
    )

    assert result.profile.kind is kind
    assert result.profile.provider_name == provider
    assert result.profile.external_cli_kind == external_kind
    assert result.profile.command_name == command
    assert result.profile.instruction_sources == (_instruction_source(),)
    assert result.external_cli_route == external_route
    assert result.model_binding == binding
    assert result.fallback_chain == fallback_chain
    [memory_access] = result.external_memory_access
    assert memory_access.read_allowed is True
    assert memory_access.import_allowed is True
    assert memory_access.mutation_allowed is False


@pytest.mark.parametrize(
    ("kind", "provider", "external_kind", "command"),
    [
        (CliProfileKind.CLAUDE_CODE, "claude_code", "claude-code", "claude"),
        (CliProfileKind.CODEX, "codex", "codex", "codex"),
    ],
)
def test_claude_code_and_codex_resolution_require_explicit_source_policy(
    kind: CliProfileKind,
    provider: str,
    external_kind: str,
    command: str,
) -> None:
    with pytest.raises(ValueError, match="requires explicit source declarations"):
        resolve_cli_profile(
            CliProfileResolutionRequest(
                model_binding=_binding(provider),
                fallback_chain=_chain(provider),
                profile_kind=kind,
                external_cli_route=_route(provider, external_kind, command),
            )
        )


def test_instruction_sources_load_under_profile_policy(tmp_path: Path) -> None:
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Use harness memory under explicit Codex policy.\n", encoding="utf-8")
    profile = CliProfile(
        profile_id="profile:custom-codex",
        kind=CliProfileKind.CUSTOM,
        provider_name="generic-command",
        external_cli_kind="generic-command",
        command_name="codex-wrapper",
        instruction_sources=(_instruction_source(),),
        external_memory_sources=(_memory_source(allow_mutation=True),),
        import_policy=CliImportPolicy.BIDIRECTIONAL_SYNC,
    )

    result = resolve_cli_profile(
        CliProfileResolutionRequest(
            model_binding=_binding("generic-command"),
            fallback_chain=_chain("generic-command", family=ProviderFamily.LOCAL_OPEN_WEIGHT),
            profile=profile,
            external_cli_route=_route("generic-command", "generic-command", "codex-wrapper"),
            instruction_root=tmp_path,
        )
    )

    [loaded_source] = result.loaded_instruction_sources
    assert loaded_source.source_id == "project-instructions"
    assert loaded_source.path == "AGENTS.md"
    assert loaded_source.content == "Use harness memory under explicit Codex policy.\n"
    [memory_access] = result.external_memory_access
    assert memory_access.read_allowed is True
    assert memory_access.import_allowed is True
    assert memory_access.mutation_allowed is True
    assert memory_access.denied_reason is None


def test_required_instruction_source_missing_fails_closed(tmp_path: Path) -> None:
    profile = CliProfile(
        profile_id="profile:custom-missing",
        kind=CliProfileKind.CUSTOM,
        provider_name="generic-command",
        external_cli_kind="generic-command",
        command_name="custom",
        instruction_sources=(_instruction_source("missing/AGENTS.md"),),
    )

    with pytest.raises(CliProfileResolutionError, match="required instruction source"):
        resolve_cli_profile(
            CliProfileResolutionRequest(
                model_binding=_binding("generic-command"),
                fallback_chain=_chain("generic-command", family=ProviderFamily.LOCAL_OPEN_WEIGHT),
                profile=profile,
                external_cli_route=_route("generic-command", "generic-command", "custom"),
                instruction_root=tmp_path,
            )
        )


def test_profile_resolution_fails_when_route_would_override_selected_provider() -> None:
    with pytest.raises(CliProfileResolutionError, match="does not match selected provider"):
        resolve_cli_profile(
            CliProfileResolutionRequest(
                model_binding=_binding("openai"),
                fallback_chain=_chain("openai"),
                profile_kind=CliProfileKind.CODEX,
                instruction_sources=(_instruction_source(),),
                external_cli_route=_route("codex", "codex", "codex"),
            )
        )


def test_resolved_cli_profile_threads_into_capture_and_injection_records(
    tmp_path: Path,
) -> None:
    resolved = resolve_cli_profile(
        CliProfileResolutionRequest(
            model_binding=_binding("codex"),
            fallback_chain=_chain("codex"),
            external_cli_route=_route("codex", "codex", "codex"),
            instruction_sources=(_instruction_source(),),
            import_policy=CliImportPolicy.READ_ONLY,
        )
    )
    store = CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    recorder = EpisodicMemoryCapture(
        store=store,
        actor=_ACTOR,
        project="arhugula-v2",
    )

    capture = recorder.capture_run_start(
        run_id="run-u-mem-18",
        workflow_id="memory-substrate",
        thread_id="thread-cli-profile",
        provider_route=("codex:gpt-5",),
        timestamp=_NOW,
        provider=resolved.profile.provider_name,
        model=resolved.model_binding.model,
        cli_profile=resolved.profile.profile_id,
        engine_class=MemoryOperationEngineClass.PURE_PATTERN_NO_ENGINE,
        policy_ref="policy:u-mem-18",
        procedural_snapshot_ref=None,
    )

    assert capture.status is MemoryCaptureStatus.CAPTURED
    assert capture.memory_id is not None
    run_record = store.read_record(
        capture.memory_id,
        MemoryRecordKind.EPISODIC_RUN,
        run_id="run-u-mem-18",
    )
    assert run_record.content["cli_profile"] == "profile:codex"
    assert run_record.envelope.scope.cli_profile == "profile:codex"
    [capture_operation] = store.read_memory_operations()
    assert capture_operation.cli_profile == "profile:codex"

    index_store = DerivedRetrievalIndexStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    index_store.rebuild(indexed_at=_NOW)
    composer = RuntimeMemoryContextComposer(
        retriever=MemoryRetriever(
            store=store,
            index_store=index_store,
            policy_resolver=MemoryPolicyResolver(
                MemoryPolicyDocument(policy_id="policy:u-mem-18", enabled=False)
            ),
            policy_ref="policy:u-mem-18",
        ),
        operation_store=store,
    )
    context = composer.compose_run_start(
        MemoryContextCompositionRequest(
            run_id="run-u-mem-18",
            workflow_id="memory-substrate",
            workload_class="coding-arc",
            query_summary="U-MEM-18 resolved CLI profile provenance",
            model_binding=resolved.model_binding,
            fallback_chain=resolved.fallback_chain,
            cli_profile=resolved.profile,
            workflow_policy=MemoryPolicyDocument(policy_id="policy:u-mem-18", enabled=False),
            token_budget=120,
            record_scope=MemoryScope(
                project="arhugula-v2",
                workflow="memory-substrate",
                workload_class="coding-arc",
                provider_family=ProviderFamily.OPENAI.value,
                cli_profile=resolved.profile.profile_id,
                visibility=MemoryVisibility.WORKFLOW,
            ),
            timestamp=_NOW,
            actor=_ACTOR,
            policy_ref="policy:u-mem-18",
            engine_class=MemoryOperationEngineClass.PURE_PATTERN_NO_ENGINE,
            provider_capabilities=MemoryProviderCapabilities(
                provider="codex",
                model="gpt-5",
                supports_native_memory=False,
                supports_standard_memory_tools=False,
                supports_prompt_extension_packet=True,
            ),
            external_cli_route=resolved.external_cli_route,
        )
    )

    assert context.selection.cli_profile_ref == "profile:codex"
    assert context.external_cli_route_ref == "codex:codex"
    operations = store.read_memory_operations()
    assert operations[-1].operation_kind is MemoryOperationKind.INJECT
    assert operations[-1].cli_profile == "profile:codex"
