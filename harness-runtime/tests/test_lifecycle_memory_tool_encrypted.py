"""`B-MEMORY-SURFACE-BACKEND-IMPLS` — `FernetContentCodec` unit tests.

Spec contract: runtime spec §14.12.3 step 2 ENCRYPTED_FILESYSTEM bullet
(per-path encryption at rest) + §14.12.5 invariant 4 (no secret echoed). The
codec is exercised end-to-end through the filesystem backend in
`test_lifecycle_memory_tool_filesystem.py` and through the registry factory in
`test_u_rt_80_memory_tool_registry_factory.py`; this module unit-tests the codec
against a real `cryptography` Fernet.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from harness_runtime.lifecycle.memory_tool_encrypted import FernetContentCodec
from harness_runtime.lifecycle.memory_tool_types import MemoryCallbackIOError


def _codec() -> FernetContentCodec:
    return FernetContentCodec(Fernet(Fernet.generate_key()))


def test_encode_decode_round_trips() -> None:
    codec = _codec()
    plaintext = b"hello memory store"
    assert codec.decode(codec.encode(plaintext)) == plaintext


def test_encode_produces_ciphertext_not_plaintext() -> None:
    """At-rest bytes are ciphertext — the plaintext does not appear verbatim."""
    codec = _codec()
    plaintext = b"super secret note"
    ciphertext = codec.encode(plaintext)
    assert ciphertext != plaintext
    assert plaintext not in ciphertext


def test_encode_is_nondeterministic_per_message_iv() -> None:
    """Fernet uses a fresh random IV per message — identical plaintext yields
    distinct ciphertext (the basis for 'per-path encryption' without per-path
    key derivation)."""
    codec = _codec()
    plaintext = b"same content twice"
    assert codec.encode(plaintext) != codec.encode(plaintext)


def test_decode_of_garbage_raises_callback_io_error() -> None:
    codec = _codec()
    with pytest.raises(MemoryCallbackIOError, match="not valid ciphertext"):
        codec.decode(b"this is not a fernet token")


def test_decode_under_wrong_key_raises_callback_io_error() -> None:
    """Content encrypted under one key cannot be decoded under another."""
    writer = FernetContentCodec(Fernet(Fernet.generate_key()))
    reader = FernetContentCodec(Fernet(Fernet.generate_key()))
    token = writer.encode(b"payload")
    with pytest.raises(MemoryCallbackIOError):
        reader.decode(token)


def test_decode_error_does_not_echo_key_or_ciphertext() -> None:
    """inv 4 — the decode failure message carries no secret material."""
    key = Fernet.generate_key()
    codec = FernetContentCodec(Fernet(key))
    token = codec.encode(b"sensitive")
    # Tamper the token so decode fails.
    tampered = token[:-4] + b"AAAA"
    with pytest.raises(MemoryCallbackIOError) as excinfo:
        codec.decode(tampered)
    msg = str(excinfo.value)
    assert key.decode("ascii") not in msg
    assert token.decode("ascii") not in msg
