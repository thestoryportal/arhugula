"""U-RT-28 — BatchSpanProcessor + OTLPSpanExporter attach tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L6 U-RT-28:
  #1 spans flush on demand.
     -> test_flush_returns_true_with_no_buffered_spans
     -> test_flush_drives_exporter_for_emitted_span
     -> test_processor_uses_collector_config_batch_size_and_window
  #2 reachability matrix enforced.
     -> test_bootstrap_in_process_placement_passes_reachability
     -> test_bootstrap_self_hosted_backend_collector_passes_reachability
     -> test_bootstrap_sidecar_placement_raises_reachability_error
     -> test_bootstrap_vendor_pipeline_placement_raises_reachability_error
     -> test_assert_otlp_reachable_table_covers_4_sandbox_tiers (canonical check)

Plus composer plumbing + invariants:
  -> test_materialize_returns_stage_with_processor_and_exporter
  -> test_materialize_attaches_processor_to_provider
  -> test_materialize_default_exporter_is_otlp_grpc
  -> test_span_processor_stage_is_frozen
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import DeploymentSurface, PersonaTier
from harness_cp.topology_pattern import TopologyPattern
from harness_od.per_cell_collector_placement_matrix import CollectorPlacement
from harness_od.per_sandbox_tier_otlp_reachability import (
    PER_SANDBOX_TIER_REACHABILITY,
)
from harness_od.redaction_span_processor import RedactionSpanProcessor
from harness_runtime.lifecycle.span_processor import (
    SpanProcessorReachabilityError,
    SpanProcessorStage,
    materialize_span_processor_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _config(
    tmp_path: Path,
    *,
    placement: CollectorPlacement = CollectorPlacement.IN_PROCESS,
    bootstrap_sandbox_tier: SandboxTier = SandboxTier.TIER_1_PROCESS,
    batch_size: int = 512,
    batch_window_seconds: int = 5,
    persona_tier: PersonaTier = PersonaTier.SOLO_DEVELOPER,
    tenant_id: str | None = None,
) -> RuntimeConfig:
    """Build a minimal `RuntimeConfig` for U-RT-28 tests."""
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(
            placement=placement,
            bootstrap_sandbox_tier=bootstrap_sandbox_tier,
            batch_size=batch_size,
            batch_window_seconds=batch_window_seconds,
        ),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        persona_tier=persona_tier,
        tenant_id=tenant_id,
    )


def _provider() -> TracerProvider:
    """Build a bare `TracerProvider` for tests (does not register globally)."""
    return TracerProvider()


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.appended: list[tuple[str | None, object]] = []

    def append(self, tenant_id: str | None, audit_entry: object) -> Any:
        self.appended.append((tenant_id, audit_entry))
        return "appended"


# ---------------------------------------------------------------------------
# AC #1 — spans flush on demand.
# ---------------------------------------------------------------------------


def test_flush_returns_true_with_no_buffered_spans(tmp_path: Path) -> None:
    """A no-op `flush()` (no spans emitted) returns True within timeout."""
    stage = materialize_span_processor_stage(
        _config(tmp_path), _provider(), exporter=InMemorySpanExporter()
    )
    assert stage.flush(timeout_millis=5000) is True


def test_flush_drives_exporter_for_emitted_span(tmp_path: Path) -> None:
    """After emitting a span and calling flush, the exporter has received it."""
    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(_config(tmp_path), provider, exporter=in_memory)
    tracer = provider.get_tracer("u-rt-28-test")
    with tracer.start_as_current_span("test-span"):
        pass
    assert stage.flush(timeout_millis=5000) is True
    finished = in_memory.get_finished_spans()
    assert len(finished) == 1
    assert finished[0].name == "test-span"


def test_processor_uses_collector_config_batch_size_and_window(
    tmp_path: Path,
) -> None:
    """The BSP's batch size + schedule delay come from `CollectorConfig`."""
    stage = materialize_span_processor_stage(
        _config(tmp_path, batch_size=64, batch_window_seconds=2),
        _provider(),
        exporter=InMemorySpanExporter(),
    )
    # The OTel SDK does not expose batch-size / schedule-delay publicly on
    # `BatchSpanProcessor` directly; the values live on the inner
    # `_batch_processor`. Accessing the private attribute is the only way to
    # verify the composer wired the `CollectorConfig` values into the SDK.
    inner = stage.processor._batch_processor  # type: ignore[attr-defined]  # pyright: ignore[reportPrivateUsage]
    assert inner._max_export_batch_size == 64  # pyright: ignore[reportPrivateUsage]
    assert inner._schedule_delay_millis == 2_000  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# AC #2 — reachability matrix enforced.
# ---------------------------------------------------------------------------


