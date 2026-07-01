"""R-CC-1 arc #3 cascade step 1 — `api.resume(...)` workflow-layer durable-resume.

C-RT-35 (NEW): `harness_runtime.resume(workflow, *, pause_snapshot, ...)` — the
Track-A sibling of `run()` that continues a paused workflow from a caller-supplied
`PauseSnapshot` after a fresh bootstrap (a process restart). Design:
`.harness/r-cc-1-arc-3-workflow-durable-resume-design-v1.md` §7a.

Two tests:

1. `test_build_run_result_surfaces_paused` — the C-RT-09 surfacing half (unit):
   a CP `RunStatus.PAUSED` result → runtime `RunResult(status='paused',
   pause_snapshot=...)`. Guards the `_CP_TO_RT_STATUS` PAUSED mapping (which
   previously KeyError'd) + the `_build_run_result` snapshot carry-through.

2. `test_api_resume_restart_proof_round_trip` — the resume half (restart-proof
   e2e): a `PauseSnapshot` captured via the real bootstrapped protocol is
   JSON round-tripped (simulating the caller persisting it across a restart),
   then `api.resume(...)` on a *fresh* bootstrap drives the workflow to
   SUCCESS. Position-only resume is correct by the data-stateless execution
   model (design §1.1); the MVP `_pause_context_reader` constant sentinel →
   no material diff → STRICT admits the resume.

Substrate lifted from `test_run_smoke.py` (the get_tracer-capable
`_FakeTracerProvider` — `api.run`/`api.resume` create their `ctx` internally,
so the shared `patched_runtime` `_attach_get_tracer_to_ctx` post-bootstrap hack
does not reach them). Lifted by value per FM-2 cross-test-file independence.
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
    EvaluatorOptimizerResumeState,
    EvaluatorOptimizerStepResumeState,
    HandoffResumeState,
    HandoffStageResumeState,
    PauseSnapshot,
    WorkflowPauseReason,
)
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import StepDispatcher as _CpStepDispatcher
from harness_cp.workflow_driver_types import (
    RunResult as _CpRunResult,
)
from harness_cp.workflow_driver_types import (
    RunStatus as _CpRunStatus,
)
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.path_class_registry import PathClass
from harness_runtime.api import RunResult, _build_run_result, resume
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
_WORKFLOW_ID = "wf-r-cc-1-resume-e2e"

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


def _config_opt_in(tmp_path: Path) -> RuntimeConfig:
    """Config with the pause/resume protocol opted in (the same opt-in that
    produced the pause; required for `ctx.pause_resume_protocol` to bind)."""
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
    """A tracer provider that, unlike the shared `patched_runtime` fake, exposes
    `get_tracer` — required because the CP workflow_driver opens the
    workflow.envelope via `ctx.tracer_provider.get_tracer(...)`, and
    `api.resume` creates its `ctx` internally (cannot be patched post-bootstrap)."""

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
    """Patch providers + stage-4 OD + tracer with in-process fakes (the
    get_tracer-capable tracer is the load-bearing difference vs the shared
    fixture). Mirrors `test_run_smoke.py::_patched_runtime`."""
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


class _NoopDispatcher:
    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = binding, step_context
        return {"step_id": str(step.step_id), "ok": True}


def _single_kind_registry(dispatcher: Any) -> Any:
    class _Reg:
        def lookup(self, step_kind: Any) -> Any:
            _ = step_kind
            return dispatcher

    return _Reg()


class _Workflow:
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
        return _single_kind_registry(_NoopDispatcher())

    @property
    def default_model_binding(self) -> ModelBinding:
        return ModelBinding(provider="anthropic", model="claude-haiku-4-5")


# ---------------------------------------------------------------------------
# Test 1 — C-RT-09 surfacing half: a PAUSED CP result → status='paused'.
# ---------------------------------------------------------------------------


def test_build_run_result_surfaces_paused() -> None:
    """`_build_run_result` maps a CP `RunStatus.PAUSED` → runtime
    `RunResult(status='paused')` and carries the `pause_snapshot` through.

    Guards the `_CP_TO_RT_STATUS` PAUSED mapping (which previously had no entry
    → KeyError on any paused workflow) + the snapshot carry-through. C-RT-35 /
    C-RT-09."""
    snapshot = PauseSnapshot(
        workflow_id=_WORKFLOW_ID,
        run_id="run-paused-unit",
        step_index=0,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        state_summary=_minimal_state_summary(),
        snapshot_hash="0" * 64,
        created_at=1,
        state_ledger_anchor="0" * 64,
    )
    cp_result = _CpRunResult(
        workflow_id=_WORKFLOW_ID,
        run_id="run-paused-unit",
        status=_CpRunStatus.PAUSED,
        terminal_step_index=None,
        partial_state={},
        final_state=None,
        fail_class=None,
        pause_snapshot=snapshot,
    )

    class _ShutdownReport:
        audit_ledger_head_hash = "deadbeef"

    result = _build_run_result(cp_result, _ShutdownReport())

    assert isinstance(result, RunResult)
    assert result.status == "paused"
    assert result.pause_snapshot is snapshot
    assert result.failure_cause is None


def _minimal_state_summary() -> Any:
    from harness_cp.handoff_context import StateSummary
    from harness_is.state_ledger_entry_schema import Identifier

    return StateSummary(
        relevant_entries=(),
        summary_text="",
        summary_hash="0" * 64,
        idempotency_key=Identifier(""),
        external_references=(),
    )


# ---------------------------------------------------------------------------
# Test 2 — resume half: restart-proof round-trip through api.resume.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_resume_restart_proof_round_trip(
    tmp_path: Path,
    _patched_runtime: None,
) -> None:
    """A `PauseSnapshot` captured via the real bootstrapped protocol, JSON
    round-tripped (simulating the caller persisting it across a process
    restart), drives `api.resume(...)` on a *fresh* bootstrap to SUCCESS.

    Proves the full public resume path end-to-end across a bootstrap boundary:
    `resume()` re-bootstraps → entry-point resume detection fires
    `attempt_resume(snapshot, STRICT)` → MVP constant-anchor → no material diff
    → resume admitted → `resume_at_step_index=0` → NoopDispatcher reached →
    SUCCESS. Position-only resume is correct (data-stateless model, design §1.1).
    """
    _ = _patched_runtime
    config = _config_opt_in(tmp_path)

    # ---- "Pause" — capture a real PauseSnapshot via a bootstrapped protocol.
    capture_ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert capture_ctx.pause_resume_protocol is not None
    snapshot = await capture_ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id="run-resume-e2e",
        step_index=0,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
    )

    # ---- "Restart" — persist + reload across a process boundary (JSON round-trip).
    rehydrated = PauseSnapshot.model_validate_json(snapshot.model_dump_json())
    assert rehydrated == snapshot

    # ---- "Resume" — fresh bootstrap inside api.resume → continue to SUCCESS.
    result = await resume(_Workflow(), pause_snapshot=rehydrated, config=config)

    assert isinstance(result, RunResult)
    assert result.status == "completed", (
        f"expected completed resume, got {result.status}; failure_cause={result.failure_cause}"
    )
    assert result.workflow_id == _WORKFLOW_ID
    assert result.failure_cause is None
    # A successful resume terminal carries no pause_snapshot.
    assert result.pause_snapshot is None


@pytest.mark.asyncio
async def test_api_resume_corrupt_snapshot_surfaces_failed(
    tmp_path: Path,
    _patched_runtime: None,
) -> None:
    """A snapshot with a mutated `snapshot_hash` (transit/storage corruption)
    → `attempt_resume` returns FAILED before any step runs → `api.resume`
    surfaces `RunResult(status='failed')` carrying the CP fail-class. C-RT-35
    `RT-FAIL-RESUME-SNAPSHOT-CORRUPTION` family."""
    _ = _patched_runtime
    config = _config_opt_in(tmp_path)

    capture_ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert capture_ctx.pause_resume_protocol is not None
    snapshot = await capture_ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id="run-resume-corrupt",
        step_index=0,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
    )
    corrupted = snapshot.model_copy(update={"snapshot_hash": "f" * 64})

    result = await resume(_Workflow(), pause_snapshot=corrupted, config=config)

    assert result.status == "failed"
    assert result.failure_cause is not None
    assert result.failure_cause.validator_fail_class == "CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION"


# ---------------------------------------------------------------------------
# Resume preconditions (detect-then-refuse) — Codex-caught correctness gaps.
# ---------------------------------------------------------------------------


def _snapshot_for(workflow_id: str, *, step_index: int = 0) -> PauseSnapshot:
    return PauseSnapshot(
        workflow_id=workflow_id,
        run_id="run-precondition",
        step_index=step_index,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        state_summary=_minimal_state_summary(),
        snapshot_hash="0" * 64,
        created_at=1,
        state_ledger_anchor="0" * 64,
    )


@pytest.mark.asyncio
async def test_api_resume_no_protocol_opt_in_fails_loud(tmp_path: Path) -> None:
    """resume() with a config lacking the pause/resume opt-in fails fast
    (`ResumeProtocolNotBoundError`) pre-bootstrap — detect-then-refuse rather
    than SILENTLY re-running the workflow from step 0 (which would re-execute
    completed prefix steps + side effects). Codex-caught P1. No bootstrap
    occurs → no `_patched_runtime` fixture needed."""
    from harness_runtime.api import ResumeProtocolNotBoundError

    config = _config_opt_in(tmp_path).model_copy(update={"pause_resume_protocol_config": None})
    with pytest.raises(ResumeProtocolNotBoundError):
        await resume(_Workflow(), pause_snapshot=_snapshot_for(_WORKFLOW_ID), config=config)


@pytest.mark.asyncio
async def test_api_resume_workflow_id_mismatch_fails_loud(tmp_path: Path) -> None:
    """resume() with a snapshot from a DIFFERENT workflow fails fast
    (`ResumeWorkflowMismatchError`) pre-bootstrap — a snapshot may only resume
    its own workflow (else its hash validates against its own fields and the
    wrong run_id/step_index applies to these steps). Codex-caught P2."""
    from harness_runtime.api import ResumeWorkflowMismatchError

    config = _config_opt_in(tmp_path)
    with pytest.raises(ResumeWorkflowMismatchError):
        await resume(
            _Workflow(), pause_snapshot=_snapshot_for("some-other-workflow"), config=config
        )


@pytest.mark.asyncio
async def test_api_resume_step_index_out_of_range_fails_loud(tmp_path: Path) -> None:
    """resume() with a snapshot whose `step_index` is beyond the supplied
    workflow's steps (the workflow changed since the pause) fails fast
    (`ResumeStepIndexOutOfRangeError`) — else `steps[resume_at:]` is empty and
    a successful completed run executes nothing (silent false-success).
    Codex-caught (round 2). `_Workflow` has 1 step; step_index=5 is out of range."""
    from harness_runtime.api import ResumeStepIndexOutOfRangeError

    config = _config_opt_in(tmp_path)
    with pytest.raises(ResumeStepIndexOutOfRangeError):
        await resume(
            _Workflow(),
            pause_snapshot=_snapshot_for(_WORKFLOW_ID, step_index=5),
            config=config,
        )


# ===========================================================================
# Cascade step 2 — harness-owned durable JournalWorkflowPauseStore.
# (R-CC-1 arc #3 cascade step 2; design §7b.)
# ===========================================================================


def _config_durable(tmp_path: Path) -> RuntimeConfig:
    """`_config_opt_in` but with the DURABLE snapshot-store opt-in — the stage-5
    factory wraps the CP protocol in `DurablePauseResumeProtocol`."""
    return _config_opt_in(tmp_path).model_copy(
        update={"pause_resume_protocol_config": PauseResumeProtocolConfig(durable=True)}
    )


# ---- Store unit tests (no bootstrap) --------------------------------------


def test_durable_store_capture_read_round_trip(tmp_path: Path) -> None:
    """`capture` then `read_latest` returns the same `PauseSnapshot`."""
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    snap = _snapshot_for(_WORKFLOW_ID)
    store.capture(snap)
    assert store.read_latest(_WORKFLOW_ID) == snap


def test_durable_store_latest_record_wins(tmp_path: Path) -> None:
    """Two captures for one workflow → `read_latest` returns the LAST (a torn
    or stale earlier append must never be resumed in place of the latest)."""
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    first = _snapshot_for(_WORKFLOW_ID, step_index=0)
    second = _snapshot_for(_WORKFLOW_ID, step_index=0).model_copy(
        update={"run_id": "run-second", "created_at": 999}
    )
    store.capture(first)
    store.capture(second)
    latest = store.read_latest(_WORKFLOW_ID)
    assert latest is not None
    assert latest.run_id == "run-second"


def test_durable_store_missing_workflow_returns_none(tmp_path: Path) -> None:
    """No journal file for the workflow → `read_latest` returns `None`."""
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    assert store.read_latest("never-captured") is None


def test_durable_store_corrupt_latest_fails_closed(tmp_path: Path) -> None:
    """A torn/garbage TRAILING line → `read_latest` fails closed (`None`) rather
    than resuming an older snapshot or raising. (A crash mid-append leaves a torn
    last line; only the latest record is consulted.)"""
    import hashlib

    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    store.capture(_snapshot_for(_WORKFLOW_ID))
    # Append a torn trailing line directly to the workflow's journal file.
    digest = hashlib.sha256(_WORKFLOW_ID.encode("utf-8")).hexdigest()
    journal_file = tmp_path / "pj" / f"{digest}.jsonl"
    with journal_file.open("a", encoding="utf-8") as handle:
        handle.write('{"workflow_id": "wf", "pause_snapshot": {INCOMPLE\n')
    assert store.read_latest(_WORKFLOW_ID) is None


def test_durable_store_recovers_from_torn_unterminated_append(tmp_path: Path) -> None:
    """A crash mid-append can leave a partial trailing line with NO terminating
    newline. The next `capture()` must NOT concatenate onto that fragment (which
    would brick `read_latest` permanently). The store writes a leading newline so
    the fragment becomes its own (ignored) line and the new record is the clean
    latest line → resume self-heals. Codex-caught (round 3)."""
    import hashlib

    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    store.capture(_snapshot_for(_WORKFLOW_ID))
    # Simulate a crash mid-append: a partial record with NO trailing newline.
    digest = hashlib.sha256(_WORKFLOW_ID.encode("utf-8")).hexdigest()
    journal_file = tmp_path / "pj" / f"{digest}.jsonl"
    with journal_file.open("a", encoding="utf-8") as handle:
        handle.write('{"workflow_id": "wf", "pause_snapshot": {TORN-NO-NEWLINE')
    # A valid capture after the torn append must be cleanly recoverable.
    recovered = _snapshot_for(_WORKFLOW_ID).model_copy(update={"run_id": "run-after-torn"})
    store.capture(recovered)
    latest = store.read_latest(_WORKFLOW_ID)
    assert latest is not None, "torn append must not brick future resumes"
    assert latest.run_id == "run-after-torn"


def test_durable_store_per_workflow_isolation(tmp_path: Path) -> None:
    """Each workflow's pauses live in a dedicated file; a read for one workflow
    never returns another's snapshot."""
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    snap_a = _snapshot_for("wf-a")
    snap_b = _snapshot_for("wf-b")
    store.capture(snap_a)
    store.capture(snap_b)
    assert store.read_latest("wf-a") == snap_a
    assert store.read_latest("wf-b") == snap_b


