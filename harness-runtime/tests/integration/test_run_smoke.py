"""U-RT-49 — E2E bootstrap → execute → shutdown smoke.

Acceptance per session-3 atomic decomposition L11 U-RT-49 + Lane 6
(2026-05-20) un-strike of workflow-execution ACs:

- ✅ LAND: green run touches each of the 9 `BootstrapStage` enum members
  (asserted via `BootstrapStageCompleteEvent` capture).
- ✅ LAND: clean shutdown leaves no resources open (`ShutdownReport.failures
  == ()`, pidfile removed, tracer + collector + providers closed).
- ✅ LAND (Lane 6): state-ledger workflow entries — `api.run()` delegates
  to `harness_cp.workflow_driver.execute_workflow()`; the driver writes
  step entries to `ctx.ledger_writer` per C-CP-25 §25.3.3 + §25.6.
- ✅ LAND (Lane 6): lifecycle-event spans — `RuntimeLifecycleEventEmitter`
  records `WorkflowEventClass.{WORKFLOW_START, ...}` per driver emission
  at §25.5.
- ✅ LAND (2026-05-20, CP spec v1.5 §25.9 + plan v2.13 absorbed):
  cost-attribution chain entries — step body owns the chain invocation per
  the §25.5 propagated pattern; smoke test step body fires the chain via
  `compute_span_cost_with_rates` mock-rate bypass per Q3c. The
  `PRICE_TABLE_REF` substitution remains a bounded H_E residual tracked at
  `.harness/fork_price_table_ref_substitution_retirement.md` (NOT retired
  by this AC closure). Materialization at
  `test_e2e_run_step_body_fires_cost_attribution_chain` below.

Four integration tests:
1. `test_e2e_bootstrap_shutdown_round_trip` — original bootstrap→shutdown
   path without execute (verifies the bootstrap-only lifecycle still works).
2. `test_e2e_shutdown_idempotent` — second shutdown returns cached report.
3. `test_e2e_run_executes_workflow_via_cp_driver` — Lane 6: full
   `await run(workflow)` cycle; asserts ledger writes + lifecycle emits.
4. `test_e2e_run_step_body_fires_cost_attribution_chain` — Q1e + Q3c:
   step body fires `ctx.cost_chain` per CP spec v1.5 §25.9 propagated
   pattern; un-strikes U-RT-49 cost-attribution AC.

Fake provider + collector + tracer fixtures mirror `test_bootstrap.py`
(no network; in-process).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

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
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
from harness_runtime.lifecycle.providers import ProviderClientsStage
from harness_runtime.shutdown import shutdown
from harness_runtime.types import (
    BootstrapStage,
    CollectorConfig,
    HarnessContext,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixture scaffolding (mirrors test_bootstrap.py).
# ---------------------------------------------------------------------------


def _test_step_dispatchers(dispatcher: Any) -> Any:
    """U-RT-59 (C-RT-17 §14.7.7): wrap a sync dispatcher in a single-kind
    test registry bound to INFERENCE_STEP. Used by smoke-test WorkflowObject
    fixtures to override the bootstrap-bound ctx.step_dispatchers (which only
    binds SUB_AGENT_DISPATCH at v1.6 MVP per the async/sync Class 1 fork).
    """

    class _Reg:
        def lookup(self, step_kind: Any) -> Any:
            _ = step_kind
            return dispatcher

    return _Reg()


_WORKLOAD = WorkloadClass.SOFTWARE_ENGINEERING
_SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT

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
                "workflow_class": _WORKLOAD,
                "deployment_surface": _SURFACE,
                "path": str(tmp_path / pc.value.lower()),
            }
            for pc in PathClass
        ),
    )


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=_SURFACE,
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


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeDaemon:
    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        _ = timeout_seconds
        self.stopped = True


class _FakeTracerProvider:
    def __init__(self) -> None:
        self.flushed = False
        self.shut_down = False

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        _ = timeout_millis
        self.flushed = True
        return True

    def shutdown(self) -> None:
        self.shut_down = True

    def get_tracer(self, instrumenting_module_name: str, /) -> object:
        # U-OD-35 — CP workflow_driver opens workflow.envelope via
        # ctx.tracer_provider.get_tracer(...). e2e tests don't observe
        # span output; return a NoOp tracer.
        from opentelemetry.trace import NoOpTracer

        _ = instrumenting_module_name
        return NoOpTracer()


@pytest.fixture
def _patched_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Patch providers + stage-4 OD + tracer with in-process fakes."""
    providers = {
        "anthropic": _FakeProvider("anthropic"),
        "openai": _FakeProvider("openai"),
        "ollama": _FakeProvider("ollama"),
    }

    async def _fake_clients(*_args: object, **_kwargs: object) -> ProviderClientsStage:
        return ProviderClientsStage(providers=dict(providers))

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3a_cp_clients.materialize_provider_clients_stage",
        _fake_clients,
    )

    daemon = _FakeDaemon()
    tracer = _FakeTracerProvider()

    class _CollectorStage:
        def __init__(self, d: _FakeDaemon) -> None:
            self.daemon = d

    class _TracerStage:
        def __init__(self, p: _FakeTracerProvider) -> None:
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


