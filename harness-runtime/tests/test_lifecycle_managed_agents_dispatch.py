"""C-RT-28 §14.20 — ManagedAgentsStepDispatcher + factory unit tests (arc M).

Provider-free: a configurable fake `ManagedAgentsClientProtocol` + a real
in-memory tracer exercise the dispatcher's dispatch flow + telemetry, and the
stage-5 factory's surface-gated opt-in. The live vendor path is the separate
skipif-gated `@pytest.mark.e2e` test (surfaced vendor-gate; never auto-fired).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from harness_core import StepID
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_runtime.bootstrap.factories.managed_agents_dispatcher_factory import (
    materialize_managed_agents_dispatcher_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.managed_agents import (
    ManagedAgentEvent,
    ManagedAgentSession,
    ManagedAgentSessionStatus,
)
from harness_runtime.lifecycle.managed_agents_dispatch import (
    ManagedAgentsConfig,
    ManagedAgentsSessionError,
    ManagedAgentsStageMaterializeError,
    ManagedAgentsStepDispatcher,
)
from harness_runtime.lifecycle.sync_dispatcher_facade import (
    materialize_sync_dispatcher_facade,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class _FakeClient:
    """Configurable fake `ManagedAgentsClientProtocol`.

    `retrieve_statuses` is consumed one-per-`retrieve_session` call so a test
    can model a non-terminal→terminal poll sequence; the last value repeats.
    """

    def __init__(
        self,
        *,
        retrieve_statuses: list[ManagedAgentSessionStatus],
        cancel_raises: bool = False,
    ) -> None:
        self._statuses = retrieve_statuses
        self._retrieve_calls = 0
        self._cancel_raises = cancel_raises
        self.sent: list[ManagedAgentEvent] = []
        self.cancelled: list[str] = []

    async def create_session(
        self,
        *,
        agent_id: str,
        environment_id: str,
        title: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ManagedAgentSession:
        _ = (title, metadata)
        return ManagedAgentSession(
            session_id="session_test",
            agent_id=agent_id,
            environment_id=environment_id,
            status=ManagedAgentSessionStatus.CREATED,
            runtime_ms=0,
            billable_seconds=0.0,
        )

    async def send_event(self, *, session_id: str, event: ManagedAgentEvent) -> ManagedAgentEvent:
        _ = session_id
        self.sent.append(event)
        return event

    async def retrieve_session(self, *, session_id: str) -> ManagedAgentSession:
        idx = min(self._retrieve_calls, len(self._statuses) - 1)
        self._retrieve_calls += 1
        return ManagedAgentSession(
            session_id=session_id,
            agent_id="agent_test",
            environment_id="env_test",
            status=self._statuses[idx],
            runtime_ms=1250,
            billable_seconds=1.25,
        )

    async def cancel_session(self, *, session_id: str) -> ManagedAgentSession:
        self.cancelled.append(session_id)
        if self._cancel_raises:
            raise RuntimeError("vendor cancel failed")
        return ManagedAgentSession(
            session_id=session_id,
            agent_id="agent_test",
            environment_id="env_test",
            status=ManagedAgentSessionStatus.CANCELED,
            runtime_ms=1500,
            billable_seconds=1.5,
        )


def _tracer_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _step(payload: dict[str, Any]) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.MANAGED_AGENTS,
        step_payload=payload,
    )


_VALID_PAYLOAD = {"agent_id": "agent_test", "environment_id": "env_test"}


@pytest.mark.asyncio
async def test_dispatch_success_returns_outcome_and_emits_span() -> None:
    provider, exporter = _tracer_provider()
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.IDLE])
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)

    out = await dispatcher.dispatch(
        cast(Any, None), _step(dict(_VALID_PAYLOAD)), step_context=cast(Any, None)
    )

    assert out["session_id"] == "session_test"
    assert out["status"] == "idle"
    assert out["runtime_ms"] == 1250
    assert out["billable_seconds"] == 1.25
    assert len(client.sent) == 1
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["managed_agents.runtime"]
    assert spans[0].attributes is not None
    assert spans[0].attributes["managed_agents.session_id"] == "session_test"


@pytest.mark.asyncio
async def test_dispatch_polls_until_terminal() -> None:
    provider, _ = _tracer_provider()
    # RUNNING twice, then COMPLETED — the poll loop must advance to terminal.
    client = _FakeClient(
        retrieve_statuses=[
            ManagedAgentSessionStatus.RUNNING,
            ManagedAgentSessionStatus.RUNNING,
            ManagedAgentSessionStatus.COMPLETED,
        ]
    )
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)
    out = await dispatcher.dispatch(
        cast(Any, None),
        _step({**_VALID_PAYLOAD, "poll_interval_seconds": 0.0}),
        step_context=cast(Any, None),
    )
    assert out["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_failed_status_raises_session_error() -> None:
    provider, _ = _tracer_provider()
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.FAILED])
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)
    with pytest.raises(ManagedAgentsSessionError):
        await dispatcher.dispatch(
            cast(Any, None), _step(dict(_VALID_PAYLOAD)), step_context=cast(Any, None)
        )


@pytest.mark.asyncio
async def test_dispatch_missing_input_raises_session_error() -> None:
    provider, _ = _tracer_provider()
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.IDLE])
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)
    with pytest.raises(ManagedAgentsSessionError):
        await dispatcher.dispatch(
            cast(Any, None), _step({"agent_id": "a"}), step_context=cast(Any, None)
        )


@pytest.mark.asyncio
async def test_dispatch_poll_budget_exhausted_cancels_then_raises() -> None:
    """§14.20.2 step 4 — on poll-budget exhaustion the dispatcher best-effort
    cancels the still-running (billable) vendor session before raising, so a
    given-up session is not orphaned."""
    provider, _ = _tracer_provider()
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.RUNNING])
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)
    with pytest.raises(ManagedAgentsSessionError):
        await dispatcher.dispatch(
            cast(Any, None),
            _step({**_VALID_PAYLOAD, "max_poll_attempts": 2, "poll_interval_seconds": 0.0}),
            step_context=cast(Any, None),
        )
    # The give-up path cancelled the session exactly once.
    assert client.cancelled == ["session_test"]


@pytest.mark.asyncio
async def test_dispatch_budget_exhausted_cancel_failure_still_raises_session_error() -> None:
    """A best-effort cancel that itself fails must NOT mask the primary
    budget-exhausted error — the dispatch still raises ManagedAgentsSessionError
    (with the orphan-warning chained note)."""
    provider, _ = _tracer_provider()
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.RUNNING], cancel_raises=True)
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)
    with pytest.raises(ManagedAgentsSessionError, match="may be orphaned"):
        await dispatcher.dispatch(
            cast(Any, None),
            _step({**_VALID_PAYLOAD, "max_poll_attempts": 2, "poll_interval_seconds": 0.0}),
            step_context=cast(Any, None),
        )
    assert client.cancelled == ["session_test"]


@pytest.mark.asyncio
async def test_dispatch_terminal_failure_does_not_cancel() -> None:
    """A session that reaches a (non-success) TERMINAL status is already done
    server-side — the give-up cancel path must NOT fire (no orphan to clean)."""
    provider, _ = _tracer_provider()
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.FAILED])
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)
    with pytest.raises(ManagedAgentsSessionError):
        await dispatcher.dispatch(
            cast(Any, None), _step(dict(_VALID_PAYLOAD)), step_context=cast(Any, None)
        )
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_dispatch_through_sync_facade_production_path() -> None:
    """advisor finding #2 — exercise the ACTUAL production binding: the async
    `ManagedAgentsStepDispatcher` wrapped via the production
    `materialize_sync_dispatcher_facade` and driven from a worker thread (the
    CP driver's sync per-step seam), NOT a sync stand-in. Mirrors the
    `test_d7_dispatcher_chain_loop_affinity_through_facade` pattern: the
    facade hops the worker-thread call back onto the captured outer loop via
    `run_coroutine_threadsafe`, so the async dispatch + its `asyncio.sleep`
    poll loop run on the loop and the outcome returns verbatim."""
    provider, exporter = _tracer_provider()
    client = _FakeClient(
        retrieve_statuses=[
            ManagedAgentSessionStatus.RUNNING,
            ManagedAgentSessionStatus.COMPLETED,
        ]
    )
    dispatcher = ManagedAgentsStepDispatcher(client=cast(Any, client), tracer_provider=provider)
    facade = materialize_sync_dispatcher_facade(cast(Any, dispatcher), result_timeout_seconds=5.0)
    step = _step({**_VALID_PAYLOAD, "poll_interval_seconds": 0.0})

    def _worker() -> Mapping[str, Any]:
        # Worker thread has no running loop — the invariant the facade bridges.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return facade.dispatch(cast(Any, None), step, step_context=cast(Any, None))

    out = await asyncio.to_thread(_worker)
    assert out["session_id"] == "session_test"
    assert out["status"] == "completed"
    # The async poll loop + span emission ran on the outer loop through the hop.
    assert [s.name for s in exporter.get_finished_spans()] == ["managed_agents.runtime"]


# --- factory surface-gating ---------------------------------------------------


def _config(*, surface: DeploymentSurface, managed: ManagedAgentsConfig | None) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=surface,
        repository_root=Path("/tmp/arc-m-test"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
        managed_agents_config=managed,
    )


@pytest.mark.asyncio
async def test_factory_optout_returns_none() -> None:
    cfg = _config(surface=DeploymentSurface.MANAGED_CLOUD, managed=None)
    ctx = _MutableHarnessContext()
    ctx.tracer_provider = _tracer_provider()[0]
    assert await materialize_managed_agents_dispatcher_stage(cfg, ctx) is None


@pytest.mark.asyncio
async def test_factory_non_managed_cloud_returns_none() -> None:
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.IDLE])
    cfg = _config(
        surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        managed=ManagedAgentsConfig(client=client),
    )
    ctx = _MutableHarnessContext()
    ctx.tracer_provider = _tracer_provider()[0]
    # Opted-in but NOT managed-cloud → silently not bound (the AS-8f exclusion).
    assert await materialize_managed_agents_dispatcher_stage(cfg, ctx) is None


@pytest.mark.asyncio
async def test_factory_optin_managed_cloud_returns_dispatcher() -> None:
    client = _FakeClient(retrieve_statuses=[ManagedAgentSessionStatus.IDLE])
    cfg = _config(
        surface=DeploymentSurface.MANAGED_CLOUD,
        managed=ManagedAgentsConfig(client=client),
    )
    ctx = _MutableHarnessContext()
    ctx.tracer_provider = _tracer_provider()[0]
    dispatcher = await materialize_managed_agents_dispatcher_stage(cfg, ctx)
    assert isinstance(dispatcher, ManagedAgentsStepDispatcher)


@pytest.mark.asyncio
async def test_factory_managed_cloud_no_client_raises() -> None:
    cfg = _config(
        surface=DeploymentSurface.MANAGED_CLOUD,
        managed=ManagedAgentsConfig(client=None),
    )
    ctx = _MutableHarnessContext()
    ctx.tracer_provider = _tracer_provider()[0]
    with pytest.raises(ManagedAgentsStageMaterializeError):
        await materialize_managed_agents_dispatcher_stage(cfg, ctx)


def test_managed_agents_config_has_decoupled_step_timeout_default() -> None:
    """advisor finding #1 — the managed-agents facade timeout is a SEPARATE
    knob from the shared `step_dispatch_timeout_seconds` (30s), defaulted high
    (a vendor session runs minutes). The default must comfortably exceed the
    default poll budget (max_poll_attempts=30 × poll_interval=1.0 = 30s)."""
    cfg = ManagedAgentsConfig(client=None)
    assert cfg.step_timeout_seconds == 600.0
    assert cfg.step_timeout_seconds > 30.0  # the shared step_dispatch default


# --- structural coverage for the substrate-blocked stage-5 / freeze seams -----
# A full MANAGED_CLOUD bootstrap is infeasible provider-free (the memory-tool
# fail-closed lock: MANAGED_CLOUD admits only {s3, database}, neither config-
# free). Per the U-RT-68 stage-5 precedent these MANAGED_CLOUD-only binding
# seams get source-grep coverage; the behavioral dispatch + cancel + facade-hop
# paths above are real (the load-bearing surfaces). `ctx.managed_agents_client`
# is currently a write-only carrier (no post-freeze reader) — forward-
# correctness, so the freeze passthrough gets structural coverage.


def test_stage_5_binds_managed_agents_facade_with_decoupled_timeout() -> None:
    """advisor finding #1b — stage 5 wraps the managed-agents dispatcher with
    its OWN `managed_agents_config.step_timeout_seconds`, NOT the shared
    `step_dispatch_timeout_seconds`. The sharp pair: the decoupled line is
    present AND the shared-timeout facade-binding count stays at 3 (the
    INFERENCE / SUB_AGENT / TOOL facades) so a copy-paste re-coupling of the
    managed-agents facade to the shared 30s bound trips this test."""
    from harness_runtime.bootstrap import stage_5_loop_init

    assert stage_5_loop_init.__file__ is not None
    src = Path(stage_5_loop_init.__file__).read_text(encoding="utf-8")
    assert "result_timeout_seconds=config.managed_agents_config.step_timeout_seconds" in src
    shared = src.count("result_timeout_seconds=config.step_dispatch_timeout_seconds")
    assert shared == 3, (
        f"expected exactly 3 facades bound to the shared step_dispatch_timeout_seconds "
        f"(INFERENCE / SUB_AGENT / TOOL); found {shared} — a 4th means the managed-agents "
        f"facade was re-coupled to the shared 30s bound (the advisor finding #1 defect)"
    )


def test_freeze_preserves_managed_agents_client() -> None:
    """Codex finding — `_MutableHarnessContext.freeze()` must pass
    `managed_agents_client` into the frozen `HarnessContext`, else an opted-in
    run's frozen carrier silently falls back to the `None` default. Structural:
    the field is a frozen-model field AND the freeze body passes it through (a
    positive 41-field freeze is infeasible to hand-build; the field is write-only
    so there is no behavioral surface to exercise yet)."""
    from harness_runtime.bootstrap import mutable_context
    from harness_runtime.types import HarnessContext

    assert "managed_agents_client" in HarnessContext.model_fields
    assert mutable_context.__file__ is not None
    src = Path(mutable_context.__file__).read_text(encoding="utf-8")
    assert "managed_agents_client=self.managed_agents_client," in src
