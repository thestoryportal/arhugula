"""Routing-layer enumeration — declared at U-CP-06 (carrier-home).

Declares the closed 3-value `RoutingLayer` enum — the layered-routing-strategy
layer discriminator.

**Carrier-home note.** The CP plan assigns `RoutingLayer`'s declaration site to
U-CP-05 ("logical: routing-layer-enum" in U-CP-05 Files affected). But U-CP-05
`Depends on: [U-CP-01, U-CP-02, U-CP-04, U-CP-06]` and U-CP-06's `LayerBudget`
signature consumes `RoutingLayer` by type — a mutual U-CP-05 ↔ U-CP-06
dependency cycle. U-CP-06 lands at topological Level 1, before U-CP-05. Per the
carrier-home defect pattern, the shared enum is declared here at the
first-consumer-lands site (U-CP-06) and U-CP-05 imports it from here at its
later landing. `RoutingLayer` is **fully spec-specified** — C-CP-02 §2.3 names
the value domain `routing.layer ∈ {manifest, embedding, llm_as_router}` and
C-CP-03 §3.1 names `LayerBudget.layer : "manifest" | "embedding" |
"llm_as_router"` — so this is a determinate factor-out, not a design extension.

Member string values are the C-CP-02 §2.3 / C-CP-03 §3.1 value domain verbatim
(`manifest` | `embedding` | `llm_as_router`). The SCREAMING_SNAKE_CASE member
names are the U-CP-05 plan-body names (`DECLARATIVE` | `EMBEDDING` |
`LLM_AS_ROUTER`) — a Python-stack naming convention; the string values match
the spec byte-exact.

The layer set is **closed** at cardinality 3 per C-CP-02 §2.1 — reordering or
extension is a Workflow §4.1.2 Class-2 F1 revision.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-06 (carrier-home
relocation from U-CP-05 per the carrier-home defect pattern — mutual
U-CP-05 ↔ U-CP-06 cycle); Spec_Control_Plane_v1_2.md §2 C-CP-02 §2.1 + §2.3 +
§3 C-CP-03 §3.1 (preserved verbatim into v1.3); ADR-F1 v1.2 §Decision.
"""

from __future__ import annotations

from enum import StrEnum


class RoutingLayer(StrEnum):
    """The 3 routing-strategy layers (C-CP-02 §2.1, value domain §2.3).

    Layer ordering is fixed: `DECLARATIVE` → `EMBEDDING` → `LLM_AS_ROUTER` per
    the §2.2 cheapest-deterministic-first invariant. Closed at cardinality 3.
    """

    DECLARATIVE = "manifest"
    """Manifest-driven role × workload binding; zero inference cost."""

    EMBEDDING = "embedding"
    """Semantic similarity to canonical role descriptors; one embedding call."""

    LLM_AS_ROUTER = "llm_as_router"
    """Fallback layer; LLM classification — one full LLM call, last-resort."""