# ---- Durable wrapper unit (capture persists + returns; read_latest delegates) ----


@pytest.mark.asyncio
async def test_durable_wrapper_persists_on_capture(tmp_path: Path) -> None:
    """`DurablePauseResumeProtocol.capture_pause_snapshot` composes the snapshot
    via the parent AND persists it to the store; a fresh store over the same dir
    reads it back (durable across instances — the cross-restart guarantee)."""
    from harness_runtime.lifecycle.durable_pause_resume_protocol import (
        DurablePauseResumeProtocol,
    )
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    protocol = DurablePauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (_minimal_state_summary(), "0" * 64),
        store=store,
    )

    returned = await protocol.capture_pause_snapshot(
        _WORKFLOW_ID, "run-x", 0, WorkflowPauseReason.EXPLICIT_OPERATOR
    )
    assert returned.workflow_id == _WORKFLOW_ID
    assert returned.run_id == "run-x"
    # A fresh store over the same dir reads it back (durable across instances —
    # the same path api.resume uses post-restart, not the in-process protocol).
    assert (
        JournalWorkflowPauseStore(journal_dir=tmp_path / "pj").read_latest(_WORKFLOW_ID) == returned
    )


@pytest.mark.asyncio
async def test_durable_wrapper_forwards_handoff_resume(tmp_path: Path) -> None:
    """B-HANDOFF-PAUSE regression (Codex-caught [P1]): the durable wrapper's
    `capture_pause_snapshot` override MUST accept + forward the `handoff_resume`
    carrier. Under durable pause/resume config a `DECENTRALIZED_HANDOFF` pause calls
    `capture_pause_snapshot(handoff_resume=...)`; a wrapper that only accepted the
    fan-out carriers would raise `TypeError` before returning PAUSED (and silently
    drop the cursor from the journal). Assert the carrier survives the durable
    capture + the cross-instance journal read-back."""
    from harness_runtime.lifecycle.durable_pause_resume_protocol import (
        DurablePauseResumeProtocol,
    )
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    protocol = DurablePauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (_minimal_state_summary(), "0" * 64),
        store=store,
    )
    handoff_resume = HandoffResumeState(
        completed_stages=(
            HandoffStageResumeState(stage_index=0, step_id="s0", output={"role": "s0"}),
        ),
        stage_count=2,
    )

    returned = await protocol.capture_pause_snapshot(
        _WORKFLOW_ID,
        "run-handoff",
        1,
        WorkflowPauseReason.EXPLICIT_OPERATOR,
        handoff_resume=handoff_resume,
    )
    # The carrier survived the durable capture (forwarded to the parent, not dropped).
    assert returned.handoff_resume == handoff_resume
    assert returned.fan_out_resume is None
    assert returned.peer_fan_out_resume is None
    # And it round-trips through the journal a fresh store (the cross-restart path).
    read_back = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj").read_latest(_WORKFLOW_ID)
    assert read_back == returned
    assert read_back is not None
    assert read_back.handoff_resume == handoff_resume