def test_bootstrap_in_process_placement_passes_reachability(tmp_path: Path) -> None:
    """`CollectorPlacement.IN_PROCESS` is reachable from the bootstrap tier
    (TIER_1_PROCESS) per C-OD-20 §20.3 — materialize completes."""
    stage = materialize_span_processor_stage(
        _config(tmp_path, placement=CollectorPlacement.IN_PROCESS),
        _provider(),
        exporter=InMemorySpanExporter(),
    )
    assert isinstance(stage, SpanProcessorStage)


def test_bootstrap_self_hosted_backend_collector_passes_reachability(
    tmp_path: Path,
) -> None:
    """`SELF_HOSTED_BACKEND_COLLECTOR` is reachable from the host bootstrap
    tier when the collector endpoint is operator-bound on host loopback."""
    stage = materialize_span_processor_stage(
        _config(tmp_path, placement=CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR),
        _provider(),
        exporter=InMemorySpanExporter(),
    )
    assert isinstance(stage, SpanProcessorStage)


def test_bootstrap_sidecar_placement_raises_reachability_error(
    tmp_path: Path,
) -> None:
    """`CollectorPlacement.SIDECAR` is not reachable from the bootstrap tier
    (TIER_1_PROCESS expects localhost-socket reachability) — raises typed."""
    with pytest.raises(SpanProcessorReachabilityError, match=r"C-OD-20 §20\.3"):
        materialize_span_processor_stage(
            _config(tmp_path, placement=CollectorPlacement.SIDECAR),
            _provider(),
            exporter=InMemorySpanExporter(),
        )


def test_bootstrap_vendor_pipeline_placement_raises_reachability_error(
    tmp_path: Path,
) -> None:
    """`CollectorPlacement.VENDOR_PIPELINE` is not reachable from the bootstrap
    tier per the §20.3 matrix — raises typed."""
    with pytest.raises(SpanProcessorReachabilityError, match=r"C-OD-20 §20\.3"):
        materialize_span_processor_stage(
            _config(tmp_path, placement=CollectorPlacement.VENDOR_PIPELINE),
            _provider(),
            exporter=InMemorySpanExporter(),
        )


def test_bootstrap_vendor_pipeline_with_full_vm_tier_passes_reachability(
    tmp_path: Path,
) -> None:
    """A managed-cloud/full-VM bootstrap tier may reach a vendor pipeline.

    This preserves the default Tier-1 localhost guard while allowing R-421's
    explicit managed-cloud FULL_VM binding to materialize the span processor.
    """
    stage = materialize_span_processor_stage(
        _config(
            tmp_path,
            placement=CollectorPlacement.VENDOR_PIPELINE,
            bootstrap_sandbox_tier=SandboxTier.TIER_4_FULL_VM,
        ),
        _provider(),
        exporter=InMemorySpanExporter(),
    )
    assert isinstance(stage, SpanProcessorStage)


def test_assert_otlp_reachable_table_covers_4_sandbox_tiers() -> None:
    """The OD `PER_SANDBOX_TIER_REACHABILITY` matrix covers all 4 AS sandbox
    tiers — the canonical source U-RT-28 enforces against."""
    assert set(PER_SANDBOX_TIER_REACHABILITY) == set(SandboxTier)
    assert len(PER_SANDBOX_TIER_REACHABILITY) == 4


# ---------------------------------------------------------------------------
# Composer plumbing + invariants.
# ---------------------------------------------------------------------------


def test_materialize_returns_stage_with_processor_and_exporter(
    tmp_path: Path,
) -> None:
    exporter = InMemorySpanExporter()
    stage = materialize_span_processor_stage(_config(tmp_path), _provider(), exporter=exporter)
    assert isinstance(stage, SpanProcessorStage)
    assert isinstance(stage.processor, BatchSpanProcessor)
    assert stage.exporter is exporter
    assert isinstance(stage.redaction_processor, RedactionSpanProcessor)


# ---------------------------------------------------------------------------
# C-OD-12 + C-OD-13 redaction wiring (H_T-OD-4 substrate retirement).
# ---------------------------------------------------------------------------


def test_materialize_attaches_redaction_processor_before_bsp(
    tmp_path: Path,
) -> None:
    """OD-4 AC: materialize wires RedactionSpanProcessor + BSP on the provider.

    Verified via behavior — emit a span carrying a §12.1 content attribute,
    flush, observe the exporter received the span WITHOUT the content
    attribute. The default attribute is stripped at on_end via the redaction
    processor registered ahead of the BSP.
    """
    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(_config(tmp_path), provider, exporter=in_memory)
    tracer = provider.get_tracer("redaction-wire-test")
    with tracer.start_as_current_span("anthropic.messages.create") as span:
        span.set_attribute("gen_ai.input.messages", "PII payload")
        span.set_attribute("gen_ai.operation.name", "chat")
    stage.flush(timeout_millis=5000)
    [exported] = in_memory.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes
    assert exported.attributes["gen_ai.operation.name"] == "chat"


