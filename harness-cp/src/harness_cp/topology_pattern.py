"""Six-pattern multi-agent topology taxonomy + admissibility predicate — U-CP-22.

Implements C-CP-10 §10.1 (six-pattern topology taxonomy), §10.2 (the
`cascade_policy` field domain on the workflow-definition surface), §10.3
(cross-pattern admissibility per workload class).

Declares the closed 6-value `TopologyPattern` enum, the `CascadePolicy` enum
(the §10.2 `cascade_policy` string-literal field domain materialized as a named
enum), and `is_admissible` — the §10.3 cross-pattern admissibility predicate.

The taxonomy is **closed** at cardinality 6 per C-CP-10 §10.1 / ADR-D4 §1.1;
extension is a Workflow §4.1.2 Class-2 D4 revision.

`is_admissible` answers the **§10.3 cross-pattern** question only: "is `pattern`
admissible as a *non-primary* option at `workload`?" The §10.3 contract is
titled "Cross-pattern admissibility per workload class" and annotates exactly
the non-primary cells that the workflow-definition surface must nonetheless
accept. The *primary* topology pattern per workload class is a separate
commitment at C-CP-11 §11.1, owned by U-CP-23 — U-CP-22 cannot compose it
(U-CP-23 depends on U-CP-22; the reverse edge would cycle). Total admissibility
is therefore the composition of this §10.3 predicate with U-CP-23's §11.1
primary-pattern table; this module owns the §10.3 half.

Authority: Implementation_Plan_Control_Plane_v2_5.md §2.4 U-CP-22 (v2.4 §10.1/
§10.2 conformance — Tension 002 / §4A verbatim-divergence cluster resolution;
v2.5 `Depends on: [U-CP-00]` amendment — Tension 003 resolution);
Spec_Control_Plane_v1_2.md §10 C-CP-10 §10.1/§10.2/§10.3 (preserved verbatim
into v1.3); ADR-D4 v1.1 §1.1 + §1.2.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core.workload_class import WorkloadClass


class TopologyPattern(StrEnum):
    """The 6 multi-agent topology patterns (C-CP-10 §10.1, verbatim).

    Member string values are the §10.1 six-pattern taxonomy "Pattern" column
    verbatim. The SCREAMING_SNAKE_CASE member names are a Python-stack naming
    convention; the string values match §10.1 byte-exact. Closed at
    cardinality 6 (acceptance #4).
    """

    SINGLE_THREADED_LINEAR = "single-threaded-linear"
    ORCHESTRATOR_WORKERS = "orchestrator-workers"
    DECENTRALIZED_HANDOFF = "decentralized-handoff"
    HIERARCHICAL_DELEGATION = "hierarchical-delegation"
    EVALUATOR_OPTIMIZER = "evaluator-optimizer"
    PARALLELIZATION = "parallelization"


class CascadePolicy(StrEnum):
    """The `cascade_policy` field domain (C-CP-10 §10.2, verbatim).

    §10.2 declares `cascade_policy` as a string-literal field domain on
    `TopologyDeclaration` — `"pause" | "proceed" | "cascade-cancel"` — not a
    named enum. This is the permitted plan-side materialization of that domain
    as a named enum; the member string values are the §10.2 domain literals
    verbatim.
    """

    PAUSE = "pause"
    PROCEED = "proceed"
    CASCADE_CANCEL = "cascade-cancel"


#: The C-CP-10 §10.3 cross-pattern admissibility set — the (pattern, workload)
#: cells §10.3 annotates as admissible for a *non-primary* pattern. Transcribed
#: verbatim from the §10.3 annotation block (per ADR-D4 v1.1 §1.2).
_CROSS_PATTERN_ADMISSIBLE: frozenset[tuple[TopologyPattern, WorkloadClass]] = frozenset(
    {
        # hierarchical-delegation — software-engineering and research
        # (scope-bounded recursion; fan-out cap 3 per parent).
        (TopologyPattern.HIERARCHICAL_DELEGATION, WorkloadClass.SOFTWARE_ENGINEERING),
        (TopologyPattern.HIERARCHICAL_DELEGATION, WorkloadClass.RESEARCH),
        # decentralized-handoff — pipeline-automation per-stage-expert workflows
        # (cascade-policy `cascade-cancel`; single-owner-at-a-time).
        (TopologyPattern.DECENTRALIZED_HANDOFF, WorkloadClass.PIPELINE_AUTOMATION),
        # parallelization — research breadth-search and content-creation
        # A/B-variant generation (cap 3-5; voting aggregator at synthesis).
        (TopologyPattern.PARALLELIZATION, WorkloadClass.RESEARCH),
        (TopologyPattern.PARALLELIZATION, WorkloadClass.CONTENT_CREATION),
    }
)


def is_admissible(pattern: TopologyPattern, workload: WorkloadClass) -> bool:
    """Return whether `pattern` is §10.3 cross-pattern admissible at `workload`.

    Answers the C-CP-10 §10.3 question: is `pattern` an admissible *non-primary*
    topology option for `workload`? `True` for the §10.3-annotated cells —
    `hierarchical-delegation` at software-engineering / research,
    `decentralized-handoff` at pipeline-automation, `parallelization` at
    research / content-creation — and `False` otherwise.

    This is **not** total admissibility: a workload's *primary* pattern is
    committed separately at C-CP-11 §11.1 (U-CP-23). A `False` result means
    "not annotated as cross-pattern admissible at §10.3", not "inadmissible
    outright" — the primary-pattern cell composes from §11.1.
    """
    return (pattern, workload) in _CROSS_PATTERN_ADMISSIBLE
