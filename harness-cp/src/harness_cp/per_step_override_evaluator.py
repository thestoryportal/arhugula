"""Per-step override evaluator + CP audit-ledger entry composition — U-CP-14.

Implements C-CP-06 §6.2 (the per-step override evaluator) and — per the
Implementation Plan v2.9 factor-out delta — C-CP-16 §16.2 + C-CP-20 §20.1/§20.4
(the CP audit-ledger entry records).

Declares the `CPAuditLedgerEntry` record (8 fields, response-conditional hash
fields per C-CP-16 §16.2), the `CPSignedAuditLedgerEntry` record (wraps
`CPAuditLedgerEntry` + 5 `audit.signature.*` fields per C-CP-20 §20.4), the
`StepEffectiveBinding` record, the deterministic `resolve_step_binding`
evaluator, and `emit_override_audit_entry`.

**Name-collision resolution (v2.9 §0.5.1).** `harness-od` (U-OD-00) has already
landed a distinct `AuditLedgerEntry` — the OD-local audit-ledger family. CP's
audit-ledger entry is a parallel sibling family, CP-spec-owned (C-CP-16 §16.2 /
C-CP-20 §20.1), composing against the IS-exported `StateLedgerEntry` shape via
the CP→IS edges. To avoid a nominal collision the CP types are named distinctly
— `CPAuditLedgerEntry` / `CPSignedAuditLedgerEntry` — with NO import of, and NO
structural reconciliation with, the OD `AuditLedgerEntry`. CP→OD stays
foreclosed (CXA matrix CP→OD = 0).

`emit_override_audit_entry` composes the override audit entry: it builds a
`CPAuditLedgerEntry` whose `prior_event_hash` is the F2 hash-chain link
(constructed per U-IS-08/09 canonicalize+chain discipline) and whose entry is
appended to the F2 ledger per U-IS-11. The F2 delegation surface is the
IS-exported `StateLedgerEntry` shape (C-IS-10 §10.1/§10.3/§10.5).

Authority: Implementation_Plan_Control_Plane_v2_9.md §2A U-CP-14 (revised body
— `CPAuditLedgerEntry` + `CPSignedAuditLedgerEntry` factor-out; §0.5.1
name-collision resolution); Spec_Control_Plane_v1_2.md §6 C-CP-06 §6.2 + §16
C-CP-16 §16.2 + §20 C-CP-20 §20.1, §20.4 (preserved verbatim into v1.3);
ADR-F2 v1.2 audit composition.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from harness_as import GateLevel
from harness_core import PersonaTier
from harness_core.identity import ActionID
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteResult
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import ActorIdentity, AgentRole, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.handoff_context import LedgerEntryRef
from harness_cp.hitl_placement import HITLPlacement, LoosenablePlacementKind
from harness_cp.state_ledger_canonicalization import _canonicalize_outcome_bytes
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry


class CPAuditLedgerEntry(BaseModel):
    """A CP per-response audit-ledger entry (C-CP-16 §16.2).

    One record with response-conditional optional hash fields — a faithful
    factor-out of the C-CP-16 §16.2 four-row per-response audit-ledger entry
    table. `edited_proposal_hash` is populated iff `response == "edit"`,
    `rejection_reason_hash` iff `"reject"`, `response_text_hash` iff `"respond"`;
    all three absent for `"approve"`.

    CP-spec-owned (v2.9 §0.5.1 T2 `AuditLedgerEntry @ CP` row); composes against
    the IS-exported `StateLedgerEntry` shape via the CP→IS edges. Nominally
    distinct from the OD-landed `AuditLedgerEntry` (U-OD-00) — no import, no
    structural reconciliation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: ActionID
    gate_level: GateLevel
    """`{auto, ask, deny}` per C-CP-19 §19.1."""

    response: str
    """`∈ {approve, edit, reject, respond}` per C-CP-16 §16.1."""

    edited_proposal_hash: str | None = None
    """SHA256 hex-64; populated iff `response == "edit"` (§16.2 row 2)."""

    rejection_reason_hash: str | None = None
    """SHA256 hex-64; populated iff `response == "reject"` (§16.2 row 3)."""

    response_text_hash: str | None = None
    """SHA256 hex-64; populated iff `response == "respond"` (§16.2 row 4)."""

    timestamp: str
    """ISO-8601 timestamp."""

    prior_event_hash: str
    """SHA256 hex-64 hash-chain link per C-IS-06."""


