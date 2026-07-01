#!/usr/bin/env python3
"""Live R-500 multi-tenant self-hosted e2e against the R-420 collector stack.

This command is intentionally not part of CI. It expects the operator-owned
R-420 Docker Compose telemetry backend to be running locally. It loads the
SELF_HOSTED_SERVER config twice with different non-default tenant IDs and the
MULTI_TENANT_COMPLIANCE persona tier, emits always-sampled audit traces
through the real OTLP exporter, verifies the `tenant.id` resource attribute in
Tempo, verifies pre-collector content redaction, and exercises tenant-scoped
audit-ledger reads over a temporary local JSONL ledger.

No hosted-provider inference, secrets, or paid calls are used.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

DEFAULT_TEMPO_URL = "http://127.0.0.1:3200"
TENANT_A = "r500-tenant-a"
TENANT_B = "r500-tenant-b"
CONTENT_SENTINEL = "r500-content-sentinel-must-not-export"
STRUCTURE_SENTINEL = "r500-structure-sentinel"
ROOT_SPAN = "audit.r500.root"
CHILD_SPAN = "audit.r500.child"


class R500LiveE2EError(RuntimeError):
    """Raised for a failed R-500 live gate."""


@dataclass(frozen=True, slots=True)
class TempoSpanRecord:
    """One Tempo span plus the resource attributes inherited by that span."""

    name: str
    span_attributes: dict[str, Any]
    resource_attributes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _SpanAttributeSnapshot:
    """Adapter for OD span-attribute validators that expect `.attributes`."""

    attributes: Mapping[str, Any]


def _print_step(message: str) -> None:
    print(f"[r500-live] {message}", flush=True)


def _trace_id_hex(trace_id: int) -> str:
    return f"{trace_id:032x}"


def _probe_tcp(url: str, *, timeout_seconds: float) -> None:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise R500LiveE2EError(f"endpoint has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((parsed.hostname, port), timeout=timeout_seconds):
        return None


def _normalise_otel_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        values = value["arrayValue"].get("values", [])
        return [_normalise_otel_value(item) for item in values]
    if "kvlistValue" in value:
        values = value["kvlistValue"].get("values", [])
        return _attributes_to_dict(values)
    return value


def _attributes_to_dict(raw: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            attrs[str(key)] = _normalise_otel_value(value)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not isinstance(key, str):
                continue
            attrs[key] = _normalise_otel_value(item.get("value"))
    return attrs


def _iter_tempo_span_records(
    payload: Any,
    *,
    resource_attributes: Mapping[str, Any] | None = None,
) -> Iterable[TempoSpanRecord]:
    inherited = dict(resource_attributes or {})
    if isinstance(payload, dict):
        merged_resource_attrs = inherited
        resource = payload.get("resource")
        if isinstance(resource, dict):
            merged_resource_attrs = {
                **merged_resource_attrs,
                **_attributes_to_dict(resource.get("attributes")),
            }

        spans = payload.get("spans")
        if isinstance(spans, list):
            for span in spans:
                if not isinstance(span, dict):
                    continue
                name = span.get("name")
                if isinstance(name, str):
                    yield TempoSpanRecord(
                        name=name,
                        span_attributes=_attributes_to_dict(span.get("attributes")),
                        resource_attributes=dict(merged_resource_attrs),
                    )

        for key in ("batches", "resourceSpans", "scopeSpans", "instrumentationLibrarySpans"):
            children = payload.get(key)
            if isinstance(children, list):
                for child in children:
                    yield from _iter_tempo_span_records(
                        child,
                        resource_attributes=merged_resource_attrs,
                    )
            elif isinstance(children, dict):
                yield from _iter_tempo_span_records(
                    children,
                    resource_attributes=merged_resource_attrs,
                )
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_tempo_span_records(item, resource_attributes=inherited)


def _tempo_span_records(payload: dict[str, Any]) -> list[TempoSpanRecord]:
    return list(_iter_tempo_span_records(payload))


def _tempo_trace_payload(
    *,
    tempo_url: str,
    trace_id: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    trace_url = urljoin(tempo_url.rstrip("/") + "/", f"api/traces/{trace_id}")
    try:
        with urlopen(trace_url, timeout=timeout_seconds) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise R500LiveE2EError(f"Tempo trace query failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise R500LiveE2EError(f"Tempo trace query failed for {trace_id}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise R500LiveE2EError(f"Tempo trace query returned non-object JSON: {type(loaded)}")
    return loaded


def _wait_for_trace_records(
    *,
    tempo_url: str,
    trace_id: str,
    expected_names: set[str],
    timeout_seconds: float,
    query_interval_seconds: float,
) -> list[TempoSpanRecord]:
    deadline = time.monotonic() + timeout_seconds
    last_seen: set[str] = set()
    while time.monotonic() < deadline:
        payload = _tempo_trace_payload(
            tempo_url=tempo_url,
            trace_id=trace_id,
            timeout_seconds=min(query_interval_seconds, 5.0),
        )
        if payload is not None:
            records = _tempo_span_records(payload)
            last_seen = {record.name for record in records}
            if expected_names <= last_seen:
                return records
        time.sleep(query_interval_seconds)

    raise R500LiveE2EError(
        f"Tempo never exposed trace {trace_id} with spans {sorted(expected_names)}; "
        f"last_seen={sorted(last_seen)}"
    )


def _load_tenant_config(config_path: Path, *, tenant_id: str) -> Any:
    from harness_core import PersonaTier
    from harness_runtime.config_source import RuntimeConfigSource

    return RuntimeConfigSource.load(
        config_file=config_path,
        cli_overrides={
            "tenant_id": tenant_id,
            "persona_tier": PersonaTier.MULTI_TENANT_COMPLIANCE.value,
        },
    )


def _validate_config_for_r500(config: Any) -> float:
    from harness_core import DeploymentSurface, PersonaTier
    from harness_od.base_rate_set_and_envelope import PER_CELL_BASE_RATE_ENVELOPE
    from harness_od.multi_tenant_trace_separation_and_audit_ledger import (
        PER_TENANT_SEPARATION_BINDINGS,
        TenantSeparationStrategy,
    )
    from harness_od.observability_matrix import CellID, reject_excluded_cell
    from harness_od.redaction_gradient import PER_PERSONA_TIER_REDACTION

    if config.deployment_surface == DeploymentSurface.LOCAL_DEVELOPMENT:
        raise R500LiveE2EError("R-500 requires a non-LOCAL deployment surface")
    if config.deployment_surface != DeploymentSurface.SELF_HOSTED_SERVER:
        raise R500LiveE2EError("this live proof targets the R-420 SELF_HOSTED_SERVER stack")
    if not config.tenant_id:
        raise R500LiveE2EError("R-500 requires a non-empty tenant_id")
    if config.persona_tier is not PersonaTier.MULTI_TENANT_COMPLIANCE:
        raise R500LiveE2EError("R-500 live proof requires MULTI_TENANT_COMPLIANCE")

    cell = CellID(
        persona_tier=config.persona_tier,
        deployment_surface=config.deployment_surface,
    )
    reject_excluded_cell(cell)
    envelope = PER_CELL_BASE_RATE_ENVELOPE[cell]
    if envelope.default_rate != 0.2:
        raise R500LiveE2EError(f"unexpected R-500 base_rate default: {envelope.default_rate}")

    redaction = PER_PERSONA_TIER_REDACTION[config.persona_tier]
    if redaction.toggleable:
        raise R500LiveE2EError("multi-tenant redaction posture must be non-toggleable")

    separation = PER_TENANT_SEPARATION_BINDINGS[cell]
    if separation.tenant_id_attribute != "tenant.id":
        raise R500LiveE2EError("multi-tenant separation key is not tenant.id")
    if separation.strategy is not TenantSeparationStrategy.PER_TENANT_OTLP_COLLECTOR_ROUTING:
        raise R500LiveE2EError("self-hosted multi-tenant cell must use collector routing")
    if not separation.cross_tenant_aggregation_forbidden:
        raise R500LiveE2EError("cross-tenant aggregation must be forbidden")
    return envelope.default_rate


def _emit_tenant_trace(config: Any, *, flush_timeout_millis: int) -> str:
    from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
    from harness_runtime.lifecycle.tracer_provider import materialize_tracer_provider_stage

    provider_stage = materialize_tracer_provider_stage(config, register_globally=False)
    sampler_description = provider_stage.provider.sampler.get_description()
    if "base_rate=0.2" not in sampler_description:
        raise R500LiveE2EError(
            f"R-500 provider sampler missing base_rate=0.2: {sampler_description}"
        )
    if provider_stage.provider.resource.attributes.get("tenant.id") != config.tenant_id:
        raise R500LiveE2EError("provider resource did not carry RuntimeConfig.tenant_id")

    processor_stage = materialize_span_processor_stage(config, provider_stage.provider)
    if processor_stage.tail_keep_processor is None:
        raise R500LiveE2EError("tail-keep wrapper was not engaged for non-LOCAL config")

    tracer = provider_stage.provider.get_tracer("r500-multitenant-selfhosted-live")
    try:
        with tracer.start_as_current_span(ROOT_SPAN) as root:
            trace_id = _trace_id_hex(root.get_span_context().trace_id)
            root.set_attribute("audit.signature.sha256", STRUCTURE_SENTINEL)
            root.set_attribute("gen_ai.input.messages", CONTENT_SENTINEL)
            with tracer.start_as_current_span(CHILD_SPAN) as child:
                child.set_attribute("audit.signature.sha256", STRUCTURE_SENTINEL)
                child.set_attribute("mcp.tool.call.arguments", CONTENT_SENTINEL)
        if not processor_stage.flush(timeout_millis=flush_timeout_millis):
            raise R500LiveE2EError(f"tenant trace force_flush timed out: {config.tenant_id}")
        return trace_id
    finally:
        provider_stage.provider.shutdown()


def _assert_tenant_trace_records(records: Sequence[TempoSpanRecord], *, tenant_id: str) -> None:
    from harness_core import DeploymentSurface, PersonaTier
    from harness_od.content_structure_discipline import DEFAULT_OFF_CONTENT_ATTRIBUTES
    from harness_od.multi_tenant_trace_separation_and_audit_ledger import (
        TenantIdMissingViolation,
        assert_tenant_id_on_every_span_at_multi_tenant_cells,
    )
    from harness_od.observability_matrix import CellID

    cell = CellID(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
    )
    matched = [record for record in records if record.name in {ROOT_SPAN, CHILD_SPAN}]
    if len(matched) != 2:
        raise R500LiveE2EError(f"expected two R-500 records, got {[r.name for r in matched]}")

    for record in matched:
        combined_attrs = {**record.resource_attributes, **record.span_attributes}
        try:
            assert_tenant_id_on_every_span_at_multi_tenant_cells(
                _SpanAttributeSnapshot(attributes=combined_attrs),
                cell,
            )
        except TenantIdMissingViolation as exc:
            raise R500LiveE2EError(str(exc)) from exc
        if record.resource_attributes.get("tenant.id") != tenant_id:
            raise R500LiveE2EError(
                f"Tempo resource tenant mismatch for {record.name}: "
                f"{record.resource_attributes.get('tenant.id')!r}"
            )
        if DEFAULT_OFF_CONTENT_ATTRIBUTES & set(record.span_attributes):
            present = sorted(DEFAULT_OFF_CONTENT_ATTRIBUTES & set(record.span_attributes))
            raise R500LiveE2EError(f"redacted content attributes reached Tempo: {present}")
        if record.span_attributes.get("audit.signature.sha256") != STRUCTURE_SENTINEL:
            raise R500LiveE2EError(f"structure attribute missing from {record.name}")


def _make_audit_entry(entry_hash: str, prior_hash: str = "0" * 64) -> Any:
    from harness_od.audit_ledger_types import (
        AuditLedgerEntry,
        AuditPayload,
        AuditSignatureAttributes,
        SignatureAlgorithm,
        StateLedgerEntryRef,
    )

    return AuditLedgerEntry(
        payload=AuditPayload(
            entry_core=StateLedgerEntryRef(f"r500-entry-ref-{entry_hash[:8]}"),
            audit_namespace_attrs={"audit.actor": "r500-live-proof"},
            prior_entry_hash=prior_hash,
        ),
        signature_attrs=AuditSignatureAttributes(
            audit_signature_value=f"sig:{entry_hash[:8]}",
            audit_signature_algorithm=SignatureAlgorithm.ED25519,
            audit_signature_key_id="r500-live-test-key",
            audit_signature_key_period="2026-Q2",
        ),
        entry_hash=entry_hash,
    )


def _prove_audit_ledger_separation() -> tuple[int, int]:
    from harness_core import DeploymentSurface
    from harness_core.workload_class import WorkloadClass
    from harness_cp.topology_pattern import TopologyPattern
    from harness_is.chain_verification import VerificationStatus, verify_chain
    from harness_is.path_class_registry import PathClass
    from harness_is.path_resolver import PathResolver
    from harness_is.state_ledger_entry_schema import Actor, ActorClass
    from harness_is.state_ledger_write import WriteResult, read_ledger
    from harness_runtime.config.path_bindings import build_path_binding
    from harness_runtime.lifecycle.audit_writer import materialize_audit_writer_stage
    from harness_runtime.lifecycle.state_ledger import materialize_state_ledger
    from harness_runtime.types import (
        CollectorConfig,
        OTelConfig,
        PathBindingConfig,
        ProviderSecretsConfig,
        RuntimeConfig,
    )

    with tempfile.TemporaryDirectory(prefix="r500-audit-ledger-") as temp_dir:
        temp_root = Path(temp_dir)
        path_bindings = PathBindingConfig(
            raw_entries=(
                {
                    "path_class": PathClass.STATE_LEDGER,
                    "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                    "deployment_surface": DeploymentSurface.SELF_HOSTED_SERVER,
                    "path": str(temp_root / "state.jsonl"),
                },
            ),
        )
        resolver = PathResolver(build_path_binding(path_bindings))
        ledger_writer = materialize_state_ledger(
            resolver,
            workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
            deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
            actor=Actor(actor_class=ActorClass.AGENT, actor_id="r500-live-proof"),
        )
        config = RuntimeConfig(
            deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
            repository_root=temp_root,
            path_bindings=path_bindings,
            provider_secrets=ProviderSecretsConfig(),
            otel=OTelConfig(otlp_endpoint="http://127.0.0.1:4317"),
            collector=CollectorConfig(),
            default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        )
        now = datetime(2026, 6, 7, 0, 0, 0, tzinfo=UTC)

        def _tick() -> datetime:
            nonlocal now
            now = now + timedelta(microseconds=1)
            return now

        writer = materialize_audit_writer_stage(
            config,
            ledger_writer,
            time_source=_tick,
        ).writer

        if writer.append(TENANT_A, _make_audit_entry("1" * 64)) is not WriteResult.APPENDED:
            raise R500LiveE2EError("tenant A first audit append failed")
        if writer.append(TENANT_A, _make_audit_entry("2" * 64)) is not WriteResult.APPENDED:
            raise R500LiveE2EError("tenant A second audit append failed")
        if writer.append(TENANT_B, _make_audit_entry("3" * 64)) is not WriteResult.APPENDED:
            raise R500LiveE2EError("tenant B audit append failed")

        a_view = writer.read_for_tenant(TENANT_A)
        b_view = writer.read_for_tenant(TENANT_B)
        if len(a_view) != 2 or len(b_view) != 1:
            raise R500LiveE2EError(
                f"tenant audit views have unexpected sizes: A={len(a_view)} B={len(b_view)}"
            )
        if any(TENANT_B in entry.action_id for entry in a_view):
            raise R500LiveE2EError("tenant B audit entry leaked into tenant A reader")
        if any(TENANT_A in entry.action_id for entry in b_view):
            raise R500LiveE2EError("tenant A audit entry leaked into tenant B reader")
        if writer.read_for_tenant("r500-tenant-missing"):
            raise R500LiveE2EError("unknown tenant audit reader returned entries")

        chain = verify_chain(read_ledger(writer.ledger_writer.handle))
        if chain.status is not VerificationStatus.VALID:
            raise R500LiveE2EError(f"audit ledger IS chain failed verification: {chain.status}")
        return len(a_view), len(b_view)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="SELF_HOSTED_SERVER harness config")
    parser.add_argument("--tempo-url", default=DEFAULT_TEMPO_URL)
    parser.add_argument("--positive-timeout", type=float, default=30.0)
    parser.add_argument("--query-interval", type=float, default=1.0)
    parser.add_argument("--flush-timeout-millis", type=int, default=30_000)
    args = parser.parse_args(argv)

    config_path = args.config.resolve()
    try:
        from harness_runtime.config_source import RuntimeConfigLoadError

        _print_step("loading two non-default tenant configs at MULTI_TENANT_COMPLIANCE")
        tenant_a_config = _load_tenant_config(config_path, tenant_id=TENANT_A)
        tenant_b_config = _load_tenant_config(config_path, tenant_id=TENANT_B)
        base_rate_a = _validate_config_for_r500(tenant_a_config)
        base_rate_b = _validate_config_for_r500(tenant_b_config)

        _print_step("probing local OTLP exporter and Tempo TCP endpoints")
        _probe_tcp(tenant_a_config.otel.otlp_endpoint, timeout_seconds=5)
        _probe_tcp(args.tempo_url, timeout_seconds=5)

        _print_step("emitting tenant A audit trace through OTLP")
        tenant_a_trace_id = _emit_tenant_trace(
            tenant_a_config,
            flush_timeout_millis=args.flush_timeout_millis,
        )
        tenant_a_records = _wait_for_trace_records(
            tempo_url=args.tempo_url,
            trace_id=tenant_a_trace_id,
            expected_names={ROOT_SPAN, CHILD_SPAN},
            timeout_seconds=args.positive_timeout,
            query_interval_seconds=args.query_interval,
        )
        _assert_tenant_trace_records(tenant_a_records, tenant_id=TENANT_A)

        _print_step("emitting tenant B audit trace through OTLP")
        tenant_b_trace_id = _emit_tenant_trace(
            tenant_b_config,
            flush_timeout_millis=args.flush_timeout_millis,
        )
        tenant_b_records = _wait_for_trace_records(
            tempo_url=args.tempo_url,
            trace_id=tenant_b_trace_id,
            expected_names={ROOT_SPAN, CHILD_SPAN},
            timeout_seconds=args.positive_timeout,
            query_interval_seconds=args.query_interval,
        )
        _assert_tenant_trace_records(tenant_b_records, tenant_id=TENANT_B)

        _print_step("exercising tenant-scoped audit-ledger reads")
        tenant_a_audit_entries, tenant_b_audit_entries = _prove_audit_ledger_separation()
    except (R500LiveE2EError, RuntimeConfigLoadError, ModuleNotFoundError, OSError) as exc:
        print(f"R-500 live e2e failed: {exc}", file=sys.stderr)
        return 1

    _print_step(
        "completed: "
        f"tenant_a_trace_id={tenant_a_trace_id} "
        f"tenant_b_trace_id={tenant_b_trace_id} "
        f"base_rate_a={base_rate_a} "
        f"base_rate_b={base_rate_b} "
        f"tenant_a_audit_entries={tenant_a_audit_entries} "
        f"tenant_b_audit_entries={tenant_b_audit_entries} "
        "tenant-resource-separated=true "
        "content-redacted=true "
        "audit-ledger-separated=true "
        "cost=0 hosted-provider-calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
