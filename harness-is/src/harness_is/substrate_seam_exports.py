"""IS substrate seam exports manifest — U-IS-17.

Implements C-IS-10 §10.1-§10.6 (the IS-axis substrate seam exports surface).
Declares the terminal aggregate exporter manifest — six `SubstrateSeamExport`
records the AS / CP / OD axes consume via stable `C-IS-10 §10.X` citations.

Declarative only — no executable behavior (acceptance #5). U-IS-17 is the
IS-axis-stream terminal unit.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.2 U-IS-17
(REVISED — R2 CONFORM; manifest content preserved verbatim from v2.1 §2.6);
Spec_Information_Substrate_v1.md C-IS-10 §10.1-§10.6.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import UnitId
from pydantic import BaseModel, ConfigDict


class SeamId(StrEnum):
    """The 6 IS substrate seam exports (C-IS-10 §10.1-§10.6)."""

    STATE_LEDGER_ENTRY_SHAPE_EXPORT = "state_ledger_entry_shape_export"
    IDEMPOTENCY_KEY_JOIN_EXPORT = "idempotency_key_join_export"
    HASH_CHAIN_CONSTRUCTION_DISCIPLINE_EXPORT = "hash_chain_construction_discipline_export"
    FILESYSTEM_PATH_CONTRACT_EXPORT = "filesystem_path_contract_export"
    JSONL_EVENT_LEDGER_FORMAT_EXPORT = "jsonl_event_ledger_format_export"
    WORKLOAD_CLASS_OPT_IN_MANIFEST_EXPORT = "workload_class_opt_in_manifest_export"


class ConsumingAxis(StrEnum):
    """A downstream axis that consumes an IS substrate seam export."""

    ACTION_SURFACE = "action_surface"
    CONTROL_PLANE = "control_plane"
    OPERATIONAL_DISCIPLINE = "operational_discipline"


class SubstrateSeamExport(BaseModel):
    """One IS substrate seam export declaration (C-IS-10 §10.X)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seam_id: SeamId
    spec_citation: str
    export_surface: str
    carrier_units: tuple[UnitId, ...]
    consuming_axes: tuple[ConsumingAxis, ...]
    composition_references: tuple[str, ...]


_AS = ConsumingAxis.ACTION_SURFACE
_CP = ConsumingAxis.CONTROL_PLANE
_OD = ConsumingAxis.OPERATIONAL_DISCIPLINE

#: ADR body-citation versions per U-IS-17 acceptance #7 (latest filed per
#: Workflow v1.6 §7 at IS plan v2.1; preserved verbatim at v2.3 R2 CONFORM).
ADR_BODY_CITATION_VERSIONS: dict[str, str] = {
    "F1": "v1.2",
    "F2": "v1.2",
    "F3": "v1.1",
    "D1": "v1.1",
    "D2": "v1.1",
    "D3": "v1.2",
    "D4": "v1.1",
    "D5": "v1.3",
    "D6": "v1.1",
}

