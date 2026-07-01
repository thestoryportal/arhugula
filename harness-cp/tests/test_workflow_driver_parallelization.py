"""B1-impl-6 — U-CP-86 `PARALLELIZATION` driver strategy (CP plan v2.32 §2.2).

The first non-linear topology strategy: fan-out N branches over varied inputs
concurrently, barrier until all finish, fold structured outputs into one
deterministic result (synthesis/voting; lowest-branch-index tiebreak). Per
C-CP-25 §25.11 "strategies differ only in *control flow over steps*", each
declared `WorkflowStep` is one branch — the SAME `steps` sequence the linear
loop runs sequentially, run concurrently and aggregated.

Acceptance-criterion coverage (Implementation_Plan_Control_Plane_v2_32.md
U-CP-86):
  functional — fan-out → barrier → single deterministic aggregate (lowest-
    branch-index tiebreak; "first to finish wins" forbidden):
      → test_parallelization_fans_out_and_aggregates_single_result
      → test_parallelization_completion_order_independent
      → test_parallelization_aggregate_voting_majority
      → test_parallelization_aggregate_all_distinct_lowest_index_tiebreak
  branch entries persist in branch-index order with branch_metadata causality:
      → test_parallelization_branch_metadata_causality
      → test_parallelization_persisted_in_branch_index_order
  no silent loss (all N branch outputs preserved; completed entries persist on
    a sibling failure):
      → test_parallelization_preserves_all_branch_outputs
      → test_parallelization_branch_failure_returns_failed_and_persists_completed
  lifecycle + scope:
      → test_parallelization_emits_workflow_start_and_step_boundaries
      → test_parallelization_empty_steps_returns_success
  live e2e (real IS writer; §6.3 hash chain re-verifies post-drain):
      → test_parallelization_live_e2e_real_ledger_chain_valid

Authority: `Spec_Control_Plane_v1_32.md` §25.10/§25.11/§25.12/§25.13 +
`Spec_Harness_Runtime_v1.md` v1.48 §2.2 +
`Implementation_Plan_Control_Plane_v2_32.md` §2.2 (U-CP-86).
"""

from __future__ import annotations

import asyncio
import threading
import time
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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-parallelization")


def _manifest(
    *,
    workflow_id: str = "wf-par",
    persona_tier: PersonaTier = PersonaTier.TEAM_BINDING,
) -> WorkflowManifestEntry:
    """A PARALLELIZATION manifest (engine in scope; admissibility is enforced at
    workflow-binding per §25.10 Invariant 2, NOT re-checked by the driver).

    `persona_tier` selects the §11.4 D4 `cascade_policy` the strategy resolves on
    a branch failure (B-PARALLELIZATION-CASCADE): SOLO_DEVELOPER→proceed /
    TEAM_BINDING→pause / MULTI_TENANT_COMPLIANCE→cascade-cancel."""
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=persona_tier,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.PARALLELIZATION,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _branch_step(index: int) -> WorkflowStep:
    """One branch = one declarative step with a varied input (`index`)."""
    return WorkflowStep(
        step_id=StepID(f"branch-{index}"),
        step_kind=StepKind.DECLARATIVE_STEP,
        step_payload={"index": index},
    )


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
    """Minimal fake `DriverContext` for PARALLELIZATION e2e through
    `execute_workflow`. The strategy reads `procedural_tier_snapshot_resolver`
    via `getattr(..., None)` — absent here → the R-003 sidecar stays `None`."""

    def __init__(self, *, ledger: Any, emitter: _Emitter) -> None:
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


class _MultiKindRegistry:
    """Binds a single dispatcher for `DECLARATIVE_STEP` (all branches share the
    step kind under PARALLELIZATION)."""

    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is not StepKind.DECLARATIVE_STEP:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _MultiKindRegistry(dispatcher))


class _VariedDispatcher:
    """Echoes a per-branch output keyed by the step's varied `index` input.

    Optional `outputs` overrides the echoed output per index (to exercise the
    voting aggregator). Optional `fail_index` raises for one branch.
    """

    def __init__(
        self,
        *,
        outputs: dict[int, dict[str, Any]] | None = None,
        fail_index: int | None = None,
    ) -> None:
        self._outputs = outputs
        self._fail_index = fail_index

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        idx = int(step.step_payload["index"])
        if self._fail_index is not None and idx == self._fail_index:
            raise RuntimeError(f"simulated branch failure at index {idx}")
        if self._outputs is not None:
            return dict(self._outputs[idx])
        return {"branch": idx, "echoed": dict(step.step_payload)}


