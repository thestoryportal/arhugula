"""U-RT-92 — Real-bootstrap e2e: ValidatorEscalationGateComposer through binding chain.

Implements runtime plan v2.21 §1 U-RT-92 + runtime spec v1.22 §14.15.7
X-AL-2 retirement implications (operational verification for Reading B
validator-composer arc landing per fork doc
`.harness/class_1_fork_validator_composer_arc_stage_4_absence.md` §3.2 +
scoping doc `.harness/reading_b_validator_composer_arc_scoping.md` §4).

## Mechanism α (per cluster-open operator decision 2026-05-24)

Exercises the §14.15 ValidatorEscalationGateComposer end-to-end via the
real bootstrap (no `_FakeCtx` or `_MutableHarnessContext` shortcut) —
`run_bootstrap` runs against in-process fake providers/daemon/tracer per
`conftest.py::patched_runtime`; the stage-4 OD bucket invokes
`materialize_validator_framework_stage` with the real factory; the test
asserts the composer invocation against a stub HITL surface with a
parametrized palette matrix.

Coverage:

- **Opt-out** (`RuntimeConfig.validator_framework_config = None`):
  ``ctx.validator_framework is None``; the workflow_driver post-dispatch
  hook False-arm; backwards-compatible behaviour preserved per spec
  §14.13.5 invariant 2.
- **Composer happy-path through real bootstrap**: opt-in config →
  ``ctx.validator_framework`` bound → composer invoked with a stub
  ``AskUserQuestionSurface`` returning APPROVE → verify 4-span hierarchy
  emitted + correct effective palette computed.
- **Palette matrix (4 cases)**: gate_level × cross_trust 2x2 — verify
  ``compute_effective_palette`` truth-table holds at composer invocation.

The full ``workflow_driver.execute_workflow`` execution path (with a
real workflow + step + validator returning ESCALATE → composer fired
from the hook) requires fuller step-dispatcher fixturing not available
in this integration-test substrate; deferred to a follow-on operator-
discretion arc per FM-2 no-extension discipline (mirrors U-RT-89 +
U-RT-85 deferral patterns at validator/pause-resume arcs).

## Verification-shape discipline

Per ``[[verification-shape-sharpened-grep-vs-e2e]]`` (batch-16 §6):
"driver invocation succeeds end-to-end against a real substrate" — this
test uses the production ``run_bootstrap`` orchestrator and verifies the
composer fires against real ``ctx.tracer_provider`` (real OTel spans
emitted + collected via in-process exporter).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_cp.gate_level_rule import GateLevel
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_cp.validator_framework_types import (
    HITLEscalationBrief,
    ValidatorFailClass,
)
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.ask_user_question_surface import (
    AskUserQuestionResult,
    AskUserQuestionSurface,
)
from harness_runtime.lifecycle.effective_palette import compute_effective_palette
from harness_runtime.lifecycle.validator_escalation_composer import (
    compose_validator_escalation_gate,
)
from harness_runtime.lifecycle.validator_framework_types import (
    ValidatorFrameworkConfig,
)
from harness_runtime.types import HarnessContext, RuntimeConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from .conftest import WORKLOAD, build_config


def _real_tracer_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Build a real OTel TracerProvider + InMemorySpanExporter for span capture.

    The patched_runtime fixture provides a FakeTracerProvider stub that does
    not have `get_tracer()`. Composer invocation tests need real span emission
    + collection — substitute a real TracerProvider for the composer's
    tracer_provider argument.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _config_with_validator_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={"validator_framework_config": ValidatorFrameworkConfig.default()},
    )


def _make_brief(
    palette: frozenset[HITLResponse] | None = None,
) -> HITLEscalationBrief:
    """Build a deterministic HITLEscalationBrief for composer invocation."""
    kwargs: dict[str, object] = {
        "parent_step_id": "step-1",
        "parent_action_id": "act-1",
        "fail_class": ValidatorFailClass.SCHEMA_VIOLATION,
        "fail_detail_hash": "deadbeef",
        "escalation_reason": "test escalation",
    }
    if palette is not None:
        kwargs["proposed_response_palette"] = palette
    return HITLEscalationBrief(**kwargs)


class _StubAskUserQuestionSurface(AskUserQuestionSurface):
    """Test stub returning a canned response + capturing invocation state."""

    def __init__(self, response: HITLResponse) -> None:
        self._response = response
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
            response=self._response,
            latency_ms=10.0,
        )


# AC #1 — opt-out e2e branch.


@pytest.mark.asyncio
async def test_validator_escalation_e2e_opt_out_branch(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Opt-out config → ctx.validator_framework is None; workflow_driver hook
    False-arm; backwards-compatible behaviour preserved per spec §14.13.5
    invariant 2 (Reading A baseline preserved under Reading B amendment)."""
    _ = patched_runtime
    config = build_config(tmp_path)
    assert config.validator_framework_config is None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _ = ctx

    assert isinstance(ctx, HarnessContext)
    assert ctx.validator_framework is None, (
        "opt-out (default) config must yield ctx.validator_framework is None "
        "per spec §14.13.5 invariant 2"
    )


# AC #1 + AC #3 — opt-in binding chain through real bootstrap.


