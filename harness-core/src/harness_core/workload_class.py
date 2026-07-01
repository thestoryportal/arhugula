"""Workload-class taxonomy — U-CP-00.

Implements C-CP-07 §7.3 (workload-binding-time selection contract — the
workload-class taxonomy half). Declares the closed 4-value `WorkloadClass`
enum.

`WorkloadClass` is a **cross-axis shared type** — referenced across the IS / AS
/ CP / OD axis plans (ADR-D4 + Persona §3.1) — and therefore resides in
`harness-core` rather than in any single axis package, per the operator
resolution of Phase 7 Class-1 Tension 003.

The taxonomy is **closed** at cardinality 4. C-CP-07 §7.3 also names an
`extension-class` binding-time option ("extension-class per Persona §3.2") —
the open-extension escape hatch for a workload class beyond the 4 canonical.
That is deliberately NOT an enum member: a closed enum cannot carry an open
"extension" member. Persona §3.2's extension mechanism is out of scope for this
foundational enum (no spec extension — C-CP-07 §7.3 commits only the 4 values
as the closed taxonomy).

Authority: Implementation_Plan_Control_Plane_v2_5.md §2.0 U-CP-00 (new
foundational unit per Tension 003 resolution); Spec_Control_Plane_v1_2.md §7
C-CP-07 §7.3 (preserved verbatim into v1.3); ADR-D4 v1.1; Persona §3.1.
"""

from __future__ import annotations

from enum import StrEnum


class WorkloadClass(StrEnum):
    """The 4 canonical workload classes (C-CP-07 §7.3, verbatim).

    Member string values are the §7.3 workload-class taxonomy verbatim
    (`software-engineering | content-creation | pipeline-automation |
    research`). The SCREAMING_SNAKE_CASE member names are a Python-stack naming
    convention; the string values match §7.3 byte-exact.

    Closed at cardinality 4 — extension is a Workflow §4.1.2 Class-2 D4
    revision. `extension-class` (§7.3) is the binding-time open-extension
    option, not a member of this closed set.
    """

    SOFTWARE_ENGINEERING = "software-engineering"
    CONTENT_CREATION = "content-creation"
    PIPELINE_AUTOMATION = "pipeline-automation"
    RESEARCH = "research"
