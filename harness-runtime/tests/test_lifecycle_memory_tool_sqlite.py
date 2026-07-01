"""R-830 — ``SqliteMemoryToolBackend`` tests (SELF_HOSTED_SERVER DATABASE backend).

Mirrors the ``LocalFilesystemMemoryToolBackend`` AC set (semantics parity per the
shared ``MemoryToolStorageBackendProtocol``) plus DATABASE-specific cases:

1. Instantiation + ``@runtime_checkable`` Protocol conformance.
2. Round-trip: create → view → delete → subsequent view raises ``MemoryCallbackIOError``.
3. ``str_replace`` after create → view returns replaced content; absent ``old`` raises.
4. ``insert`` 1-indexed (top / append) + out-of-range raises.
5. Path discipline: ``..`` traversal + absolute-outside-scope + bare ``/memories/`` raise
   ``MemoryPathViolationError`` BEFORE database I/O.
6. Absent-key parity: ``view`` / ``str_replace`` / ``insert`` on an absent path raise
   ``MemoryCallbackIOError`` (mirror filesystem absent→OSError→wrapped).
7. ``create`` overwrites; ``delete`` is a no-op if absent.
8. **DATABASE-specific:** content persists across backend re-instantiation over the
   same database file (proves real storage, not in-process state).
9. Concurrency: distinct paths concurrent; same-path ``str_replace`` race-free (per-path lock).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from harness_runtime.lifecycle.memory_tool_sqlite import SqliteMemoryToolBackend
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
    MemoryToolStorageBackendProtocol,
)


def _backend(tmp_path: Path) -> SqliteMemoryToolBackend:
    return SqliteMemoryToolBackend(db_path=tmp_path / "memories.db")


# AC #1 — instantiation + Protocol conformance.


def test_backend_instantiates_and_creates_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "memories.db"
    backend = SqliteMemoryToolBackend(db_path=db_path)
    assert db_path.exists()  # parent dir + schema created on construction
    assert isinstance(backend, MemoryToolStorageBackendProtocol)


# AC #2 — round-trip create → view → delete → view raises.


async def test_create_view_delete_roundtrip(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.create("/memories/note.txt", b"hello")
    assert await backend.view("/memories/note.txt") == b"hello"
    await backend.delete("/memories/note.txt")
    with pytest.raises(MemoryCallbackIOError):
        await backend.view("/memories/note.txt")


# AC #3 — str_replace.


async def test_str_replace_replaces_substring(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.create("/memories/note.txt", b"hello world")
    await backend.str_replace("/memories/note.txt", "world", "sqlite")
    assert await backend.view("/memories/note.txt") == b"hello sqlite"


async def test_str_replace_absent_substring_raises(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.create("/memories/note.txt", b"hello")
    with pytest.raises(MemoryCallbackIOError):
        await backend.str_replace("/memories/note.txt", "absent", "x")


# AC #4 — insert (1-indexed) + out-of-range.


async def test_insert_one_indexed_top_and_append(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.create("/memories/note.txt", b"line2\n")
    await backend.insert("/memories/note.txt", 1, "line1\n")  # top
    await backend.insert("/memories/note.txt", 3, "line3\n")  # append (len+1)
    assert await backend.view("/memories/note.txt") == b"line1\nline2\nline3\n"


async def test_insert_out_of_range_raises(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.create("/memories/note.txt", b"only\n")
    with pytest.raises(MemoryCallbackIOError):
        await backend.insert("/memories/note.txt", 99, "x\n")


# AC #5 — path discipline (raised BEFORE database I/O).


@pytest.mark.parametrize(
    "bad_path",
    [
        "/memories/../etc/passwd",
        "/etc/passwd",
        "/memories/",
        "/memories//double",
        "relative/no/scope",
    ],
)
async def test_path_discipline_violations_raise(tmp_path: Path, bad_path: str) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(MemoryPathViolationError):
        await backend.view(bad_path)


# AC #6 — absent-key parity (mirror filesystem absent→OSError→MemoryCallbackIOError).


async def test_view_absent_key_raises_io_error(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(MemoryCallbackIOError):
        await backend.view("/memories/missing.txt")


async def test_str_replace_absent_key_raises_io_error(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(MemoryCallbackIOError):
        await backend.str_replace("/memories/missing.txt", "a", "b")


async def test_insert_absent_key_raises_io_error(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(MemoryCallbackIOError):
        await backend.insert("/memories/missing.txt", 1, "x\n")


# AC #7 — create overwrites; delete no-op if absent.


async def test_create_overwrites_existing(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.create("/memories/note.txt", b"first")
    await backend.create("/memories/note.txt", b"second")
    assert await backend.view("/memories/note.txt") == b"second"


async def test_delete_absent_is_noop(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.delete("/memories/never-existed.txt")  # no raise


# AC #8 — DATABASE-specific: persistence across re-instantiation.


async def test_content_persists_across_reinstantiation(tmp_path: Path) -> None:
    db_path = tmp_path / "memories.db"
    backend_a = SqliteMemoryToolBackend(db_path=db_path)
    await backend_a.create("/memories/durable.txt", b"persisted")

    # A fresh backend over the same DB file sees the prior write — proves the
    # entry is stored in the database, not in per-instance process state.
    backend_b = SqliteMemoryToolBackend(db_path=db_path)
    assert await backend_b.view("/memories/durable.txt") == b"persisted"


# AC #9 — concurrency (distinct paths + same-path race-free).


async def test_concurrent_distinct_paths(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await asyncio.gather(
        *(backend.create(f"/memories/f{i}.txt", str(i).encode()) for i in range(50))
    )
    for i in range(50):
        assert await backend.view(f"/memories/f{i}.txt") == str(i).encode()


async def test_concurrent_same_path_insert_race_free(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    await backend.create("/memories/counter.txt", b"base\n")
    # 20 concurrent read-modify-write inserts at line 1 on the SAME path. The
    # per-path lock serializes them; without it, concurrent reads would see the
    # same snapshot and overwrite each other (lost updates) → fewer than 21 lines.
    await asyncio.gather(*(backend.insert("/memories/counter.txt", 1, "L\n") for _ in range(20)))
    final = await backend.view("/memories/counter.txt")
    assert final.decode("utf-8").splitlines() == ["L"] * 20 + ["base"]
