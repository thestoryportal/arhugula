"""Persona-tier × engine-class HITL matrix + cell exclusion inheritance — U-CP-40.

Implements C-CP-18 §18.1 + §18.2 — the synchrony-class × HITL-primitive-shape
2D matrix per persona-tier × D1-engine-class, with cell-exclusion inheritance
from C-CP-07 §7.2. Declares `SynchronyClass`, `HITLPrimitiveShape`,
`HITLMatrixCell`, the 15-entry `HITL_MATRIX`, and the `matrix_cell_for` lookup.

**Declaration-site conversion (v2.6).** `PersonaTier` is imported from
`harness-core` (U-CORE-01) — not re-declared. `SynchronyClass`,
`HITLPrimitiveShape`, and `HITLMatrixCell` stay U-CP-40-owned (no other axis
declares them — carrier-map disposition: in-axis self-declared). The matrix
body is preserved verbatim from the v2.4 → v2.1 body.

The 15 cells are the §18.1 matrix verbatim (3 persona tiers × 5 engine
classes); the two `(_, pure-pattern-no-engine)` cells at `team-binding` and
`multi-tenant-compliance` are `EXCLUDED`, inheriting the C-CP-07 §7.2
candidate-set exclusion without revisiting it (§18.2).

Authority: Implementation_Plan_Control_Plane_v2_6.md §2.5 U-CP-40 (v2.6
declaration-site conversion — `PersonaTier` import from `harness-core`;
`SynchronyClass`/`HITLPrimitiveShape`/`HITLMatrixCell` matrix preserved
verbatim from v2.1/v2.4); Spec_Control_Plane_v1_2.md §18 C-CP-18 §18.1 + §18.2
(preserved verbatim into v1.3); ADR-D5 v1.3 §1.2 + §1.7.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass


class SynchronyClass(StrEnum):
    """The 4 HITL synchrony classes (C-CP-18 §18.1 + §18.3).

    Closed at cardinality 4. `BOTH_BY_TIER` is the §18.3 per-tool overlay
    class; `EXCLUDED` marks a structurally-excluded cell per §18.2.
    """

    SYNC_BLOCKING = "sync-blocking"
    DURABLE_ASYNC = "durable-async"
    BOTH_BY_TIER = "both-by-tier"
    EXCLUDED = "excluded"


class HITLPrimitiveShape(StrEnum):
    """The 12 HITL primitive shapes named in the C-CP-18 §18.1 matrix cells."""

    IN_PROCESS_FUNCTION_SYNCHRONOUS_RETURN = "in-process-function-synchronous-return"
    LANGGRAPH_INTERRUPT_COMMAND_RESUME = "langgraph-interrupt-command-resume"
    TWELVE_FACTOR_APPLICATION_DEFINED_EVENT_AND_RESUME = (
        "twelve-factor-application-defined-event-and-resume"
    )
    SEGMENT_RESUME_WITH_APPROVAL_PENDING_MARKER = "segment-resume-with-approval-pending-marker"
    CONTACT_CHANNEL_CR_MESH_PATTERN = "contact-channel-cr-mesh-pattern"
    TEMPORAL_WAIT_CONDITION_SIGNAL_HANDLER = "temporal-wait-condition-signal-handler"
    LANGGRAPH_POSTGRES_REDIS_LEASE = "langgraph-postgres-redis-lease"
    CLAUDE_CODE_PERMISSION_MODEL = "claude-code-permission-model"
    TEMPORAL_CLOUD_BEDROCK_VERTEX_NATIVE = "temporal-cloud-bedrock-vertex-native"
    LANGGRAPH_DYNAMODB_MANAGED_CHECKPOINTER = "langgraph-dynamodb-managed-checkpointer"
    ACP_K8S_MULTI_TENANT_CONTACT_CHANNEL = "acp-k8s-multi-tenant-contact-channel"
    MANAGED_WAL_CRYPTOGRAPHIC_AUDIT = "managed-wal-cryptographic-audit"


class HITLMatrixCell(BaseModel):
    """One cell of the C-CP-18 §18.1 persona-tier × engine-class matrix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    engine_class: EngineClass
    synchrony_class: SynchronyClass
    primary_primitive_shapes: tuple[HITLPrimitiveShape, ...]
    candidate_evidence: str
    """The §18.1 cell evidence column verbatim."""

    is_excluded: bool
    exclusion_source: str | None
    """`"C-CP-07 §7.2"` for excluded cells; `None` otherwise."""


