"""Per-sibling F2 ledger entry composition + F2-14 Reading 1 rationale — U-CP-34.

Implements C-CP-15 §15.1 (per-sibling F2 ledger entry composition) and §15.3
(the F2-14 Reading 1 missing-F2-fields rationale).

Per-sibling tool calls produce ledger entries keyed on the sibling's
`thread_id`, honoring F2's six-field entry shape (C-IS-05). This unit composes
the §15.1 `action_id` / `idempotency_key` construction and delegates the
canonicalize+hash to U-IS-08, chain construction to U-IS-09, and the
append-only C3-pole write to U-IS-11 (`append_ledger_entry`).

Declares:
  - `SiblingLedgerEntry` — the F2 six-field entry (subclasses the IS-exported
    `StateLedgerEntry`; acc #1 — matches the F2 six-field shape verbatim).
  - `construct_sibling_ledger_entry` — builds the §15.1 `EntryPayload`
    (caller-supplied content; `response_hash` / `prior_event_hash` are computed
    inside U-IS-11 per the C-IS-07 §7.1 write contract).
  - `append_sibling_ledger_entry` — delegates to U-IS-11's `append_ledger_entry`.
  - `F2_14_Reading_1_Rationale` + `F2_14_READING_1_RATIONALE` — the §15.3
    3-entry missing-F2-fields rationale (one per omitted F2 field at
    `parent_fanout_close_entry`).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.5 U-CP-34;
Spec_Control_Plane_v1_2.md §15 C-CP-15 §15.1 + §15.3 (preserved verbatim into
v1.3); ADR-D4 v1.1 §1.10; Spec_Information_Substrate_v1.md C-IS-05 + C-IS-07.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import (
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)
from harness_is.state_ledger_write import (
    EntryPayload,
    WriteKey,
    WriteResult,
    append_ledger_entry,
)
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import ActorIdentity


class SiblingLedgerEntry(StateLedgerEntry):
    """A per-sibling F2 ledger entry (C-CP-15 §15.1).

    Subclasses the IS-exported `StateLedgerEntry` — it inherits the F2 six-field
    shape verbatim (`action_id`, `idempotency_key`, `actor`, `response_hash`,
    `timestamp`, `prior_event_hash`) and adds no field (acceptance #1)."""


class F2_14_Reading_1_Rationale(BaseModel):  # noqa: N801 — class name encodes the F2-14 Reading 1 citation ID
    """One §15.3 missing-F2-field rationale row (F2-14 Reading 1 closure)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primitive_name: str
    omitted_field: str
    omission_rationale: str


# --- §15.3 F2-14 Reading 1 rationale (3 omitted F2 fields) ------------------

F2_14_READING_1_RATIONALE: tuple[F2_14_Reading_1_Rationale, ...] = (
    F2_14_Reading_1_Rationale(
        primitive_name="parent_fanout_close_entry",
        omitted_field="idempotency_key",
        omission_rationale=(
            "F2's idempotency_key is per-action; fanout-close is per-topology, "
            "not per-action. The fanout-close primitive sits at topology "
            "boundary, not action boundary."
        ),
    ),
    F2_14_Reading_1_Rationale(
        primitive_name="parent_fanout_close_entry",
        omitted_field="actor",
        omission_rationale=(
            "The fanout-close writer is structurally the orchestrator agent; "
            "the topology context already disambiguates the writer, so actor "
            "would be redundant."
        ),
    ),
    F2_14_Reading_1_Rationale(
        primitive_name="parent_fanout_close_entry",
        omitted_field="response_hash",
        omission_rationale=(
            "A fanout aggregate has no single response; the response IS the "
            "merkle-root over siblings, carried in the fanout-specific field "
            "sibling_ledger_root."
        ),
    ),
)
"""The 3 §15.3 F2-14 Reading 1 rationale rows — one per omitted F2 field at
`parent_fanout_close_entry`."""


def _sibling_idempotency_key(
    parent_action_id: str,
    sibling_thread_id: str,
    step_index: int,
    tool: str,
    canonical_args: str,
) -> str:
    """Stripe-style sibling idempotency key (C-CP-15 §15.1; C-IS-10 §10.2).

    `sha256(parent_action_id, sibling_thread_id, step_index, tool,
    canonical_args)`."""
    joined = "\x1f".join(
        (parent_action_id, sibling_thread_id, str(step_index), tool, canonical_args)
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def construct_sibling_ledger_entry(
    parent_action_id: str,
    sibling_thread_id: str,
    step_index: int,
    tool: str,
    canonical_args: str,
    sibling_agent_identity: ActorIdentity,
    timestamp: datetime,
    procedural_tier_snapshot_ref: Identifier | None = None,
) -> EntryPayload:
    """Compose the §15.1 per-sibling ledger entry caller-content.

    `action_id` is the structural concatenation
    `ParentActionID || sibling_thread_id || step_index` (§15.1).
    `idempotency_key` is the Stripe-style sha256 over the 5-tuple (§15.1).
    `actor` is the F2 `Actor` shape — `actor_class = SUB_AGENT`, `actor_id`
    carries the sibling agent identity.
    `response_hash` / `prior_event_hash` are computed inside U-IS-11's
    `append_ledger_entry` per the C-IS-07 §7.1 write contract (acc #2/#3) — so
    the caller-content is an `EntryPayload`.

    `procedural_tier_snapshot_ref` (R-003 producer-site lift) is the
    D-derivative sidecar per IS spec v1.3 §C-IS-05 §5.1. This is a workflow-
    context emission, so the caller (`RuntimeCpIsWiring.emit_sibling_ledger_entry`)
    supplies the resolved value from its bound resolver closure. The bare CP
    helper takes it as a param (default `None` for the outside-workflow /
    test paths) — the sidecar is producer-supplied, not IS-computed (only
    `response_hash` / `prior_event_hash` are IS-computed)."""
    action_id = f"{parent_action_id}{sibling_thread_id}{step_index}"
    idempotency_key = _sibling_idempotency_key(
        parent_action_id, sibling_thread_id, step_index, tool, canonical_args
    )
    return EntryPayload(
        action_id=Identifier(action_id),
        idempotency_key=Identifier(idempotency_key),
        actor=Actor(actor_class=ActorClass.SUB_AGENT, actor_id=str(sibling_agent_identity)),
        timestamp=timestamp,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
    )


def append_sibling_ledger_entry(
    ledger_handle: JsonlLedgerHandle,
    entry_payload: EntryPayload,
    write_key: WriteKey,
) -> WriteResult:
    """Append a per-sibling ledger entry via U-IS-11's C3-pole append-only write.

    Delegates to U-IS-11 `append_ledger_entry` (C-IS-07 §7.1) — chain
    construction (U-IS-09) + canonicalize/hash (U-IS-08) happen inside that
    contract (acceptance #5)."""
    return append_ledger_entry(ledger_handle, entry_payload, write_key)
