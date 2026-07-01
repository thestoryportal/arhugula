"""HITL-as-tool-call rewriting algorithm + three semantic variants — U-CP-39.

Implements C-CP-17 §17.2 (the HITL-as-tool-call rewriting contract). Declares
the `HITLSemanticVariant` 3-value enum, the `EngineBindingClass` enum, the
`HITL_SEMANTIC_VARIANTS` 3-entry binding table, the `RewrittenToolCall`
record, and `rewrite_tool_call_to_hitl` — the rewriting algorithm.

Every tool call is evaluated against `_hitl_required` (U-CP-43) before
dispatch (acceptance #7): if the predicate is false the original tool call
passes through unchanged; if true the call is rewritten into one of three
semantic variants, selected deterministically by the cell synchrony class.

`ToolName` / `MCPServerID` are the AS-owned tool-name / server-name concepts;
no NewType is landed for either in `harness_as` (the spec treats them as
opaque string keys), so both are typed `str` here — the same precedent as
`ToolName` at U-CP-04 / U-CP-38.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.6 U-CP-39 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §17 C-CP-17 §17.2;
ADR-D5 v1.3 §1.3.2.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum

from harness_core import PersonaTier
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteResult
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import ProposedAction
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.state_ledger_canonicalization import _canonicalize_outcome_bytes
from harness_cp.validator_fail_transient_staircase import (
    CrossTrustBoundaryState,
    compute_restricted_palette,
)

#: `ToolName` — AS-owned tool-name concept; no NewType landed in `harness_as`.
type ToolName = str
#: `MCPServerID` — AS-owned MCP-server-id concept; no NewType landed.
type MCPServerID = str

# The full 4-response HITL palette per C-CP-16 §16.1 (U-CP-37 canonical set).
_FULL_PALETTE: frozenset[HITLResponse] = frozenset(HITLResponse)


class HITLSemanticVariant(StrEnum):
    """The 3 HITL-as-tool-call semantic variants (C-CP-17 §17.2)."""

    REQUEST_HUMAN_INPUT = "request_human_input"
    """`request_human_input(prompt, options)` — synchronous return; bound to
    sync-blocking cells (§17.2 row 1)."""

    AWAIT_HUMAN_APPROVAL = "await_human_approval"
    """`await_human_approval(action, context, channel)` — durable
    signal-and-wait; bound to durable-async cells (§17.2 row 2)."""

    ESCALATE_TO_HUMAN = "escalate_to_human"
    """`escalate_to_human(severity, summary, retry_history)` — triggered post
    retry-budget exhaustion; composes with the §17.1 validator-escalation
    placement at all cells (§17.2 row 3)."""


class EngineBindingClass(StrEnum):
    """The engine-binding class of a HITL semantic variant (C-CP-17 §17.2)."""

    SYNC_BLOCKING = "sync-blocking"
    DURABLE_ASYNC = "durable-async"
    ALL_CELLS = "all-cells"


class HITLSemanticVariantBinding(BaseModel):
    """One §17.2 variant ↔ engine-binding ↔ cell-mapping row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: HITLSemanticVariant
    tool_signature: str
    """The §17.2 "Tool signature" column verbatim."""

    engine_binding: EngineBindingClass
    cell_mapping: str
    """The §17.2 "Cell mapping" column verbatim."""


HITL_SEMANTIC_VARIANTS: tuple[HITLSemanticVariantBinding, ...] = (
    HITLSemanticVariantBinding(
        variant=HITLSemanticVariant.REQUEST_HUMAN_INPUT,
        tool_signature="request_human_input(prompt, options)",
        engine_binding=EngineBindingClass.SYNC_BLOCKING,
        cell_mapping="C-CP-18 §18.1 sync-blocking rows",
    ),
    HITLSemanticVariantBinding(
        variant=HITLSemanticVariant.AWAIT_HUMAN_APPROVAL,
        tool_signature="await_human_approval(action, context, channel)",
        engine_binding=EngineBindingClass.DURABLE_ASYNC,
        cell_mapping="C-CP-18 §18.1 durable-async rows",
    ),
    HITLSemanticVariantBinding(
        variant=HITLSemanticVariant.ESCALATE_TO_HUMAN,
        tool_signature="escalate_to_human(severity, summary, retry_history)",
        engine_binding=EngineBindingClass.ALL_CELLS,
        cell_mapping="Composes with §17.1 validator-escalation placement",
    ),
)
"""The 3 §17.2 HITL semantic-variant bindings, verbatim."""


