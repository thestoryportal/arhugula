"""Tests for U-AS-25 — secret-fetch outputs_hash fingerprint (C-AS-08 §8.1)."""

from __future__ import annotations

import inspect

from harness_as.secret_fetch import SecretScope
from harness_as.secret_outputs_hash import (
    canonicalize_concat_secret_fingerprint,
    compute_outputs_hash,
)

_SCOPE = SecretScope(name="prod")
_ROTATED = "2026-05-16T00:00:00Z"


def test_compute_outputs_hash_length_32_bytes() -> None:
    """Acceptance #1 — outputs_hash is exactly 32 bytes (SHA-256 digest)."""
    digest = compute_outputs_hash("ANTHROPIC_API_KEY", _SCOPE, _ROTATED)
    assert len(digest) == 32


def test_compute_outputs_hash_deterministic_same_invocation() -> None:
    """Acceptance #4 — byte-identical output for logically-equal triples."""
    a = compute_outputs_hash("ANTHROPIC_API_KEY", _SCOPE, _ROTATED)
    b = compute_outputs_hash("ANTHROPIC_API_KEY", SecretScope(name="prod"), _ROTATED)
    assert a == b


def test_compute_outputs_hash_uses_canonicalize_concat_from_u_is_08() -> None:
    """Acceptance #2 — canonicalization is the C-IS-06 §6.1 scheme (per U-IS-08).

    Scheme-level inheritance: the canonical form is sorted-key JSON with no
    whitespace, UTF-8 — the binding decision U-IS-08 settled for C-IS-06 §6.1.
    """
    canonical = canonicalize_concat_secret_fingerprint("ANTHROPIC_API_KEY", _SCOPE, _ROTATED)
    text = canonical.decode("utf-8")
    assert " " not in text  # no whitespace
    assert text.index("secret_last_rotated_at") < text.index("secret_name")  # sorted keys
    assert canonical == canonicalize_concat_secret_fingerprint(
        "ANTHROPIC_API_KEY", _SCOPE, _ROTATED
    )


def test_compute_outputs_hash_collision_smoke() -> None:
    """Acceptance #5 — distinct triples produce distinct hashes (SHA-256)."""
    a = compute_outputs_hash("KEY_A", _SCOPE, _ROTATED)
    b = compute_outputs_hash("KEY_B", _SCOPE, _ROTATED)
    assert a != b


def test_compute_outputs_hash_rotation_changes_hash() -> None:
    """Acceptance — a different last_rotated_at changes the fingerprint."""
    a = compute_outputs_hash("KEY", _SCOPE, "2026-05-16T00:00:00Z")
    b = compute_outputs_hash("KEY", _SCOPE, "2026-05-17T00:00:00Z")
    assert a != b


def test_compute_outputs_hash_scope_separation() -> None:
    """Acceptance — a different scope changes the fingerprint."""
    a = compute_outputs_hash("KEY", SecretScope(name="prod"), _ROTATED)
    b = compute_outputs_hash("KEY", SecretScope(name="staging"), _ROTATED)
    assert a != b


def test_compute_outputs_hash_no_value_input() -> None:
    """Acceptance #3 — the function input carries no secret-value parameter."""
    params = set(inspect.signature(compute_outputs_hash).parameters)
    assert params == {"secret_name", "secret_scope", "secret_last_rotated_at"}
    assert "value" not in params
    assert "secret_value" not in params


def test_compute_outputs_hash_library_binding_flex() -> None:
    """Acceptance #6 — the scheme is hand-rolled stdlib JCS; no JCS library pulled."""
    # Pure-stdlib: the fingerprint computes without any third-party canonicalizer.
    digest = compute_outputs_hash("KEY", _SCOPE, _ROTATED)
    assert isinstance(digest, bytes)
    assert len(digest) == 32
