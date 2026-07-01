"""R-FS-1 arc `B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION` — populate the
§15.3 cross-family family tag on production LLM cost records so
`RollupAxis.PER_PROVIDER_DISCRIMINATOR` (`RunResult.cost_attribution_by_provider_discriminator`,
runtime spec v1.58 §9) is non-vacuous in production.

Covers:
- `cross_family_cost_tag` — the provider→`CrossFamilyTag` map (the §15.1 example
  grouping; the one-source-of-truth `provider_family_for_provider` reused by
  `retry_breaker_fallback`).
- `attribute_llm_dispatch_cost` / `_attribute_cost_best_effort` — the LLM cost
  path now stamps `provider_discriminator` from the dispatched provider's family
  (by-execution, real cost chain + rate table).
- `_rollup_cost_attribution_by_provider_discriminator` / `_build_run_result` —
  the new run-result field rolls up only the LLM subtotal (skips `None`-tag
  non-LLM records); the other two cost fields are unchanged (no regression).
- End-to-end through the **real** `RetryBreakerFallbackDispatcher`: a cross-family
  fallback (anthropic fails → openai succeeds) yields a real cost record tagged
  `frontier_managed_alt`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier
from harness_core.identity import StepID
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.engine_namespace import ReplayDisposition
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.routing_manifest_residence import RetryPolicy
from harness_cp.workflow_driver_types import (
    RunResult as _CpRunResult,
)
from harness_cp.workflow_driver_types import (
    RunStatus as _CpRunStatus,
)
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_od.cross_family_rollup import CrossFamilyTag, RollupAxis
from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord
from harness_od.rate_table_v1 import RATE_TABLE_V1
from harness_runtime.api import (
    _build_run_result,
    _rollup_cost_attribution,
    _rollup_cost_attribution_by_dispatch_kind,
    _rollup_cost_attribution_by_provider_discriminator,
)
from harness_runtime.lifecycle import retry_breaker_fallback as _rbf
from harness_runtime.lifecycle.cost_attribution import RuntimeCostAttributionChain
from harness_runtime.lifecycle.cross_family_cost_tag import (
    cross_family_tag_for_provider,
    provider_family_for_provider,
)
from harness_runtime.lifecycle.llm_dispatch import (
    LLMDispatchProviderUnreachableError,
    _attribute_cost_best_effort,
)
from harness_runtime.lifecycle.retry_breaker import (
    DEFAULT_RETRY_POLICY,
    RuntimeRetryBreaker,
)
from harness_runtime.lifecycle.retry_breaker_fallback import (
    RESERVED_LLM_DISPATCH_KEY,
    RetryBreakerFallbackDispatcher,
)
from opentelemetry.sdk.trace import TracerProvider

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


class _RecordingAuditWriter:
    def append(self, tenant_id: str | None, audit_entry: object) -> object:
        return "appended"


def _record(
    *,
    cost: float,
    provider_discriminator: str | None,
    dispatch_kind: DispatchKind,
    provider: str = "p",
    model: str = "m",
    span_id: str = "s",
) -> SpanCostRecord:
    return SpanCostRecord(
        span_id=span_id,
        idempotency_key="idem",
        total_cost=cost,
        total_latency_ms=1,
        derived_keys=(),
        engine_replay_disposition=ReplayDisposition.NO_REPLAY,
        retry_attempt_number=None,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator=provider_discriminator,
        dispatch_kind=dispatch_kind,
        gen_ai_provider_name=provider,
        gen_ai_request_model=model,
    )


def _success_cp_result() -> _CpRunResult:
    return _CpRunResult(
        workflow_id="wf-bfc",
        run_id="run-bfc-unit",
        status=_CpRunStatus.SUCCESS,
        terminal_step_index=None,
        partial_state=None,
        final_state={"k": "v"},
        fail_class=None,
    )


class _ShutdownReport:
    audit_ledger_head_hash = "deadbeef"


# ===========================================================================
# 1. cross_family_cost_tag — the provider → CrossFamilyTag map
# ===========================================================================


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", CrossFamilyTag.FRONTIER_MANAGED),
        ("openai", CrossFamilyTag.FRONTIER_MANAGED_ALT),
        ("google", CrossFamilyTag.FRONTIER_MANAGED_ALT),  # §15.1 "OpenAI/Gemini" grouping
        ("gemini", CrossFamilyTag.FRONTIER_MANAGED_ALT),
        ("ollama", CrossFamilyTag.LOCAL_OLLAMA),
    ],
)
def test_cross_family_tag_for_provider_per_family(provider: str, expected: CrossFamilyTag) -> None:
    tag = cross_family_tag_for_provider(provider)
    assert tag == expected.value
    # The returned string MUST be a valid CrossFamilyTag (it is consumed by the
    # PER_PROVIDER_DISCRIMINATOR rollup's _validate_family_tag, which raises on a
    # non-member). Round-trip proves the rollup will not raise on our tags.
    assert CrossFamilyTag(tag) is expected


def test_cross_family_tag_for_provider_unknown_falls_back_local_ollama() -> None:
    # Conservative attribution fallback (mirrors the prior _provider_family
    # semantics) — never raises, never affects WHICH model is dispatched.
    assert (
        cross_family_tag_for_provider("some-unknown-provider") == CrossFamilyTag.LOCAL_OLLAMA.value
    )


def test_provider_family_for_provider_maps_and_falls_back() -> None:
    assert provider_family_for_provider("anthropic") is ProviderFamily.ANTHROPIC
    assert provider_family_for_provider("openai") is ProviderFamily.OPENAI
    assert provider_family_for_provider("google") is ProviderFamily.GOOGLE
    assert provider_family_for_provider("ollama") is ProviderFamily.LOCAL_OPEN_WEIGHT
    assert provider_family_for_provider("nonsense") is ProviderFamily.LOCAL_OPEN_WEIGHT


def test_retry_breaker_fallback_reuses_canonical_provider_family() -> None:
    """One source of truth: retry_breaker_fallback's local `_provider_family`
    IS the canonical `provider_family_for_provider` (no duplicate map)."""
    assert _rbf._provider_family is provider_family_for_provider


# ===========================================================================
# 2. _rollup_cost_attribution_by_provider_discriminator — LLM-subtotal partition
# ===========================================================================


def test_rollup_by_provider_discriminator_empty_and_none_yield_empty_tuple() -> None:
    assert _rollup_cost_attribution_by_provider_discriminator([]) == ()
    assert _rollup_cost_attribution_by_provider_discriminator(None) == ()


def test_rollup_by_provider_discriminator_partitions_llm_subtotal_only() -> None:
    """Tagged LLM records roll up per family; None-tag (tool/validator/webhook)
    records are SKIPPED — so the sum is the LLM subtotal, NOT the run total."""
    records = [
        _record(
            cost=1.0, provider_discriminator="frontier_managed", dispatch_kind=DispatchKind.LLM
        ),
        _record(
            cost=2.0, provider_discriminator="frontier_managed", dispatch_kind=DispatchKind.LLM
        ),
        _record(
            cost=4.0, provider_discriminator="frontier_managed_alt", dispatch_kind=DispatchKind.LLM
        ),
        # Non-LLM records carry no family tag (None) — skipped by the axis.
        _record(cost=10.0, provider_discriminator=None, dispatch_kind=DispatchKind.TOOL),
        _record(cost=0.5, provider_discriminator=None, dispatch_kind=DispatchKind.WEBHOOK),
    ]
    rollups = _rollup_cost_attribution_by_provider_discriminator(records)

    by_key = {r.group_key: r for r in rollups}
    assert set(by_key) == {"frontier_managed", "frontier_managed_alt"}
    assert by_key["frontier_managed"].total_cost == pytest.approx(3.0)
    assert by_key["frontier_managed"].span_count == 2
    assert by_key["frontier_managed_alt"].total_cost == pytest.approx(4.0)
    assert all(r.rollup_axis is RollupAxis.PER_PROVIDER_DISCRIMINATOR for r in rollups)

    # The load-bearing invariant: this axis partitions the LLM SUBTOTAL (7.0),
    # NOT the full run total (17.5). Asserting "== run total" would be a false RED.
    llm_subtotal = sum(r.total_cost for r in records if r.dispatch_kind is DispatchKind.LLM)
    run_total = sum(r.total_cost for r in records)
    assert sum(r.total_cost for r in rollups) == pytest.approx(llm_subtotal)
    assert sum(r.total_cost for r in rollups) != pytest.approx(run_total)


def test_rollup_by_provider_discriminator_all_non_llm_yields_empty() -> None:
    records = [
        _record(cost=1.0, provider_discriminator=None, dispatch_kind=DispatchKind.TOOL),
        _record(cost=2.0, provider_discriminator=None, dispatch_kind=DispatchKind.VALIDATOR),
    ]
    assert _rollup_cost_attribution_by_provider_discriminator(records) == ()


def test_build_run_result_populates_three_cost_fields_without_regression() -> None:
    """The new field is populated; the v1.53 + v1.57 fields are byte-unchanged
    (no double-count, no regression)."""
    records = [
        _record(
            cost=3.0,
            provider_discriminator="frontier_managed",
            dispatch_kind=DispatchKind.LLM,
            provider="anthropic",
            model="claude-opus",
        ),
        _record(
            cost=1.0,
            provider_discriminator=None,
            dispatch_kind=DispatchKind.TOOL,
            provider="tool:echo",
            model="",
        ),
    ]
    result = _build_run_result(_success_cp_result(), _ShutdownReport(), cost_records=records)

    # New field — only the LLM record contributes ($3.0), the tool record skipped.
    assert (
        result.cost_attribution_by_provider_discriminator
        == _rollup_cost_attribution_by_provider_discriminator(records)
    )
    by_fam = {r.group_key: r.total_cost for r in result.cost_attribution_by_provider_discriminator}
    assert by_fam == {"frontier_managed": pytest.approx(3.0)}

    # No regression: the two full-run-partition fields still sum to the $4.0 total.
    assert result.cost_attribution == _rollup_cost_attribution(records)
    assert result.cost_attribution_by_dispatch_kind == _rollup_cost_attribution_by_dispatch_kind(
        records
    )
    assert sum(r.total_cost for r in result.cost_attribution) == pytest.approx(4.0)
    assert sum(r.total_cost for r in result.cost_attribution_by_dispatch_kind) == pytest.approx(4.0)


def test_build_run_result_no_records_yields_empty_provider_discriminator_field() -> None:
    assert (
        _build_run_result(
            _success_cp_result(), _ShutdownReport()
        ).cost_attribution_by_provider_discriminator
        == ()
    )


# ===========================================================================
# 3. _attribute_cost_best_effort — the real LLM cost path stamps the tag
# ===========================================================================


def _attribute_real(provider: str, model: str, sink: list[SpanCostRecord]) -> None:
    tracer = TracerProvider().get_tracer("bfc-test")
    with tracer.start_as_current_span("dispatch") as span:
        _attribute_cost_best_effort(
            span=span,
            cost_chain=RuntimeCostAttributionChain(),
            audit_writer=_RecordingAuditWriter(),
            rate_table=RATE_TABLE_V1,
            cost_record_sink=sink,
            provider_name=provider,
            model=model,
            parent_idempotency_key="parent-idem-1",
            workflow_id="test-wf",
            parent_action_id="workflow:test-wf:step:0",
            input_tokens=1000,
            output_tokens=500,
            cache_creation=None,
            cache_read=None,
            tenant_id=None,
        )


def test_real_cost_path_stamps_frontier_managed_alt_for_openai() -> None:
    sink: list[SpanCostRecord] = []
    _attribute_real("openai", "gpt-5", sink)
    assert len(sink) == 1  # a silent rate-table miss would leave this empty
    assert sink[0].provider_discriminator == CrossFamilyTag.FRONTIER_MANAGED_ALT.value
    rollups = _rollup_cost_attribution_by_provider_discriminator(sink)
    assert len(rollups) == 1
    assert rollups[0].group_key == "frontier_managed_alt"


def test_real_cost_path_stamps_frontier_managed_for_anthropic() -> None:
    sink: list[SpanCostRecord] = []
    _attribute_real("anthropic", "claude-haiku-4-5", sink)
    assert len(sink) == 1
    assert sink[0].provider_discriminator == CrossFamilyTag.FRONTIER_MANAGED.value


# ===========================================================================
# 4. End-to-end through the REAL RetryBreakerFallbackDispatcher
#    — a cross-family fallback yields a family-tagged cost record.
# ===========================================================================


@dataclass
class _CostAttributingInner:
    """A fake inner dispatcher that runs the REAL cost path on success — so a
    cross-family fallback through the real RetryBreakerFallbackDispatcher
    produces a real, family-tagged SpanCostRecord without a live provider SDK.

    Mirrors `RuntimeLLMDispatcher.dispatch`: reads the rebound provider, fails
    the named providers (unreachable → candidate abandoned → chain advances),
    and on a reachable provider runs `_attribute_cost_best_effort` (appending to
    the run-scoped sink) then returns a success Mapping."""

    sink: list[SpanCostRecord]
    fail_providers: frozenset[str]
    dispatched: list[str] = field(default_factory=list)

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> Mapping[str, Any]:
        provider = binding.model_binding.provider
        self.dispatched.append(provider)
        if provider in self.fail_providers:
            raise LLMDispatchProviderUnreachableError(f"{provider} unreachable")
        _attribute_real(provider, binding.model_binding.model, self.sink)
        return {"ok": provider}


def _candidate(provider: str, model: str, family: ProviderFamily) -> ProviderCandidate:
    return ProviderCandidate(provider=provider, model=model, family=family)


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-bfc",
        model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-bfc"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"messages": [{"role": "user", "content": "hi"}], "tools": None, "params": {}},
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="test-wf",
        parent_action_id="workflow:test-wf:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="test-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="test-step-key",
        tenant_id=None,
        step_index=0,
    )


def _retry_breaker() -> RuntimeRetryBreaker:
    return RuntimeRetryBreaker(
        retry_policies={
            RESERVED_LLM_DISPATCH_KEY: RetryPolicy(
                max_attempts=2, backoff="full_jitter", jitter="full_jitter"
            )
        },
        default_policy=DEFAULT_RETRY_POLICY,
        base_delay_seconds=0.0,
        delay_cap_seconds=0.01,
    )


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_cross_family_fallback_yields_alt_family_tagged_cost_record() -> None:
    """anthropic (primary) is unreachable → openai (cross-family) succeeds; the
    real cost path stamps the surviving record `frontier_managed_alt` and the
    PER_PROVIDER_DISCRIMINATOR rollup is non-empty."""
    sink: list[SpanCostRecord] = []
    inner = _CostAttributingInner(sink=sink, fail_providers=frozenset({"anthropic"}))
    chain = FallbackChain(
        primary=_candidate("anthropic", "claude-haiku-4-5", ProviderFamily.ANTHROPIC),
        same_family=(),
        cross_family=(_candidate("openai", "gpt-5", ProviderFamily.OPENAI),),
        terminal=None,
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker(),
        fallback_chain=chain,
        tracer_provider=TracerProvider(),
        sleep_fn=_noop_sleep,
    )

    result = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    # The cross-family transition actually occurred (anthropic abandoned → openai).
    assert result == {"ok": "openai"}
    assert inner.dispatched == ["anthropic", "openai"]
    # Exactly one cost record (the successful openai dispatch), tagged alt-family.
    assert len(sink) == 1
    assert sink[0].gen_ai_provider_name == "openai"
    assert sink[0].provider_discriminator == CrossFamilyTag.FRONTIER_MANAGED_ALT.value
    # The run-result axis is non-vacuous in production.
    rollups = _rollup_cost_attribution_by_provider_discriminator(sink)
    assert [r.group_key for r in rollups] == ["frontier_managed_alt"]
    assert rollups[0].total_cost == pytest.approx(sink[0].total_cost)
