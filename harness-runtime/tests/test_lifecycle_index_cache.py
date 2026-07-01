"""U-RT-09 — `materialize_index_cache` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L2:
- Index handle returned non-null.
- Existing index reattaches with byte-identical content hash.
- Missing index path fresh-creates idempotently.
- Cache hits observable in tests.
"""

from __future__ import annotations

from pathlib import Path

from harness_runtime.lifecycle.index_cache import (
    ContentAddressedIndex,
    IndexCacheStage,
    SemanticCache,
    materialize_index_cache,
)

# ---------------------------------------------------------------------------
# Index handle returned (plan AC).
# ---------------------------------------------------------------------------


def test_index_handle_returned_non_null(tmp_path: Path) -> None:
    """Materialize against an absent path → non-null index + cache."""
    stage = materialize_index_cache(tmp_path / "index.json")
    assert isinstance(stage, IndexCacheStage)
    assert isinstance(stage.index, ContentAddressedIndex)
    assert isinstance(stage.cache, SemanticCache)


def test_index_handle_returned_when_present(tmp_path: Path) -> None:
    """Materialize against an existing path → handle wraps prior content."""
    index_path = tmp_path / "index.json"
    # Pre-seed via a first materialize + put.
    first = materialize_index_cache(index_path)
    first.index.put("k1", "v1")
    first.index.save()

    # Reattach.
    second = materialize_index_cache(index_path)
    assert second.index.get("k1") == "v1"


# ---------------------------------------------------------------------------
# Existing index reattaches byte-identical (plan AC).
# ---------------------------------------------------------------------------


def test_reattach_byte_identical_content_hash(tmp_path: Path) -> None:
    """Load → save round-trip on disk is byte-identical (canonical form)."""
    index_path = tmp_path / "index.json"
    # First materialize + populate.
    stage = materialize_index_cache(index_path)
    stage.index.put("key-b", "val-b")
    stage.index.put("key-a", "val-a")
    stage.index.put("key-c", "val-c")
    stage.index.save()

    on_disk_before = index_path.read_bytes()
    hash_before = stage.index.content_hash()

    # Reattach + immediately re-save.
    reattached = materialize_index_cache(index_path)
    reattached.index.save()
    on_disk_after = index_path.read_bytes()

    assert on_disk_after == on_disk_before
    assert reattached.index.content_hash() == hash_before


def test_canonical_form_is_sorted_keys(tmp_path: Path) -> None:
    """Save produces keys in sorted order regardless of insert order."""
    index_path = tmp_path / "index.json"
    stage = materialize_index_cache(index_path)
    stage.index.put("z", "z-val")
    stage.index.put("a", "a-val")
    stage.index.put("m", "m-val")
    stage.index.save()

    # Sorted keys → "a" appears before "m" before "z" in the serialized bytes.
    raw = index_path.read_text()
    a_pos = raw.index('"a"')
    m_pos = raw.index('"m"')
    z_pos = raw.index('"z"')
    assert a_pos < m_pos < z_pos


# ---------------------------------------------------------------------------
# Missing index path fresh-creates idempotently (plan AC).
# ---------------------------------------------------------------------------


def test_fresh_create_idempotent(tmp_path: Path) -> None:
    """Two materialize calls against an absent path produce equal empty state."""
    index_path = tmp_path / "index.json"
    first = materialize_index_cache(index_path)
    first_hash = first.index.content_hash()
    # Don't call save again — file already created by `open`.

    second = materialize_index_cache(index_path)
    second_hash = second.index.content_hash()

    assert first_hash == second_hash
    assert len(first.index) == 0
    assert len(second.index) == 0


def test_fresh_create_initializes_canonical_empty(tmp_path: Path) -> None:
    """First materialize writes the canonical empty `{}` to disk."""
    index_path = tmp_path / "index.json"
    materialize_index_cache(index_path)
    assert index_path.exists()
    assert index_path.read_bytes() == b"{}"


def test_fresh_create_parents_created(tmp_path: Path) -> None:
    """Nested directories in the index path are created (`parents=True`)."""
    index_path = tmp_path / "a" / "b" / "c" / "index.json"
    stage = materialize_index_cache(index_path)
    assert stage.index.path.exists()


# ---------------------------------------------------------------------------
# Cache hits observable (plan AC).
# ---------------------------------------------------------------------------


def test_cache_hit_after_put(tmp_path: Path) -> None:
    """Put + get round-trip on the SemanticCache observes the same value."""
    stage = materialize_index_cache(tmp_path / "index.json")
    stage.cache.put("query-1", {"result": "cached"})
    assert stage.cache.get("query-1") == {"result": "cached"}
    assert "query-1" in stage.cache


def test_cache_miss_returns_none(tmp_path: Path) -> None:
    """`get` on a missing key returns None."""
    stage = materialize_index_cache(tmp_path / "index.json")
    assert stage.cache.get("never-seen") is None
    assert "never-seen" not in stage.cache


def test_cache_starts_empty(tmp_path: Path) -> None:
    """A freshly-materialized stage has an empty cache."""
    stage = materialize_index_cache(tmp_path / "index.json")
    assert len(stage.cache) == 0


# ---------------------------------------------------------------------------
# Index put/get + len.
# ---------------------------------------------------------------------------


def test_index_put_get_round_trip(tmp_path: Path) -> None:
    """Index supports put/get key-value round-trip."""
    stage = materialize_index_cache(tmp_path / "index.json")
    stage.index.put("hash:abc", "entry-position:5")
    assert stage.index.get("hash:abc") == "entry-position:5"
    assert len(stage.index) == 1


def test_index_overwrite_existing_key(tmp_path: Path) -> None:
    """Put with an existing key replaces the prior value."""
    stage = materialize_index_cache(tmp_path / "index.json")
    stage.index.put("k", "v1")
    stage.index.put("k", "v2")
    assert stage.index.get("k") == "v2"
    assert len(stage.index) == 1


def test_index_cache_stage_is_frozen(tmp_path: Path) -> None:
    """`IndexCacheStage` is a frozen dataclass."""
    import pytest

    stage = materialize_index_cache(tmp_path / "index.json")
    with pytest.raises((AttributeError, Exception)):
        stage.cache = None  # type: ignore[misc,assignment]
