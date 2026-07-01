"""Sampling discipline for sandbox events + audit-floor commitments — U-AS-18.

Implements C-AS-15 §15.4 (per-event sampling posture + audit-floor commitments).
Declares `SamplingPosture`, the `SAMPLING_POLICY`, and the `sampling_posture` /
`is_operator_tunable_at_base_rate` / `audit_floor_violated` functions.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-18 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §15.4 C-AS-15; ADR-D2 v1.2 §1.7.

Depends on: U-AS-17 (`SpanEventKind`). GUARDRAIL unit
(Plan_Executability_Audit_v1.md §3.2) — the always-sampled-with-tail-keep OTel
`Sampler` is project-authored downstream; this unit declares the policy + the
audit-floor predicate. `TOOL_CALL` is not in the §15.4 policy table;
`sampling_posture` defaults it to `BASE_RATE_MATCHES_PARENT` (tool calls
inherit the parent's base rate) — documented discretion.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from harness_as.sandbox_span_schema import SpanEventKind


class SamplingPosture(StrEnum):
    """A sandbox-event sampling posture (C-AS-15 §15.4)."""

    BASE_RATE_MATCHES_PARENT = "BASE_RATE_MATCHES_PARENT"
    ALWAYS_SAMPLED_HEAD_1_0 = "ALWAYS_SAMPLED_HEAD_1_0"
    ALWAYS_SAMPLED_WITH_TAIL_KEEP = "ALWAYS_SAMPLED_WITH_TAIL_KEEP"


#: Per-event sampling policy (C-AS-15 §15.4 verbatim).
SAMPLING_POLICY: Mapping[SpanEventKind, SamplingPosture] = MappingProxyType(
    {
        SpanEventKind.SANDBOX_ENTER: SamplingPosture.BASE_RATE_MATCHES_PARENT,
        SpanEventKind.SANDBOX_EXIT: SamplingPosture.BASE_RATE_MATCHES_PARENT,
        SpanEventKind.SANDBOX_VIOLATION: SamplingPosture.ALWAYS_SAMPLED_WITH_TAIL_KEEP,
        SpanEventKind.SANDBOX_TIER_ESCALATION: SamplingPosture.ALWAYS_SAMPLED_HEAD_1_0,
    }
)

# The §15.4 always-sampled hard floor — not operator-tunable at base rate.
_HARD_FLOOR_KINDS: frozenset[SpanEventKind] = frozenset(
    {SpanEventKind.SANDBOX_VIOLATION, SpanEventKind.SANDBOX_TIER_ESCALATION}
)


def sampling_posture(kind: SpanEventKind) -> SamplingPosture:
    """Return the sampling posture for a span event kind (C-AS-15 §15.4).

    `TOOL_CALL` (absent from the §15.4 table) defaults to
    `BASE_RATE_MATCHES_PARENT`.
    """
    return SAMPLING_POLICY.get(kind, SamplingPosture.BASE_RATE_MATCHES_PARENT)


def is_operator_tunable_at_base_rate(kind: SpanEventKind) -> bool:
    """True when a span event kind's sampling is operator-tunable at base rate.

    `sandbox.violation` and `sandbox.tier_escalation` are an always-sampled
    hard floor (§15.4) — not tunable; every other kind is base-rate tunable.
    """
    return kind not in _HARD_FLOOR_KINDS


def audit_floor_violated(
    proposed_policy: Mapping[SpanEventKind, SamplingPosture],
) -> bool:
    """True when a proposed policy violates the §15.4 audit floor.

    A violation is downgrading `sandbox.violation` or `sandbox.tier_escalation`
    from an always-sampled posture to `BASE_RATE_MATCHES_PARENT`.
    """
    return any(
        proposed_policy.get(kind) == SamplingPosture.BASE_RATE_MATCHES_PARENT
        for kind in _HARD_FLOOR_KINDS
    )
