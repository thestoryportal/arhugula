"""`SubAgentBrief` schema — U-CP-28.

Implements C-CP-13 §13.2 (brief object structure for orchestrator-workers
cells). Declares `SubAgentBrief`, `OutputSchema`, the `OutputSchemaKind` enum,
`ClearTaskBoundaries`, and the `canonicalize_brief` / `compute_brief_summary_hash`
functions.

C-CP-13 §13.2 commits the 4-field brief object — `objective`, `output_format`,
`guidance`, `task_boundaries`. The plan adds a 5th field, `summary_hash`
(`sha256(canonicalize(brief))`) — a sanctioned plan-internal field (the same
pattern as U-CP-01's `cardinality`, Q-R4-2): it is the join key for the U-CP-27
sub-agent-dispatch audit entry (the `response_hash` source). `OutputSchemaKind`
is promoted to a real `enum` per v2.6 §0.11.4 (was a `// {…}` comment-enum at
v2.1).

The brief is authored at lead-agent inference time and embedded in
`HandoffContext.proposed_action` at orchestrator-workers cells; `canonicalize_brief`
yields a deterministic byte serialization so the summary hash is stable.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.5 U-CP-28 (preserved
verbatim through v2.6 — `OutputSchemaKind` enum promotion only, §0.11.4);
Spec_Control_Plane_v1_2.md §13 C-CP-13 §13.2 (preserved verbatim into v1.3);
ADR-D4 v1.1 §1.7.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class OutputSchemaKind(StrEnum):
    """The kind of output shape a sub-agent must produce (C-CP-13 §13.2).

    Promoted to a real `enum` per v2.6 §0.11.4 (was a comment-enum at v2.1).
    """

    JSON_SCHEMA = "json_schema"
    FREE_TEXT = "free_text"
    STRUCTURED_RECORD = "structured_record"


class OutputSchema(BaseModel):
    """A sub-agent's required output shape (C-CP-13 §13.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_kind: OutputSchemaKind
    schema_body: str | None = None


class ClearTaskBoundaries(BaseModel):
    """Explicit scope-limit declaration — prevents sub-agent scope-creep (§13.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    in_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    termination_criteria: tuple[str, ...]


class SubAgentBrief(BaseModel):
    """A lead-agent-authored sub-agent brief (C-CP-13 §13.2).

    Five fields — the 4 §13.2 fields (`objective`, `output_format`,
    `guidance`, `task_boundaries`) plus the sanctioned plan-internal
    `summary_hash` (the U-CP-27 audit join key).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str
    """Single sentence; bounded scope."""

    output_format: OutputSchema
    guidance: str
    """Approach hints; non-prescriptive."""

    task_boundaries: ClearTaskBoundaries

    summary_hash: str
    """`sha256(canonicalize_brief(brief))` hex digest — plan-internal join key
    for the U-CP-27 sub-agent-dispatch audit entry (`response_hash` source)."""


# The 4 C-CP-13 §13.2 brief fields, excluding the plan-internal summary_hash.
_SPEC_BRIEF_FIELDS: frozenset[str] = frozenset(
    {"objective", "output_format", "guidance", "task_boundaries"}
)


def canonicalize_brief(brief: SubAgentBrief) -> bytes:
    """Deterministically serialize `brief` for hashing.

    Sorted-key JSON over the 4 §13.2 content fields (the `summary_hash` field
    is excluded — it is the hash *of* this canonicalization, so including it
    would be self-referential). Deterministic — equal briefs (modulo
    `summary_hash`) yield equal bytes.
    """
    content = brief.model_dump(mode="json", exclude={"summary_hash"})
    return json.dumps(content, sort_keys=True, separators=(",", ":")).encode()


def compute_brief_summary_hash(brief: SubAgentBrief) -> str:
    """Return `sha256(canonicalize_brief(brief))` as a hex digest (§13.2)."""
    return hashlib.sha256(canonicalize_brief(brief)).hexdigest()
