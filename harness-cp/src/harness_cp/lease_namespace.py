"""`lease.*` span-attribute namespace + 5-attribute schema — U-CP-11.

Implements C-CP-05 §5.3 (the `lease.*` span-attribute namespace declared per
the five-attribute table). Declares `LeaseAttributeSchema`, the 5-entry
`LEASE_NAMESPACE_SCHEMA`, the `LeaseMechanism` 6-value enum, and the
`LeaseReleaseCause` 4-value enum.

**v2.8 conformance.** The v2.1 unit body named a 5-tuple `LEASE_NAMESPACE_SCHEMA`
(`lease.id`, `lease.holder`, `lease.acquired_at`, `lease.duration_ms`,
`lease.event_kind`) + a `LeaseEventKind` discriminator enum — both contradicted
the cited spec C-CP-05 §5.3, which declares the attributes `lease.key`,
`lease.holder`, `lease.ttl_ms`, `lease.mechanism`, `lease.release_cause`. CP
plan v2.8 §2.2 conforms the body to spec §5.3 verbatim per the §4A
conform-to-spec resolution; the invented `LeaseEventKind` enum is struck and
the spec §5.3 `lease.mechanism` 6-value enum + `lease.release_cause` 4-value
enum are declared as `LeaseMechanism` / `LeaseReleaseCause` here
(lease-specific — not shared, so not homed at U-CP-00c).

**Cardinality mapping.** The spec §5.3 cardinality column tokens
(`per-active-lease`, `medium`, `unbounded (metric)`, `bounded (6)`,
`bounded (4)`) are rendered onto the shared `Cardinality` enum
(`LOW`/`MEDIUM`/`HIGH`/`PER_REQUEST`) per the landed precedent at
`engine_namespace.py` (bounded enum -> `Cardinality.LOW`; per-event ->
`Cardinality.PER_REQUEST`; unbounded metric -> `Cardinality.HIGH`).

Authority: Implementation_Plan_Control_Plane_v2_8.md §2.2 U-CP-11 (v2.8
conformed body); Spec_Control_Plane_v1_2.md §5 C-CP-05 §5.3 (preserved verbatim
into v1.3); `.harness/class_1_tension_u_cp_11_lease_namespace_schema.md`
(conformance resolution).
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict


class LeaseMechanism(StrEnum):
    """The 6 lease mechanisms (C-CP-05 §5.3 `lease.mechanism` domain).

    Closed at cardinality 6. Member string values are the §5.3
    `lease.mechanism` enum-string domain verbatim."""

    ENGINE_NATIVE = "engine_native"
    REDIS_LEASE = "redis_lease"
    DB_UNIQUE_CONSTRAINT = "db_unique_constraint"
    WORKTREE_ISOLATION = "worktree_isolation"
    ETCD_CAS = "etcd_cas"
    PER_SEGMENT = "per_segment"


class LeaseReleaseCause(StrEnum):
    """The 4 lease release causes (C-CP-05 §5.3 `lease.release_cause` domain).

    Closed at cardinality 4. Member string values are the §5.3
    `lease.release_cause` enum-string domain verbatim."""

    NORMAL = "normal"
    TTL_EXPIRY = "ttl_expiry"
    HOLDER_LOSS = "holder_loss"
    LEASE_REVOKED = "lease_revoked"


class LeaseAttributeSchema(BaseModel):
    """One `lease.*` span attribute (C-CP-05 §5.3 table row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality


# --- Registry population (C-CP-05 §5.3 5-attribute table) -------------------

LEASE_NAMESPACE_SCHEMA: tuple[LeaseAttributeSchema, ...] = (
    LeaseAttributeSchema(
        attribute_name="lease.key",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.PER_REQUEST,
    ),
    LeaseAttributeSchema(
        attribute_name="lease.holder",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.MEDIUM,
    ),
    LeaseAttributeSchema(
        attribute_name="lease.ttl_ms",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.HIGH,
    ),
    LeaseAttributeSchema(
        attribute_name="lease.mechanism",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
    ),
    LeaseAttributeSchema(
        attribute_name="lease.release_cause",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
    ),
)
"""The 5 `lease.*` attributes per C-CP-05 §5.3 verbatim — `lease.key`,
`lease.holder`, `lease.ttl_ms`, `lease.mechanism`, `lease.release_cause`."""
