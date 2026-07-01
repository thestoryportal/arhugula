"""Full workflow_driver execution-path e2e for validator-escalation composer.

Authority: C-RT-25, C-CP-28.

Companion to ``test_u_rt_92_validator_escalation_e2e.py`` — the existing
U-RT-92 e2e exercises the composer via DIRECT invocation
(``compose_validator_escalation_gate(...)`` called from test body). This file
exercises the FULL path through ``workflow_driver.execute_workflow(...)`` —
validator framework returns ESCALATE_HITL → workflow_driver post-dispatch
hook fires composer → AskUserQuestionSurface invoked → APPROVE returned →
flow proceeds to ledger append → ``RunStatus.SUCCESS``.

Mirrors the deferral-pattern resolution shape catalogued at
``test_u_rt_89_pause_resume_full_execution_path.py`` (U-RT-89 follow-on)
and per FM-2 item (d) from Reading B close checkpoint:

    > Full ``execute_workflow`` exercise (validator-escalation composer
    > fired FROM workflow_driver hook with real workflow + step) — requires
    > fuller step-dispatcher fixturing; deferred per FM-2 to follow-on
    > operator-discretion arc at next retirement-batch event. Mirrors
    > U-RT-85 + U-RT-89 deferral patterns.

## Verification-shape discipline

Per ``[[verification-shape-sharpened-grep-vs-e2e]]`` (batch-16 §6) — guards
against the regression class where someone refactors ``execute_workflow``
and accidentally drops the line-874 ``next_action == "escalate_hitl"``
branch. Neither direct composer-invocation tests (existing U-RT-92 e2e)
nor mocked-ctx unit tests catch that.

## Test substrate

Reuses the ``test_u_rt_89_pause_resume_full_execution_path.py`` substrate
shape: ``_NoopDispatcher`` + ``_SingleKindRegistry`` + ``_minimal_manifest``
+ ``_single_inference_step`` + ``_attach_get_tracer_to_ctx``. The runtime
context is mutated post-bootstrap via ``ctx.model_copy(update={...})`` to
swap in the custom ValidatorFramework + stub AskUserQuestionSurface — the
production HarnessContext is Pydantic v2 frozen so model_copy is the
canonical mutation path.
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from harness_core.identity import StepID
from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.validator_framework import (
    ConcreteValidatorFramework,
    materialize_sync_validator_framework_facade,
)
from harness_cp.validator_framework_types import (
    HITLEscalationBrief,
    ValidatorFailClass,
    ValidatorOutcome,
    ValidatorResult,
)
from harness_cp.workflow_driver import execute_workflow
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.ask_user_question_surface import (
    AskUserQuestionResult,
    AskUserQuestionSurface,
)
from harness_runtime.lifecycle.validator_framework_types import (
    ValidatorFrameworkConfig,
)
from harness_runtime.types import RuntimeConfig

from .conftest import WORKLOAD, build_config


def _config_with_validator_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={"validator_framework_config": ValidatorFrameworkConfig.default()},
    )


# Same single-kind step-dispatcher registry shape as
# test_u_rt_89_pause_resume_full_execution_path.py:103-124 — single
# NoopDispatcher returning a deterministic step_output dict.
class _NoopDispatcher:
    def dispatch(
        self,
        binding: Any,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        _ = binding, step_context
        return {"step_id": str(step.step_id), "ok": True}


class _SingleKindRegistry:
    def __init__(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: Any) -> Any:
        _ = step_kind
        return self._dispatcher


_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic",
        model="claude-haiku-4-5",
        family=ProviderFamily.ANTHROPIC,
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)


_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")


def _attach_get_tracer_to_ctx(ctx: Any) -> None:
    """Same shape as test_u_rt_89_pause_resume_full_execution_path.py:142."""
    from opentelemetry.trace import NoOpTracer

    ctx.tracer_provider.get_tracer = lambda _name, /: NoOpTracer()  # type: ignore[attr-defined,method-assign]


def _minimal_manifest(workflow_id: str) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WORKLOAD,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _single_inference_step() -> tuple[WorkflowStep, ...]:
    return (
        WorkflowStep(
            step_id=StepID("step-escalation-0"),
            step_kind=StepKind.INFERENCE_STEP,
            step_payload={"index": 0},
        ),
    )


class _EscalatingValidator:
    """Test Validator returning ESCALATE_HITL with a deterministic brief.

    Implements the ``Validator`` Protocol declared at
    ``harness_cp.validator_framework_types.Validator``. The ``.validate()``
    async method always returns ``ValidatorOutcome.ESCALATE`` with an
    ``HITLEscalationBrief`` populated for composer consumption.
    """

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Any,
        *,
        step_context: Any,
    ) -> ValidatorResult:
        _ = step_result, step_context
        return ValidatorResult(
            outcome=ValidatorOutcome.ESCALATE,
            fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
            fail_detail_hash="testhash",
            escalation_brief=HITLEscalationBrief(
                parent_step_id=str(step.step_id),
                parent_action_id="test-act-id",
                fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
                fail_detail_hash="testhash",
                escalation_reason="test full-execution-path escalation",
            ),
        )


class _ApprovingAskUserQuestionSurface(AskUserQuestionSurface):
    """Stub surface returning APPROVE + capturing invocation state.

    Same shape as ``_StubAskUserQuestionSurface`` in
    ``test_u_rt_92_validator_escalation_e2e.py:120`` but bound to APPROVE
    so the composer's gate-response branch maps to ``HITLResponse.APPROVE``
    and the flow proceeds rather than raising.
    """

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    async def ask(
        self,
        prompt: str,
        options: Any,
        timeout: float | None,
    ) -> AskUserQuestionResult:
        self.invocations.append(
            {
                "prompt": prompt,
                "options": list(options),
                "timeout": timeout,
            }
        )
        return AskUserQuestionResult(
            response=HITLResponse.APPROVE,
            latency_ms=5.0,
        )


# ---------------------------------------------------------------------------
# Test — validator ESCALATE fires composer through execute_workflow.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_escalation_e2e_through_execute_workflow(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Full workflow_driver execution-path e2e — validator returns ESCALATE_HITL
    at post-dispatch hook → §14.15 composer fires → AskUserQuestionSurface
    invoked → APPROVE → flow proceeds → RunStatus.SUCCESS.

    Verifies the line-874 ``next_action == "escalate_hitl"`` driver branch
    fires end-to-end. Guards against regressions where the branch is dropped
    or short-circuited during a future workflow_driver refactor.

    Mirrors the U-RT-89 ``test_pause_path_through_execute_workflow`` +
    ``test_resume_path_through_execute_workflow`` discipline at the
    validator-escalation arc per FM-2 item (d) resolution.
    """
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    ctx_orig = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx_orig)
    assert ctx_orig.validator_framework is not None
    assert ctx_orig.ask_user_question_surface is not None

    # Swap in custom ValidatorFramework with escalating validator + stub
    # surface returning APPROVE. HarnessContext is Pydantic v2 frozen so
    # model_copy is the canonical mutation path.
    step = _single_inference_step()[0]
    custom_framework_async = ConcreteValidatorFramework(
        validator_registry={step.step_id: _EscalatingValidator()}  # type: ignore[dict-item]
    )
    # Workflow_driver invokes evaluate() sync from the worker thread;
    # wrap async framework in SyncValidatorFrameworkFacade capturing the
    # current event loop per U-CP-61 bridge pattern.
    custom_framework = materialize_sync_validator_framework_facade(
        inner=custom_framework_async,
        result_timeout_seconds=30.0,  # type: ignore[arg-type]
    )
    approving_surface = _ApprovingAskUserQuestionSurface()
    ctx = ctx_orig.model_copy(
        update={
            "validator_framework": custom_framework,
            "ask_user_question_surface": approving_surface,
        }
    )
    # Re-attach get_tracer since model_copy created a new frozen instance
    # but tracer_provider is the same object reference (Pydantic frozen
    # copies field references, not deep-copies). Defensive re-attach in
    # case identity differs.
    _attach_get_tracer_to_ctx(ctx)

    manifest = _minimal_manifest("wf-validator-escalation-e2e")
    steps = (step,)
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    # execute_workflow is sync — bridge to thread per the U-RT-89 pattern.
    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-validator-escalation-1",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )

    # The composer fired and APPROVE was returned → workflow proceeded to
    # ledger append → RunStatus.SUCCESS.
    assert result.status == RunStatus.SUCCESS, (
        f"expected SUCCESS, got {result.status}; fail_class={result.fail_class}"
    )

    # The AskUserQuestionSurface was invoked exactly once (one ESCALATE event
    # at the single step).
    assert len(approving_surface.invocations) == 1, (
        f"expected composer to invoke surface once; got "
        f"{len(approving_surface.invocations)} invocation(s)"
    )

    # The composer surfaced the escalation brief's escalation_reason in the
    # prompt (per validator_escalation_composer.py escalation_prompt body).
    invocation = approving_surface.invocations[0]
    assert "test full-execution-path escalation" in invocation["prompt"], (
        f"expected escalation_reason in prompt; got prompt={invocation['prompt']!r}"
    )

    # The composer surfaced a non-empty palette (the §14.15 composer never
    # surfaces an empty palette per spec invariant — UNION-intersection of
    # at least one restriction set).
    assert len(invocation["options"]) > 0


