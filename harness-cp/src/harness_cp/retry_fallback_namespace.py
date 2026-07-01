"""`fallback.*` + `harness.breaker.*` + `retry.*` namespace schemas — U-CP-07.

Implements C-CP-03 §3.5 (the fallback / harness-breaker / retry span-attribute
namespace declarations + the v1.3 retry.* extension to a 6-attribute child-span
schema and a 3-field `retry.attempt` parent-event schema).

Declares `FallbackAttributeSchema` / `FALLBACK_NAMESPACE_SCHEMA` (9 entries),
`HarnessBreakerAttributeSchema` / `HARNESS_BREAKER_NAMESPACE_SCHEMA` (7),
`RetryAttributeSchema` / `RETRY_NAMESPACE_SCHEMA` (6), `RetryAttemptEventField`
/ `RETRY_ATTEMPT_EVENT_SCHEMA` (3), and the `RetryCause` enum (5).

**Substitution-mechanism note.** `harness.breaker.*` is substrate-anchored at
the `c9-reliability-recovery` skill; canonical schema at OD C-OD-07 §7.1 — this
unit emits the CP-side composition surface without claiming canonical
authorship. `retry.*` is substrate-anchored across `c9-reliability-recovery`,
`c5-validation-contract`, and ADR-D1 v1.2 §1.1.1 per ADR-D6 v1.2 §1.2.2.

**Partial-land — acceptance #7 struck.** U-CP-07 acceptance #7 (the runtime
dual-emission discipline — emit both the parent `retry.attempt` event and the
retry-attempt child span, with `parent_span_id`-linked child topology) is a
behavioural contract over an OTel span emitter, which does not exist at
sub-phase 7b L0. Per the halt-route-split-AC pattern, #7 + its 5 emission tests
are struck and routed to a downstream emitter unit. Defect record:
`.harness/class_1_tension_u_cp_07_dual_emission.md`. Acceptance #1–#6 — the
schema declarations — are landed in full below.

Authority: Implementation_Plan_Control_Plane_v2_3.md §2.1 U-CP-07 (v2.3
retry.* 6-attribute + 3-field extension; v2.6 §0.11 `[U-CP-00b]` edge-add);
Spec_Control_Plane_v1_3.md §3 C-CP-03 §3.5; ADR-D6 v1.2 §1.2.2.1 + §1.2.2.2;
OD C-OD-07 §7.1 (`harness.breaker.*` canonical schema).
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict


class RetryCause(StrEnum):
    """The 5-value internal retry-decision branching enum (C-CP-03 §3.5).

    Distinct from the `retry.cause_attribution` span attribute (the broader
    open-set C5 catalog) — `RetryCause` governs internal retry control flow
    and is consumed at U-CP-48 cause-attribution-conditioned branching.
    """

    TRANSIENT_PROVIDER_ERROR = "transient_provider_error"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CAPABILITY_SHORTFALL = "capability_shortfall"
    VALIDATOR_FAIL_TRANSIENT = "validator_fail_transient"


class FallbackAttributeSchema(BaseModel):
    """One `fallback.*` span attribute (C-CP-03 §3.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality


class HarnessBreakerAttributeSchema(BaseModel):
    """One `harness.breaker.*` span attribute (C-CP-03 §3.5 + OD C-OD-07 §7.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    source_authority: str
    """Per-attribute substrate-authority annotation — substrate-anchored
    outside CP at `c9-reliability-recovery`."""


class RetryAttributeSchema(BaseModel):
    """One `retry.*` child-span attribute (C-CP-03 §3.5 + ADR-D6 v1.2 §1.2.2.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    source_authority: str
    """Per-attribute substrate-authority annotation (v2.3 amendment)."""


class RetryAttemptEventField(BaseModel):
    """One field of the parent-span `retry.attempt` event (ADR-D6 v1.2 §1.2.2.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_name: str
    value_type: AttributeValueType
    optional: bool
    """`true` for `parent.next_delay_ms` — omitted at the retry-budget-exit
    boundary (`parent.attempts_remaining == 0`)."""


# --- fallback.* namespace (spec C-CP-03 §3.5 — 9 attributes) ----------------

_C = Cardinality

FALLBACK_NAMESPACE_SCHEMA: tuple[FallbackAttributeSchema, ...] = tuple(
    FallbackAttributeSchema(attribute_name=name, value_type=vt, cardinality=card)
    for name, vt, card in (
        ("fallback.layer", AttributeValueType.ENUM_REF, _C.LOW),
        ("fallback.candidate_chosen", AttributeValueType.STRING, _C.MEDIUM),
        ("fallback.candidates_skipped", AttributeValueType.STRING, _C.MEDIUM),
        ("fallback.cause", AttributeValueType.ENUM_REF, _C.LOW),
        ("fallback.cross_family", AttributeValueType.BOOL, _C.LOW),
        ("fallback.cross_family_triggered", AttributeValueType.BOOL, _C.LOW),
        ("fallback.exhausted", AttributeValueType.BOOL, _C.LOW),
        ("fallback.depth", AttributeValueType.INT, _C.MEDIUM),
        ("fallback.cache_state_lost", AttributeValueType.BOOL, _C.LOW),
    )
)
"""The 9 `fallback.*` attributes per C-CP-03 §3.5 verbatim."""


# --- harness.breaker.* namespace (spec C-CP-03 §3.5 + OD C-OD-07 §7.1 — 7) --

_BREAKER_AUTHORITY = "c9-reliability-recovery SKILL.md"

HARNESS_BREAKER_NAMESPACE_SCHEMA: tuple[HarnessBreakerAttributeSchema, ...] = tuple(
    HarnessBreakerAttributeSchema(
        attribute_name=name,
        value_type=vt,
        cardinality=card,
        source_authority=_BREAKER_AUTHORITY,
    )
    for name, vt, card in (
        ("harness.breaker.id", AttributeValueType.STRING, _C.MEDIUM),
        ("harness.breaker.state", AttributeValueType.ENUM_REF, _C.LOW),
        ("harness.breaker.scope", AttributeValueType.STRING, _C.MEDIUM),
        ("harness.breaker.trip_count", AttributeValueType.INT, _C.MEDIUM),
        ("harness.breaker.trip_window_seconds", AttributeValueType.INT, _C.LOW),
        ("harness.breaker.fail_count_in_window", AttributeValueType.INT, _C.MEDIUM),
        ("harness.breaker.fail_threshold", AttributeValueType.INT, _C.LOW),
    )
)
"""The 7 `harness.breaker.*` attributes per C-CP-03 §3.5 + OD C-OD-07 §7.1
canonical schema verbatim — each carrying `source_authority`."""


# --- retry.* namespace (spec C-CP-03 §3.5 + ADR-D6 v1.2 §1.2.2.1 — 6) -------

RETRY_NAMESPACE_SCHEMA: tuple[RetryAttributeSchema, ...] = (
    RetryAttributeSchema(
        attribute_name="retry.attempt_number",
        value_type=AttributeValueType.INT,
        cardinality=_C.MEDIUM,
        source_authority="c9-reliability-recovery SKILL.md",
    ),
    RetryAttributeSchema(
        attribute_name="retry.original_span_id",
        value_type=AttributeValueType.STRING,
        cardinality=_C.PER_REQUEST,
        source_authority="ADR-D1 v1.2 §1.1.2.2 (F2 state-ledger entry shape)",
    ),
    RetryAttributeSchema(
        attribute_name="retry.delay_ms",
        value_type=AttributeValueType.INT,
        cardinality=_C.MEDIUM,
        source_authority="c9-reliability-recovery SKILL.md",
    ),
    RetryAttributeSchema(
        attribute_name="retry.cause_attribution",
        value_type=AttributeValueType.STRING,
        cardinality=_C.MEDIUM,
        source_authority="c5-validation-contract SKILL.md s14 §7.5(a)",
    ),
    RetryAttributeSchema(
        attribute_name="retry.fail_class",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=_C.LOW,
        source_authority="c5-validation-contract SKILL.md s14 §7.5(d)",
    ),
    RetryAttributeSchema(
        attribute_name="engine.replay_disposition",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=_C.LOW,
        source_authority="ADR-D1 v1.2 §1.1.1 (inherits parent operation value)",
    ),
)
"""The 6 `retry.*` child-span attributes per C-CP-03 §3.5 + ADR-D6 v1.2
§1.2.2.1 verbatim (v2.3 amendment from 4)."""


# --- retry.attempt parent-event schema (ADR-D6 v1.2 §1.2.2.2 — 3 fields) ----

RETRY_ATTEMPT_EVENT_SCHEMA: tuple[RetryAttemptEventField, ...] = (
    RetryAttemptEventField(
        field_name="parent.attempt_count",
        value_type=AttributeValueType.INT,
        optional=False,
    ),
    RetryAttemptEventField(
        field_name="parent.attempts_remaining",
        value_type=AttributeValueType.INT,
        optional=False,
    ),
    RetryAttemptEventField(
        field_name="parent.next_delay_ms",
        value_type=AttributeValueType.INT,
        optional=True,
    ),
)
"""The 3 `retry.attempt` parent-event fields per ADR-D6 v1.2 §1.2.2.2 verbatim
— `parent.next_delay_ms` is optional (omitted at retry-budget-exit boundary)."""
