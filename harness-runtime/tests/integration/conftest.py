"""Shared fixtures for tier-3 integration tests.

Shared by `test_run_smoke.py` (U-RT-49) and `test_bootstrap_stages.py`
(U-RT-50). The patched_runtime fixture replaces provider clients,
collector daemon, and tracer provider with in-process fakes so tests
don't hit network or globally register the tracer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.topology_pattern import TopologyPattern
from harness_is.path_class_registry import PathClass
from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
from harness_runtime.lifecycle.providers import ProviderClientsStage
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

WORKLOAD = WorkloadClass.SOFTWARE_ENGINEERING
SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT


_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic",
        model="claude-haiku-4-5",
        family=ProviderFamily.ANTHROPIC,
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)


def _path_bindings(tmp_path: Path) -> PathBindingConfig:
    return PathBindingConfig(
        raw_entries=tuple(
            {
                "path_class": pc,
                "workflow_class": WORKLOAD,
                "deployment_surface": SURFACE,
                "path": str(tmp_path / pc.value.lower()),
            }
            for pc in PathClass
        ),
    )


def build_config(tmp_path: Path) -> RuntimeConfig:
    """Construct a minimal valid `RuntimeConfig` for an integration test."""
    return RuntimeConfig(
        deployment_surface=SURFACE,
        repository_root=tmp_path,
        path_bindings=_path_bindings(tmp_path),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
        ollama_optional=True,
        routing_manifest=RoutingManifest(
            manifest_version=1,
            per_role_bindings={},
            per_workload_overrides={},
            fallback_chains=(_CHAIN,),
            retry_policies={},
        ),
    )


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeDaemon:
    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        _ = timeout_seconds
        self.stopped = True


class _FakeSpanContextHandle:
    """OTel ``SpanContext`` shim — minimal surface for ``span_id`` + ``is_valid``."""

    def __init__(self, span_id: int) -> None:
        self.span_id = span_id
        self.is_valid = True


class _FakeSpanContext:
    """Minimal span context-manager substrate for ``FakeTracerProvider``."""

    def __init__(self, name: str, parent: FakeTracerProvider) -> None:
        self.name = name
        self.attrs: dict[str, object] = {}
        self._parent = parent

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value

    def set_status(self, *_args: object, **_kwargs: object) -> None:
        """OTel ``Span.set_status`` no-op shim — production callers at
        ``hitl_gate_composer`` + ``validator_escalation_composer`` invoke
        this on every gate/escalation span; the fake captures attrs only.
        """
        return None

    def record_exception(self, *_args: object, **_kwargs: object) -> None:
        """OTel ``Span.record_exception`` no-op shim — sibling to
        ``set_status``; production wraps audit-compose failures via this.
        """
        return None

    def add_event(self, *_args: object, **_kwargs: object) -> None:
        """OTel ``Span.add_event`` no-op shim — production attaches
        per-step lifecycle events via this surface.
        """
        return None

    def get_span_context(self) -> _FakeSpanContextHandle:
        """OTel ``Span.get_span_context`` shim — production retry-instrumentation
        reads ``span_id`` from the returned handle to format
        ``retry.original_span_id``.
        """
        # Stable per-instance span_id by Python object identity; sufficient
        # for tests that only assert hex-formatting succeeds.
        return _FakeSpanContextHandle(span_id=id(self) & 0xFFFFFFFFFFFFFFFF)

    def end(self, *_args: object, **_kwargs: object) -> None:
        """OTel ``Span.end`` no-op shim — production explicitly ends some
        spans outside ``with`` blocks.
        """
        return None

    def __enter__(self) -> _FakeSpanContext:
        self._parent.spans.append(self)
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeTracer:
    def __init__(self, parent: FakeTracerProvider) -> None:
        self._parent = parent

    def start_as_current_span(self, name: str) -> _FakeSpanContext:
        return _FakeSpanContext(name, self._parent)


class FakeTracerProvider:
    def __init__(self) -> None:
        self.flushed = False
        self.shut_down = False
        # U-RT-101 — span capture surface for skill_activation emitter +
        # llm_dispatch emit-time verification at integration-test boundary.
        self.spans: list[_FakeSpanContext] = []

    def get_tracer(self, _name: str) -> _FakeTracer:
        """OTel ``TracerProvider`` surface — emit-time tracer acquisition.

        Added at U-RT-101 e2e (AS-8d retirement gate close) per
        ``[[verification-shape-sharpened-grep-vs-e2e]]`` — emitters must
        be exercised end-to-end at the integration-test boundary, not just
        at unit-test scope with private _FakeTracerProvider shims.
        """
        return _FakeTracer(self)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        _ = timeout_millis
        self.flushed = True
        return True

    def shutdown(self) -> None:
        self.shut_down = True


@pytest.fixture
def patched_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Patch providers + stage-4 OD + tracer with in-process fakes."""
    providers = {
        "anthropic": FakeProvider("anthropic"),
        "openai": FakeProvider("openai"),
        "ollama": FakeProvider("ollama"),
    }

    async def _fake_clients(*_args: object, **_kwargs: object) -> ProviderClientsStage:
        return ProviderClientsStage(providers=dict(providers))

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3a_cp_clients.materialize_provider_clients_stage",
        _fake_clients,
    )

    daemon = FakeDaemon()
    tracer = FakeTracerProvider()

    class _CollectorStage:
        def __init__(self, d: FakeDaemon) -> None:
            self.daemon = d

    class _TracerStage:
        def __init__(self, p: FakeTracerProvider) -> None:
            self.provider = p
            self.registered_globally = False

    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_collector_daemon_stage",
        lambda config, **_: _CollectorStage(daemon),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_ring_buffer_stage",
        lambda config, _d: None,
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_tracer_provider_stage",
        lambda config, **_: _TracerStage(tracer),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_span_processor_stage",
        lambda config, _p, **_k: None,
    )

    yield {
        "providers": providers,
        "daemon": daemon,
        "tracer": tracer,
    }
