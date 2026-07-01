"""R-830 — S3-backed Memory tool backend.

Authority: C-RT-22, U-RT-80, and H_T-CP-16.

Implements ``MemoryToolStorageBackend.S3`` through a narrow S3 client protocol.
The backend itself is provider-SDK-neutral and provider-free tests inject a
fake client; the factory may lazily construct a real boto3 S3 client when the
operator supplies credentials and the optional dependency is installed.

This closes the cloud-vault backend path together with the live R-830 S3 e2e
proof. MANAGED_CLOUD ``DATABASE`` is implemented separately by
``ManagedSqlMemoryToolBackend`` for PostgreSQL-compatible managed databases.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Protocol, runtime_checkable

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
)

__all__ = ["S3ClientProtocol", "S3MemoryToolBackend"]

_MEMORIES_SCOPE = "/memories/"


@runtime_checkable
class S3ClientProtocol(Protocol):
    """Minimal S3 client surface consumed by ``S3MemoryToolBackend``."""

    def put_object(self, **kwargs: Any) -> Any:
        """Write one object."""
        ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Read one object; returned mapping contains ``Body``."""
        ...

    def delete_object(self, **kwargs: Any) -> Any:
        """Delete one object."""
        ...


class S3MemoryToolBackend:
    """S3-backed ``MemoryToolStorageBackendProtocol`` implementation."""

    def __init__(
        self,
        *,
        bucket: str,
        client: S3ClientProtocol,
        key_prefix: str = "",
    ) -> None:
        if not bucket:
            raise ValueError("bucket must be non-empty")
        self._bucket = bucket
        self._client = client
        self._key_prefix = key_prefix.strip("/")
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def bucket(self) -> str:
        """S3 bucket name used for Memory tool objects."""
        return self._bucket

    @property
    def key_prefix(self) -> str:
        """Optional key prefix under the bucket."""
        return self._key_prefix

    def _object_key(self, path: str) -> str:
        """Validate ``/memories/`` path scope and return the S3 object key."""
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

        if self._key_prefix:
            return f"{self._key_prefix}/{relative}"
        return relative

    def _read_body(self, body: object) -> bytes:
        if isinstance(body, bytes):
            return body
        read = getattr(body, "read", None)
        if callable(read):
            content = read()
            if isinstance(content, bytes):
                return content
        raise MemoryCallbackIOError("S3 get_object Body did not yield bytes")

    def _get_object(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return self._read_body(response["Body"])

    def _put_object(self, key: str, content: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=content)

    def _delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    async def view(self, path: str) -> bytes:
        """Read content of ``/memories/{path}`` from S3."""
        key = self._object_key(path)
        async with self._locks[path]:
            try:
                return await asyncio.to_thread(self._get_object, key)
            except MemoryCallbackIOError:
                raise
            except Exception as exc:
                raise MemoryCallbackIOError(f"view({path!r}) failed: {exc}") from exc

    async def create(self, path: str, content: bytes) -> None:
        """Write content to ``/memories/{path}``; overwrites if present."""
        key = self._object_key(path)
        async with self._locks[path]:
            try:
                await asyncio.to_thread(self._put_object, key, content)
            except Exception as exc:
                raise MemoryCallbackIOError(f"create({path!r}) failed: {exc}") from exc

    async def delete(self, path: str) -> None:
        """Delete ``/memories/{path}``; no-op if absent."""
        key = self._object_key(path)
        async with self._locks[path]:
            try:
                await asyncio.to_thread(self._delete_object, key)
            except Exception as exc:
                raise MemoryCallbackIOError(f"delete({path!r}) failed: {exc}") from exc

    async def str_replace(self, path: str, old: str, new: str) -> None:
        """Replace ``old`` with ``new`` in ``/memories/{path}``."""
        key = self._object_key(path)
        async with self._locks[path]:
            try:
                existing = await asyncio.to_thread(self._get_object, key)
            except MemoryCallbackIOError:
                raise
            except Exception as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) read failed: {exc}") from exc

            text = existing.decode("utf-8")
            if old not in text:
                raise MemoryCallbackIOError(f"str_replace({path!r}): substring {old!r} not found")
            try:
                replaced = text.replace(old, new).encode("utf-8")
                await asyncio.to_thread(self._put_object, key, replaced)
            except Exception as exc:
                raise MemoryCallbackIOError(f"str_replace({path!r}) write failed: {exc}") from exc

    async def insert(self, path: str, line: int, content: str) -> None:
        """Insert ``content`` at 1-indexed ``line`` in ``/memories/{path}``."""
        key = self._object_key(path)
        async with self._locks[path]:
            try:
                existing = await asyncio.to_thread(self._get_object, key)
            except MemoryCallbackIOError:
                raise
            except Exception as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) read failed: {exc}") from exc

            lines = existing.decode("utf-8").splitlines(keepends=True)
            if line < 1 or line > len(lines) + 1:
                raise MemoryCallbackIOError(
                    f"insert({path!r}, line={line}): out of range (1..{len(lines) + 1})"
                )
            lines.insert(line - 1, content)
            try:
                await asyncio.to_thread(self._put_object, key, "".join(lines).encode("utf-8"))
            except Exception as exc:
                raise MemoryCallbackIOError(f"insert({path!r}) write failed: {exc}") from exc
