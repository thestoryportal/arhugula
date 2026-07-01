"""R-830 — ``SqliteMemoryToolBackend`` — SELF_HOSTED_SERVER ``DATABASE`` backend.

Implements the ``MemoryToolStorageBackend.DATABASE`` enum value (runtime spec
v1.17 §14.12.3: *"``MemoryToolStorageBackend.DATABASE`` → instantiate a
database-backed implementation per ``backend_params['connection_string']``"*)
via the stdlib ``sqlite3`` module — a local embedded SQL store. ``DATABASE`` is
admissible at SELF_HOSTED_SERVER + MANAGED_CLOUD per the graceful-degradation
resolver (``harness_as.anthropic_graceful_degradation._MEMORY_BACKENDS``).

**Scope (honest framing).** This is the embedded *SELF_HOSTED_SERVER*
``DATABASE`` backend: a local SQLite file, the SQL sibling of
``LocalFilesystemMemoryToolBackend``. MANAGED_CLOUD ``DATABASE`` is implemented
separately by ``ManagedSqlMemoryToolBackend`` for PostgreSQL-compatible managed
databases, and S3 covers the cloud-vault path. ``ENCRYPTED_FILESYSTEM`` (Fernet
content codec on the filesystem backend) and ``OPERATOR_DEFINED`` (importlib
class resolution) are implemented at ``B-MEMORY-SURFACE-BACKEND-IMPLS``; the
factory now raises only on missing/invalid ``backend_params`` (and MANAGED_CLOUD
without an explicit ``memory_tool_backend_config`` override).

**Semantics byte-mirror ``LocalFilesystemMemoryToolBackend``** per the shared
``MemoryToolStorageBackendProtocol`` contract:

- same ``/memories/`` path discipline (``MemoryPathViolationError`` BEFORE I/O;
  ``..``-traversal + empty-relative + double-slash rejection) per §14.12.5
  invariant 3 (backend-agnostic; enforced at the backend BEFORE I/O);
- same exception types (``MemoryPathViolationError`` / ``MemoryCallbackIOError``);
- ``view`` / ``str_replace`` / ``insert`` on an absent key raise
  ``MemoryCallbackIOError`` (mirror filesystem absent→OSError→wrapped);
- ``create`` overwrites if present; ``delete`` is a no-op if absent;
- ``insert`` is 1-indexed (``line=1`` → top; ``line=len+1`` → append);
- no retry inside the callback per §14.12.2 invariant 4.

Concurrency mirrors the filesystem backend for same-path operations: a per-path
``asyncio.Lock`` keyed on the raw ``/memories/...`` path string serializes
read-modify-write on the same path. SQLite also has one writer per database
file, so writes are additionally serialized through a backend-level write lock;
blocking ``sqlite3`` work still runs under ``asyncio.to_thread``. A fresh
connection is opened per operation (``contextlib.closing`` — the ``sqlite3``
connection ``with``-form manages only the transaction, not the close), which
sidesteps cross-thread connection sharing under ``to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections import defaultdict
from pathlib import Path, PurePosixPath

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
)

__all__ = ["SqliteMemoryToolBackend"]


_MEMORIES_SCOPE = "/memories/"
"""The ``/memories/`` path-scope prefix per ADR-D3 v1.2 §1.1 #11 + runtime spec
§14.12.5 invariant 3."""


class SqliteMemoryToolBackend:
    """SQLite-backed ``MemoryToolStorageBackendProtocol`` implementation.

    Stores ``/memories/{path}`` entries as ``(path, content)`` rows in a single
    ``memory_entries`` table. The raw ``/memories/...`` path is the primary key.
    Path discipline is enforced BEFORE any database I/O per §14.12.5 invariant 3.
    """

    def __init__(self, *, db_path: Path) -> None:
        """Instantiate the backend over the SQLite database at ``db_path``.

        Creates the parent directory + the ``memory_entries`` schema on demand
        (idempotent ``CREATE TABLE IF NOT EXISTS``). ``db_path`` is the resolved
        ``backend_params['connection_string']`` (or the factory default
        ``repository_root / ".harness/memories.db"``).
        """
        self._db_path = db_path
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._write_lock = asyncio.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_entries "
                "(path TEXT PRIMARY KEY, content BLOB NOT NULL)"
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Path discipline (per §14.12.5 invariant 3 — validate BEFORE I/O).
    # ------------------------------------------------------------------

    def _validate_path(self, path: str) -> str:
        """Validate ``path`` is scoped to ``/memories/``; return the raw DB key.

        Raises ``MemoryPathViolationError`` (→ RT-FAIL-MEMORY-PATH-VIOLATION
        permanent per §14.12.4) BEFORE any database I/O on: paths not prefixed
        with ``/memories/``; the bare ``/memories/`` directory; a double-slash
        after the scope; or ``..`` traversal segments. The validated raw path is
        the ``memory_entries.path`` primary key (mirrors the filesystem backend's
        per-path lock key).
        """
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

    # ------------------------------------------------------------------
    # Blocking sqlite3 helpers (run under asyncio.to_thread).
    # ------------------------------------------------------------------

    def _db_read(self, key: str) -> bytes | None:
        with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT content FROM memory_entries WHERE path = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    def _db_write(self, key: str, content: bytes) -> None:
        with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_entries (path, content) VALUES (?, ?)",
                (key, content),
            )
            conn.commit()

    def _db_delete(self, key: str) -> None:
        with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute("DELETE FROM memory_entries WHERE path = ?", (key,))
            conn.commit()

    # ------------------------------------------------------------------
    # MemoryToolStorageBackendProtocol — 5 CRUD callbacks.
    # ------------------------------------------------------------------

    async def view(self, path: str) -> bytes:
        """Read content of ``/memories/{path}`` (Protocol method).

        Absent key raises ``MemoryCallbackIOError`` (mirror filesystem
        absent→OSError). No retry inside callback per §14.12.2 invariant 4.
        """
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                content = await asyncio.to_thread(self._db_read, key)
            except sqlite3.Error as exc:
                raise MemoryCallbackIOError(f"view({path!r}) failed: {exc}") from exc
            if content is None:
                raise MemoryCallbackIOError(f"view({path!r}): no entry at {path!r}")
            return content

    async def create(self, path: str, content: bytes) -> None:
        """Create ``/memories/{path}`` with ``content``; overwrites if exists."""
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                async with self._write_lock:
                    await asyncio.to_thread(self._db_write, key, content)
            except sqlite3.Error as exc:
                raise MemoryCallbackIOError(f"create({path!r}) failed: {exc}") from exc

    async def delete(self, path: str) -> None:
        """Delete ``/memories/{path}``; no-op if absent."""
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                async with self._write_lock:
                    await asyncio.to_thread(self._db_delete, key)
            except sqlite3.Error as exc:
                raise MemoryCallbackIOError(f"delete({path!r}) failed: {exc}") from exc

    async def str_replace(self, path: str, old: str, new: str) -> None:
        """Replace ``old`` with ``new`` in ``/memories/{path}``.

        Raises ``MemoryCallbackIOError`` if the entry is absent, if ``old`` is
        not found, or on read/write failure.
        """
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                existing = await asyncio.to_thread(self._db_read, key)
            except sqlite3.Error as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) read failed: {exc}") from exc
            if existing is None:
                raise MemoryCallbackIOError(f"str_replace({path!r}): no entry at {path!r}")

            text = existing.decode("utf-8")
            if old not in text:
                raise MemoryCallbackIOError(f"str_replace({path!r}): substring {old!r} not found")

            replaced = text.replace(old, new).encode("utf-8")
            try:
                async with self._write_lock:
                    await asyncio.to_thread(self._db_write, key, replaced)
            except sqlite3.Error as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) write failed: {exc}") from exc

    async def insert(self, path: str, line: int, content: str) -> None:
        """Insert ``content`` at 1-indexed ``line`` in ``/memories/{path}``.

        ``line=1`` inserts at the top; ``line=len(lines)+1`` appends. Raises
        ``MemoryCallbackIOError`` on an absent entry, an out-of-range line, or
        I/O failure.
        """
        key = self._validate_path(path)
        async with self._locks[path]:
            try:
                existing = await asyncio.to_thread(self._db_read, key)
            except sqlite3.Error as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) read failed: {exc}") from exc
            if existing is None:
                raise MemoryCallbackIOError(f"insert({path!r}): no entry at {path!r}")

            lines = existing.decode("utf-8").splitlines(keepends=True)
            if line < 1 or line > len(lines) + 1:
                raise MemoryCallbackIOError(
                    f"insert({path!r}, line={line}): out of range (1..{len(lines) + 1})"
                )

            lines.insert(line - 1, content)
            replaced = "".join(lines).encode("utf-8")
            try:
                async with self._write_lock:
                    await asyncio.to_thread(self._db_write, key, replaced)
            except sqlite3.Error as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) write failed: {exc}") from exc