@pytest.mark.asyncio
async def test_durable_wrapper_forwards_evaluator_optimizer_resume(tmp_path: Path) -> None:
    """B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER regression (the #681 Codex [P1] precedent): the
    durable wrapper's `capture_pause_snapshot` override MUST accept + forward the
    `evaluator_optimizer_resume` carrier. Under durable pause/resume config an
    `EVALUATOR_OPTIMIZER` pause calls `capture_pause_snapshot(evaluator_optimizer_resume=
    ...)`; a wrapper that only accepted the prior 3 carriers would raise `TypeError` before
    returning PAUSED (and silently drop the iteration cursor from the journal). Assert the
    carrier survives the durable capture + the cross-instance journal read-back."""
    from harness_runtime.lifecycle.durable_pause_resume_protocol import (
        DurablePauseResumeProtocol,
    )
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

    store = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj")
    protocol = DurablePauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (_minimal_state_summary(), "0" * 64),
        store=store,
    )
    eo_resume = EvaluatorOptimizerResumeState(
        completed_steps=(
            EvaluatorOptimizerStepResumeState(
                entry_index=0, declared_step_index=0, step_id="generate", output={"draft": 1}
            ),
            EvaluatorOptimizerStepResumeState(
                entry_index=1,
                declared_step_index=1,
                step_id="evaluate",
                output={"accepted": False},
            ),
        ),
    )

    returned = await protocol.capture_pause_snapshot(
        _WORKFLOW_ID,
        "run-eo",
        2,
        WorkflowPauseReason.EXPLICIT_OPERATOR,
        evaluator_optimizer_resume=eo_resume,
    )
    # The carrier survived the durable capture (forwarded to the parent, not dropped).
    assert returned.evaluator_optimizer_resume == eo_resume
    assert returned.fan_out_resume is None
    assert returned.peer_fan_out_resume is None
    assert returned.handoff_resume is None
    # And it round-trips through the journal a fresh store (the cross-restart path).
    read_back = JournalWorkflowPauseStore(journal_dir=tmp_path / "pj").read_latest(_WORKFLOW_ID)
    assert read_back == returned
    assert read_back is not None
    assert read_back.evaluator_optimizer_resume == eo_resume


