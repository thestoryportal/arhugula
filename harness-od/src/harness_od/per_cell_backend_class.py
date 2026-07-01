"""Per-cell backend class + candidate witness columns — U-OD-02.

Implements C-OD-02 §2.1 (per-cell backend class), §2.2 (per-cell provider
candidate witness columns), §2.3 (cell-class commitment invariant).

`BackendClass` enumerates the seven distinct observability-backend
architectural classes per §2.1. `PerCellBackendBinding` carries, for one ACTIVE
matrix cell, the cell's `backend_class` (a non-empty `frozenset[BackendClass]`)
and its candidate witness column. `PER_CELL_BACKEND_BINDINGS` declares one
binding per ACTIVE cell. `select_backend_class` returns the set;
`enumerate_candidates` returns the candidate witness column.

v2.8 (D-1): `backend_class` and the `select_backend_class` return widen from a
single `BackendClass` to a non-empty `frozenset[BackendClass]`. OD spec §2.1
commits a 2-value backend-class disjunction at cell-4 (`OTEL_ONLY` OR
`DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE`) and cell-5
(`DEDICATED_LLM_OBS_PLATFORM_MULTI_NODE` OR `OTEL_TO_VENDOR`); a single-valued
field cannot represent it. Cardinality of the set is 1 for the six committed
cells (1/2/3/6/7/8) and 2 for the two design-time-flexible cells (4/5).

Authority: Implementation_Plan_Operational_Discipline_v2_8.md §3.1.2 U-OD-02
(v2.8 D-1 revision — `backend_class` + `select_backend_class` return widened to
`Set<BackendClass>`; all other surfaces preserved verbatim from v2.5);
Spec_Operational_Discipline_v1_2.md §2 C-OD-02 §2.1 + §2.2 + §2.3 (preserved
verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.1 (per-cell backend class +
provider candidates witness columns).

Depends on: [U-OD-01] — `CellID` / `EXCLUDED_CELL` / `reject_excluded_cell`
from `observability_matrix`.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.observability_matrix import (
    ACTIVE_CELLS,
    CellID,
    reject_excluded_cell,
)

__all__ = [
    "PER_CELL_BACKEND_BINDINGS",
    "BackendClass",
    "CandidateWitness",
    "PerCellBackendBinding",
    "enumerate_candidates",
    "select_backend_class",
]


class BackendClass(StrEnum):
    """The 7 observability-backend architectural classes (C-OD-02 §2.1).

    Exactly 7 distinct values per §2.1. Eight cells map onto these seven
    classes — cell-4 and cell-5 each admit a 2-value class disjunction at the
    design-time-flexible §2.1 rows.
    """

    OTEL_ONLY = "OTEL_ONLY"
    DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE = "DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE"
    DEDICATED_LLM_OBS_PLATFORM_MULTI_NODE = "DEDICATED_LLM_OBS_PLATFORM_MULTI_NODE"
    CLOUD_NATIVE_LLM_OBS_PLATFORM = "CLOUD_NATIVE_LLM_OBS_PLATFORM"
    OTEL_TO_VENDOR = "OTEL_TO_VENDOR"
    SELF_HOSTED_MULTI_TENANT_LLM_OBS_PLATFORM = "SELF_HOSTED_MULTI_TENANT_LLM_OBS_PLATFORM"
    VENDOR_MANAGED_MULTI_TENANT_LLM_OBS_OR_CLOUD_NATIVE_MANAGED_AGENT_RUNTIME = (
        "VENDOR_MANAGED_MULTI_TENANT_LLM_OBS_OR_CLOUD_NATIVE_MANAGED_AGENT_RUNTIME"
    )


class CandidateWitness(BaseModel):
    """A provider candidate within a per-cell witness column (C-OD-02 §2.2).

    Frozen → `Eq` + `Hash`, stable under serialization. A witness column is the
    bounded set of provider candidates committed at D6 v1.1 §1.1 cell-entry
    witness columns; deployment-binding-time operator selection within the
    column is permitted (C-OD-03), out-of-column selection is rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_name: str
    vendor_class: str
    deployment_form: str