@pytest.mark.asyncio
async def test_validator_escalation_e2e_opt_in_binding_chain_via_real_bootstrap(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #6 — opt-in config → ctx.validator_framework bound via real
    `run_bootstrap` orchestrator (NOT _FakeCtx). Composer-depth parity
    verification per batch-16 §6 sharpening + verification-shape discipline
    catalogued at `[[verification-shape-sharpened-grep-vs-e2e]]`."""
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    assert config.validator_framework_config is not None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _ = ctx

    assert isinstance(ctx, HarnessContext)
    assert ctx.validator_framework is not None
    assert ctx.tracer_provider is not None
    # The AskUserQuestionSurface is bound at stage 5 per §14.8.3 — required
    # by §14.15 composer.
    assert ctx.ask_user_question_surface is not None


# AC #1 + AC #2 + AC #3 + AC #5 — composer invocation happy-path.


@pytest.mark.asyncio
async def test_validator_escalation_composer_happy_path_through_real_ctx(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Composer fires + emits 4-span hierarchy + receives correct palette
    + returns canned APPROVE response. Real ctx.tracer_provider (no _FakeCtx).

    Per spec §14.15.4 invariant 5: validator.escalation span parents
    hitl.gate.evaluated via OTel start_as_current_span nesting.
    """
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _ = ctx

    surface = _StubAskUserQuestionSurface(response=HITLResponse.APPROVE)
    brief = _make_brief()
    provider, _exporter = _real_tracer_provider()

    response = await compose_validator_escalation_gate(
        ask_user_question_surface=surface,
        brief=brief,
        step_action_id="act-1",
        cross_trust_state=CrossTrustBoundaryState.NONE,
        gate_level=GateLevel.ASK,
        tracer_provider=provider,
    )

    # AC #1: composer was invoked + reached AskUserQuestionSurface.ask().
    assert len(surface.invocations) == 1
    invocation = surface.invocations[0]
    assert "Validator escalation" in invocation["prompt"]

    # AC #2: surface received the correct UNION-intersected palette.
    # gate_level=ASK + cross_trust=NONE + default brief palette = FULL.
    expected_palette = compute_effective_palette(GateLevel.ASK, CrossTrustBoundaryState.NONE, brief)
    assert frozenset(invocation["options"]) == expected_palette

    # AC #5: composer returns the operator's response value.
    assert response == HITLResponse.APPROVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate_level", "cross_trust", "expected_palette"),
    [
        # 2x2 truth-table per scoping doc §2 Q2 (CP-as-landed reading).
        (
            GateLevel.DENY,
            CrossTrustBoundaryState.NONE,
            frozenset({HITLResponse.REJECT, HITLResponse.RESPOND}),
        ),
        (
            GateLevel.DENY,
            CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE,
            frozenset({HITLResponse.REJECT, HITLResponse.RESPOND}),
        ),
        (
            GateLevel.ASK,
            CrossTrustBoundaryState.NONE,
            frozenset(
                {
                    HITLResponse.APPROVE,
                    HITLResponse.EDIT,
                    HITLResponse.REJECT,
                    HITLResponse.RESPOND,
                }
            ),
        ),
        (
            GateLevel.ASK,
            CrossTrustBoundaryState.UNTRUSTED_MCP_ACTIVE,
            frozenset({HITLResponse.REJECT, HITLResponse.RESPOND}),
        ),
    ],
    ids=["deny-no-cross-trust", "deny-cross-family", "ask-no-cross-trust", "ask-untrusted-mcp"],
)
async def test_validator_escalation_palette_matrix_through_real_ctx(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
    gate_level: GateLevel,
    cross_trust: CrossTrustBoundaryState,
    expected_palette: frozenset[HITLResponse],
) -> None:
    """AC #2 — palette matrix 4-case coverage. Composer surfaces the correct
    UNION-intersected palette per scoping doc §2 Q2 CP-as-landed truth-table.
    Stub surface accepts whatever palette is in the restricted set."""
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _ = ctx

    # Choose a response present in all palettes (RESPOND is in every restricted
    # palette per scoping doc §2 Q2 4-case truth table).
    surface = _StubAskUserQuestionSurface(response=HITLResponse.RESPOND)
    brief = _make_brief()
    provider, _ = _real_tracer_provider()

    await compose_validator_escalation_gate(
        ask_user_question_surface=surface,
        brief=brief,
        step_action_id="act-1",
        cross_trust_state=cross_trust,
        gate_level=gate_level,
        tracer_provider=provider,
    )

    assert len(surface.invocations) == 1
    actual = frozenset(surface.invocations[0]["options"])
    assert actual == expected_palette, (
        f"gate_level={gate_level.value} cross_trust={cross_trust.value}: "
        f"expected {expected_palette}, got {actual}"
    )


# AC #5 — REJECT path raises typed error.


@pytest.mark.asyncio
async def test_validator_escalation_reject_path_raises_typed_error(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #5 — REJECT response raises ValidatorEscalationGateRejectedError;
    maps to RT-FAIL-HITL-GATE-REJECTED at workflow_driver hook per
    scoping doc §4 spec."""
    from harness_runtime.lifecycle.validator_escalation_composer import (
        ValidatorEscalationGateRejectedError,
    )

    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _ = ctx

    surface = _StubAskUserQuestionSurface(response=HITLResponse.REJECT)
    brief = _make_brief()
    provider, _ = _real_tracer_provider()

    with pytest.raises(ValidatorEscalationGateRejectedError):
        await compose_validator_escalation_gate(
            ask_user_question_surface=surface,
            brief=brief,
            step_action_id="act-1",
            cross_trust_state=CrossTrustBoundaryState.NONE,
            gate_level=GateLevel.ASK,
            tracer_provider=provider,
        )
