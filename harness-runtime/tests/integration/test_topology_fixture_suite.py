"""R-100-mvp-multi-workflow-fixture-suite — topology fixture suite e2e.

Exercises all 6 H_T ``TopologyPattern`` values through the runtime
sub-agent-dispatch admissibility gate, driven by the operator-facing example
manifests at ``examples/workflows/topology/*.yaml``. Each fixture is a parent
workflow with one ``sub-agent-dispatch`` step whose *child* manifest declares a
distinct ``topology_pattern`` paired with an admissible ``workload_class`` per
the C-CP-11 §11.1 ∪ C-CP-10 §10.3 admissibility union
(``is_topology_permitted_for_workload``).

MVP-scope honesty (per probe finding + runtime spec v1.6 §14.7.2 step 5): at MVP
``topology_pattern`` has **no distinct per-pattern orchestration** — runtime
dispatch is a stateless passthrough that (a) validates admissibility at
``sub_agent_dispatch.py`` ``is_topology_permitted`` and (b) emits a
``topology.pattern`` span attribute. This suite therefore verifies the
**admissibility-matrix + dispatch-composer + telemetry** surface across all 6
patterns — NOT distinct orchestration semantics (unbuilt; CP-axis contract
work, out of scope for this entry: ``contracts:[] cross_axis:no``).

Determinism (CI-runnable, free, flake-free): the child sub-agent runner is a
deterministic stand-in returning ``RunStatus.SUCCESS`` per spec §14.7
"Deferred to implementation discretion" — the same pattern the canonical
``test_lifecycle_sub_agent_dispatch.py`` suite uses. The admissibility gate
(the surface under test) fires for real on every dispatch. A full-bootstrap
real-child-re-entry variant was considered but ``HarnessContext`` is frozen
(no post-bootstrap ``step_dispatchers`` swap) and there is no no-LLM step kind,
so honest determinism there would require faking the LLM provider response —
strictly more brittle than the spec-sanctioned deterministic child runner.

Verification (roadmap R-100-mvp-multi-workflow-fixture-suite ``must_pass``):
  - "6 fixture workflows execute end-to-end"            → per-pattern dispatch to SUCCESS
  - "each fixture exercises ≥1 topology pattern"        → is_topology_permitted fires; topology.pattern span attr
  - "audit-ledger emits expected lifecycle events"      → audit_writer.read_all() ≥ 1 per dispatch
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.per_workload_class_topology import is_topology_permitted_for_workload
from harness_cp.sub_agent_gate_level_descent import SubAgentGateLevelDescent
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver_types import (
    RunResult,
    RunStatus,
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_entry_schema import Identifier as _Identifier
from harness_od.audit_ledger_types import SignatureAlgorithm
from harness_runtime.lifecycle.audit_writer import RuntimeAuditLedgerWriter
from harness_runtime.lifecycle.handoff import RuntimeHandoffRegistry
from harness_runtime.lifecycle.state_ledger import LedgerWriter
from harness_runtime.lifecycle.sub_agent_dispatch import (
    RuntimeSubAgentDispatcher,
    SubAgentDispatchPayload,
    SubAgentDispatchTopologyInadmissibleError,
)
from harness_runtime.lifecycle.topology_dispatcher import RuntimeTopologyDispatcher
from harness_runtime.lifecycle.workflow_manifest_loader import WorkflowManifestLoader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Repo-root-relative example fixtures (tests/integration/ → parents[3] = repo root).
_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "examples" / "workflows" / "topology"

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-topology-fixture-suite")

# (fixture filename, expected child topology, expected admissible child workload).
_CASES: tuple[tuple[str, TopologyPattern, str], ...] = (
    ("single-threaded-linear.yaml", TopologyPattern.SINGLE_THREADED_LINEAR, "pipeline-automation"),
    ("orchestrator-workers.yaml", TopologyPattern.ORCHESTRATOR_WORKERS, "research"),
    ("decentralized-handoff.yaml", TopologyPattern.DECENTRALIZED_HANDOFF, "pipeline-automation"),
    (
        "hierarchical-delegation.yaml",
        TopologyPattern.HIERARCHICAL_DELEGATION,
        "software-engineering",
    ),
    ("evaluator-optimizer.yaml", TopologyPattern.EVALUATOR_OPTIMIZER, "content-creation"),
    ("parallelization.yaml", TopologyPattern.PARALLELIZATION, "research"),
)


# ---------------------------------------------------------------------------
# Deterministic dispatcher scaffold (mirror of test_lifecycle_sub_agent_dispatch)
# ---------------------------------------------------------------------------


class _MockChildWorkflowRunner:
    """Deterministic child stand-in per spec §14.7 implementation discretion."""

    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def __call__(
        self,
        *,
        workflow_id: str,
        manifest_entry: WorkflowManifestEntry,
        steps: Sequence[WorkflowStep],
        handoff_context: Any,
        descent: SubAgentGateLevelDescent,
        default_model_binding: ModelBinding,
        pause_snapshot_input: Any = None,
        child_run_id_seed: str | None = None,
    ) -> RunResult:
        # B-HIERARCHICAL-PAUSE — accept the additive resume-snapshot kwarg (None on a
        # first dispatch); the widened ChildWorkflowRunner Protocol forwards it.
        # B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT — accept the additive deterministic
        # child run_id seed (None for a non-recoverable / non-fanout child).
        _ = (pause_snapshot_input, child_run_id_seed)
        self.calls.append({"workflow_id": workflow_id, "manifest_entry": manifest_entry})
        return RunResult(
            workflow_id=workflow_id,
            run_id="child-run-1",
            status=RunStatus.SUCCESS,
            terminal_step_index=None,
            partial_state=None,
            final_state={"child_field": "value"},
            fail_class=None,
        )


def _build_ledger_writer(tmp_path: Path) -> LedgerWriter:
    from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle

    path = tmp_path / "state.jsonl"
    path.touch()
    handle = JsonlLedgerHandle(canonical_path=path, exists=True, entry_count=0)
    return LedgerWriter(handle=handle, actor=_ACTOR)


def _dispatcher(
    tmp_path: Path,
) -> tuple[RuntimeSubAgentDispatcher, _MockChildWorkflowRunner, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    runner = _MockChildWorkflowRunner()
    ledger_writer = _build_ledger_writer(tmp_path)
    audit_writer = RuntimeAuditLedgerWriter(
        ledger_writer=ledger_writer,
        time_source=lambda: datetime.now(UTC),
    )
    dispatcher = RuntimeSubAgentDispatcher(
        handoff_registry=RuntimeHandoffRegistry(),
        topology_dispatcher=RuntimeTopologyDispatcher(),
        tracer_provider=tp,
        child_workflow_runner=runner,  # type: ignore[arg-type]
        ledger_writer=ledger_writer,
        audit_writer=audit_writer,
        audit_signing_key_id="test-signing-key",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        time_source=lambda: datetime.now(UTC),
        procedural_tier_snapshot_resolver=lambda: _Identifier("b" * 64),
    )
    return dispatcher, runner, exporter


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="dispatch-step",
        model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        hitl_placement=None,
        override_applied=False,
        override_audit_ref=None,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="parent-wf",
        parent_action_id="workflow:parent-wf:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=_ACTOR,
        parent_entry_hash="",
        parent_idempotency_key="0" * 64,
        tenant_id=None,
        step_index=0,
    )


def _load_sub_agent_step(fixture: str) -> WorkflowStep:
    loaded = WorkflowManifestLoader.load_workflow(_FIXTURE_DIR / fixture)
    steps = tuple(loaded.steps)
    assert len(steps) == 1, f"{fixture}: expected 1 step, got {len(steps)}"
    step = steps[0]
    assert step.step_kind is StepKind.SUB_AGENT_DISPATCH, f"{fixture}: {step.step_kind}"
    return step


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "topology", "workload"), _CASES, ids=[c[0] for c in _CASES])
def test_topology_fixture_loads_and_payload_validates(
    fixture: str, topology: TopologyPattern, workload: str
) -> None:
    """Each example manifest loads via the real loader; its sub-agent payload
    round-trips through ``SubAgentDispatchPayload.model_validate`` with the
    expected child topology + admissible workload (admissibility precondition)."""
    step = _load_sub_agent_step(fixture)
    payload = SubAgentDispatchPayload.model_validate(dict(step.step_payload))
    cm = payload.child_manifest_entry
    assert cm.topology_pattern is topology
    assert cm.workload_class.value == workload
    assert is_topology_permitted_for_workload(cm.topology_pattern, cm.workload_class), (
        f"{fixture}: ({topology.value}, {workload}) must be admissible"
    )


@pytest.mark.parametrize(("fixture", "topology", "workload"), _CASES, ids=[c[0] for c in _CASES])
def test_topology_fixture_dispatches_to_success(
    fixture: str, topology: TopologyPattern, workload: str, tmp_path: Path
) -> None:
    """must_pass: the fixture executes end-to-end through the dispatch composer,
    the admissibility gate fires (no inadmissible error), exactly one
    ``subagent.span`` is emitted with ``topology.pattern`` = the pattern under
    test, and the child completes (``subagent.result_status == "completed"``)."""
    _ = workload
    step = _load_sub_agent_step(fixture)
    dispatcher, runner, exporter = _dispatcher(tmp_path)

    dispatcher.dispatch(_binding(), step, step_context=_step_context())

    spans = [s for s in exporter.get_finished_spans() if s.name == "subagent.span"]
    assert len(spans) == 1, f"{fixture}: expected 1 subagent.span, got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs["topology.pattern"] == topology.value
    assert attrs["topology.workload_class"] == workload
    assert attrs["subagent.result_status"] == "completed"
    # Child (deterministic stand-in) was invoked exactly once.
    assert len(runner.calls) == 1


@pytest.mark.parametrize(("fixture", "topology", "workload"), _CASES, ids=[c[0] for c in _CASES])
def test_topology_fixture_emits_audit_ledger_entry(
    fixture: str, topology: TopologyPattern, workload: str, tmp_path: Path
) -> None:
    """must_pass "audit-ledger emits expected lifecycle events per fixture":
    the §14.7.2 step-8 audit sequence persists ≥1 OD audit-ledger entry."""
    _ = topology, workload
    step = _load_sub_agent_step(fixture)
    dispatcher, _, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(_binding(), step, step_context=_step_context())
    audit_entries = dispatcher.audit_writer.read_all()
    assert len(audit_entries) >= 1, f"{fixture}: no audit-ledger entry persisted"


def test_all_six_topology_patterns_covered() -> None:
    """Completeness guard: the fixture suite collectively exercises all 6
    ``TopologyPattern`` values (no silent under-coverage)."""
    covered = {topology for _, topology, _ in _CASES}
    assert covered == set(TopologyPattern), f"missing patterns: {set(TopologyPattern) - covered}"


def test_inadmissible_topology_workload_pairing_raises(tmp_path: Path) -> None:
    """Negative: a child (topology, workload) pairing OUTSIDE the admissibility
    union raises ``SubAgentDispatchTopologyInadmissibleError`` before any
    ``subagent.span`` opens — proving the gate has real teeth (it is not a
    passthrough that accepts every pairing). PARALLELIZATION is admissible for
    {content-creation, research} only; pairing it with pipeline-automation is
    inadmissible per the matrix."""
    step = _load_sub_agent_step("parallelization.yaml")
    bad_payload = copy.deepcopy(dict(step.step_payload))
    bad_payload["child_manifest_entry"]["workload_class"] = "pipeline-automation"
    # Precondition: confirm the mutated pairing is genuinely inadmissible.
    assert not is_topology_permitted_for_workload(
        TopologyPattern.PARALLELIZATION,
        WorkflowManifestEntry.model_validate(bad_payload["child_manifest_entry"]).workload_class,
    )
    bad_step = WorkflowStep(
        step_id=step.step_id,
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload=bad_payload,
    )
    dispatcher, runner, exporter = _dispatcher(tmp_path)
    with pytest.raises(SubAgentDispatchTopologyInadmissibleError):
        dispatcher.dispatch(_binding(), bad_step, step_context=_step_context())
    spans = [s for s in exporter.get_finished_spans() if s.name == "subagent.span"]
    assert len(spans) == 0
    assert len(runner.calls) == 0


# ---------------------------------------------------------------------------
# Driver-level e2e — run each parent fixture through execute_workflow
# (mirrors test_track_b_e2e.py mech-α: real bootstrap + faked provider/OD stages,
# deterministic dispatch; no key/ollama/daemon → CI-runnable). This is the
# must_pass[0] "6 fixture workflows execute end-to-end" / shape:e2e surface:
# the workflow runs through the C-CP-25 §25.3 driver loop to a terminal
# RunStatus.SUCCESS — not just a single dispatcher call. The child stays a
# deterministic stand-in (mock runner), so the parent driver consumes the
# step_dispatchers registry we PASS IN; nothing reads ctx.step_dispatchers and
# frozen HarnessContext is irrelevant.
# ---------------------------------------------------------------------------


class _NoopSyncDispatcher:
    """Sync no-op for step kinds the parent fixtures never use."""

    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = binding, step_context
        return {"step_id": str(step.step_id), "noop": True}


class _HybridRegistry:
    """Routes SUB_AGENT_DISPATCH to the facade-wrapped real dispatcher; all
    other kinds to a no-op (parent topology fixtures contain only the
    sub-agent-dispatch step)."""

    def __init__(self, sub_agent: Any) -> None:
        self._sub_agent = sub_agent
        self._noop = _NoopSyncDispatcher()

    def lookup(self, step_kind: StepKind) -> Any:
        if step_kind is StepKind.SUB_AGENT_DISPATCH:
            return self._sub_agent
        return self._noop


async def _bootstrap_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Bootstrap a HarnessContext with faked provider + OD stages (no real LLM /
    key / daemon). Mirror of test_track_b_e2e.py's mech-α scaffold."""
    from harness_core.deployment_surface import DeploymentSurface
    from harness_core.workload_class import WorkloadClass
    from harness_cp.cross_family_fallback_chain import (
        FallbackChain,
        ProviderCandidate,
        ProviderFamily,
    )
    from harness_cp.routing_manifest_residence import RoutingManifest
    from harness_is.path_class_registry import PathClass
    from harness_runtime.bootstrap import run_bootstrap
    from harness_runtime.bootstrap import stage_3a_cp_clients as _stage_3a_mod
    from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
    from harness_runtime.lifecycle.providers import ProviderClientsStage
    from harness_runtime.types import (
        CollectorConfig,
        OTelConfig,
        PathBindingConfig,
        ProviderSecretsConfig,
        RuntimeConfig,
    )

    workload = WorkloadClass.PIPELINE_AUTOMATION  # matches the parent fixtures
    surface = DeploymentSurface.LOCAL_DEVELOPMENT

    class _FakeProvider:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            return None

    async def _fake_clients(*_a: object, **_k: object) -> ProviderClientsStage:
        return ProviderClientsStage(
            providers={n: _FakeProvider(n) for n in ("anthropic", "openai", "ollama")}
        )

    monkeypatch.setattr(_stage_3a_mod, "materialize_provider_clients_stage", _fake_clients)

    class _FakeDaemon:
        async def start(self) -> None:
            return None

        async def stop(self, *, timeout_seconds: float = 5.0) -> None:
            _ = timeout_seconds
            return None

    class _CollectorStage:
        def __init__(self, d: _FakeDaemon) -> None:
            self.daemon = d

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

    class _TracerStage:
        def __init__(self, p: _FakeTracerProvider) -> None:
            self.provider = p
            self.registered_globally = False

    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_collector_daemon_stage",
        lambda config, **_: _CollectorStage(_FakeDaemon()),
    )
    monkeypatch.setattr(_stage_4_od_mod, "materialize_ring_buffer_stage", lambda config, _d: None)
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_tracer_provider_stage",
        lambda config, **_: _TracerStage(_FakeTracerProvider()),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_span_processor_stage",
        lambda config, _p, **_k: None,
    )

    config = RuntimeConfig(
        deployment_surface=surface,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(
            raw_entries=tuple(
                {
                    "path_class": pc,
                    "workflow_class": workload,
                    "deployment_surface": surface,
                    "path": str(tmp_path / pc.value.lower()),
                }
                for pc in PathClass
            ),
        ),
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
            fallback_chains=(
                FallbackChain(
                    primary=ProviderCandidate(
                        provider="anthropic",
                        model="claude-haiku-4-5",
                        family=ProviderFamily.ANTHROPIC,
                    ),
                    same_family=(),
                    cross_family=(),
                    terminal=None,
                ),
            ),
            retry_policies={},
        ),
    )
    return await run_bootstrap(config, workload_class=workload)


