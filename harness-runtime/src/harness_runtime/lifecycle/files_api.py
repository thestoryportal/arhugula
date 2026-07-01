"""R-810 Files API runtime helpers.

This module owns the runtime-side Files API boundary and Anthropic adapter. It
supplies:

- a small provider-neutral file metadata record,
- a protocol plus Anthropic Files API adapter,
- Messages and Batch API file reference helpers, and
- a `files.operation` span helper carrying the AS `files.*` namespace.

Authority: C-AS-14 §14.6 plus H_T-AS-8e and H_T-CP-17.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, BinaryIO, Protocol, cast

__all__ = [
    "ANTHROPIC_FILES_API_BETA",
    "AnthropicFilesApiClient",
    "FilesApiClientProtocol",
    "FilesApiFile",
    "FilesOperationKind",
    "container_upload_block",
    "document_file_block",
    "files_message_batch_request",
    "files_operation_span",
]


ANTHROPIC_FILES_API_BETA = "files-api-2025-04-14"
"""Anthropic Files API beta header value used by the managed R-810 live path."""


class FilesOperationKind(StrEnum):
    """AS `files.operation.kind` enum values."""

    UPLOAD = "upload"
    LIST = "list"
    METADATA = "metadata"
    DELETE = "delete"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class FilesApiFile:
    """Provider-neutral metadata for a file stored by a managed Files API."""

    file_id: str
    filename: str
    mime_type: str
    size_bytes: int
    workspace_id: str


class FilesApiClientProtocol(Protocol):
    """Minimal async port for a provider-backed Files API adapter."""

    async def upload(
        self,
        *,
        file: BinaryIO,
        filename: str,
        mime_type: str,
    ) -> FilesApiFile: ...

    async def list_files(self) -> tuple[FilesApiFile, ...]: ...

    async def retrieve_metadata(self, *, file_id: str) -> FilesApiFile: ...

    async def delete(self, *, file_id: str) -> None: ...


def _read_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, Any], value)
        return mapping.get(field, default)
    return getattr(value, field, default)


def _file_from_anthropic(value: Any) -> FilesApiFile:
    return FilesApiFile(
        file_id=str(_read_field(value, "id", "")),
        filename=str(_read_field(value, "filename", "")),
        mime_type=str(_read_field(value, "mime_type", "")),
        size_bytes=int(_read_field(value, "size_bytes", 0) or 0),
        workspace_id=str(_read_field(value, "workspace_id", "")),
    )


class AnthropicFilesApiClient:
    """Async wrapper around Anthropic's beta Files API.

    The lockfile SDK exposes synchronous Files API methods, so this adapter
    runs provider calls in a worker thread while preserving the async runtime
    port used by the harness.
    """

    def __init__(self, *, client: Any) -> None:
        self._client = client

    async def upload(
        self,
        *,
        file: BinaryIO,
        filename: str,
        mime_type: str,
    ) -> FilesApiFile:
        def _upload() -> Any:
            return self._client.beta.files.upload(
                file=(filename, file, mime_type),
                betas=[ANTHROPIC_FILES_API_BETA],
            )

        return _file_from_anthropic(await asyncio.to_thread(_upload))

    async def list_files(self) -> tuple[FilesApiFile, ...]:
        def _list() -> Any:
            return self._client.beta.files.list(betas=[ANTHROPIC_FILES_API_BETA])

        page = await asyncio.to_thread(_list)
        data = _read_field(page, "data", page)
        if isinstance(data, list):
            return tuple(_file_from_anthropic(item) for item in cast(list[Any], data))
        return tuple(_file_from_anthropic(item) for item in data)

    async def retrieve_metadata(self, *, file_id: str) -> FilesApiFile:
        def _retrieve() -> Any:
            return self._client.beta.files.retrieve_metadata(
                file_id,
                betas=[ANTHROPIC_FILES_API_BETA],
            )

        return _file_from_anthropic(await asyncio.to_thread(_retrieve))

    async def delete(self, *, file_id: str) -> None:
        def _delete() -> Any:
            return self._client.beta.files.delete(
                file_id,
                betas=[ANTHROPIC_FILES_API_BETA],
            )

        await asyncio.to_thread(_delete)


def container_upload_block(file_id: str) -> Mapping[str, str]:
    """Return the Anthropic code-execution content block for `file_id`.

    This helper is pure data shaping; it does not validate provider reachability
    or claim that the referenced file exists.
    """

    return {"type": "container_upload", "file_id": file_id}


def document_file_block(
    file_id: str,
    *,
    title: str | None = None,
    context: str | None = None,
) -> Mapping[str, Any]:
    """Return a Messages document block that references an uploaded file."""

    block: dict[str, Any] = {
        "type": "document",
        "source": {
            "type": "file",
            "file_id": file_id,
        },
    }
    if title is not None:
        block["title"] = title
    if context is not None:
        block["context"] = context
    return block


def files_message_batch_request(
    *,
    custom_id: str,
    model: str,
    max_tokens: int,
    file_id: str,
    prompt: str,
) -> Mapping[str, Any]:
    """Build a Message Batches request that references a Files API file.

    The returned shape is provider data only; submitting it remains a caller
    decision because Message Batches are asynchronous and usage-billed.
    """

    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        document_file_block(file_id),
                    ],
                }
            ],
        },
    }


@asynccontextmanager
async def files_operation_span(
    *,
    tracer: Any,
    kind: FilesOperationKind,
    file: FilesApiFile | None = None,
    file_id: str | None = None,
    filename: str | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    workspace_id: str | None = None,
    batch_composition: bool | None = None,
    code_execution_composition: bool | None = None,
) -> AsyncGenerator[Any, None]:
    """Open a `files.operation` span with the AS `files.*` attributes.

    The helper accepts either a full `FilesApiFile` or explicit fields. This
    lets future live adapters emit metadata directly after upload/retrieve while
    provider-free tests can exercise reference/delete spans without SDK calls.
    """

    resolved_file_id = file.file_id if file is not None else file_id
    resolved_filename = file.filename if file is not None else filename
    resolved_mime_type = file.mime_type if file is not None else mime_type
    resolved_size_bytes = file.size_bytes if file is not None else size_bytes
    resolved_workspace_id = file.workspace_id if file is not None else workspace_id

    with tracer.start_as_current_span("files.operation") as span:
        span.set_attribute("files.operation.kind", kind.value)
        if resolved_file_id is not None:
            span.set_attribute("files.file_id", resolved_file_id)
        if resolved_filename is not None:
            span.set_attribute("files.filename", resolved_filename)
        if resolved_mime_type is not None:
            span.set_attribute("files.mime_type", resolved_mime_type)
        if resolved_size_bytes is not None:
            span.set_attribute("files.size_bytes", resolved_size_bytes)
        if resolved_workspace_id is not None:
            span.set_attribute("files.workspace_id", resolved_workspace_id)
        if batch_composition is not None:
            span.set_attribute("files.batch_composition", batch_composition)
        if code_execution_composition is not None:
            span.set_attribute("files.code_execution_composition", code_execution_composition)
        yield span
