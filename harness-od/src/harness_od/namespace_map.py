"""15-row specialization-layer namespace ingestion map — U-OD-05.

Implements C-OD-05 §5.1 (namespace map structure — 15 rows) and §5.2
(ingestion-posture invariants).

The namespace map is the OD-axis specialization-layer ingestion contract: each
of the 15 rows commits a namespace prefix, its authoritative declaring axis, its
attribute count, and the source spec contract section. Per §5.1 verbatim the
15 rows break down by source axis as: 1 OD-canonical (`provider_discriminator`),
7 AS-source (`anthropic.` / `mcp.` / `skill.` / `managed_agents.` / `sandbox.` /
`files.` / `memory.`), 6 CP-source (`hitl.` / `topology.fanout.` / `subagent.` /
`engine.` / `audit.` / `validator.fail.`), 1 substrate-anchored-outside-CP
(`harness.breaker.`).

Per §5.2: each namespace has exactly one authoritative declarer (the
source-as-authoritative-declarer invariant); D6 ingestion at the OD axis does
NOT re-declare attribute names — the source AS/CP/OD contract is canonical.
`assert_source_authoritative_declarer` is the Pattern P1 mechanical-alignment
anchor — it structurally rejects a namespace claimed by an axis other than its
declared `source_axis`.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.2.2 U-OD-05
(preserved verbatim through v2.5 §0.3 + v2.6 §3 — no delta);
Spec_Operational_Discipline_v1_2.md §5 C-OD-05 §5.1 / §5.2 (preserved verbatim
into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.2 specialization-layer namespace map.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "NAMESPACE_MAP",
    "AuthorityViolation",
    "NamespaceMapRow",
    "NamespaceSourceAxis",
    "assert_source_authoritative_declarer",
    "lookup_namespace",
]


class NamespaceSourceAxis(StrEnum):
    """The 4 source-axis classes of a specialization-layer namespace.

    Per C-OD-05 §5.1 + §5.2: `OD_CANONICAL` — declared at the OD axis;
    `AS_SOURCE` / `CP_SOURCE` — declared at the AS / CP axis and ingested at OD;
    `SUBSTRATE_ANCHORED_OUTSIDE_CP` — declared at OD per F-CP-01 Stage 3b
    alignment (the `harness.breaker.*` synthesis-D-ADR namespace).
    """

    OD_CANONICAL = "OD_CANONICAL"
    AS_SOURCE = "AS_SOURCE"
    CP_SOURCE = "CP_SOURCE"
    SUBSTRATE_ANCHORED_OUTSIDE_CP = "SUBSTRATE_ANCHORED_OUTSIDE_CP"


class NamespaceMapRow(BaseModel):
    """One row of the 15-row namespace ingestion map (C-OD-05 §5.1).

    Frozen → `Eq` + `Hash`, stable under serialization. Commits the namespace
    prefix, its attribute count, its authoritative source axis, and the source
    spec contract section.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace_prefix: str
    """e.g. `"anthropic."`, `"mcp."`, `"harness.breaker."`."""

    attribute_count: int
    """Attributes ingested within the namespace per the source contract."""

    source_axis: NamespaceSourceAxis
    source_contract_ref: str
    """e.g. `"C-AS-14 §14.2"`, `"C-CP-24 §24.1.A"`, `"C-OD-07 §7.1"`."""

    def __hash__(self) -> int:
        return hash(
            (
                self.namespace_prefix,
                self.attribute_count,
                self.source_axis,
                self.source_contract_ref,
            )
        )


class AuthorityViolation(Exception):  # noqa: N818 — U-OD-05 plan signature verbatim (no spec extension)
    """Raised when a namespace is claimed by an axis other than its declared
    `source_axis` — the `Err` arm of `assert_source_authoritative_declarer`.

    The Python materialization of the `Result<(), AuthorityViolation>` error arm
    in the U-OD-05 plan signature; stack is Pydantic v2 + stdlib, no `Result`
    framework pull (CLAUDE.md §3.2 / I-6).
    """


