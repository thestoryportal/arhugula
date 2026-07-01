"""Eval-vs-runtime-gate distinction via `gen_ai.eval.kind` discriminator — U-OD-26.

Implements C-OD-18 §18.3 (eval-vs-runtime-gate distinction). Two span shapes
coexist on the runtime path — in-loop gate spans (runtime; deterministic
validation per `c5-validation-contract`) and out-of-loop eval child spans
(meta-eval per `c8-eval-engineer`). The distinction is enforceable via the
`gen_ai.eval.kind` discriminator attribute, a 2-value enum (`inline_gate` /
`offline_judge`).

`classify_eval_span` reads the discriminator off a span's attribute bag;
`validate_eval_span_routing` enforces the §18.3 shape invariants — an
`inline_gate` MUST NOT be emitted as a separate child span and MUST carry
`validator.fail.*` attributes; an `offline_judge` MUST be a separate child
span (per C-OD-17 §17.2) and MUST carry an operator-burden eval primitive
reference. The distinction is non-mergeable: no span satisfies both.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.6.4 U-OD-26
(v2.6 M-1 revision — `SpanAttributes` at `classify_eval_span` re-pointed to the
U-OD-04 carrier, `[U-OD-04]` edge added; all v2.1 surfaces preserved verbatim
from v2.1 §3.6.4); Depends on: [U-OD-23, U-OD-04, U-CP-NN (cross-axis: CP —
C-CP-21 §21.5)]; Spec_Operational_Discipline_v1_2.md §18 C-OD-18 §18.3
(preserved verbatim into v1.3); ADR-D6 v1.x (drift-detection +
eval-vs-runtime-gate). `EvalShapeViolation` is inline-materialized per the
§0.8 error-type discipline.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.otel_genai_base import SpanAttributes, SpanRef

__all__ = [
    "EVAL_KIND_ATTRIBUTE_NAME",
    "EVAL_SPAN_SHAPES",
    "EvalKindDiscriminator",
    "EvalShapeViolation",
    "EvalSpanRouting",
    "EvalSpanShape",
    "SamplingPostureF18",
    "classify_eval_span",
    "validate_eval_span_routing",
]


class EvalKindDiscriminator(StrEnum):
    """The `gen_ai.eval.kind` discriminator — exactly 2 values (C-OD-18 §18.3).

    `INLINE_GATE` — in-loop runtime gate per `c5-validation-contract`;
    pass/fail routes per C-CP-21 §21.5 + C-AS-04 §4.2. `OFFLINE_JUDGE` —
    out-of-loop meta-eval per `c8-eval-engineer`; emission per C-OD-17 §17.2
    separate-child-span discipline. The member string values are the §18.3
    `gen_ai.eval.kind ∈ {"inline_gate", "offline_judge"}` set verbatim.
    """

    INLINE_GATE = "inline_gate"
    OFFLINE_JUDGE = "offline_judge"


#: The `gen_ai.eval.kind` discriminator attribute name, byte-exact (§18.3).
EVAL_KIND_ATTRIBUTE_NAME: str = "gen_ai.eval.kind"


class SamplingPostureF18(StrEnum):
    """The per-eval-kind sampling posture (C-OD-18 §18.3).

    `ALWAYS_SAMPLED_IF_FAILURE_BASE_RATE_IF_PASS` — the `inline_gate` posture:
    always-sampled if failure per C-CP-21 §21.6 (`validator.fail.permanence =
    permanent`); base-rate if pass per C-CP-21 §21.5. `SEPARATE_CHILD_SPAN_PER_U_OD_23`
    — the `offline_judge` posture per C-OD-17 §17.2 separate-child-span emission.
    """

    ALWAYS_SAMPLED_IF_FAILURE_BASE_RATE_IF_PASS = "ALWAYS_SAMPLED_IF_FAILURE_BASE_RATE_IF_PASS"
    SEPARATE_CHILD_SPAN_PER_U_OD_23 = "SEPARATE_CHILD_SPAN_PER_U_OD_23"


class EvalSpanShape(BaseModel):
    """The §18.3 span shape committed for one `EvalKindDiscriminator` value.

    Carries the discriminator value, the sampling posture, the source
    declaration reference, and the optional failure routing. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the eval-kind discriminator value this shape is for.
    discriminator_value: EvalKindDiscriminator
    #: the per-kind sampling posture (§18.3).
    sampling_posture: SamplingPostureF18
    #: the source declaration reference (§18.3 "Source declaration" column).
    source_declaration_ref: str
    #: the failure routing — `None` for `offline_judge` (§18.3).
    failure_routing: str | None


