"""R-FS-1 `B-NONLINEAR-OVERRIDE-PROVENANCE` — per-step override-ledger entry on
the non-linear topology strategies (CP spec v1.40 §6.6).

At HEAD only the `SINGLE_THREADED_LINEAR` driver path emitted the
`cp.per-step-override-application` state-ledger entry; a per-step override on any
of the 5 non-linear strategies (PARALLELIZATION / EVALUATOR_OPTIMIZER /
ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION / DECENTRALIZED_HANDOFF) was
applied at dispatch but left **no dedicated override-ledger entry** — the v1.39
§6.6 honest-scope gap. This suite proves the gap is closed: the entry is now
emitted **through the buffered-branch path** (`append_branch_override_ledger_entry`
buffered into the branch's `BufferingLedgerWriter`, serialized on the driver
thread by `drain_branch_buffers` per ADR-F2 v1.2 single-threaded-write), with
the persisted entry **byte-shape-identical** to the linear path (same `action_id`
+ the §16.5.4 `(workflow_id, step_id, outcome_hash)` idempotency key).

Each test runs through `execute_workflow` into the REAL IS writer, so the §6.3
hash chain re-verifies AND the §16.5.4 idempotency dedup is genuinely exercised
(the EVALUATOR_OPTIMIZER repeated-step case asserts the deliberate dedup-to-one,
NOT a per-iteration over-record). Provider-free (declarative steps) — no
credential needed.

Authority: `Spec_Control_Plane_v1_40.md` §6.6 + §3 (the topology-scope refresh)
+ `Spec_Control_Plane_v1_38.md` §6.6 (the provenance contract) +
`.harness/class_1_fork_b_nonlinear_override_provenance.md`.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

from harness_core import PersonaTier, StepID, WorkloadClass
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    DriverContext,
    StepDispatcher,
    StepDispatcherRegistry,
    StepKindDispatcherNotBoundError,
    execute_workflow,
)
from harness_cp.workflow_driver_types import RunStatus, StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_write import WriteResult, append_ledger_entry, read_ledger

# The CP spec §16.5.3 canonical override-application action_id — the load-bearing
# surface a provenance consumer filters on (asserting the literal locks the
# byte-shape-identical-to-linear property; a drift here is the regression).
_OVERRIDE_ACTION_ID = "cp.per-step-override-application"

_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_OVERRIDE_BINDING = ModelBinding(provider="anthropic", model="claude-opus-4-7")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-nonlinear-override")


def _override(step_id: str) -> StepOverride:
    """A per-step model-binding override (→ `override_applied=True` for the step)."""
    return StepOverride(step_id=StepID(step_id), model_binding=_OVERRIDE_BINDING)


def _manifest(
    *,
    topology_pattern: TopologyPattern,
    overrides: dict[str, StepOverride],
    persona_tier: PersonaTier = PersonaTier.SOLO_DEVELOPER,
    workflow_id: str = "wf-nlo",
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=topology_pattern,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides=overrides,
    )


def _declarative(step_id: str, **payload: Any) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(step_id), step_kind=StepKind.DECLARATIVE_STEP, step_payload=payload
    )


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
        self.emits.append(event_class)


class _Ctx:
    """Minimal fake `DriverContext`. No `procedural_tier_snapshot_resolver` bound
    → the R-003 sidecar stays `None` (the resolver-less path the override helper
    forwards as `procedural_tier_snapshot_ref=None`)."""

    def __init__(self, *, ledger: Any, emitter: _Emitter) -> None:
        import asyncio

        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = emitter
        self.drained_flag = asyncio.Event()
        self.pause_resume_protocol = None
        self.pause_requested_flag = asyncio.Event()
        self.ledger_reader = None
        self.tracer_provider = NoOpTracerProvider()
        self.validator_framework = None
        self.tenant_id = None


class _RealLedger:
    """Drain sink backed by the REAL IS writer (dedup + monotonicity + hash-chain
    + JSONL persistence all exercised). Lock-guarded — the concurrent fan-out
    strategies drain from a `cp-fanout` worker thread."""

    actor: Actor

    def __init__(self, *, handle: JsonlLedgerHandle) -> None:
        self._handle = handle
        self.actor = _ACTOR
        self.results: list[WriteResult] = []
        self._lock = threading.Lock()

    def append(self, payload: Any, write_key: Any) -> WriteResult:
        with self._lock:
            result = append_ledger_entry(self._handle, payload, write_key)
            self.results.append(result)
            return result

    @property
    def is_genesis(self) -> bool:
        return len(self.results) == 0

    @property
    def entry_count(self) -> int:
        return len(self.results)


class _EchoDispatcher:
    """Echoes a per-step output for DECLARATIVE_STEP and SUB_AGENT_DISPATCH."""

    def __init__(self) -> None:
        self.dispatched_step_ids: list[str] = []
        self.seen_bindings: dict[str, StepEffectiveBinding] = {}

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        self.dispatched_step_ids.append(sid)
        self.seen_bindings[sid] = binding
        return {"step": sid, "echoed": dict(step.step_payload)}


class _FailingEchoDispatcher:
    """Echoes, but RAISES for one step_id — to exercise the fan-out FAILURE drain
    path with an override entry present."""

    def __init__(self, *, fail_step_id: str) -> None:
        self._fail = fail_step_id

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        if sid == self._fail:
            raise RuntimeError(f"simulated dispatch failure at {sid}")
        return {"step": sid}


class _EvalOptDispatcher:
    """generate (`step_id=="generate"`) → `{"draft": N}`; evaluate
    (`step_id=="evaluate"`) → accepts on the `accept_on`-th evaluation, so a
    lower `accept_on` forces ≥2 generate dispatches (the repeated-step case)."""

    def __init__(self, *, accept_on: int) -> None:
        self._accept_on = accept_on
        self.generate_calls = 0
        self.evaluate_calls = 0

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        if sid == "generate":
            self.generate_calls += 1
            return {"draft": self.generate_calls}
        self.evaluate_calls += 1
        return {"accepted": self.evaluate_calls >= self._accept_on, "feedback": "f"}


class _SubAgentDispatcher:
    """Recurses on SUB_AGENT_DISPATCH (re-enters `execute_workflow` with the
    child manifest + steps the payload carries — mirrors the hierarchical
    test's recursion primitive) and echoes leaf DECLARATIVE_STEPs."""

    def __init__(self, *, ctx: DriverContext) -> None:
        self.ctx = ctx
        self.registry: StepDispatcherRegistry | None = None

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        if step.step_kind is StepKind.SUB_AGENT_DISPATCH:
            assert self.registry is not None
            child = execute_workflow(
                cast(WorkflowManifestEntry, step.step_payload["child_manifest"]),
                cast(list[WorkflowStep], step.step_payload["child_steps"]),
                run_id=f"child-{sid}",
                ctx=self.ctx,
                default_model_binding=_DEFAULT_BINDING,
                step_dispatchers=self.registry,
            )
            if child.status is RunStatus.FAILED:
                raise RuntimeError(f"child failed: {child.fail_class}")
            return {"step": sid, "child": dict(child.final_state or {})}
        return {"step": sid}


class _Registry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind in (StepKind.DECLARATIVE_STEP, StepKind.SUB_AGENT_DISPATCH):
            return self._dispatcher
        raise StepKindDispatcherNotBoundError(step_kind)


def _run(
    *,
    manifest: WorkflowManifestEntry,
    steps: list[WorkflowStep],
    ledger: _RealLedger,
    dispatcher: StepDispatcher,
    emitter: _Emitter | None = None,
) -> Any:
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter or _Emitter()))
    return execute_workflow(
        manifest,
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(StepDispatcherRegistry, _Registry(dispatcher)),
    )