# ---- Restart-proof e2e via the harness-owned store (resume_handle) ----------


@pytest.mark.asyncio
async def test_api_resume_durable_handle_restart_proof(
    tmp_path: Path,
    _patched_runtime: None,
) -> None:
    """The harness-owned durability path: capture through a DURABLE-bootstrapped
    protocol persists the snapshot to disk; a *fresh* bootstrap +
    `api.resume(workflow, resume_handle=workflow_id)` reads it back from the
    journal under the resolved STATE_LEDGER dir and drives the workflow to
    SUCCESS — the caller never persisted the snapshot itself (crash-recovery)."""
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
        pause_journal_dir_for,
    )

    _ = _patched_runtime
    config = _config_durable(tmp_path)

    # ---- "Pause" — capture via the DURABLE protocol → persists to the journal.
    capture_ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert capture_ctx.pause_resume_protocol is not None
    snapshot = await capture_ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id="run-durable-e2e",
        step_index=0,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
    )

    # ---- The harness owns the durable copy (NOT the caller). A fresh store over
    # the resolved STATE_LEDGER pause-journal dir reads it back across instances.
    state_ledger_dir = tmp_path / PathClass.STATE_LEDGER.value.lower()
    durable = JournalWorkflowPauseStore(
        journal_dir=pause_journal_dir_for(state_ledger_dir)
    ).read_latest(_WORKFLOW_ID)
    assert durable == snapshot

    # ---- "Resume" by HANDLE — fresh bootstrap reads the snapshot itself.
    result = await resume(_Workflow(), resume_handle=_WORKFLOW_ID, config=config)

    assert isinstance(result, RunResult)
    assert result.status == "completed", (
        f"expected completed resume, got {result.status}; failure_cause={result.failure_cause}"
    )
    assert result.workflow_id == _WORKFLOW_ID
    assert result.pause_snapshot is None


