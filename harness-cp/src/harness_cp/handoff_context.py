"""`HandoffContext` + `ProposedAction` + `StateSummary` + `LedgerEntryRef`
family schemas — U-CP-30.

Implements C-CP-13 §13.1 (the `HandoffContext` seven-field record), §13.4
(`StateSummary`), and §13.5 (`LedgerEntryRef`). Also declares the supporting
factor-out types specified at Implementation Plan v2.9 §0.3: the `ActionKind`
enum (promoted from the v2.1 inline comment per carrier-map line 113), the
`ActionPayload` opaque alias, `ProposedAction`, `FailedAttempt`, `Alternative`,
`RetryHistory`, and `ExternalReference` (with its `ReferenceClass` enum per
C-CP-22 §22.2).

`HandoffContext` is the parent->child handoff payload at sub-agent-dispatch
cells. `StateSummary` is the across-turn state digest (the spec's `CurrentState`
spelling is unified to `StateSummary` per v2.9 §0.3 — no `CurrentState`
spelling appears). `LedgerEntryRef` is the F2 audit-trail anchor.

All structured types are faithful factor-outs of their committing spec
sections — no member set or field invented. Where a contract defers a sub-shape
(`ActionPayload`), the factor-out adopts the spec's opaque `Mapping[str, Any]`
vocabulary.

Authority: Implementation_Plan_Control_Plane_v2_9.md §2A U-CP-30 (revised body;
factor-out delta over v2.1); Spec_Control_Plane_v1_3.md §13 C-CP-13 §13.1 +
§13.4 + §13.5; §16 C-CP-16 §16.1; §17 C-CP-17 §17.1; §3 C-CP-03 §3.5
`retry.*` namespace; §22 C-CP-22 §22.2 material-diff reference-class table;
ADR-D5 §1.1.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from harness_core.identity import ActionID
from harness_is.state_ledger_entry_schema import Identifier
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.sub_agent_brief import SubAgentBrief

# `ActionPayload` is an opaque alias. NOT a T2 candidate (v2.9 §0.3.1): the
# `ProposedAction` committing sections (C-CP-16 / §17) reference
# `edited_proposal` / `edited_proposal_hash` but do NOT decompose the
# action-payload field shape; §16.4 carries an explicit deferred clause. The
# faithful factor-out is the spec's own opaque vocabulary — no field invented.
type ActionPayload = Mapping[str, Any]


class ActionKind(StrEnum):
    """The kind of a proposed action (C-CP-17 §17.1 placement-trigger taxonomy).

    Closed at cardinality 3. Promotion of the v2.1 U-CP-30 inline comment
    `// {TOOL_CALL, SUB_AGENT_DISPATCH, INFERENCE_STEP}` to a real enum
    (carrier-map line 113 — decided inline-comment-enum promotion). Values
    trace to the C-CP-17 §17.1 placement-trigger taxonomy + the C-CP-13 §13.1
    `ProposedAction` constituent."""

    TOOL_CALL = "tool_call"
    """Tool-call action (C-CP-17 §17.1 pre-action placement trigger)."""

    SUB_AGENT_DISPATCH = "sub_agent_dispatch"
    """Parent->child handoff (C-CP-17 §17.1 sub-agent-boundary placement)."""

    INFERENCE_STEP = "inference_step"
    """Inference-step action."""


class ReferenceClass(StrEnum):
    """External-reference class per C-CP-22 §22.2 material-diff reference-class
    table. Closed at cardinality 4."""

    F2_LEDGER_ENTRY = "f2_ledger_entry"
    EXTERNAL_MCP_RESOURCE = "external_mcp_resource"
    FILESYSTEM_STATE = "filesystem_state"
    FAILED_ATTEMPTS_HISTORY = "failed_attempts_history"


class ProposedAction(BaseModel):
    """A proposed action subject to the four-response HITL palette.

    Per C-CP-16 §16.1 (approve/edit/reject of a *proposed action*) + C-CP-17
    §17 three-placement HITL primitive; ADR-D5 §1.1. Exactly three fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_kind: ActionKind
    payload: ActionPayload
    brief: SubAgentBrief | None = None
    """Populated when `action_kind == SUB_AGENT_DISPATCH`."""


class FailedAttempt(BaseModel):
    """A prior sub-agent failure on the same task.

    C-CP-13 §13.1 — `HandoffContext.failed_attempts: List<FailedAttempt>`. The
    `cause` vocabulary joins `retry.cause` per C-CP-03 §3.5 `retry.*`
    namespace. Exactly three fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_index: int
    """Ordinal of this failed attempt."""

    cause: str
    """Failure cause; joins `retry.cause` per C-CP-03 §3.5."""

    attempted_at: str
    """ISO8601 timestamp."""


class Alternative(BaseModel):
    """An alternative the lead agent considered and rejected.

    C-CP-13 §13.1 — `HandoffContext.alternatives_considered: List<Alternative>`
    "lead's deliberation context". Exactly two fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    """The alternative the lead considered."""

    rejected_reason: str
    """Why the lead did not take it."""


class RetryHistory(BaseModel):
    """C9 retry-primitive state for a handoff.

    C-CP-13 §13.1 — `RetryHistory` named in spec ("retry primitives state per
    `retry.*` namespace at C-CP-03 §3.5"). `FailedAttempt` is its constituent.
    §13.4 defers the cardinality cap to implementation discretion. Exactly
    three fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempts: tuple[FailedAttempt, ...]
    retry_count: int
    last_retry_cause: str | None = None
    """Joins `retry.cause` per C-CP-03 §3.5."""


class LedgerEntryRef(BaseModel):
    """A reference to an F2 state-ledger entry (the audit-trail anchor).

    C-CP-13 §13.5 — three fields verbatim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: ActionID
    entry_hash: str
    """The entry's `response_hash` per the F2 entry shape (SHA256 hex-64)."""

    actor: ActorIdentity


class ExternalReference(BaseModel):
    """A pause-time external-state reference anchor.

    The `reference_class` value set is the C-CP-22 §22.2 four-row material-diff
    reference-class table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_class: ReferenceClass
    reference_id: str
    snapshot_capture_at_pause: bytes | None = None


class StateSummary(BaseModel):
    """The across-turn state digest carried in a handoff.

    C-CP-13 §13.4 — committed by name and field-by-field (five fields). The CP
    audit's `CurrentState` concept is this type under a plan spelling (v2.9
    §0.3 `CurrentState` row); the canonical spelling is `StateSummary` — no
    `CurrentState` spelling appears. `external_references` carries the
    pause-time snapshot anchors consumed by U-CP-49."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relevant_entries: tuple[LedgerEntryRef, ...]
    summary_text: str
    summary_hash: str
    """SHA256 hex-64 over the canonicalized summary."""

    idempotency_key: Identifier
    """IS-exported idempotency-key join per C-IS-10 §10.2."""

    external_references: tuple[ExternalReference, ...]
    """Pause-time snapshot anchors per U-CP-49."""


class HandoffContext(BaseModel):
    """The parent->child handoff payload at sub-agent-dispatch cells.

    C-CP-13 §13.1 — seven fields verbatim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposed_action: ProposedAction
    agent_confidence: float | None = None
    failed_attempts: tuple[FailedAttempt, ...]
    alternatives_considered: tuple[Alternative, ...]
    state_summary: StateSummary
    audit_trail_link: LedgerEntryRef
    retry_history: RetryHistory