class CPSignedAuditLedgerEntry(BaseModel):
    """A signed CP audit-ledger entry (C-CP-20 §20.1, §20.4).

    `CPAuditLedgerEntry` + the five signature-bearing `audit.signature.*`
    attributes from C-CP-20 §20.4. A faithful factor-out of the C-CP-20 §20.1
    per-persona-tier cryptographic shape; emitted at multi-tenant-compliance
    (and team-binding opt-in). Nominally distinct from the OD `AuditLedgerEntry`
    family per §0.5.1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: CPAuditLedgerEntry
    audit_signature_sha256: str
    """Hex-64; the signed hash (C-CP-20 §20.4)."""

    audit_signature_value: bytes
    """Per-entry signature bytes (C-CP-20 §20.4)."""

    audit_signature_algorithm: str
    """`∈ {ed25519, ecdsa-p256, rsa-pss-2048}` (C-CP-20 §20.4)."""

    audit_signature_key_id: str
    """F5 signing-key identifier (C-CP-20 §20.4)."""

    audit_signature_key_period: int
    """Monotonic key-period (C-CP-20 §20.4)."""


class StepEffectiveBinding(BaseModel):
    """The effective per-step binding after override application (C-CP-06 §6.2).

    Combines manifest-entry defaults with the per-step override field-by-field.
    `override_audit_ref` is populated only when `override_applied` is `True`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    model_binding: ModelBinding
    """Effective binding — override value or manifest default."""

    model_binding_override: ModelBinding | None = None
    """v1.50 addition (CP spec v1.50 §6.2; B-MODEL-RESOLUTION-CONSOLIDATION). The
    resolved per-step MODEL override — the override value when a `StepOverride`
    for this step carries a `model_binding`, else `None`.

    Unlike `model_binding` (which resolves override-or-manifest-default to a
    concrete value), this is the `None`-or-override SIGNAL — mirroring
    `prompt_version_sha` / `agent_role`. It exists because `model_binding` is
    ALWAYS set (`override.model_binding or default`), so nothing downstream could
    otherwise distinguish a per-step model *override* from the manifest default.
    The C-RT-16 fallback wrapper reads this to honour the model-resolution
    precedence per-step > per-workload > per-role > routed > default (runtime
    spec §14.5.3/§14.6); `None` means "no per-step model override" → the wrapper
    falls through to the next precedence source.

    Like `prompt_version_sha`/`agent_role`, this rides `binding.model_dump(...)`
    into the per-step override state-ledger entry's outcome-hash for step-level
    provenance (CP spec v1.50 §6.6).
    """

    engine_class: EngineClass
    hitl_placement: HITLPlacement | None = None
    override_applied: bool
    override_audit_ref: LedgerEntryRef | None = None
    """Populated only when `override_applied` is `True`."""

    persona_tier: PersonaTier
    """Persona-tier resolution per CP spec v1.17 §6.5 (required, no default).

    Caller resolves persona tier prior to invocation per §6.5.3 — canonical
    upstream is `WorkflowManifestEntry.persona_tier` per §6.1 (also exposed
    at routing-manifest tier resolution per C-CP-01 §1.3).
    """

    prompt_version_sha: str | None = None
    """v1.37 addition (CP spec v1.37 §6.2; R-FS-1 arc B4 Slice 3 per-step
    PROMPT override). The resolved per-step prompt `version_sha` — the override
    value when a `StepOverride` for this step carries one, else `None`.

    Unlike `model_binding`/`engine_class` (which resolve override-or-manifest-
    default to a concrete value), this is `None`-or-override: there is no
    manifest-entry-level prompt default at the CP layer (the run-level default +
    per-role prompts resolve downstream at the runtime §14.5.2 dispatch). `None`
    here means "no per-step prompt override" → the runtime dispatch falls through
    to per-role, then the run-level default.

    Because this is a `StepEffectiveBinding` field, it rides
    `binding.model_dump(...)` into the per-step override state-ledger entry's
    outcome-hash (the wired `emit_override_state_ledger_entry` site), giving the
    per-step prompt override live step-level provenance (CP spec v1.37 §6.6).
    """

    agent_role: AgentRole | None = None
    """v1.38 addition (CP spec v1.38 §6.2; R-FS-1 arc B4 Slice 4 per-step ROLE
    override). The resolved per-step `AgentRole` — the override value when a
    `StepOverride` for this step carries one, else `None`.

    Like `prompt_version_sha`, this is `None`-or-override (no manifest-entry-level
    role default at the CP layer; the fan-out-derived role + linear-path default
    resolve downstream at CP-driver composition). `None` means "no per-step role
    override" → the driver composes `StepExecutionContext.agent_role` from the
    fan-out-derived role (non-linear) or leaves it unset (linear → runtime
    `_MVP_DEFAULT_AGENT_ROLE`).

    The CP driver folds this into the SINGLE `StepExecutionContext.agent_role`
    source at composition (precedence per-step > fan-out-derived > default), so
    the runtime dispatch reads one role source — the composition-time relaxation
    of the §14.5.3 invariant-2/3 (runtime spec v1.52). Rides
    `binding.model_dump(...)` into the per-step override state-ledger entry's
    outcome-hash for step-level provenance (CP spec v1.38 §6.6), like
    `prompt_version_sha`.
    """

    removed_placements: frozenset[LoosenablePlacementKind] = frozenset()
    """CP spec v1.53 addition (§6.2; `B-HITL-PLACEMENT-PER-STEP-LOOSEN`, R-FS-1
    final-closure — the operator-ratified committed-invariant relaxation of the
    §17.1 monotone-HITL "all cells" floor).

    The opt-in set of HITL placements this step REMOVES. `LoosenablePlacementKind`
    is a closed one-member enum (`SUB_AGENT_BOUNDARY` only) so `PRE_ACTION` /
    `VALIDATOR_ESCALATION` are STRUCTURALLY unrepresentable here (the §19.1
    floor-evaluation site + the §14.15-path placement, respectively — see
    `LoosenablePlacementKind`). **Default empty ⇒ the default (non-opted) path is
    byte-identical + monotone** (the `fold_step_hitl_placements` ADD-only fold is
    untouched; this carrier is a SEPARATE directive honoured at the
    SUB_AGENT_BOUNDARY composer).

    A removal is NOT unconditional: at the composer (`hitl_gate_composer.py` step
    4c) it is solo-scoped (`PersonaTier.SOLO_DEVELOPER` only — team = registered
    follow-on, multi-tenant structurally foreclosed) and FLOOR-CLAMPED — it
    overrides only the §19.1 PERSONA-tier human-oversight-at-handoff floor (and the
    `blast_radius` floor at the LOCAL_MUTATION cell per the ratified `{read-only,
    local-mutation}` scope); the HARD `per_tool` / `mcp_trust` floors and any
    `blast_radius` ABOVE local-mutation are NEVER override-able, so a removal on a
    high-blast / deny-tier-tool / untrusted-MCP dispatch is REFUSED (the gate
    fires) per the decline-mirror. Every applied removal is auto-audited
    (fail-closed) so a removed preventive gate never goes live un-audited.

    Like `prompt_version_sha`/`agent_role`, this rides `binding.model_dump(...)`
    into the per-step override state-ledger entry's outcome-hash for step-level
    provenance (CP spec v1.53 §6.6) — NO new §5.2/IS hash field.
    """