#: The §18.3 eval span shapes — exactly 2 entries, one per `EvalKindDiscriminator`
#: value (acceptance #3). `INLINE_GATE` per §18.3 row 1; `OFFLINE_JUDGE` per
#: §18.3 row 2.
EVAL_SPAN_SHAPES: dict[EvalKindDiscriminator, EvalSpanShape] = {
    EvalKindDiscriminator.INLINE_GATE: EvalSpanShape(
        discriminator_value=EvalKindDiscriminator.INLINE_GATE,
        sampling_posture=SamplingPostureF18.ALWAYS_SAMPLED_IF_FAILURE_BASE_RATE_IF_PASS,
        source_declaration_ref="C-CP-21 §21.5",
        failure_routing="C-CP-21 §21.6 + C-AS-04 §4.2",
    ),
    EvalKindDiscriminator.OFFLINE_JUDGE: EvalSpanShape(
        discriminator_value=EvalKindDiscriminator.OFFLINE_JUDGE,
        sampling_posture=SamplingPostureF18.SEPARATE_CHILD_SPAN_PER_U_OD_23,
        source_declaration_ref="U-OD-23 (C-OD-17 §17.2)",
        failure_routing=None,
    ),
}


class EvalSpanRouting(BaseModel):
    """The observed routing of an eval span — checked against its §18.3 shape.

    The runtime caller of `validate_eval_span_routing` fills this from the
    actual span: whether the span was emitted as a separate child span,
    whether it carries `validator.fail.*` attributes, and whether it carries
    an operator-burden eval primitive reference (per U-OD-23). Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: `True` if the span was emitted as a separate child span.
    emitted_as_child_span: bool
    #: `True` if the span carries `validator.fail.*` attributes.
    has_validator_fail_attributes: bool
    #: `True` if the span carries an operator-burden eval primitive reference.
    has_operator_burden_eval_reference: bool


class EvalShapeViolation(Exception):  # noqa: N818 — name is the U-OD-26 plan signature verbatim
    """Raised when an eval span violates its §18.3 shape invariants.

    The Python materialization of the plan's `Result<(), EvalShapeViolation>`
    error branch (the §0.8 error-type discipline: a `Result<(), E>` is
    `-> None` on success and `raise EvalShapeViolation` on the error branch —
    the landed-unit convention, see U-OD-23 `EmissionContractViolation`).
    """


def classify_eval_span(attrs: SpanAttributes) -> EvalKindDiscriminator | None:
    """Classify a span by its `gen_ai.eval.kind` discriminator (C-OD-18 §18.3).

    Reads `EVAL_KIND_ATTRIBUTE_NAME` off the span attribute bag (`attrs`, the
    U-OD-04 `SpanAttributes` alias of the OTel attribute map). Returns the
    matching `EvalKindDiscriminator` if the attribute is present and carries a
    valid §18.3 value; returns `None` if the attribute is absent or carries an
    unrecognized value (acceptance #6 — `None` if absent).
    """
    if attrs is None:
        return None
    raw = attrs.get(EVAL_KIND_ATTRIBUTE_NAME)
    if not isinstance(raw, str):
        return None
    try:
        return EvalKindDiscriminator(raw)
    except ValueError:
        return None


def validate_eval_span_routing(
    discriminator: EvalKindDiscriminator,
    span_ref: SpanRef,
    routing: EvalSpanRouting,
) -> None:
    """Validate an eval span's routing against its §18.3 shape (C-OD-18 §18.3).

    Returns (`None`) iff the span's observed `routing` conforms to the §18.3
    shape for `discriminator`. Raises `EvalShapeViolation` when (acceptance #7):

    - an `inline_gate` is emitted as a separate child span;
    - an `inline_gate` lacks `validator.fail.*` attributes;
    - an `offline_judge` is NOT emitted as a separate child span (i.e. emitted
      as a span event);
    - an `offline_judge` lacks an operator-burden eval primitive reference.

    The distinction is non-mergeable (acceptance #8): the two branches enforce
    disjoint invariants, so no span can satisfy both. `span_ref` is the live
    OTel-SDK span handle (`SpanRef`, carried at U-OD-04) the routing was
    observed on; it is threaded for correlation.
    """
    _ = span_ref  # span handle threaded for correlation
    if discriminator is EvalKindDiscriminator.INLINE_GATE:
        if routing.emitted_as_child_span:
            raise EvalShapeViolation(
                "inline_gate span MUST NOT be emitted as a separate child span (C-OD-18 §18.3)"
            )
        if not routing.has_validator_fail_attributes:
            raise EvalShapeViolation(
                "inline_gate span MUST carry validator.fail.* attributes "
                "(C-OD-18 §18.3; C-CP-21 §21.5)"
            )
        return
    # discriminator is EvalKindDiscriminator.OFFLINE_JUDGE
    if not routing.emitted_as_child_span:
        raise EvalShapeViolation(
            "offline_judge span MUST be emitted as a separate child span "
            "(C-OD-18 §18.3; C-OD-17 §17.2)"
        )
    if not routing.has_operator_burden_eval_reference:
        raise EvalShapeViolation(
            "offline_judge span MUST carry an operator-burden eval primitive "
            "reference (C-OD-18 §18.3; U-OD-23)"
        )
