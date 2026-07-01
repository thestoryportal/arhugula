"""U-RT-09 — Content-addressed index + semantic cache.

Per `Spec_Harness_Runtime_v1.md` v1.1 §4 (C-RT-04 `index` / `cache` fields)
and Phase 2 Session 3 plan v2.1 §2 L2, this module:

- Defines a JSON-file-backed `ContentAddressedIndex` runtime composition
  primitive with canonical serialization (sorted keys, separator-tight)
  so reattach → re-save is byte-identical.
- Defines an in-memory `SemanticCache` runtime composition primitive.
- Provides `materialize_index_cache(index_path)` that opens the on-disk
  index (fresh-creates idempotently if absent) and constructs an empty
  cache.

Class 2 Protocol-stub concretization per the L0 Tension record
(`.harness/class_2_tension_phase_2_session_5_harness_context_axis_type_mapping.md`):
the IS library shipped contracts/schemas but no `ContentAddressedIndex` /
`SemanticCache` runtime types; this module is the runtime's composition.

Scope discipline:
- This index is NOT the state ledger. The ledger is hash-chained per
  C-IS-06; this index is a separate runtime key-value handle whose
  semantics (what gets indexed) are filled in by downstream consumers
  (e.g., response-hash → entry-position lookup at U-RT-32).
- This cache is NOT the workflow lifecycle replay buffer (that's CP). It
  is a generic key-value scratchpad for response-reuse hints.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ContentAddressedIndex",
    "IndexCacheStage",
    "SemanticCache",
    "materialize_index_cache",
]


def _canonical_dumps(data: dict[str, str]) -> bytes:
    """Canonical JSON: sorted keys, separator-tight, UTF-8 encoded."""
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class ContentAddressedIndex:
    """File-backed key-value index with canonical (byte-stable) serialization.

    Construction reads `path` if present; absent → empty store. Save
    re-writes the file with `_canonical_dumps` so round-trip
    load-then-save is byte-identical.
    """

    path: Path
    _store: dict[str, str] = field(default_factory=lambda: dict[str, str]())

    @classmethod
    def open(cls, path: Path) -> ContentAddressedIndex:
        """Open (or fresh-create) the index at `path`."""
        data: dict[str, str]
        if path.exists():
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8")) if raw else {}
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            path.write_bytes(_canonical_dumps(data))
        return cls(path=path, _store=dict(data))

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def put(self, key: str, value: str) -> None:
        self._store[key] = value

    def save(self) -> None:
        """Write the current store to disk in canonical form."""
        self.path.write_bytes(_canonical_dumps(self._store))

    def content_hash(self) -> str:
        """SHA-256 of the canonical serialization of the current store.

        Round-trip invariant: an opened-then-immediately-saved index produces
        the same hash as the on-disk content (canonical form is stable).
        """
        return hashlib.sha256(_canonical_dumps(self._store)).hexdigest()

    def __len__(self) -> int:
        return len(self._store)


@dataclass
class SemanticCache:
    """In-memory key-value cache (runtime composition primitive)."""

    _store: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    def get(self, key: str) -> Any | None:
        return self._store.get(key)

    def put(self, key: str, value: Any) -> None:
        self._store[key] = value

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        return key in self._store


@dataclass(frozen=True)
class IndexCacheStage:
    """Result of `materialize_index_cache`: index + cache handles."""

    index: ContentAddressedIndex
    cache: SemanticCache


def materialize_index_cache(index_path: Path) -> IndexCacheStage:
    """Open/fresh-create the index at `index_path`; build an empty cache.

    Idempotency guarantees:
    - Re-running against an existing index file yields the same
      `content_hash()` as the prior save (canonical form stability).
    - Fresh-create writes a canonical empty-object file (`{}`) so a
      subsequent call yields the same state.
    """
    index = ContentAddressedIndex.open(index_path)
    cache = SemanticCache()
    return IndexCacheStage(index=index, cache=cache)