def resolve_step_binding(
    manifest_entry: WorkflowManifestEntry,
    step_id: str,
    *,
    default_model_binding: ModelBinding,
    persona_tier: PersonaTier,
) -> StepEffectiveBinding:
    """Resolve the effective binding for a step — deterministic (C-CP-06 §6.2).

    Applies the `manifest_entry.per_step_overrides` entry for `step_id` over the
    manifest-entry defaults field-by-field; an absent override field inherits
    the manifest default. No field-set substitution — each field is resolved
    independently. The procedure is deterministic given its inputs.

    `default_model_binding` carries the manifest-default model binding (the
    manifest-entry shape does not carry a top-level `model_binding`; the default
    is supplied by the caller's routing-manifest resolution per C-CP-01 §1.3).

    `persona_tier` is required (no default) per CP spec v1.17 §6.5; the caller
    resolves the tier from `WorkflowManifestEntry.persona_tier` (§6.1) or
    routing-manifest tier resolution (C-CP-01 §1.3) prior to invocation.
    """
    override = manifest_entry.per_step_overrides.get(step_id)  # type: ignore[arg-type]
    if override is None:
        return StepEffectiveBinding(
            step_id=step_id,
            model_binding=default_model_binding,
            engine_class=manifest_entry.engine_class,
            hitl_placement=None,
            override_applied=False,
            override_audit_ref=None,
            persona_tier=persona_tier,
        )
    audit_entry = emit_override_audit_entry(
        workflow_id=manifest_entry.workflow_id,
        step_id=step_id,
        override=override,
        actor=ActorIdentity("control-plane"),
    )
    return StepEffectiveBinding(
        step_id=step_id,
        model_binding=override.model_binding or default_model_binding,
        engine_class=override.engine_class or manifest_entry.engine_class,
        hitl_placement=override.hitl_placement,
        override_applied=True,
        override_audit_ref=LedgerEntryRef(
            action_id=ActionID(audit_entry.action_id),
            entry_hash=audit_entry.prior_event_hash,
            actor=ActorIdentity("control-plane"),
        ),
        persona_tier=persona_tier,
        # CP spec v1.37 §6.2 — per-step PROMPT override (B4 Slice 3). `None`-or-
        # override (no manifest-entry prompt default at the CP layer); the runtime
        # §14.5.2 dispatch resolves the sha → content with precedence
        # per-step > per-role > run-level default.
        prompt_version_sha=override.prompt_version_sha,
        # CP spec v1.38 §6.2 — per-step ROLE override (B4 Slice 4). `None`-or-
        # override; the CP driver folds it into the single
        # `StepExecutionContext.agent_role` source at composition (precedence
        # per-step > fan-out-derived > default), relaxing the §14.5.3 invariant-2/3
        # at composition-time only (single dispatch-read role source preserved).
        agent_role=override.agent_role,
        # CP spec v1.50 §6.2 — per-step MODEL override SIGNAL
        # (B-MODEL-RESOLUTION-CONSOLIDATION). `None`-or-override (the override's
        # own `model_binding`, NOT the resolved `model_binding` above which is
        # always concrete): the C-RT-16 wrapper reads this to honour per-step at
        # the head of the model-resolution precedence (runtime §14.5.3/§14.6).
        model_binding_override=override.model_binding,
        # CP spec v1.53 §6.2 — per-step HITL placement REMOVAL set
        # (B-HITL-PLACEMENT-PER-STEP-LOOSEN). Empty-or-opt-in; the
        # SUB_AGENT_BOUNDARY composer honours it solo-scoped + floor-clamped +
        # auto-audited (the ADD-only fold stays untouched).
        removed_placements=override.removed_placements,
    )


