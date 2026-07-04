"""Tests for U-MEM-14 - runtime memory context composition."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from harness_core import DeploymentSurface
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain, ProviderCandidate, ProviderFamily
from harness_cp.memory_access_mode import (
    ExternalCliRoute,
    MemoryAccessMode,
    MemoryAccessModeDenialReason,
    MemoryProviderCapabilities,
)
from harness_is.cli_profile import CliProfile, CliProfileKind
from harness_is.memory_operation_ledger import MemoryOperationKind, MemoryOperationProjection
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_policy import (
    AccessDecision,
    MemoryPolicyDocument,
    MemoryPolicyResolver,
    ReviewMode,
)
from harness_is.memory_record_envelope import (
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    MemoryVisibility,
    RedactionState,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_retrieval import MemoryRetriever
from harness_is.memory_retrieval_index import DerivedRetrievalIndexStore
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.memory_context import (
    MemoryContextCompositionRequest,
    RuntimeMemoryContextComposer,
    render_prompt_extension_packet,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_NOW = datetime(2026, 7, 2, 16, 0, 0, tzinfo=UTC)
_POLICY_REF = "policy:u-mem-14"
_RUN_ID = "run-u-mem-14"


def _binding(tmp_path: Path) -> MemoryRootBinding:
    return MemoryRootBinding(default_root=tmp_path / "memory")


def _store(binding: MemoryRootBinding) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _index_store(binding: MemoryRootBinding) -> DerivedRetrievalIndexStore:
    return DerivedRetrievalIndexStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _tracer_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _scope(
    *,
    cli_profile: str = "codex",
    provider_family: str = ProviderFamily.OPENAI.value,
) -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow="memory-substrate",
        workload_class="coding-arc",
        cli_profile=cli_profile,
        provider_family=provider_family,
        visibility=MemoryVisibility.WORKFLOW,
    )


def _record(
    *,
    statement: str,
    kind: MemoryRecordKind = MemoryRecordKind.PREFERENCE,
    cli_profile: str = "codex",
    provider_family: str = ProviderFamily.OPENAI.value,
) -> MemoryStoreRecord:
    content: dict[str, object] = {
        "semantic_kind": kind.value,
        "statement": statement,
        "confidence": "high",
        "source_authority": "operator_direct",
        "status": "active",
        "injection_policy": "prompt_packet_allowed",
        "tags": ["codex", "memory", "workflow"],
        "pinned": True,
        "preference_subject": "operator_workflow",
        "preference_strength": "strong",
        "confirmation_required": False,
    }
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(MemoryTier.SEMANTIC, kind, content_hash),
            schema_version="memory-store-record/v1",
            tier=MemoryTier.SEMANTIC,
            kind=kind,
            created_at=_NOW,
            source_refs=(SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:u-mem-14"),),
            scope=_scope(cli_profile=cli_profile, provider_family=provider_family),
            content_hash=content_hash,
        ),
        content=content,
    )


def _enabled_policy(**overrides: object) -> MemoryPolicyDocument:
    fields: dict[str, object] = {
        "policy_id": _POLICY_REF,
        "enabled": True,
        "retrieval_access": AccessDecision.RETRIEVAL_ONLY,
        "injection_access": AccessDecision.PROMPT_PACKET,
        "review_mode": ReviewMode.AUTOMATIC,
    }
    fields.update(overrides)
    return MemoryPolicyDocument(**fields)


def _profile(profile_id: str = "codex") -> CliProfile:
    return CliProfile(profile_id=profile_id, kind=CliProfileKind.CUSTOM)


def _binding_for(
    *,
    provider: str = "openai",
    model: str = "gpt-5",
) -> ModelBinding:
    return ModelBinding(provider=provider, model=model)


def _chain_for(
    *,
    provider: str = "openai",
    model: str = "gpt-5",
    family: ProviderFamily = ProviderFamily.OPENAI,
) -> FallbackChain:
    return FallbackChain(
        primary=ProviderCandidate(provider=provider, model=model, family=family),
        same_family=(),
        cross_family=(),
    )


def _capabilities(
    *,
    provider: str = "openai",
    model: str = "gpt-5",
    native: bool = False,
    tools: bool = False,
    prompt: bool = True,
) -> MemoryProviderCapabilities:
    return MemoryProviderCapabilities(
        provider=provider,
        model=model,
        supports_native_memory=native,
        supports_standard_memory_tools=tools,
        supports_prompt_extension_packet=prompt,
    )


def _composer(
    tmp_path: Path,
    *,
    policy: MemoryPolicyDocument,
    record: MemoryStoreRecord | None = None,
    tracer_provider: TracerProvider | None = None,
) -> tuple[CanonicalMemoryStore, RuntimeMemoryContextComposer]:
    binding = _binding(tmp_path)
    store = _store(binding)
    if record is not None:
        store.write_record(record)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)
    retriever = MemoryRetriever(
        store=store,
        index_store=index_store,
        policy_resolver=MemoryPolicyResolver(policy),
        policy_ref=_POLICY_REF,
    )
    return store, RuntimeMemoryContextComposer(
        retriever=retriever,
        operation_store=store,
        tracer_provider=tracer_provider,
    )


def _request(
    *,
    workflow_policy: MemoryPolicyDocument,
    provider: str = "openai",
    model: str = "gpt-5",
    cli_profile: CliProfile | None = None,
    fallback_chain: FallbackChain | None = None,
    provider_capabilities: MemoryProviderCapabilities | None = None,
    external_cli_route: ExternalCliRoute | None = None,
    provider_family: str = ProviderFamily.OPENAI.value,
) -> MemoryContextCompositionRequest:
    return MemoryContextCompositionRequest(
        run_id=_RUN_ID,
        workflow_id="memory-substrate",
        workload_class="coding-arc",
        query_summary="codex memory substrate workflow",
        model_binding=_binding_for(provider=provider, model=model),
        fallback_chain=fallback_chain or _chain_for(provider=provider, model=model),
        cli_profile=cli_profile or _profile(),
        workflow_policy=workflow_policy,
        token_budget=120,
        record_scope=_scope(
            cli_profile=(cli_profile or _profile()).profile_id,
            provider_family=provider_family,
        ),
        allowed_kinds=(MemoryRecordKind.PREFERENCE,),
        timestamp=_NOW,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        policy_ref=_POLICY_REF,
        provider_capabilities=provider_capabilities,
        external_cli_route=external_cli_route,
    )


def test_prompt_packet_context_retrieves_packet_and_ledgers_injection(tmp_path: Path) -> None:
    policy = _enabled_policy()
    record = _record(statement="Codex memory substrate workflow should resume from U-MEM-14.")
    store, composer = _composer(tmp_path, policy=policy, record=record)

    context = composer.compose_run_start(
        _request(
            workflow_policy=policy,
            provider_capabilities=_capabilities(prompt=True),
        )
    )

    assert context.access_mode is MemoryAccessMode.PROMPT_EXTENSION_PACKET
    assert context.packet is not None
    assert context.packet_hash == context.packet.packet_hash
    assert context.policy_ref == _POLICY_REF
    assert context.selected_refs == (record.envelope.memory_id,)
    assert context.denial_reason is None

    retrieval_entry, injection_entry = store.read_memory_operations()
    assert retrieval_entry.operation_kind is MemoryOperationKind.RETRIEVE
    assert injection_entry.operation_kind is MemoryOperationKind.INJECT
    assert injection_entry.operation_projection is MemoryOperationProjection.INJECTION_DECISIONS
    assert injection_entry.run_id == _RUN_ID
    assert injection_entry.provider == "openai"
    assert injection_entry.model == "gpt-5"
    assert injection_entry.cli_profile == "codex"
    assert injection_entry.memory_refs == context.selected_refs
    assert injection_entry.policy_ref == _POLICY_REF


def test_context_composer_emits_c_mem_19_spans_for_retrieval_packet_and_injection(
    tmp_path: Path,
) -> None:
    tracer_provider, exporter = _tracer_provider()
    policy = _enabled_policy()
    record = _record(statement="Memory observability should cover packet assembly.")
    _, composer = _composer(
        tmp_path,
        policy=policy,
        record=record,
        tracer_provider=tracer_provider,
    )

    context = composer.compose_run_start(
        _request(
            workflow_policy=policy,
            provider_capabilities=_capabilities(prompt=True),
        )
    )

    attrs_by_name = {
        span.attributes["memory.operation.name"]: dict(span.attributes or {})
        for span in exporter.get_finished_spans()
        if span.name == "memory.operation"
    }
    assert {
        "retrieval",
        "ranking",
        "packet_assembly",
        "injection",
    } <= set(attrs_by_name)
    retrieval = attrs_by_name["retrieval"]
    assert retrieval["memory.packet_hash"] == context.packet_hash
    assert retrieval["memory.record_count"] == 1
    assert retrieval["memory.access_mode"] == MemoryAccessMode.PROMPT_EXTENSION_PACKET.value
    assert retrieval["memory.provider"] == "openai"
    assert retrieval["memory.model"] == "gpt-5"
    assert retrieval["memory.cli_profile"] == "codex"
    assert attrs_by_name["ranking"]["memory.record_count"] == 1
    assert attrs_by_name["packet_assembly"]["memory.packet_hash"] == context.packet_hash
    assert attrs_by_name["injection"]["memory.operation.kind"] == MemoryOperationKind.INJECT.value


def test_native_provider_memory_context_uses_native_packet_mode(tmp_path: Path) -> None:
    policy = _enabled_policy(
        injection_access=AccessDecision.DENY,
        native_memory_access=AccessDecision.NATIVE_PROVIDER,
    )
    record = _record(
        statement="Codex memory substrate workflow can use Anthropic native memory.",
        provider_family=ProviderFamily.ANTHROPIC.value,
    )
    store, composer = _composer(tmp_path, policy=policy, record=record)

    context = composer.compose_run_start(
        _request(
            workflow_policy=policy,
            provider="anthropic",
            model="claude-opus-4-7",
            fallback_chain=_chain_for(
                provider="anthropic",
                model="claude-opus-4-7",
                family=ProviderFamily.ANTHROPIC,
            ),
            provider_capabilities=_capabilities(
                provider="anthropic",
                model="claude-opus-4-7",
                native=True,
                prompt=True,
            ),
            provider_family=ProviderFamily.ANTHROPIC.value,
        )
    )

    assert context.access_mode is MemoryAccessMode.NATIVE_PROVIDER_MEMORY
    assert context.packet is not None
    assert context.packet.access_mode.value == MemoryAccessMode.NATIVE_PROVIDER_MEMORY.value

    operations = store.read_memory_operations()
    assert [entry.operation_kind for entry in operations] == [
        MemoryOperationKind.RETRIEVE,
        MemoryOperationKind.INJECT,
    ]
    assert operations[-1].provider == "anthropic"
    assert operations[-1].memory_refs == context.selected_refs


def test_no_memory_access_denial_is_explicit_and_ledgered_without_retrieval(
    tmp_path: Path,
) -> None:
    disabled_policy = MemoryPolicyDocument(policy_id=_POLICY_REF)
    store, composer = _composer(tmp_path, policy=disabled_policy)

    context = composer.compose_run_start(
        _request(
            workflow_policy=disabled_policy,
            provider_capabilities=_capabilities(prompt=True),
        )
    )

    assert context.access_mode is MemoryAccessMode.NO_MEMORY_ACCESS
    assert context.denial_reason is MemoryAccessModeDenialReason.POLICY_DENIED
    assert context.ledgerable_denial is True
    assert context.packet is None
    assert context.packet_hash is None
    assert context.selected_refs == ()

    [denial_entry] = store.read_memory_operations()
    assert denial_entry.operation_kind is MemoryOperationKind.INJECT
    assert denial_entry.operation_projection is MemoryOperationProjection.INJECTION_DECISIONS
    assert denial_entry.memory_refs == ()
    assert denial_entry.policy_ref == _POLICY_REF


def test_context_composer_denial_emits_policy_failure_class(tmp_path: Path) -> None:
    tracer_provider, exporter = _tracer_provider()
    disabled_policy = MemoryPolicyDocument(policy_id=_POLICY_REF)
    _, composer = _composer(
        tmp_path,
        policy=disabled_policy,
        tracer_provider=tracer_provider,
    )

    composer.compose_run_start(
        _request(
            workflow_policy=disabled_policy,
            provider_capabilities=_capabilities(prompt=True),
        )
    )

    [denial_span] = [
        span
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get("memory.operation.name") == "denial"
    ]
    attrs = dict(denial_span.attributes or {})
    assert attrs["memory.failure_class"] == "policy_denial"
    assert attrs["memory.policy.decision"] == MemoryAccessModeDenialReason.POLICY_DENIED.value
    assert attrs["memory.record_count"] == 0


def test_external_cli_route_metadata_composes_with_standard_tool_context(
    tmp_path: Path,
) -> None:
    policy = _enabled_policy(
        injection_access=AccessDecision.DENY,
        standard_tool_access=AccessDecision.STANDARD_TOOLS,
    )
    cli_profile = CliProfile(
        profile_id="codex-cli",
        kind=CliProfileKind.CUSTOM,
        provider_name="codex",
        external_cli_kind="codex",
        command_name="codex",
    )
    record = _record(
        statement="Codex memory substrate workflow should expose standard memory tools.",
        cli_profile="codex-cli",
    )
    store, composer = _composer(tmp_path, policy=policy, record=record)

    context = composer.compose_run_start(
        _request(
            workflow_policy=policy,
            provider="codex",
            model="gpt-5",
            cli_profile=cli_profile,
            fallback_chain=_chain_for(provider="codex", model="gpt-5"),
            provider_capabilities=_capabilities(provider="codex", model="gpt-5", tools=True),
            external_cli_route=ExternalCliRoute(
                provider_name="codex",
                external_cli_kind="codex",
                command_name="codex",
                auth_check_passed=True,
                optional=False,
                degradation_allowed=False,
            ),
        )
    )

    assert context.access_mode is MemoryAccessMode.STANDARD_MEMORY_TOOLS
    assert context.external_cli_route_ref == "codex:codex"
    assert context.packet is not None
    assert context.packet.access_mode.value == MemoryAccessMode.STANDARD_MEMORY_TOOLS.value

    operations = store.read_memory_operations()
    assert [entry.operation_kind for entry in operations] == [
        MemoryOperationKind.RETRIEVE,
        MemoryOperationKind.INJECT,
    ]
    assert operations[-1].provider == "codex"
    assert operations[-1].cli_profile == "codex-cli"


def test_prompt_extension_packet_rendering_is_bounded_cited_and_stable(
    tmp_path: Path,
) -> None:
    policy = _enabled_policy(eligible_record_kinds=(MemoryRecordKind.PREFERENCE,))
    allowed = _record(statement="Codex memory prompt packets must cite selected refs.")
    denied = _record(
        statement="Codex denied memory must not appear in rendered packets.",
        kind=MemoryRecordKind.CONVENTION,
    )
    redacted = _record(
        statement="Codex redacted memory must not appear in rendered packets.",
    )
    redacted = redacted.model_copy(
        update={
            "envelope": redacted.envelope.model_copy(
                update={"redaction_state": RedactionState.REDACTED}
            )
        }
    )

    binding = _binding(tmp_path)
    store = _store(binding)
    store.write_record(allowed)
    store.write_record(denied)
    store.write_record(redacted)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)
    composer = RuntimeMemoryContextComposer(
        retriever=MemoryRetriever(
            store=store,
            index_store=index_store,
            policy_resolver=MemoryPolicyResolver(policy),
            policy_ref=_POLICY_REF,
        ),
        operation_store=store,
    )

    context = composer.compose_run_start(
        _request(
            workflow_policy=policy,
            provider_capabilities=_capabilities(prompt=True),
        )
    )

    rendered = render_prompt_extension_packet(context)
    repeated = render_prompt_extension_packet(context)

    assert rendered is not None
    assert rendered == repeated
    assert rendered.packet_hash == context.packet_hash
    assert rendered.policy_ref == _POLICY_REF
    assert rendered.selected_refs == context.selected_refs
    assert context.packet is not None
    assert rendered.section_token_estimate <= context.packet.token_budget
    assert str(allowed.envelope.memory_id) in rendered.content
    assert "Codex memory prompt packets must cite selected refs." in rendered.content
    assert "Codex denied memory must not appear in rendered packets." not in rendered.content
    assert "Codex redacted memory must not appear in rendered packets." not in rendered.content
    assert "read-only memory packet" in rendered.content
