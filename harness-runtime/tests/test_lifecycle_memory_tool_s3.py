"""R-830 — S3 Memory tool backend provider-free tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from harness_runtime.lifecycle.memory_tool_s3 import S3MemoryToolBackend
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
    MemoryToolStorageBackendProtocol,
)


@dataclass
class _Body:
    content: bytes

    def read(self) -> bytes:
        return self.content


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[(bucket, key)] = body

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        try:
            content = self.objects[(bucket, key)]
        except KeyError as exc:
            raise KeyError("NoSuchKey") from exc
        return {"Body": _Body(content)}

    def delete_object(self, **kwargs: object) -> None:
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        self.objects.pop((bucket, key), None)


def _backend() -> tuple[S3MemoryToolBackend, _FakeS3Client]:
    client = _FakeS3Client()
    return (
        S3MemoryToolBackend(
            bucket="memory-bucket",
            key_prefix="tenant-a",
            client=client,
        ),
        client,
    )


def test_s3_backend_satisfies_protocol() -> None:
    backend, _ = _backend()
    assert isinstance(backend, MemoryToolStorageBackendProtocol)


@pytest.mark.asyncio
async def test_round_trip_create_view_delete() -> None:
    backend, client = _backend()

    await backend.create("/memories/foo.txt", b"hello")
    assert await backend.view("/memories/foo.txt") == b"hello"
    assert client.objects[("memory-bucket", "tenant-a/foo.txt")] == b"hello"

    await backend.delete("/memories/foo.txt")
    with pytest.raises(MemoryCallbackIOError):
        await backend.view("/memories/foo.txt")


@pytest.mark.asyncio
async def test_str_replace_and_insert_round_trip() -> None:
    backend, _ = _backend()

    await backend.create("/memories/foo.txt", b"hello world\n")
    await backend.str_replace("/memories/foo.txt", "world", "s3")
    await backend.insert("/memories/foo.txt", 2, "inserted\n")

    assert await backend.view("/memories/foo.txt") == b"hello s3\ninserted\n"


@pytest.mark.asyncio
async def test_path_traversal_rejected_before_s3_call() -> None:
    backend, client = _backend()

    with pytest.raises(MemoryPathViolationError):
        await backend.view("/memories/../secret.txt")

    assert client.objects == {}


@pytest.mark.asyncio
async def test_delete_is_noop_when_object_absent() -> None:
    backend, _ = _backend()
    await backend.delete("/memories/missing.txt")
