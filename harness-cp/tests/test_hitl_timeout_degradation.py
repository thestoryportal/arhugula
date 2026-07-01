"""Tests for U-CP-52 — HITL timeout-degradation + webhook delivery (C-CP-21).

Lands the v2.9-revised body. Acceptance-criterion coverage:
  #1 TimeoutDegradationKind 3    -> test_timeout_degradation_three_kinds
  #2 degradation table 3 entries -> test_timeout_degradation_table_three_entries
  #3 on_hitl_timeout per tier    -> test_on_hitl_timeout_per_persona_tier
  #6 delivery outcome 3 values   -> test_webhook_delivery_outcome_three
  #8 WebhookConfig 4 fields      -> test_webhook_config_four_fields_cp_21_8
  #8 WebhookPayload 4 fields     -> test_webhook_payload_four_fields_cp_21_8
                                    test_webhook_payload_body_opaque_no_invented_fields
  #4 retry delegated             -> test_deliver_webhook_surface
"""

from __future__ import annotations

import pytest
from harness_core import ActionID, EntryID, PersonaTier
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import (
    ActionKind,
    HandoffContext,
    LedgerEntryRef,
    ProposedAction,
    RetryHistory,
    StateSummary,
)
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.hitl_timeout_degradation import (
    TIMEOUT_DEGRADATION_TABLE,
    FailOpenDegradationRefusedError,
    TimeoutDegradationKind,
    TimeoutDegradationPolicy,
    WebhookConfig,
    WebhookDeliveryOutcome,
    WebhookPayload,
    deliver_webhook,
    on_hitl_timeout,
    validate_no_fail_open,
)
from harness_cp.workload_binding_engine_class_selection import HITLInvocation
from harness_is.state_ledger_entry_schema import Identifier


def _invocation() -> HITLInvocation:
    ctx = HandoffContext(
        proposed_action=ProposedAction(
            action_kind=ActionKind.INFERENCE_STEP, payload={}, brief=None
        ),
        agent_confidence=None,
        failed_attempts=(),
        alternatives_considered=(),
        state_summary=StateSummary(
            relevant_entries=(),
            summary_text="s",
            summary_hash="0" * 64,
            idempotency_key=Identifier("k"),
            external_references=(),
        ),
        audit_trail_link=LedgerEntryRef(
            action_id=ActionID("a0"), entry_hash="0" * 64, actor=ActorIdentity("op")
        ),
        retry_history=RetryHistory(attempts=(), retry_count=0),
    )
    return HITLInvocation(
        invocation_id="inv-0",
        placement="pre-action",
        handoff_context=ctx,
        response_palette=frozenset(HITLResponse),
        timeout=None,
        cascade_policy="pause",
        opened_at="2026-05-16T00:00:00Z",
    )


def test_timeout_degradation_three_kinds() -> None:
    """#1 — TimeoutDegradationKind declares exactly three values per §21.8."""
    assert len(TimeoutDegradationKind) == 3


def test_timeout_degradation_kinds_vocab_a(  # U-CP-92 AC-2
) -> None:
    """U-CP-92 AC-2 — the value-set is vocab-A `{fail-closed,
    escalate-secondary-channel, fail-open}` (CP §21.8 + ADR-D5 §1.6 + the CP
    §20.6 span value-set), NOT the drifted vocab-B `{continue-as-reject,
    escalate-to-review-board, abort-workflow}`. By execution."""
    assert {k.value for k in TimeoutDegradationKind} == {
        "fail-closed",
        "escalate-secondary-channel",
        "fail-open",
    }


