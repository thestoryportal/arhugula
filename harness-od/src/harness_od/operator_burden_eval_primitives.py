"""Five operator-burden eval primitives + child-span emission — U-OD-23.

Implements C-OD-17 §17.1 (the five-primitive operator-burden eval set) and
§17.2 (the separate-child-span eval emission commitment — eval scores emit as
a separate child span attached to the parent span being evaluated;
span-event-only emission is non-conformant).

`EVAL_PRIMITIVE_DECLARATIONS` declares the five primitives per §17.1 verbatim;
`EVAL_EMISSION_CONTRACT` carries the §17.2 child-span commitment;
`emit_eval_as_child_span` emits an eval score as a child span (returning the
`ChildSpanRef`); `reject_span_event_only_emission` rejects the non-conformant
span-event-only emission.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.6.1 U-OD-23
(v2.6 M-1 revision — `ChildSpanRef` at `emit_eval_as_child_span` re-pointed to
the U-OD-04 carrier; no new edge, `[U-OD-04]` already present in v2.1; all
v2.1 surfaces preserved verbatim from v2.1 §3.6.1);
Spec_Operational_Discipline_v1_2.md §17 C-OD-17 §17.1 + §17.2 (preserved
verbatim into v1.3); ADR-D6 v1.1 §1.6 (online vs offline eval pattern — D6
commits separate child span emission at all cells).

Depends on: [U-OD-04, U-AS-NN (cross-axis: AS — C-AS-15 §15.4 + C-AS-14 §14.2),
U-CP-NN (cross-axis: CP — C-CP-20 §20.6)]. The cross-axis dependencies are
attribute-name surfaces (`sandbox.violation`, `anthropic.cache_*`,
`hitl.invocation.responded`) resolved at U-OD-34; the only non-OD-internal
types — `SpanRef` / `ChildSpanRef` — are carried at U-OD-04.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.otel_genai_base import ChildSpanRef, SpanRef

__all__ = [
    "EVAL_EMISSION_CONTRACT",
    "EVAL_PRIMITIVE_DECLARATIONS",
    "ComputationKind",
    "EmissionContractViolation",
    "EvalEmissionContract",
    "EvalPrimitiveDeclaration",
    "OperatorBurdenEvalPrimitive",
    "emit_eval_as_child_span",
    "reject_span_event_only_emission",
]


class OperatorBurdenEvalPrimitive(StrEnum):
    """The five operator-burden eval primitives (C-OD-17 §17.1, verbatim order).

    Canonical declaration order per the U-OD-23 §3.6.1 signature block:
    HITL invocations, sandbox violations, sandbox-tier routing accuracy,
    cache-hit-rate alignment floor, routing-accuracy holdout.
    """

    EXPECTED_HITL_INVOCATIONS_PER_SESSION = "expected_hitl_invocations_per_session"
    EXPECTED_SANDBOX_VIOLATIONS_PER_SESSION = "expected_sandbox_violations_per_session"
    SANDBOX_TIER_ROUTING_ACCURACY = "sandbox_tier_routing_accuracy"
    CACHE_HIT_RATE_ALIGNMENT_FLOOR = "cache_hit_rate_alignment_floor"
    ROUTING_ACCURACY_HOLDOUT = "routing_accuracy_holdout"


class ComputationKind(StrEnum):
    """The computation shape of an eval primitive (C-OD-17 §17.1).

    `COUNTER_ROLLUP` — primitives 1, 2 (counter rolled up over span counts).
    `HOLDOUT_META_JUDGE_RATIO` — primitives 3, 5 (holdout-evaluable meta-judge
    ratio). `RATIO_ROLLUP` — primitive 4 (cache-hit-rate ratio rollup).
    """

    COUNTER_ROLLUP = "COUNTER_ROLLUP"
    HOLDOUT_META_JUDGE_RATIO = "HOLDOUT_META_JUDGE_RATIO"
    RATIO_ROLLUP = "RATIO_ROLLUP"


class EvalPrimitiveDeclaration(BaseModel):
    """The per-primitive declaration record (C-OD-17 §17.1, verbatim content).

    Carries the §17.1 table content for one primitive: source ADR, declaration
    site (`None` for ADR-only), computation kind, rollup dimensions, source
    span class, computation formula (`None` unless §17.1 commits one), and
    whether the primitive is holdout-evaluable. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    primitive: OperatorBurdenEvalPrimitive
    source_adr: str
    declaration_site: str | None
    computation_kind: ComputationKind
    rollup_dimensions: tuple[str, ...]
    source_span_class: str
    computation_formula: str | None
    holdout_evaluable: bool