@pytest.mark.asyncio
async def test_api_resume_durable_handle_skips_completed_prefix(
    tmp_path: Path,
    _patched_runtime: None,
) -> None:
    """Position-only resume through the durable handle SKIPS the completed prefix.

    A 2-step workflow durably paused at `step_index=1` resumes via `resume_handle`
    and dispatches ONLY step-1 — step-0 (the completed prefix) is NOT re-executed,
    so its side effects do not re-fire. This proves the core resume guarantee
    (continue from step k without re-running 0..k-1) end-to-end, which a 1-step
    pause-at-0 e2e cannot distinguish from a fresh run."""
    _ = _patched_runtime
    config = _config_durable(tmp_path)

    dispatched: list[str] = []

    class _RecordingDispatcher:
        def dispatch(
            self, binding: Any, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            _ = binding, step_context
            dispatched.append(str(step.step_id))
            return {"step_id": str(step.step_id), "ok": True}

    class _TwoStepWorkflow(_Workflow):
        @property
        def steps(self) -> Sequence[WorkflowStep]:
            return (
                WorkflowStep(
                    step_id=StepID("step-0"),
                    step_kind=StepKind.INFERENCE_STEP,
                    step_payload={"index": 0},
                ),
                WorkflowStep(
                    step_id=StepID("step-1"),
                    step_kind=StepKind.INFERENCE_STEP,
                    step_payload={"index": 1},
                ),
            )

        @property
        def step_dispatchers(self) -> Any:
            return _single_kind_registry(_RecordingDispatcher())

    workflow = _TwoStepWorkflow()

    # ---- "Pause" at step_index=1 — capture via the DURABLE protocol → journaled.
    capture_ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert capture_ctx.pause_resume_protocol is not None
    await capture_ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id="run-prefix-skip",
        step_index=1,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
    )

    # ---- "Resume" by HANDLE — fresh bootstrap; only step-1 runs.
    result = await resume(workflow, resume_handle=_WORKFLOW_ID, config=config)

    assert result.status == "completed", (
        f"expected completed, got {result.status}; failure_cause={result.failure_cause}"
    )
    # The completed prefix (step-0) is NOT re-dispatched; only step-1 runs.
    assert dispatched == ["step-1"], (
        f"resume must skip the completed prefix (step-0); dispatched={dispatched}"
    )


