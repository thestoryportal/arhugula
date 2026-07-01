"""Tests for U-RT-93 — `_evaluate_cell_synchrony` matrix-cell synchrony helper
+ `HITLPauseRequestedSignal` typed control-flow exception class.

Authority: Runtime spec v1.24 §14.8.8.2 (signal) + §14.8.8.3 (helper)
preserved verbatim at v1.25 + v1.26; Implementation_Plan_Harness_Runtime_v2_25.md
§7.1 U-RT-93 ACs #1–#7 (post-CP-v1.17 §6.5 absorption — persona_tier is now
a required field on the canonical StepEffectiveBinding; the getattr-tolerance
fallback path is retired per ACs #5/#6/#7).
"""

from __future__ import annotations

import pytest
from harness_core import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.persona_engine_hitl_matrix import SynchronyClass, matrix_cell_for
from harness_cp.validator_framework_types import HITLEscalationBrief
from harness_runtime.lifecycle.hitl_gate_composer import (
    HITLPauseRequestedSignal,
    _evaluate_cell_synchrony_tolerant,  # pyright: ignore[reportPrivateUsage]
)
from harness_runtime.lifecycle.webhook_delivery_composer import WebhookDeliveryResult

# --- _evaluate_cell_synchrony ----------------------------------------------


def _binding_with_persona_tier(
    *, persona_tier: PersonaTier, engine_class: EngineClass
) -> StepEffectiveBinding:
    """Canonical StepEffectiveBinding fixture with operator-supplied tier.

    Post-CP-v1.17 §6.5: StepEffectiveBinding declares ``persona_tier`` as a
    required field — no duck-typing needed.
    """
    return StepEffectiveBinding(
        step_id="step-1",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=engine_class,
        override_applied=False,
        persona_tier=persona_tier,
    )


def test_evaluate_cell_synchrony_tolerant_returns_none_for_none_binding() -> None:
    """AC #1 — `binding is None` → returns None (operator opt-out arm).

    Composer at §14.8.8.1 step 1 + adjacent defect (ii) treats `None` as a
    sync-blocking fall-through signal.
    """
    assert _evaluate_cell_synchrony_tolerant(None) is None


def test_evaluate_cell_synchrony_tolerant_returns_synchrony_for_sync_blocking_cell() -> None:
    """AC #2 — sync-blocking cell from §18.1 returns SynchronyClass.SYNC_BLOCKING.

    Cell sampled per CP test_persona_engine_hitl_matrix.py precedent:
    `(SOLO_DEVELOPER, EVENT_SOURCED_REPLAY) → SYNC_BLOCKING`.
    """
    binding = _binding_with_persona_tier(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
    )
    assert _evaluate_cell_synchrony_tolerant(binding) is SynchronyClass.SYNC_BLOCKING


def test_evaluate_cell_synchrony_tolerant_returns_synchrony_for_durable_async_cell() -> None:
    """AC #2 — durable-async cell from §18.1 returns SynchronyClass.DURABLE_ASYNC.

    Cell sampled per CP test_persona_engine_hitl_matrix.py precedent:
    `(SOLO_DEVELOPER, RECONCILER_LOOP) → DURABLE_ASYNC`.
    """
    binding = _binding_with_persona_tier(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.RECONCILER_LOOP,
    )
    assert _evaluate_cell_synchrony_tolerant(binding) is SynchronyClass.DURABLE_ASYNC


def test_evaluate_cell_synchrony_tolerant_returns_excluded_for_excluded_cell() -> None:
    """AC #2 — excluded cell from §18.1 returns SynchronyClass.EXCLUDED.

    Excluded cell handling is delegated to the existing §14.8.2 step 4b
    `HITLCellExcludedError` raise per spec §14.8.8.7 invariant 1; this helper
    returns the raw synchrony class without filtering. Cell sampled:
    `(TEAM_BINDING, PURE_PATTERN_NO_ENGINE) → EXCLUDED`.
    """
    binding = _binding_with_persona_tier(
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
    )
    assert _evaluate_cell_synchrony_tolerant(binding) is SynchronyClass.EXCLUDED


