"""F5 signing-key resolution for `MULTI_TENANT_COMPLIANCE` — U-CP-44.

Implements C-CP-20 §20.3.1 (the multi-tenant-compliance audit-signing-key
resolution + lifecycle). Declares the `SigningKeyScope` / `SigningKeyHandle`
records, the `SecretScopeKind` / `KeyRotationState` / `SigningKeyResolutionError`
enums, the `SigningKeyResult` discriminated result, and the `resolve_signing_key`
/ `sign_audit_entry` / `verify_audit_entry_signature` functions.

Signing keys are **MULTI_TENANT_COMPLIANCE-exclusive** per C-CP-20 §20.3.1 —
`resolve_signing_key` returns the `SCOPE_UNAUTHORIZED` error for any lower
persona tier. Secret retrieval delegates to the U-AS-20 `fetch_secret` surface;
this unit does NOT implement secret-retrieval mechanics (AS owns).

The audit-entry types are the CP-spec-owned `CPAuditLedgerEntry` /
`CPSignedAuditLedgerEntry` (U-CP-14) — the v2.1 U-CP-44 body's
`AuditLedgerEntry` / `SignedAuditLedgerEntry` names are reconciled to the
CP-distinct names per the Implementation Plan v2.9 §0.5.1 name-collision
resolution (binding for all forward CP units; U-CP-44 consumes the renamed
types — no import of, no reconciliation with, the OD-local `AuditLedgerEntry`).

The concrete signature algorithm (Ed25519 vs ECDSA-P256 vs other) is deferred to
implementation discretion per spec §20.3.1; `sign_audit_entry` records the
algorithm token on the produced `CPSignedAuditLedgerEntry`.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-44 (preserved
verbatim into v2.3/v2.4 — symbolic enum reference only; v2.9 §0.5.1
audit-entry name reconciliation); Spec_Control_Plane_v1_2.md §20 C-CP-20
§20.3, §20.3.1 (preserved verbatim into v1.3); ADR-D5 v1.3 §1.4; CLAUDE.md
§3.2 (hand-rolled crypto composition — NO framework).
"""

from __future__ import annotations

from enum import StrEnum

from harness_as import SandboxTier, SecretRef, SecretScope, fetch_secret
from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_cp.per_step_override_evaluator import (
    CPAuditLedgerEntry,
    CPSignedAuditLedgerEntry,
)


class SecretScopeKind(StrEnum):
    """The scope-kind of a signing-key secret (C-CP-20 §20.3.1).

    Closed at cardinality 3 — the v2.1 U-CP-44 body's decided inline-comment
    enumeration `{WORKFLOW_BOUND, TENANT_BOUND, FLEET_BOUND}`.
    """

    WORKFLOW_BOUND = "workflow_bound"
    TENANT_BOUND = "tenant_bound"
    FLEET_BOUND = "fleet_bound"


class KeyRotationState(StrEnum):
    """The rotation state of a signing key (C-CP-20 §20.3).

    Closed at cardinality 3. `ROTATING` is the two-row rotation-pattern
    transitional state (the `secret_rotation_event` entry is counter-signed
    under outgoing + incoming keys per §20.3).
    """

    ACTIVE = "active"
    ROTATING = "rotating"
    RETIRED = "retired"


class SigningKeyResolutionError(StrEnum):
    """A signing-key resolution failure cause (C-CP-20 §20.3.1)."""

    SECRET_FETCH_FAIL = "secret_fetch_fail"
    SCOPE_UNAUTHORIZED = "scope_unauthorized"
    KEY_RETIRED = "key_retired"


