"""B-FANOUT-PAUSE (R-FS-1) — full-runtime `api.resume` of a paused fan-out.

The gold-standard close for the resumable `cascade_policy=pause` fan-out: a
`FanOutResumeState`-bearing `PauseSnapshot`, captured via a real bootstrapped
protocol and JSON round-tripped (a process-restart), drives `api.resume(...)` on
a *fresh* bootstrap through the WHOLE runtime stack — bootstrap → entry-point
resume detection (`attempt_resume`, STRICT, MVP constant anchor → admitted) →
the ORCHESTRATOR_WORKERS strategy re-entry with the snapshot → SUCCESS. Proves
the fan-out snapshot survives the durable round-trip + the PAUSED→'paused'
projection + the opaque `pause_snapshot_input` threading end-to-end (closing the
half-proof gap a CP-only witness leaves — `[[full-chain-witness-not-half-proofs]]`).

Substrate (provider/tracer/OD fakes + the get_tracer-capable tracer) lifted by
value from `test_r_cc_1_api_resume.py` per FM-2 cross-test-file independence.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.identity import StepID
from harness_core.persona_tier import PersonaTier
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.pause_resume_protocol_types import (
    FanOutBranchResumeState,
    FanOutResumeState,
    PauseSnapshot,
    WorkflowPauseReason,
)
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import StepDispatcher as _CpStepDispatcher
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.path_class_registry import PathClass
from harness_runtime.api import RunResult, resume
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)
from harness_runtime.lifecycle.providers import ProviderClientsStage
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

_WORKLOAD = WorkloadClass.SOFTWARE_ENGINEERING
_SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT
_WORKFLOW_ID = "wf-b-fanout-pause-resume-e2e"

_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
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


def _config_opt_in(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=_SURFACE,
        repository_root=tmp_path,
        path_bindings=_path_bindings(tmp_path),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.ORCHESTRATOR_WORKERS,
        mcp_clients=[],
        ollama_optional=True,
        pause_resume_protocol_config=PauseResumeProtocolConfig.default(),
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

    async def aclose(self) -> None:
        return None


class _FakeDaemon:
    async def start(self) -> None:
        return None

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        _ = timeout_seconds


class _FakeTracerProvider:
    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        _ = timeout_millis
        return True

    def shutdown(self) -> None:
        return None

    def get_tracer(self, instrumenting_module_name: str, /) -> object:
        from opentelemetry.trace import NoOpTracer

        _ = instrumenting_module_name
        return NoOpTracer()


@pytest.fixture
def _patched_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    providers = {
        "anthropic": _FakeProvider("anthropic"),
        "openai": _FakeProvider("openai"),
        "ollama": _FakeProvider("ollama"),
    }

    async def _fake_clients(*_a: object, **_k: object) -> ProviderClientsStage:
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
    monkeypatch.setattr(_stage_4_od_mod, "materialize_ring_buffer_stage", lambda config, _d: None)
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_tracer_provider_stage",
        lambda config, **_: _TracerStage(tracer),
    )
    monkeypatch.setattr(
        _stage_4_od_mod, "materialize_span_processor_stage", lambda config, _p, **_k: None
    )
    yield None


# A module-level record of which step_ids were dispatched during the RESUME run
# (the workflow's dispatcher records here so the test can assert re-dispatch).
_RESUME_DISPATCHED: list[str] = []


class _RecordingDispatcher:
    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = binding, step_context
        _RESUME_DISPATCHED.append(str(step.step_id))
        return {"step_id": str(step.step_id), "ok": True, "fresh": True}


def _single_kind_registry(dispatcher: Any) -> Any:
    class _Reg:
        def lookup(self, step_kind: Any) -> Any:
            _ = step_kind
            return dispatcher

    return _Reg()


class _FanOutWorkflow:
    """An ORCHESTRATOR_WORKERS workflow: steps[0] orchestrator + 2 workers, under
    the TEAM_BINDING persona (→ cascade_policy = pause)."""

    @property
    def workflow_id(self) -> str:
        return _WORKFLOW_ID

    @property
    def workload_class(self) -> WorkloadClass:
        return _WORKLOAD

    @property
    def manifest_entry(self) -> WorkflowManifestEntry:
        return WorkflowManifestEntry(
            workflow_id=_WORKFLOW_ID,
            workload_class=_WORKLOAD,
            persona_tier=PersonaTier.TEAM_BINDING,
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
            topology_pattern=TopologyPattern.ORCHESTRATOR_WORKERS,
            layer_budgets=(),
            fallback_chain=_CHAIN,
            hitl_placements=(),
            per_step_overrides={},
        )

    @property
    def steps(self) -> Sequence[WorkflowStep]:
        return (
            WorkflowStep(
                step_id=StepID("orchestrator"),
                step_kind=StepKind.DECLARATIVE_STEP,
                step_payload={"role": "orchestrator"},
            ),
            WorkflowStep(
                step_id=StepID("worker-0"),
                step_kind=StepKind.DECLARATIVE_STEP,
                step_payload={"index": 0},
            ),
            WorkflowStep(
                step_id=StepID("worker-1"),
                step_kind=StepKind.DECLARATIVE_STEP,
                step_payload={"index": 1},
            ),
        )

    @property
    def step_dispatcher(self) -> _CpStepDispatcher:
        return cast(_CpStepDispatcher, _RecordingDispatcher())

    @property
    def step_dispatchers(self) -> Any:
        return _single_kind_registry(_RecordingDispatcher())

    @property
    def default_model_binding(self) -> ModelBinding:
        return ModelBinding(provider="anthropic", model="claude-haiku-4-5")


@pytest.mark.asyncio
async def test_api_resume_fan_out_pause_restart_proof_round_trip(
    tmp_path: Path,
    _patched_runtime: None,
) -> None:
    """A fan-out `PauseSnapshot` (worker-0 terminal+recovered; worker-1 left
    re-dispatchable), captured via the real bootstrapped protocol + JSON
    round-tripped, drives `api.resume(...)` on a fresh bootstrap to SUCCESS:
      - the orchestrator + worker-0 are NOT re-dispatched (recovered from snapshot),
      - ONLY worker-1 is re-dispatched,
      - the resumed aggregate fuses the recovered (orchestrator + worker-0) with
        the fresh worker-1 output.
    Full-runtime witness through the public `api.resume` surface (C-RT-35)."""
    _RESUME_DISPATCHED.clear()
    config = _config_opt_in(tmp_path)

    # ---- "Pause" — capture a real fan-out PauseSnapshot via a bootstrapped protocol.
    capture_ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert capture_ctx.pause_resume_protocol is not None
    snapshot = await capture_ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id="run-fanout-resume-e2e",
        step_index=0,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        fan_out_resume=FanOutResumeState(
            orchestrator_output={"role": "orchestrator", "recovered": True},
            orchestrator_step_id="orchestrator",
            branches=(
                FanOutBranchResumeState(
                    branch_index=0,
                    step_id="worker-0",
                    terminal_status="completed",
                    output={"step_id": "worker-0", "recovered": True},
                ),
            ),  # worker-1 absent → re-dispatchable
            worker_count=2,
        ),
    )

    # ---- "Restart" — persist + reload across a process boundary (JSON round-trip).
    rehydrated = PauseSnapshot.model_validate_json(snapshot.model_dump_json())
    assert rehydrated == snapshot
    assert rehydrated.fan_out_resume is not None

    # ---- "Resume" — fresh bootstrap inside api.resume → continue to SUCCESS.
    result = await resume(_FanOutWorkflow(), pause_snapshot=rehydrated, config=config)

    assert isinstance(result, RunResult)
    assert result.status == "completed", (
        f"expected completed resume, got {result.status}; failure_cause={result.failure_cause}"
    )
    assert result.workflow_id == _WORKFLOW_ID
    assert result.pause_snapshot is None
    # Only the re-dispatchable worker-1 ran on resume; the orchestrator + the
    # terminal worker-0 were recovered, NOT re-dispatched (obligation 7).
    assert _RESUME_DISPATCHED == ["worker-1"], (
        f"resume must skip the recovered orchestrator + terminal worker-0; "
        f"dispatched={_RESUME_DISPATCHED}"
    )