#: The 15-row specialization-layer namespace ingestion map (C-OD-05 §5.1
#: verbatim). Row order, attribute counts, source axes, and contract sections
#: transcribed from the §5.1 table. Per-source-axis breakdown: 1 OD_CANONICAL +
#: 7 AS_SOURCE + 6 CP_SOURCE + 1 SUBSTRATE_ANCHORED_OUTSIDE_CP = 15
#: (acceptance #3).
NAMESPACE_MAP: tuple[NamespaceMapRow, ...] = (
    # --- 7 AS-source rows (§5.1 rows 1-5, 12, 13) --------------------------
    NamespaceMapRow(
        namespace_prefix="anthropic.",
        attribute_count=10,
        source_axis=NamespaceSourceAxis.AS_SOURCE,
        source_contract_ref="C-AS-14 §14.2",
    ),
    NamespaceMapRow(
        namespace_prefix="mcp.",
        attribute_count=7,
        source_axis=NamespaceSourceAxis.AS_SOURCE,
        source_contract_ref="C-AS-14 §14.3",
    ),
    NamespaceMapRow(
        namespace_prefix="skill.",
        attribute_count=6,
        source_axis=NamespaceSourceAxis.AS_SOURCE,
        source_contract_ref="C-AS-14 §14.4",
    ),
    NamespaceMapRow(
        namespace_prefix="managed_agents.",
        attribute_count=3,
        source_axis=NamespaceSourceAxis.AS_SOURCE,
        source_contract_ref="C-AS-14 §14.5",
    ),
    NamespaceMapRow(
        namespace_prefix="sandbox.",
        attribute_count=7,
        source_axis=NamespaceSourceAxis.AS_SOURCE,
        source_contract_ref="C-AS-15 §15.2",
    ),
    NamespaceMapRow(
        namespace_prefix="files.",
        attribute_count=8,
        source_axis=NamespaceSourceAxis.AS_SOURCE,
        source_contract_ref="C-AS-14 §14.6",
    ),
    NamespaceMapRow(
        namespace_prefix="memory.",
        attribute_count=6,
        source_axis=NamespaceSourceAxis.AS_SOURCE,
        source_contract_ref="C-AS-14 §14.7",
    ),
    # --- 6 CP-source rows (§5.1 rows 6-11) ---------------------------------
    NamespaceMapRow(
        namespace_prefix="hitl.",
        attribute_count=11,
        source_axis=NamespaceSourceAxis.CP_SOURCE,
        source_contract_ref="C-CP-20 §20.6",
    ),
    NamespaceMapRow(
        # OD spec §5.1 row 7 gives no integer ("included in C-CP-14 §14.2
        # topology.* attribute set"); the count is sourced from the CP spec
        # C-CP-24 export table (Spec_Control_Plane_v1_2.md line 2146 —
        # `topology.*` C-CP-14 §14.2 = 10 attributes).
        namespace_prefix="topology.fanout.",
        attribute_count=10,
        source_axis=NamespaceSourceAxis.CP_SOURCE,
        source_contract_ref="C-CP-14 §14.2",
    ),
    NamespaceMapRow(
        namespace_prefix="subagent.",
        attribute_count=7,
        source_axis=NamespaceSourceAxis.CP_SOURCE,
        source_contract_ref="C-CP-14 §14.2",
    ),
    NamespaceMapRow(
        namespace_prefix="engine.",
        attribute_count=3,
        source_axis=NamespaceSourceAxis.CP_SOURCE,
        source_contract_ref="C-CP-09 §9.1",
    ),
    NamespaceMapRow(
        namespace_prefix="audit.",
        attribute_count=7,
        source_axis=NamespaceSourceAxis.CP_SOURCE,
        source_contract_ref="C-CP-20 §20.4",
    ),
    NamespaceMapRow(
        namespace_prefix="validator.fail.",
        attribute_count=3,
        source_axis=NamespaceSourceAxis.CP_SOURCE,
        source_contract_ref="C-CP-21 §21.5",
    ),
    # --- 1 substrate-anchored-outside-CP row (§5.1 row 14) -----------------
    NamespaceMapRow(
        namespace_prefix="harness.breaker.",
        attribute_count=7,
        source_axis=NamespaceSourceAxis.SUBSTRATE_ANCHORED_OUTSIDE_CP,
        source_contract_ref="C-OD-07 §7.1",
    ),
    # --- 1 OD-canonical row (§5.1 row 15) ----------------------------------
    NamespaceMapRow(
        namespace_prefix="provider_discriminator",
        attribute_count=1,
        source_axis=NamespaceSourceAxis.OD_CANONICAL,
        source_contract_ref="F1 v1.2 composition context",
    ),
)


def lookup_namespace(prefix: str) -> NamespaceMapRow | None:
    """Return the `NamespaceMapRow` for `prefix`, or `None` if not in the map.

    Materializes the `Option<NamespaceMapRow>` return — `Some` for any of the
    15 declared prefixes, `None` otherwise (acceptance #5).
    """
    for row in NAMESPACE_MAP:
        if row.namespace_prefix == prefix:
            return row
    return None


def assert_source_authoritative_declarer(prefix: str, source: NamespaceSourceAxis) -> None:
    """Assert that `source` is the authoritative declarer of namespace `prefix`.

    The Pattern P1 mechanical-alignment anchor (acceptance #6, §5.2): returns
    `None` (the `Ok(())` arm) when `source` matches the namespace's declared
    `source_axis`; raises `AuthorityViolation` (the `Err` arm) when a namespace
    is claimed by an axis that does not match its declared `source_axis`, or
    when `prefix` is not a declared namespace at all.

    Each namespace has exactly one authoritative declarer per §5.2; this is the
    structural rejection of attribute-name / authority drift at the namespace
    boundary.
    """
    row = lookup_namespace(prefix)
    if row is None:
        raise AuthorityViolation(
            f"namespace authority violation: '{prefix}' is not a declared "
            f"namespace in the 15-row C-OD-05 §5.1 map"
        )
    if row.source_axis is not source:
        raise AuthorityViolation(
            f"namespace authority violation: '{prefix}' is claimed by "
            f"{source} but its authoritative declarer per C-OD-05 §5.1 is "
            f"{row.source_axis}"
        )
    return None
