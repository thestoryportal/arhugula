"""U-RT-77 — `LocalFilesystemMemoryToolBackend` filesystem implementation.

Implements runtime spec v1.17 §14.12.3 step 2a (`MemoryToolStorageBackend.FILESYSTEM`
→ filesystem-backed implementation) + §14.12.2 per-callback invocation discipline
(path validation at every callback; async-only Protocol surface; no retry inside
callback) + §14.12.5 invariants 3 (path discipline enforced at backend BEFORE I/O)
+ 6 (per-backend lifecycle owned by backend).

Per L9-octies cluster discipline (runtime plan v2.15 §1):
- L1 within-cluster (←U-RT-76).
- §14.D operator-ratified scope at the v2.14/v2.15 arc was filesystem-only.
  The real-bootstrap filesystem memory-tool e2e maps to U-RT-82 and exercises
  this backend as the concrete filesystem carrier.
  Later R-830 slices added DATABASE (SQLite) and S3; the R-FS-1
  `B-MEMORY-SURFACE-BACKEND-IMPLS` arc added ENCRYPTED_FILESYSTEM (realized as
  an injected content codec on this backend — see `MemoryContentCodec` below +
  `memory_tool_encrypted.py`) and OPERATOR_DEFINED (factory class-qualified-name
  resolution); both per spec §14.12.3 step 2.

Content-codec seam (`B-MEMORY-SURFACE-BACKEND-IMPLS`): the backend routes
at-rest content through an injectable `MemoryContentCodec` (identity by default
→ FILESYSTEM; Fernet → ENCRYPTED_FILESYSTEM). The codec runs INSIDE the
existing per-path lock so the single source of truth for read-modify-write
atomicity (`str_replace`/`insert`) stays this backend's per-path lock; an
external encrypt-wrapper would have to DUPLICATE that lock to match atomicity,
re-deriving the read-modify-write — codec injection keeps one authoritative
lock per spec §14.12.2 invariant 3 ("backend owns concurrency discipline").
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Protocol

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
)

__all__ = [
    "LocalFilesystemMemoryToolBackend",
    "MemoryContentCodec",
]


_MEMORIES_SCOPE = "/memories/"
"""The `/memories/` path-scope prefix per ADR-D3 v1.2 §1.1 #11 + runtime spec
§14.12.5 invariant 3."""


_TEXT_ENCODING = "utf-8"
"""Text encoding for the `str_replace` / `insert` byte↔str boundary.

