"""B1-impl-7 — U-CP-87 `EVALUATOR_OPTIMIZER` driver strategy (CP plan v2.32 §2.2).

The SECOND non-linear topology strategy: a sequential generate→evaluate→
(accept | regenerate) loop bounded by a max-iteration cap; terminal on the first
evaluator-accept or at the cap. Per C-CP-25 §25.11 EVALUATOR_OPTIMIZER row +
"strategies differ only in *control flow over steps*", `steps[0]` is the generate
step and `steps[1]` is the evaluate step; the two are distinguished by their
per-step prompt (R-PM-1 §29) — non-hollow at B1 without B4 because generate ≠
evaluate by `step_id` (hence by selected prompt).

Acceptance-criterion coverage (Implementation_Plan_Control_Plane_v2_32.md
U-CP-87):
  functional — generate→evaluate loop accepts on evaluator-accept; terminates at
    the cap otherwise; entries persist via the buffered path:
      → test_evaluator_optimizer_accepts_on_evaluator_accept
      → test_evaluator_optimizer_iterates_then_accepts
      → test_evaluator_optimizer_terminates_at_iteration_cap
  evaluator/optimizer distinguished by per-step (R-PM-1 §29) — non-hollow:
      → test_evaluator_optimizer_distinct_step_dispatch_non_hollow
  §25.18 fan-out cadence adapted for a SEQUENTIAL strategy:
    persisted-branch-causality → "no fan-out branch_metadata required" (assert
      branch_metadata is None); cascade-cancel-idempotency → no cascade (U-CP-85
      non-dep), so the load-bearing idempotency assertion is "unique keys, no
      same-step-index collapse across iterations":
      → test_evaluator_optimizer_buffered_entries_carry_no_branch_metadata
      → test_evaluator_optimizer_unique_idempotency_keys_no_collapse
      → test_evaluator_optimizer_persisted_in_execution_order
  no silent loss + lifecycle + scope:
      → test_evaluator_optimizer_step_failure_returns_failed_and_persists_prior
      → test_evaluator_optimizer_emits_workflow_start_and_step_boundaries
      → test_evaluator_optimizer_empty_steps_returns_success
      → test_evaluator_optimizer_malformed_step_count_returns_failed
  live e2e (real IS writer; §6.3 hash chain re-verifies post-drain):
      → test_evaluator_optimizer_live_e2e_real_ledger_chain_valid

Non-hollow honesty: R-PM-1 §29 prompt SELECTION is composed at runtime stage-0,
not in the CP driver — so this CP-unit suite proves distinct-step dispatch (a
different `step_id` resolves a different binding), NOT live prompt selection. The
provider-free real-IS-writer e2e exercises the full persist+drain+chain path with
a structured fake dispatcher (a live LLM returns text, not the structured
`accepted` signal — so the structured-output proof stays at unit level here).

Authority: `Spec_Control_Plane_v1_32.md` §25.10/§25.11/§25.12/§25.17/§25.18 +
`Implementation_Plan_Control_Plane_v2_32.md` §2.2 (U-CP-87).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
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
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_write import (
    WriteResult,
    append_ledger_entry,
    read_ledger,
)

# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------

_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-evaluator-optimizer")
_GENERATE = "generate"
_EVALUATE = "evaluate"


def _manifest(
    *,
    workflow_id: str = "wf-eo",
    persona_tier: PersonaTier = PersonaTier.TEAM_BINDING,
) -> WorkflowManifestEntry:
    """An EVALUATOR_OPTIMIZER manifest (engine in scope; admissibility is enforced
    at workflow-binding per §25.10 Invariant 2, NOT re-checked by the driver).

    `persona_tier` selects the §11.4 D4 `cascade_policy` the EO step-failure path reads
    (SOLO→proceed / TEAM→pause / MTC→cascade-cancel) — default TEAM_BINDING. Pre-arc the
    driver ignored cascade_policy; B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER materializes the
    `pause` branch (TEAM → resumable PAUSED), so a generic-FAILED test selects a non-pause
    tier."""
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.EVALUATOR_OPTIMIZER,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _step(step_id: str) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(step_id),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"role": step_id},
    )


def _loop_steps() -> list[WorkflowStep]:
    """The canonical 2-step EVALUATOR_OPTIMIZER body: generate then evaluate."""
    return [_step(_GENERATE), _step(_EVALUATE)]


class _RecordingLedger:
    """In-memory `LedgerWriterLike` that records drained appends in order."""

    actor: Actor

    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> Any:
        self.appends.append((payload, write_key))
        return "appended"

    @property
    def is_genesis(self) -> bool:
        return len(self.appends) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appends)


class _RealLedgerWriter:
    """A `LedgerWriterLike` drain sink backed by the REAL IS writer (so dedup,
    timestamp-monotonicity, hash-chain construction, and JSONL persistence are
    all exercised — `verify_chain` then re-verifies the §6.3 chain)."""

    def __init__(self, *, handle: JsonlLedgerHandle, actor: Actor) -> None:
        self._handle = handle
        self.actor = actor
        self.results: list[WriteResult] = []

    def append(self, payload: Any, write_key: Any) -> None:
        self.results.append(append_ledger_entry(self._handle, payload, write_key))

    @property
    def is_genesis(self) -> bool:
        return len(self.results) == 0

    @property
    def entry_count(self) -> int:
        return len(self.results)


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
        self.emits.append(event_class)


class _Ctx:
    """Minimal fake `DriverContext` for the EVALUATOR_OPTIMIZER e2e through
    `execute_workflow`. The strategy reads `procedural_tier_snapshot_resolver`
    via `getattr(..., None)` — absent here → the R-003 sidecar stays `None`."""

    def __init__(
        self, *, ledger: Any, emitter: _Emitter, inter_step_output_channel: Any = None
    ) -> None:
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
        # B-INTERSTEP (runtime spec §14.21 C-RT-34) — None (default) → the driver's
        # `getattr(..., None)` read records nothing; a bound channel exercises the
        # producer half (the driver records each step's output during the EO run).
        self.inter_step_output_channel = inter_step_output_channel


class _MultiKindRegistry:
    """Binds a single dispatcher for `DECLARATIVE_STEP` (both the generate and
    evaluate steps share the step kind; they differ by `step_id`)."""

    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is not StepKind.DECLARATIVE_STEP:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _MultiKindRegistry(dispatcher))


class _EvalOptDispatcher:
    """Distinguishes the generate vs evaluate step by `step_id` — the distinction
    the driver sees as two distinct bindings (the non-hollow B1 property; the
    live R-PM-1 §29 prompt selection is a runtime-stage-0 concern).

    - generate (`step_id == _GENERATE`) → `{"draft": <1-based generate-call>}`.
    - evaluate (`step_id == _EVALUATE`) → `{"accepted": <bool>, "feedback": ...}`;
      accepts on the `accept_on_iteration`-th evaluation (1-based) and after; a
      `None` cap never accepts (forces the loop to the iteration cap).
    """

    def __init__(self, *, accept_on_iteration: int | None) -> None:
        self._accept_on = accept_on_iteration
        self.generate_calls = 0
        self.evaluate_calls = 0
        self.seen_step_ids: list[str] = []
        # The `step_context.step_index` the dispatcher observes per call (the
        # declared ordinal downstream policy/audit selection keys on — Codex [P2]).
        self.seen_step_indices: list[int] = []

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        self.seen_step_ids.append(sid)
        if step_context is not None:
            self.seen_step_indices.append(step_context.step_index)
        if sid == _GENERATE:
            self.generate_calls += 1
            return {"draft": self.generate_calls}
        if sid == _EVALUATE:
            self.evaluate_calls += 1
            accepted = self._accept_on is not None and self.evaluate_calls >= self._accept_on
            return {"accepted": accepted, "feedback": f"rev-{self.evaluate_calls}"}
        raise AssertionError(f"unexpected step_id {sid!r}")


class _FailingDispatcher:
    """Echoes generate/evaluate but RAISES on the `fail_on_step`-th total
    dispatch call (1-based) to exercise the FAILED-with-prior-persisted path."""

    def __init__(self, *, fail_on_step: int) -> None:
        self._fail_on = fail_on_step
        self.calls = 0

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError(f"simulated dispatch failure at call {self.calls}")
        sid = str(step.step_id)
        if sid == _EVALUATE:
            return {"accepted": False}
        return {"draft": self.calls}


def _run(
    *,
    steps: list[WorkflowStep],
    dispatcher: StepDispatcher,
    ledger: Any,
    emitter: _Emitter | None = None,
    workflow_id: str = "wf-eo",
    inter_step_output_channel: Any = None,
    persona_tier: PersonaTier = PersonaTier.TEAM_BINDING,
) -> Any:
    emitter = emitter if emitter is not None else _Emitter()
    ctx = cast(
        DriverContext,
        _Ctx(ledger=ledger, emitter=emitter, inter_step_output_channel=inter_step_output_channel),
    )
    return execute_workflow(
        _manifest(workflow_id=workflow_id, persona_tier=persona_tier),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
    )


def _idempotency_keys(ledger: _RecordingLedger) -> list[str]:
    return [str(payload.idempotency_key) for payload, _wk in ledger.appends]


def _action_ids(ledger: _RecordingLedger) -> list[str]:
    return [str(payload.action_id) for payload, _wk in ledger.appends]


# ---------------------------------------------------------------------------
# Functional — accept / iterate / cap
# ---------------------------------------------------------------------------


def test_evaluator_optimizer_accepts_on_evaluator_accept() -> None:
    """The evaluator accepts on iteration 1 → the loop STOPS early (1 iteration,
    not the cap): SUCCESS, accepted, exactly 2 persisted entries (gen+eval)."""
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=1)
    result = _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.fail_class is None
    assert result.final_state is not None
    assert result.final_state["accepted"] is True
    assert result.final_state["iterations"] == 1
    assert result.final_state["output"] == {"draft": 1}
    assert result.final_state["evaluation"]["accepted"] is True
    # Loop stopped early (1 iteration × 2 steps), did NOT run to the cap.
    assert len(ledger.appends) == 2
    assert dispatcher.generate_calls == 1
    assert dispatcher.evaluate_calls == 1


def test_evaluator_optimizer_iterates_then_accepts() -> None:
    """The evaluator rejects twice then accepts on iteration 3 → 3 iterations;
    the accepted output is the 3rd generate draft; 3 × 2 = 6 persisted entries."""
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=3)
    result = _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["accepted"] is True
    assert result.final_state["iterations"] == 3
    assert result.final_state["output"] == {"draft": 3}
    assert len(ledger.appends) == 6


def test_evaluator_optimizer_terminates_at_iteration_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The evaluator never accepts → the loop terminates at the cap (a bounded,
    best-effort SUCCESS with `accepted=False`; §25.17 lists no cap-failure)."""
    from harness_cp import workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS", 2)
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=None)
    result = _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["accepted"] is False
    assert result.final_state["iterations"] == 2
    # Ran exactly the cap (2 iterations × 2 steps), no more.
    assert len(ledger.appends) == 4
    assert dispatcher.generate_calls == 2
    assert dispatcher.evaluate_calls == 2