class _ReverseCompletionDispatcher:
    """Forces a DETERMINISTIC out-of-order (reverse-index) completion: branch i
    waits on branch (i+1)'s done-event before completing, so branches finish in
    strictly descending index order — N-1 first, 0 last. No `time.sleep`; the
    events are a hard sync point, so the test is not timing-flaky. The highest
    index waits on no one (it completes first and unblocks the chain)."""

    def __init__(self, *, n: int) -> None:
        self._events = {i: threading.Event() for i in range(n)}
        self._n = n

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        idx = int(step.step_payload["index"])
        higher = idx + 1
        if higher < self._n:
            # Wait until the next-higher branch has completed (reverse chain).
            assert self._events[higher].wait(timeout=10.0), f"branch {higher} never completed"
        self._events[idx].set()
        return {"branch": idx}


class _FailAfterSiblingsCompleteDispatcher:
    """The `fail_index` branch raises ONLY after every other branch's dispatch has
    completed (their events set) — making "a sibling fails, the completed branches
    still persist" DETERMINISTIC, not timing-flaky.

    Why this is race-free (it mirrors `_ReverseCompletionDispatcher`'s event-sync
    philosophy): the strategy buffers a branch's entries on the loop thread
    immediately AFTER its `await asyncio.to_thread(dispatch)` returns, with NO
    further await (so a resumed branch coroutine buffers atomically and cannot be
    cancelled mid-buffer). Because the failing branch waits for all siblings, its
    `to_thread` future resolves STRICTLY AFTER theirs, so the loop's FIFO ready-
    queue resumes + buffers the completed siblings BEFORE the failure propagates
    and `bounded_barrier`'s leak-free `finally` cancels pending tasks. No
    `time.sleep`; the events are a hard sync point. (The executor is sized to the
    fan-out, so the failing branch blocking its thread cannot starve the others.)"""

    def __init__(self, *, n: int, fail_index: int) -> None:
        self._fail_index = fail_index
        self._sibling_events = {i: threading.Event() for i in range(n) if i != fail_index}

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        idx = int(step.step_payload["index"])
        if idx == self._fail_index:
            for sibling, event in self._sibling_events.items():
                assert event.wait(timeout=10.0), f"sibling branch {sibling} never completed"
            raise RuntimeError(f"simulated branch failure at index {idx}")
        self._sibling_events[idx].set()
        return {"branch": idx}


def _run(
    *,
    steps: list[WorkflowStep],
    dispatcher: StepDispatcher,
    ledger: Any,
    emitter: _Emitter | None = None,
    workflow_id: str = "wf-par",
    persona_tier: PersonaTier = PersonaTier.TEAM_BINDING,
) -> Any:
    emitter = emitter if emitter is not None else _Emitter()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=emitter))
    return execute_workflow(
        _manifest(workflow_id=workflow_id, persona_tier=persona_tier),
        steps,
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(dispatcher),
    )


def _branch_indices_in_append_order(ledger: _RecordingLedger) -> list[int]:
    """The `branch_index` of each drained entry, in append order (each branch
    contributes a step entry then a terminal entry)."""
    return [payload.branch_metadata.branch_index for payload, _wk in ledger.appends]


# ---------------------------------------------------------------------------
# Functional — fan-out → barrier → single deterministic aggregate
# ---------------------------------------------------------------------------