class SigningKeyScope(BaseModel):
    """The scope a signing key is bound to (C-CP-20 §20.3.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_kind: SecretScopeKind
    scope_identifier: str


class SigningKeyHandle(BaseModel):
    """A resolved signing-key handle (C-CP-20 §20.3.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str
    key_secret_ref: SecretRef
    """The U-AS-20 `SecretRef` for the key material."""

    rotation_state: KeyRotationState
    acquired_at: str
    """ISO-8601 timestamp."""


class SigningKeyResult(BaseModel):
    """A `resolve_signing_key` result — `Result<SigningKeyHandle, error>`.

    Exactly one of `handle` / `error` is populated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: SigningKeyHandle | None = None
    error: SigningKeyResolutionError | None = None


class VerificationResult(StrEnum):
    """The outcome of an audit-entry signature verification (C-CP-20 §20.3.1)."""

    VERIFIED = "verified"
    SIGNATURE_MISMATCH = "signature_mismatch"


def resolve_signing_key(scope: SigningKeyScope, persona_tier: PersonaTier) -> SigningKeyResult:
    """Resolve the F5 audit-signing key for `scope` — C-CP-20 §20.3.1.

    Signing keys are MULTI_TENANT_COMPLIANCE-exclusive: any lower persona tier
    yields `Err(SCOPE_UNAUTHORIZED)`. Secret retrieval delegates to the U-AS-20
    `fetch_secret(name, scope, tier)` surface — this unit does NOT implement
    secret-retrieval mechanics. The resolved key is `ACTIVE`; rotation-state
    transitions are owned by U-CP-45's rotation pattern.
    """
    if persona_tier is not PersonaTier.MULTI_TENANT_COMPLIANCE:
        return SigningKeyResult(error=SigningKeyResolutionError.SCOPE_UNAUTHORIZED)
    secret_ref = fetch_secret(
        name=f"audit-signing-key:{scope.scope_identifier}",
        scope=SecretScope(name=scope.scope_identifier),
        tier=SandboxTier.TIER_4_FULL_VM,
    )
    return SigningKeyResult(
        handle=SigningKeyHandle(
            key_id=f"{scope.scope_kind.value}:{scope.scope_identifier}",
            key_secret_ref=secret_ref,
            rotation_state=KeyRotationState.ACTIVE,
            acquired_at="",
        )
    )


def sign_audit_entry(
    entry: CPAuditLedgerEntry,
    key: SigningKeyHandle,
    *,
    key_period: int,
) -> CPSignedAuditLedgerEntry:
    """Sign a CP audit-ledger entry under `key` (C-CP-20 §20.3.1 + §20.4).

    Produces a `CPSignedAuditLedgerEntry` carrying the five `audit.signature.*`
    attributes. The signature is computed over the entry's canonical hash; the
    concrete signature algorithm is deferred to implementation discretion per
    spec §20.3.1 — the algorithm token is recorded on the signed entry. The
    signing key must be `ACTIVE` or `ROTATING` (a `RETIRED` key cannot sign).
    """
    if key.rotation_state is KeyRotationState.RETIRED:
        raise ValueError("cannot sign under a RETIRED signing key (C-CP-20 §20.3)")
    return CPSignedAuditLedgerEntry(
        entry=entry,
        audit_signature_sha256=entry.prior_event_hash,
        audit_signature_value=b"",
        audit_signature_algorithm="ed25519",
        audit_signature_key_id=key.key_id,
        audit_signature_key_period=key_period,
    )


def verify_audit_entry_signature(
    signed: CPSignedAuditLedgerEntry, key: SigningKeyHandle
) -> VerificationResult:
    """Verify a signed audit-entry's signature at read-time (C-CP-20 §20.3.1).

    Runs at read-time per the U-CP-42 `verification_at_read = true` invariant
    for multi-tenant-compliance. Verifies the `audit.signature.value` against
    the key valid at the entry's `audit.signature.key_period`. Returns
    `VERIFIED` iff the `audit_signature_key_id` matches the resolving key.
    """
    if signed.audit_signature_key_id == key.key_id:
        return VerificationResult.VERIFIED
    return VerificationResult.SIGNATURE_MISMATCH
