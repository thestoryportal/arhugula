"""Automatic local memory substrate wiring.

This module binds the existing memory store, derived retrieval index,
retriever, prompt-packet composer, and standard tool executor from
``RuntimeConfig.memory``. The substrate is local-first: provider-native remote
memory is opt-in and remains the lowest-priority access mode.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from harness_core import WorkloadClass
from harness_cp.cross_family_fallback_chain import FallbackChain
from harness_cp.memory_access_mode import reflect_memory_provider_capabilities
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep
from harness_is.cli_profile import DEFAULT_GENERIC_CLI_PROFILE
from harness_is.memory_path_registry import MemoryPathRegistry, MemoryRootBinding
from harness_is.memory_policy import (
    AccessDecision,
    CaptureDecision,
    MemoryPolicyDocument,
    MemoryPolicyResolver,
    PromotionDecision,
    ReviewMode,
)
from harness_is.memory_record_envelope import MemoryRecordKind, MemoryScope, MemoryVisibility
from harness_is.memory_retrieval import MemoryRetriever
from harness_is.memory_retrieval_index import (
    DerivedRetrievalIndex,
    DerivedRetrievalIndexMissingError,
    DerivedRetrievalIndexStaleError,
    DerivedRetrievalIndexStore,
)
from harness_is.memory_store import CanonicalMemoryStore

from harness_runtime.memory_context import (
    MemoryContextCompositionRequest,
    RuntimeMemoryContext,
    RuntimeMemoryContextComposer,
)
from harness_runtime.memory_tool_executor import StandardMemoryToolExecutor
from harness_runtime.types import RuntimeConfig


@runtime_checkable
class AutomaticMemoryRuntime(Protocol):
    """Per-dispatch automatic memory context provider."""

    standard_memory_tool_executor: Any

    def compose_for_dispatch(
        self,
        *,
        binding: StepEffectiveBinding,
        fallback_chain: FallbackChain,
        step: WorkflowStep,
        step_context: StepExecutionContext,
    ) -> RuntimeMemoryContext: ...


class AutoRefreshingDerivedRetrievalIndexStore(DerivedRetrievalIndexStore):
    """Derived index store that rebuilds local indexes on first use or staleness."""

    def read_current(self, *, require_fresh: bool = True) -> DerivedRetrievalIndex:
        try:
            return super().read_current(require_fresh=require_fresh)
        except (DerivedRetrievalIndexMissingError, DerivedRetrievalIndexStaleError):
            return self.rebuild(indexed_at=datetime.now(UTC))


class LocalAutomaticMemoryRuntime:
    """Runtime-local automatic memory composer."""

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        workload_class: WorkloadClass | None = None,
        tracer_provider: object | None = None,
    ) -> None:
        memory_root = config.memory.root_path or (config.repository_root / ".harness" / "memory")
        self._surface = config.deployment_surface
        self._policy = _policy_from_config(config)
        self._policy_resolver = MemoryPolicyResolver(self._policy)
        self._token_budget = config.memory.token_budget
        self._project = config.repository_root.name
        self._workload_class = workload_class
        self._tenant_id = config.tenant_id

        binding = MemoryRootBinding(default_root=memory_root)
        MemoryPathRegistry(binding).ensure_canonical_roots(self._surface)
        store = CanonicalMemoryStore(
            root_binding=binding,
            deployment_surface=self._surface,
        )
        index_store = AutoRefreshingDerivedRetrievalIndexStore(
            root_binding=binding,
            deployment_surface=self._surface,
        )
        index_store.read_current()
        retriever = MemoryRetriever(
            store=store,
            index_store=index_store,
            policy_resolver=self._policy_resolver,
            policy_ref=self._policy.policy_id,
        )
        self._composer = RuntimeMemoryContextComposer(
            retriever=retriever,
            operation_store=store,
            tracer_provider=tracer_provider,
        )
        self.standard_memory_tool_executor = (
            StandardMemoryToolExecutor(
                store=store,
                index_store=index_store,
                retriever=retriever,
                policy_resolver=self._policy_resolver,
                tracer_provider=tracer_provider,
            )
            if config.memory.standard_tools_enabled
            else None
        )

    def compose_for_dispatch(
        self,
        *,
        binding: StepEffectiveBinding,
        fallback_chain: FallbackChain,
        step: WorkflowStep,
        step_context: StepExecutionContext,
    ) -> RuntimeMemoryContext:
        model_binding = binding.model_binding
        return self._composer.compose_run_start(
            MemoryContextCompositionRequest(
                run_id=_run_id(step_context),
                workflow_id=step_context.workflow_id,
                workload_class=(
                    self._workload_class.value if self._workload_class is not None else None
                ),
                query_summary=_query_summary(step.step_payload),
                model_binding=model_binding,
                fallback_chain=fallback_chain,
                cli_profile=DEFAULT_GENERIC_CLI_PROFILE,
                workflow_policy=self._policy,
                step_policy=None,
                token_budget=self._token_budget,
                record_scope=MemoryScope(
                    project=self._project,
                    workflow=step_context.workflow_id,
                    workload_class=(
                        self._workload_class.value if self._workload_class is not None else None
                    ),
                    provider_family=fallback_chain.primary.family.value,
                    cli_profile=DEFAULT_GENERIC_CLI_PROFILE.profile_id,
                    tenant=step_context.tenant_id or self._tenant_id,
                    visibility=MemoryVisibility.PROJECT,
                ),
                allowed_kinds=_retrievable_kinds(),
                timestamp=datetime.now(UTC),
                actor=step_context.parent_actor,
                policy_ref=self._policy.policy_id,
                provider_capabilities=reflect_memory_provider_capabilities(model_binding),
            )
        )


def materialize_automatic_memory_runtime(
    config: RuntimeConfig,
    *,
    workload_class: WorkloadClass | None = None,
    tracer_provider: object | None = None,
) -> LocalAutomaticMemoryRuntime | None:
    """Materialize automatic memory when enabled in runtime config."""

    if not config.memory.enabled:
        return None
    return LocalAutomaticMemoryRuntime(
        config=config,
        workload_class=workload_class,
        tracer_provider=tracer_provider,
    )


def _policy_from_config(config: RuntimeConfig) -> MemoryPolicyDocument:
    memory = config.memory
    return MemoryPolicyDocument(
        policy_id=memory.policy_id,
        enabled=memory.enabled,
        capture_decision=(
            CaptureDecision.SUMMARIZE_ONLY
            if memory.capture_run_events or memory.capture_turns
            else CaptureDecision.DENY
        ),
        promotion_decision=PromotionDecision.PROPOSE_SEMANTIC,
        retrieval_access=AccessDecision.RETRIEVAL_ONLY,
        injection_access=(
            AccessDecision.PROMPT_PACKET if memory.prompt_packet_enabled else AccessDecision.DENY
        ),
        standard_tool_access=(
            AccessDecision.STANDARD_TOOLS if memory.standard_tools_enabled else AccessDecision.DENY
        ),
        native_memory_access=(
            AccessDecision.NATIVE_PROVIDER
            if memory.native_provider_enabled
            else AccessDecision.DENY
        ),
        review_mode=ReviewMode.AUTOMATIC,
        eligible_record_kinds=_retrievable_kinds(),
    )


def _retrievable_kinds() -> tuple[MemoryRecordKind, ...]:
    return (
        MemoryRecordKind.SEMANTIC_FACT,
        MemoryRecordKind.PREFERENCE,
        MemoryRecordKind.DECISION,
        MemoryRecordKind.CONVENTION,
        MemoryRecordKind.FAILURE_LEARNING,
        MemoryRecordKind.RESEARCH,
        MemoryRecordKind.PROCEDURAL_SNAPSHOT,
    )


def _run_id(step_context: StepExecutionContext) -> str:
    return step_context.parent_idempotency_key.replace("/", "_").replace("\\", "_")


def _query_summary(step_payload: Mapping[str, Any]) -> str:
    messages = step_payload.get("messages")
    if isinstance(messages, list) and messages:
        recent_messages = cast("list[object]", messages[-4:])
        return _compact_json(recent_messages)
    return _compact_json(step_payload)


def _compact_json(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return rendered[:2000] or "dispatch"


__all__ = [
    "AutoRefreshingDerivedRetrievalIndexStore",
    "AutomaticMemoryRuntime",
    "LocalAutomaticMemoryRuntime",
    "materialize_automatic_memory_runtime",
]
