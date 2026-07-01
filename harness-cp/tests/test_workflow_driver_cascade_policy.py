"""U-CP-85 — `cascade_policy` consumption + cascade-cancel reach (C-CP-25 §25.15).

Exercises the cascade-policy machinery (R-FS-1 arc #11):

- `cascade_policy_run_status` — the §25.15.1 on-branch-failure run-level status
  mapping (obligation 6).
- `resume_should_redispatch` — resume-idempotency-terminality (obligation 7).
- `cascade_cancel_barrier` — `asyncio.TaskGroup` structured cancellation of
  not-yet-dispatched siblings + the wall-clock deadline hard cap (obl. 1 + 8).
- `dispatch_branch_step_shielded` — an in-flight effectful dispatch runs to
  completion / deadline-timeout under a cascade-cancel (obl. 1 + 3 + 4).

The three discriminating terminal dispositions (`cancelled` / `completed` /
`timed_out`, obligation 4) are driven through REAL `asyncio` cancellation via a
synthetic branch harness (`_cascade_branch`) that mirrors the canonical
gate → shielded-dispatch → classify → record shape documented at
`dispatch_branch_step_shielded` — the shape the U-CP-88 ORCHESTRATOR_WORKERS
strategy (the first real cascade-policy consumer) follows. The machinery is
unit-proven here against synthetic branches; the real-strategy + `RunResult`
status e2e lands at U-CP-88.

`asyncio_mode = "auto"` (pyproject §366) → `async def test_*` runs directly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_cp.cp_shared_types import AgentRole
from harness_cp.gate_level_rule import GateLevel
from harness_cp.topology_pattern import CascadePolicy
from harness_cp.workflow_driver import (
    BufferingLedgerWriter,
    append_branch_step_ledger_entry,
    append_branch_terminal_ledger_entry,
    cascade_cancel_barrier,
    cascade_policy_run_status,
    dispatch_branch_step_shielded,
    drain_branch_buffers,
    resume_should_redispatch,
)
from harness_cp.workflow_driver_errors import BranchBarrierDeadlineExceededError
from harness_cp.workflow_driver_types import RunStatus, compose_branch_child_context
from harness_is.state_ledger_entry_schema import Actor, ActorClass

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="cascade-test")
_TS = datetime(2026, 6, 13, tzinfo=UTC)

# Generous timing margins so the cancellation-ordering tests are deterministic
# (and never flaky under CI load — `[[ci-flaky-flush-to-sqlite-perf-test]]`).
_FAST = 0.002
_SLOW = 0.05
_STUCK = 5.0  # >> any deadline — a "stuck" dispatch the deadline must cut off


def _linear_ctx(
    *,
    parent_action_id: str = "workflow:wf-1:step:3",
    step_index: int = 3,
):
    """A per-step context as the SINGLE_THREADED_LINEAR path composes it (no
    branch fields) — the spawning context a branch fan-out descends from."""
    from harness_cp.workflow_driver_types import StepExecutionContext

    return StepExecutionContext(
        workflow_id="wf-1",
        parent_action_id=parent_action_id,
        parent_gate_level=GateLevel.ASK,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=_ACTOR,
        parent_entry_hash="",
        parent_idempotency_key="k",
        tenant_id=None,
        step_index=step_index,
    )


class _RecordingWriter:
    """A real `LedgerWriterLike` sink that records drained append order — the
    single writer `drain_branch_buffers` serializes through."""

    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appended: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> None:
        self.appended.append((payload, write_key))

    @property
    def is_genesis(self) -> bool:
        return len(self.appended) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appended)


class _Step(NamedTuple):
    """One synthetic branch step: a gate delay (pre-dispatch boundary), a
    dispatch delay (the 'model call' latency), an optional gate-time failure (the
    branch raises at the gate — the cascade trigger), and an optional
    dispatch-time error (the in-flight model/tool call RAISES after its latency —
    exercises the F2-01 ran-and-errored-under-cascade-cancel path)."""

    gate: float
    dispatch: float
    fail: bool = False
    dispatch_raises: bool = False


async def _effect(
    trace: list[tuple[str, int, int]],
    *,
    branch_index: int,
    local: int,
    delay: float,
    raises: bool = False,
) -> tuple[int, int]:
    """A synthetic effectful dispatch: sleeps `delay` (the latency that scrambles
    completion order). If `raises`, the dispatch RAN but ERRORED (a model/tool
    error) — it raises after the latency, before recording its landed effect.
    Otherwise it records its landed effect into `trace` and returns."""
    await asyncio.sleep(delay)
    if raises:
        raise RuntimeError(f"dispatch error b{branch_index}s{local}")
    trace.append(("dispatch", branch_index, local))
    return (branch_index, local)


async def _cascade_branch(
    *,
    parent,
    branch_index: int,
    run_idempotency_key: str,
    buffer: BufferingLedgerWriter,
    dispositions: dict[int, str],
    trace: list[tuple[str, int, int]],
    steps: Sequence[_Step],
    dispatch_started: asyncio.Event | None = None,
) -> tuple[int, int] | None:
    """Synthetic stand-in for a U-CP-88 cascade branch — the canonical
    gate → shielded-dispatch → classify → record shape (see
    `dispatch_branch_step_shielded`). Records each step + a discriminating
    terminal entry (U-CP-84) into `buffer`; reports its disposition into
    `dispositions[branch_index]`; appends gate/dispatch events to `trace`.
    Re-raises `CancelledError` to honor a cascade-cancel of this branch."""
    child = compose_branch_child_context(
        parent, branch_index=branch_index, agent_role=AgentRole("w")
    )

    def _record_step(local: int) -> None:
        append_branch_step_ledger_entry(
            branch_writer=buffer,
            branch_context=child,
            run_idempotency_key=run_idempotency_key,
            local_step_index=local,
            timestamp=_TS,
        )

    def _record_terminal(status: Literal["cancelled", "completed", "timed_out"]) -> None:
        dispositions[branch_index] = status
        append_branch_terminal_ledger_entry(
            branch_writer=buffer,
            branch_context=child,
            run_idempotency_key=run_idempotency_key,
            terminal_status=status,
            timestamp=_TS,
        )

    last: tuple[int, int] | None = None
    for local, step in enumerate(steps):
        # Pre-dispatch gate (obl. 2/5) — a not-yet-dispatched boundary where a
        # cascade-cancel is clean (no effect in flight) → `cancelled`.
        try:
            await asyncio.sleep(step.gate)
        except asyncio.CancelledError:
            _record_terminal("cancelled")
            raise
        trace.append(("gate", branch_index, local))
        if step.fail:
            raise ValueError(f"branch {branch_index} step {local} boom")
        inflight = asyncio.ensure_future(
            _effect(
                trace,
                branch_index=branch_index,
                local=local,
                delay=step.dispatch,
                raises=step.dispatch_raises,
            )
        )
        if dispatch_started is not None:
            dispatch_started.set()
        try:
            last = await dispatch_branch_step_shielded(inflight)
        except asyncio.CancelledError:
            # obl. 3: the step WAS dispatched → record its step entry before the
            # terminal marker on BOTH terminal paths (completed + timed_out);
            # only `cancelled` (not-yet-dispatched, handled at the gate) records none.
            _record_step(local)
            _record_terminal(
                "timed_out" if (inflight.cancelled() or not inflight.done()) else "completed"
            )
            raise
        _record_step(local)
    _record_terminal("completed")
    return last


async def _fail_after_dispatch_started(dispatch_started: asyncio.Event) -> None:
    await asyncio.wait_for(dispatch_started.wait(), timeout=1.0)
    raise ValueError("branch 0 step 0 boom")


# ---------------------------------------------------------------------------
# Pure decision functions — `cascade_policy_run_status` (obl. 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (CascadePolicy.CASCADE_CANCEL, RunStatus.FAILED),
        (CascadePolicy.PROCEED, RunStatus.PARTIAL),
        (CascadePolicy.PAUSE, RunStatus.PAUSED),
    ],
)
def test_cascade_policy_run_status_mapping(policy: CascadePolicy, expected: RunStatus) -> None:
    """§25.15.1 on-branch-failure run-level status: cascade-cancel→FAILED,
    proceed→PARTIAL, pause→PAUSED (existing RunStatus members; no new value)."""
    assert cascade_policy_run_status(policy) is expected


def test_cascade_policy_run_status_covers_all_members() -> None:
    """Every `CascadePolicy` member maps to a status — guards the mapping table
    against silent enum growth (the StrEnum is closed at 3; a 4th member would
    KeyError here rather than fall through silently)."""
    for policy in CascadePolicy:
        assert isinstance(cascade_policy_run_status(policy), RunStatus)


def test_partial_belongs_to_proceed_not_cascade_cancel() -> None:
    """Obl. 6 (advisor-caught at the council): PARTIAL is `proceed`'s status,
    NEVER cascade-cancel's (cascade-cancel → FAILED). No `degraded` field is
    minted — RunStatus.PARTIAL is the sole degradation signal."""
    assert cascade_policy_run_status(CascadePolicy.PROCEED) is RunStatus.PARTIAL
    assert cascade_policy_run_status(CascadePolicy.CASCADE_CANCEL) is RunStatus.FAILED
    assert cascade_policy_run_status(CascadePolicy.CASCADE_CANCEL) is not RunStatus.PARTIAL


# ---------------------------------------------------------------------------
# Pure decision functions — `resume_should_redispatch` (obl. 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("terminal_status", "expected"),
    [
        ("cancelled", False),
        ("completed", False),
        ("timed_out", False),
        (None, True),
    ],
)
def test_resume_should_redispatch(
    terminal_status: Literal["cancelled", "completed", "timed_out"] | None,
    expected: bool,
) -> None:
    """Obl. 7 resume-idempotency-terminality: a branch with ANY persisted
    terminal disposition is NOT re-dispatched; only `None` (no persisted terminal
    entry) is re-dispatch-eligible."""
    assert resume_should_redispatch(terminal_status) is expected


def test_resume_does_not_redispatch_terminal_branches() -> None:
    """Obl. 7 integration: a synthetic `api.resume` loop over persisted branch
    terminal_statuses re-dispatches ONLY the never-terminated (`None`) branch."""
    persisted: dict[int, Literal["cancelled", "completed", "timed_out"] | None] = {
        0: "completed",
        1: "cancelled",
        2: "timed_out",
        3: None,  # never reached a dispatch boundary
    }
    redispatched = [bi for bi, ts in persisted.items() if resume_should_redispatch(ts)]
    assert redispatched == [3]


# ---------------------------------------------------------------------------
# `cascade_cancel_barrier` — structured cancellation + deadline (obl. 1 + 8)
# ---------------------------------------------------------------------------


async def test_cascade_cancel_barrier_clean_path_returns_input_order() -> None:
    """All branches succeed → results returned in INPUT (branch) order,
    independent of completion order (branch 0 finishes LAST here)."""

    async def _branch(value: int, delay: float) -> int:
        await asyncio.sleep(delay)
        return value

    # branch 0 is slowest → completes last; result order must still be [0,1,2].
    results = await cascade_cancel_barrier(
        [_branch(0, _SLOW), _branch(1, _FAST), _branch(2, _FAST)],
        deadline_seconds=_STUCK,
    )
    assert results == [0, 1, 2]


async def test_cascade_cancel_barrier_branch_failure_raises_exception_group() -> None:
    """A branch raising surfaces as a BaseExceptionGroup carrying the original
    exception UNCHANGED (the strategy maps it to RunStatus.FAILED)."""

    async def _ok() -> int:
        await asyncio.sleep(_FAST)
        return 1

    async def _boom() -> int:
        await asyncio.sleep(_FAST)
        raise ValueError("branch boom")

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await cascade_cancel_barrier([_ok(), _boom()], deadline_seconds=_STUCK)
    assert any(isinstance(e, ValueError) for e in excinfo.value.exceptions)


async def test_cascade_cancel_barrier_cancels_not_yet_dispatched_siblings() -> None:
    """Obl. 1 + 8: a branch failure cancels a sibling at a NOT-YET-DISPATCHED
    boundary — the sibling's later effect never fires (cleanly cancellable)."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buffers = {bi: BufferingLedgerWriter(actor=_ACTOR, branch_index=bi) for bi in (0, 1)}

    with pytest.raises(BaseExceptionGroup):
        await cascade_cancel_barrier(
            [
                # branch 0: fails at its gate shortly after start.
                _cascade_branch(
                    parent=parent,
                    branch_index=0,
                    run_idempotency_key="rik",
                    buffer=buffers[0],
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_FAST, dispatch=0.0, fail=True)],
                ),
                # branch 1: still inside a LONG gate when branch 0 fails → cancelled
                # at a not-yet-dispatched boundary; its dispatch never fires.
                _cascade_branch(
                    parent=parent,
                    branch_index=1,
                    run_idempotency_key="rik",
                    buffer=buffers[1],
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_SLOW, dispatch=_FAST)],
                ),
            ],
            deadline_seconds=_STUCK,
        )

    assert dispositions[1] == "cancelled"
    # branch 1's effect never landed (no ("dispatch", 1, 0) in the trace).
    assert ("dispatch", 1, 0) not in trace