Explicit UTF-8 (vs the locale-default `Path.read_text()`/`write_text()` used at
the v2.15 landing) so that — under the content-codec seam — the round-trip is
byte-faithful: a codec MUST see the exact stored bytes, and CRLF→LF universal-
newline translation would corrupt that. UTF-8 is also the correct encoding for
the memory-file content (markdown / JSON) Claude controls."""


class MemoryContentCodec(Protocol):
    """At-rest content transform for the filesystem-backed memory store.

    `encode` maps plaintext → at-rest bytes (write path); `decode` maps at-rest
    bytes → plaintext (read path). The identity codec (default) leaves bytes
    unchanged (`MemoryToolStorageBackend.FILESYSTEM`); the Fernet codec
    (`memory_tool_encrypted.FernetContentCodec`) returns ciphertext at rest
    (`MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM`).

    Transforms run INSIDE the backend's per-path lock (see module docstring):
    the lock is the single authoritative serialization point for the
    `str_replace`/`insert` read-modify-write, so the codec composes with
    atomicity without a second lock. `encode`/`decode` MAY raise
    `MemoryCallbackIOError` (e.g. ciphertext that fails authentication)."""

    def encode(self, plaintext: bytes) -> bytes: ...

    def decode(self, stored: bytes) -> bytes: ...


class _IdentityContentCodec:
    """Pass-through codec — at-rest bytes == plaintext (FILESYSTEM backend)."""

    def encode(self, plaintext: bytes) -> bytes:
        return plaintext

    def decode(self, stored: bytes) -> bytes:
        return stored


class LocalFilesystemMemoryToolBackend:
    """Filesystem-backed `MemoryToolStorageBackendProtocol` implementation.

    Roots `/memories/` callback paths at a deployment-surface-resolved
    filesystem path. Path discipline enforced BEFORE I/O per §14.12.5
    invariant 3 (NOT relying on filesystem permission errors). Concurrency
    via per-path `asyncio.Lock` per §14.12.2 invariant 3 (backend owns
    concurrency model).
    """

    def __init__(self, *, root: Path, codec: MemoryContentCodec | None = None) -> None:
        """Instantiate backend rooted at `root` filesystem path.

        `root` is the directory under which `/memories/{relative}` callbacks
        resolve. Resolution to PathClass enum is deferred per
        §14.12.7 implementation discretion; this constructor accepts a
        `Path` directly per plan v2.15 U-RT-77 signature.

        `codec` is the at-rest content transform (`B-MEMORY-SURFACE-BACKEND-IMPLS`).
        `None` (default) → identity codec = `MemoryToolStorageBackend.FILESYSTEM`
        (byte-identical to the v2.15 behavior); a Fernet codec →
        `MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM` (ciphertext at rest).
        """
        self._root = root.resolve()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._codec: MemoryContentCodec = codec if codec is not None else _IdentityContentCodec()

    # ------------------------------------------------------------------
    # Path discipline (per §14.12.5 invariant 3 — validate BEFORE I/O).
    # ------------------------------------------------------------------

    def _validate_path(self, path: str) -> Path:
        """Validate `path` is scoped to `/memories/` + map to filesystem path.

        Raises `MemoryPathViolationError` (→ RT-FAIL-MEMORY-PATH-VIOLATION
        permanent per §14.12.4) BEFORE any filesystem I/O on:
        - paths not prefixed with `/memories/`
        - paths containing `..` traversal segments
        - paths whose resolved location escapes `self._root`
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

        # Reject `..` traversal segments BEFORE resolution attempt
        # (defense in depth — resolution check below also catches escapes).
        if ".." in PurePosixPath(relative).parts:
            raise MemoryPathViolationError(f"path {path!r} contains path-traversal segment '..'")

        resolved = (self._root / relative).resolve()
        # Defense-in-depth: resolved path must lie inside self._root
        # (catches symlink-based escapes + normalization edge cases).
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise MemoryPathViolationError(
                f"path {path!r} resolves outside /memories/ scope root {self._root!r}"
            ) from exc

        return resolved

    # ------------------------------------------------------------------
    # MemoryToolStorageBackendProtocol — 5 CRUD callbacks.
    # ------------------------------------------------------------------

    async def view(self, path: str) -> bytes:
        """Read content of `/memories/{path}` (Protocol method).

        Per §14.12.2 invariant 4: no retry inside callback; first-attempt
        I/O failure propagates as `MemoryCallbackIOError`.
        """
        target = self._validate_path(path)
        async with self._locks[path]:
            try:
                raw = await asyncio.to_thread(target.read_bytes)
            except OSError as exc:
                raise MemoryCallbackIOError(f"view({path!r}) failed: {exc}") from exc
            return self._codec.decode(raw)

    async def create(self, path: str, content: bytes) -> None:
        """Create `/memories/{path}` with `content`; overwrites if exists.

        Parents are created on demand (mkdir(parents=True, exist_ok=True))
        per filesystem-style interface ergonomic at /memories paths Claude
        controls.
        """
        target = self._validate_path(path)
        payload = self._codec.encode(content)
        async with self._locks[path]:
            try:
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(target.write_bytes, payload)
            except OSError as exc:
                raise MemoryCallbackIOError(f"create({path!r}) failed: {exc}") from exc

    async def delete(self, path: str) -> None:
        """Delete `/memories/{path}`; no-op if absent."""
        target = self._validate_path(path)
        async with self._locks[path]:
            try:
                await asyncio.to_thread(target.unlink, missing_ok=True)
            except OSError as exc:
                raise MemoryCallbackIOError(f"delete({path!r}) failed: {exc}") from exc

    async def str_replace(self, path: str, old: str, new: str) -> None:
        """Replace `old` with `new` in `/memories/{path}`.

        Raises `MemoryCallbackIOError` if `old` not found OR if file absent
        OR on read/write I/O failure.
        """
        target = self._validate_path(path)
        async with self._locks[path]:
            try:
                raw = await asyncio.to_thread(target.read_bytes)
            except OSError as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) read failed: {exc}") from exc

            content = self._codec.decode(raw).decode(_TEXT_ENCODING)
            if old not in content:
                raise MemoryCallbackIOError(f"str_replace({path!r}): substring {old!r} not found")

            replaced = content.replace(old, new)
            payload = self._codec.encode(replaced.encode(_TEXT_ENCODING))
            try:
                await asyncio.to_thread(target.write_bytes, payload)
            except OSError as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) write failed: {exc}") from exc

    async def insert(self, path: str, line: int, content: str) -> None:
        """Insert `content` at 1-indexed `line` in `/memories/{path}`.

        Per Anthropic Memory tool convention: lines are 1-indexed; `line=1`
        inserts at the top of the file. Raises `MemoryCallbackIOError` on
        out-of-range line OR I/O failure.
        """
        target = self._validate_path(path)
        async with self._locks[path]:
            try:
                raw = await asyncio.to_thread(target.read_bytes)
            except OSError as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) read failed: {exc}") from exc

            existing = self._codec.decode(raw).decode(_TEXT_ENCODING)
            lines = existing.splitlines(keepends=True)
            # 1-indexed; line=1 → insert before lines[0]; line=len(lines)+1 → append.
            if line < 1 or line > len(lines) + 1:
                raise MemoryCallbackIOError(
                    f"insert({path!r}, line={line}): out of range (1..{len(lines) + 1})"
                )

            lines.insert(line - 1, content)
            replaced = "".join(lines)
            payload = self._codec.encode(replaced.encode(_TEXT_ENCODING))

            try:
                await asyncio.to_thread(target.write_bytes, payload)
            except OSError as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) write failed: {exc}") from exc