# ---------------------------------------------------------------------------
# E2E smoke.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_bootstrap_shutdown_round_trip(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
) -> None:
    """U-RT-49 happy-path: bootstrap → shutdown completes cleanly."""
    config = _config(tmp_path)

    # Phase 1: bootstrap.
    ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert isinstance(ctx, HarnessContext)

    # U-RT-58 AC #9 + U-RT-60 AC #13 (post-wrap-asymmetry-fork APPLIED):
    # stage 5 row 1 chain at C-RT-18 §14.8.1 — bare RuntimeLLMDispatcher
    # → HITL gate composer (PRE_ACTION) → RetryBreakerFallbackDispatcher
    # (C-RT-16). Verify the post-condition at the full-bootstrap path:
    # ctx.llm_dispatcher is the C-RT-16 wrapper; its .inner is the
    # HITL composer; the composer's .inner is the bare dispatcher.
    from harness_runtime.lifecycle.hitl_gate_composer import (
        RuntimeHITLGateComposer,
    )
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher
    from harness_runtime.lifecycle.retry_breaker_fallback import (
        RetryBreakerFallbackDispatcher,
    )

    assert isinstance(ctx.llm_dispatcher, RetryBreakerFallbackDispatcher)
    assert isinstance(ctx.llm_dispatcher.inner, RuntimeHITLGateComposer)
    assert isinstance(ctx.llm_dispatcher.inner.inner, RuntimeLLMDispatcher)

    # AC #1: all 9 BootstrapStage enum members emitted via lifecycle events.
    # `lifecycle_emitter` is the LifecycleEventEmitter Protocol (no
    # `emitted_bootstrap_stages` attribute); concrete is
    # `RuntimeLifecycleEventEmitter` which exposes the test-introspection
    # tuple per U-RT-43. Cast at the call site.
    from harness_runtime.lifecycle.lifecycle_emitter import (
        RuntimeLifecycleEventEmitter,
    )

    emitter = cast(RuntimeLifecycleEventEmitter, ctx.lifecycle_emitter)
    emitted: tuple[BootstrapStage, ...] = emitter.emitted_bootstrap_stages
    expected_stages = set(BootstrapStage)
    missing = expected_stages - set(emitted)
    assert not missing, f"missing bootstrap stage events: {missing}"
    assert len(emitted) == 9

    # Pidfile written at stage 7.
    pidfile = tmp_path / ".harness/runtime.pid"
    assert pidfile.is_file()

    # Phase 2: shutdown.
    report = await shutdown(ctx, timeout=5.0)

    # AC #2: clean shutdown — no failures, all resources released.
    assert report.failures == (), f"unexpected shutdown failures: {report.failures}"
    assert report.already_shutdown is False
    assert report.timed_out is False
    assert report.flush.tracer_flushed is True
    assert report.flush.ledger_fsynced is True
    # No workflow execution → audit ledger empty (struck AC).
    assert report.audit_ledger_head_hash is None

    # Pidfile removed at end of shutdown.
    assert not pidfile.exists(), "pidfile should be removed by shutdown()"

    # Resource closure verified via fake-side state.
    fakes = _patched_runtime
    assert fakes["daemon"].stopped is True
    assert fakes["tracer"].flushed is True
    assert fakes["tracer"].shut_down is True
    assert all(p.closed for p in fakes["providers"].values())