def test_evaluate_cell_synchrony_tolerant_delegates_to_matrix_cell_for() -> None:
    """AC #2 — helper is a thin-wrap; result matches direct matrix_cell_for call.

    Cross-checks all 15 §18.1 cells against the helper's output to verify
    pure delegation per Q1 (α-revised) thin-wrap discipline.
    """
    for persona_tier in PersonaTier:
        for engine_class in EngineClass:
            binding = _binding_with_persona_tier(
                persona_tier=persona_tier, engine_class=engine_class
            )
            expected = matrix_cell_for(persona_tier, engine_class).synchrony_class
            assert _evaluate_cell_synchrony_tolerant(binding) is expected


# --- HITLPauseRequestedSignal ----------------------------------------------


def _build_brief() -> HITLEscalationBrief:
    """Test fixture brief for signal-carrier tests.

    Post-CP-v1.18 §25.2.X: ``HITLEscalationBrief.fail_class`` widened to
    ``ValidatorFailClass | None = None``. Post-CP-v1.19 §25.2.Y:
    ``HITLEscalationBrief.fail_detail_hash`` widened to ``str | None = None``.
    Durable-async cell pause-trigger construction site uses ``fail_class=None``
    + ``fail_detail_hash=None`` directly (the v2.24-era sentinel
    ``ValidatorFailClass.SCHEMA_VIOLATION + fail_detail_hash="0"*64``
    placeholder is fully retired per runtime plan v2.25 §7.1 AC #6 + v2.26
    U-RT-94 AC #13 absorption).
    """
    return HITLEscalationBrief(
        parent_step_id="step-1",
        parent_action_id="action-1",
        fail_class=None,
        fail_detail_hash=None,
        escalation_reason="durable_async_cell_synchrony",
        proposed_response_palette=frozenset(),
    )


def _build_delivery_result(*, delivered: bool = True) -> WebhookDeliveryResult:
    return WebhookDeliveryResult(
        delivered=delivered,
        status_code=200 if delivered else None,
        response_idempotency_key="hitl:action-1:wrap",
        delivery_attempts=1,
        final_attempt_at=1_700_000_000_000,
    )


def test_hitl_pause_requested_signal_inherits_base_exception_not_exception() -> None:
    """AC #3 — `HITLPauseRequestedSignal` inherits ``BaseException`` (not
    ``Exception``) per spec §14.8.8.2 inheritance-choice-rationale.

    Normal-path ``try / except Exception`` blocks MUST NOT suppress the signal.
    """
    assert issubclass(HITLPauseRequestedSignal, BaseException)
    assert not issubclass(HITLPauseRequestedSignal, Exception)


def test_hitl_pause_requested_signal_normal_except_exception_does_not_suppress() -> None:
    """AC #3 — empirical verification: ``except Exception`` does NOT catch
    ``HITLPauseRequestedSignal``. Only ``except BaseException`` (or the typed
    handler at U-RT-95 driver) consumes it.
    """
    sig = HITLPauseRequestedSignal(brief=_build_brief(), delivery_result=_build_delivery_result())
    suppressed = False
    try:
        try:
            raise sig
        except Exception:
            suppressed = True
    except HITLPauseRequestedSignal:
        pass
    assert suppressed is False


def test_hitl_pause_requested_signal_carries_brief_and_delivery_result() -> None:
    """AC #4 — signal carries 2 fields per spec §14.8.8.2 carrier definition.

    Constructor accepts ``brief`` + ``delivery_result`` as keyword-only;
    attributes round-trip without modification.
    """
    brief = _build_brief()
    delivery = _build_delivery_result()
    sig = HITLPauseRequestedSignal(brief=brief, delivery_result=delivery)
    assert sig.brief is brief
    assert sig.delivery_result is delivery


def test_hitl_pause_requested_signal_constructor_requires_keyword_only_fields() -> None:
    """AC #4 — both carrier fields are keyword-only per the documented constructor."""
    with pytest.raises(TypeError):
        HITLPauseRequestedSignal(  # pyright: ignore[reportCallIssue]
            _build_brief(),  # type: ignore[misc]
            _build_delivery_result(),  # type: ignore[misc]
        )