@pytest.mark.asyncio
@pytest.mark.parametrize(("fixture", "topology", "workload"), _CASES, ids=[c[0] for c in _CASES])
async def test_topology_fixture_executes_through_driver_to_success(
    fixture: str,
    topology: TopologyPattern,
    workload: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """must_pass[0] / shape:e2e — each parent fixture runs through the CP
    ``execute_workflow`` driver loop to a terminal ``RunStatus.SUCCESS``.

    The driver consumes the PASSED-IN ``step_dispatchers`` registry whose
    SUB_AGENT_DISPATCH entry is the real ``RuntimeSubAgentDispatcher`` (whose
    ``dispatch`` is sync + driver-callable directly; the bootstrap only wraps
    the *async* HITL composer in a facade). ``is_topology_permitted`` fires for
    the child topology under test; the child stays a deterministic stand-in, so
    frozen ``HarnessContext`` is moot."""
    loaded = WorkflowManifestLoader.load_workflow(_FIXTURE_DIR / fixture)
    steps = tuple(loaded.steps)
    assert len(steps) == 1 and steps[0].step_kind is StepKind.SUB_AGENT_DISPATCH

    ctx = await _bootstrap_ctx(tmp_path, monkeypatch)
    dispatcher, runner, exporter = _dispatcher(tmp_path)
    registry = _HybridRegistry(dispatcher)

    from harness_cp.workflow_driver import execute_workflow

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=loaded.manifest_entry,
            steps=steps,
            run_id=f"run-topology-{topology.value}",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=loaded.default_model_binding,
            step_dispatchers=registry,  # type: ignore[arg-type]
        )
    )

    assert result.status is RunStatus.SUCCESS, (
        f"{fixture}: status={result.status} fail_class={result.fail_class}"
    )
    # The child sub-agent (deterministic stand-in) was dispatched exactly once;
    # the admissibility gate accepted the ({topology}, {workload}) pairing.
    assert len(runner.calls) == 1
    spans = [s for s in exporter.get_finished_spans() if s.name == "subagent.span"]
    assert len(spans) == 1
    assert dict(spans[0].attributes or {})["topology.pattern"] == topology.value