# ---------------------------------------------------------------------------
# Non-hollow — distinct-step dispatch
# ---------------------------------------------------------------------------


def test_evaluator_optimizer_distinct_step_dispatch_non_hollow() -> None:
    """The generate and evaluate steps dispatch as DISTINCT steps (alternating
    `step_id`), producing distinct outputs — the B1 non-hollow property (roles by
    per-step prompt; here proven as distinct-step dispatch, the CP-driver-visible
    half — live prompt selection is runtime stage-0)."""
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=2)
    _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    # 2 iterations: generate, evaluate, generate, evaluate — distinct steps, the
    # generate output ({"draft":...}) is never confused with the evaluate output.
    assert dispatcher.seen_step_ids == [_GENERATE, _EVALUATE, _GENERATE, _EVALUATE]


def test_evaluator_optimizer_step_context_carries_declared_ordinal_not_ledger_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex [P2] regression: `StepExecutionContext.step_index` MUST be the
    DECLARED step ordinal (0=generate, 1=evaluate) on EVERY iteration — what
    downstream dispatchers key per-step policy/override selection + audit context
    on — NOT the monotonic ledger row index (which would drift 2,3,… and break
    policy matching after iteration 1). The unique ledger row index stays on the
    ledger action_id / idempotency key only."""
    from harness_cp import workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS", 3)
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=None)  # 3 iterations
    _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    # 3 iterations × (generate=0, evaluate=1) — the declared ordinal repeats, it
    # does NOT become 0,1,2,3,4,5 (the ledger row index, which lives on action_ids).
    assert dispatcher.seen_step_indices == [0, 1, 0, 1, 0, 1]
    # The ledger action_ids DO use the unique monotonic row index (0..5).
    assert _action_ids(ledger) == [f"workflow:wf-eo:step:{i}" for i in range(6)]


# ---------------------------------------------------------------------------
# §25.18 fan-out cadence adapted for a SEQUENTIAL strategy
# ---------------------------------------------------------------------------


def test_evaluator_optimizer_buffered_entries_carry_no_branch_metadata() -> None:
    """Persisted-branch-causality (§25.18 cadence) for a SEQUENTIAL strategy =
    "no fan-out branch_metadata required" (the AC): every drained entry carries
    the carrier default `branch_metadata is None`, NOT the U-CP-84 fan-out
    causality sidecar."""
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=2)
    _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    assert len(ledger.appends) == 4
    for payload, _wk in ledger.appends:
        assert payload.branch_metadata is None


def test_evaluator_optimizer_unique_idempotency_keys_no_collapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing idempotency assertion (the cascade-cancel-idempotency
    cadence adapted — EO has no cascade): re-dispatching the SAME 2 declared steps
    across iterations must NOT collapse on the IS writer's idempotency_key-only
    dedup (C-IS-07 §7.5). The monotonic `entry_index` makes every entry's key
    distinct — a regression that reused the declared step_index (0/1) would yield
    only 2 distinct keys for a 3-iteration run."""
    from harness_cp import workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS", 3)
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=None)
    _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    keys = _idempotency_keys(ledger)
    assert len(keys) == 6
    assert len(set(keys)) == 6  # all distinct → no same-step-index collapse