async def test_cascade_cancel_barrier_deadline_exceeded_raises() -> None:
    """§25.11: a stuck branch is bounded — the wall-clock deadline raises
    BranchBarrierDeadlineExceededError (and does NOT wait the full dispatch)."""

    async def _stuck() -> int:
        await asyncio.sleep(_STUCK)
        return 1

    t0 = time.monotonic()
    with pytest.raises(BranchBarrierDeadlineExceededError):
        await cascade_cancel_barrier([_stuck()], deadline_seconds=_SLOW)
    assert time.monotonic() - t0 < 1.0  # cut off at ~_SLOW, NOT _STUCK (no hang)


async def test_cascade_cancel_barrier_leak_free_after_failure() -> None:
    """Obl. 8 leak-freedom: after a branch failure, no sibling task is left
    pending/running (the foreclosed `gather`-leaks-orphans anti-pattern)."""
    started: list[int] = []
    cancelled: list[int] = []

    async def _ok(name: int) -> int:
        started.append(name)
        try:
            await asyncio.sleep(_STUCK)  # would strand if not cancelled
        except asyncio.CancelledError:
            cancelled.append(name)
            raise
        return name

    async def _boom() -> int:
        await asyncio.sleep(_FAST)
        raise ValueError("boom")

    with pytest.raises(BaseExceptionGroup):
        await cascade_cancel_barrier([_ok(0), _ok(1), _boom()], deadline_seconds=_STUCK)
    # both long-running siblings were cancelled (not left orphaned).
    assert sorted(cancelled) == [0, 1]


