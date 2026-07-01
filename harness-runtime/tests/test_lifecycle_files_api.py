"""R-810 provider-free Files API contract tests."""

from __future__ import annotations

from io import BytesIO
from typing import BinaryIO

import pytest
from harness_runtime.lifecycle.files_api import (
    ANTHROPIC_FILES_API_BETA,
    AnthropicFilesApiClient,
    FilesApiClientProtocol,
    FilesApiFile,
    FilesOperationKind,
    container_upload_block,
    document_file_block,
    files_message_batch_request,
    files_operation_span,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


class FakeFilesApiClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def upload(
        self,
        *,
        file: BinaryIO,
        filename: str,
        mime_type: str,
    ) -> FilesApiFile:
        data = file.read()
        return FilesApiFile(
            file_id="file_test",
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(data),
            workspace_id="workspace_test",
        )

    async def list_files(self) -> tuple[FilesApiFile, ...]:
        return (
            FilesApiFile(
                file_id="file_test",
                filename="data.csv",
                mime_type="text/csv",
                size_bytes=11,
                workspace_id="workspace_test",
            ),
        )

    async def retrieve_metadata(self, *, file_id: str) -> FilesApiFile:
        return FilesApiFile(
            file_id=file_id,
            filename="data.csv",
            mime_type="text/csv",
            size_bytes=11,
            workspace_id="workspace_test",
        )

    async def delete(self, *, file_id: str) -> None:
        self.deleted.append(file_id)


class _SdkFile:
    def __init__(
        self,
        *,
        file_id: str,
        filename: str = "data.csv",
        mime_type: str = "text/csv",
        size_bytes: int = 8,
        workspace_id: str = "workspace_test",
    ) -> None:
        self.id = file_id
        self.filename = filename
        self.mime_type = mime_type
        self.size_bytes = size_bytes
        self.workspace_id = workspace_id


class _FakeSdkFiles:
    def __init__(self) -> None:
        self.upload_calls: list[tuple[object, list[str]]] = []
        self.list_betas: list[list[str]] = []
        self.retrieve_calls: list[tuple[str, list[str]]] = []
        self.deleted: list[tuple[str, list[str]]] = []

    def upload(self, *, file: object, betas: list[str]) -> _SdkFile:
        self.upload_calls.append((file, betas))
        return _SdkFile(file_id="file_uploaded")

    def list(self, *, betas: list[str]) -> object:
        self.list_betas.append(betas)

        class Page:
            data = [_SdkFile(file_id="file_uploaded")]

        return Page()

    def retrieve_metadata(self, file_id: str, *, betas: list[str]) -> _SdkFile:
        self.retrieve_calls.append((file_id, betas))
        return _SdkFile(file_id=file_id)

    def delete(self, file_id: str, *, betas: list[str]) -> None:
        self.deleted.append((file_id, betas))


class _FakeSdkBeta:
    def __init__(self) -> None:
        self.files = _FakeSdkFiles()


class _FakeSdkClient:
    def __init__(self) -> None:
        self.beta = _FakeSdkBeta()


@pytest.fixture
def tracer_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_beta_header_constant_tracks_anthropic_files_api() -> None:
    assert ANTHROPIC_FILES_API_BETA == "files-api-2025-04-14"


def test_container_upload_block_shapes_reference_content() -> None:
    assert container_upload_block("file_123") == {
        "type": "container_upload",
        "file_id": "file_123",
    }


def test_document_file_block_shapes_reference_content() -> None:
    assert document_file_block("file_123", title="Fixture", context="R-810") == {
        "type": "document",
        "source": {
            "type": "file",
            "file_id": "file_123",
        },
        "title": "Fixture",
        "context": "R-810",
    }


def test_files_message_batch_request_reuses_file_id() -> None:
    request = files_message_batch_request(
        custom_id="r810-live",
        model="claude-haiku-4-5",
        max_tokens=16,
        file_id="file_123",
        prompt="Read the fixture.",
    )

    assert request == {
        "custom_id": "r810-live",
        "params": {
            "model": "claude-haiku-4-5",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read the fixture."},
                        {
                            "type": "document",
                            "source": {
                                "type": "file",
                                "file_id": "file_123",
                            },
                        },
                    ],
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_files_api_protocol_is_provider_free() -> None:
    client: FilesApiClientProtocol = FakeFilesApiClient()

    uploaded = await client.upload(
        file=BytesIO(b"a,b\n1,2\n"),
        filename="data.csv",
        mime_type="text/csv",
    )
    assert uploaded == FilesApiFile(
        file_id="file_test",
        filename="data.csv",
        mime_type="text/csv",
        size_bytes=8,
        workspace_id="workspace_test",
    )

    listed = await client.list_files()
    assert listed[0].file_id == "file_test"
    assert await client.retrieve_metadata(file_id="file_test") == listed[0]
    await client.delete(file_id="file_test")
    assert client.deleted == ["file_test"]


@pytest.mark.asyncio
async def test_anthropic_files_api_client_maps_sdk_and_sets_beta() -> None:
    sdk_client = _FakeSdkClient()
    client = AnthropicFilesApiClient(client=sdk_client)
    content = BytesIO(b"a,b\n1,2\n")

    uploaded = await client.upload(
        file=content,
        filename="data.csv",
        mime_type="text/csv",
    )
    listed = await client.list_files()
    retrieved = await client.retrieve_metadata(file_id="file_uploaded")
    await client.delete(file_id="file_uploaded")

    assert uploaded == FilesApiFile(
        file_id="file_uploaded",
        filename="data.csv",
        mime_type="text/csv",
        size_bytes=8,
        workspace_id="workspace_test",
    )
    assert listed == (uploaded,)
    assert retrieved == uploaded
    upload_file, upload_betas = sdk_client.beta.files.upload_calls[0]
    assert upload_file == ("data.csv", content, "text/csv")
    assert upload_betas == [ANTHROPIC_FILES_API_BETA]
    assert sdk_client.beta.files.list_betas == [[ANTHROPIC_FILES_API_BETA]]
    assert sdk_client.beta.files.retrieve_calls == [("file_uploaded", [ANTHROPIC_FILES_API_BETA])]
    assert sdk_client.beta.files.deleted == [("file_uploaded", [ANTHROPIC_FILES_API_BETA])]


@pytest.mark.asyncio
async def test_files_operation_span_emits_upload_namespace(
    tracer_with_exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = tracer_with_exporter
    tracer = provider.get_tracer(__name__)
    uploaded = FilesApiFile(
        file_id="file_test",
        filename="data.csv",
        mime_type="text/csv",
        size_bytes=8,
        workspace_id="workspace_test",
    )

    async with files_operation_span(
        tracer=tracer,
        kind=FilesOperationKind.UPLOAD,
        file=uploaded,
    ):
        pass

    spans = [span for span in exporter.get_finished_spans() if span.name == "files.operation"]
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["files.operation.kind"] == "upload"
    assert attrs["files.file_id"] == "file_test"
    assert attrs["files.filename"] == "data.csv"
    assert attrs["files.mime_type"] == "text/csv"
    assert attrs["files.size_bytes"] == 8
    assert attrs["files.workspace_id"] == "workspace_test"
    assert "files.batch_composition" not in attrs
    assert "files.code_execution_composition" not in attrs


@pytest.mark.asyncio
async def test_files_operation_span_emits_reference_composition_flags(
    tracer_with_exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = tracer_with_exporter
    tracer = provider.get_tracer(__name__)

    async with files_operation_span(
        tracer=tracer,
        kind=FilesOperationKind.REFERENCE,
        file_id="file_test",
        workspace_id="workspace_test",
        batch_composition=True,
        code_execution_composition=True,
    ):
        pass

    spans = [span for span in exporter.get_finished_spans() if span.name == "files.operation"]
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["files.operation.kind"] == "reference"
    assert attrs["files.file_id"] == "file_test"
    assert attrs["files.workspace_id"] == "workspace_test"
    assert attrs["files.batch_composition"] is True
    assert attrs["files.code_execution_composition"] is True