# --- §18.1 matrix construction ---------------------------------------------

_PT = PersonaTier
_EC = EngineClass
_SC = SynchronyClass
_HS = HITLPrimitiveShape

# Each tuple: persona_tier, engine_class, synchrony, (shapes...), evidence.
# Excluded cells carry an empty shapes tuple. The matrix is the §18.1 table
# read row-by-row (solo → team → multi; each × 5 engine classes).
_MATRIX_ROWS: tuple[
    tuple[PersonaTier, EngineClass, SynchronyClass, tuple[HITLPrimitiveShape, ...], str],
    ...,
] = (
    # --- solo-developer row ---
    (
        _PT.SOLO_DEVELOPER,
        _EC.EVENT_SOURCED_REPLAY,
        _SC.SYNC_BLOCKING,
        (_HS.IN_PROCESS_FUNCTION_SYNCHRONOUS_RETURN,),
        "sync-blocking PRIMARY | in-process function with synchronous return; "
        "durable-async available via DBOS-as-library MODERATE",
    ),
    (
        _PT.SOLO_DEVELOPER,
        _EC.SAVE_POINT_CHECKPOINT,
        _SC.SYNC_BLOCKING,
        (_HS.LANGGRAPH_INTERRUPT_COMMAND_RESUME,),
        "sync-blocking PRIMARY | LangGraph `interrupt()` + Command resume per "
        "LangGraph HITL doc [HIGH]",
    ),
    (
        _PT.SOLO_DEVELOPER,
        _EC.PURE_PATTERN_NO_ENGINE,
        _SC.SYNC_BLOCKING,
        (_HS.TWELVE_FACTOR_APPLICATION_DEFINED_EVENT_AND_RESUME,),
        "sync-blocking PRIMARY | 12-Factor Factor 7 application-defined event-and-resume [HIGH]",
    ),
    (
        _PT.SOLO_DEVELOPER,
        _EC.RECONCILER_LOOP,
        _SC.DURABLE_ASYNC,
        (_HS.CONTACT_CHANNEL_CR_MESH_PATTERN,),
        "durable-async PRIMARY (rare at solo; if K8s local — Kind/k3s) | "
        "`ContactChannel` CR mesh-pattern",
    ),
    (
        _PT.SOLO_DEVELOPER,
        _EC.WAL_SEGMENT,
        _SC.SYNC_BLOCKING,
        (_HS.SEGMENT_RESUME_WITH_APPROVAL_PENDING_MARKER,),
        "sync-blocking PRIMARY | segment-resume on restart with approval-pending-segment marker",
    ),
    # --- team-binding row ---
    (
        _PT.TEAM_BINDING,
        _EC.EVENT_SOURCED_REPLAY,
        _SC.DURABLE_ASYNC,
        (_HS.TEMPORAL_WAIT_CONDITION_SIGNAL_HANDLER,),
        "durable-async PRIMARY | Temporal `wait_condition` + signal-handler "
        "with `timeout=days` per Temporal HITL doc [HIGH]",
    ),
    (
        _PT.TEAM_BINDING,
        _EC.SAVE_POINT_CHECKPOINT,
        _SC.BOTH_BY_TIER,
        (_HS.LANGGRAPH_POSTGRES_REDIS_LEASE, _HS.CLAUDE_CODE_PERMISSION_MODEL),
        "both-by-tier PRIMARY | LangGraph + Postgres + Redis-lease + per-tool "
        "tier annotation; Claude Code permission model `deny -> ask -> allow`",
    ),
    (
        _PT.TEAM_BINDING,
        _EC.PURE_PATTERN_NO_ENGINE,
        _SC.EXCLUDED,
        (),
        "EXCLUDED (per C-CP-07 §7.2 self-hosted-server row excludes pure-pattern for durable pole)",
    ),
    (
        _PT.TEAM_BINDING,
        _EC.RECONCILER_LOOP,
        _SC.DURABLE_ASYNC,
        (_HS.CONTACT_CHANNEL_CR_MESH_PATTERN,),
        "durable-async PRIMARY | `ContactChannel` CR mesh-pattern with K8s-resident operator",
    ),
    (
        _PT.TEAM_BINDING,
        _EC.WAL_SEGMENT,
        _SC.DURABLE_ASYNC,
        (_HS.SEGMENT_RESUME_WITH_APPROVAL_PENDING_MARKER,),
        "durable-async PRIMARY | segment-resume + external trigger via webhook ingress",
    ),
    # --- multi-tenant-compliance row ---
    (
        _PT.MULTI_TENANT_COMPLIANCE,
        _EC.EVENT_SOURCED_REPLAY,
        _SC.DURABLE_ASYNC,
        (_HS.TEMPORAL_CLOUD_BEDROCK_VERTEX_NATIVE,),
        "durable-async PRIMARY | Temporal Cloud / AWS Bedrock AgentCore / "
        "Google Vertex Agent Engine native HITL primitives",
    ),
    (
        _PT.MULTI_TENANT_COMPLIANCE,
        _EC.SAVE_POINT_CHECKPOINT,
        _SC.DURABLE_ASYNC,
        (_HS.LANGGRAPH_DYNAMODB_MANAGED_CHECKPOINTER,),
        "durable-async PRIMARY | LangGraph + DynamoDBSaver + managed "
        "checkpointer with engine-bound HITL signal",
    ),
    (
        _PT.MULTI_TENANT_COMPLIANCE,
        _EC.PURE_PATTERN_NO_ENGINE,
        _SC.EXCLUDED,
        (),
        "EXCLUDED (analogous to team-binding; pure-pattern excluded for "
        "durable pole at managed-cloud surface)",
    ),
    (
        _PT.MULTI_TENANT_COMPLIANCE,
        _EC.RECONCILER_LOOP,
        _SC.DURABLE_ASYNC,
        (_HS.ACP_K8S_MULTI_TENANT_CONTACT_CHANNEL,),
        "durable-async PRIMARY | ACP K8s-managed with multi-tenant "
        "`ContactChannel` namespace isolation",
    ),
    (
        _PT.MULTI_TENANT_COMPLIANCE,
        _EC.WAL_SEGMENT,
        _SC.DURABLE_ASYNC,
        (_HS.MANAGED_WAL_CRYPTOGRAPHIC_AUDIT,),
        "durable-async PRIMARY | managed-WAL with cryptographic-signed audit ledger",
    ),
)


HITL_MATRIX: tuple[HITLMatrixCell, ...] = tuple(
    HITLMatrixCell(
        persona_tier=tier,
        engine_class=engine,
        synchrony_class=sync,
        primary_primitive_shapes=shapes,
        candidate_evidence=evidence,
        is_excluded=sync is _SC.EXCLUDED,
        exclusion_source="C-CP-07 §7.2" if sync is _SC.EXCLUDED else None,
    )
    for tier, engine, sync, shapes, evidence in _MATRIX_ROWS
)
"""The 15-entry C-CP-18 §18.1 matrix (3 persona tiers × 5 engine classes)."""

_CELL_INDEX: dict[tuple[PersonaTier, EngineClass], HITLMatrixCell] = {
    (c.persona_tier, c.engine_class): c for c in HITL_MATRIX
}


def matrix_cell_for(persona_tier: PersonaTier, engine_class: EngineClass) -> HITLMatrixCell:
    """Return the §18.1 matrix cell for `(persona_tier, engine_class)`.

    Total over `PersonaTier × EngineClass` — `HITL_MATRIX` carries one entry
    per (tier, class) pair.
    """
    return _CELL_INDEX[(persona_tier, engine_class)]