# ---- api.resume arg guards (detect-then-refuse) -----------------------------


@pytest.mark.asyncio
async def test_api_resume_both_sources_fails_loud(tmp_path: Path) -> None:
    """Supplying BOTH `pause_snapshot` and `resume_handle` → `ResumeArgsError`
    (ambiguous source) pre-bootstrap."""
    from harness_runtime.api import ResumeArgsError

    config = _config_durable(tmp_path)
    with pytest.raises(ResumeArgsError):
        await resume(
            _Workflow(),
            pause_snapshot=_snapshot_for(_WORKFLOW_ID),
            resume_handle=_WORKFLOW_ID,
            config=config,
        )


@pytest.mark.asyncio
async def test_api_resume_neither_source_fails_loud(tmp_path: Path) -> None:
    """Supplying NEITHER source → `ResumeArgsError` (nothing to resume)."""
    from harness_runtime.api import ResumeArgsError

    config = _config_durable(tmp_path)
    with pytest.raises(ResumeArgsError):
        await resume(_Workflow(), config=config)


@pytest.mark.asyncio
async def test_api_resume_handle_without_durable_fails_loud(tmp_path: Path) -> None:
    """`resume_handle` without the durable opt-in → `ResumeArgsError`: there is
    no harness-owned store to read from."""
    from harness_runtime.api import ResumeArgsError

    config = _config_opt_in(tmp_path)  # opted into pause/resume, but durable=False
    with pytest.raises(ResumeArgsError):
        await resume(_Workflow(), resume_handle=_WORKFLOW_ID, config=config)


