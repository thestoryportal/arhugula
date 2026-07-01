"""`harness_runtime.run` + `RunResult` shape tests.

ACs (U-RT-42 + Lane 6 2026-05-20 un-strike of U-RT-44 AC #2 + U-RT-49
workflow-execution ACs):

1. Signature pinned: `async def run(workflow, *, config=None) -> RunResult`.
2. `RunResult` is frozen Pydantic v2 with C-RT-09 field set.
3. Workflow validation: non-`WorkflowObject` input → `InvalidWorkflowError`
   (pre-bootstrap rejection).
4. Concurrency guard: second concurrent `run()` → `ConcurrentRunNotSupported`
   (C-RT-08 v1.1 idempotency-and-concurrency).
5. Bootstrap-then-execute wiring: valid-shape `run()` runs bootstrap and
   delegates to `harness_cp.workflow_driver.execute_workflow()`; the
   former `WorkflowExecutionNotYetLandedError` stub is removed at Lane 6.
6. Module-level lock is `asyncio.Lock`; re-export wiring at package root.
7. Pre-bootstrap drain rejection (C-RT-11 surface (3)).

End-to-end execution behavior is exercised at
`tests/integration/test_run_smoke.py`; these tests focus on ingress paths.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from typing import Any, Literal

import harness_runtime
import harness_runtime.api as _api
import pytest
from harness_core.identity import StepID, WorkflowID
from harness_core.persona_tier import PersonaTier
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import StepDispatcher
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_od.cross_family_rollup import CrossFamilyCostRollup, RollupAxis
from harness_runtime.api import (
    ConcurrentRunNotSupported,
    FailureCause,
    HarnessDraining,
    InvalidWorkflowError,
    RunResult,
    WorkflowObject,
    run,
)
from harness_runtime.types import CostRecordAccumulator, RuntimeConfig

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_TEST_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_TEST_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic",
        model="claude-haiku-4-5",
        family=ProviderFamily.ANTHROPIC,
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)


def _test_manifest_entry(workflow_id: str = "wf-test-1") -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_TEST_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


class _NoopDispatcher:
    def dispatch(self, binding: object, step: WorkflowStep) -> dict[str, object]:
        _ = binding
        return {"step_id": str(step.step_id)}


class _Workflow:
    """Structural `WorkflowObject` for tests — full Lane 6 surface."""

    def __init__(
        self,
        workflow_id: str = "wf-test-1",
        workload_class: WorkloadClass = WorkloadClass.SOFTWARE_ENGINEERING,
    ) -> None:
        self._wid = workflow_id
        self._wc = workload_class
        self._manifest = _test_manifest_entry(workflow_id)
        self._steps: tuple[WorkflowStep, ...] = (
            WorkflowStep(
                step_id=StepID("step-0"),
                step_kind=StepKind.INFERENCE_STEP,
                step_payload={"index": 0},
            ),
        )
        self._dispatcher = _NoopDispatcher()

    @property
    def workflow_id(self) -> str:
        return self._wid

    @property
    def workload_class(self) -> WorkloadClass:
        return self._wc

    @property
    def manifest_entry(self) -> WorkflowManifestEntry:
        return self._manifest

    @property
    def steps(self) -> Sequence[WorkflowStep]:
        return self._steps

    @property
    def step_dispatcher(self) -> StepDispatcher:
        # Cast at the call site — _NoopDispatcher structurally satisfies StepDispatcher.
        return self._dispatcher  # type: ignore[return-value]

    @property
    def default_model_binding(self) -> ModelBinding:
        return _TEST_BINDING


def _rollup() -> CrossFamilyCostRollup:
    return CrossFamilyCostRollup(
        rollup_axis=RollupAxis.PER_PROVIDER_DISCRIMINATOR,
        group_key="anthropic",
        total_cost=0.0,
        span_count=1,
    )


# ---------------------------------------------------------------------------
# AC #1 — Signature.
# ---------------------------------------------------------------------------


def test_run_is_async() -> None:
    assert inspect.iscoroutinefunction(run)


def test_run_signature_matches_spec() -> None:
    """`async def run(workflow, *, config=None) -> RunResult` per C-RT-08."""
    sig = inspect.signature(run)
    params = list(sig.parameters.items())
    assert params[0][0] == "workflow"
    assert params[1][0] == "config"
    assert sig.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["config"].default is None
    assert sig.return_annotation == "RunResult"


# ---------------------------------------------------------------------------
# AC #2 — RunResult shape (C-RT-09).
# ---------------------------------------------------------------------------


def test_run_result_is_frozen() -> None:
    result = RunResult(
        status="completed",
        workflow_id=WorkflowID("wf-1"),
        terminal_state={},
        audit_ledger_head_hash="0" * 64,
        trace_ids=(),
        cost_attribution=(),
    )
    with pytest.raises(Exception):
        result.status = "failed"  # type: ignore[misc]


def test_run_result_all_required_fields_per_c_rt_09() -> None:
    fields = set(RunResult.model_fields)
    assert fields == {
        "status",
        "workflow_id",
        "terminal_state",
        "audit_ledger_head_hash",
        "trace_ids",
        "cost_attribution",
        # NEW v1.57 (B-COST-DISCRIMINATOR-TAXONOMY) — optional dispatch-type rollup.
        "cost_attribution_by_dispatch_kind",
        # NEW v1.58 (B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION) — optional
        # cross-family family-tag rollup (LLM-subtotal partition).
        "cost_attribution_by_provider_discriminator",
        "failure_cause",
        # NEW v1.45 (C-RT-35, R-CC-1 arc #3) — optional pause-snapshot surface.
        "pause_snapshot",
    }


def test_run_result_status_literal_values() -> None:
    """`status: Literal['completed', 'drained', 'failed', 'paused', 'partial']`
    per C-RT-09 (v1.45 added 'paused' for C-RT-35 resume; U-RT-113 added
    'partial' for R-FS-1 B1 proceed-cascade graceful degradation). The
    `pause_snapshot is not None iff status=='paused'` invariant is documented,
    not model-enforced — same posture as the `status=='failed' -> failure_cause`
    sibling invariant."""
    field = RunResult.model_fields["status"]
    # Pydantic v2 stores Literal in field annotation; round-trip every literal.
    for value in ("completed", "drained", "failed", "paused", "partial"):
        result = RunResult(
            status=value,  # type: ignore[arg-type]
            workflow_id=WorkflowID("wf-1"),
            terminal_state={},
            audit_ledger_head_hash="0" * 64,
            trace_ids=(),
            cost_attribution=(),
            failure_cause=(
                FailureCause(runtime_fail_class="RT-FAIL-BOOTSTRAP", detail="x")
                if value == "failed"
                else None
            ),
        )
        assert result.status == value
    _ = field  # annotation introspection is Pydantic-version-specific


def test_run_result_rejects_unknown_status() -> None:
    with pytest.raises(Exception):
        RunResult(
            status="unknown",  # type: ignore[arg-type]
            workflow_id=WorkflowID("wf-1"),
            terminal_state={},
            audit_ledger_head_hash="0" * 64,
            trace_ids=(),
            cost_attribution=(),
        )


def test_run_result_cost_attribution_carries_cross_family_rollup() -> None:
    """`cost_attribution` is `tuple[CrossFamilyCostRollup, ...]` — Class 3 drift note."""
    result = RunResult(
        status="completed",
        workflow_id=WorkflowID("wf-1"),
        terminal_state={},
        audit_ledger_head_hash="0" * 64,
        trace_ids=(),
        cost_attribution=(_rollup(),),
    )
    assert isinstance(result.cost_attribution[0], CrossFamilyCostRollup)


def test_failure_cause_mirrors_c_rt_14() -> None:
    cause = FailureCause(
        runtime_fail_class="RT-FAIL-BOOTSTRAP",
        detail="stage 1 IS failed",
    )
    assert cause.runtime_fail_class == "RT-FAIL-BOOTSTRAP"
    assert cause.validator_fail_class is None


def test_build_run_result_projects_partial() -> None:
    """U-RT-113: `_build_run_result` maps a CP `RunStatus.PARTIAL` → runtime
    `RunResult(status='partial')` (proceed-cascade graceful degradation).

    `failure_cause` stays None (a degraded run did not fail — the
    `elif status == 'failed'` branch does not fire); `terminal_state` carries
    the partial aggregate (`partial_state`). Guards the `_CP_TO_RT_STATUS`
    PARTIAL → 'partial' flip (was the v1.4 defensive 'failed' placeholder).
    C-RT-09 §9 / CP spec v1.32 §25.15.1.

    NOTE — the *integration* AC (a real `proceed`-cascade fan-out returning
    PARTIAL end-to-end) is DEFERRED to a later B1-impl-N PR: no driver strategy
    returns PARTIAL until U-CP-85 (cascade_policy) + a fan-out strategy land.
    This is the functional projection unit."""
    from harness_cp.workflow_driver_types import RunResult as _CpRunResult
    from harness_cp.workflow_driver_types import RunStatus as _CpRunStatus
    from harness_runtime.api import _build_run_result

    cp_result = _CpRunResult(
        workflow_id="wf-partial",
        run_id="run-partial-unit",
        status=_CpRunStatus.PARTIAL,
        terminal_step_index=None,
        partial_state={"branch_0": "ok"},
        final_state=None,
        fail_class=None,
    )

    class _ShutdownReport:
        audit_ledger_head_hash = "deadbeef"

    result = _build_run_result(cp_result, _ShutdownReport())
    assert isinstance(result, RunResult)
    assert result.status == "partial"
    assert result.failure_cause is None
    assert result.terminal_state == {"branch_0": "ok"}


# ---------------------------------------------------------------------------
# AC #3 — Workflow validation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_rejects_non_workflow_object() -> None:
    with pytest.raises(InvalidWorkflowError):
        await run("not-a-workflow-object")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_rejects_object_missing_workflow_id_property() -> None:
    class _BadObj:
        pass

    with pytest.raises(InvalidWorkflowError):
        await run(_BadObj())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC #4 — Concurrency guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_raises_on_concurrent_invocation() -> None:
    """Holding the module-level lock surfaces `ConcurrentRunNotSupported`."""
    await _api._run_lock.acquire()  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(ConcurrentRunNotSupported):
            await run(_Workflow())
    finally:
        _api._run_lock.release()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# AC #5 — Bootstrap → execute → shutdown wiring (Lane 6).
# ---------------------------------------------------------------------------


def _fake_run_result(workflow_id: str = "wf-test-1") -> RunResult:
    """A pre-baked runtime RunResult used by the bootstrap-monkeypatch tests
    below; the bootstrap-and-shutdown machinery is mocked at the run-body
    boundary so this file stays an ingress-path test surface."""
    return RunResult(
        status="completed",
        workflow_id=WorkflowID(workflow_id),
        terminal_state={},
        audit_ledger_head_hash="",
        trace_ids=(),
        cost_attribution=(),
        failure_cause=None,
    )


@pytest.mark.asyncio
async def test_valid_run_executes_via_driver_and_returns_run_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid-shape `run()` reaches the driver delegation site.

    The bootstrap path is faked via monkeypatch; the driver itself is
    short-circuited at `asyncio.to_thread` via a stub that returns a
    runtime RunResult directly. Real driver behavior is exercised at
    `tests/integration/test_run_smoke.py`.
    """
    import sys

    _shutdown_mod = sys.modules["harness_runtime.shutdown"]

    from types import SimpleNamespace

    # U-RT-62 AC #5 — `api.run()` now delegates execution via the in-process
    # MCP tool path. The fake bootstrap returns a minimal context carrying
    # a `mcp_server` namespace (the api.run body writes `_state[
    # '_harness_ctx']` + `workflow_registry[workflow_id]` on it before
    # the tool invocation); the in-process tool invocation is stubbed at
    # the helper level rather than at `asyncio.to_thread` per the v1.12
    # internal layout.
    fake_mcp_server = SimpleNamespace(
        server=object(),
        _state={},
        workflow_registry={},
    )
    fake_ctx = SimpleNamespace(
        mcp_server=fake_mcp_server, cost_record_accumulator=CostRecordAccumulator()
    )

    async def _fake_bootstrap(config, *, workload_class, requires_inference=True):  # type: ignore[no-untyped-def]
        _ = config
        _ = workload_class
        return fake_ctx

    async def _fake_invoke(fastmcp_server, workflow_id):  # type: ignore[no-untyped-def]
        _ = fastmcp_server, workflow_id
        from harness_cp.workflow_driver_types import (
            RunResult as _CpRR,
        )
        from harness_cp.workflow_driver_types import (
            RunStatus as _CpRS,
        )

        return _CpRR(
            workflow_id="wf-test-1",
            run_id="run-fake",
            status=_CpRS.SUCCESS,
            final_state={},
        )

    async def _fake_shutdown(ctx, *, timeout=5.0):  # type: ignore[no-untyped-def]
        _ = ctx, timeout
        return _shutdown_mod.ShutdownReport(
            already_shutdown=False,
            timed_out=False,
            flush=_shutdown_mod.FlushReport(
                tracer_flushed=True,
                ledger_fsynced=True,
                cost_chain_noop=True,
                timed_out=False,
                failures=(),
            ),
            failures=(),
            audit_ledger_head_hash=None,
        )

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _fake_bootstrap)
    monkeypatch.setattr(_api, "_default_config", lambda: SimpleNamespace(drain_timeout_seconds=5.0))
    monkeypatch.setattr(_api, "_invoke_run_workflow_via_in_process_mcp", _fake_invoke)
    monkeypatch.setattr(_shutdown_mod, "shutdown", _fake_shutdown)

    result = await run(_Workflow())
    assert result.status == "completed"
    assert result.workflow_id == "wf-test-1"