def emit_override_audit_entry(
    workflow_id: str,
    step_id: str,
    override: StepOverride,
    actor: ActorIdentity,
) -> CPAuditLedgerEntry:
    """Compose the per-step override audit-ledger entry (C-CP-06 §6.2 + §16.2).

    Builds a `CPAuditLedgerEntry` for the override application. The
    `action_id` is composed as `workflow_id || step_id` per the F2 audit
    composition. F2 canonicalize+hash is delegated to U-IS-08, chain
    construction to U-IS-09, and append to U-IS-11 — the entry composes against
    the IS-exported `StateLedgerEntry` shape (C-IS-10 §10.1/§10.3/§10.5). An
    override application is recorded as an `approve` response (no operator
    edit/reject/respond), so the three response-specific hash fields are absent.
    """
    # `override` + `actor` preserved as ignored per CP spec v1.28 §16.5.6.X:
    # C-CP-16 §16.2 audit-entry shape does not include an `actor` field;
    # `override`'s fields surface at the caller's `StepEffectiveBinding` per
    # line 193-205, not into the audit entry.
    _ = (override, actor)
    return CPAuditLedgerEntry(
        action_id=ActionID(f"{workflow_id}||{step_id}"),
        gate_level=GateLevel.AUTO,
        response="approve",
        timestamp=datetime.now(UTC).isoformat(),
        # `prior_event_hash="0"*64` sentinel canonical at solo-developer
        # tier per ADR-D5 §1.4 row 1 ("no hash chain required by default").
        # Team-binding+ tier wiring deferred per CP spec v1.28 §16.5.6.X.
        prior_event_hash="0" * 64,
    )