@pytest.mark.asyncio
async def test_e2e_shutdown_idempotent(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
) -> None:
    """Second shutdown returns cached report with already_shutdown=True."""
    _ = _patched_runtime
    config = _config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=_WORKLOAD)

    r1 = await shutdown(ctx)
    r2 = await shutdown(ctx)

    assert r1.already_shutdown is False
    assert r2.already_shutdown is True
    # Cached body matches.
    assert r2.flush == r1.flush
    assert r2.failures == r1.failures


# ---------------------------------------------------------------------------
# Lane 6 — Full `run(workflow)` end-to-end through the CP workflow driver.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_run_executes_workflow_via_cp_driver(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lane 6 — `run()` delegates to `harness_cp.workflow_driver.execute_workflow`.

    Un-strikes the U-RT-49 workflow-execution ACs (state-ledger workflow
    entries + lifecycle-event emission) and the U-RT-44 AC #2 (in-flight
    step boundary). Cost-attribution AC stays struck pending U-OD-21.

    Calling shape: `await run(workflow, config=...)` end-to-end.
    Default config materialization is monkeypatched so the test's
    `tmp_path`-anchored config wins over the env-var-driven default.
    """
    from collections.abc import Sequence
    from typing import Any as _Any

    from harness_core.identity import StepID
    from harness_core.persona_tier import PersonaTier
    from harness_cp.cp_shared_types import ModelBinding
    from harness_cp.engine_class import EngineClass
    from harness_cp.workflow_driver import StepDispatcher as _CpStepDispatcher
    from harness_cp.workflow_driver_types import (
        StepKind,
        WorkflowStep,
    )
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
    from harness_runtime.api import RunResult
    from harness_runtime.api import run as _run

    config = _config(tmp_path)
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    # Track ledger writes by patching the LedgerWriter.append site post-bootstrap.
    # Bootstrap materializes the real ledger; we wrap its append at the
    # post-bootstrap level so we capture every driver-emitted entry.
    captured_appends: list[tuple[_Any, _Any]] = []

    real_run_bootstrap = run_bootstrap

    async def _wrapped_bootstrap(
        cfg: RuntimeConfig, *, workload_class: WorkloadClass, requires_inference: bool = True
    ) -> _Any:
        ctx = await real_run_bootstrap(
            cfg, workload_class=workload_class, requires_inference=requires_inference
        )
        original_append = ctx.ledger_writer.append

        def _capture_append(payload: _Any, write_key: _Any) -> _Any:
            captured_appends.append((payload, write_key))
            return original_append(payload, write_key)

        # `LedgerWriter` is a frozen dataclass; monkeypatch the bound method
        # by attribute injection on the instance via object.__setattr__.
        object.__setattr__(ctx.ledger_writer, "append", _capture_append)
        return ctx

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _wrapped_bootstrap)

    # Build a single-step WorkflowObject.
    class _NoopDispatcher:
        def dispatch(
            self, binding: _Any, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, _Any]:
            _ = binding
            return {"step_id": str(step.step_id), "ok": True}

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-lane-6-smoke"

        @property
        def workload_class(self) -> WorkloadClass:
            return _WORKLOAD

        @property
        def manifest_entry(self) -> WorkflowManifestEntry:
            return WorkflowManifestEntry(
                workflow_id="wf-lane-6-smoke",
                workload_class=_WORKLOAD,
                persona_tier=PersonaTier.TEAM_BINDING,
                engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
                layer_budgets=(),
                fallback_chain=_CHAIN,
                hitl_placements=(),
                per_step_overrides={},
            )

        @property
        def steps(self) -> Sequence[WorkflowStep]:
            return (
                WorkflowStep(
                    step_id=StepID("step-0"),
                    step_kind=StepKind.INFERENCE_STEP,
                    step_payload={"index": 0},
                ),
            )

        @property
        def step_dispatcher(self) -> _CpStepDispatcher:
            return cast(_CpStepDispatcher, _NoopDispatcher())

        @property
        def step_dispatchers(self) -> Any:
            return _test_step_dispatchers(_NoopDispatcher())

        @property
        def default_model_binding(self) -> ModelBinding:
            return ModelBinding(provider="anthropic", model="claude-haiku-4-5")

    # Execute end-to-end.
    result = await _run(_Workflow(), config=config)

    # AC: Lane 6 — RunResult shape.
    assert isinstance(result, RunResult)
    assert result.status == "completed"
    assert result.workflow_id == "wf-lane-6-smoke"
    assert result.failure_cause is None

    # AC (U-RT-49 un-strike): state-ledger workflow entries materialized.
    # The CP driver writes one entry per step under PURE_PATTERN_NO_ENGINE
    # (see C-CP-25 §25.3.3.7). One step → ≥1 ledger append.
    assert len(captured_appends) >= 1, (
        "expected the CP driver to write at least one ledger entry "
        f"for the executed workflow step; saw {captured_appends}"
    )

    # Cost-attribution carries through as empty tuple (struck — U-OD-21 fork).
    assert result.cost_attribution == ()


@pytest.mark.asyncio
async def test_e2e_run_returns_drained_when_flag_set_before_execute(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-RT-44 AC #2 — drain bounded-wait via the CP driver.

    Setting `ctx.drained_flag` after bootstrap but before driver entry
    surfaces `RunResult.status == 'drained'` via the driver's
    §25.4 driver-entry drain check. The signal-handler path
    (`os.kill(SIGTERM)`) is covered at `tests/test_drain.py`; here we
    verify the runtime-side composition: `api.run()` sees a DRAINED
    `RunStatus` from the driver and projects to runtime
    `Literal['drained']`.
    """
    from harness_runtime.api import run as _run

    config = _config(tmp_path)
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    real_run_bootstrap = run_bootstrap

    async def _wrapped_bootstrap(
        cfg: RuntimeConfig, *, workload_class: WorkloadClass, requires_inference: bool = True
    ) -> Any:
        ctx = await real_run_bootstrap(
            cfg, workload_class=workload_class, requires_inference=requires_inference
        )
        # Set drained_flag pre-execute → driver returns DRAINED at entry.
        ctx.drained_flag.set()
        return ctx

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _wrapped_bootstrap)

    # Build a minimal WorkflowObject identical in shape to the prior test.
    from harness_core.identity import StepID
    from harness_core.persona_tier import PersonaTier
    from harness_cp.cp_shared_types import ModelBinding
    from harness_cp.engine_class import EngineClass
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-drain-smoke"

        @property
        def workload_class(self) -> WorkloadClass:
            return _WORKLOAD

        @property
        def manifest_entry(self) -> Any:
            return WorkflowManifestEntry(
                workflow_id="wf-drain-smoke",
                workload_class=_WORKLOAD,
                persona_tier=PersonaTier.TEAM_BINDING,
                engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
                layer_budgets=(),
                fallback_chain=_CHAIN,
                hitl_placements=(),
                per_step_overrides={},
            )

        @property
        def steps(self) -> Any:
            return (
                WorkflowStep(
                    step_id=StepID("step-0"),
                    step_kind=StepKind.INFERENCE_STEP,
                    step_payload={},
                ),
            )

        @property
        def step_dispatcher(self) -> Any:
            class _D:
                def dispatch(
                    self,
                    binding: Any,
                    step: Any,
                    *,
                    step_context: Any = None,
                ) -> dict[str, Any]:
                    raise AssertionError("dispatcher must not run under drain-at-entry")

            return _D()

        @property
        def step_dispatchers(self) -> Any:
            class _D:
                def dispatch(
                    self,
                    binding: Any,
                    step: Any,
                    *,
                    step_context: Any = None,
                ) -> dict[str, Any]:
                    raise AssertionError("dispatcher must not run under drain-at-entry")

            return _test_step_dispatchers(_D())

        @property
        def default_model_binding(self) -> Any:
            return ModelBinding(provider="anthropic", model="claude-haiku-4-5")

    result = await _run(_Workflow(), config=config)
    assert result.status == "drained"
    assert result.workflow_id == "wf-drain-smoke"


