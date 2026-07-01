"""Deferral envelope — committed-at-D6 vs deferred-to-deployment-binding — U-OD-03.

Implements C-OD-03 §3.1 (committed-at-D6 surfaces), §3.2 (deferred-to-deployment-
binding surfaces), §3.3 (deferral boundary invariant).

`SurfaceCommitmentClass` enumerates the 2 commitment classes per §3.1-§3.2.
`COMMITTED_AT_D6_SURFACES` declares the 6 design-time-committed surfaces (the
plan acc #2 verbatim 6-entry set). `DEFERRED_SURFACES` aggregates the
"Deferred to implementation discretion" enumeration blocks across OD spec v1.2.

The deferral envelope is the design-time committed-vs-deferred boundary: every
surface is in exactly one class (§3.3 — acc #4). It composes with U-OD-01's
9-cell matrix + U-OD-02's per-cell backend class to form the full design-time
committed surface; downstream U-OD-28 / U-OD-30 / U-OD-31 / U-OD-32 / U-OD-34
compose against this committed envelope (acc #7).

Plan-vs-spec note. Plan acc #2 commits exactly 6 verbatim entries for
`COMMITTED_AT_D6_SURFACES` — a subset of the longer §3.1 committed-surface
table; the plan is execution authority on the signature cardinality and the
6-entry set is transcribed verbatim from the plan acc #2 list. Plan acc #3
cites "the 11 ... blocks"; OD spec v1.2 in fact carries exactly one
"Deferred to implementation discretion" block per contract §1-§23 (23 blocks).
Plan acc #5 requires every such block to have a corresponding `DEFERRED_SURFACES`
entry and the acc #3 signature comment says "11+ entries"; the strict-coverage
reading (all 23 blocks enumerated) satisfies acc #5 strictly and acc #3's "11+"
bound. `DEFERRED_SURFACES` therefore declares one entry per spec contract.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.1.3 U-OD-03
(v2.1-base; never revised — preserved verbatim through v2.8);
Spec_Operational_Discipline_v1_2.md §3 C-OD-03 §3.1 / §3.2 / §3.3 (preserved
verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.9 (cell-selection contract).

Depends on: [U-OD-01, U-OD-02] — composition anchors only (the deferral
envelope is a static declaration; it does not import `CellID` or `BackendClass`
at the type level, it references them as the committed surfaces it bounds).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "COMMITTED_AT_D6_SURFACES",
    "DEFERRED_SURFACES",
    "CommittedSurface",
    "DeferredSurface",
    "SurfaceCommitmentClass",
]


class SurfaceCommitmentClass(StrEnum):
    """The 2 surface-commitment classes (C-OD-03 §3.1-§3.2).

    Exactly 2 values per the §3.1 / §3.2 split: a surface is either committed
    at D6 or deferred to deployment-binding time (acc #1).
    """

    COMMITTED_AT_D6 = "COMMITTED_AT_D6"
    """The surface is fixed at design time (ADR-D6 v1.1) — §3.1."""

    DEFERRED_TO_DEPLOYMENT_BINDING_TIME = "DEFERRED_TO_DEPLOYMENT_BINDING_TIME"
    """The surface is bound at deployment-binding time x persona-tier-binding
    time, within the envelope a committed surface allows — §3.2."""


class CommittedSurface(BaseModel):
    """A surface committed at D6 (C-OD-03 §3.1).

    Frozen → `Eq` + `Hash`, stable under serialization. `contract_anchor`
    resolves to the OD spec contract section that fixes the surface (acc #7 —
    contract anchors resolve to OD spec sections).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the committed surface name (§3.1 table, verbatim).
    surface_name: str
    #: the OD spec contract section anchoring the commitment.
    contract_anchor: str


class DeferredSurface(BaseModel):
    """A surface deferred to deployment-binding time (C-OD-03 §3.2).

    Frozen → `Eq` + `Hash`. `closure_target` is one of two values per acc #6 —
    the surface closes either at deployment-binding time or at Phase 6
    implementation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the deferred surface name.
    surface_name: str
    #: the OD spec contract anchoring the deferral block.
    contract_anchor: str
    #: closure target — one of two values per acc #6.
    closure_target: str


#: The 6 surfaces committed at D6 (C-OD-03 §3.1 — plan acc #2 verbatim 6-entry
#: set). The plan acc #2 fixes exactly these 6: per-cell backend class,
#: sampling discipline, redaction class, trace storage tier, collector
#: placement, retention class.
COMMITTED_AT_D6_SURFACES: tuple[CommittedSurface, ...] = (
    CommittedSurface(
        surface_name="per-cell backend class",
        contract_anchor="C-OD-02",
    ),
    CommittedSurface(
        surface_name="sampling discipline",
        contract_anchor="C-OD-09 + C-OD-10",
    ),
    CommittedSurface(
        surface_name="redaction class",
        contract_anchor="C-OD-12 + C-OD-13",
    ),
    CommittedSurface(
        surface_name="trace storage tier",
        contract_anchor="C-OD-01 §1.3",
    ),
    CommittedSurface(
        surface_name="collector placement",
        contract_anchor="C-OD-19 + C-OD-20",
    ),
    CommittedSurface(
        surface_name="retention class",
        contract_anchor="C-OD-01 §1.3",
    ),
)


#: closure-target values (acc #6).
_DEPLOYMENT_BINDING_TIME = "deployment_binding_time"
_PHASE_6_IMPLEMENTATION = "phase_6_implementation"

#: Admissible `DeferredSurface.closure_target` values (acc #6 — exactly 2).
CLOSURE_TARGETS: frozenset[str] = frozenset({_DEPLOYMENT_BINDING_TIME, _PHASE_6_IMPLEMENTATION})


#: The deferred-surface inventory — one entry per OD spec v1.2 contract §1-§23,
#: each carrying a "Deferred to implementation discretion" block (C-OD-03 §3.2;
#: acc #3 + acc #5 strict-coverage reading). 23 entries.
DEFERRED_SURFACES: tuple[DeferredSurface, ...] = (
    DeferredSurface(
        surface_name="cell-identification API surface + cell-transition "
        "state-machine + cell-binding persistence mechanism",
        contract_anchor="C-OD-01 §1 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="provider selection mechanism + provider-API-binding + "
        "per-tier reachability validation + candidate-rotation cadence",
        contract_anchor="C-OD-02 §2 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="deployment-binding-time configuration format + "
        "operator-facing selection UX + selection-validation mechanism + "
        "candidate-deprecation handling",
        contract_anchor="C-OD-03 §3 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="OTel SDK binding per language ecosystem + span exporter "
        "wiring + instrumentation library version pinning + cross-SDK "
        "conformance test harness",
        contract_anchor="C-OD-04 §4 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="cross-SDK namespace conformance test harness + "
        "namespace-version-migration protocol + runtime namespace-presence "
        "validation mechanism",
        contract_anchor="C-OD-05 §5 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="span-event emission API + sibling-span parent-correlation "
        "mechanism + retry-span lifecycle + replay-trace-emission semantics",
        contract_anchor="C-OD-06 §6 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="OTel/OTLP span emission for breaker.tripped + "
        "attribute-validation mechanism + breaker-state-machine + "
        "C10-gate / C7-span subscription wiring",
        contract_anchor="C-OD-07 §7 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="runtime cross-namespace validation mechanism + "
        "attribute-namespace prefix enforcement + OTel-attribute-set "
        "version-pinning convention",
        contract_anchor="C-OD-08 §8 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="tail-based sampling decision algorithm + "
        "tail-keep-on-classification filter + always-sampled-event detection + "
        "cross-SDK sampling-decision conformance test",
        contract_anchor="C-OD-09 §9 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="tail-based sampling filter + base-rate numeric "
        "calibration + per-cell sampling-decision algorithm + cross-SDK "
        "base-rate conformance",
        contract_anchor="C-OD-10 §10 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="cardinality-budget numeric thresholds per cell + "
        "cardinality-blowup detection mechanism + metric-dimension "
        "static-schema validation + per-tenant rate-limit implementation",
        contract_anchor="C-OD-11 §11 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="OTLP-collector default-off filter + "
        "content-attribute encryption-in-flight + structure-attribute "
        "serialization format + hash-digest algorithm at attribute level",
        contract_anchor="C-OD-12 §12 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="eval-grade redaction pipeline implementation + "
        "redaction-token format + per-session content-capture toggle UX + "
        "audit-ledger-entry emission API + pre-collector redaction injection "
        "boundary",
        contract_anchor="C-OD-13 §13 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="cost-attribution-per-span emission mechanism + "
        "per-cell cost-rollup query + replay-dedup algorithm + "
        "BASE_INPUT/BASE_OUTPUT rate-table refresh cadence",
        contract_anchor="C-OD-14 §14 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="dashboard query implementation per backend + "
        "price-table refresh cadence + tokenizer-version migration handling + "
        "cross-family rollup query per backend",
        contract_anchor="C-OD-15 §15 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="dashboard authoring per backend + alerting backend "
        "integration + dashboard versioning protocol + per-class-cost-ceiling "
        "threshold values",
        contract_anchor="C-OD-16 §16 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="child-span emission API + holdout-set construction "
        "protocol + Husain manual-review loop tooling + per-cell dashboard "
        "query authoring + alignment-floor threshold values",
        contract_anchor="C-OD-17 §17 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="drift-detection algorithm per primitive + "
        "re-baselining cycle workflow + dashboard alerting integration + "
        "eval-kind enforcement at SDK boundary",
        contract_anchor="C-OD-18 §18 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="otelcol-contrib configuration manifest + sqlite schema "
        "for the ring-buffer + TUI trace browser + ring-buffer rotation "
        "mechanism + cross-platform packaging",
        contract_anchor="C-OD-19 §19 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="collector configuration manifest per cell + "
        "K8s DaemonSet manifest + vendor-pipeline SDK binding + "
        "cross-cell collector configuration migration + BatchSpanProcessor "
        "timeout / batch-size tuning",
        contract_anchor="C-OD-20 §20 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="per-tenant routing mechanism at the OTLP collector + "
        "cryptographic key custody mechanism + tenant-isolation primitive "
        "selection + cross-tenant aggregation prohibition enforcement",
        contract_anchor="C-OD-21 §21 'Deferred to implementation discretion'",
        closure_target=_DEPLOYMENT_BINDING_TIME,
    ),
    DeferredSurface(
        surface_name="transition-planning UX + cross-cell observability-config "
        "migration mechanism + transition-validation enforcement + "
        "bridging-arc-binding state machine",
        contract_anchor="C-OD-22 §22 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
    DeferredSurface(
        surface_name="cross-spec citation strings + seam-versioning convention "
        "+ Phase 6+ implementation-planning surface",
        contract_anchor="C-OD-23 §23 'Deferred to implementation discretion'",
        closure_target=_PHASE_6_IMPLEMENTATION,
    ),
)


# Boundary-invariant sanity pin (§3.3 — acc #4 / acc #1): committed and
# deferred surface-name sets are disjoint — no surface is in both classes.
assert not (
    {s.surface_name for s in COMMITTED_AT_D6_SURFACES} & {s.surface_name for s in DEFERRED_SURFACES}
), "deferral envelope §3.3 boundary invariant — committed and deferred surfaces must be disjoint"

# Cardinality pins per acc #2 / acc #3.
assert len(COMMITTED_AT_D6_SURFACES) == 6, "C-OD-03 §3.1 — exactly 6 committed surfaces"
assert len(DEFERRED_SURFACES) >= 11, "C-OD-03 §3.2 — at least 11 deferred surfaces"