# --- U-CP-74 §16.5 sibling composer — CP→IS state-ledger emission ----------
#
# `emit_override_state_ledger_entry` is the §16.5 (S) sibling-variant composer
# producing the IS-anchored state-ledger entry per CP spec v1.26 §16.5.3 +
# §16.5.4 + §16.5.5 + §16.5.6 + §16.5.7. It is ADDITIVE — existing
# `emit_override_audit_entry` above (line 200) is preserved verbatim per
# §16.5.6 dual-emission discipline. The §16.5 contract preserves §16.2
# CPAuditLedgerEntry shape + §20.4 signing contract verbatim.
#
# Dual-emission wiring at `resolve_step_binding:179` invokes BOTH composers per
# §16.5.6. The async composer surface here is bound by the runtime-wiring
# layer (separate runtime-plan unit) to an async `ledger_writer` that wraps the
# IS HEAD sync `append_ledger_entry` per spec v1.26 §16.5.8.

_OVERRIDE_ACTION_ID = "cp.per-step-override-application"
"""CP spec v1.26 §16.5.3 row U-CP-14 canonical action_id."""

_RECORD_SEPARATOR = b"\x1e"
"""ASCII 0x1E (record-separator) byte — CP spec v1.26 §16.5.4 idempotency-key
canonical-form rule. Forecloses concatenation-ambiguity attacks across the
||-separated disambiguator segments."""


def _override_idempotency_key(
    workflow_id: str,
    step_id: str,
    outcome_hash_hex: str,
) -> str:
    """Compose the U-CP-14 idempotency-key per CP spec v1.27 §16.5.4 row 1.

    Bytes are the 0x1E-separated 3-tuple `(workflow_id, step_id,
    sha256(outcome_canonical_bytes).hex())`; SHA-256-hashed; hex-64 encoded. The
    `(workflow_id, step_id)` discriminator carries per-WorkflowManifestEntry
    step-id uniqueness per `per_step_overrides: dict[StepID, StepOverride]` at
    `workflow_manifest_entry.py:109`; the outcome-hash suffix carries the Q5(a)
    "hash-over-outcome-bytes" semantic at the dedup-key discriminator. v1.25 +
    v1.26 `override_id` + `policy_id` placeholder segments dropped per Q1=A
    operator ratification 2026-05-29 (Reading A).
    """
    segments = [
        workflow_id.encode("utf-8"),
        step_id.encode("utf-8"),
        outcome_hash_hex.encode("utf-8"),
    ]
    return hashlib.sha256(_RECORD_SEPARATOR.join(segments)).hexdigest()


