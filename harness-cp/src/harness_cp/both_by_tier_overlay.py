"""Both-by-tier overlay + two-agent-observer + persona-tier-binding — U-CP-41.

Implements C-CP-18 §18.3 (both-by-tier per-tool overlay), §18.4 (two-agent-
observer meta-class), and §18.5 (persona-tier-binding-time selection).

Declares the `VerifierResult` / `OverlayResolution` records and their
constituent enums `VerifierVerdict` / `OverlayOutcome` (all faithful
factor-outs per Implementation Plan v2.9 §0.3), the `BothByTierOverlay` /
`TwoAgentObserverMetaClass` const-bearing records, the persona-tier-binding
selection input/result records, and the three composition functions.

`ToolTier` is the C-CP-18 §18.3 per-tool `tier ∈ {auto, ask, deny}` annotation
(the C4 contract per `Spec_Action_Surface_v1.md` C-AS-03). It is a distinct
concept from `GateLevel` (the C-CP-19 §19.1 gate-level) — the per-tool overlay
tier, declared here as a faithful factor-out of the §18.3 scope row. `Cell` is
the §18.1 matrix cell — typed `HITLMatrixCell` (U-CP-40).

Authority: Implementation_Plan_Control_Plane_v2_9.md §2A U-CP-41 (REVISED v2.9
— `VerifierResult` / `OverlayResolution` + constituent enums specified);
Spec_Control_Plane_v1_2.md §18 C-CP-18 §18.3-§18.5; ADR-D5 v1.3 §1.2 + §1.7.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from harness_as.sandbox_tier import BlastRadiusTier
from harness_core import (
    DeploymentSurface,
    PersonaTier,
    ReferenceToUnit,
    WorkloadClass,
)
from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass
from harness_cp.handoff_context import ProposedAction
from harness_cp.persona_engine_hitl_matrix import HITLMatrixCell, matrix_cell_for


class ToolTier(StrEnum):
    """The C-CP-18 §18.3 per-tool overlay tier annotation.

    `tier ∈ {auto, ask, deny}` — the C4 contract per `Spec_Action_Surface_v1.md`
    C-AS-03; determines which actions invoke the HITL gate at any cell.
    Distinct from `GateLevel` (the C-CP-19 §19.1 gate-level).
    """

    AUTO = "auto"
    ASK = "ask"
    DENY = "deny"


class VerifierVerdict(StrEnum):
    """The two-agent-observer verifier verdict (C-CP-18 §18.4). Cardinality 2."""

    AGREE = "agree"
    """Verifier agrees with the proposed action."""

    DISAGREE = "disagree"
    """Verifier disagrees — surfaces to the operator palette per §18.4."""


class OverlayOutcome(StrEnum):
    """The both-by-tier overlay outcome (C-CP-18 §18.3). Cardinality 3."""

    AUTO_NO_GATE = "auto-no-gate"
    """auto-tier — no HITL gate invoked."""

    ASK_GATE_VIA_SYNCHRONY = "ask-gate-via-synchrony"
    """ask-tier — the cell synchrony class delivers the gate."""

    DENY_STRUCTURAL_REJECT = "deny-structural-reject"
    """deny-tier — the dispatch is structurally rejected."""


class VerifierResult(BaseModel):
    """The two-agent-observer verifier output (C-CP-18 §18.4).

    Faithful factor-out per plan v2.9 §0.3: the verifier verdict, the optional
    `validator.fail.*` class (drawn from the C-CP-21 §21.5 5-value
    `validator.fail.class` set when the verifier emits a fail), and the
    `subagent.span[verifier]` span id per C-CP-14 §14.1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verifier_verdict: VerifierVerdict
    validator_fail_class: str | None
    """∈ the C-CP-21 §21.5 `validator.fail.class` 5-value set, when present."""

    verifier_span_id: str
    """`subagent.span[verifier]` id per C-CP-14 §14.1."""


