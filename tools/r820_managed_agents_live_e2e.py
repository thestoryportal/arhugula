#!/usr/bin/env python3
"""Live R-820 Anthropic Managed Agents + managed-cloud telemetry e2e.

This command is intentionally excluded from CI. It performs approved live calls:

* one short Anthropic Managed Agents session using either supplied agent and
  environment IDs or disposable resources created by the command; and
* one `managed_agents.runtime` span emitted through the configured MANAGED_CLOUD
  OTLP exporter, optionally authenticated with a Cloud Run ID token and verified
  in Cloud Trace.

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import grpc
from harness_core import DeploymentSurface, PersonaTier
from harness_od.per_sandbox_tier_otlp_reachability import (
    ReachabilityViolation,
    assert_otlp_reachable_from_sandbox,
)
from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource
from harness_runtime.lifecycle.managed_agents import (
    ANTHROPIC_MANAGED_AGENTS_BETA,
    AnthropicManagedAgentsClient,
    ManagedAgentEvent,
    ManagedAgentSession,
    ManagedAgentSessionStatus,
    managed_agents_runtime_span,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROADMAP_ITEM = "R-820-managed-agents-integration"
SPAN_NAME = "managed_agents.runtime"
DEFAULT_MODEL = "claude-haiku-4-5"


class R820LiveE2EError(RuntimeError):
    """Raised for a failed R-820 live e2e gate."""


@dataclass(frozen=True)
class ManagedAgentsLiveResult:
    session: ManagedAgentSession
    event_types: tuple[str, ...]
    agent_created: bool
    environment_created: bool


@dataclass(frozen=True)
class TraceQueryResult:
    trace_id: str
    observed: bool
    span_names: frozenset[str]
    managed_attrs_observed: bool


def _print_step(message: str) -> None:
    print(f"[r820-managed-agents-live] {message}", flush=True)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise R820LiveE2EError(f"{name} is required")
    return value


def _assert_managed_cloud_config(config: Any) -> None:
    if config.deployment_surface is not DeploymentSurface.MANAGED_CLOUD:
        raise R820LiveE2EError(
            f"expected managed-cloud deployment surface; observed {config.deployment_surface.value}"
        )
    if config.persona_tier is not PersonaTier.SOLO_DEVELOPER:
        raise R820LiveE2EError(
            "R-820 live proof is deterministic only at solo-developer persona tier"
        )
    _probe_endpoint(config.otel.otlp_endpoint)
    _assert_runtime_bootstrap_can_reach_collector(config)


def _trace_id_hex(trace_id: int) -> str:
    return f"{trace_id:032x}"


def _probe_endpoint(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise R820LiveE2EError(
            "R-820 managed OTLP endpoint must be an https Cloud Run/managed collector URL"
        )
    if parsed.path not in ("", "/"):
        raise R820LiveE2EError(
            "runtime uses the OTLP gRPC exporter; use the collector service URL "
            "without a /v1/traces suffix"
        )


def _assert_runtime_bootstrap_can_reach_collector(config: Any) -> None:
    try:
        assert_otlp_reachable_from_sandbox(
            config.collector.bootstrap_sandbox_tier,
            config.collector.placement,
        )
    except ReachabilityViolation as exc:
        raise R820LiveE2EError(
            f"runtime bootstrap OTLP reachability failed before live session creation: {exc}"
        ) from exc


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
        raise R820LiveE2EError(
            f"failed to fetch Cloud Run identity token with {gcloud_bin!r}"
        ) from exc

    token = result.stdout.strip()
    if not token or "\n" in token:
        raise R820LiveE2EError("gcloud returned an invalid Cloud Run identity token")
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
        raise R820LiveE2EError("google-auth is required for Cloud Trace querying") from exc

    auth_default: Any = google.auth.__dict__["default"]
    credentials, _project = auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(GoogleAuthRequest())
    token = getattr(credentials, "token", None)
    if not isinstance(token, str) or not token:
        raise R820LiveE2EError("ADC did not provide an access token for Cloud Trace")
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
        raise R820LiveE2EError(f"Cloud Trace query failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise R820LiveE2EError(f"Cloud Trace query failed for {trace_id}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise R820LiveE2EError(f"Cloud Trace query returned non-object JSON: {type(loaded)}")
    return cast(dict[str, Any], loaded)


def _trace_span_names(payload: dict[str, Any]) -> frozenset[str]:
    names: set[str] = set()
    for span in _iter_span_dicts(payload):
        name = span.get("name")
        if isinstance(name, str):
            names.add(name)
    return frozenset(names)


def _event_type(event: Any) -> str:
    if isinstance(event, dict):
        event_mapping = cast(dict[str, Any], event)
        return str(event_mapping.get("type", ""))
    return str(getattr(event, "type", ""))


def _iter_text_blocks(event: Any) -> Iterable[str]:
    if isinstance(event, dict):
        event_mapping = cast(dict[str, Any], event)
        raw_content: object = event_mapping.get("content", [])
    else:
        raw_content = getattr(event, "content", [])
    content = cast(list[Any], raw_content) if isinstance(raw_content, list) else []
    for block in content:
        if isinstance(block, dict):
            block_mapping = cast(dict[str, Any], block)
            text = block_mapping.get("text")
        else:
            text = getattr(block, "text", None)
        if isinstance(text, str):
            yield text


def _create_disposable_agent(client: Any, *, model: str, suffix: str) -> str:
    agent = client.beta.agents.create(
        name=f"arhugula-r820-{suffix}",
        model=model,
        system=(
            "You are a minimal test agent. Answer the user's request directly and do not use tools."
        ),
        metadata={"roadmap_item": "R-820", "created_by": "arhugula-live-e2e"},
        betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
    )
    return str(agent.id)


def _create_disposable_environment(client: Any, *, suffix: str) -> str:
    environment = client.beta.environments.create(
        name=f"arhugula-r820-{suffix}",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
        },
        metadata={"roadmap_item": "R-820", "created_by": "arhugula-live-e2e"},
        betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
    )
    return str(environment.id)


def _cleanup_disposable_resources(
    client: Any,
    *,
    session_id: str | None,
    agent_id: str | None,
    environment_id: str | None,
) -> None:
    if session_id:
        try:
            client.beta.sessions.delete(
                session_id,
                betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            )
        except Exception as exc:
            _print_step(f"session delete skipped: {type(exc).__name__}")
            try:
                client.beta.sessions.archive(
                    session_id,
                    betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
                )
            except Exception as archive_exc:
                _print_step(f"session archive skipped: {type(archive_exc).__name__}")
    if agent_id:
        try:
            client.beta.agents.archive(agent_id, betas=[ANTHROPIC_MANAGED_AGENTS_BETA])
        except Exception as exc:
            _print_step(f"agent archive skipped: {type(exc).__name__}")
    if environment_id:
        try:
            client.beta.environments.delete(
                environment_id,
                betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            )
        except Exception as exc:
            _print_step(f"environment delete skipped: {type(exc).__name__}")
            try:
                client.beta.environments.archive(
                    environment_id,
                    betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
                )
            except Exception as archive_exc:
                _print_step(f"environment archive skipped: {type(archive_exc).__name__}")


async def _run_managed_agents_session(
    *,
    client: Any,
    agent_id: str | None,
    environment_id: str | None,
    model: str,
    session_timeout_seconds: float,
) -> ManagedAgentsLiveResult:
    suffix = uuid4().hex[:12]
    created_agent_id: str | None = None
    created_environment_id: str | None = None
    session_id: str | None = None
    event_types: list[str] = []
    text_chunks: list[str] = []

    try:
        if agent_id is None:
            agent_id = _create_disposable_agent(client, model=model, suffix=suffix)
            created_agent_id = agent_id
            _print_step("created disposable Managed Agents agent")
        if environment_id is None:
            environment_id = _create_disposable_environment(client, suffix=suffix)
            created_environment_id = environment_id
            _print_step("created disposable Managed Agents environment")

        adapter = AnthropicManagedAgentsClient(client=client)
        session = await adapter.create_session(
            agent_id=agent_id,
            environment_id=environment_id,
            title="Arhugula R-820 live e2e",
            metadata={"roadmap_item": "R-820"},
        )
        session_id = session.session_id
        _print_step(f"created Managed Agents session {session_id}")

        with client.beta.sessions.events.stream(
            session_id,
            betas=[ANTHROPIC_MANAGED_AGENTS_BETA],
            timeout=session_timeout_seconds,
        ) as stream:
            await adapter.send_event(
                session_id=session_id,
                event=ManagedAgentEvent(
                    event_type="user.message",
                    payload={
                        "content": [
                            {
                                "type": "text",
                                "text": "Reply with exactly: r820-managed-agents-ok",
                            }
                        ]
                    },
                ),
            )
            deadline = time.monotonic() + session_timeout_seconds
            for event in stream:
                event_type = _event_type(event)
                if event_type:
                    event_types.append(event_type)
                text_chunks.extend(_iter_text_blocks(event))
                if event_type == "session.status_idle":
                    break
                if event_type in {"session.status_terminated", "session.error"}:
                    raise R820LiveE2EError(f"Managed Agents session failed: {event_type}")
                if time.monotonic() > deadline:
                    raise R820LiveE2EError("timed out waiting for session.status_idle")

        retrieved = await adapter.retrieve_session(session_id=session_id)
        if retrieved.status not in {
            ManagedAgentSessionStatus.IDLE,
            ManagedAgentSessionStatus.RUNNING,
        }:
            raise R820LiveE2EError(f"unexpected final session status {retrieved.status.value}")
        if "session.status_idle" not in event_types:
            raise R820LiveE2EError("stream ended without session.status_idle")
        if "r820-managed-agents-ok" not in "".join(text_chunks):
            _print_step("agent response text did not contain exact sentinel; stream still idled")
        return ManagedAgentsLiveResult(
            session=retrieved,
            event_types=tuple(event_types),
            agent_created=created_agent_id is not None,
            environment_created=created_environment_id is not None,
        )
    finally:
        _cleanup_disposable_resources(
            client,
            session_id=session_id,
            agent_id=created_agent_id,
            environment_id=created_environment_id,
        )


async def _emit_managed_agents_trace(
    config: Any,
    *,
    session: ManagedAgentSession,
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
    tracer = provider_stage.provider.get_tracer("r820-managed-agents-live")
    try:
        async with managed_agents_runtime_span(tracer=tracer, session=session) as span:
            span.set_attribute("roadmap.item", ROADMAP_ITEM)
            span.set_attribute("deployment.surface", "managed-cloud")
            trace_id = _trace_id_hex(span.get_span_context().trace_id)
        if not processor_stage.flush(timeout_millis=flush_timeout_millis):
            raise R820LiveE2EError("managed OTLP trace force_flush timed out")
        return trace_id
    finally:
        provider_stage.provider.shutdown()


def _span_has_managed_attrs(payload: dict[str, Any]) -> bool:
    for span in _iter_span_dicts(payload):
        if span.get("name") != SPAN_NAME:
            continue
        labels = span.get("labels")
        if isinstance(labels, dict):
            label_mapping = cast(dict[str, Any], labels)
            if any("managed_agents." in str(key) for key in label_mapping):
                return True
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            attribute_mapping = cast(dict[str, Any], attributes)
            if "managed_agents.session_id" in str(attribute_mapping):
                return True
    return False


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


def _wait_for_managed_agents_trace(
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
            attrs_observed = _span_has_managed_attrs(payload)
            if SPAN_NAME in names and attrs_observed:
                return TraceQueryResult(
                    trace_id=trace_id,
                    observed=True,
                    span_names=names,
                    managed_attrs_observed=True,
                )
            last_names = names
            last_attrs = attrs_observed
        time.sleep(query_interval_seconds)

    raise R820LiveE2EError(
        f"Cloud Trace never exposed trace {trace_id} with {SPAN_NAME} and "
        f"managed_agents.* attrs; last_seen={sorted(last_names)} attrs={last_attrs}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="MANAGED_CLOUD harness config")
    parser.add_argument("--agent-id", default=os.environ.get("R820_MANAGED_AGENT_ID"))
    parser.add_argument(
        "--environment-id",
        default=os.environ.get("R820_MANAGED_ENVIRONMENT_ID"),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("R820_MANAGED_AGENT_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument("--session-timeout", type=float, default=180.0)
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
        _print_step("running real Anthropic Managed Agents session")
        live_result = asyncio.run(
            _run_managed_agents_session(
                client=anthropic_client,
                agent_id=args.agent_id,
                environment_id=args.environment_id,
                model=args.model,
                session_timeout_seconds=args.session_timeout,
            )
        )

        _print_step("emitting managed_agents.runtime span through managed OTLP")
        trace_id = asyncio.run(
            _emit_managed_agents_trace(
                config,
                session=live_result.session,
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
                raise R820LiveE2EError(
                    "Cloud Trace query requires --trace-query-project or gcp_project_id"
                )
            _print_step("polling Cloud Trace for managed_agents.runtime")
            query_result = _wait_for_managed_agents_trace(
                project_id=trace_query_project,
                trace_id=trace_id,
                timeout_seconds=args.trace_query_timeout,
                query_interval_seconds=args.trace_query_interval,
            )
    except (
        R820LiveE2EError,
        RuntimeConfigLoadError,
        ModuleNotFoundError,
        OSError,
    ) as exc:
        print(f"R-820 Managed Agents live e2e failed: {exc}", file=sys.stderr)
        return 1

    trace_query = (
        "trace-query=skipped"
        if query_result is None
        else (
            "trace-query=observed "
            f"span_names={sorted(query_result.span_names)} "
            f"managed_attrs_observed={query_result.managed_attrs_observed}"
        )
    )
    _print_step(
        "completed: "
        f"session_id={live_result.session.session_id} "
        f"session_status={live_result.session.status.value} "
        f"runtime_ms={live_result.session.runtime_ms} "
        f"billable_seconds={live_result.session.billable_seconds} "
        f"event_types={list(live_result.event_types)} "
        f"trace_id={trace_id} "
        "managed-agents-session=true "
        "managed-otlp-export=true "
        f"{trace_query} "
        "hosted-provider-calls=1 "
        "cost=usage-billed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
