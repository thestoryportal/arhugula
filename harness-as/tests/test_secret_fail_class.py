"""Tests for U-AS-24 — secret-fetch fail-class taxonomy + breaker key (C-AS-07 §7)."""

from __future__ import annotations

from harness_as.secret_fail_class import (
    SecretC5FailClass,
    SecretC9RetryPosture,
    SecretFailClass,
    construct_breaker_key,
    secret_fail_class_metadata,
)
from harness_as.secret_fetch import SecretScope

_SCOPE = SecretScope(name="prod")


def test_secret_fail_class_cardinality_five() -> None:
    """Acceptance #1 — SecretFailClass declares exactly five values."""
    assert len(SecretFailClass) == 5


def test_secret_fail_class_identifier_strings_snake_case_byte_exact() -> None:
    """Acceptance #1 — §7.1 `secret.fail.class` identifier strings byte-exact."""
    assert {fc.value for fc in SecretFailClass} == {
        "secret_unknown",
        "secret_unavailable",
        "secret_expired",
        "secret_locked",
        "secret_revoked",
    }


def test_secret_fail_class_metadata_table_per_spec_row_by_row() -> None:
    """Acceptance #2 — per-class C5/C9 metadata matches the §7.1 table."""
    expected = {
        SecretFailClass.SECRET_UNKNOWN: (
            SecretC5FailClass.PERMANENT_FAIL,
            SecretC9RetryPosture.NO_RETRY_ROUTE_TO_HITL,
        ),
        SecretFailClass.SECRET_UNAVAILABLE: (
            SecretC5FailClass.TRANSIENT_FAIL,
            SecretC9RetryPosture.C9_BACKOFF_RETRY_WITH_BACKEND_BREAKER,
        ),
        SecretFailClass.SECRET_EXPIRED: (
            SecretC5FailClass.REFLEXION_RECOVERABLE,
            SecretC9RetryPosture.REFRESH_AND_RETRY_PRESERVING_IDEMPOTENCY_KEY,
        ),
        SecretFailClass.SECRET_LOCKED: (
            SecretC5FailClass.HITL_RECOVERABLE,
            SecretC9RetryPosture.WORKLOAD_MODE_AWARE_EPHEMERAL_FAIL_FAST_OR_DURABLE_PAUSE,
        ),
        SecretFailClass.SECRET_REVOKED: (
            SecretC5FailClass.HITL_RECOVERABLE,
            SecretC9RetryPosture.WORKLOAD_MODE_AWARE_EPHEMERAL_FAIL_FAST_OR_DURABLE_PAUSE,
        ),
    }
    for fail_class, (c5, c9) in expected.items():
        meta = secret_fail_class_metadata(fail_class)
        assert meta.c5_classification is c5
        assert meta.c9_retry_posture is c9


def test_orthogonal_to_sandbox_uniform_true() -> None:
    """Acceptance #3 — orthogonal_to_sandbox is True for every fail class (§7.2)."""
    for fail_class in SecretFailClass:
        assert secret_fail_class_metadata(fail_class).orthogonal_to_sandbox is True


def test_secret_expired_routes_refresh_and_retry() -> None:
    """Acceptance #4 — SECRET_EXPIRED routes refresh-and-retry preserving the key."""
    assert (
        secret_fail_class_metadata(SecretFailClass.SECRET_EXPIRED).c9_retry_posture
        is SecretC9RetryPosture.REFRESH_AND_RETRY_PRESERVING_IDEMPOTENCY_KEY
    )


def test_secret_locked_and_revoked_route_workload_mode_aware() -> None:
    """Acceptance #5 — SECRET_LOCKED + SECRET_REVOKED route workload-mode-aware."""
    for fail_class in (SecretFailClass.SECRET_LOCKED, SecretFailClass.SECRET_REVOKED):
        assert (
            secret_fail_class_metadata(fail_class).c9_retry_posture
            is SecretC9RetryPosture.WORKLOAD_MODE_AWARE_EPHEMERAL_FAIL_FAST_OR_DURABLE_PAUSE
        )


def test_secret_unknown_routes_no_retry_to_hitl() -> None:
    """Acceptance #2 — SECRET_UNKNOWN routes NO_RETRY_ROUTE_TO_HITL."""
    assert (
        secret_fail_class_metadata(SecretFailClass.SECRET_UNKNOWN).c9_retry_posture
        is SecretC9RetryPosture.NO_RETRY_ROUTE_TO_HITL
    )


def test_construct_breaker_key_deterministic() -> None:
    """Acceptance #7 — construct_breaker_key is deterministic for equal inputs."""
    assert construct_breaker_key("vault", _SCOPE) == construct_breaker_key("vault", _SCOPE)


def test_construct_breaker_key_distinct_for_different_backend() -> None:
    """Acceptance #6/#7 — breaker keys differ across secret backends."""
    assert construct_breaker_key("vault", _SCOPE) != construct_breaker_key("keychain", _SCOPE)


def test_construct_breaker_key_distinct_for_different_scope() -> None:
    """Acceptance #6/#7 — breaker keys differ across scopes."""
    assert construct_breaker_key("vault", _SCOPE) != construct_breaker_key(
        "vault", SecretScope(name="staging")
    )