# ---------------------------------------------------------------------------
# Discriminating terminal_status — cancelled / completed / timed_out (obl. 4)
# Driven through REAL asyncio cancellation; persisted via U-CP-84 (drained).
# ---------------------------------------------------------------------------


def _drained_terminal_status(buffer: BufferingLedgerWriter) -> Any:
    """Drain a branch buffer through a real writer and read the persisted
    `branch_metadata.terminal_status` of its terminal entry (the LAST append)."""
    writer = _RecordingWriter()
    drain_branch_buffers(writer, [buffer])
    payload, _ = writer.appended[-1]
    return payload.branch_metadata.terminal_status


async def test_branch_cancelled_at_not_yet_dispatched_boundary() -> None:
    """`cancelled`: a sibling fails while this branch sits at its gate (no
    in-flight dispatch) → terminal_status `cancelled`, no effect landed.
    The classification PERSISTS through U-CP-84's terminal entry."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buf = BufferingLedgerWriter(actor=_ACTOR, branch_index=1)

    with pytest.raises(BaseExceptionGroup):
        await cascade_cancel_barrier(
            [
                _cascade_branch(
                    parent=parent,
                    branch_index=0,
                    run_idempotency_key="rik",
                    buffer=BufferingLedgerWriter(actor=_ACTOR, branch_index=0),
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_FAST, dispatch=0.0, fail=True)],
                ),
                _cascade_branch(
                    parent=parent,
                    branch_index=1,
                    run_idempotency_key="rik",
                    buffer=buf,
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_SLOW, dispatch=_FAST)],
                ),
            ],
            deadline_seconds=_STUCK,
        )

    assert dispositions[1] == "cancelled"
    assert ("dispatch", 1, 0) not in trace
    assert _drained_terminal_status(buf) == "cancelled"


async def test_branch_completed_when_in_flight_runs_to_completion() -> None:
    """`completed`: a sibling fails while this branch's effectful step is IN
    FLIGHT → the shielded dispatch runs to completion (the effect LANDS + is
    recorded), terminal_status `completed` (obl. 1 + 3 + 4)."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buf = BufferingLedgerWriter(actor=_ACTOR, branch_index=1)
    dispatch_started = asyncio.Event()

    with pytest.raises(BaseExceptionGroup):
        await cascade_cancel_barrier(
            [
                _fail_after_dispatch_started(dispatch_started),
                # branch 1 enters its dispatch (fast gate) then is cancelled mid
                # in-flight when branch 0 fails → drives to completion.
                _cascade_branch(
                    parent=parent,
                    branch_index=1,
                    run_idempotency_key="rik",
                    buffer=buf,
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_FAST, dispatch=_SLOW)],
                    dispatch_started=dispatch_started,
                ),
            ],
            deadline_seconds=_STUCK,
        )

    assert dispositions[1] == "completed"
    # the in-flight effect LANDED despite the cascade-cancel (obl. 1 + 3).
    assert ("dispatch", 1, 0) in trace
    assert _drained_terminal_status(buf) == "completed"


