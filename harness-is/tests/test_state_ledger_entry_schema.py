"""Tests for U-IS-07 — state-ledger entry shape schema (C-IS-05 §5).

Test set per the U-IS-07 `Tests:` field — covers acceptance #1-#6.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)
from pydantic import ValidationError

_F_LAYER_FIELDS = {
    "action_id",
    "idempotency_key",
    "actor",
    "response_hash",
    "timestamp",
    "prior_event_hash",
}

# NEW D-derivative sidecars — additive at the extensibility layer authorized
# by §5 "Field-shape extensibility commitment." The F-layer six-field shape is
# PRESERVED VERBATIM; the sidecars live at the D-derivative layer.
#   - `procedural_tier_snapshot_ref` (C-IS-05 §5.1, v1.3)
#   - `branch_metadata` (C-IS-05 §5.4, v1.8 — U-IS-19 branch-causality carrier)
_D_DERIVATIVE_FIELDS = {
    "procedural_tier_snapshot_ref",
    "branch_metadata",
}
_CURRENT_FIELDS = _F_LAYER_FIELDS | _D_DERIVATIVE_FIELDS


def _entry(action_id: str = "00000000-0000-4000-8000-000000000000") -> StateLedgerEntry:
    return StateLedgerEntry(
        action_id=Identifier(action_id),
        idempotency_key=Identifier("idem-1"),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="agent-1"),
        response_hash=b"\x11" * 32,
        timestamp=datetime(2026, 5, 15, tzinfo=UTC),
        prior_event_hash=ALL_ZEROS_SENTINEL,
    )


def test_state_ledger_entry_schema_completeness() -> None:
    """Acceptance #1 — StateLedgerEntry declares the 6 F-layer fields per §5
    plus the D-derivative sidecar fields (§5.1 `procedural_tier_snapshot_ref`
    at v1.3; §5.4 `branch_metadata` at v1.8).

    The F-layer six-field shape is preserved verbatim; the sidecars are
    additive at the extensibility layer authorized by §5.
    """
    assert set(StateLedgerEntry.model_fields) == _CURRENT_FIELDS
    # F-layer subset invariant — the 6 §5 fields remain unchanged.
    assert _F_LAYER_FIELDS.issubset(set(StateLedgerEntry.model_fields))


def test_actor_class_enum_completeness() -> None:
    """Acceptance #2 — ActorClass declares exactly 3 values."""
    assert len(ActorClass) == 3
    assert {a.name for a in ActorClass} == {"AGENT", "SUB_AGENT", "OPERATOR"}


def test_all_zeros_sentinel_value() -> None:
    """Acceptance #3 — ALL_ZEROS_SENTINEL is 32 bytes of zero."""
    assert ALL_ZEROS_SENTINEL == b"\x00" * 32
    assert len(ALL_ZEROS_SENTINEL) == 32


def test_extensibility_commitment_permits_additive_fields() -> None:
    """Acceptance #5 — a per-workload-class extension record includes the six
    F-layer fields and MAY add fields."""

    class ExtendedEntry(StateLedgerEntry):
        workload_class: str

    extended = ExtendedEntry(
        action_id=Identifier("a-1"),
        idempotency_key=Identifier("idem-1"),
        actor=Actor(actor_class=ActorClass.SUB_AGENT, actor_id="sub-1"),
        response_hash=b"\x22" * 32,
        timestamp=datetime(2026, 5, 15, tzinfo=UTC),
        prior_event_hash=ALL_ZEROS_SENTINEL,
        workload_class="software-engineering",
    )
    assert set(_F_LAYER_FIELDS).issubset(set(type(extended).model_fields))
    assert extended.workload_class == "software-engineering"


def test_extensibility_commitment_prohibits_f_layer_modification() -> None:
    """Acceptance #5 — omitting an F-layer field, or supplying it under a
    different name (rejected by `extra='forbid'`), fails validation."""
    with pytest.raises(ValidationError):
        StateLedgerEntry(  # type: ignore[call-arg]  # action_id omitted
            idempotency_key=Identifier("idem-1"),
            actor=Actor(actor_class=ActorClass.AGENT, actor_id="agent-1"),
            response_hash=b"\x11" * 32,
            timestamp=datetime(2026, 5, 15, tzinfo=UTC),
            prior_event_hash=ALL_ZEROS_SENTINEL,
        )
    with pytest.raises(ValidationError):
        StateLedgerEntry.model_validate(
            {
                "action_id_renamed": "a-1",
                "idempotency_key": "idem-1",
                "actor": {"actor_class": "agent", "actor_id": "agent-1"},
                "response_hash": b"\x11" * 32,
                "timestamp": datetime(2026, 5, 15, tzinfo=UTC),
                "prior_event_hash": ALL_ZEROS_SENTINEL,
            }
        )


def test_identifier_type_binding_flex() -> None:
    """Acceptance #4 — Identifier binding is format-flexible: a UUID v4 string
    and a ULID string both validate per the §5 deferral."""
    uuid_v4 = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert _entry(action_id=uuid_v4).action_id == uuid_v4
    assert _entry(action_id=ulid).action_id == ulid


def test_response_hash_must_be_32_bytes() -> None:
    """Acceptance #6 — Bytes32 fields reject a non-32-byte sequence."""
    with pytest.raises(ValidationError):
        StateLedgerEntry(
            action_id=Identifier("a-1"),
            idempotency_key=Identifier("idem-1"),
            actor=Actor(actor_class=ActorClass.AGENT, actor_id="agent-1"),
            response_hash=b"\x11" * 31,
            timestamp=datetime(2026, 5, 15, tzinfo=UTC),
            prior_event_hash=ALL_ZEROS_SENTINEL,
        )
