"""Secret-fetch `outputs_hash` structure-not-content fingerprint — U-AS-25.

Implements C-AS-08 §8.1 (the `outputs_hash` formula). Declares
`canonicalize_concat_secret_fingerprint` and `compute_outputs_hash` — the
structure-not-content fingerprint of a secret-fetch event.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-25 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §8.1 C-AS-08; ADR-F5 v1.1 §Decision.

Depends on: U-AS-20 (`SecretScope`); U-IS-08 (cross-axis: IS — C-IS-06 §6.1
canonicalization).

GUARDRAIL unit (Plan_Executability_Audit_v1.md §3.2) — JCS canonicalization
carry-forward from U-IS-08. AC2's "delegates to U-IS-08's `canonicalize`" is
interpreted as **scheme-level inheritance**: spec §8.1 frames `canonicalize_concat`
as "the canonicalization function per C-IS-06 §6.1 ... library binding deferred",
and the GUARDRAIL carry-forward inherits U-IS-08's *binding decision*. U-IS-08's
exported `canonicalize` is `StateLedgerEntry`-typed — not a reusable cross-axis
primitive — so this unit re-applies the C-IS-06 §6.1 scheme (NFC Unicode
normalization + sorted-key JSON, no whitespace, UTF-8) to the secret-fingerprint
triple. The scheme is the inherited binding decision, hand-rolled on the stdlib
per framework-pull discipline (I-6). The entry-typed-vs-reusable seam mismatch
is a Class 3 cross-axis observation (a shared canonicalization primitive may be
factored at 7c).

The secret **value** never enters this function — the three inputs
(`secret_name`, `secret_scope`, `secret_last_rotated_at`) capture structure
only (acceptance #3).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from harness_as.secret_fetch import SecretScope


def _nfc(value: str) -> str:
    """NFC-normalize a string (RFC 8785 JCS Unicode normalization, C-IS-06 §6.1)."""
    return unicodedata.normalize("NFC", value)


def canonicalize_concat_secret_fingerprint(
    secret_name: str,
    secret_scope: SecretScope,
    secret_last_rotated_at: str,
) -> bytes:
    """Canonicalize the secret-fetch fingerprint triple to deterministic bytes.

    Applies the C-IS-06 §6.1 canonicalization scheme (the binding decision
    inherited from U-IS-08): NFC Unicode normalization of every string value,
    then `json.dumps` with sorted keys and no whitespace, UTF-8 encoded.
    Deterministic — byte-identical output for logically-equal triples across
    runs. `secret_last_rotated_at` is an ISO-8601 timestamp string (a version
    attribute — structure, not the secret value).
    """
    payload: dict[str, object] = {
        "secret_name": _nfc(secret_name),
        "secret_scope": {"name": _nfc(secret_scope.name)},
        "secret_last_rotated_at": _nfc(secret_last_rotated_at),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_outputs_hash(
    secret_name: str,
    secret_scope: SecretScope,
    secret_last_rotated_at: str,
) -> bytes:
    """Compute the secret-fetch `outputs_hash` structure-not-content fingerprint.

    Per C-AS-08 §8.1: `outputs_hash = SHA-256(canonicalize_concat(secret.name,
    secret.scope, secret.last_rotated_at))`. Returns exactly 32 bytes
    (acceptance #1). The secret value is never an input (acceptance #3).
    Consumed by U-AS-26 to populate the audit-ledger `response_hash` (§8.2).
    """
    return hashlib.sha256(
        canonicalize_concat_secret_fingerprint(secret_name, secret_scope, secret_last_rotated_at)
    ).digest()