async def test_branch_timed_out_when_deadline_cuts_off_in_flight() -> None:
    """`timed_out`: the barrier deadline cuts off an in-flight dispatch →
    terminal_status `timed_out` (obl. 1 "...or barrier-deadline timeout"). The
    deadline is a HARD cap — it does NOT wait the full _STUCK dispatch."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buf = BufferingLedgerWriter(actor=_ACTOR, branch_index=0)

    t0 = time.monotonic()
    with pytest.raises(BranchBarrierDeadlineExceededError):
        await cascade_cancel_barrier(
            [
                _cascade_branch(
                    parent=parent,
                    branch_index=0,
                    run_idempotency_key="rik",
                    buffer=buf,
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_FAST, dispatch=_STUCK)],
                )
            ],
            deadline_seconds=_SLOW,
        )

    assert time.monotonic() - t0 < 1.0  # cut off at ~_SLOW, NOT _STUCK
    assert dispositions[0] == "timed_out"
    assert ("dispatch", 0, 0) not in trace  # the stuck dispatch never completed
    # obl. 3: the dispatched-but-timed-out step has its OWN step entry (None) +
    # the discriminating terminal entry (timed_out) — the step WAS dispatched
    # (effect may have landed), so it is NOT a silent audit gap.
    writer = _RecordingWriter()
    drain_branch_buffers(writer, [buf])
    statuses = [p.branch_metadata.terminal_status for p, _ in writer.appended]
    assert statuses == [None, "timed_out"]


async def test_cascade_deadline_multi_branch_all_in_flight_timed_out() -> None:
    """Multi-branch deadline (U-CP-88 is ALWAYS multi-branch — the single-branch
    deadline case is degenerate): 3 branches ALL with an in-flight dispatch >> the
    deadline → BranchBarrierDeadlineExceededError and ALL THREE persist
    `timed_out`. This pins the deadline exception surface — N children re-raising
    CancelledError WHILE `asyncio.timeout` is also cancelling must collapse to the
    deadline error, NOT leak a `BaseExceptionGroup[CancelledError]`. The deadline
    is a HARD cap (no _STUCK hang)."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buffers = {bi: BufferingLedgerWriter(actor=_ACTOR, branch_index=bi) for bi in (0, 1, 2)}

    t0 = time.monotonic()
    with pytest.raises(BranchBarrierDeadlineExceededError):
        await cascade_cancel_barrier(
            [
                _cascade_branch(
                    parent=parent,
                    branch_index=bi,
                    run_idempotency_key="rik",
                    buffer=buffers[bi],
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_FAST, dispatch=_STUCK)],
                )
                for bi in (0, 1, 2)
            ],
            deadline_seconds=_SLOW,
        )

    assert time.monotonic() - t0 < 1.0  # hard cap — NOT the _STUCK dispatch
    assert dispositions == {0: "timed_out", 1: "timed_out", 2: "timed_out"}
    # each branch persists a step entry (None) + a timed_out terminal entry (obl. 3).
    for bi in (0, 1, 2):
        writer = _RecordingWriter()
        drain_branch_buffers(writer, [buffers[bi]])
        statuses = [p.branch_metadata.terminal_status for p, _ in writer.appended]
        assert statuses == [None, "timed_out"]