def _override_entries(handle: JsonlLedgerHandle) -> list[Any]:
    """Persisted state-ledger entries with the override action_id — the surface a
    provenance consumer reads."""
    return [e for e in read_ledger(handle) if str(e.action_id) == _OVERRIDE_ACTION_ID]


def _handle(tmp_path: Path) -> JsonlLedgerHandle:
    return JsonlLedgerHandle(canonical_path=tmp_path / "state.jsonl", exists=False, entry_count=0)


# ---------------------------------------------------------------------------
# PARALLELIZATION (U-CP-86)
# ---------------------------------------------------------------------------


def test_parallelization_emits_override_entry_for_overridden_branch(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    ledger = _RealLedger(handle=handle)
    emitter = _Emitter()
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.PARALLELIZATION,
            overrides={"branch-1": _override("branch-1")},
        ),
        steps=[_declarative(f"branch-{i}", index=i) for i in range(3)],
        ledger=ledger,
        dispatcher=_EchoDispatcher(),
        emitter=emitter,
    )
    assert result.status is RunStatus.SUCCESS
    entries = _override_entries(handle)
    # Exactly ONE override entry — for the single overridden branch (the other two
    # branches resolve no override → no entry).
    assert len(entries) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID
    # The branch_metadata=None override entry must NOT inflate the per-step
    # STEP_BOUNDARY count: 3 branches ran a step → exactly 3 boundaries (the
    # override entry is not a step entry).
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 3


