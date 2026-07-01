"""R-830 — managed SQL Memory tool backend.

Authority: C-RT-22, U-RT-80, and H_T-CP-16.

Implements the MANAGED_CLOUD ``MemoryToolStorageBackend.DATABASE`` remainder
for PostgreSQL-compatible managed databases. The backend keeps the same
filesystem-style ``/memories/...`` CRUD semantics as the filesystem, SQLite,
and S3 backends while storing bytes in a managed SQL table.

The driver dependency is intentionally optional. Production construction is
lazy through the factory's ``psycopg`` import; provider-free tests inject a
DB-API-compatible connect callable.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, Protocol, runtime_checkable

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
)

__all__ = [
    "ManagedSqlConnect",
    "ManagedSqlConnection",
    "ManagedSqlCursor",
    "ManagedSqlMemoryToolBackend",
]

_MEMORIES_SCOPE = "/memories/"


@runtime_checkable
class ManagedSqlCursor(Protocol):
    """Minimal cursor surface consumed from a DB-API/psycopg execute result."""

    def fetchone(self) -> tuple[object, ...] | None:
        """Return one row or ``None``."""
        ...


@runtime_checkable
class ManagedSqlConnection(Protocol):
    """Minimal PostgreSQL-compatible connection surface used by this backend."""

    def execute(self, query: str, params: tuple[object, ...] = ()) -> ManagedSqlCursor:
        """Execute SQL and return a cursor/result object."""
        ...

    def commit(self) -> None:
        """Commit the current transaction."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...


ManagedSqlConnect = Callable[[str], ManagedSqlConnection]


class ManagedSqlMemoryToolBackend:
    """PostgreSQL-compatible managed-DB ``MemoryToolStorageBackendProtocol``.

    Stores raw ``/memories/...`` paths as primary keys in ``memory_entries``.
    The backend opens a fresh connection per operation and serializes
    same-path read-modify-write operations with an in-process per-path lock.
    Cross-process/write concurrency is delegated to the managed database's
    primary-key and transaction semantics.
    """

    def __init__(self, *, connection_string: str, connect: ManagedSqlConnect) -> None:
        if not connection_string:
            raise ValueError("connection_string must be non-empty")
        self._connection_string = connection_string
        self._connect = connect
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._with_connection(
            lambda conn: conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_entries "
                "(path TEXT PRIMARY KEY, content BYTEA NOT NULL)"
            )
        )

    @property
    def connection_string(self) -> str:
        """Opaque managed-DB connection string."""
        return self._connection_string

    def _validate_path(self, path: str) -> str:
        if not path.startswith(_MEMORIES_SCOPE):
            raise MemoryPathViolationError(f"path {path!r} not prefixed with {_MEMORIES_SCOPE!r}")

        relative = path[len(_MEMORIES_SCOPE) :]
        if not relative:
            raise MemoryPathViolationError(
                f"path {path!r} resolves to /memories/ directory itself; expected file path"
            )
        if relative.startswith("/"):
            raise MemoryPathViolationError(f"path {path!r} double-slash after /memories/ scope")
        if ".." in PurePosixPath(relative).parts:
            raise MemoryPathViolationError(f"path {path!r} contains path-traversal segment '..'")
        return path

    def _with_connection(self, fn: Callable[[ManagedSqlConnection], Any]) -> Any:
        conn = self._connect(self._connection_string)
        try:
            result = fn(conn)
            conn.commit()
            return result
        finally:
            conn.close()

    def _db_read(self, key: str) -> bytes | None:
        cursor = self._with_connection(
            lambda conn: conn.execute("SELECT content FROM memory_entries WHERE path = %s", (key,))
        )
        row = cursor.fetchone()
        if row is None:
            return None
        raw = row[0]
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, memoryview):
            return raw.tobytes()
        return bytes(raw)  # type: ignore[arg-type]

    def _db_write(self, key: str, content: bytes) -> None:
        self._with_connection(
            lambda conn: conn.execute(
                "INSERT INTO memory_entries (path, content) VALUES (%s, %s) "
                "ON CONFLICT (path) DO UPDATE SET content = EXCLUDED.content",
                (key, content),
            )
        )

    def _db_delete(self, key: str) -> None:
        self._with_connection(
            lambda conn: conn.execute("DELETE FROM memory_entries WHERE path = %s", (key,))
        )

    async def view(self, path: str) -> bytes:
        """Read content of ``/memories/{path}`` from the managed database."""
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                content = await asyncio.to_thread(self._db_read, key)
            except Exception as exc:
                raise MemoryCallbackIOError(f"view({path!r}) failed: {exc}") from exc
            if content is None:
                raise MemoryCallbackIOError(f"view({path!r}): no entry at {path!r}")
            return content

    async def create(self, path: str, content: bytes) -> None:
        """Create or overwrite ``/memories/{path}``."""
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                await asyncio.to_thread(self._db_write, key, content)
            except Exception as exc:
                raise MemoryCallbackIOError(f"create({path!r}) failed: {exc}") from exc

    async def delete(self, path: str) -> None:
        """Delete ``/memories/{path}``; no-op if absent."""
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                await asyncio.to_thread(self._db_delete, key)
            except Exception as exc:
                raise MemoryCallbackIOError(f"delete({path!r}) failed: {exc}") from exc

    async def str_replace(self, path: str, old: str, new: str) -> None:
        """Replace ``old`` with ``new`` in ``/memories/{path}``."""
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                existing = await asyncio.to_thread(self._db_read, key)
            except Exception as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) read failed: {exc}") from exc
            if existing is None:
                raise MemoryCallbackIOError(f"str_replace({path!r}): no entry at {path!r}")

            text = existing.decode("utf-8")
            if old not in text:
                raise MemoryCallbackIOError(f"str_replace({path!r}): substring {old!r} not found")
            try:
                await asyncio.to_thread(self._db_write, key, text.replace(old, new).encode("utf-8"))
            except Exception as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) write failed: {exc}") from exc

    async def insert(self, path: str, line: int, content: str) -> None:
        """Insert ``content`` at 1-indexed ``line`` in ``/memories/{path}``."""
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                existing = await asyncio.to_thread(self._db_read, key)
            except Exception as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) read failed: {exc}") from exc
            if existing is None:
                raise MemoryCallbackIOError(f"insert({path!r}): no entry at {path!r}")

            lines = existing.decode("utf-8").splitlines(keepends=True)
            if line < 1 or line > len(lines) + 1:
                raise MemoryCallbackIOError(
                    f"insert({path!r}, line={line}): out of range (1..{len(lines) + 1})"
                )
            lines.insert(line - 1, content)
            try:
                await asyncio.to_thread(self._db_write, key, "".join(lines).encode("utf-8"))
            except Exception as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) write failed: {exc}") from exc
