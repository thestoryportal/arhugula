"""CP-source namespace set verification (6 rows) — U-OD-07.

Implements C-OD-05 §5.1 (CP-source rows of the 15-row namespace ingestion map).

The 15-row C-OD-05 §5.1 namespace map ingests 6 namespaces whose authoritative
declaration site is the Control Plane (CP) axis: `hitl.` / `topology.fanout.` /
`subagent.` / `engine.` / `audit.` / `validator.fail.` (§5.1 rows 6-11). This
unit declares the CP-source prefix set and the two verification gates that
enforce Pattern P1 mechanical-alignment discipline against the CP plan U-CP-54
substrate seam exports manifest (C-CP-24 §24.1.A + §24.1.B).

`routing.*` is deliberately NOT in the CP-source set (acceptance #3): per
C-OD-05 §5.1 it is a CP-axis-only namespace declared at C-CP-01 §1.4 and
inherited from the parent LLM inference span under sampling composition — it
is not directly ingested at the OD axis.

Per the §5.2 source-as-authoritative-declarer invariant, OD does NOT re-declare
CP attribute names — the CP contract (C-CP-09 / C-CP-14 / C-CP-20 / C-CP-21) is
canonical. This unit verifies the OD-side ingested prefix set against the
CP-side declaration; any drift is a Pattern P1 violation.

Prefix-form note. The OD plan v2.1 §3.2.4 U-OD-07 signature block transcribes
the topology prefix as `"topology."`; the cited C-OD-05 §5.1 row 7 (the
authoritative source — spec is canonical over the plan per the authority chain)
declares `topology.fanout.*`, and the landed U-OD-05 namespace map
(`namespace_map.py`) ingests `topology.fanout.`. Acceptance criterion #1
requires byte-exact match against the §5.1 row set, so the prefix is
`topology.fanout.` here — a determinate conformance to the cited spec section,
not a design choice.

Cross-axis posture: the CP-source authority resolves at the CP plan U-CP-54
terminal exporter manifest (C-CP-24 §24.1.A + §24.1.B). Per Phase 7 7b
discipline, this OD unit is built against the cited CP contract sections; the
cross-axis edge resolves at 7c composition. CP substrate is consumed READ-ONLY.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.2.4 U-OD-07
(preserved verbatim through v2.5 §0.3 + v2.6 §3 — no delta; v2.6 §3 pointer
table line 152); Spec_Operational_Discipline_v1_2.md §5 C-OD-05 §5.1
(preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.2
specialization-layer namespace map.
"""

from __future__ import annotations

__all__ = [
    "CP_SOURCE_NAMESPACE_PREFIXES",
    "AttributeCountMismatch",
    "NamespaceSetMismatch",
    "assert_namespace_attribute_count",
    "verify_cp_source_namespace_set",
]


#: The 6 CP-source namespace prefixes of the C-OD-05 §5.1 map (rows 6-11).
#: Byte-exact against the CP plan U-CP-54 substrate seam exports manifest per
#: Pattern P1 mechanical-alignment discipline (acceptance #1, #4).
CP_SOURCE_NAMESPACE_PREFIXES: frozenset[str] = frozenset(
    {
        "hitl.",
        "topology.fanout.",
        "subagent.",
        "engine.",
        "audit.",
        "validator.fail.",
    }
)


class NamespaceSetMismatch(Exception):  # noqa: N818 — U-OD-07 plan signature verbatim (no spec extension)
    """Raised when a declared CP-source prefix set differs from the canonical
    `CP_SOURCE_NAMESPACE_PREFIXES` — the `Err` arm of
    `verify_cp_source_namespace_set`.

    Inline-materialized per the M-1 error-type discipline; stack is Pydantic v2
    + stdlib, no `Result` framework pull (CLAUDE.md §3.2 / I-6).
    """


class AttributeCountMismatch(Exception):  # noqa: N818 — U-OD-07 plan signature verbatim (no spec extension)
    """Raised when an observed per-prefix attribute count differs from the CP
    plan U-CP-54 manifest declaration — the `Err` arm of
    `assert_namespace_attribute_count`.

    Inline-materialized per the M-1 error-type discipline.
    """


def verify_cp_source_namespace_set(declared: frozenset[str]) -> None:
    """Verify a declared CP-source prefix set against the canonical set.

    Returns `None` (the `Ok(())` arm) when `declared` equals
    `CP_SOURCE_NAMESPACE_PREFIXES`; raises `NamespaceSetMismatch` (the `Err`
    arm) on any drift. This is the OD-side Pattern P1 mechanical-alignment gate
    against the CP plan U-CP-54 manifest (acceptance #4).
    """
    if declared != CP_SOURCE_NAMESPACE_PREFIXES:
        missing = CP_SOURCE_NAMESPACE_PREFIXES - declared
        extra = declared - CP_SOURCE_NAMESPACE_PREFIXES
        raise NamespaceSetMismatch(
            f"CP-source namespace set mismatch against C-OD-05 §5.1: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    return None


def assert_namespace_attribute_count(prefix: str, expected_count: int, observed_count: int) -> None:
    """Assert an observed per-prefix attribute count matches the expectation.

    Returns `None` (the `Ok(())` arm) when `observed_count == expected_count`;
    raises `AttributeCountMismatch` (the `Err` arm) when the observed count for
    `prefix` differs from the CP plan U-CP-54 manifest declaration (acceptance
    #2). `prefix` must be a CP-source prefix; a non-CP-source prefix — notably
    `routing.` (acceptance #3, excluded from the CP-source set) — is itself a
    mismatch.
    """
    if prefix not in CP_SOURCE_NAMESPACE_PREFIXES:
        raise AttributeCountMismatch(
            f"attribute-count assertion: '{prefix}' is not a CP-source "
            f"namespace prefix in the C-OD-05 §5.1 map"
        )
    if observed_count != expected_count:
        raise AttributeCountMismatch(
            f"attribute-count mismatch for '{prefix}': observed "
            f"{observed_count}, CP plan U-CP-54 manifest declares "
            f"{expected_count}"
        )
    return None