def test_parallelization_override_on_failed_branch_no_spurious_step_boundary(
    tmp_path: Path,
) -> None:
    """Failure-path drain with an override present: a single overridden branch
    whose dispatch RAISES. Under B-PARALLELIZATION-CASCADE the manifest's persona
    (SOLO→proceed) degrades the single-branch failure to PARTIAL (the failed
    branch contributes no survivor). The branch ALSO records a real step entry
    (obl. 3 — a ran-and-errored dispatch is no longer a silent gap), so it emits
    exactly ONE STEP_BOUNDARY — the override entry (`branch_metadata=None`) must
    NOT be additionally mis-counted as a step (that would be 2). The override
    entry persists (emitted before dispatch — linear-consistent), chain valid."""
    handle = _handle(tmp_path)
    ledger = _RealLedger(handle=handle)
    emitter = _Emitter()
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.PARALLELIZATION,
            overrides={"branch-0": _override("branch-0")},
        ),
        steps=[_declarative("branch-0", index=0)],
        ledger=ledger,
        dispatcher=_FailingEchoDispatcher(fail_step_id="branch-0"),
        emitter=emitter,
    )
    assert result.status is RunStatus.PARTIAL
    # The failed branch records its real step entry (obl. 3) → exactly ONE
    # STEP_BOUNDARY; the override entry is NOT mis-counted (else this would be 2).
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 1
    # Provenance preserved: the override entry persisted (resolution-time fact,
    # emitted before the dispatch that raised); chain re-verifies.
    assert len(_override_entries(handle)) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


def test_parallelization_no_override_emits_no_override_entry(tmp_path: Path) -> None:
    """No-regression: a fan-out with no per-step override emits ZERO override
    entries (byte-identical to pre-arc behavior)."""
    handle = _handle(tmp_path)
    result = _run(
        manifest=_manifest(topology_pattern=TopologyPattern.PARALLELIZATION, overrides={}),
        steps=[_declarative(f"branch-{i}", index=i) for i in range(3)],
        ledger=_RealLedger(handle=handle),
        dispatcher=_EchoDispatcher(),
    )
    assert result.status is RunStatus.SUCCESS
    assert _override_entries(handle) == []


def test_parallelization_emits_one_override_entry_per_overridden_branch(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.PARALLELIZATION,
            overrides={"branch-0": _override("branch-0"), "branch-2": _override("branch-2")},
        ),
        steps=[_declarative(f"branch-{i}", index=i) for i in range(3)],
        ledger=_RealLedger(handle=handle),
        dispatcher=_EchoDispatcher(),
    )
    assert result.status is RunStatus.SUCCESS
    # Two distinct overridden branches → two distinct override entries (distinct
    # step_ids → distinct §16.5.4 keys → no dedup).
    assert len(_override_entries(handle)) == 2


# ---------------------------------------------------------------------------
# EVALUATOR_OPTIMIZER (U-CP-87) — the repeated-step dedup case (advisor-flagged)
# ---------------------------------------------------------------------------


def test_evaluator_optimizer_repeated_step_override_dedups_to_one(tmp_path: Path) -> None:
    """`generate` is re-dispatched across iterations (accept_on=2 → 2 generate
    dispatches), each buffering the same `(step, outcome)` override key. The IS
    writer idempotently dedups to EXACTLY ONE persisted override entry — the
    deliberate §16.5.4 semantic (an override is a static binding property, not a
    per-execution event), NOT a per-iteration over-record."""
    handle = _handle(tmp_path)
    dispatcher = _EvalOptDispatcher(accept_on=2)
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.EVALUATOR_OPTIMIZER,
            overrides={"generate": _override("generate")},
        ),
        steps=[_declarative("generate"), _declarative("evaluate")],
        ledger=_RealLedger(handle=handle),
        dispatcher=dispatcher,
    )
    assert result.status is RunStatus.SUCCESS
    assert dispatcher.generate_calls >= 2  # the repeated-step precondition
    assert len(_override_entries(handle)) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# ORCHESTRATOR_WORKERS (U-CP-88)