def test_evaluator_optimizer_persisted_in_execution_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries drain (buffered path) in execution order, keyed by the monotonic
    `entry_index` (0..2N-1) — a deterministic-append regression for the
    sequential strategy."""
    from harness_cp import workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS", 3)
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=None)
    _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    assert _action_ids(ledger) == [f"workflow:wf-eo:step:{i}" for i in range(6)]


# ---------------------------------------------------------------------------
# Failure + lifecycle + scope
# ---------------------------------------------------------------------------


def test_evaluator_optimizer_step_failure_returns_failed_and_persists_prior() -> None:
    """A dispatch raising under cascade_policy=proceed (SOLO) → FAILED with the generic
    `evaluator-optimizer-step-failure` class. B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER
    materializes ONLY the `pause` branch; `proceed` / `cascade-cancel` retain EO's
    existing terminal-FAILED disposition (the surgical additive scope — owned in the
    v1.48 change-note). The fully-completed prior steps' buffered entries STILL drain (no
    silent loss). Failing on call 3 (iteration 2's generate) → iteration 1 fully persisted
    (2 entries), iteration 2's failing generate persists nothing."""
    ledger = _RecordingLedger()
    dispatcher = _FailingDispatcher(fail_on_step=3)
    result = _run(
        steps=_loop_steps(),
        dispatcher=cast(StepDispatcher, dispatcher),
        ledger=ledger,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "evaluator-optimizer-step-failure" in result.fail_class
    # Iteration 1 (gen+eval) persisted; the failing iteration-2 generate did not.
    assert len(ledger.appends) == 2
    assert _action_ids(ledger) == ["workflow:wf-eo:step:0", "workflow:wf-eo:step:1"]


def test_evaluator_optimizer_step_failure_cascade_cancel_tier_returns_failed() -> None:
    """A dispatch raising under cascade_policy=cascade-cancel (MTC) → FAILED with the
    generic `evaluator-optimizer-step-failure` class (the preserved disposition — only
    `pause` is materialized for EO). Contrasting-baseline to the TEAM pause path."""
    ledger = _RecordingLedger()
    dispatcher = _FailingDispatcher(fail_on_step=3)
    result = _run(
        steps=_loop_steps(),
        dispatcher=cast(StepDispatcher, dispatcher),
        ledger=ledger,
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "evaluator-optimizer-step-failure" in result.fail_class
    assert len(ledger.appends) == 2


def test_evaluator_optimizer_emits_workflow_start_and_step_boundaries() -> None:
    """WORKFLOW_START emitted once; one STEP_BOUNDARY per dispatched step
    (2 iterations × 2 steps = 4) — single-threaded on the driver thread."""
    ledger = _RecordingLedger()
    emitter = _Emitter()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=2)
    _run(
        steps=_loop_steps(),
        dispatcher=cast(StepDispatcher, dispatcher),
        ledger=ledger,
        emitter=emitter,
    )
    assert emitter.emits.count(WorkflowEventClass.WORKFLOW_START) == 1
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 4


def test_evaluator_optimizer_empty_steps_returns_success() -> None:
    """An empty step sequence → trivially SUCCESS (mirrors the linear empty-loop +
    the PARALLELIZATION empty-steps SUCCESS); no appends."""
    ledger = _RecordingLedger()
    result = _run(
        steps=[],
        dispatcher=cast(StepDispatcher, _EvalOptDispatcher(accept_on_iteration=1)),
        ledger=ledger,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state == {
        "accepted": False,
        "iterations": 0,
        "output": {},
        "evaluation": {},
    }
    assert ledger.appends == []


def test_evaluator_optimizer_malformed_step_count_returns_failed() -> None:
    """EVALUATOR_OPTIMIZER is exactly generate→evaluate (2 steps). A non-empty
    count != 2 is malformed for this pattern → FAILED before any dispatch (no
    WORKFLOW_START, no appends) — no silent reshape."""
    ledger = _RecordingLedger()
    emitter = _Emitter()
    result = _run(
        steps=[_step(_GENERATE)],  # only 1 step — no evaluator
        dispatcher=cast(StepDispatcher, _EvalOptDispatcher(accept_on_iteration=1)),
        ledger=ledger,
        emitter=emitter,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "evaluator-optimizer-malformed" in result.fail_class
    assert ledger.appends == []
    assert WorkflowEventClass.WORKFLOW_START not in emitter.emits


# ---------------------------------------------------------------------------
# Live e2e — real IS writer; §6.3 hash chain re-verifies post-drain
# ---------------------------------------------------------------------------


def test_evaluator_optimizer_live_e2e_real_ledger_chain_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real loop through `execute_workflow` into the REAL IS writer: the
    persisted entries re-verify as a VALID §6.3 hash chain, all distinct (the
    monotonic-entry_index idempotency key never collapses). Provider-free
    (declarative steps, structured fake evaluator) — no external credential."""
    from harness_cp import workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS", 3)
    handle = JsonlLedgerHandle(canonical_path=tmp_path / "state.jsonl", exists=False, entry_count=0)
    real = _RealLedgerWriter(handle=handle, actor=_ACTOR)
    dispatcher = _EvalOptDispatcher(accept_on_iteration=None)  # run to the cap (3 × 2 = 6)
    result = _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=real)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["accepted"] is False
    # Every drained append landed (no silent IDEMPOTENT_NOOP drop): 3 × 2 = 6.
    assert len(real.results) == 6
    entries = read_ledger(handle)
    assert len(entries) == 6
    # §6.3 chain re-verifies VALID after the buffered drain.
    assert verify_chain(entries).status is VerificationStatus.VALID
    # Sequential strategy: every entry carries no fan-out branch_metadata.
    assert all(e.branch_metadata is None for e in entries)


# ---------------------------------------------------------------------------
# B-INTERSTEP (runtime spec §14.21 C-RT-34) — inter-step DATA flow (producer).
#
# The CP driver records each completed step's output to a run-scoped channel so a
# subsequent dispatch can read it. harness-cp must NOT depend on harness-runtime,
# so these CP-side tests use an interface-faithful fake channel (the driver only
# `getattr`s the bound object and calls `.record(...)`). The real
# `InterStepOutputChannel` semantics are unit-tested in harness-runtime; the
# GENUINE LLM-dispatcher consumer (prior output → the actual provider call) is
# proven in `test_lifecycle_llm_dispatch.py::test_inter_step_channel_injects_*`.
# ---------------------------------------------------------------------------


class _FakeInterStepChannel:
    """Interface-faithful stand-in for the runtime `InterStepOutputChannel`."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, step_id: str, output: Any) -> None:
        self.records.append((step_id, dict(output)))

    def most_recent_output(self) -> Any:
        return self.records[-1][1] if self.records else None


class _UpstreamObservingDispatcher:
    """Holds the SAME channel the driver records into (mirroring how the real
    `RuntimeLLMDispatcher` is threaded the channel at stage 5). On each dispatch it
    captures `most_recent_output()` — proving the driver has ALREADY recorded the
    PRIOR step's output by the time THIS step dispatches."""

    def __init__(self, *, channel: _FakeInterStepChannel) -> None:
        self._channel = channel
        self._gen = 0
        self._eval = 0
        self.upstream_seen_by_step: list[tuple[str, Any]] = []

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        sid = str(step.step_id)
        self.upstream_seen_by_step.append((sid, self._channel.most_recent_output()))
        if sid == _GENERATE:
            self._gen += 1
            return {"draft": self._gen}
        self._eval += 1
        return {"accepted": self._eval >= 2, "feedback": f"rev-{self._eval}"}


def test_evaluator_optimizer_records_outputs_to_inter_step_channel() -> None:
    """Producer half: a bound channel → the driver records each EO step's output in
    execution order (gen→eval per iteration), defensively copied."""
    ledger = _RecordingLedger()
    channel = _FakeInterStepChannel()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=2)
    result = _run(
        steps=_loop_steps(),
        dispatcher=cast(StepDispatcher, dispatcher),
        ledger=ledger,
        inter_step_output_channel=channel,
    )
    assert result.status is RunStatus.SUCCESS
    assert [sid for sid, _ in channel.records] == [_GENERATE, _EVALUATE, _GENERATE, _EVALUATE]
    assert channel.records[0][1] == {"draft": 1}
    assert channel.records[1][1] == {"accepted": False, "feedback": "rev-1"}
    assert channel.records[2][1] == {"draft": 2}
    assert channel.records[3][1] == {"accepted": True, "feedback": "rev-2"}


def test_evaluator_optimizer_dispatch_observes_prior_step_output() -> None:
    """Full chain: a dispatch SEES the prior step's output through the channel — the
    evaluate dispatch observes the generate draft; the regenerate (iteration-2
    generate) observes the iteration-1 evaluator feedback; iteration-2 evaluate
    observes the regenerated draft. This is the EVALUATOR_OPTIMIZER data flow the
    §25.11 comment said was control-flow-only before B-INTERSTEP."""
    ledger = _RecordingLedger()
    channel = _FakeInterStepChannel()
    dispatcher = _UpstreamObservingDispatcher(channel=channel)
    result = _run(
        steps=_loop_steps(),
        dispatcher=cast(StepDispatcher, dispatcher),
        ledger=ledger,
        inter_step_output_channel=channel,
    )
    assert result.status is RunStatus.SUCCESS
    seen = dispatcher.upstream_seen_by_step
    assert seen[0] == (_GENERATE, None)
    assert seen[1] == (_EVALUATE, {"draft": 1})
    assert seen[2] == (_GENERATE, {"accepted": False, "feedback": "rev-1"})
    assert seen[3] == (_EVALUATE, {"draft": 2})


def test_evaluator_optimizer_no_channel_records_nothing_byte_identical() -> None:
    """Opt-out (default): no channel bound → the driver records nothing and the EO
    result is unchanged (byte-identical to pre-v1.59)."""
    ledger = _RecordingLedger()
    dispatcher = _EvalOptDispatcher(accept_on_iteration=1)
    result = _run(steps=_loop_steps(), dispatcher=cast(StepDispatcher, dispatcher), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["output"] == {"draft": 1}
    assert len(ledger.appends) == 2