def test_timeout_degradation_table_three_entries() -> None:
    """#2 / U-CP-92 AC-2 — TIMEOUT_DEGRADATION_TABLE declares the 3 §21.8
    per-tier rows in vocab-A (solo→fail-closed; team→escalate-secondary-channel;
    multi→fail-closed)."""
    assert len(TIMEOUT_DEGRADATION_TABLE) == 3
    by_tier = {p.persona_tier: p for p in TIMEOUT_DEGRADATION_TABLE}
    assert by_tier[PersonaTier.SOLO_DEVELOPER].default_kind is TimeoutDegradationKind.FAIL_CLOSED
    assert by_tier[PersonaTier.SOLO_DEVELOPER].override_permitted is True
    assert (
        by_tier[PersonaTier.TEAM_BINDING].default_kind
        is TimeoutDegradationKind.ESCALATE_SECONDARY_CHANNEL
    )
    assert by_tier[PersonaTier.TEAM_BINDING].override_permitted is True
    mtc = by_tier[PersonaTier.MULTI_TENANT_COMPLIANCE]
    assert mtc.default_kind is TimeoutDegradationKind.FAIL_CLOSED
    assert mtc.override_permitted is False
    assert all(p.audit_required for p in TIMEOUT_DEGRADATION_TABLE)


def test_timeout_table_multi_is_not_abort_workflow() -> None:
    """U-CP-92 AC-2 (contrasting-baseline) — multi-tenant-compliance resolves to
    FAIL_CLOSED, NOT the drifted vocab-B `abort-workflow` (the materially-
    different compliance-tier disposition the drift carried: fail-closed
    continues the workflow with a denied step + alerting; abort-workflow was a
    terminal stop). `abort-workflow` is no longer a value at all."""
    by_tier = {p.persona_tier: p for p in TIMEOUT_DEGRADATION_TABLE}
    multi = by_tier[PersonaTier.MULTI_TENANT_COMPLIANCE].default_kind
    assert multi is TimeoutDegradationKind.FAIL_CLOSED
    assert multi.value != "abort-workflow"
    assert "abort-workflow" not in {k.value for k in TimeoutDegradationKind}


def test_on_hitl_timeout_per_persona_tier() -> None:
    """#3 — on_hitl_timeout returns the per-tier degradation mode (vocab-A)."""
    inv = _invocation()
    assert on_hitl_timeout(inv, PersonaTier.SOLO_DEVELOPER) is TimeoutDegradationKind.FAIL_CLOSED
    assert (
        on_hitl_timeout(inv, PersonaTier.TEAM_BINDING)
        is TimeoutDegradationKind.ESCALATE_SECONDARY_CHANNEL
    )
    assert (
        on_hitl_timeout(inv, PersonaTier.MULTI_TENANT_COMPLIANCE)
        is TimeoutDegradationKind.FAIL_CLOSED
    )


def test_on_hitl_timeout_accepts_none_invocation() -> None:
    """U-CP-92 — `on_hitl_timeout` is persona_tier-only; the `invocation` arg is
    widened to `HITLInvocation | None` so the runtime timeout dispatch (U-RT-119)
    can consult by persona-tier without constructing a `HITLInvocation`."""
    assert on_hitl_timeout(None, PersonaTier.SOLO_DEVELOPER) is TimeoutDegradationKind.FAIL_CLOSED


def test_webhook_delivery_outcome_three() -> None:
    """#6 — WebhookDeliveryOutcome declares exactly three values."""
    assert len(WebhookDeliveryOutcome) == 3
    assert {o.value for o in WebhookDeliveryOutcome} == {
        "delivered",
        "retry-pending",
        "exhausted-after-retries",
    }


def test_webhook_config_four_fields_cp_21_8() -> None:
    """#8 — WebhookConfig declares exactly four fields."""
    assert set(WebhookConfig.model_fields) == {
        "webhook_id",
        "endpoint_url",
        "timeout",
        "degradation_mode",
    }


def test_webhook_payload_four_fields_cp_21_8() -> None:
    """#8 — WebhookPayload declares exactly four fields."""
    assert set(WebhookPayload.model_fields) == {
        "approval_id",
        "idempotency_key",
        "gate_evaluation_ref",
        "payload_body",
    }