def test_materialize_strips_all_13_content_attributes_at_export(
    tmp_path: Path,
) -> None:
    """OD-4 AC: full §12.1 13-attribute set stripped at export time."""
    from harness_od.content_structure_discipline import (
        DEFAULT_OFF_CONTENT_ATTRIBUTES,
    )

    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(_config(tmp_path), provider, exporter=in_memory)
    tracer = provider.get_tracer("redaction-full-test")
    with tracer.start_as_current_span("s") as span:
        for key in DEFAULT_OFF_CONTENT_ATTRIBUTES:
            span.set_attribute(key, f"content for {key}")
        span.set_attribute("sandbox.tier", "tier_1_process")
    stage.flush(timeout_millis=5000)
    [exported] = in_memory.get_finished_spans()
    for key in DEFAULT_OFF_CONTENT_ATTRIBUTES:
        assert key not in exported.attributes, f"runtime wiring leaked {key}"
    assert exported.attributes["sandbox.tier"] == "tier_1_process"


def test_materialize_multi_tenant_tokenizes_content_with_audit_map(
    tmp_path: Path,
) -> None:
    """R-008 gate (b): multi-tenant runtime redaction uses eval-grade tokens.

    The exported span receives an opaque semantic category token, while the raw
    value is persisted only through the signed redaction-token audit map.
    """
    audit_writer = _RecordingAuditWriter()
    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(
        _config(
            tmp_path,
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
            tenant_id="tenant-r008",
        ),
        provider,
        exporter=in_memory,
        audit_writer=audit_writer,
    )
    assert stage.redaction_processor.tokenizer_enabled is True

    tracer = provider.get_tracer("redaction-token-wire-test")
    with tracer.start_as_current_span("anthropic.messages.create") as span:
        span.set_attribute("gen_ai.input.messages", "customer ssn 123-45-6789")
        span.set_attribute("gen_ai.operation.name", "chat")
    stage.flush(timeout_millis=5000)

    [exported] = in_memory.get_finished_spans()
    token = exported.attributes["gen_ai.input.messages"]
    assert isinstance(token, str)
    assert token.startswith("[REDACTED:PII:")
    assert "123-45-6789" not in token
    assert exported.attributes["gen_ai.operation.name"] == "chat"

    [(tenant_id, audit_entry)] = audit_writer.appended
    assert tenant_id == "tenant-r008"
    attrs = audit_entry.payload.audit_namespace_attrs
    assert attrs["audit.redaction_token.token"] == token
    assert attrs["audit.redaction_token.semantic_category"] == "PII"
    assert attrs["audit.redaction_token.raw_value"] == "customer ssn 123-45-6789"
    assert audit_entry.signature_attrs.audit_signature_key_id == "harness-runtime-redaction-token"


def test_materialize_multi_tenant_without_audit_writer_preserves_strip_mode(
    tmp_path: Path,
) -> None:
    """Without the audit sink, multi-tenant remains fail-closed by stripping."""
    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(
        _config(tmp_path, persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE),
        provider,
        exporter=in_memory,
    )
    assert stage.redaction_processor.tokenizer_enabled is False

    tracer = provider.get_tracer("redaction-strip-fallback-test")
    with tracer.start_as_current_span("anthropic.messages.create") as span:
        span.set_attribute("gen_ai.input.messages", "customer ssn 123-45-6789")
    stage.flush(timeout_millis=5000)

    [exported] = in_memory.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes


def test_materialize_attaches_processor_to_provider(tmp_path: Path) -> None:
    """The composer wires the BSP into the provider via `add_span_processor`.

    Verified by emitting a span post-materialize and observing flush delivers
    it — equivalent to checking the processor is part of the provider's
    pipeline."""
    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(_config(tmp_path), provider, exporter=in_memory)
    tracer = provider.get_tracer("attach-test")
    with tracer.start_as_current_span("attached"):
        pass
    stage.flush(timeout_millis=5000)
    assert len(in_memory.get_finished_spans()) == 1


def test_materialize_default_exporter_is_otlp_grpc(tmp_path: Path) -> None:
    """When `exporter=None`, the composer constructs an `OTLPSpanExporter`
    (gRPC) — the exporter does NOT connect at construction time."""
    stage = materialize_span_processor_stage(_config(tmp_path), _provider())
    assert isinstance(stage.exporter, OTLPSpanExporter)
    # Tear down: explicit shutdown to free the gRPC channel; not strictly
    # needed in tests but keeps the test process clean.
    stage.processor.shutdown()