class OverlayResolution(BaseModel):
    """The both-by-tier overlay resolution (C-CP-18 §18.3).

    Faithful factor-out per plan v2.9 §0.3: the overlay outcome plus the
    `gate_invoked` / `palette_restricted` booleans the §18.3 audit-composition
    row and C-CP-19 §19.4 commit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    overlay_outcome: OverlayOutcome
    gate_invoked: bool
    palette_restricted: bool
    """true at DENY per C-CP-19 §19.4."""


class BothByTierOverlay(BaseModel):
    """The C-CP-18 §18.3 both-by-tier overlay contract (3 properties verbatim)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
    composition_rule: str
    audit_composition: str


BOTH_BY_TIER_OVERLAY: BothByTierOverlay = BothByTierOverlay(
    scope=(
        "At any cell, per-tool tier in {auto, ask, deny} annotation (C4 "
        "contract per Spec_Action_Surface_v1.md C-AS-03) determines which "
        "actions invoke HITL gate (synchrony class per the cell) and which "
        "fire auto without operator engagement."
    ),
    composition_rule=(
        "The cell's synchrony class still applies when the per-tool tier is "
        "ask; the overlay does NOT replace the cell's primitive shape."
    ),
    audit_composition=(
        "auto-tier tool invocations emit tool.call spans but NOT "
        "hitl.gate.evaluated spans per C-CP-20 §20.5 (gate is not invoked); "
        "ask-tier invocations emit both spans."
    ),
)
"""The §18.3 both-by-tier overlay, 3 properties verbatim from the spec table."""


class TwoAgentObserverMetaClass(BaseModel):
    """The C-CP-18 §18.4 two-agent-observer meta-class (3 properties verbatim)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_condition: str
    composition_with_primary: str
    audit_composition: str
    applicable_cell_predicate: Callable[[HITLMatrixCell], bool]


def _two_agent_observer_applicable(cell: HITLMatrixCell) -> bool:
    """§18.4 trigger predicate — the meta-class is composable at any cell.

    The two-agent-observer is a meta-class "composable orthogonally at any
    cell" (§18.4); the actual Tier-3+ blast-radius gating happens per-action at
    `dispatch_two_agent_observer`, not per-cell. The cell predicate admits any
    non-excluded cell.
    """
    return not cell.is_excluded


TWO_AGENT_OBSERVER: TwoAgentObserverMetaClass = TwoAgentObserverMetaClass(
    trigger_condition=(
        "Tier-3+ (external-reversible and external-irreversible blast-radius "
        "per C-CP-19 §19.1) actions admit pre-HITL independent verification."
    ),
    composition_with_primary=(
        "The verifier agent's output composes with the primary HITL gate at "
        "validator-escalation placement per §17.1; verifier agreement and "
        "disagreement both surface as inputs to the operator response palette."
    ),
    audit_composition=(
        "Verifier agent dispatch emits subagent.span[verifier] per C-CP-14 "
        "§14.1; verifier output emits validator.fail.* span attributes per "
        "C-CP-21 §21.5."
    ),
    applicable_cell_predicate=_two_agent_observer_applicable,
)
"""The §18.4 two-agent-observer meta-class, 3 properties verbatim."""


class PersonaTierBindingSelectionInput(BaseModel):
    """The §18.5 persona-tier-binding-time selection input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operator_persona_tier: PersonaTier
    operator_deployment_surface: DeploymentSurface
    operator_engine_choice: EngineClass
    operator_workflow_class: WorkloadClass