class PerCellBackendBinding(BaseModel):
    """The backend-class + candidate witness column for one ACTIVE cell.

    Frozen → `Eq`. `backend_class` is a non-empty `frozenset[BackendClass]`
    (v2.8 D-1) — cardinality 1 for the six committed cells (1/2/3/6/7/8) and 2
    for the two design-time-flexible cells (4/5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: non-empty backend-class set per §2.1; |·| ∈ {1, 2}.
    backend_class: frozenset[BackendClass]
    #: the per-cell provider candidate witness column (§2.2).
    candidates: tuple[CandidateWitness, ...]


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    """Construct the canonical `CellID` for `(pt, ds)`."""
    return CellID(persona_tier=pt, deployment_surface=ds)


# --- §2.1/§2.2 per-cell backend class + witness column ---------------------

# cell-1 — solo-developer x local-development.
_CELL_1 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
# cell-2 — solo-developer x self-hosted-server.
_CELL_2 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.SELF_HOSTED_SERVER)
# cell-3 — solo-developer x managed-cloud.
_CELL_3 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.MANAGED_CLOUD)
# cell-4 — team-binding x local-development.
_CELL_4 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT)
# cell-5 — team-binding x self-hosted-server.
_CELL_5 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
# cell-6 — team-binding x managed-cloud.
_CELL_6 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.MANAGED_CLOUD)
# cell-7 — multi-tenant-compliance x self-hosted-server.
_CELL_7 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER)
# cell-8 — multi-tenant-compliance x managed-cloud.
_CELL_8 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD)


def _w(candidate_name: str, vendor_class: str, deployment_form: str) -> CandidateWitness:
    """Construct a `CandidateWitness` (compact §2.2 table transcription)."""
    return CandidateWitness(
        candidate_name=candidate_name,
        vendor_class=vendor_class,
        deployment_form=deployment_form,
    )


#: The per-cell backend-class bindings — exactly 8 entries, one per ACTIVE
#: cell (C-OD-02 §2.1 + §2.2; §2.3 cell-class commitment invariant). Backend
#: classes and witness columns transcribed from the §2.1 / §2.2 tables.
PER_CELL_BACKEND_BINDINGS: dict[CellID, PerCellBackendBinding] = {
    _CELL_1: PerCellBackendBinding(
        cell_id=_CELL_1,
        backend_class=frozenset({BackendClass.OTEL_ONLY}),
        candidates=(_w("otelcol-contrib + sqlite ring-buffer", "OTel-only", "in-process"),),
    ),
    _CELL_2: PerCellBackendBinding(
        cell_id=_CELL_2,
        backend_class=frozenset({BackendClass.DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE}),
        candidates=(
            _w("Langfuse self-hosted single-node", "dedicated LLM-obs", "single-node"),
            _w("Arize Phoenix OSS PostgreSQL single-node", "dedicated LLM-obs", "single-node"),
            _w("Helicone HTTP-proxy", "dedicated LLM-obs", "single-node"),
        ),
    ),
    _CELL_3: PerCellBackendBinding(
        cell_id=_CELL_3,
        backend_class=frozenset({BackendClass.CLOUD_NATIVE_LLM_OBS_PLATFORM}),
        candidates=(
            _w("Langfuse Cloud free-tier", "cloud-native LLM-obs", "managed-cloud"),
            _w("Arize Phoenix OSS at managed-cloud", "cloud-native LLM-obs", "managed-cloud"),
            _w("Datadog free-tier", "cloud-native LLM-obs", "managed-cloud"),
            _w("Sentry/Seer hobbyist tier", "cloud-native LLM-obs", "managed-cloud"),
        ),
    ),
    _CELL_4: PerCellBackendBinding(
        cell_id=_CELL_4,
        backend_class=frozenset(
            {
                BackendClass.OTEL_ONLY,
                BackendClass.DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE,
            }
        ),
        candidates=(
            _w("otelcol-contrib + sqlite ring-buffer", "OTel-only", "in-process"),
            _w("Langfuse self-hosted single-node", "dedicated LLM-obs", "single-node"),
        ),
    ),
    _CELL_5: PerCellBackendBinding(
        cell_id=_CELL_5,
        backend_class=frozenset(
            {
                BackendClass.DEDICATED_LLM_OBS_PLATFORM_MULTI_NODE,
                BackendClass.OTEL_TO_VENDOR,
            }
        ),
        candidates=(
            _w("Langfuse self-hosted multi-node ClickHouse", "dedicated LLM-obs", "multi-node"),
            _w("Arize AX self-hosted", "dedicated LLM-obs", "multi-node"),
            _w("Helicone self-hosted (ClickHouse + Kafka)", "dedicated LLM-obs", "multi-node"),
            _w("Datadog self-hosted equivalent", "dedicated LLM-obs", "multi-node"),
            _w("Sentry self-hosted", "dedicated LLM-obs", "multi-node"),
            _w("Grafana stack", "OTel-to-vendor", "multi-node"),
        ),
    ),
    _CELL_6: PerCellBackendBinding(
        cell_id=_CELL_6,
        backend_class=frozenset({BackendClass.CLOUD_NATIVE_LLM_OBS_PLATFORM}),
        candidates=(
            _w("Langfuse Cloud paid tier", "cloud-native LLM-obs", "managed-cloud"),
            _w("Arize AX SaaS", "cloud-native LLM-obs", "managed-cloud"),
            _w("LangSmith", "cloud-native LLM-obs", "managed-cloud"),
            _w("Datadog LLM Observability", "cloud-native LLM-obs", "managed-cloud"),
            _w("Sentry/Seer", "cloud-native LLM-obs", "managed-cloud"),
        ),
    ),
    _CELL_7: PerCellBackendBinding(
        cell_id=_CELL_7,
        backend_class=frozenset({BackendClass.SELF_HOSTED_MULTI_TENANT_LLM_OBS_PLATFORM}),
        candidates=(
            _w(
                "Langfuse self-hosted multi-tenant + per-tenant ClickHouse partitioning",
                "self-hosted multi-tenant LLM-obs",
                "multi-node",
            ),
            _w(
                "Arize AX self-hosted multi-tenant + per-tenant PostgreSQL schema separation",
                "self-hosted multi-tenant LLM-obs",
                "multi-node",
            ),
        ),
    ),
    _CELL_8: PerCellBackendBinding(
        cell_id=_CELL_8,
        backend_class=frozenset(
            {BackendClass.VENDOR_MANAGED_MULTI_TENANT_LLM_OBS_OR_CLOUD_NATIVE_MANAGED_AGENT_RUNTIME}
        ),
        candidates=(
            _w("AWS Bedrock AgentCore Runtime", "managed agent runtime", "managed-cloud"),
            _w("Google Vertex Agent Engine", "managed agent runtime", "managed-cloud"),
            _w(
                "LangSmith Enterprise (customer VPC)",
                "vendor-managed multi-tenant LLM-obs",
                "managed-cloud",
            ),
            _w(
                "Langfuse Cloud Enterprise",
                "vendor-managed multi-tenant LLM-obs",
                "managed-cloud",
            ),
        ),
    ),
}


def select_backend_class(cell: CellID) -> frozenset[BackendClass]:
    """Return the non-empty backend-class set committed for `cell` (C-OD-02 §2.1).

    Raises `CellBindingViolation` (the `Err` arm) for the EXCLUDED cell per the
    U-OD-01 `reject_excluded_cell` composition — backend class is undefined at
    the EXCLUDED cell (acc #5). For every ACTIVE cell returns a non-empty set:
    a singleton for the six committed cells (1/2/3/6/7/8), a 2-element set for
    cell-4 and cell-5 (the §2.1 design-time-flexible disjunction rows).
    """
    reject_excluded_cell(cell)
    return PER_CELL_BACKEND_BINDINGS[cell].backend_class


def enumerate_candidates(cell: CellID) -> tuple[CandidateWitness, ...]:
    """Return the candidate witness column for `cell` (C-OD-02 §2.2, acc #6).

    Candidates are witness columns — operators MAY select within the list at
    deployment-binding time; out-of-witness selection is structurally rejected
    per §2.3. Raises `CellBindingViolation` for the EXCLUDED cell.
    """
    reject_excluded_cell(cell)
    return PER_CELL_BACKEND_BINDINGS[cell].candidates


# Sanity-pin: a missing ACTIVE cell would break `select_backend_class` —
# assert coverage at import (8 ACTIVE cells per C-OD-01 §1.3).
assert set(PER_CELL_BACKEND_BINDINGS) == set(ACTIVE_CELLS), (
    "PER_CELL_BACKEND_BINDINGS must cover exactly the 8 ACTIVE cells"
)