#: The IS-axis substrate seam exports manifest (C-IS-10 §10.1-§10.6) — the
#: terminal aggregate exporter, consumed by AS / CP / OD via stable citation.
IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST: tuple[SubstrateSeamExport, ...] = (
    SubstrateSeamExport(
        seam_id=SeamId.STATE_LEDGER_ENTRY_SHAPE_EXPORT,
        spec_citation="C-IS-10 §10.1",
        export_surface="Six-field `StateLedgerEntry` record per C-IS-05",
        carrier_units=(UnitId("U-IS-07"),),
        consuming_axes=(_CP, _OD, _AS),
        composition_references=(
            "D1 v1.1 engine event history joins on `idempotency_key` (Tier-3 ↔ "
            "Tier-5 ledger composition per ADR-F3 §Consequences (a) + ADR-D1 "
            "v1.1); D5 v1.3 audit-ledger inherits entry shape + `audit.*` "
            "attribute namespace per ADR-D5 v1.3 §1.4 + §1.4.1; D2 v1.1 "
            "sandbox-violation events join on `idempotency_key` per ADR-D2 v1.1 "
            "§1.8; D6 v1.1 cost-attribution-per-span joins on `idempotency_key` "
            "per ADR-D6 v1.1 §1.5.",
        ),
    ),
    SubstrateSeamExport(
        seam_id=SeamId.IDEMPOTENCY_KEY_JOIN_EXPORT,
        spec_citation="C-IS-10 §10.2",
        export_surface="`idempotency_key` field; harness-canonical cross-axis join key",
        carrier_units=(UnitId("U-IS-07"), UnitId("U-IS-12")),
        consuming_axes=(_AS, _CP, _OD),
        composition_references=(
            "Cross-axis join key for replay-safe composition per ADD §2.2 "
            "Synthesis closing sentence; F2-12 carry-forward — "
            "replay-trace-emission contract (D1 v1.1 → v1.2) deferred per ADD "
            "§6.3.1 + PRD §[carry-forwards] [CF-1]: span re-emission semantics "
            "under engine replay; `retry.attempt` sibling-span discipline; "
            "trace-ingestion dedup composition with `idempotency_key` remain "
            "open.",
        ),
    ),
    SubstrateSeamExport(
        seam_id=SeamId.HASH_CHAIN_CONSTRUCTION_DISCIPLINE_EXPORT,
        spec_citation="C-IS-10 §10.3",
        export_surface="Canonicalize → SHA-256 → prior-event-hash chaining per C-IS-06",
        carrier_units=(UnitId("U-IS-08"), UnitId("U-IS-09"), UnitId("U-IS-10")),
        consuming_axes=(_OD,),
        composition_references=(
            "D5 v1.3 audit-ledger uses F2 hash-chain construction at "
            "team-binding+ persona tiers per ADR-D5 v1.3 §1.4 + §1.4.1; "
            "multi-tenant-compliance persona tier extends hash chain with "
            "cryptographic signature (`audit.signature.value` + "
            "`audit.signature.algorithm` + `audit.signature.key_id` + "
            "`audit.signature.key_period`) per ADR-D5 v1.3 §1.4.",
        ),
    ),
    SubstrateSeamExport(
        seam_id=SeamId.FILESYSTEM_PATH_CONTRACT_EXPORT,
        spec_citation="C-IS-10 §10.4",
        export_surface="Canonical filesystem path classes per C-IS-01",
        carrier_units=(UnitId("U-IS-01"), UnitId("U-IS-02")),
        consuming_axes=(_AS, _CP),
        composition_references=(
            "D3 v1.2 Skills loading discipline reads Skills-as-files from "
            "filesystem per cache-prefix integrity discipline per ADR-D3 v1.2; "
            "F1 v1.2 routing manifest resides at canonical filesystem path per "
            "ADR-F1 v1.2 Consequences §(a).",
        ),
    ),
    SubstrateSeamExport(
        seam_id=SeamId.JSONL_EVENT_LEDGER_FORMAT_EXPORT,
        spec_citation="C-IS-10 §10.5",
        export_surface="JSONL with stable indexable per-event shape per C-IS-07 §7.3",
        carrier_units=(
            UnitId("U-IS-05"),
            UnitId("U-IS-07"),
            UnitId("U-IS-11"),
            UnitId("U-IS-12"),
        ),
        consuming_axes=(_OD,),
        composition_references=(
            "D6 v1.1 OTLP collector boundary composes against F2 JSONL event "
            "ledger at within-turn streaming + across-turn durable trace "
            "storage per ADR-D6 v1.1 §1.7 (T-perm-2 D6-layer commitment per "
            "ADD §5.2.2).",
        ),
    ),
    SubstrateSeamExport(
        seam_id=SeamId.WORKLOAD_CLASS_OPT_IN_MANIFEST_EXPORT,
        spec_citation="C-IS-10 §10.6",
        export_surface="`WorkloadManifestOptIns` schema per C-IS-08 §8.1 + C-IS-09 §9.1",
        carrier_units=(UnitId("U-IS-13"),),
        consuming_axes=(_CP,),
        composition_references=(
            "D4 v1.1 sub-agent fan-out composes worktree-isolation with "
            "sub-agent privilege inheritance + sandbox-tier monotonicity + "
            "cross-deployment monotonicity per ADD §5.3.2 sub-agent boundary as "
            "monotonic-only descent; D5 v1.3 cross-deployment monotonicity "
            "engages T-perm-3 at shadow-Git checkpoint cadence vs retry-mechanics "
            'seam per ADR-F2 §"Permanent tensions engaged" T-perm-3 touch + '
            "ADD §5.2.3 residual surface.",
        ),
    ),
)