@pytest.mark.asyncio
async def test_run_releases_lock_after_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful `run()` releases the lock; subsequent calls work."""
    import sys

    _shutdown_mod = sys.modules["harness_runtime.shutdown"]

    from types import SimpleNamespace

    # U-RT-62 AC #5 — stub at the MCP tool invocation layer (see prior test
    # body for explanation). Each call yields a fresh fake ctx so the
    # mutable holders don't carry state across the second invocation.
    def _make_fake_ctx() -> Any:
        return SimpleNamespace(
            mcp_server=SimpleNamespace(
                server=object(),
                _state={},
                workflow_registry={},
            ),
            cost_record_accumulator=CostRecordAccumulator(),
        )

    async def _fake_bootstrap(config, *, workload_class, requires_inference=True):  # type: ignore[no-untyped-def]
        _ = config
        _ = workload_class
        return _make_fake_ctx()

    async def _fake_invoke(fastmcp_server, workflow_id):  # type: ignore[no-untyped-def]
        _ = fastmcp_server, workflow_id
        from harness_cp.workflow_driver_types import (
            RunResult as _CpRR,
        )
        from harness_cp.workflow_driver_types import (
            RunStatus as _CpRS,
        )

        return _CpRR(
            workflow_id="wf-test-1",
            run_id="run-fake",
            status=_CpRS.SUCCESS,
            final_state={},
        )

    async def _fake_shutdown(ctx, *, timeout=5.0):  # type: ignore[no-untyped-def]
        _ = ctx, timeout
        return _shutdown_mod.ShutdownReport(
            already_shutdown=False,
            timed_out=False,
            flush=_shutdown_mod.FlushReport(
                tracer_flushed=True,
                ledger_fsynced=True,
                cost_chain_noop=True,
                timed_out=False,
                failures=(),
            ),
            failures=(),
            audit_ledger_head_hash=None,
        )

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _fake_bootstrap)
    monkeypatch.setattr(_api, "_default_config", lambda: SimpleNamespace(drain_timeout_seconds=5.0))
    monkeypatch.setattr(_api, "_invoke_run_workflow_via_in_process_mcp", _fake_invoke)
    monkeypatch.setattr(_shutdown_mod, "shutdown", _fake_shutdown)

    _ = await run(_Workflow())
    # Lock is released, so a second call also reaches the driver delegation.
    _ = await run(_Workflow())
    assert not _api._run_lock.locked()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# AC #6 — Module-level lock + package-root re-export.
# ---------------------------------------------------------------------------


def test_run_lock_is_asyncio_lock() -> None:
    assert isinstance(_api._run_lock, asyncio.Lock)  # pyright: ignore[reportPrivateUsage]


def test_package_root_re_exports_api() -> None:
    """`harness_runtime.run`, `RunResult`, errors, `WorkflowObject` at package root."""
    assert harness_runtime.run is run
    assert harness_runtime.RunResult is RunResult
    assert harness_runtime.WorkflowObject is WorkflowObject
    assert harness_runtime.InvalidWorkflowError is InvalidWorkflowError
    assert harness_runtime.ConcurrentRunNotSupported is ConcurrentRunNotSupported
    assert harness_runtime.FailureCause is FailureCause


def test_workflow_execution_stub_error_removed_at_lane_6() -> None:
    """`WorkflowExecutionNotYetLandedError` is removed at Lane 6 (2026-05-20).

    The stub-call surface vanishes once `run()` delegates to the CP driver.
    Anchor test: importing the removed symbol from `harness_runtime` raises
    `AttributeError`.
    """
    with pytest.raises(AttributeError):
        _ = harness_runtime.WorkflowExecutionNotYetLandedError  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC #7 (U-RT-44) — Pre-bootstrap drain rejection (C-RT-11 surface (3)).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_raises_harness_draining_when_process_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-RT-11 surface (3) — drained process refuses new `run()` invocations.

    `monkeypatch.setattr` restores the original module attribute at test
    teardown, so the one-way `_process_drained` flag is reset between tests.
    """
    from harness_runtime import drain as drain_mod

    monkeypatch.setattr(drain_mod, "_process_drained", True)
    with pytest.raises(HarnessDraining):
        await run(_Workflow())