# ---------------------------------------------------------------------------


def test_orchestrator_workers_emits_override_entries_for_orchestrator_and_worker(
    tmp_path: Path,
) -> None:
    """Both the orchestrator step's own override AND a worker's override emit a
    dedicated entry (orchestrator: sequential driver-thread site; worker: the
    fan-out branch-plan site)."""
    handle = _handle(tmp_path)
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.ORCHESTRATOR_WORKERS,
            overrides={
                "orchestrator": _override("orchestrator"),
                "worker-0": _override("worker-0"),
            },
        ),
        steps=[_declarative("orchestrator"), _declarative("worker-0"), _declarative("worker-1")],
        ledger=_RealLedger(handle=handle),
        dispatcher=_EchoDispatcher(),
    )
    assert result.status is RunStatus.SUCCESS
    entries = _override_entries(handle)
    assert len(entries) == 2
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


def test_orchestrator_workers_override_persists_on_orchestrator_failure(tmp_path: Path) -> None:
    """The orchestrator step's override is recorded BEFORE dispatch (uniform with
    the linear path), so an orchestrator dispatch FAILURE still persists the
    override entry (drained on the orchestrator-failure path), with no spurious
    STEP_BOUNDARY for the override-only writer."""
    handle = _handle(tmp_path)
    ledger = _RealLedger(handle=handle)
    emitter = _Emitter()
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.ORCHESTRATOR_WORKERS,
            overrides={"orchestrator": _override("orchestrator")},
        ),
        steps=[_declarative("orchestrator"), _declarative("worker-0")],
        ledger=ledger,
        dispatcher=_FailingEchoDispatcher(fail_step_id="orchestrator"),
        emitter=emitter,
    )
    assert result.status is RunStatus.FAILED
    assert len(_override_entries(handle)) == 1
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 0
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


def _branch_terminals(handle: JsonlLedgerHandle) -> list[Any]:
    """Persisted branch entries carrying a terminal disposition (the §25.13
    terminal entries — `branch_metadata.terminal_status` set)."""
    return [
        e
        for e in read_ledger(handle)
        if e.branch_metadata is not None and e.branch_metadata.terminal_status is not None
    ]


def test_orchestrator_workers_cascade_cancel_overridden_worker_still_gets_cancelled_terminal(
    tmp_path: Path,
) -> None:
    """Regression for the pre-fan-out override buffering × the CASCADE_CANCEL
    not-yet-dispatched scan (out-of-family Codex finding). An OVERRIDDEN worker
    cancelled before dispatch buffers ONLY its override entry — the scan must
    still record its `cancelled` terminal (the resume-idempotency contract,
    §25.15.2 obl. 4). The scan keys on the ABSENCE of a step/terminal disposition
    (`_writer_has_branch_disposition`), NOT `entry_count == 0` (the override makes
    `entry_count == 1`). MTC tier → CASCADE_CANCEL. The worker uses the poison
    TOOL_STEP kind (registry lookup raises synchronously → fails before dispatch,
    deterministically not-yet-dispatched)."""
    handle = _handle(tmp_path)
    ledger = _RealLedger(handle=handle)
    poison_worker = WorkflowStep(
        step_id=StepID("worker-0"), step_kind=StepKind.TOOL_STEP, step_payload={}
    )
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.ORCHESTRATOR_WORKERS,
            overrides={"worker-0": _override("worker-0")},
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        ),
        steps=[_declarative("orchestrator"), poison_worker],
        ledger=ledger,
        dispatcher=_EchoDispatcher(),
    )
    assert result.status is RunStatus.FAILED
    # The overridden, never-dispatched worker recorded a `cancelled` terminal
    # (resume-ineligible) — NOT skipped because its override made the buffer
    # non-empty.
    cancelled = [
        t for t in _branch_terminals(handle) if t.branch_metadata.terminal_status == "cancelled"
    ]
    assert len(cancelled) == 1
    # Its override entry persists alongside (recorded before the failed dispatch).
    assert len(_override_entries(handle)) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# HIERARCHICAL_DELEGATION (U-CP-89) — inherits the orchestrator-workers loop
