"""AS-source namespace set verification (7 rows) — U-OD-06.

Implements C-OD-05 §5.1 (AS-source rows of the 15-row namespace ingestion map).

The 15-row C-OD-05 §5.1 namespace map ingests 7 namespaces whose authoritative
declaration site is the Action Surface (AS) axis: `anthropic.` / `mcp.` /
`skill.` / `managed_agents.` / `sandbox.` / `files.` / `memory.` (§5.1 rows
1-5, 12, 13). This unit declares the AS-source prefix set and the two
verification gates that enforce Pattern P1 mechanical-alignment discipline
against the AS plan U-AS-33 substrate seam exports manifest: a set-equality
gate (`verify_as_source_namespace_set`) and a per-prefix attribute-count gate
(`assert_namespace_attribute_count`).

Per the §5.2 source-as-authoritative-declarer invariant, OD does NOT re-declare
AS attribute names — the AS contract (C-AS-14 / C-AS-15) is canonical. This unit
verifies the OD-side ingested prefix set against the AS-side declaration; any
drift is a Pattern P1 violation (§5.1 acceptance #5).

Cross-axis posture: the AS-source authority resolves at the AS plan U-AS-33
terminal exporter manifest (C-AS-16 §16.1 + §16.4). Per the Phase 7 7b
discipline, this OD unit is built against the cited AS contract sections;
the cross-axis edge resolves at 7c composition. AS substrate is consumed
READ-ONLY.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.2.3 U-OD-06
(preserved verbatim through v2.5 §0.3 + v2.6 §3 — no delta; v2.6 §3 pointer
table line 151); Spec_Operational_Discipline_v1_2.md §5 C-OD-05 §5.1
(preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.2
specialization-layer namespace map.
"""

from __future__ import annotations

__all__ = [
    "AS_SOURCE_NAMESPACE_PREFIXES",
    "AttributeCountMismatch",
    "NamespaceSetMismatch",
    "assert_namespace_attribute_count",
    "verify_as_source_namespace_set",
]


#: The 7 AS-source namespace prefixes of the C-OD-05 §5.1 map (rows 1-5, 12,
#: 13). Byte-exact against the AS plan U-AS-33 substrate seam exports manifest
#: per Pattern P1 mechanical-alignment discipline (acceptance #1, #5).
AS_SOURCE_NAMESPACE_PREFIXES: frozenset[str] = frozenset(
    {
        "anthropic.",
        "mcp.",
        "skill.",
        "managed_agents.",
        "sandbox.",
        "files.",
        "memory.",
    }
)


class NamespaceSetMismatch(Exception):  # noqa: N818 — U-OD-06 plan signature verbatim (no spec extension)
    """Raised when a declared AS-source prefix set differs from the canonical
    `AS_SOURCE_NAMESPACE_PREFIXES` — the `Err` arm of
    `verify_as_source_namespace_set`.

    The Python materialization of the `Result<(), NamespaceSetMismatch>` error
    arm in the U-OD-06 plan signature; inline-materialized per the M-1
    error-type discipline (no shape to get wrong). Stack is Pydantic v2 +
    stdlib, no `Result` framework pull (CLAUDE.md §3.2 / I-6).
    """


class AttributeCountMismatch(Exception):  # noqa: N818 — U-OD-06 plan signature verbatim (no spec extension)
    """Raised when an observed per-prefix attribute count differs from the AS
    plan U-AS-33 manifest declaration — the `Err` arm of
    `assert_namespace_attribute_count`.

    Inline-materialized per the M-1 error-type discipline.
    """


def verify_as_source_namespace_set(declared: frozenset[str]) -> None:
    """Verify a declared AS-source prefix set against the canonical set.

    Returns `None` (the `Ok(())` arm) when `declared` equals
    `AS_SOURCE_NAMESPACE_PREFIXES`; raises `NamespaceSetMismatch` (the `Err`
    arm) on any drift — a missing prefix, an extra prefix, or a renamed prefix
    (acceptance #3). This is the OD-side Pattern P1 mechanical-alignment gate
    against the AS plan U-AS-33 manifest (acceptance #5).
    """
    if declared != AS_SOURCE_NAMESPACE_PREFIXES:
        missing = AS_SOURCE_NAMESPACE_PREFIXES - declared
        extra = declared - AS_SOURCE_NAMESPACE_PREFIXES
        raise NamespaceSetMismatch(
            f"AS-source namespace set mismatch against C-OD-05 §5.1: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    return None


def assert_namespace_attribute_count(prefix: str, expected_count: int, observed_count: int) -> None:
    """Assert an observed per-prefix attribute count matches the expectation.

    Returns `None` (the `Ok(())` arm) when `observed_count == expected_count`;
    raises `AttributeCountMismatch` (the `Err` arm) when the observed count for
    `prefix` differs from the AS plan U-AS-33 manifest declaration (acceptance
    #2, #4). `prefix` must be an AS-source prefix; a non-AS-source prefix is
    itself a mismatch (the count cannot be verified against the AS manifest).
    """
    if prefix not in AS_SOURCE_NAMESPACE_PREFIXES:
        raise AttributeCountMismatch(
            f"attribute-count assertion: '{prefix}' is not an AS-source "
            f"namespace prefix in the C-OD-05 §5.1 map"
        )
    if observed_count != expected_count:
        raise AttributeCountMismatch(
            f"attribute-count mismatch for '{prefix}': observed "
            f"{observed_count}, AS plan U-AS-33 manifest declares "
            f"{expected_count}"
        )
    return None