def test_webhook_payload_body_opaque_no_invented_fields() -> None:
    """#8 — WebhookPayload.payload_body is opaque (any mapping shape)."""
    payload = WebhookPayload(
        approval_id="ap-0",
        idempotency_key=Identifier("k0"),
        gate_evaluation_ref=EntryID("e0"),
        payload_body={"arbitrary": "shape", "n": 1},
    )
    assert payload.payload_body["n"] == 1
    empty = WebhookPayload(
        approval_id="ap-1",
        idempotency_key=Identifier("k1"),
        gate_evaluation_ref=EntryID("e1"),
        payload_body={},
    )
    assert empty.payload_body == {}


def test_deliver_webhook_surface() -> None:
    """#4 — deliver_webhook declares the delivery-semantics surface."""
    config = WebhookConfig(
        webhook_id="wh-0",
        endpoint_url="https://example/hook",
        timeout=5000,
        degradation_mode="fail-closed",
    )
    payload = WebhookPayload(
        approval_id="ap-0",
        idempotency_key=Identifier("k0"),
        gate_evaluation_ref=EntryID("e0"),
        payload_body={},
    )
    with pytest.raises(NotImplementedError):
        deliver_webhook(config, payload)


# --- U-CP-92 AC-1: fail-open refused at ALL tiers (C10 guard + X-AL-3) -------


def test_validate_no_fail_open_passes_canonical_table() -> None:
    """U-CP-92 AC-1 — the canonical table assigns fail-open to no tier, so the
    guard passes (no raise). `fail-open` is registered-not-granted."""
    validate_no_fail_open()  # canonical TIMEOUT_DEGRADATION_TABLE — no raise


@pytest.mark.parametrize(
    "tier",
    [
        PersonaTier.SOLO_DEVELOPER,
        PersonaTier.TEAM_BINDING,
        PersonaTier.MULTI_TENANT_COMPLIANCE,
    ],
)
def test_validate_no_fail_open_refuses_at_every_tier(tier: PersonaTier) -> None:
    """U-CP-92 AC-1 (contrasting-baseline at ALL tiers) — a deployment-supplied
    table assigning `fail-open` at ANY tier is refused (detect-then-refuse).
    multi-tenant-compliance is the explicit ADR/CP prohibition; solo/team are
    not-yet-granted (a runtime extension beyond the cleared authorities).
    Mirrors the F-B3-1 register-don't-extend / multi structural-foreclosure."""
    rogue = (
        TimeoutDegradationPolicy(
            persona_tier=tier,
            default_kind=TimeoutDegradationKind.FAIL_OPEN,
            override_permitted=True,
            audit_required=True,
        ),
    )
    with pytest.raises(FailOpenDegradationRefusedError):
        validate_no_fail_open(rogue)


def test_webhook_config_refuses_fail_open_degradation_mode() -> None:
    """U-CP-92 AC-1 — the one live per-config carrier of a degradation mode
    (`WebhookConfig.degradation_mode`) refuses `fail-open` at construction. The
    typed `FailOpenDegradationRefusedError` propagates raw (NOT wrapped in a
    pydantic `ValidationError`)."""
    with pytest.raises(FailOpenDegradationRefusedError):
        WebhookConfig(
            webhook_id="wh-fo",
            endpoint_url="https://example/hook",
            timeout=5000,
            degradation_mode="fail-open",
        )


def test_webhook_config_accepts_granted_degradation_modes() -> None:
    """U-CP-92 AC-1 (contrasting baseline) — the 2 ADR/CP-granted modes are
    accepted; only `fail-open` is refused."""
    for mode in ("fail-closed", "escalate-secondary-channel"):
        cfg = WebhookConfig(
            webhook_id=f"wh-{mode}",
            endpoint_url="https://example/hook",
            timeout=5000,
            degradation_mode=mode,
        )
        assert cfg.degradation_mode == mode
