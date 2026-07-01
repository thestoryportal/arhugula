"""HITL timeout-degradation + webhook delivery semantics — U-CP-52.

Implements C-CP-21 §21.8 (HITL timeout-degradation mode) + C-CP-18 §18.5 /
C-CP-21 §21.8 (webhook delivery semantics).

Declares the `TimeoutDegradationKind` enum, the `TimeoutDegradationPolicy`
record + `TIMEOUT_DEGRADATION_TABLE`, the `WebhookConfig` / `WebhookPayload`
records (faithful factor-outs per Implementation Plan v2.9 §0.3), the
`WebhookDeliveryEvent` record + `WebhookDeliveryOutcome` enum, the two
functions `on_hitl_timeout` / `deliver_webhook`, and the `fail-open` config
guard (`FailOpenDegradationRefusedError` + `validate_no_fail_open`).

`HITLInvocation` is consumed cross-cluster from U-CP-17 via the pre-existing
`[U-CP-17]` edge. `WebhookConfig` / `WebhookPayload` are the v2.9 §0.3 faithful
factor-outs of the C-CP-21 §21.8 idempotency-keyed webhook signal-delivery
contract; `payload_body` is opaque (`Mapping[str, Any]`) per the §21.8 deferred
clause. Webhook retry mechanics are delegated to the harness retry primitive
(hand-rolled — NO tenacity/pybreaker per CLAUDE.md §3.2).

**Vocabulary reconciliation (U-CP-92 / B3-impl-2).** The `TimeoutDegradationKind`
enum + `TIMEOUT_DEGRADATION_TABLE` were reconciled from the drifted vocab-B
`{continue-as-reject, escalate-to-review-board, abort-workflow}` to the
canonical vocab-A `{fail-closed, escalate-secondary-channel, fail-open}` per
F-B3-2 (`.harness/class_1_fork_b3_2_timeout_degradation_vocabulary_drift.md`,
operator-ratified 2026-06-14 — reconcile code → ADR-D5 §1.6 vocabulary; NO ADR
change), and the wrong-section `§21.6` timeout cite corrected to `§21.8` (real
§21.6 is validator-failure-span sampling; the per-tier timeout-degradation MODE
table is §21.8). The value-set agrees byte-exact with CP §21.8 + ADR-D5 §1.6 +
the CP §20.6 `hitl.timeout.degradation_mode_applied` span value-set. `fail-open`
is in the value-set but ADR/CP-granted to NO tier — refused at config/bootstrap
(`validate_no_fail_open`; register-don't-extend per F-B3-2 §2.5).

Authority: Implementation_Plan_Control_Plane_v2_9.md §2A U-CP-52 (REVISED v2.9
— `WebhookConfig` / `WebhookPayload` specified) + v2.33 U-CP-92 (vocab
reconciliation + fail-open guard); Spec_Control_Plane_v1_2.md §21 C-CP-21
§21.8 + §18 C-CP-18 §18.5; ADR-D5 §1.6 v1.3.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from harness_core import EntryID, PersonaTier
from harness_is.state_ledger_entry_schema import Identifier
from pydantic import BaseModel, ConfigDict, field_validator

from harness_cp.workload_binding_engine_class_selection import HITLInvocation

#: `Duration` — millisecond integer (C-CP-21 §21.3 vocabulary).
type Duration = int


class TimeoutDegradationKind(StrEnum):
    """The 3 HITL timeout-degradation modes (C-CP-21 §21.8; vocab-A).

    Reconciled from the drifted vocab-B `{continue-as-reject,
    escalate-to-review-board, abort-workflow}` per F-B3-2 (operator-ratified
    2026-06-14 — reconcile code → ADR-D5 §1.6; no ADR change). Byte-exact with
    CP §21.8 + ADR-D5 §1.6 + the CP §20.6 span value-set.
    """

    FAIL_CLOSED = "fail-closed"
    """Treat the timeout as a REJECT (deny the step; fail-safe). Solo default;
    team configurable; multi default."""

    ESCALATE_SECONDARY_CHANNEL = "escalate-secondary-channel"
    """Deliver the gate to the secondary channel (webhook) + pause/await. Team
    default; routes through the §14.8.8 durable-async webhook surface (NOT a
    review-board re-invocation — F-B3-2 §2.5)."""

    FAIL_OPEN = "fail-open"
    """In the value-set but ADR/CP-granted to NO tier — appears only in the
    multi-tenant *prohibition* clause (Persona §10.4). Refused at config/
    bootstrap (`validate_no_fail_open`); granting it to any tier owes ADR-D5
    §1.6 + CP §21.8 ratification (register-don't-extend per F-B3-2 §2.5)."""


class TimeoutDegradationPolicy(BaseModel):
    """One per-persona-tier timeout-degradation policy (C-CP-21 §21.8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    default_kind: TimeoutDegradationKind
    override_permitted: bool
    audit_required: bool
    """Always true."""


TIMEOUT_DEGRADATION_TABLE: tuple[TimeoutDegradationPolicy, ...] = (
    TimeoutDegradationPolicy(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        default_kind=TimeoutDegradationKind.FAIL_CLOSED,
        override_permitted=True,
        audit_required=True,
    ),
    TimeoutDegradationPolicy(
        persona_tier=PersonaTier.TEAM_BINDING,
        default_kind=TimeoutDegradationKind.ESCALATE_SECONDARY_CHANNEL,
        override_permitted=True,
        audit_required=True,
    ),
    TimeoutDegradationPolicy(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        default_kind=TimeoutDegradationKind.FAIL_CLOSED,
        override_permitted=False,
        audit_required=True,
    ),
)
"""The 3 per-persona-tier timeout-degradation policies, C-CP-21 §21.8 + ADR-D5
§1.6 verbatim. SOLO_DEVELOPER -> FAIL_CLOSED (override permitted); TEAM_BINDING
-> ESCALATE_SECONDARY_CHANNEL (default; FAIL_CLOSED configurable);
MULTI_TENANT_COMPLIANCE -> FAIL_CLOSED + alerting (override prohibited;
`fail-open` structurally prohibited per Persona §10.4). `fail-open` is granted
to NO tier — refused at config/bootstrap via `validate_no_fail_open`."""


class FailOpenDegradationRefusedError(Exception):
    """Raised when a timeout-degradation configuration assigns `fail-open`.

    `fail-open` is in the `TimeoutDegradationKind` value-set but is ADR/CP-
    granted to NO tier (CP §21.8 / ADR-D5 §1.6 / CP §20.6 list it only in the
    multi-tenant *prohibition* clause, Persona §10.4). Per the X-AL-3 anti-
    extension rule (F-B3-2 AC-1), a deployment configuring `fail-open` at ANY
    tier — or a webhook `degradation_mode` of `fail-open` — is refused at
    config/bootstrap (detect-then-refuse), never silently honored at the
    timeout path. Granting `fail-open` to a tier owes ADR-D5 §1.6 + CP §21.8
    ratification (register-don't-extend per F-B3-2 §2.5).

    Subclasses `Exception` (NOT `ValueError`) so it propagates raw out of the
    `WebhookConfig` field-validator rather than being wrapped in a pydantic
    `ValidationError` — a hard config refusal is a distinct typed surface.
    """


def validate_no_fail_open(
    table: tuple[TimeoutDegradationPolicy, ...] = TIMEOUT_DEGRADATION_TABLE,
) -> None:
    """Refuse any timeout-degradation policy assigning `fail-open` to a tier.

    The C10 `fail-open` guard (F-B3-2 AC-1 / runtime spec §14.8.9 AC-1). The
    canonical `TIMEOUT_DEGRADATION_TABLE` assigns `fail-open` to no tier, so
    this passes on the canonical table. **Register-don't-extend:** the per-tier
    operator-override surface (a deployment-supplied degradation table / config
    field) is NOT built at HEAD; when it lands it MUST call this at bootstrap
    before the timeout dispatch (U-RT-119) consults the table — that is where
    the guard fires fully (mirrors the G2c → O-CP-3 producer-not-built
    registration). Raises `FailOpenDegradationRefusedError` naming the
    offending tier(s).
    """
    offending = [
        p.persona_tier for p in table if p.default_kind is TimeoutDegradationKind.FAIL_OPEN
    ]
    if offending:
        raise FailOpenDegradationRefusedError(
            "fail-open is ADR/CP-granted to no tier (CP §21.8 / ADR-D5 §1.6); "
            f"refused for tier(s): {[t.value for t in offending]}"
        )


class WebhookConfig(BaseModel):
    """A per-webhook delivery descriptor (C-CP-18 §18.5 / C-CP-21 §21.8).

    Faithful factor-out per plan v2.9 §0.3 — the webhook-ingress contract for
    durable-async cells. No field invented.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    webhook_id: str
    endpoint_url: str
    timeout: Duration
    """Per the C-CP-17 §17.1.1 `hitl_gate` timeout."""

    degradation_mode: str
    """∈ {fail-closed, escalate-secondary-channel} per the C-CP-21 §21.8
    timeout-degradation table. `fail-open` refused per `_refuse_fail_open`."""

    @field_validator("degradation_mode")
    @classmethod
    def _refuse_fail_open(cls, value: str) -> str:
        """Detect-then-refuse a `fail-open` webhook degradation mode (F-B3-2
        AC-1). `fail-open` is ADR/CP-granted to no tier; a webhook configured
        with it is refused at construction (the one live per-config carrier of
        a degradation mode at HEAD)."""
        if value == TimeoutDegradationKind.FAIL_OPEN.value:
            raise FailOpenDegradationRefusedError(
                "WebhookConfig.degradation_mode='fail-open' is refused — "
                "fail-open is ADR/CP-granted to no tier (CP §21.8 / ADR-D5 §1.6)"
            )
        return value


class WebhookPayload(BaseModel):
    """An inbound webhook-signal record (C-CP-21 §21.8).

    Faithful factor-out per plan v2.9 §0.3 — keyed by the §21.8
    `(approval_id, idempotency_key)` pair; `payload_body` is opaque per the
    §21.8 deferred clause. No field invented.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    idempotency_key: Identifier
    """§21.8 idempotency-keyed signal delivery."""

    gate_evaluation_ref: EntryID
    """Ledger join key."""

    payload_body: Mapping[str, Any]
    """Opaque — §21.8 defers idempotency-key extraction from inbound payload."""


class WebhookDeliveryOutcome(StrEnum):
    """The outcome of a webhook delivery attempt (C-CP-21 §21.8)."""

    DELIVERED = "delivered"
    RETRY_PENDING = "retry-pending"
    EXHAUSTED_AFTER_RETRIES = "exhausted-after-retries"


class WebhookDeliveryEvent(BaseModel):
    """A webhook-delivery event record (C-CP-21 §21.8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    webhook_id: str
    workflow_id: str
    gate_evaluation_ref: EntryID
    payload_hash: str
    """`sha256(canonicalize(payload))`."""

    delivery_attempts: int
    delivery_outcome: WebhookDeliveryOutcome


def on_hitl_timeout(
    invocation: HITLInvocation | None, persona_tier: PersonaTier
) -> TimeoutDegradationKind:
    """Resolve the timeout-degradation mode for a timed-out HITL invocation.

    Per C-CP-21 §21.8: the degradation mode is the `TIMEOUT_DEGRADATION_TABLE`
    `default_kind` for `persona_tier` (vocab-A post-U-CP-92). The timeout emits
    an audit entry per the U-CP-46 `audit.*` attributes (the F2 entry written
    via U-IS-07 + U-IS-11). `invocation` is accepted for the U-CP-17 cross-
    cluster signature but is **persona_tier-only** in the body (the per-tier
    table is the sole determinant); it is widened to `HITLInvocation | None`
    (U-CP-92) so the runtime timeout dispatch (U-RT-119) can consult by
    persona-tier without constructing a `HITLInvocation`. The concrete
    audit-emission composes against the IS substrate at integration time.
    """
    _ = invocation
    policy = next(p for p in TIMEOUT_DEGRADATION_TABLE if p.persona_tier is persona_tier)
    return policy.default_kind


def deliver_webhook(webhook: WebhookConfig, payload: WebhookPayload) -> WebhookDeliveryEvent:
    """Deliver a webhook signal (C-CP-18 §18.5 / C-CP-21 §21.8).

    Webhook delivery is **idempotent** — duplicate delivery on retry does not
    produce duplicate workflow side effects (receiver-side dedup by the
    `gate_evaluation_ref` join). Retry mechanics delegate to the harness retry
    primitive (hand-rolled per CLAUDE.md §3.2). This is the delivery-semantics
    surface; the concrete HTTP delivery + retry loop composes at integration
    time. `payload_hash = sha256(canonicalize(payload))` per the §21.8
    signature discipline.
    """
    _ = (webhook, payload)
    raise NotImplementedError(
        "deliver_webhook composes the HTTP delivery + hand-rolled retry loop; "
        "the CP plan U-CP-52 unit declares the webhook delivery-semantics "
        "surface (C-CP-18 §18.5 / C-CP-21 §21.8)."
    )