def compose_override_entry_payload(
    *,
    workflow_id: str,
    step_id: str,
    post_override_step_config: Mapping[str, Any],
    actor: ActorIdentity,
    procedural_tier_snapshot_ref: Identifier | None,
    timestamp: datetime,
) -> EntryPayload:
    """Compose the §16.5 U-CP-14 override-application `EntryPayload` (NO write).

    The single source of truth for the override-entry SHAPE — `action_id`
    (`cp.per-step-override-application`), the §16.5.4 idempotency-key 3-tuple
    `(workflow_id, step_id, sha256(outcome_canonical_bytes))`, and the IS HEAD
    5-field payload `(action_id, idempotency_key, actor, timestamp,
    procedural_tier_snapshot_ref)`. `response_hash` / `prior_event_hash` are
    IS-internal (C-IS-06 §6.2 + C-IS-13 §13.5).

    Both write paths compose through here so the persisted override entry is
    byte-shape-identical across all topologies:

    - the async `emit_override_state_ledger_entry` composer below (the
      `SINGLE_THREADED_LINEAR` driver-thread site + the runtime cp_is_wiring
      binding), passing `timestamp=datetime.now(UTC)` + the resolver's value; and
    - the CP-driver buffered-branch path (`append_branch_override_ledger_entry`
      at `workflow_driver.py`, R-FS-1 `B-NONLINEAR-OVERRIDE-PROVENANCE`), passing
      a buffer-time placeholder `timestamp` the barrier drain re-stamps + the
      branch's caller-supplied `procedural_tier_snapshot_ref`.

    Because the key is the §16.5.4 per-`(step, outcome)` 3-tuple (NOT
    branch-scoped — branch-scoping would change the cleared §16.5.4 formula), a
    repeated `(step_id, outcome)` across non-linear iterations / recursion levels
    idempotently dedups at the IS writer to one entry — the override is a static
    binding property, not a per-execution event (the spec's designed key
    semantic).
    """
    outcome_canonical_bytes = _canonicalize_outcome_bytes(post_override_step_config)
    outcome_hash_hex = hashlib.sha256(outcome_canonical_bytes).hexdigest()
    idempotency_key = _override_idempotency_key(workflow_id, step_id, outcome_hash_hex)
    return EntryPayload(
        action_id=Identifier(_OVERRIDE_ACTION_ID),
        idempotency_key=Identifier(idempotency_key),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id=str(actor)),
        timestamp=timestamp,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
    )


async def emit_override_state_ledger_entry(
    *,
    workflow_id: str,
    step_id: str,
    post_override_step_config: Mapping[str, Any],
    actor: ActorIdentity,
    ledger_writer: Callable[[EntryPayload], Awaitable[WriteResult]],
    procedural_tier_snapshot_resolver: Callable[[], Identifier],
) -> WriteResult:
    """Compose + emit the §16.5 IS-anchored state-ledger entry for U-CP-14.

    Per CP spec v1.26 §16.5.3 + v1.29 §16.5.12 + v1.30 §1.2 canonical reading:
    produces `EntryPayload` per IS HEAD 5-field shape `(action_id,
    idempotency_key, actor, timestamp, procedural_tier_snapshot_ref)` via
    `compose_override_entry_payload` (the shared shape authority).

    The `procedural_tier_snapshot_resolver` kw-only param is invoked at emission
    per v1.30 §1.2 + §1.3 uniform-resolver-closure recipe; failure HALTs per
    §16.5.12.5 (composer propagates resolver-raise to caller).

    Composer awaits `ledger_writer(payload)` return per §16.5.9 invariant 4;
    does NOT condition on `WriteResult` variant.
    """
    payload = compose_override_entry_payload(
        workflow_id=workflow_id,
        step_id=step_id,
        post_override_step_config=post_override_step_config,
        actor=actor,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_resolver(),
        timestamp=datetime.now(UTC),
    )
    return await ledger_writer(payload)
