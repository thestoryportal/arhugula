"""Brief-authoring model-binding inheritance table — U-CP-29.

Implements C-CP-13 §13.3 (the brief-authoring model-binding inheritance table).
Declares the `InheritanceRule` 2-value enum, the `BriefAuthoringInheritance`
record, the 4-entry `BRIEF_AUTHORING_INHERITANCE` table — one per
`WorkloadClass` — and the `inheritance_for` accessor.

This unit **declares the inheritance rule only** (C-CP-13 §13.3 + plan
acceptance #3). The actual brief-authoring model binding is resolved against
the AS-side per-sub-agent-role x model-binding catalog (U-AS-29); brief-
authoring binding is **not independently configurable** (§13.3 closing
sentence — `reducible_to_haiku` is always `False` per ADR-D3 v1.2 §1.4).

**Partial-land — `resolve_brief_authoring_model_binding` struck.** Acc #3's
`resolve_brief_authoring_model_binding(...) -> ModelBinding` delegator is a
cross-axis seam: U-AS-29's catalog returns an AS-axis `ModelBinding`
(`primary_model` / `qualifier` / `cap`) which is structurally distinct from
the U-CP-00c `ModelBinding` (`provider` / `model`). Reconciling the two is a
CXA v2.1 §2.3.3 (CP->AS bucket) concern resolved at sub-phase 7c, not a 7b
unit-internal surface. The delegator + `test_delegates_to_u_as_29` are struck;
the inheritance table (accs #1, #2, #4) is landed in full. Defect record:
`.harness/class_1_tension_u_cp_29_cross_axis_modelbinding_seam.md`.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.5 U-CP-29 (v2.6 §0.12
`[U-CP-00]` edge-add — CONFORM); Spec_Control_Plane_v1_2.md §13 C-CP-13 §13.3
(preserved verbatim into v1.3); ADR-D3 v1.2 §1.4 (NOT-reducible-to-Haiku).
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import WorkloadClass
from pydantic import BaseModel, ConfigDict


class InheritanceRule(StrEnum):
    """The brief-authoring inheritance rule (C-CP-13 §13.3)."""

    INHERIT_LEAD_BINDING = "inherit_lead_binding"
    """Brief-authoring inherits the lead-agent's model binding."""

    INHERIT_PER_STAGE_LEAD_BINDING = "inherit_per_stage_lead_binding"
    """Brief-authoring inherits the per-stage lead-agent binding."""


class BriefAuthoringInheritance(BaseModel):
    """One workload class's brief-authoring inheritance rule (C-CP-13 §13.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workload_class: WorkloadClass
    inheritance_rule: InheritanceRule
    reducible_to_haiku: bool
    """Always `False` per ADR-D3 v1.2 §1.4 — brief authoring is never reduced
    to Haiku."""

    narrative: str


# --- Registry population (C-CP-13 §13.3 4-row table) ------------------------

BRIEF_AUTHORING_INHERITANCE: tuple[BriefAuthoringInheritance, ...] = (
    BriefAuthoringInheritance(
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        inheritance_rule=InheritanceRule.INHERIT_LEAD_BINDING,
        reducible_to_haiku=False,
        narrative="Software-engineering brief authoring inherits the lead-agent binding.",
    ),
    BriefAuthoringInheritance(
        workload_class=WorkloadClass.CONTENT_CREATION,
        inheritance_rule=InheritanceRule.INHERIT_LEAD_BINDING,
        reducible_to_haiku=False,
        narrative="Content-creation brief authoring inherits the lead-agent binding.",
    ),
    BriefAuthoringInheritance(
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        inheritance_rule=InheritanceRule.INHERIT_PER_STAGE_LEAD_BINDING,
        reducible_to_haiku=False,
        narrative="Pipeline-automation brief authoring inherits the per-stage lead binding.",
    ),
    BriefAuthoringInheritance(
        workload_class=WorkloadClass.RESEARCH,
        inheritance_rule=InheritanceRule.INHERIT_LEAD_BINDING,
        reducible_to_haiku=False,
        narrative="Research brief authoring inherits the lead-agent binding.",
    ),
)
"""The 4 brief-authoring inheritance rows per C-CP-13 §13.3 verbatim."""

_INHERITANCE_BY_CLASS: dict[WorkloadClass, BriefAuthoringInheritance] = {
    e.workload_class: e for e in BRIEF_AUTHORING_INHERITANCE
}


def inheritance_for(workload_class: WorkloadClass) -> BriefAuthoringInheritance:
    """Return the brief-authoring inheritance rule for a workload class.

    Total over `WorkloadClass`; deterministic."""
    return _INHERITANCE_BY_CLASS[workload_class]