# ---------------------------------------------------------------------------


def test_hierarchical_delegation_emits_override_entry_for_worker(tmp_path: Path) -> None:
    """HIERARCHICAL_DELEGATION re-enters `_execute_orchestrator_workers`, so a
    top-level worker override emits its entry through the same wired worker-loop
    site."""
    handle = _handle(tmp_path)
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.HIERARCHICAL_DELEGATION,
            overrides={"h-worker-0": _override("h-worker-0")},
        ),
        steps=[_declarative("h-orchestrator"), _declarative("h-worker-0")],
        ledger=_RealLedger(handle=handle),
        dispatcher=_EchoDispatcher(),
    )
    assert result.status is RunStatus.SUCCESS
    assert len(_override_entries(handle)) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


def test_hierarchical_delegation_emits_nested_child_override_entry(tmp_path: Path) -> None:
    """The recursion genuinely inherits the mechanism: a per-step override on a
    CHILD workflow's worker (reached via a SUB_AGENT_DISPATCH worker) emits its
    override entry from the recursive `execute_workflow` call."""
    handle = _handle(tmp_path)
    ledger = _RealLedger(handle=handle)
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=_Emitter()))
    dispatcher = _SubAgentDispatcher(ctx=ctx)
    registry = cast(StepDispatcherRegistry, _Registry(cast(StepDispatcher, dispatcher)))
    dispatcher.registry = registry

    child_manifest = _manifest(
        topology_pattern=TopologyPattern.ORCHESTRATOR_WORKERS,
        overrides={"c-worker-0": _override("c-worker-0")},
        workflow_id="wf-child",
    )
    child_steps = [_declarative("c-orchestrator"), _declarative("c-worker-0")]
    parent_steps = [
        _declarative("p-orchestrator"),
        WorkflowStep(
            step_id=StepID("p-sub"),
            step_kind=StepKind.SUB_AGENT_DISPATCH,
            step_payload={"child_manifest": child_manifest, "child_steps": child_steps},
        ),
    ]
    result = execute_workflow(
        _manifest(
            topology_pattern=TopologyPattern.HIERARCHICAL_DELEGATION,
            overrides={},
            workflow_id="wf-parent",
        ),
        parent_steps,
        run_id="run-parent",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=registry,
    )
    assert result.status is RunStatus.SUCCESS
    # The nested child worker's override entry is persisted (the recursion routes
    # through the same wired worker-loop site).
    entries = _override_entries(handle)
    assert len(entries) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# DECENTRALIZED_HANDOFF (U-CP-90)
# ---------------------------------------------------------------------------


def test_decentralized_handoff_emits_override_entry_for_stage(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.DECENTRALIZED_HANDOFF,
            overrides={"stage-1": _override("stage-1")},
        ),
        steps=[_declarative("stage-0"), _declarative("stage-1"), _declarative("stage-2")],
        ledger=_RealLedger(handle=handle),
        dispatcher=_EchoDispatcher(),
    )
    assert result.status is RunStatus.SUCCESS
    assert len(_override_entries(handle)) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


def test_decentralized_handoff_override_persists_on_stage_failure(tmp_path: Path) -> None:
    """A stage's override is recorded BEFORE dispatch (uniform with the linear
    path), so a stage dispatch FAILURE still persists the override (drained via
    `_finish` over stage_writers, which the failed stage's writer now joins before
    its try). SOLO tier → PROCEED → PARTIAL over the completed prefix."""
    handle = _handle(tmp_path)
    result = _run(
        manifest=_manifest(
            topology_pattern=TopologyPattern.DECENTRALIZED_HANDOFF,
            overrides={"stage-1": _override("stage-1")},
            persona_tier=PersonaTier.SOLO_DEVELOPER,
        ),
        steps=[_declarative("stage-0"), _declarative("stage-1"), _declarative("stage-2")],
        ledger=_RealLedger(handle=handle),
        dispatcher=_FailingEchoDispatcher(fail_step_id="stage-1"),
    )
    assert result.status is RunStatus.PARTIAL
    # The overridden stage failed, but its override entry persists (recorded
    # before the dispatch that raised); stage-0 ran (no override) → no entry.
    assert len(_override_entries(handle)) == 1
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID
