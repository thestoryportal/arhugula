"""`B-MEMORY-SURFACE-BACKEND-IMPLS` — Fernet content codec for ENCRYPTED_FILESYSTEM.

Authority: C-RT-22, U-RT-80, and H_T-CP-16.

Realizes runtime spec §14.12.3 step 2 `MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM`
("wrap filesystem implementation with per-path encryption per operator-supplied
key reference at `backend_params`") as a `MemoryContentCodec` injected into
`LocalFilesystemMemoryToolBackend` (per §14.12.7 per-backend impl-module +
concurrency-model discretion) rather than an external wrapper.

**Why a codec, not an external wrapper (one-source-of-truth).** The filesystem
backend's per-path `asyncio.Lock` is the single authoritative serialization
point for the `str_replace`/`insert` read-modify-write. An external encrypt-
wrapper would have to DUPLICATE that lock (and re-derive the read-modify-write
as `view`+`create`) to preserve the same atomicity — a second authority for the
same invariant. Codec injection reuses the one lock (CLAUDE.md §4 one
authoritative representation; spec §14.12.2 invariant 3 "backend owns
concurrency discipline"). The spec's "wrap" names the *encryption invariant*
(ciphertext at rest), which this preserves; the wrapping *site* is impl
discretion per §14.12.7.

**Encryption semantics.** "Per-path encryption per operator-supplied key
reference" = per-file content encryption under the SINGLE operator key, NOT
per-path key derivation. Fernet (authenticated AES-128-CBC + HMAC-SHA256) uses
a fresh random IV per message, so identical plaintext at two paths yields
distinct ciphertext without any per-path key machinery.

`cryptography` is imported lazily at the factory construction site
(`memory_tool_registry_factory._create_fernet_from_key`), NOT here — this module
stays import-safe in provider-free environments that never select the encrypted
backend. The codec depends only on the structural `FernetLike` surface.
"""

from __future__ import annotations

from typing import Protocol

from harness_runtime.lifecycle.memory_tool_types import MemoryCallbackIOError

__all__ = [
    "FernetContentCodec",
    "FernetLike",
]


class FernetLike(Protocol):
    """Structural subset of `cryptography.fernet.Fernet` the codec consumes."""

    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, token: bytes) -> bytes: ...


class FernetContentCodec:
    """`MemoryContentCodec` backing `MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM`.

    `encode` encrypts plaintext → ciphertext-at-rest; `decode` authenticates +
    decrypts. A decrypt failure (tampered/truncated ciphertext, or content
    written under a different key) raises `MemoryCallbackIOError`
    (→ `RT-FAIL-MEMORY-CALLBACK-IO`) so it propagates through the backend's
    callback fail-class discipline rather than escaping unmapped — the backend
    itself catches only `OSError`, so the codec owns this mapping.
    """

    def __init__(self, fernet: FernetLike) -> None:
        self._fernet = fernet

    def encode(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decode(self, stored: bytes) -> bytes:
        try:
            return self._fernet.decrypt(stored)
        except Exception as exc:
            # cryptography.fernet.InvalidToken (+ malformed-input TypeError):
            # authentication/decrypt failure. Caught broadly because the codec
            # must not depend on a top-level `cryptography` import (see module
            # docstring); the only operation here is `decrypt`, whose failures
            # all mean "stored content cannot be recovered". The key is never
            # in scope here, so no secret can leak into the message (inv 4).
            raise MemoryCallbackIOError(
                "encrypted-filesystem decode failed: stored content is not "
                "valid ciphertext for the configured key (tampered, truncated, "
                "or written under a different key)"
            ) from exc
