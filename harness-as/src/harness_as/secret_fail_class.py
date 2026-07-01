"""Secret-fetch fail-class taxonomy + per-backend breaker key — U-AS-24.

Implements C-AS-07 §7.1 (`secret.fail.class` five-value enum), §7.2 (C-AS-04
orthogonality), §7.3 (per-`(secret_backend, scope)` breaker placement).
Declares `SecretFailClass`, the C5/C9 routing enums, the per-class metadata
table, and the breaker-key constructor.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-24 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §7 C-AS-07; ADR-F5 v1.1 §Decision.

Depends on: U-AS-20 (`SecretScope`). U-AS-03 is the §7.2 sibling
`sandbox.fail.class` taxonomy — orthogonal, composed only at the C-AS-15
span-emission layer; this unit declares its own C5/C9 enums per §7.1.

Naming note: the plan signature names the metadata accessor `fail_class_metadata`;
the AS axis already exports a `fail_class_metadata` for the U-AS-03 sandbox
fail-class taxonomy. To keep the public API unambiguous the accessor is named
`secret_fail_class_metadata` here — benign implementation discretion.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from harness_as.secret_fetch import SecretScope


class SecretFailClass(StrEnum):
    """The 5 secret-fetch fail classes (C-AS-07 §7.1). Identifier strings
    byte-exact snake_case per the §7.1 `secret.fail.class` column."""

    SECRET_UNKNOWN = "secret_unknown"
    SECRET_UNAVAILABLE = "secret_unavailable"
    SECRET_EXPIRED = "secret_expired"
    SECRET_LOCKED = "secret_locked"
    SECRET_REVOKED = "secret_revoked"


class SecretC5FailClass(StrEnum):
    """C5 fail-class mapping for a secret-fetch failure (C-AS-07 §7.1)."""

    PERMANENT_FAIL = "PERMANENT_FAIL"
    TRANSIENT_FAIL = "TRANSIENT_FAIL"
    REFLEXION_RECOVERABLE = "REFLEXION_RECOVERABLE"
    HITL_RECOVERABLE = "HITL_RECOVERABLE"


class SecretC9RetryPosture(StrEnum):
    """C9 retry posture for a secret-fetch failure (C-AS-07 §7.1)."""

    NO_RETRY_ROUTE_TO_HITL = "NO_RETRY_ROUTE_TO_HITL"
    C9_BACKOFF_RETRY_WITH_BACKEND_BREAKER = "C9_BACKOFF_RETRY_WITH_BACKEND_BREAKER"
    REFRESH_AND_RETRY_PRESERVING_IDEMPOTENCY_KEY = "REFRESH_AND_RETRY_PRESERVING_IDEMPOTENCY_KEY"
    WORKLOAD_MODE_AWARE_EPHEMERAL_FAIL_FAST_OR_DURABLE_PAUSE = (
        "WORKLOAD_MODE_AWARE_EPHEMERAL_FAIL_FAST_OR_DURABLE_PAUSE"
    )


class SecretFailClassMetadata(BaseModel):
    """Per-class C5/C9 routing metadata for a secret-fetch fail class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fail_class: SecretFailClass
    c5_classification: SecretC5FailClass
    c9_retry_posture: SecretC9RetryPosture
    orthogonal_to_sandbox: bool
    """Always True — `secret.fail.class` is orthogonal to `sandbox.fail.class`
    (§7.2); the two enums compose only at the C-AS-15 span-emission layer."""


class SecretBackendBreakerKey(BaseModel):
    """Per-`(secret_backend, scope)` breaker key (C-AS-07 §7.3).

    Analog of the ADR-F1 per-`(provider, model)` breaker key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    secret_backend: str
    scope: SecretScope


_NO_HITL = SecretC9RetryPosture.NO_RETRY_ROUTE_TO_HITL
_C9_BREAKER = SecretC9RetryPosture.C9_BACKOFF_RETRY_WITH_BACKEND_BREAKER
_REFRESH = SecretC9RetryPosture.REFRESH_AND_RETRY_PRESERVING_IDEMPOTENCY_KEY
_WORKLOAD = SecretC9RetryPosture.WORKLOAD_MODE_AWARE_EPHEMERAL_FAIL_FAST_OR_DURABLE_PAUSE

# §7.1 table: (C5 classification, C9 retry posture) per fail class.
_METADATA_SPEC: dict[SecretFailClass, tuple[SecretC5FailClass, SecretC9RetryPosture]] = {
    SecretFailClass.SECRET_UNKNOWN: (SecretC5FailClass.PERMANENT_FAIL, _NO_HITL),
    SecretFailClass.SECRET_UNAVAILABLE: (SecretC5FailClass.TRANSIENT_FAIL, _C9_BREAKER),
    SecretFailClass.SECRET_EXPIRED: (
        SecretC5FailClass.REFLEXION_RECOVERABLE,
        _REFRESH,
    ),
    SecretFailClass.SECRET_LOCKED: (SecretC5FailClass.HITL_RECOVERABLE, _WORKLOAD),
    SecretFailClass.SECRET_REVOKED: (SecretC5FailClass.HITL_RECOVERABLE, _WORKLOAD),
}

#: Per-class secret-fetch fail-class routing metadata (C-AS-07 §7.1).
SECRET_FAIL_CLASS_METADATA: Mapping[SecretFailClass, SecretFailClassMetadata] = MappingProxyType(
    {
        fail_class: SecretFailClassMetadata(
            fail_class=fail_class,
            c5_classification=c5,
            c9_retry_posture=c9,
            orthogonal_to_sandbox=True,
        )
        for fail_class, (c5, c9) in _METADATA_SPEC.items()
    }
)


def secret_fail_class_metadata(c: SecretFailClass) -> SecretFailClassMetadata:
    """Return the C5/C9 routing metadata for a secret-fetch fail class (§7.1)."""
    return SECRET_FAIL_CLASS_METADATA[c]


def construct_breaker_key(backend: str, scope: SecretScope) -> SecretBackendBreakerKey:
    """Construct the per-`(secret_backend, scope)` breaker key (C-AS-07 §7.3).

    Deterministic — identical `(backend, scope)` inputs produce an equal key.
    """
    return SecretBackendBreakerKey(secret_backend=backend, scope=scope)
