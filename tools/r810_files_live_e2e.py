#!/usr/bin/env python3
"""Live R-810 Anthropic Files API + managed-cloud telemetry e2e.

This command is intentionally excluded from CI. It performs approved live calls:

* upload/retrieve/reference/delete against Anthropic's beta Files API;
* one short Messages request that references the uploaded file by `file_id`; and
* `files.operation` spans emitted through the configured MANAGED_CLOUD OTLP
  exporter, optionally authenticated with a Cloud Run ID token and verified in
  Cloud Trace.

No secret values are printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import grpc
from harness_core import DeploymentSurface, PersonaTier
from harness_od.per_sandbox_tier_otlp_reachability import (
    ReachabilityViolation,
    assert_otlp_reachable_from_sandbox,
)
from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource
from harness_runtime.lifecycle.files_api import (
    ANTHROPIC_FILES_API_BETA,
    AnthropicFilesApiClient,
    FilesApiFile,
    FilesOperationKind,
    document_file_block,
    files_message_batch_request,
    files_operation_span,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROADMAP_ITEM = "R-810-files-api-integration"
SPAN_NAME = "files.operation"
DEFAULT_MODEL = "claude-haiku-4-5"
SENTINEL = "r810-files-ok"


class R810LiveE2EError(RuntimeError):
    """Raised for a failed R-810 live e2e gate."""


@dataclass(frozen=True)
class FilesLiveResult:
    file: FilesApiFile
    response_text: str
    batch_request: Mapping[str, Any]


@dataclass(frozen=True)
class TraceQueryResult:
    trace_id: str
    observed: bool
    span_names: frozenset[str]
    files_attrs_observed: bool


def _print_step(message: str) -> None:
    print(f"[r810-files-live] {message}", flush=True)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise R810LiveE2EError(f"{name} is required")
    return value


def _assert_managed_cloud_config(config: Any) -> None:
    if config.deployment_surface is not DeploymentSurface.MANAGED_CLOUD:
        raise R810LiveE2EError(
            f"expected managed-cloud deployment surface; observed {config.deployment_surface.value}"
        )
    if config.persona_tier is not PersonaTier.SOLO_DEVELOPER:
        raise R810LiveE2EError("R-810 live proof is deterministic at solo-developer tier")
    _probe_endpoint(config.otel.otlp_endpoint)
    try:
        assert_otlp_reachable_from_sandbox(
            config.collector.bootstrap_sandbox_tier,
            config.collector.placement,
        )
    except ReachabilityViolation as exc:
        raise R810LiveE2EError(
            f"runtime bootstrap OTLP reachability failed before live file upload: {exc}"
        ) from exc


def _probe_endpoint(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise R810LiveE2EError(
            "R-810 managed OTLP endpoint must be an https Cloud Run/managed collector URL"
        )
    if parsed.path not in ("", "/"):
        raise R810LiveE2EError(
            "runtime uses the OTLP gRPC exporter; use the collector service URL "
            "without a /v1/traces suffix"
        )


def _trace_id_hex(trace_id: int) -> str:
    return f"{trace_id:032x}"


def _fetch_cloud_run_id_token(
    *,
    audience: str,
    gcloud_bin: str,
    impersonate_service_account: str | None = None,
) -> str:
    command = [
        gcloud_bin,
        "auth",
        "print-identity-token",
        f"--audiences={audience}",
    ]
    if impersonate_service_account is not None:
        command.append(f"--impersonate-service-account={impersonate_service_account}")
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError) as exc:
        raise R810LiveE2EError(
            f"failed to fetch Cloud Run identity token with {gcloud_bin!r}"
        ) from exc

    token = result.stdout.strip()
    if not token or "\n" in token:
        raise R810LiveE2EError("gcloud returned an invalid Cloud Run identity token")
    return token


def _cloud_run_grpc_credentials(token: str) -> grpc.ChannelCredentials:
    def metadata_callback(_context: Any, callback: Any) -> None:
        callback((("authorization", f"Bearer {token}"),), None)

    return grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.metadata_call_credentials(cast(Any, metadata_callback)),
    )


def _cloud_trace_token() -> str:
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except ModuleNotFoundError as exc:
        raise R810LiveE2EError("google-auth is required for Cloud Trace querying") from exc

    auth_default: Any = google.auth.__dict__["default"]
    credentials, _project = auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(GoogleAuthRequest())
    token = getattr(credentials, "token", None)
    if not isinstance(token, str) or not token:
        raise R810LiveE2EError("ADC did not provide an access token for Cloud Trace")
    return token


def _cloud_trace_payload(
    *,
    project_id: str,
    trace_id: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    encoded_project = quote(project_id, safe="")
    encoded_trace = quote(trace_id, safe="")
    url = f"https://cloudtrace.googleapis.com/v1/projects/{encoded_project}/traces/{encoded_trace}"
    request = Request(
        url,
        headers={"Authorization": f"Bearer {_cloud_trace_token()}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise R810LiveE2EError(f"Cloud Trace query failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise R810LiveE2EError(f"Cloud Trace query failed for {trace_id}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise R810LiveE2EError(f"Cloud Trace query returned non-object JSON: {type(loaded)}")
    return cast(dict[str, Any], loaded)


def _iter_span_dicts(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        payload_mapping = cast(dict[str, Any], payload)
        spans = payload_mapping.get("spans")
        if isinstance(spans, list):
            for item in cast(list[Any], spans):
                if isinstance(item, dict):
                    yield cast(dict[str, Any], item)
        for key in ("batches", "resourceSpans", "scopeSpans", "span"):
            children = payload_mapping.get(key)
            if isinstance(children, list):
                for child in cast(list[Any], children):
                    yield from _iter_span_dicts(child)
            elif isinstance(children, dict):
                yield from _iter_span_dicts(children)
    elif isinstance(payload, list):
        for item in cast(list[Any], payload):
            yield from _iter_span_dicts(item)


def _trace_span_names(payload: dict[str, Any]) -> frozenset[str]:
    names: set[str] = set()
    for span in _iter_span_dicts(payload):
        name = span.get("name")
        if isinstance(name, str):
            names.add(name)
    return frozenset(names)


def _span_has_files_attrs(payload: dict[str, Any]) -> bool:
    for span in _iter_span_dicts(payload):
        if span.get("name") != SPAN_NAME:
            continue
        labels = span.get("labels")
        if isinstance(labels, dict):
            label_mapping = cast(dict[str, Any], labels)
            if any(str(key).startswith("files.") for key in label_mapping):
                return True
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            attribute_mapping = cast(dict[str, Any], attributes)
            if "files.file_id" in str(attribute_mapping):
                return True
    return False


def _response_text(response: Any) -> str:
    raw_content = getattr(response, "content", [])
    content = cast(list[Any], raw_content) if isinstance(raw_content, list) else []
    chunks: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            text = cast(dict[str, Any], block).get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


async def _run_files_round_trip(*, client: Any, model: str) -> FilesLiveResult:
    adapter = AnthropicFilesApiClient(client=client)
    file_id: str | None = None
    uploaded: FilesApiFile | None = None
    fixture = (
        b"R-810 Anthropic Files API live e2e fixture.\n"
        b"The expected sentinel is r810-files-ok.\n"
        b"This plaintext file is uploaded, referenced by file_id, and deleted.\n"
    )
    try:
        uploaded = await adapter.upload(
            file=BytesIO(fixture),
            filename="arhugula-r810-files-live.txt",
            mime_type="text/plain",
        )
        file_id = uploaded.file_id
        _print_step(f"uploaded Anthropic file {file_id}")

        metadata = await adapter.retrieve_metadata(file_id=file_id)
        if metadata.file_id != file_id:
            raise R810LiveE2EError("retrieved file metadata did not match uploaded id")

        batch_request = files_message_batch_request(
            custom_id="r810-files-live",
            model=model,
            max_tokens=32,
            file_id=file_id,
            prompt=f"Read the attached text file and reply with exactly: {SENTINEL}",
        )
        response = await asyncio.to_thread(
            client.beta.messages.create,
            model=model,
            max_tokens=32,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Read the attached text file and reply with exactly: {SENTINEL}"
                            ),
                        },
                        document_file_block(
                            file_id,
                            title="Arhugula R-810 live text fixture",
                            context="Small live e2e fixture.",
                        ),
                    ],
                }
            ],
            betas=[ANTHROPIC_FILES_API_BETA],
        )
        text = _response_text(response)
        if SENTINEL not in text:
            raise R810LiveE2EError("Messages file reference response missed sentinel")
        return FilesLiveResult(file=metadata, response_text=text, batch_request=batch_request)
    finally:
        if file_id is not None:
            try:
                await adapter.delete(file_id=file_id)
                _print_step("deleted uploaded Anthropic file")
            except Exception as exc:
                _print_step(f"file delete skipped: {type(exc).__name__}")


async def _emit_files_trace(
    config: Any,
    *,
    file: FilesApiFile,
    flush_timeout_millis: int,
    cloud_run_auth_audience: str | None,
    gcloud_bin: str,
    impersonate_service_account: str | None,
) -> str:
    from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
    from harness_runtime.lifecycle.tracer_provider import materialize_tracer_provider_stage
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    provider_stage = materialize_tracer_provider_stage(config, register_globally=False)
    exporter = None
    if cloud_run_auth_audience is not None:
        token = _fetch_cloud_run_id_token(
            audience=cloud_run_auth_audience,
            gcloud_bin=gcloud_bin,
            impersonate_service_account=impersonate_service_account,
        )
        exporter = OTLPSpanExporter(
            endpoint=config.otel.otlp_endpoint,
            credentials=_cloud_run_grpc_credentials(token),
        )
    processor_stage = materialize_span_processor_stage(
        config,
        provider_stage.provider,
        exporter=exporter,
    )
    tracer = provider_stage.provider.get_tracer("r810-files-live")
    try:
        with tracer.start_as_current_span("r810.files.live") as root:
            root.set_attribute("roadmap.item", ROADMAP_ITEM)
            root.set_attribute("deployment.surface", "managed-cloud")
            trace_id = _trace_id_hex(root.get_span_context().trace_id)
            async with files_operation_span(
                tracer=tracer,
                kind=FilesOperationKind.UPLOAD,
                file=file,
            ):
                pass
            async with files_operation_span(
                tracer=tracer,
                kind=FilesOperationKind.METADATA,
                file=file,
            ):
                pass
            async with files_operation_span(
                tracer=tracer,
                kind=FilesOperationKind.REFERENCE,
                file=file,
                batch_composition=True,
                code_execution_composition=False,
            ):
                pass
            async with files_operation_span(
                tracer=tracer,
                kind=FilesOperationKind.DELETE,
                file_id=file.file_id,
                filename=file.filename,
                mime_type=file.mime_type,
                size_bytes=file.size_bytes,
                workspace_id=file.workspace_id,
            ):
                pass
        if not processor_stage.flush(timeout_millis=flush_timeout_millis):
            raise R810LiveE2EError("managed OTLP trace force_flush timed out")
        return trace_id
    finally:
        provider_stage.provider.shutdown()


def _wait_for_files_trace(
    *,
    project_id: str,
    trace_id: str,
    timeout_seconds: float,
    query_interval_seconds: float,
) -> TraceQueryResult:
    deadline = time.monotonic() + timeout_seconds
    last_names: frozenset[str] = frozenset()
    last_attrs = False
    while time.monotonic() < deadline:
        payload = _cloud_trace_payload(
            project_id=project_id,
            trace_id=trace_id,
            timeout_seconds=min(query_interval_seconds, 10.0),
        )
        if payload is not None:
            names = _trace_span_names(payload)
            attrs_observed = _span_has_files_attrs(payload)
            if SPAN_NAME in names and attrs_observed:
                return TraceQueryResult(
                    trace_id=trace_id,
                    observed=True,
                    span_names=names,
                    files_attrs_observed=True,
                )
            last_names = names
            last_attrs = attrs_observed
        time.sleep(query_interval_seconds)

    raise R810LiveE2EError(
        f"Cloud Trace never exposed trace {trace_id} with {SPAN_NAME} and "
        f"files.* attrs; last_seen={sorted(last_names)} attrs={last_attrs}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="MANAGED_CLOUD harness config")
    parser.add_argument("--model", default=os.environ.get("R810_FILES_MODEL", DEFAULT_MODEL))
    parser.add_argument("--flush-timeout-millis", type=int, default=30_000)
    parser.add_argument("--trace-query-project", default=None)
    parser.add_argument("--trace-query-timeout", type=float, default=180.0)
    parser.add_argument("--trace-query-interval", type=float, default=5.0)
    parser.add_argument("--cloud-run-auth-audience", default=None)
    parser.add_argument("--gcloud-bin", default="gcloud")
    parser.add_argument("--cloud-run-auth-impersonate-service-account", default=None)
    parser.add_argument(
        "--skip-trace-query",
        action="store_true",
        help="Emit and flush OTLP only; do not poll Cloud Trace for visibility.",
    )
    args = parser.parse_args(argv)

    config_path = args.config.resolve()
    try:
        _require_env("ANTHROPIC_API_KEY")

        _print_step("loading runtime config")
        config = RuntimeConfigSource.load(config_file=config_path)
        _assert_managed_cloud_config(config)

        from anthropic import Anthropic

        anthropic_client = Anthropic()
        _print_step("running real Anthropic Files upload/reference/delete")
        live_result = asyncio.run(_run_files_round_trip(client=anthropic_client, model=args.model))

        _print_step("emitting files.operation spans through managed OTLP")
        trace_id = asyncio.run(
            _emit_files_trace(
                config,
                file=live_result.file,
                flush_timeout_millis=args.flush_timeout_millis,
                cloud_run_auth_audience=args.cloud_run_auth_audience,
                gcloud_bin=args.gcloud_bin,
                impersonate_service_account=args.cloud_run_auth_impersonate_service_account,
            )
        )

        query_result: TraceQueryResult | None = None
        trace_query_project = args.trace_query_project or config.provider_secrets.gcp_project_id
        if not args.skip_trace_query:
            if not trace_query_project:
                raise R810LiveE2EError(
                    "Cloud Trace query requires --trace-query-project or gcp_project_id"
                )
            _print_step("polling Cloud Trace for files.operation")
            query_result = _wait_for_files_trace(
                project_id=trace_query_project,
                trace_id=trace_id,
                timeout_seconds=args.trace_query_timeout,
                query_interval_seconds=args.trace_query_interval,
            )
    except (
        R810LiveE2EError,
        RuntimeConfigLoadError,
        ModuleNotFoundError,
        OSError,
    ) as exc:
        print(f"R-810 Files API live e2e failed: {exc}", file=sys.stderr)
        return 1

    trace_query = (
        "trace-query=skipped"
        if query_result is None
        else (
            "trace-query=observed "
            f"span_names={sorted(query_result.span_names)} "
            f"files_attrs_observed={query_result.files_attrs_observed}"
        )
    )
    _print_step(
        "completed: "
        f"file_id={live_result.file.file_id} "
        f"filename={live_result.file.filename} "
        f"size_bytes={live_result.file.size_bytes} "
        f"response_contains_sentinel={SENTINEL in live_result.response_text} "
        f"batch_custom_id={live_result.batch_request['custom_id']} "
        f"trace_id={trace_id} "
        "files-api-upload=true "
        "files-api-reference=true "
        "files-api-delete=true "
        "files-batch-composition=true "
        "files-otlp-export=true "
        f"{trace_query} "
        "hosted-provider-calls=1 "
        "cost=usage-billed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