# ---------------------------------------------------------------------------
# Review-hardening — nested fan-out hard cap (Codex P1) + dispatch-error-under-
# cascade (adversarial F2-01). Both reachable at U-CP-88/89.
# ---------------------------------------------------------------------------


async def test_nested_outer_deadline_is_hard_cap_over_inner_in_flight() -> None:
    """Nested fan-out (HIERARCHICAL_DELEGATION, U-CP-89): the OUTER barrier's
    deadline is a HARD cap over an INNER barrier's shielded in-flight dispatch.
    The `_BRANCH_INFLIGHT_DISPATCHES` registry CHAINS across nesting — an inner
    dispatch registers in the OUTER registry too — so the outer watchdog cuts off
    inner in-flight work. Without the chain, the outer `asyncio.timeout` would
    cancel only the outer branch task while the shielded inner dispatch (governed
    only by the LONGER inner deadline) outlives the outer deadline."""

    async def _inner_branch() -> int:
        inflight = asyncio.ensure_future(asyncio.sleep(_STUCK, result=1))
        return await dispatch_branch_step_shielded(inflight)

    async def _outer_branch() -> list[int]:
        # inner barrier with a LONG deadline — the outer SHORT deadline must win.
        return await cascade_cancel_barrier([_inner_branch()], deadline_seconds=_STUCK)

    t0 = time.monotonic()
    with pytest.raises(BranchBarrierDeadlineExceededError):
        await cascade_cancel_barrier([_outer_branch()], deadline_seconds=_SLOW)
    assert time.monotonic() - t0 < 1.0  # HARD CAP — NOT the inner _STUCK deadline/dispatch