#: The five operator-burden eval primitives per C-OD-17 §17.1, verbatim
#: (acceptance #2 — exactly 5 entries in canonical order).
EVAL_PRIMITIVE_DECLARATIONS: tuple[EvalPrimitiveDeclaration, ...] = (
    EvalPrimitiveDeclaration(
        primitive=OperatorBurdenEvalPrimitive.EXPECTED_HITL_INVOCATIONS_PER_SESSION,
        source_adr="ADR-D5 v1.3 §1.8",
        declaration_site="C-CP-20 §20.6",
        computation_kind=ComputationKind.COUNTER_ROLLUP,
        rollup_dimensions=("agent_role", "workload_class"),
        source_span_class="hitl.invocation.responded",
        computation_formula=None,
        holdout_evaluable=False,
    ),
    EvalPrimitiveDeclaration(
        primitive=OperatorBurdenEvalPrimitive.EXPECTED_SANDBOX_VIOLATIONS_PER_SESSION,
        source_adr="ADR-D2 v1.1 §1.8",
        declaration_site="C-AS-15 §15.4",
        computation_kind=ComputationKind.COUNTER_ROLLUP,
        rollup_dimensions=("sandbox_tier", "blast_radius_tier"),
        source_span_class="sandbox.violation",
        computation_formula=None,
        holdout_evaluable=False,
    ),
    EvalPrimitiveDeclaration(
        primitive=OperatorBurdenEvalPrimitive.SANDBOX_TIER_ROUTING_ACCURACY,
        source_adr="ADR-D2 v1.1 §1.5",
        declaration_site=None,
        computation_kind=ComputationKind.HOLDOUT_META_JUDGE_RATIO,
        rollup_dimensions=(
            "per_tool_gate_level",
            "per_mcp_server_trust_tier",
            "persona_tier",
            "blast_radius_tier",
            "sandbox_tier",
        ),
        source_span_class="meta-eval",
        computation_formula=None,
        holdout_evaluable=True,
    ),
    EvalPrimitiveDeclaration(
        primitive=OperatorBurdenEvalPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR,
        source_adr="ADR-D3 v1.1 §1.5 + §1.8",
        declaration_site="C-AS-14 §14.2",
        computation_kind=ComputationKind.RATIO_ROLLUP,
        rollup_dimensions=("agent_role", "session"),
        source_span_class="anthropic.cache",
        computation_formula=(
            "anthropic.cache_read_input_tokens / (anthropic.cache_read_input_tokens "
            "+ anthropic.cache_creation_input_tokens)"
        ),
        holdout_evaluable=False,
    ),
    EvalPrimitiveDeclaration(
        primitive=OperatorBurdenEvalPrimitive.ROUTING_ACCURACY_HOLDOUT,
        source_adr="ADR-F1 v1.2 §Decision",
        declaration_site=None,
        computation_kind=ComputationKind.HOLDOUT_META_JUDGE_RATIO,
        rollup_dimensions=("fallback_chain",),
        source_span_class="meta-eval",
        computation_formula=None,
        holdout_evaluable=True,
    ),
)


class EvalEmissionContract(BaseModel):
    """The separate-child-span eval emission commitment (C-OD-17 §17.2).

    Per ADR-D6 v1.1 §1.6: D6 commits separate child span emission for eval
    scores at all cells, NOT span-event-only emission. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: eval scores emit as a separate child span — `True` per §17.2.
    child_span_emission_required: bool
    #: meta-eval-traceability rationale per `c8-eval-engineer` ownership.
    rationale: str
    #: the higher-span-volume tradeoff is accepted — `True` per §17.2.
    span_volume_tradeoff_accepted: bool
    #: the commitment binds every cell — `True` per §17.2.
    applies_at_all_cells: bool


#: The §17.2 separate-child-span eval emission commitment.
EVAL_EMISSION_CONTRACT: EvalEmissionContract = EvalEmissionContract(
    child_span_emission_required=True,
    rationale=(
        "Span-event-only emission collapses meta-eval traceability — meta-eval "
        "(eval-of-eval) cannot run over a span event without re-emission. "
        "Child-span emission preserves per-eval span identity that meta-eval "
        "requires per c8-eval-engineer SKILL.md."
    ),
    span_volume_tradeoff_accepted=True,
    applies_at_all_cells=True,
)


class EmissionContractViolation(Exception):  # noqa: N818 — name is the U-OD-23 plan signature verbatim
    """Raised when an eval emission violates the §17.2 child-span contract.

    The Python materialization of the `Result<_, EmissionContractViolation>`
    error arm in the U-OD-23 signatures — inline per the OD plan §0.8
    error-type discipline, stack is Pydantic v2 + stdlib (no `Result`
    framework pull — CLAUDE.md §3.2 / I-6).
    """


def emit_eval_as_child_span(
    parent_span_ref: SpanRef,
    primitive: OperatorBurdenEvalPrimitive,
    value: float,
) -> ChildSpanRef:
    """Emit an eval score as a separate child span (C-OD-17 §17.2).

    Per the §17.2 commitment, an eval score emits as a separate child span
    attached to `parent_span_ref` — NOT a span event. Returns the
    `ChildSpanRef` for the emitted child span (the `Ok` arm). The emission is
    contract-conformant by construction; a caller that attempts span-event-only
    emission is rejected by `reject_span_event_only_emission`.

    The child span is created via the OTel-SDK tracer bound to the parent's
    instrumentation scope, with the parent span set as the active context so
    the child inherits the parent's trace context (C-OD-04 §4.4).
    """
    from opentelemetry.trace import get_tracer_provider, set_span_in_context

    tracer = get_tracer_provider().get_tracer("harness-od.eval")
    parent_context = set_span_in_context(parent_span_ref)
    child: ChildSpanRef = tracer.start_span(
        name=f"eval {primitive.value}",
        context=parent_context,
    )
    child.set_attribute("eval.primitive", primitive.value)
    child.set_attribute("eval.value", value)
    child.end()
    return child


def reject_span_event_only_emission(
    parent_span_ref: SpanRef,
    primitive: OperatorBurdenEvalPrimitive,
    value: float,
) -> None:
    """Reject a span-event-only eval emission (C-OD-17 §17.2).

    Span-event-only emission of an eval score is non-conformant per §17.2 —
    it collapses meta-eval traceability. This function always raises
    `EmissionContractViolation`; the conformant path is
    `emit_eval_as_child_span`.
    """
    _ = (parent_span_ref, value)
    raise EmissionContractViolation(
        f"span-event-only emission of {primitive.value} is non-conformant "
        "(C-OD-17 §17.2 — eval scores MUST emit as a separate child span)"
    )
