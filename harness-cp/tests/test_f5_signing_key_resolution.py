"""Tests for U-CP-44 — F5 signing-key resolution (C-CP-20 §20.3.1).

Acceptance-criterion coverage:
  #1 SCOPE_UNAUTHORIZED below MTC -> test_resolve_signing_key_scope_unauthorized_below_multi_tenant
  #2 delegates to U-AS-20         -> test_resolve_delegates_to_u_as_20
  #3 rotation_state 3 values      -> test_signing_key_rotation_state_three_values
  #4 sign produces signed entry   -> test_sign_produces_signed_entry
  #5 verify at read               -> test_verify_at_read
  #6 signature algorithm deferred -> test_signature_algorithm_recorded
"""

from __future__ import annotations

import pytest
from harness_as import GateLevel, SecretRef
from harness_core import PersonaTier
from harness_cp.f5_signing_key_resolution import (
    KeyRotationState,
    SecretScopeKind,
    SigningKeyHandle,
    SigningKeyResolutionError,
    SigningKeyScope,
    VerificationResult,
    resolve_signing_key,
    sign_audit_entry,
    verify_audit_entry_signature,
)
from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry

_SCOPE = SigningKeyScope(scope_kind=SecretScopeKind.TENANT_BOUND, scope_identifier="tenant-7")
_ENTRY = CPAuditLedgerEntry(
    action_id="wf||s1",  # type: ignore[arg-type]
    gate_level=GateLevel.AUTO,
    response="approve",
    timestamp="2026-05-16T00:00:00Z",
    prior_event_hash="a" * 64,
)


def test_resolve_signing_key_scope_unauthorized_below_multi_tenant() -> None:
    for tier in (PersonaTier.SOLO_DEVELOPER, PersonaTier.TEAM_BINDING):
        result = resolve_signing_key(_SCOPE, tier)
        assert result.handle is None
        assert result.error is SigningKeyResolutionError.SCOPE_UNAUTHORIZED


def test_resolve_delegates_to_u_as_20() -> None:
    result = resolve_signing_key(_SCOPE, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert result.error is None
    assert result.handle is not None
    # The key material is carried as a U-AS-20 SecretRef — secret-retrieval
    # mechanics are delegated to harness-as (this unit does not implement them).
    assert isinstance(result.handle.key_secret_ref, SecretRef)


def test_signing_key_rotation_state_three_values() -> None:
    assert len(list(KeyRotationState)) == 3
    assert {s.value for s in KeyRotationState} == {"active", "rotating", "retired"}


def test_sign_produces_signed_entry() -> None:
    result = resolve_signing_key(_SCOPE, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert result.handle is not None
    signed = sign_audit_entry(_ENTRY, result.handle, key_period=3)
    assert signed.entry == _ENTRY
    assert signed.audit_signature_key_period == 3
    assert signed.audit_signature_key_id == result.handle.key_id


def test_sign_rejects_retired_key() -> None:
    retired = SigningKeyHandle(
        key_id="k0",
        key_secret_ref=SecretRef.model_construct(),
        rotation_state=KeyRotationState.RETIRED,
        acquired_at="",
    )
    with pytest.raises(ValueError, match="RETIRED"):
        sign_audit_entry(_ENTRY, retired, key_period=1)


def test_verify_at_read() -> None:
    result = resolve_signing_key(_SCOPE, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert result.handle is not None
    signed = sign_audit_entry(_ENTRY, result.handle, key_period=1)
    assert verify_audit_entry_signature(signed, result.handle) is VerificationResult.VERIFIED


def test_signature_algorithm_recorded() -> None:
    result = resolve_signing_key(_SCOPE, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert result.handle is not None
    signed = sign_audit_entry(_ENTRY, result.handle, key_period=1)
    # The concrete algorithm is deferred to implementation discretion per
    # §20.3.1; the algorithm token is recorded on the signed entry.
    assert signed.audit_signature_algorithm in {
        "ed25519",
        "ecdsa-p256",
        "rsa-pss-2048",
    }