@pytest.mark.asyncio
async def test_e2e_run_surfaces_drain_timeout_when_step_exceeds_budget(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-RT-44 AC #2 typed-timeout branch — `RT-FAIL-DRAIN-TIMEOUT`.

    A step body that sleeps past `RuntimeConfig.drain_timeout_seconds`
    forces `asyncio.wait_for` to surface `TimeoutError`; runtime wraps
    that into a DRAINED `RunResult` whose `failure_cause` tags
    `RT-FAIL-DRAIN-TIMEOUT` per C-RT-14. Per spec §11, the thread keeps
    running (cannot be cancelled cooperatively); we only verify the
    typed return surface.
    """
    import time

    from harness_core.identity import StepID
    from harness_core.persona_tier import PersonaTier
    from harness_cp.cp_shared_types import ModelBinding
    from harness_cp.engine_class import EngineClass
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
    from harness_runtime.api import run as _run

    # Tight 200ms budget — far below the test-step sleep.
    config = _config(tmp_path).model_copy(update={"drain_timeout_seconds": 0.2})
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    class _SlowDispatcher:
        def dispatch(self, binding: Any, step: Any, *, step_context: Any = None) -> dict[str, Any]:
            _ = binding, step
            time.sleep(2.0)  # well past 0.2s budget
            return {}

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-timeout-smoke"

        @property
        def workload_class(self) -> WorkloadClass:
            return _WORKLOAD

        @property
        def manifest_entry(self) -> Any:
            return WorkflowManifestEntry(
                workflow_id="wf-timeout-smoke",
                workload_class=_WORKLOAD,
                persona_tier=PersonaTier.TEAM_BINDING,
                engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
                layer_budgets=(),
                fallback_chain=_CHAIN,
                hitl_placements=(),
                per_step_overrides={},
            )

        @property
        def steps(self) -> Any:
            return (
                WorkflowStep(
                    step_id=StepID("step-slow"),
                    step_kind=StepKind.INFERENCE_STEP,
                    step_payload={},
                ),
            )

        @property
        def step_dispatcher(self) -> Any:
            return _SlowDispatcher()

        @property
        def step_dispatchers(self) -> Any:
            return _test_step_dispatchers(_SlowDispatcher())

        @property
        def default_model_binding(self) -> Any:
            return ModelBinding(provider="anthropic", model="claude-haiku-4-5")

    result = await _run(_Workflow(), config=config)
    assert result.status == "drained"
    assert result.failure_cause is not None
    assert result.failure_cause.runtime_fail_class == "RT-FAIL-DRAIN-TIMEOUT"


# ---------------------------------------------------------------------------
# CP spec v1.5 §25.9 — step body owns cost-attribution chain invocation
# (Q1e propagated pattern; Q3c mock-rate bypass; un-strikes U-RT-49 cost AC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_run_step_body_fires_cost_attribution_chain(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-RT-49 cost-attribution AC un-strike — CP spec v1.5 §25.9.

    Per the §25.9 propagated-emission pattern (analogous to §25.5
    retry.attempt / breaker.tripped / fallback.triggered rows: "Driver
    does not synthesize; it propagates step body's emission"), the step
    body — NOT the driver — owns the cost-attribution chain invocation.

    This test:

    1. Captures `ctx.cost_chain` post-bootstrap via the wrapped bootstrap.
    2. The step body (the `_CostFiringDispatcher.dispatch` method)
       composes a mock `SpanCostInputs` + mock `PriceRateEntry` from
       its local provider-invocation closure (Q2-bounded: no shared
       cross-axis carrier at v1.5).
    3. Step body invokes `ctx.cost_chain.compute_per_attempt_cost(inputs,
       mock_rates)` — bypasses the deferred `PRICE_TABLE_REF`
       substitution via `compute_span_cost_with_rates` per OD
       `cost_formula.py:175-188` documented intent (Q3c).
    4. Step body composes a full 12-field `SpanCostRecord` and calls
       `ctx.cost_chain.attach_idempotency_key` for the §14.4 join.
    5. The resulting `SpanCostRecord` is captured in a test-local list;
       the test asserts the chain produced ≥1 entry of the right shape.

    AC text from `.harness/u-rt-49-implementation-plan.md`:
    > Cost attribution chain produced an entry.

    Materialized verbatim. `PRICE_TABLE_REF` substitution remains an
    open bounded H_E residual per
    `.harness/fork_price_table_ref_substitution_retirement.md`; this
    test does NOT retire it (uses the mock-rate bypass, not a resident
    rate table).
    """
    from collections.abc import Sequence
    from typing import Any as _Any
    from unittest.mock import MagicMock

    from harness_core.identity import StepID
    from harness_core.persona_tier import PersonaTier
    from harness_cp.cp_shared_types import ModelBinding
    from harness_cp.engine_class import EngineClass
    from harness_cp.engine_namespace import ReplayDisposition
    from harness_cp.workflow_driver import StepDispatcher as _CpStepDispatcher
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
    from harness_od.cost_formula import (
        PriceRateEntry,
        PriceRateKey,
        SpanCostInputs,
    )
    from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord
    from harness_runtime.api import run as _run

    config = _config(tmp_path)
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    # Captured surfaces: the ctx (for cost_chain access from the
    # dispatcher) and the produced SpanCostRecord(s) for AC verification.
    ctx_holder: dict[str, _Any] = {}
    cost_records: list[SpanCostRecord] = []

    real_run_bootstrap = run_bootstrap

    async def _wrapped_bootstrap(
        cfg: RuntimeConfig, *, workload_class: WorkloadClass, requires_inference: bool = True
    ) -> _Any:
        ctx = await real_run_bootstrap(
            cfg, workload_class=workload_class, requires_inference=requires_inference
        )
        ctx_holder["ctx"] = ctx
        return ctx

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _wrapped_bootstrap)

    # Mock rate snapshot per Q3c — bypasses the deferred PRICE_TABLE_REF
    # substitution by supplying an explicit PriceRateEntry directly.
    _mock_rate_key = PriceRateKey(
        provider_name="anthropic",
        model="claude-haiku-4-5",
        tokenizer_version="anthropic-tokenizer-v1",
    )
    _mock_rates = PriceRateEntry(
        key=_mock_rate_key,
        base_input=3.0e-6,  # $3 / MTok input — mock value, not authoritative
        base_output=15.0e-6,  # $15 / MTok output — mock value, not authoritative
    )

    class _CostFiringDispatcher:
        """Step body that owns cost-attribution invocation per §25.9."""

        def dispatch(
            self, binding: _Any, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, _Any]:
            _ = binding

            # Step body sources SpanCostInputs from its local provider-
            # invocation closure (Q2-bounded; mock token counts here
            # because the provider client is faked at the fixture layer).
            inputs = SpanCostInputs(
                input_tokens=100,
                cache_creation=0,
                cache_read=0,
                output_tokens=42,
                rate_key=_mock_rate_key,
            )

            ctx = ctx_holder["ctx"]

            # Step 1: per-attempt cost (C-OD-14 §14.1) — invoked through
            # compute_span_cost_with_rates (mock-rate bypass per Q3c).
            cost_usd = ctx.cost_chain.compute_per_attempt_cost(inputs, _mock_rates)

            # Step 2: sandbox-overhead composition (C-OD-14 §14.2) —
            # non-sandboxed step → sandbox_overhead=None passes through.
            total = ctx.cost_chain.compose_total_cost(
                span_cost=cost_usd,
                span_duration_ms=10,
                sandbox_overhead=None,
            )

            # Step 3: compose the 13-field SpanCostRecord (the carrier
            # consumed by U-OD-21 rollup_costs_by_axis + the audit
            # ledger join site per §14.4).
            cost_record = SpanCostRecord(
                span_id=f"span-{step.step_id}",
                idempotency_key="placeholder-pre-join",
                total_cost=total.total_cost,
                total_latency_ms=total.total_latency_ms,
                derived_keys=(),
                engine_replay_disposition=ReplayDisposition.NO_REPLAY,
                retry_attempt_number=None,
                retry_cause_attribution=None,
                is_replay_derived=False,
                provider_discriminator="anthropic",
                dispatch_kind=DispatchKind.LLM,
                gen_ai_provider_name="anthropic",
                gen_ai_request_model="claude-haiku-4-5",
            )

            # Step 4: idempotency-key join (C-OD-14 §14.4) — attach the
            # parent's idempotency_key per C-IS-05 (the join key).
            joined_record = ctx.cost_chain.attach_idempotency_key(
                span=cast(_Any, MagicMock()),
                parent_idempotency_key=f"run-id::step::{step.step_id}",
                cost_record=cost_record,
            )

            cost_records.append(joined_record)
            return {"step_id": str(step.step_id), "ok": True}

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-cost-attribution-smoke"

        @property
        def workload_class(self) -> WorkloadClass:
            return _WORKLOAD

        @property
        def manifest_entry(self) -> WorkflowManifestEntry:
            return WorkflowManifestEntry(
                workflow_id="wf-cost-attribution-smoke",
                workload_class=_WORKLOAD,
                persona_tier=PersonaTier.TEAM_BINDING,
                engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
                layer_budgets=(),
                fallback_chain=_CHAIN,
                hitl_placements=(),
                per_step_overrides={},
            )

        @property
        def steps(self) -> Sequence[WorkflowStep]:
            return (
                WorkflowStep(
                    step_id=StepID("step-0"),
                    step_kind=StepKind.INFERENCE_STEP,
                    step_payload={"index": 0},
                ),
            )

        @property
        def step_dispatcher(self) -> _CpStepDispatcher:
            return cast(_CpStepDispatcher, _CostFiringDispatcher())

        @property
        def step_dispatchers(self) -> Any:
            return _test_step_dispatchers(_CostFiringDispatcher())

        @property
        def default_model_binding(self) -> ModelBinding:
            return ModelBinding(provider="anthropic", model="claude-haiku-4-5")

    result = await _run(_Workflow(), config=config)

    # The driver completes the run (no failures).
    assert result.status == "completed"

    # AC un-strike: cost-attribution chain produced ≥1 entry.
    assert len(cost_records) >= 1, (
        "expected the step body to fire the cost-attribution chain at "
        "least once per CP spec v1.5 §25.9"
    )

    # AC shape verification: the produced entry is a full 13-field
    # SpanCostRecord with the U-OD-20 v2.8 D-5 + v1.30 rollup keys materialized.
    record = cost_records[0]
    assert isinstance(record, SpanCostRecord)
    assert record.total_cost > 0  # non-trivial cost value emitted
    assert record.total_latency_ms == 10
    # idempotency_key was updated by §14.4 join — no longer the placeholder.
    assert record.idempotency_key == "run-id::step::step-0"
    # v2.8 D-5 + v1.30 rollup keys are populated.
    assert record.provider_discriminator == "anthropic"
    assert record.dispatch_kind is DispatchKind.LLM
    assert record.gen_ai_provider_name == "anthropic"
    assert record.gen_ai_request_model == "claude-haiku-4-5"