@pytest.mark.asyncio
async def test_api_resume_unknown_handle_fails_loud(tmp_path: Path) -> None:
    """`resume_handle` for a workflow with no journaled pause → fail fast
    (`ResumeHandleUnknownError`) pre-bootstrap rather than silently re-run from
    step 0. No capture happened, so the store read returns `None`."""
    from harness_runtime.api import ResumeHandleUnknownError

    config = _config_durable(tmp_path)
    with pytest.raises(ResumeHandleUnknownError):
        await resume(_Workflow(), resume_handle="never-paused", config=config)


@pytest.mark.asyncio
async def test_api_resume_handle_concurrency_guard_precedes_store_read(
    tmp_path: Path,
) -> None:
    """With a `run()`/`resume()` in flight (the run lock held), a `resume_handle`
    call surfaces `ConcurrentRunNotSupported` — NOT a spurious
    `ResumeHandleUnknownError` from reading the shared journal mid-flight. The
    concurrency guard must precede the durable-store read. Codex-caught ordering
    (round 4): here NO snapshot is journaled, so without the guard-first ordering
    the store read would return `None` → `ResumeHandleUnknownError`; the held lock
    must win instead."""
    from harness_runtime.api import ConcurrentRunNotSupported, _run_lock

    config = _config_durable(tmp_path)
    async with _run_lock:  # simulate an in-flight run/resume holding the lock
        with pytest.raises(ConcurrentRunNotSupported):
            await resume(_Workflow(), resume_handle="never-paused", config=config)
