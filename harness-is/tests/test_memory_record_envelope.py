"""Tests for U-MEM-01 - memory vocabulary and record envelope schema."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError


def _memory_module():
    return importlib.import_module("harness_is.memory_record_envelope")


def _digest(byte: int = 0x11) -> bytes:
    return bytes([byte]) * 32


def _valid_envelope_payload() -> dict[str, object]:
    m = _memory_module()
    content_hash = _digest()
    return {
        "memory_id": m.derive_memory_id(
            m.MemoryTier.SEMANTIC,
            m.MemoryRecordKind.SEMANTIC_FACT,
            content_hash,
        ),
        "schema_version": "memory-record-envelope/v1",
        "tier": m.MemoryTier.SEMANTIC,
        "kind": m.MemoryRecordKind.SEMANTIC_FACT,
        "created_at": datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        "source_refs": [
            m.SourceRef(
                ref_type=m.SourceRefType.OPERATOR,
                ref="operator:approved-memory-gap-plan",
                content_hash=None,
            )
        ],
        "scope": m.MemoryScope(project="arhugula-v2", visibility=m.MemoryVisibility.PROJECT),
        "content_hash": content_hash,
    }


def test_memory_vocabulary_matches_spec_values() -> None:
    """U-MEM-01 acceptance - tier and kind vocabularies reject illegal values."""
    m = _memory_module()

    assert {tier.value for tier in m.MemoryTier} == {
        "working",
        "episodic",
        "semantic",
        "procedural",
        "durable",
    }
    assert {kind.value for kind in m.MemoryRecordKind} == {
        "episodic_run",
        "episodic_turn",
        "tool_event",
        "compaction_event",
        "semantic_fact",
        "preference",
        "decision",
        "convention",
        "failure_learning",
        "research",
        "procedural_snapshot",
        "memory_operation",
    }


def test_memory_record_envelope_declares_common_identity_fields() -> None:
    """C-MEM-03 fields, including supersession and redaction, are present."""
    m = _memory_module()

    assert set(m.MemoryRecordEnvelope.model_fields) == {
        "memory_id",
        "schema_version",
        "tier",
        "kind",
        "created_at",
        "updated_at",
        "source_refs",
        "scope",
        "content_hash",
        "supersedes",
        "superseded_by",
        "redaction_state",
    }

    envelope = m.MemoryRecordEnvelope(**_valid_envelope_payload())
    dumped = envelope.model_dump()
    assert dumped["updated_at"] is None
    assert dumped["supersedes"] == ()
    assert dumped["superseded_by"] == ()
    assert dumped["redaction_state"] is m.RedactionState.ACTIVE
    assert isinstance(envelope.source_refs, tuple)
    assert isinstance(envelope.supersedes, tuple)
    assert isinstance(envelope.superseded_by, tuple)


def test_memory_record_envelope_rejects_illegal_tier_and_kind() -> None:
    """U-MEM-01 acceptance - invalid tier and kind strings fail validation."""
    m = _memory_module()
    payload = _valid_envelope_payload()

    with pytest.raises(ValidationError):
        m.MemoryRecordEnvelope.model_validate({**payload, "tier": "cache"})
    with pytest.raises(ValidationError):
        m.MemoryRecordEnvelope.model_validate({**payload, "kind": "freeform_note"})


def test_source_ref_and_envelope_hashes_are_sha256_sized() -> None:
    """C-MEM-03 digest fields reject non-SHA-256 byte lengths."""
    m = _memory_module()

    with pytest.raises(ValidationError):
        m.SourceRef(ref_type=m.SourceRefType.FILE, ref="README.md", content_hash=b"x" * 31)
    with pytest.raises(ValidationError):
        m.MemoryRecordEnvelope(**{**_valid_envelope_payload(), "content_hash": b"x" * 31})


def test_content_hash_is_deterministic_for_equivalent_content() -> None:
    """U-MEM-01 acceptance - equivalent record content hashes identically."""
    m = _memory_module()
    nfc = "café-memory"
    nfd = "cafe\u0301-memory"

    first = {
        "statement": nfc,
        "confidence": "high",
        "tags": ["memory", "routing"],
    }
    second = {
        "tags": ["memory", "routing"],
        "confidence": "high",
        "statement": nfd,
    }

    assert m.compute_memory_content_hash(first) == m.compute_memory_content_hash(second)


def test_content_hash_golden_vector_pins_canonical_bytes() -> None:
    """U-MEM-01 acceptance - content-addressed identity has a golden vector."""
    m = _memory_module()
    content = {
        "statement": "provider-neutral memory",
        "confidence": "verified",
        "tags": ["routing", "memory"],
    }

    expected = (
        b'{"confidence":"verified","statement":"provider-neutral memory",'
        b'"tags":["routing","memory"]}'
    )
    assert m.canonicalize_memory_content(content) == expected
    assert (
        m.compute_memory_content_hash(content).hex()
        == "c76c0da194f3e1ae47060b75e72df11c14f07e5e07c6645d6f5faa1c6abe863f"
    )


def test_content_hash_excludes_derived_indexes() -> None:
    """C-MEM-03 invariant - derived indexes do not change canonical content hash."""
    m = _memory_module()
    canonical = {"statement": "provider-neutral memory", "confidence": "verified"}
    indexed = {
        "statement": "provider-neutral memory",
        "confidence": "verified",
        "derived_indexes": {"embedding": [0.1, 0.2, 0.3]},
    }

    assert m.compute_memory_content_hash(canonical) == m.compute_memory_content_hash(indexed)


def test_nested_derived_indexes_are_canonical_content() -> None:
    """Only top-level derived index material is excluded from the content hash."""
    m = _memory_module()
    with_nested_content = {
        "statement": "nested derived_indexes can be real content",
        "metadata": {"derived_indexes": "literal field name from imported content"},
    }
    without_nested_content = {
        "statement": "nested derived_indexes can be real content",
        "metadata": {},
    }

    assert m.compute_memory_content_hash(with_nested_content) != m.compute_memory_content_hash(
        without_nested_content
    )


def test_canonicalize_memory_content_accepts_timestamp_and_bytes() -> None:
    """Supported non-JSON primitives have pinned canonical representations."""
    m = _memory_module()
    content = {
        "when": datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        "digest": b"\xab" * 32,
    }

    assert m.canonicalize_memory_content(content) == (
        b'{"digest":"' + b"ab" * 32 + b'","when":"2026-07-01T12:00:00+00:00"}'
    )


def test_derive_memory_id_is_stable_for_tier_kind_and_hash() -> None:
    """U-MEM-01 acceptance - stable record identity helper is deterministic."""
    m = _memory_module()
    digest = _digest(0x22)

    first = m.derive_memory_id(m.MemoryTier.SEMANTIC, m.MemoryRecordKind.SEMANTIC_FACT, digest)
    second = m.derive_memory_id(m.MemoryTier.SEMANTIC, m.MemoryRecordKind.SEMANTIC_FACT, digest)
    changed_kind = m.derive_memory_id(m.MemoryTier.SEMANTIC, m.MemoryRecordKind.DECISION, digest)

    assert first == second
    assert first != changed_kind
    assert first == f"mem:semantic:semantic_fact:{digest.hex()}"


def test_derive_memory_id_rejects_non_sha256_digest() -> None:
    """U-MEM-01 acceptance - memory IDs cannot be derived from invalid digests."""
    m = _memory_module()

    with pytest.raises(ValueError, match="exactly 32 bytes"):
        m.derive_memory_id(m.MemoryTier.SEMANTIC, m.MemoryRecordKind.SEMANTIC_FACT, b"x" * 31)


def test_memory_envelope_package_re_exports() -> None:
    """The package-level IS API exposes the U-MEM-01 schema and helpers."""
    h_is = importlib.import_module("harness_is")
    m = _memory_module()

    assert h_is.MemoryRecordEnvelope is m.MemoryRecordEnvelope
    assert h_is.compute_memory_content_hash is m.compute_memory_content_hash