def test_parallelization_fans_out_and_aggregates_single_result() -> None:
    """N branches over varied inputs barrier to one SUCCESS result; the
    aggregate is a single deterministic value + all branch outputs preserved."""
    ledger = _RecordingLedger()
    result = _run(
        steps=[_branch_step(i) for i in range(4)],
        dispatcher=_VariedDispatcher(),
        ledger=ledger,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.fail_class is None
    assert result.final_state is not None
    # All 4 branch outputs preserved (keyed by step_id) — no silent discard.
    assert set(result.final_state["branch_outputs"]) == {f"branch-{i}" for i in range(4)}
    # A single aggregate result (voting; all-distinct → lowest-index = branch 0).
    assert result.final_state["aggregate"] == {"branch": 0, "echoed": {"index": 0}}
    # Every branch dispatched (4 branches × {step, terminal} = 8 drained entries).
    assert len(ledger.appends) == 8


def test_parallelization_completion_order_independent() -> None:
    """Branches completing in REVERSE index order still persist in branch-index
    order and aggregate identically ("first to finish wins" is forbidden — the
    §25.12 determinism boundary)."""
    ledger = _RecordingLedger()
    result = _run(
        steps=[_branch_step(i) for i in range(4)],
        dispatcher=_ReverseCompletionDispatcher(n=4),
        ledger=ledger,
    )
    assert result.status is RunStatus.SUCCESS
    # Persisted order is branch-index order [0,0,1,1,2,2,3,3] — NOT completion
    # order [3,3,2,2,1,1,0,0] the reverse-chain dispatcher forced.
    assert _branch_indices_in_append_order(ledger) == [0, 0, 1, 1, 2, 2, 3, 3]
    # The aggregate is a pure function of the ordered set → branch 0 (lowest).
    assert result.final_state is not None
    assert result.final_state["aggregate"] == {"branch": 0}


def test_parallelization_aggregate_voting_majority() -> None:
    """The voting aggregator returns the MOST-voted output (not a positional
    pick): 3 of 4 branches vote `{"v": "x"}` → that is the aggregate."""
    ledger = _RecordingLedger()
    outputs = {0: {"v": "z"}, 1: {"v": "x"}, 2: {"v": "x"}, 3: {"v": "x"}}
    result = _run(
        steps=[_branch_step(i) for i in range(4)],
        dispatcher=_VariedDispatcher(outputs=outputs),
        ledger=ledger,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["aggregate"] == {"v": "x"}


def test_parallelization_aggregate_all_distinct_lowest_index_tiebreak() -> None:
    """All-distinct outputs → every vote is 1 → tie → the LOWEST branch-index
    wins (the deterministic floor — pinned as intended, not accidental)."""
    ledger = _RecordingLedger()
    outputs = {0: {"v": "a"}, 1: {"v": "b"}, 2: {"v": "c"}}
    result = _run(
        steps=[_branch_step(i) for i in range(3)],
        dispatcher=_VariedDispatcher(outputs=outputs),
        ledger=ledger,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["aggregate"] == {"v": "a"}


def test_parallelization_two_way_tie_breaks_to_lowest_index() -> None:
    """A 2-vs-2 count tie breaks to the lowest branch-index among the tied
    outputs (branch 0's `{"v": "a"}`, not branch 1's `{"v": "b"}`)."""
    ledger = _RecordingLedger()
    outputs = {0: {"v": "a"}, 1: {"v": "b"}, 2: {"v": "a"}, 3: {"v": "b"}}
    result = _run(
        steps=[_branch_step(i) for i in range(4)],
        dispatcher=_VariedDispatcher(outputs=outputs),
        ledger=ledger,
    )
    assert result.final_state is not None
    assert result.final_state["aggregate"] == {"v": "a"}


# ---------------------------------------------------------------------------
# Branch-index order + branch_metadata causality
# ---------------------------------------------------------------------------


def test_parallelization_persisted_in_branch_index_order() -> None:
    """Drained entries are grouped + ordered by branch_index (U-CP-82 drain)."""
    ledger = _RecordingLedger()
    _run(
        steps=[_branch_step(i) for i in range(3)],
        dispatcher=_VariedDispatcher(),
        ledger=ledger,
    )
    assert _branch_indices_in_append_order(ledger) == [0, 0, 1, 1, 2, 2]


def test_parallelization_branch_metadata_causality() -> None:
    """Each branch step entry carries causality-only `branch_metadata`
    (`terminal_status=None`); a fresh terminal entry carries `completed`
    (dispatch-boundary disposition; never `failed`)."""
    ledger = _RecordingLedger()
    _run(
        steps=[_branch_step(i) for i in range(2)],
        dispatcher=_VariedDispatcher(),
        ledger=ledger,
    )
    # appends: [b0 step, b0 terminal, b1 step, b1 terminal]
    fanout = "workflow:wf-par:fanout"
    for branch_index in (0, 1):
        step_payload, _ = ledger.appends[branch_index * 2]
        terminal_payload, _ = ledger.appends[branch_index * 2 + 1]
        # Causality: parent_action_id = the fan-out point; branch_index set.
        assert str(step_payload.branch_metadata.parent_action_id) == fanout
        assert step_payload.branch_metadata.branch_index == branch_index
        assert step_payload.branch_metadata.terminal_status is None
        # Terminal: a fresh entry (distinct action_id) carrying `completed`.
        assert terminal_payload.branch_metadata.branch_index == branch_index
        assert terminal_payload.branch_metadata.terminal_status == "completed"
        assert str(step_payload.action_id) == f"{fanout}:branch:{branch_index}:step:0"
        assert str(terminal_payload.action_id) == f"{fanout}:branch:{branch_index}:terminal"


def test_parallelization_preserves_all_branch_outputs() -> None:
    """No branch result is discarded — all N outputs survive in
    `final_state.branch_outputs` (parity with the linear step_id keying)."""
    ledger = _RecordingLedger()
    outputs = {i: {"v": i} for i in range(5)}
    result = _run(
        steps=[_branch_step(i) for i in range(5)],
        dispatcher=_VariedDispatcher(outputs=outputs),
        ledger=ledger,
    )
    assert result.final_state is not None
    assert result.final_state["branch_outputs"] == {f"branch-{i}": {"v": i} for i in range(5)}


# ---------------------------------------------------------------------------
# Failure + lifecycle + scope
# ---------------------------------------------------------------------------


def test_parallelization_pause_branch_failure_fails_honestly_when_no_protocol_bound() -> None:
    """B-FANOUT-PAUSE-PARALLELIZATION — under `pause` (TEAM_BINDING) a branch failure
    with NO `pause_resume_protocol` bound fails HONESTLY: FAILED +
    `parallelization-pause-resume-protocol-not-bound` (a false-resumable PAUSED is
    foreclosed — returning PAUSED without a snapshot would advertise a resumability
    `api.resume` cannot honor). The resumable PAUSED path (protocol bound) is covered
    in `test_workflow_driver_parallelization_pause.py`. The completed branches'
    entries STILL persist; the salvaged survivors are `partial_state`.

    Supersedes the interim `...-pause-resume-not-yet-materialized` FAILED: the
    follow-on `B-FANOUT-PAUSE-PARALLELIZATION` materialized §25.15.1 `pause → PAUSED`,
    so the only honest-FAILED remaining on the `pause` path is the protocol-not-bound
    detect-then-refuse (mirrors `_execute_orchestrator_workers`)."""
    ledger = _RecordingLedger()
    # Deterministic: branch 1 raises only AFTER branches 0 + 2 have completed (so
    # they have buffered) — not the timing-flaky plain `_VariedDispatcher(fail_index=1)`,
    # whose unsynchronized failure could cancel a still-pending sibling before it
    # buffered (the race that surfaced as a CI flake on slower schedulers).
    result = _run(
        steps=[_branch_step(i) for i in range(3)],
        dispatcher=_FailAfterSiblingsCompleteDispatcher(n=3, fail_index=1),
        ledger=ledger,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "parallelization-pause-resume-protocol-not-bound" in result.fail_class
    # No false-PAUSED leaks (the silent-degradation mode this arc forecloses).
    assert result.status is not RunStatus.PAUSED
    # All three branches persist now: branch 1 ran-and-errored → records its step
    # + a `completed` terminal (obl. 3 — dispatch-boundary, no silent gap); 0 + 2
    # completed cleanly. Each branch = {step, terminal}.
    persisted = _branch_indices_in_append_order(ledger)
    assert sorted(set(persisted)) == [0, 1, 2]
    # The salvaged survivors (the clean branches 0 + 2) are carried as partial_state.
    assert result.partial_state is not None
    assert set(result.partial_state["branch_outputs"]) == {"branch-0", "branch-2"}


def test_parallelization_proceed_branch_failure_degrades_to_partial() -> None:
    """B-PARALLELIZATION-CASCADE — under `proceed` (SOLO_DEVELOPER) a branch
    failure does NOT fail-fast the whole fan-out (the U-CP-86 happy-path-only bug):
    surviving branches run to completion and the run degrades to PARTIAL with the
    survivors salvaged — §25.15.1 `proceed → PARTIAL`."""
    ledger = _RecordingLedger()
    result = _run(
        steps=[_branch_step(i) for i in range(3)],
        dispatcher=_VariedDispatcher(fail_index=1),
        ledger=ledger,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    assert result.status is RunStatus.PARTIAL
    assert result.fail_class is None
    # Survivors salvaged; the failed branch contributes nothing to the aggregate.
    assert result.partial_state is not None
    assert set(result.partial_state["branch_outputs"]) == {"branch-0", "branch-2"}
    assert result.final_state is None
    # All three branches recorded (no silent loss): the failed branch persists its
    # step + a `completed` terminal too.
    assert sorted(set(_branch_indices_in_append_order(ledger))) == [0, 1, 2]


def test_parallelization_unbound_step_kind_fails_loud_not_silent_partial() -> None:
    """B-PARALLELIZATION-CASCADE (out-of-family Codex [P2]) — an UNBOUND StepKind
    is a SETUP/config error, NOT a degradable branch failure: even under `proceed`
    (SOLO_DEVELOPER) it must FAIL the whole run LOUD, never silently degrade to
    PARTIAL (which would drop the branch). The pre-flight dispatcher resolution
    catches it before fan-out, matching the linear path + the old fail-fast."""
    ledger = _RecordingLedger()
    # `_MultiKindRegistry` only binds DECLARATIVE_STEP → a TOOL_STEP branch raises
    # StepKindDispatcherNotBoundError at lookup (the setup error).
    unbound = WorkflowStep(
        step_id=StepID("branch-0"), step_kind=StepKind.TOOL_STEP, step_payload={"index": 0}
    )
    result = _run(
        steps=[unbound],
        dispatcher=_VariedDispatcher(),
        ledger=ledger,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    assert result.status is RunStatus.FAILED
    assert result.status is not RunStatus.PARTIAL
    assert result.fail_class is not None
    assert "parallelization-step-kind-not-bound" in result.fail_class


def test_parallelization_cascade_cancel_branch_failure_returns_failed() -> None:
    """B-PARALLELIZATION-CASCADE — under `cascade-cancel` (MULTI_TENANT_COMPLIANCE)
    a branch failure halts the fan-out → FAILED + `parallelization-cascade-cancel`
    (§25.15.1 `cascade-cancel → FAILED`); the completed siblings' entries persist
    (no silent loss)."""
    ledger = _RecordingLedger()
    result = _run(
        steps=[_branch_step(i) for i in range(3)],
        dispatcher=_FailAfterSiblingsCompleteDispatcher(n=3, fail_index=1),
        ledger=ledger,
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "parallelization-cascade-cancel" in result.fail_class
    # cascade-cancel does not salvage survivors into partial_state.
    assert result.partial_state is None
    # The completed branches still persist (audit-honoring; no silent failure).
    assert sorted(set(_branch_indices_in_append_order(ledger))) == [0, 1, 2]


def test_parallelization_emits_workflow_start_and_step_boundaries() -> None:
    """WORKFLOW_START emitted once at fan-out; one STEP_BOUNDARY per branch
    (single-threaded at the drain — never from the worker threads)."""
    ledger = _RecordingLedger()
    emitter = _Emitter()
    _run(
        steps=[_branch_step(i) for i in range(3)],
        dispatcher=_VariedDispatcher(),
        ledger=ledger,
        emitter=emitter,
    )
    assert emitter.emits.count(WorkflowEventClass.WORKFLOW_START) == 1
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 3


def test_parallelization_empty_steps_returns_success() -> None:
    """An empty step sequence → trivially SUCCESS with an empty aggregate (no
    fan-out; mirrors the linear empty-loop SUCCESS)."""
    ledger = _RecordingLedger()
    result = _run(steps=[], dispatcher=_VariedDispatcher(), ledger=ledger)
    assert result.status is RunStatus.SUCCESS
    assert result.final_state == {"branch_outputs": {}, "aggregate": {}}
    assert ledger.appends == []


class _BlockingDispatcher:
    """A SYNC dispatch that blocks past the barrier deadline (a wedged branch).

    Self-releases at `self_release_seconds` as a CI backstop so a regression
    FAILS the elapsed-time bound instead of hanging the suite; the test also
    sets `release` in a `finally` so the abandoned threads exit promptly."""

    def __init__(self, *, release: threading.Event, self_release_seconds: float) -> None:
        self._release = release
        self._self_release_seconds = self_release_seconds

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self._release.wait(timeout=self._self_release_seconds)
        return {"branch": int(step.step_payload["index"])}


def test_parallelization_barrier_deadline_does_not_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch stuck past the wall-clock deadline → the run returns FAILED
    PROMPTLY (the §25.11 bounded-barrier "cannot strand its parent" guarantee).

    Regression guard for the `asyncio.run`-joins-the-executor hazard: a wedged
    SYNC branch thread is not cancellable, and `asyncio.run` would join it at
    shutdown — re-defeating the deadline (the parent would hang ~`block` seconds
    until the threads self-release). `_run_fanout_to_completion` abandons the
    executor instead, so the parent returns at the ~0.2s deadline. The elapsed
    bound distinguishes the fix (~0.2s) from the regression (~`block`s)."""
    from harness_cp import workflow_driver as wd

    monkeypatch.setattr(wd, "_DEFAULT_FANOUT_BARRIER_DEADLINE_SECONDS", 0.2)
    release = threading.Event()
    dispatcher = _BlockingDispatcher(release=release, self_release_seconds=2.0)
    ledger = _RecordingLedger()
    started = time.monotonic()
    try:
        result = _run(
            steps=[_branch_step(i) for i in range(3)],
            dispatcher=cast(StepDispatcher, dispatcher),
            ledger=ledger,
        )
    finally:
        release.set()  # let the abandoned worker threads exit (no leak)
    elapsed = time.monotonic() - started
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    # B-PARALLELIZATION-CASCADE — a stuck fan-out (no branch raised) under the
    # resolved cascade_policy is a barrier-deadline FAILED (TEAM→pause: the
    # deadline-strike has no clean pause boundary → FAILED, not a false-PAUSED).
    assert "parallelization-barrier-deadline" in result.fail_class
    # Returned at ~the 0.2s deadline, NOT after the 2.0s block — the parent is
    # not stranded by the wedged sync threads (Codex [P1] regression guard).
    assert elapsed < 1.5


# ---------------------------------------------------------------------------
# Live e2e — real IS writer; §6.3 hash chain re-verifies post-drain
# ---------------------------------------------------------------------------


def test_parallelization_live_e2e_real_ledger_chain_valid(tmp_path: Path) -> None:
    """A real fan-out through `execute_workflow` into the REAL IS writer: the
    persisted branch entries re-verify as a VALID §6.3 hash chain, drained in
    branch-index order. Provider-free (declarative branches) — no external
    credential needed."""
    handle = JsonlLedgerHandle(canonical_path=tmp_path / "state.jsonl", exists=False, entry_count=0)
    real = _RealLedgerWriter(handle=handle, actor=_ACTOR)
    result = _run(
        steps=[_branch_step(i) for i in range(4)],
        dispatcher=_VariedDispatcher(),
        ledger=real,
    )
    assert result.status is RunStatus.SUCCESS
    # Every drained append landed (no silent IDEMPOTENT_NOOP drop): 4 × 2 = 8.
    assert len(real.results) == 8
    entries = read_ledger(handle)
    assert len(entries) == 8
    # §6.3 chain re-verifies VALID after the barrier drain.
    assert verify_chain(entries).status is VerificationStatus.VALID
    # Persisted in branch-index order (the deterministic drain).
    persisted_branch_indices = [
        e.branch_metadata.branch_index for e in entries if e.branch_metadata is not None
    ]
    assert persisted_branch_indices == [0, 0, 1, 1, 2, 2, 3, 3]


# ---------------------------------------------------------------------------
# B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3/§4) — opt-in terminal synthesis step
# ---------------------------------------------------------------------------


class _SynthesisCapturingDispatcher:
    """A `POST_JOIN_SYNTHESIS` dispatcher that CAPTURES the branch-index-ordered
    sibling outputs it receives + returns a synthesized aggregate. Stands in for
    the runtime `PostJoinSynthesisStepDispatcher` (no real LLM in this CP-side
    witness — the full-chain real-provider witness is the runtime e2e)."""

    def __init__(self) -> None:
        self.received_siblings: Any = None
        self.received_step_kind: Any = None

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self.received_siblings = step_context.sibling_outputs
        self.received_step_kind = step.step_kind
        return {"synthesis": "composed", "n_siblings": len(step_context.sibling_outputs)}


class _BranchOrSynthesisRegistry:
    """Binds `DECLARATIVE_STEP` (branches) + `POST_JOIN_SYNTHESIS` (synthesis)."""

    def __init__(self, branch: StepDispatcher, synthesis: StepDispatcher) -> None:
        self._branch = branch
        self._synthesis = synthesis

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind is StepKind.DECLARATIVE_STEP:
            return self._branch
        if step_kind is StepKind.POST_JOIN_SYNTHESIS:
            return self._synthesis
        raise StepKindDispatcherNotBoundError(step_kind)


def _synthesis_step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"prompt": "compose the siblings"},
    )


def test_parallelization_post_join_synthesis_replaces_fold_and_reads_siblings() -> None:
    """An opt-in terminal POST_JOIN_SYNTHESIS step REPLACES the deterministic fold
    on SUCCESS: it is carved out of the branch set, receives the branch-index-
    ordered sibling outputs, and ITS output becomes final_state; a disclosing
    synthesis ledger entry is appended POST-drain (CP spec v1.54 §3/§4 — the new
    capability, the §25.12 Point-2 sacrifice)."""
    ledger = _RecordingLedger()
    synth = _SynthesisCapturingDispatcher()
    ctx = cast(DriverContext, _Ctx(ledger=ledger, emitter=_Emitter()))
    result = execute_workflow(
        _manifest(persona_tier=PersonaTier.SOLO_DEVELOPER),
        [_branch_step(0), _branch_step(1), _branch_step(2), _synthesis_step()],
        run_id="run-1",
        ctx=ctx,
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry,
            _BranchOrSynthesisRegistry(_VariedDispatcher(), synth),
        ),
    )
    assert result.status is RunStatus.SUCCESS
    # final_state IS the synthesis output (NOT the {branch_outputs, aggregate} fold).
    assert result.final_state == {"synthesis": "composed", "n_siblings": 3}
    # The synthesis dispatcher (NOT a branch) received the 3 branch-index-ORDERED
    # siblings — the synthesis step itself was carved OUT of the branch set.
    assert synth.received_step_kind is StepKind.POST_JOIN_SYNTHESIS
    assert [bi for bi, _out in synth.received_siblings] == [0, 1, 2]
    assert synth.received_siblings[0][1] == {"branch": 0, "echoed": {"index": 0}}
    # A disclosing post-join-synthesis ledger entry was appended (§25.12 Point-2
    # sacrifice disclosure), AFTER the 3 branches' {step, terminal} entries:
    #   3 branches × {step, terminal} = 6 + 1 synthesis = 7.
    synth_keys = [
        wk for _payload, wk in ledger.appends if str(wk.step_id).startswith("post-join-synthesis")
    ]
    assert len(synth_keys) == 1
    assert len(ledger.appends) == 7


def test_parallelization_without_synthesis_uses_deterministic_fold() -> None:
    """Negative control: absent a POST_JOIN_SYNTHESIS terminal step, the
    deterministic fold is byte-identical to pre-v1.54 (no synthesis dispatch, no
    synthesis entry)."""
    ledger = _RecordingLedger()
    result = _run(
        steps=[_branch_step(i) for i in range(3)],
        dispatcher=_VariedDispatcher(),
        ledger=ledger,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    # The deterministic-fold shape, NOT a synthesis output.
    assert set(result.final_state) == {"branch_outputs", "aggregate"}
    # No post-join-synthesis entry; 3 branches × {step, terminal} = 6.
    assert not any(str(wk.step_id).startswith("post-join-synthesis") for _p, wk in ledger.appends)
    assert len(ledger.appends) == 6