async def test_branch_completed_when_in_flight_dispatch_errors_under_cascade() -> None:
    """F2-01 (adversarial): a sibling fails AND this branch's in-flight dispatch
    ALSO ERRORS during the cascade-cancel drive → the branch is `completed` (a
    ran-and-errored branch is `completed`: dispatch-boundary, not step-outcome),
    NOT spuriously FAILED, and its terminal_status IS recorded (no audit gap). The
    barrier's ExceptionGroup carries ONLY the triggering sibling's failure — the
    cancelled branch's dispatch error never escapes as a second failure."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buf = BufferingLedgerWriter(actor=_ACTOR, branch_index=1)
    dispatch_started = asyncio.Event()

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await cascade_cancel_barrier(
            [
                _fail_after_dispatch_started(dispatch_started),
                # branch 1 is in-flight (fast gate) when branch 0 fails; its
                # in-flight dispatch then ERRORS during the shielded drive.
                _cascade_branch(
                    parent=parent,
                    branch_index=1,
                    run_idempotency_key="rik",
                    buffer=buf,
                    dispositions=dispositions,
                    trace=trace,
                    steps=[_Step(gate=_FAST, dispatch=_SLOW, dispatch_raises=True)],
                    dispatch_started=dispatch_started,
                ),
            ],
            deadline_seconds=_STUCK,
        )

    # ONLY branch 0's ValueError surfaces — branch 1's dispatch RuntimeError did
    # NOT become a spurious second failure.
    assert excinfo.group_contains(ValueError)
    assert not excinfo.group_contains(RuntimeError)
    # branch 1 recorded a discriminating `completed` terminal — NO silent audit gap.
    assert dispositions[1] == "completed"
    writer = _RecordingWriter()
    drain_branch_buffers(writer, [buf])
    statuses = [p.branch_metadata.terminal_status for p, _ in writer.appended]
    assert statuses == [None, "completed"]  # step entry (dispatched) + completed terminal


# ---------------------------------------------------------------------------
# Obligations 2 + 3 — gate-before-dispatch + audit-completeness
# ---------------------------------------------------------------------------


async def test_no_gate_bypass_gate_fires_before_dispatch() -> None:
    """Obl. 2 (no-gate-bypass-by-buffering): every step's pre-dispatch gate fires
    BEFORE its effect dispatches — the buffered path defers the ledger WRITE, not
    the gate. The trace shows ('gate', …) strictly before ('dispatch', …)."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buf = BufferingLedgerWriter(actor=_ACTOR, branch_index=0)

    await cascade_cancel_barrier(
        [
            _cascade_branch(
                parent=parent,
                branch_index=0,
                run_idempotency_key="rik",
                buffer=buf,
                dispositions=dispositions,
                trace=trace,
                steps=[_Step(gate=_FAST, dispatch=_FAST), _Step(gate=_FAST, dispatch=_FAST)],
            )
        ],
        deadline_seconds=_STUCK,
    )

    # per step, the gate event precedes the dispatch event.
    for local in (0, 1):
        gate_i = trace.index(("gate", 0, local))
        dispatch_i = trace.index(("dispatch", 0, local))
        assert gate_i < dispatch_i


async def test_audit_completeness_every_dispatched_step_recorded() -> None:
    """Obl. 3 (audit-completeness): every fully-dispatched effectful step has its
    own recorded step ledger entry — no landed effect is silent. A 3-step clean
    branch buffers 3 step entries + 1 terminal entry."""
    parent = _linear_ctx()
    trace: list[tuple[str, int, int]] = []
    dispositions: dict[int, str] = {}
    buf = BufferingLedgerWriter(actor=_ACTOR, branch_index=0)

    await cascade_cancel_barrier(
        [
            _cascade_branch(
                parent=parent,
                branch_index=0,
                run_idempotency_key="rik",
                buffer=buf,
                dispositions=dispositions,
                trace=trace,
                steps=[_Step(gate=_FAST, dispatch=_FAST)] * 3,
            )
        ],
        deadline_seconds=_STUCK,
    )

    writer = _RecordingWriter()
    drain_branch_buffers(writer, [buf])
    statuses = [p.branch_metadata.terminal_status for p, _ in writer.appended]
    # 3 step entries (terminal_status None) + 1 terminal entry ("completed").
    assert statuses == [None, None, None, "completed"]
    assert dispositions[0] == "completed"