# ---------------------------------------------------------------------------
# Test — validator PASS fires neither composer nor surface.
# ---------------------------------------------------------------------------


class _PassingValidator:
    """Test Validator returning PASS — no escalation, composer NOT fired."""

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Any,
        *,
        step_context: Any,
    ) -> ValidatorResult:
        _ = step, step_result, step_context
        return ValidatorResult(
            outcome=ValidatorOutcome.PASS,
            fail_class=None,
            fail_detail_hash=None,
            escalation_brief=None,
        )


@pytest.mark.asyncio
async def test_validator_pass_does_not_fire_composer_through_execute_workflow(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Inverse-arm guard — validator PASS → driver hook PROCEED branch fires;
    composer NOT invoked; surface NOT invoked; RunStatus.SUCCESS.

    Verifies the line-874 ``escalate_hitl`` branch is BRANCH-INDEPENDENT
    from the line-790 PROCEED branch — PASS must NOT trigger the composer.
    Guards against regressions where the branch logic conflates the two.
    """
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    ctx_orig = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx_orig)

    step = _single_inference_step()[0]
    custom_framework_async = ConcreteValidatorFramework(
        validator_registry={step.step_id: _PassingValidator()}  # type: ignore[dict-item]
    )
    custom_framework = materialize_sync_validator_framework_facade(
        inner=custom_framework_async,
        result_timeout_seconds=30.0,  # type: ignore[arg-type]
    )
    approving_surface = _ApprovingAskUserQuestionSurface()
    ctx = ctx_orig.model_copy(
        update={
            "validator_framework": custom_framework,
            "ask_user_question_surface": approving_surface,
        }
    )
    _attach_get_tracer_to_ctx(ctx)

    manifest = _minimal_manifest("wf-validator-pass-e2e")
    steps = (step,)
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-validator-pass-1",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )

    assert result.status == RunStatus.SUCCESS, (
        f"expected SUCCESS, got {result.status}; fail_class={result.fail_class}"
    )

    # PASS branch must NOT invoke the composer surface.
    assert len(approving_surface.invocations) == 0, (
        f"expected zero composer invocations on PASS; got {len(approving_surface.invocations)}"
    )
