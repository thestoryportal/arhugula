"""Tests for U-CP-11 — `lease.*` namespace + 5-attribute schema (C-CP-05 §5.3).

Acceptance-criterion coverage (CP plan v2.8 §2.2 conformed body):
  #1 5 attributes per §5.3      -> test_lease_namespace_cardinality_five,
                                   test_lease_attributes_byte_exact_with_spec_5_3
  #2 per-attribute value/card   -> test_lease_attribute_value_and_cardinality
  #3 LeaseMechanism cardinality -> test_lease_mechanism_cardinality_six
  #4 LeaseReleaseCause card.    -> test_lease_release_cause_cardinality_four
  #5 LeaseEventKind struck      -> test_no_lease_event_kind_enum
  #6 no spec extension          -> covered by #1-#5 assertions
"""

from __future__ import annotations

import harness_cp.lease_namespace as ln
from harness_core import AttributeValueType, Cardinality
from harness_cp.lease_namespace import (
    LEASE_NAMESPACE_SCHEMA,
    LeaseMechanism,
    LeaseReleaseCause,
)

_SPEC_ATTRIBUTES = (
    "lease.key",
    "lease.holder",
    "lease.ttl_ms",
    "lease.mechanism",
    "lease.release_cause",
)


def test_lease_namespace_cardinality_five() -> None:
    """#1 — exactly five attributes."""
    assert len(LEASE_NAMESPACE_SCHEMA) == 5


def test_lease_attributes_byte_exact_with_spec_5_3() -> None:
    """#1 — attribute names are the §5.3 set verbatim; struck v2.1 tuple absent."""
    names = tuple(a.attribute_name for a in LEASE_NAMESPACE_SCHEMA)
    assert names == _SPEC_ATTRIBUTES
    # The struck v2.1 5-tuple does not reappear.
    for stale in ("lease.id", "lease.acquired_at", "lease.duration_ms", "lease.event_kind"):
        assert stale not in names


def test_lease_attribute_value_and_cardinality() -> None:
    """#2 — per-attribute value_type / cardinality match the §5.3 table."""
    by_name = {a.attribute_name: a for a in LEASE_NAMESPACE_SCHEMA}
    assert by_name["lease.key"].value_type is AttributeValueType.STRING
    assert by_name["lease.key"].cardinality is Cardinality.PER_REQUEST
    assert by_name["lease.holder"].value_type is AttributeValueType.STRING
    assert by_name["lease.holder"].cardinality is Cardinality.MEDIUM
    assert by_name["lease.ttl_ms"].value_type is AttributeValueType.INT
    assert by_name["lease.ttl_ms"].cardinality is Cardinality.HIGH
    assert by_name["lease.mechanism"].value_type is AttributeValueType.ENUM_REF
    assert by_name["lease.mechanism"].cardinality is Cardinality.LOW
    assert by_name["lease.release_cause"].value_type is AttributeValueType.ENUM_REF
    assert by_name["lease.release_cause"].cardinality is Cardinality.LOW


def test_lease_mechanism_cardinality_six() -> None:
    """#3 — LeaseMechanism declares exactly six values."""
    assert len(LeaseMechanism) == 6
    assert {m.value for m in LeaseMechanism} == {
        "engine_native",
        "redis_lease",
        "db_unique_constraint",
        "worktree_isolation",
        "etcd_cas",
        "per_segment",
    }


def test_lease_release_cause_cardinality_four() -> None:
    """#4 — LeaseReleaseCause declares exactly four values."""
    assert len(LeaseReleaseCause) == 4
    assert {m.value for m in LeaseReleaseCause} == {
        "normal",
        "ttl_expiry",
        "holder_loss",
        "lease_revoked",
    }


def test_no_lease_event_kind_enum() -> None:
    """#5 — the invented LeaseEventKind enum does not reappear (regression)."""
    assert not hasattr(ln, "LeaseEventKind")