class RewrittenToolCall(BaseModel):
    """The outcome of a HITL-as-tool-call rewriting evaluation (§17.2).

    When `hitl_required` is false the tool call is unchanged (`variant` and
    `response_palette` are `None` — the original call is passed through). When
    true the call carries the selected semantic variant and the cell's
    response palette (full, or restricted per the U-CP-48 cross-trust-boundary
    rule).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: ToolName
    server: MCPServerID
    hitl_required: bool
    variant: HITLSemanticVariant | None
    """Populated iff `hitl_required` — the selected §17.2 variant."""

    response_palette: frozenset[HITLResponse] | None
    """Populated iff `hitl_required` — full, or restricted per U-CP-48."""


def select_variant(
    cell_synchrony_class: SynchronyClass,
) -> HITLSemanticVariant:
    """Select the §17.2 semantic variant for a cell synchrony class (acc #4).

    Deterministic: `SYNC_BLOCKING` → `REQUEST_HUMAN_INPUT`; `DURABLE_ASYNC` →
    `AWAIT_HUMAN_APPROVAL`. The `BOTH_BY_TIER` and `EXCLUDED` synchrony classes
    route to `ESCALATE_TO_HUMAN` — the all-cells validator-escalation variant
    (the §17.2 row-3 variant composes with the §17.1 validator-escalation
    placement at any cell).
    """
    if cell_synchrony_class is SynchronyClass.SYNC_BLOCKING:
        return HITLSemanticVariant.REQUEST_HUMAN_INPUT
    if cell_synchrony_class is SynchronyClass.DURABLE_ASYNC:
        return HITLSemanticVariant.AWAIT_HUMAN_APPROVAL
    return HITLSemanticVariant.ESCALATE_TO_HUMAN


def rewrite_tool_call_to_hitl(
    tool: ToolName,
    server: MCPServerID,
    persona_tier: PersonaTier,
    proposed_action: ProposedAction,
    cell_synchrony_class: SynchronyClass,
    cross_trust_boundary_state: CrossTrustBoundaryState,
    hitl_required: bool,
) -> RewrittenToolCall:
    """Rewrite a tool call into a HITL semantic variant per C-CP-17 §17.2.

    The `_hitl_required` predicate is evaluated by U-CP-43; this unit consumes
    the boolean result (acceptance #3 — passed as `hitl_required`; the U-CP-43
    `hitl_required(GateLevelInput)` predicate is the upstream evaluator). When
    false, the original tool call is returned unchanged. When true, the call
    is rewritten: the variant is selected deterministically by
    `cell_synchrony_class` (acceptance #4) and the response palette is the
    full 4-response set when no cross-trust-boundary state is active, or the
    U-CP-48 restricted palette otherwise (acceptance #5).

    Rewriting fires **before** tool dispatch (acceptance #7) — `rewrite` is the
    last gate before the action surface.

    `persona_tier` and `proposed_action` are part of the §17.2 rewriting
    argument set; per-tool `tier` is read from SKILL.md frontmatter / MCP
    server manifest at runtime (acceptance #6 — this unit does not declare the
    frontmatter schema; that is the AS-side C4 contract).
    """
    _ = (persona_tier, proposed_action)
    if not hitl_required:
        return RewrittenToolCall(
            tool=tool,
            server=server,
            hitl_required=False,
            variant=None,
            response_palette=None,
        )
    if cross_trust_boundary_state is CrossTrustBoundaryState.NONE:
        palette = _FULL_PALETTE
    else:
        palette = compute_restricted_palette(cross_trust_boundary_state)
    return RewrittenToolCall(
        tool=tool,
        server=server,
        hitl_required=True,
        variant=select_variant(cell_synchrony_class),
        response_palette=palette,
    )


# --- U-CP-77 §16.5 greenfield composer — CP→IS state-ledger emission -------
#
# `emit_hitl_tool_call_rewriting_state_ledger_entry` is the §16.5 row U-CP-37
# greenfield composer producing the IS-anchored state-ledger entry per CP spec
# v1.26 §16.5.3 + §16.5.4 + §16.5.5 + §16.5.7 at HITL tool-call rewriting
# invocations. ZERO CP audit-ledger entry is emitted per §16.5.9 invariant 5
# (greenfield composer). Sibling to U-CP-74/U-CP-75/U-CP-76 §16.5 composers;
# reuses shared `_canonicalize_outcome_bytes` helper from U-CP-74.


_HITL_TOOL_CALL_REWRITING_ACTION_ID = "cp.hitl-tool-call-rewriting"
"""CP spec v1.26 §16.5.3 row U-CP-37 canonical action_id."""

_RECORD_SEPARATOR = b"\x1e"
"""ASCII 0x1E (record-separator) byte — CP spec v1.26 §16.5.4 canonical-form
rule shared across §16.5 composers."""


def _hitl_tool_call_rewriting_idempotency_key(
    workflow_id: str,
    step_id: str,
    tool_call_id: str,
    semantic_variant_binding_id: str,
    outcome_hash_hex: str,
) -> str:
    """Compose the U-CP-37 idempotency-key per CP spec v1.26 §16.5.4 row 4.

    Bytes are the 0x1E-separated 5-tuple `(workflow_id, step_id, tool_call_id,
    semantic_variant_binding_id, sha256(outcome_canonical_bytes).hex())`;
    SHA-256-hashed; hex-64 encoded. v1.25 disambiguator segments preserved
    verbatim per Q-β.i-1(a); the outcome-hash suffix carries the Q5(a)
    "hash-over-outcome-bytes" semantic at the dedup-key discriminator.
    """
    segments = [
        workflow_id.encode("utf-8"),
        step_id.encode("utf-8"),
        tool_call_id.encode("utf-8"),
        semantic_variant_binding_id.encode("utf-8"),
        outcome_hash_hex.encode("utf-8"),
    ]
    return hashlib.sha256(_RECORD_SEPARATOR.join(segments)).hexdigest()


async def emit_hitl_tool_call_rewriting_state_ledger_entry(
    *,
    workflow_id: str,
    step_id: str,
    tool_call_id: str,
    semantic_variant_binding_id: str,
    rewritten_tool_call: RewrittenToolCall,
    actor: ActorIdentity,
    ledger_writer: Callable[[EntryPayload], Awaitable[WriteResult]],
    procedural_tier_snapshot_resolver: Callable[[], Identifier],
) -> WriteResult:
    """Compose + emit the §16.5 IS-anchored state-ledger entry for U-CP-37.

    Per CP spec v1.26 §16.5.3: produces `EntryPayload` per IS HEAD 4-field shape
    `(action_id, idempotency_key, actor, timestamp)`. `response_hash` and
    `prior_event_hash` are IS-internal — composer does NOT control them
    (C-IS-06 §6.2 + C-IS-13 §13.5). The outcome-bytes semantic at §16.5.5 row
    U-CP-37 (`RewrittenToolCall` canonical JSON bytes) is carried at the
    `idempotency_key` discriminator per §16.5.4 + Q-β.i-1(a).

    Fires AFTER `rewrite_tool_call_to_hitl(...)` at line 149 produces the
    `RewrittenToolCall` and BEFORE the rewritten call returns to the caller
    per §16.5.7. ZERO `CPAuditLedgerEntry` is constructed per §16.5.9
    invariant 5 (greenfield composer at this CP source).

    Composer awaits `ledger_writer(payload)` return per §16.5.9 invariant 4;
    does NOT condition on `WriteResult` variant.
    """
    outcome_canonical_bytes = _canonicalize_outcome_bytes(rewritten_tool_call)
    outcome_hash_hex = hashlib.sha256(outcome_canonical_bytes).hexdigest()
    idempotency_key = _hitl_tool_call_rewriting_idempotency_key(
        workflow_id,
        step_id,
        tool_call_id,
        semantic_variant_binding_id,
        outcome_hash_hex,
    )
    payload = EntryPayload(
        action_id=Identifier(_HITL_TOOL_CALL_REWRITING_ACTION_ID),
        idempotency_key=Identifier(idempotency_key),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id=str(actor)),
        timestamp=datetime.now(UTC),
        procedural_tier_snapshot_ref=procedural_tier_snapshot_resolver(),
    )
    return await ledger_writer(payload)
