"""R-830 — managed SQL Memory tool backend provider-free tests."""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping

import pytest
from harness_runtime.lifecycle.memory_tool_managed_db import (
    ManagedSqlConnection,
    ManagedSqlCursor,
    ManagedSqlMemoryToolBackend,
)
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
    MemoryToolStorageBackendProtocol,
)


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _Connection:
    def __init__(self, store: MutableMapping[str, bytes]) -> None:
        self._store = store
        self.closed = False
        self.commits = 0

    def execute(self, query: str, params: tuple[object, ...] = ()) -> ManagedSqlCursor:
        if query.startswith("CREATE TABLE"):
            return _Cursor()
        if query.startswith("SELECT content"):
            key = str(params[0])
            value = self._store.get(key)
            return _Cursor(None if value is None else (value,))
        if query.startswith("INSERT INTO memory_entries"):
            key = str(params[0])
            raw = params[1]
            assert isinstance(raw, bytes)
            self._store[key] = raw
            return _Cursor()
        if query.startswith("DELETE FROM memory_entries"):
            key = str(params[0])
            self._store.pop(key, None)
            return _Cursor()
        raise AssertionError(f"unexpected query: {query}")

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class _Connect:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.connection_strings: list[str] = []

    def __call__(self, connection_string: str) -> ManagedSqlConnection:
        self.connection_strings.append(connection_string)
        return _Connection(self.store)


def _backend() -> tuple[ManagedSqlMemoryToolBackend, _Connect]:
    connect = _Connect()
    return (
        ManagedSqlMemoryToolBackend(
            connection_string="postgresql://example.invalid/memory",
            connect=connect,
        ),
        connect,
    )


def test_backend_instantiates_and_conforms_to_protocol() -> None:
    backend, connect = _backend()

    assert isinstance(backend, MemoryToolStorageBackendProtocol)
    assert connect.connection_strings == ["postgresql://example.invalid/memory"]


async def test_create_view_delete_roundtrip() -> None:
    backend, _connect = _backend()

    await backend.create("/memories/note.txt", b"hello")
    assert await backend.view("/memories/note.txt") == b"hello"
    await backend.delete("/memories/note.txt")
    with pytest.raises(MemoryCallbackIOError):
        await backend.view("/memories/note.txt")


async def test_str_replace_and_insert_update_content() -> None:
    backend, _connect = _backend()

    await backend.create("/memories/note.txt", b"alpha\nbeta\n")
    await backend.str_replace("/memories/note.txt", "beta", "gamma")
    await backend.insert("/memories/note.txt", 1, "header\n")

    assert await backend.view("/memories/note.txt") == b"header\nalpha\ngamma\n"


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
async def test_path_discipline_violations_raise_before_io(bad_path: str) -> None:
    backend, connect = _backend()
    connect.connection_strings.clear()

    with pytest.raises(MemoryPathViolationError):
        await backend.view(bad_path)

    assert connect.connection_strings == []


async def test_absent_and_invalid_update_cases_raise_io_error() -> None:
    backend, _connect = _backend()

    with pytest.raises(MemoryCallbackIOError):
        await backend.str_replace("/memories/missing.txt", "a", "b")
    with pytest.raises(MemoryCallbackIOError):
        await backend.insert("/memories/missing.txt", 1, "x\n")

    await backend.create("/memories/note.txt", b"only\n")
    with pytest.raises(MemoryCallbackIOError):
        await backend.str_replace("/memories/note.txt", "absent", "x")
    with pytest.raises(MemoryCallbackIOError):
        await backend.insert("/memories/note.txt", 99, "x\n")


async def test_content_persists_across_backend_reinstantiation() -> None:
    connect = _Connect()
    backend_a = ManagedSqlMemoryToolBackend(
        connection_string="postgresql://example.invalid/memory",
        connect=connect,
    )
    await backend_a.create("/memories/durable.txt", b"persisted")

    backend_b = ManagedSqlMemoryToolBackend(
        connection_string="postgresql://example.invalid/memory",
        connect=connect,
    )
    assert await backend_b.view("/memories/durable.txt") == b"persisted"


async def test_concurrent_same_path_insert_race_free() -> None:
    backend, _connect = _backend()
    await backend.create("/memories/counter.txt", b"base\n")

    await asyncio.gather(*(backend.insert("/memories/counter.txt", 1, "L\n") for _ in range(20)))

    final = await backend.view("/memories/counter.txt")
    assert final.decode("utf-8").splitlines() == ["L"] * 20 + ["base"]