def test_span_processor_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_span_processor_stage(
        _config(tmp_path), _provider(), exporter=InMemorySpanExporter()
    )
    with pytest.raises(FrozenInstanceError):
        stage.processor = BatchSpanProcessor(InMemorySpanExporter())  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §9.1 per-deployment-surface tail-keep wrapper gating.
# ---------------------------------------------------------------------------


def _config_at_surface(
    tmp_path: Path,
    *,
    deployment_surface: DeploymentSurface,
) -> RuntimeConfig:
    """Build a `RuntimeConfig` at the requested surface for §9.1 tests."""
    return RuntimeConfig(
        deployment_surface=deployment_surface,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(placement=CollectorPlacement.IN_PROCESS),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def test_local_development_surface_does_not_wrap_with_tail_keep(
    tmp_path: Path,
) -> None:
    """At LOCAL_DEVELOPMENT, §9.1 mandates head-based sampling — no tail-keep wrap."""
    stage = materialize_span_processor_stage(
        _config_at_surface(tmp_path, deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT),
        _provider(),
        exporter=InMemorySpanExporter(),
    )
    assert stage.tail_keep_processor is None


def test_self_hosted_server_surface_wraps_with_tail_keep(tmp_path: Path) -> None:
    """At SELF_HOSTED_SERVER, §9.1 mandates tail-based sampling — wrap engaged."""
    stage = materialize_span_processor_stage(
        _config_at_surface(tmp_path, deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER),
        _provider(),
        exporter=InMemorySpanExporter(),
    )
    assert stage.tail_keep_processor is not None
    assert stage.tail_keep_processor.downstream is stage.processor


def test_managed_cloud_surface_wraps_with_tail_keep(tmp_path: Path) -> None:
    """At MANAGED_CLOUD, §9.1 mandates tail-based sampling — wrap engaged."""
    stage = materialize_span_processor_stage(
        _config_at_surface(tmp_path, deployment_surface=DeploymentSurface.MANAGED_CLOUD),
        _provider(),
        exporter=InMemorySpanExporter(),
    )
    assert stage.tail_keep_processor is not None
    assert stage.tail_keep_processor.downstream is stage.processor


def test_production_surface_threads_collector_buffer_bounds(tmp_path: Path) -> None:
    """OD spec v1.28 §9.3: CollectorConfig tail-keep ceilings thread through + enforce.

    Behavioral end-to-end check (config → materializer → processor): a tiny
    operator-configured ceiling bounds a pathological producer that opens
    traces without ever closing their roots.
    """
    from opentelemetry import trace as otel_trace

    provider = _provider()
    config = RuntimeConfig(
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(
            placement=CollectorPlacement.IN_PROCESS,
            tail_keep_max_buffered_traces=4,
        ),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )
    stage = materialize_span_processor_stage(config, provider, exporter=InMemorySpanExporter())
    assert stage.tail_keep_processor is not None
    tracer = provider.get_tracer("v1-28-bounds-threading-test")
    roots = []
    for _ in range(20):  # 20 >> 4 ceiling: pathological producer
        root = tracer.start_span("workflow.envelope.pathological")
        child = tracer.start_span("inner.work", context=otel_trace.set_span_in_context(root))
        child.end()
        roots.append(root)
    assert stage.tail_keep_processor.buffered_trace_count == 4  # bounded
    assert stage.tail_keep_processor.dropped_trace_count == 16  # threaded + enforced
    assert len(roots) == 20


def test_production_surface_drops_unclassified_trace_at_export(
    tmp_path: Path,
) -> None:
    """At production surface, a trace with no §10.2 trigger is dropped — exporter sees nothing."""
    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(
        _config_at_surface(tmp_path, deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER),
        provider,
        exporter=in_memory,
    )
    tracer = provider.get_tracer("u-od-3-tail-keep-test")
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span("plain.work"):
            pass
    assert stage.flush(timeout_millis=5000) is True
    finished = in_memory.get_finished_spans()
    assert finished == ()


def test_production_surface_preserves_trace_when_sandbox_violation_present(
    tmp_path: Path,
) -> None:
    """At production surface, sandbox.violation child preserves the entire trace."""
    in_memory = InMemorySpanExporter()
    provider = _provider()
    stage = materialize_span_processor_stage(
        _config_at_surface(tmp_path, deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER),
        provider,
        exporter=in_memory,
    )
    tracer = provider.get_tracer("u-od-3-tail-keep-test")
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span("sandbox.violation"):
            pass
    assert stage.flush(timeout_millis=5000) is True
    finished = in_memory.get_finished_spans()
    finished_names = {s.name for s in finished}
    assert "sandbox.violation" in finished_names
    assert "workflow.envelope" in finished_names