@pytest.mark.asyncio
async def test_harness_draining_raised_before_workflow_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain check fires pre-validation — bad workflow + drained → `HarnessDraining`."""
    from harness_runtime import drain as drain_mod

    monkeypatch.setattr(drain_mod, "_process_drained", True)
    with pytest.raises(HarnessDraining):
        await run("not-a-workflow")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_harness_draining_raised_before_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drained process surfaces `HarnessDraining` even with the lock held."""
    from harness_runtime import drain as drain_mod

    monkeypatch.setattr(drain_mod, "_process_drained", True)
    await _api._run_lock.acquire()  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(HarnessDraining):
            await run(_Workflow())
    finally:
        _api._run_lock.release()  # pyright: ignore[reportPrivateUsage]


def test_harness_draining_is_distinct_typed_error() -> None:
    """`HarnessDraining` is `Exception`-rooted (not NotImplementedError-rooted)."""
    assert issubclass(HarnessDraining, Exception)
    assert not issubclass(HarnessDraining, NotImplementedError)
    assert not issubclass(HarnessDraining, InvalidWorkflowError)
    assert not issubclass(HarnessDraining, ConcurrentRunNotSupported)


def test_harness_draining_re_exported_at_package_root() -> None:
    assert harness_runtime.HarnessDraining is HarnessDraining


def test_run_accepts_optional_config(tmp_path: object) -> None:
    """`config=None` default; both call forms reach the bootstrap stub."""
    # Signature carries `config: RuntimeConfig | None = None`.
    sig = inspect.signature(run)
    assert sig.parameters["config"].annotation == "RuntimeConfig | None"


def test_workflow_object_protocol_runtime_checkable() -> None:
    """`isinstance(_Workflow(), WorkflowObject)` works (runtime_checkable)."""
    assert isinstance(_Workflow(), WorkflowObject)
    assert not isinstance("string", WorkflowObject)


_ = Literal  # silence unused-import on the imported Literal anchor
_ = RuntimeConfig  # signature-introspection anchor