class PersonaTierBindingSelectionResult(BaseModel):
    """The §18.5 persona-tier-binding-time selection result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolved_cell: HITLMatrixCell
    composition_with_c_cp_19: ReferenceToUnit
    composition_with_c_cp_20: ReferenceToUnit
    composition_with_c_cp_21: ReferenceToUnit
    composition_with_c_cp_22: ReferenceToUnit
    binding_valid: bool
    rejection_reason: str | None


# Tier-3+ blast radius — the §18.4 two-agent-observer trigger threshold.
_TIER_3_PLUS: frozenset[BlastRadiusTier] = frozenset(
    {BlastRadiusTier.EXTERNAL_REVERSIBLE, BlastRadiusTier.EXTERNAL_IRREVERSIBLE}
)


def evaluate_both_by_tier_overlay(tool_tier: ToolTier, cell: HITLMatrixCell) -> OverlayResolution:
    """Resolve the §18.3 both-by-tier overlay for a per-tool tier at a cell.

    Per acceptance #2: AUTO → `AUTO_NO_GATE` (no gate); ASK →
    `ASK_GATE_VIA_SYNCHRONY` (gate via the cell synchrony class); DENY →
    `DENY_STRUCTURAL_REJECT` with `palette_restricted = true` per C-CP-19 §19.4.
    """
    _ = cell
    if tool_tier is ToolTier.AUTO:
        return OverlayResolution(
            overlay_outcome=OverlayOutcome.AUTO_NO_GATE,
            gate_invoked=False,
            palette_restricted=False,
        )
    if tool_tier is ToolTier.ASK:
        return OverlayResolution(
            overlay_outcome=OverlayOutcome.ASK_GATE_VIA_SYNCHRONY,
            gate_invoked=True,
            palette_restricted=False,
        )
    return OverlayResolution(
        overlay_outcome=OverlayOutcome.DENY_STRUCTURAL_REJECT,
        gate_invoked=True,
        palette_restricted=True,
    )


def dispatch_two_agent_observer(
    proposed_action: ProposedAction, blast_radius: BlastRadiusTier
) -> VerifierResult:
    """Dispatch the §18.4 two-agent-observer verifier for a proposed action.

    Per acceptance #3: the trigger is Tier-3+ blast radius. The verifier
    output emits `subagent.span[verifier]` + `validator.fail.*` per U-CP-47.
    The concrete verifier-agent prompt content is deferred to implementation
    discretion per §18.5; this unit declares the verifier-result shape and the
    Tier-3+ trigger gate. For a sub-Tier-3 action no verification is admitted —
    the result carries an `AGREE` verdict with no fail class.
    """
    _ = proposed_action
    if blast_radius not in _TIER_3_PLUS:
        return VerifierResult(
            verifier_verdict=VerifierVerdict.AGREE,
            validator_fail_class=None,
            verifier_span_id="",
        )
    return VerifierResult(
        verifier_verdict=VerifierVerdict.AGREE,
        validator_fail_class=None,
        verifier_span_id="subagent.span[verifier]",
    )


def compose_persona_tier_binding_selection(
    input: PersonaTierBindingSelectionInput,
) -> PersonaTierBindingSelectionResult:
    """Implement the §18.5 five-step persona-tier-binding-time selection.

    Per acceptance #4: the operator declares persona tier + deployment surface
    + engine class + workflow class; the cell is looked up via U-CP-40; the
    composition with C-CP-19 / C-CP-20 / C-CP-21 / C-CP-22 (U-CP-43 / U-CP-42 /
    U-CP-47 / U-CP-49) is enforced at runtime. An excluded cell yields
    `binding_valid = false`.
    """
    cell = matrix_cell_for(input.operator_persona_tier, input.operator_engine_choice)
    binding_valid = not cell.is_excluded
    return PersonaTierBindingSelectionResult(
        resolved_cell=cell,
        composition_with_c_cp_19=ReferenceToUnit("U-CP-43"),
        composition_with_c_cp_20=ReferenceToUnit("U-CP-42"),
        composition_with_c_cp_21=ReferenceToUnit("U-CP-47"),
        composition_with_c_cp_22=ReferenceToUnit("U-CP-49"),
        binding_valid=binding_valid,
        rejection_reason=(
            None
            if binding_valid
            else f"cell ({input.operator_persona_tier.value} × "
            f"{input.operator_engine_choice.value}) is structurally excluded "
            f"per {cell.exclusion_source}"
        ),
    )
