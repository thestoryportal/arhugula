#!/usr/bin/env python3
"""Live R-421 managed-cloud e2e.

This command is intentionally excluded from CI. It performs approved live calls:

* static MANAGED_CLOUD readiness against a runtime config;
* GCP Secret Manager resolution of `e2b-secret` through the runtime backend;
* one short-lived usage-billed E2B hosted sandbox command; and
* one OTLP gRPC trace emitted to the configured managed collector.

When `--trace-query-project` is present, it also polls Cloud Trace for the
emitted trace ID using application-default credentials. No secret values are
printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import grpc
from harness_core import DeploymentSurface, PersonaTier
from harness_od.base_rate_set_and_envelope import PER_CELL_BASE_RATE_ENVELOPE
from harness_od.observability_matrix import CellID
from harness_od.per_sandbox_tier_otlp_reachability import (
    ReachabilityViolation,
    assert_otlp_reachable_from_sandbox,
)
from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.managed_cloud_readiness import load_report  # noqa: E402
from tools.r421_e2b_live_probe import (  # noqa: E402
    E2B_SECRET_NAME,
    LiveProbeError,
    _load_sandbox_class,
    resolve_e2b_secret_from_config,
    run_probe,
)

ROOT_SPAN = "r421.managed_cloud.root"
TRIGGER_SPAN = "sandbox.violation"
ROADMAP_ITEM = "R-421-managed-cloud-deployment-e2e"


class R421LiveE2EError(RuntimeError):
    """Raised for a failed R-421 live e2e gate."""


@dataclass(frozen=True)
class TraceQueryResult:
    trace_id: str
    observed: bool
    span_names: frozenset[str]


def _print_step(message: str) -> None:
    print(f"[r421-managed-live] {message}", flush=True)


def _trace_id_hex(trace_id: int) -> str:
    return f"{trace_id:032x}"


def _probe_endpoint(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise R421LiveE2EError(
            "R-421 managed OTLP endpoint must be an https Cloud Run/managed collector URL"
        )
    if parsed.path not in ("", "/"):
        raise R421LiveE2EError(
            "runtime uses the OTLP gRPC exporter; use the collector service URL "
            "without a /v1/traces suffix"
        )


def _assert_static_ready(config_file: Path) -> None:
    report = load_report(config_file, hosted_sandbox_provider="e2b")
    if report.ready:
        return
    failures = "; ".join(f"{check.name}: {check.detail}" for check in report.checks if not check.ok)
    raise R421LiveE2EError(f"static managed-cloud readiness failed: {failures}")


def _assert_deterministic_managed_cell(config: Any) -> None:
    if config.deployment_surface is not DeploymentSurface.MANAGED_CLOUD:
        raise R421LiveE2EError(
            f"expected managed-cloud deployment surface; observed {config.deployment_surface.value}"
        )
    if config.persona_tier is not PersonaTier.SOLO_DEVELOPER:
        raise R421LiveE2EError(
            "R-421 live proof is deterministic only at solo-developer persona tier"
        )
    cell = CellID(
        persona_tier=config.persona_tier,
        deployment_surface=config.deployment_surface,
    )
    default_rate = PER_CELL_BASE_RATE_ENVELOPE[cell].default_rate
    if default_rate != 1.0:
        raise R421LiveE2EError(
            "R-421 live proof is deterministic only at base_rate=1.0; "
            f"{cell} default_rate={default_rate}"
        )


def _assert_runtime_bootstrap_can_reach_collector(config: Any) -> None:
    try:
        assert_otlp_reachable_from_sandbox(
            config.collector.bootstrap_sandbox_tier,
            config.collector.placement,
        )
    except ReachabilityViolation as exc:
        raise R421LiveE2EError(
            f"runtime bootstrap OTLP reachability failed before E2B sandbox creation: {exc}"
        ) from exc


def _run_hosted_e2b_probe(
    *,
    config_file: Path,
    secret_name: str,
    sandbox_timeout_seconds: int,
    command_timeout_seconds: int,
) -> None:
    previous_key = os.environ.get("E2B_API_KEY")
    try:
        os.environ["E2B_API_KEY"] = resolve_e2b_secret_from_config(
            config_file,
            secret_name=secret_name,
        )
        _print_step(f"resolved {secret_name} from provider-secret backend (redacted)")
        sandbox_cls = _load_sandbox_class()
        stdout = run_probe(
            sandbox_cls=sandbox_cls,
            command="printf r421-e2b-ok",
            sandbox_timeout_seconds=sandbox_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )
    except LiveProbeError as exc:
        raise R421LiveE2EError(str(exc)) from exc
    finally:
        if previous_key is None:
            os.environ.pop("E2B_API_KEY", None)
        else:
            os.environ["E2B_API_KEY"] = previous_key

    if stdout != "r421-e2b-ok":
        raise R421LiveE2EError(f"unexpected E2B stdout {stdout!r}; expected 'r421-e2b-ok'")


def _emit_trigger_trace(config: Any, *, flush_timeout_millis: int) -> str:
    from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
    from harness_runtime.lifecycle.tracer_provider import materialize_tracer_provider_stage

    provider_stage = materialize_tracer_provider_stage(config, register_globally=False)
    processor_stage = materialize_span_processor_stage(config, provider_stage.provider)
    if processor_stage.tail_keep_processor is None:
        raise R421LiveE2EError("tail-keep wrapper was not engaged for managed-cloud config")

    tracer = provider_stage.provider.get_tracer("r421-managed-cloud-live")
    try:
        with tracer.start_as_current_span(ROOT_SPAN) as root:
            root.set_attribute("roadmap.item", ROADMAP_ITEM)
            root.set_attribute("deployment.surface", "managed-cloud")
            trace_id = _trace_id_hex(root.get_span_context().trace_id)
            with tracer.start_as_current_span(TRIGGER_SPAN) as trigger:
                trigger.set_attribute("roadmap.item", ROADMAP_ITEM)
                trigger.set_attribute("sandbox.violation.kind", "r421-managed-cloud-live")
        if not processor_stage.flush(timeout_millis=flush_timeout_millis):
            raise R421LiveE2EError("managed OTLP trace force_flush timed out")
        return trace_id
    finally:
        provider_stage.provider.shutdown()


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
        raise R421LiveE2EError(
            f"failed to fetch Cloud Run identity token with {gcloud_bin!r}"
        ) from exc

    token = result.stdout.strip()
    if not token or "\n" in token:
        raise R421LiveE2EError("gcloud returned an invalid Cloud Run identity token")
    return token


def _cloud_run_grpc_credentials(token: str) -> grpc.ChannelCredentials:
    def metadata_callback(_context: Any, callback: Any) -> None:
        callback((("authorization", f"Bearer {token}"),), None)

    return grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.metadata_call_credentials(metadata_callback),
    )


def _emit_authenticated_trigger_trace(
    config: Any,
    *,
    flush_timeout_millis: int,
    audience: str,
    gcloud_bin: str,
    impersonate_service_account: str | None,
) -> str:
    from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
    from harness_runtime.lifecycle.tracer_provider import materialize_tracer_provider_stage
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    token = _fetch_cloud_run_id_token(
        audience=audience,
        gcloud_bin=gcloud_bin,
        impersonate_service_account=impersonate_service_account,
    )
    provider_stage = materialize_tracer_provider_stage(config, register_globally=False)
    exporter = OTLPSpanExporter(
        endpoint=config.otel.otlp_endpoint,
        credentials=_cloud_run_grpc_credentials(token),
    )
    processor_stage = materialize_span_processor_stage(
        config,
        provider_stage.provider,
        exporter=exporter,
    )
    if processor_stage.tail_keep_processor is None:
        raise R421LiveE2EError("tail-keep wrapper was not engaged for managed-cloud config")

    tracer = provider_stage.provider.get_tracer("r421-managed-cloud-live")
    try:
        with tracer.start_as_current_span(ROOT_SPAN) as root:
            root.set_attribute("roadmap.item", ROADMAP_ITEM)
            root.set_attribute("deployment.surface", "managed-cloud")
            trace_id = _trace_id_hex(root.get_span_context().trace_id)
            with tracer.start_as_current_span(TRIGGER_SPAN) as trigger:
                trigger.set_attribute("roadmap.item", ROADMAP_ITEM)
                trigger.set_attribute("sandbox.violation.kind", "r421-managed-cloud-live")
        if not processor_stage.flush(timeout_millis=flush_timeout_millis):
            raise R421LiveE2EError("managed OTLP trace force_flush timed out")
        return trace_id
    finally:
        provider_stage.provider.shutdown()


def _iter_span_dicts(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        spans = payload.get("spans")
        if isinstance(spans, list):
            for item in spans:
                if isinstance(item, dict):
                    yield item
        for key in ("batches", "resourceSpans", "scopeSpans", "span"):
            children = payload.get(key)
            if isinstance(children, list):
                for child in children:
                    yield from _iter_span_dicts(child)
            elif isinstance(children, dict):
                yield from _iter_span_dicts(children)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_span_dicts(item)


def _trace_span_names(payload: dict[str, Any]) -> frozenset[str]:
    names: set[str] = set()
    for span in _iter_span_dicts(payload):
        name = span.get("name")
        if isinstance(name, str):
            names.add(name)
    return frozenset(names)


def _cloud_trace_token() -> str:
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except ModuleNotFoundError as exc:
        raise R421LiveE2EError("google-auth is required for Cloud Trace querying") from exc

    credentials, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(GoogleAuthRequest())
    token = getattr(credentials, "token", None)
    if not isinstance(token, str) or not token:
        raise R421LiveE2EError("ADC did not provide an access token for Cloud Trace")
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
        raise R421LiveE2EError(f"Cloud Trace query failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise R421LiveE2EError(f"Cloud Trace query failed for {trace_id}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise R421LiveE2EError(f"Cloud Trace query returned non-object JSON: {type(loaded)}")
    return loaded


def _wait_for_cloud_trace(
    *,
    project_id: str,
    trace_id: str,
    timeout_seconds: float,
    query_interval_seconds: float,
) -> TraceQueryResult:
    deadline = time.monotonic() + timeout_seconds
    last_names: frozenset[str] = frozenset()
    while time.monotonic() < deadline:
        payload = _cloud_trace_payload(
            project_id=project_id,
            trace_id=trace_id,
            timeout_seconds=min(query_interval_seconds, 10.0),
        )
        if payload is not None:
            names = _trace_span_names(payload)
            if not names or {ROOT_SPAN, TRIGGER_SPAN} <= names:
                return TraceQueryResult(trace_id=trace_id, observed=True, span_names=names)
            last_names = names
        time.sleep(query_interval_seconds)

    raise R421LiveE2EError(
        f"Cloud Trace never exposed trace {trace_id} with expected spans; "
        f"last_seen={sorted(last_names)}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="MANAGED_CLOUD harness config")
    parser.add_argument("--secret-name", default=E2B_SECRET_NAME)
    parser.add_argument("--sandbox-timeout", type=int, default=60)
    parser.add_argument("--command-timeout", type=int, default=15)
    parser.add_argument("--flush-timeout-millis", type=int, default=30_000)
    parser.add_argument(
        "--trace-query-project",
        default=None,
        help=(
            "GCP project ID for Cloud Trace polling. Defaults to the config "
            "provider_secrets.gcp_project_id when omitted."
        ),
    )
    parser.add_argument("--trace-query-timeout", type=float, default=180.0)
    parser.add_argument("--trace-query-interval", type=float, default=5.0)
    parser.add_argument(
        "--cloud-run-auth-audience",
        default=None,
        help=(
            "Cloud Run service URL/audience. When set, the e2e fetches a "
            "short-lived ID token with gcloud and sends it as an OTLP "
            "Authorization header."
        ),
    )
    parser.add_argument(
        "--gcloud-bin",
        default="gcloud",
        help="gcloud executable used for --cloud-run-auth-audience token fetch.",
    )
    parser.add_argument(
        "--cloud-run-auth-impersonate-service-account",
        default=None,
        help=(
            "Optional service account for gcloud auth print-identity-token "
            "impersonation. Needed when active gcloud auth is a user account."
        ),
    )
    parser.add_argument(
        "--skip-trace-query",
        action="store_true",
        help="Emit and flush OTLP only; do not poll Cloud Trace for visibility.",
    )
    args = parser.parse_args(argv)

    config_path = args.config.resolve()
    try:
        _print_step("checking static MANAGED_CLOUD readiness")
        _assert_static_ready(config_path)

        _print_step("loading runtime config")
        config = RuntimeConfigSource.load(config_file=config_path)
        _assert_deterministic_managed_cell(config)
        _probe_endpoint(config.otel.otlp_endpoint)
        _assert_runtime_bootstrap_can_reach_collector(config)

        _print_step("creating hosted E2B sandbox and running deterministic command")
        _run_hosted_e2b_probe(
            config_file=config_path,
            secret_name=args.secret_name,
            sandbox_timeout_seconds=args.sandbox_timeout,
            command_timeout_seconds=args.command_timeout,
        )

        _print_step("emitting classification-trigger trace through managed OTLP")
        if args.cloud_run_auth_audience:
            trace_id = _emit_authenticated_trigger_trace(
                config,
                flush_timeout_millis=args.flush_timeout_millis,
                audience=args.cloud_run_auth_audience,
                gcloud_bin=args.gcloud_bin,
                impersonate_service_account=args.cloud_run_auth_impersonate_service_account,
            )
        else:
            trace_id = _emit_trigger_trace(
                config,
                flush_timeout_millis=args.flush_timeout_millis,
            )

        trace_query_project = args.trace_query_project or config.provider_secrets.gcp_project_id
        query_result: TraceQueryResult | None = None
        if not args.skip_trace_query:
            if not trace_query_project:
                raise R421LiveE2EError(
                    "Cloud Trace query requires --trace-query-project or gcp_project_id"
                )
            _print_step("polling Cloud Trace for emitted trace")
            query_result = _wait_for_cloud_trace(
                project_id=trace_query_project,
                trace_id=trace_id,
                timeout_seconds=args.trace_query_timeout,
                query_interval_seconds=args.trace_query_interval,
            )
    except (
        R421LiveE2EError,
        RuntimeConfigLoadError,
        ModuleNotFoundError,
        OSError,
    ) as exc:
        print(f"R-421 managed-cloud live e2e failed: {exc}", file=sys.stderr)
        return 1

    trace_query = (
        "trace-query=skipped"
        if query_result is None
        else f"trace-query=observed span_names={sorted(query_result.span_names)}"
    )
    _print_step(
        "completed: "
        f"trace_id={trace_id} "
        "managed-otlp-export=true "
        f"{trace_query} "
        "e2b-sandbox=true "
        "hosted-provider-calls=1 "
        "cost=usage-billed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
