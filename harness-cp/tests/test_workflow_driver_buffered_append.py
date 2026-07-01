"""B1-impl-3 branch buffered-append substrate — U-CP-82 + U-CP-83 (CP plan v2.32 §3.2).

U-CP-82 — buffered/deferred-append drain path + bounded barriers + determinism
(C-CP-25 §25.11/§25.12): the shared substrate every non-linear topology strategy
(U-CP-86..U-CP-90) reuses — a branch buffers its pending ledger entries
(`BufferingLedgerWriter`), the orchestrator drains them through the single real
writer in **branch-index order** at the barrier (`drain_branch_buffers`), and
every barrier is bounded by a wall-clock deadline (`bounded_barrier`). The
branch-step `action_id` honors the U-CP-81 forward obligation (no flat
`workflow:{wf}:step:{N}` shape).

U-CP-83 — branch-scoped idempotency-key composition (C-CP-25 §25.16): N branches
at the same declared `step_index` compose DISTINCT idempotency keys via
`branch_path` (no same-step collapse under the IS writer's `idempotency_key`-only
dedup); the `SINGLE_THREADED_LINEAR` path composes the existing key byte-identically.

**Scope note.** The per-branch control flow here (`_run_branch`) is a stand-in
for the U-CP-86+ strategy step loop — only the dispatch boundary (the
`asyncio.sleep` standing in for a model call's variable latency) is a stand-in.
The `BufferingLedgerWriter` + `drain_branch_buffers` + `bounded_barrier` + the
`compose_branch_*` composers + `_compute_step_idempotency_key` ARE the real
U-CP-82/83 substrate the strategies will invoke; the determinism AC is exercised
through real concurrency, not a pre-scrambled static list. The full strategy e2e
(real `StepDispatcher`) is each strategy unit's own AC (U-CP-86+). The
`SINGLE_THREADED_LINEAR` inline-append regression lives at `test_workflow_driver.py`.

Authority: `Spec_Control_Plane_v1_32.md` §25.11/§25.12/§25.16 +
`Implementation_Plan_Control_Plane_v2_32.md` §3.2 (U-CP-82/U-CP-83).
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_cp import workflow_driver
from harness_cp.cp_shared_types import AgentRole
from harness_cp.gate_level_rule import GateLevel
from harness_cp.workflow_driver import (
    BufferingLedgerWriter,
    LedgerWriterLike,
    _compute_step_idempotency_key,
    bounded_barrier,
    drain_branch_buffers,
)
from harness_cp.workflow_driver_errors import BranchBarrierDeadlineExceededError
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    compose_branch_child_context,
    compose_branch_path,
    compose_branch_step_action_id,
)
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import (
    EntryPayload,
    WriteKey,
    append_ledger_entry,
    read_ledger,
)

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-buffered-append")


def _linear_ctx(
    *,
    parent_action_id: str = "workflow:wf-1:step:3",
    step_index: int = 3,
) -> StepExecutionContext:
    """A per-step context as the SINGLE_THREADED_LINEAR path composes it (no
    branch fields) — the spawning context a branch fan-out descends from."""
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
    """A real `LedgerWriterLike` sink that records append order — the single
    writer the drain serializes through (D1: single-parent linear, no second
    `prior_event_hash`)."""

    def __init__(self, *, actor: Actor) -> None:
        self.actor = actor
        self.appended: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> None:
        self.appended.append((payload, write_key))

    @property
    def is_genesis(self) -> bool:
        return len(self.appended) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appended)


async def _run_branch(
    parent: StepExecutionContext,
    *,
    branch_index: int,
    n_steps: int,
    per_step_delay: float,
    run_idempotency_key: str,
) -> BufferingLedgerWriter:
    """Stand-in for a U-CP-86+ strategy's per-branch control flow.

    Composes a branch child context (the real arc-8 `compose_branch_child_context`),
    then executes `n_steps`, buffering each step's composed
    `(branch-step action_id, branch-scoped idempotency_key)` through a real
    `BufferingLedgerWriter` (D1.b — the write is deferred, not the dispatch).
    `per_step_delay` is the 'model call' latency that scrambles completion order
    across branches. Returns the branch buffer for the orchestrator to drain.
    """
    child = compose_branch_child_context(
        parent, branch_index=branch_index, agent_role=AgentRole("w")
    )
    branch_path = compose_branch_path(child)
    buffer = BufferingLedgerWriter(actor=parent.parent_actor, branch_index=branch_index)
    for local in range(n_steps):
        await asyncio.sleep(per_step_delay)  # the 'model call' — returns out of order
        action_id = compose_branch_step_action_id(child, local)
        # §25.16 key uses the branch-LOCAL step ordinal (`local`) — NOT the
        # inherited spawning `child.step_index` (constant across a branch's
        # steps) — so a branch's distinct steps compose DISTINCT keys and do not
        # collapse under the real IS writer's idempotency_key-only dedup
        # (C-IS-07 §7.5). branch_path keeps sibling branches distinct.
        idempotency_key = _compute_step_idempotency_key(run_idempotency_key, local, branch_path)
        # Buffer a real `EntryPayload` (D1.b deferred append) so the drain's
        # drain-time re-stamp (`payload.model_copy(update={"timestamp": ...})`)
        # exercises the production payload shape. The `timestamp` here is a
        # buffer-time placeholder the drain overrides.
        payload = EntryPayload(
            action_id=Identifier(action_id),
            idempotency_key=Identifier(idempotency_key),
            actor=parent.parent_actor,
            timestamp=datetime.now(UTC),
        )
        buffer.append(payload, idempotency_key)
    return buffer


def _expected_branch_index_order(branch_indices: tuple[int, ...], n_steps: int) -> list[str]:
    """The action_ids in branch-index order (each branch's steps in step order)."""
    return [
        f"workflow:wf-1:step:3:branch:{bi}:step:{local}"
        for bi in sorted(branch_indices)
        for local in range(n_steps)
    ]


# ---------------------------------------------------------------------------
# U-CP-83 — branch-scoped idempotency-key composition (C-CP-25 §25.16)
# ---------------------------------------------------------------------------


def test_linear_idempotency_key_is_byte_identical_regression() -> None:
    """The SINGLE_THREADED_LINEAR path (branch_path=None) composes the existing
    `sha256(run_idempotency_key, step_index)` key BYTE-IDENTICALLY — no extra
    separator is hashed (regression-safe; the live call sites pass no branch_path)."""
    rik, idx = "rik-abc", 3
    h = hashlib.sha256()
    h.update(rik.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(idx).encode("utf-8"))
    expected = h.hexdigest()
    assert _compute_step_idempotency_key(rik, idx) == expected
    assert _compute_step_idempotency_key(rik, idx, None) == expected


def test_sibling_branches_compose_distinct_idempotency_keys_no_collapse() -> None:
    """§25.16: N sibling branches at the SAME declared step_index compose
    DISTINCT idempotency keys via branch_path → no same-step-index collapse under
    the IS writer's idempotency_key-only dedup (C-IS-07 §7.5)."""
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3", step_index=3)
    keys = set()
    for bi in range(4):
        child = compose_branch_child_context(parent, branch_index=bi, agent_role=AgentRole("w"))
        keys.add(_compute_step_idempotency_key("rik", child.step_index, compose_branch_path(child)))
    assert len(keys) == 4  # all distinct — no collapse


def test_same_branch_distinct_steps_compose_distinct_keys() -> None:
    """A single branch's distinct steps compose DISTINCT idempotency keys via the
    branch-LOCAL step ordinal → they do not collapse under the IS writer's
    idempotency_key-only dedup. (Faithfulness regression for the buffered-append
    exercised path: reusing the constant spawning step_index per branch step
    would make a real dedup-ing writer silently drop all but the first step.)"""
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3", step_index=3)
    child = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("w"))
    branch_path = compose_branch_path(child)
    keys = [_compute_step_idempotency_key("rik", local, branch_path) for local in range(3)]
    assert len(set(keys)) == 3  # all distinct — no within-branch collapse


def test_branch_scoped_key_differs_from_linear_key_at_same_step() -> None:
    """A branch-scoped key at step N differs from the linear key at step N — the
    branch entry does not collide with a (hypothetical) linear entry."""
    parent = _linear_ctx(step_index=3)
    child = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("w"))
    linear = _compute_step_idempotency_key("rik", 3)
    branch = _compute_step_idempotency_key("rik", 3, compose_branch_path(child))
    assert linear != branch


def test_compose_branch_path_shape_from_identity() -> None:
    """branch_path derives from (parent_action_id, branch_index)."""
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3")
    child = compose_branch_child_context(parent, branch_index=2, agent_role=AgentRole("w"))
    assert compose_branch_path(child) == "workflow:wf-1:step:3:2"


# ---------------------------------------------------------------------------
# U-CP-82 — branch-step action_id (honors the U-CP-81 forward obligation)
# ---------------------------------------------------------------------------


def test_branch_step_action_id_matches_pinned_shape() -> None:
    """The branch-step action_id is NOT the flat workflow:{wf}:step:{N} shape
    (the U-CP-81 forward obligation) and matches the shape the arc-8 substrate
    test pinned: workflow:wf-1:step:3:branch:0:step:7."""
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3", step_index=3)
    child = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("w"))
    assert compose_branch_step_action_id(child, 7) == "workflow:wf-1:step:3:branch:0:step:7"


def test_branch_step_action_ids_globally_unique_across_siblings() -> None:
    """Two sibling branches at the same local step compose DISTINCT action_ids
    (the IS §5 global-uniqueness invariant the flat shape would violate)."""
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3", step_index=3)
    b0 = compose_branch_child_context(parent, branch_index=0, agent_role=AgentRole("w"))
    b1 = compose_branch_child_context(parent, branch_index=1, agent_role=AgentRole("w"))
    assert compose_branch_step_action_id(b0, 0) != compose_branch_step_action_id(b1, 0)


def test_branch_composers_reject_linear_context() -> None:
    """compose_branch_step_action_id / compose_branch_path are branch-only — a
    linear (SINGLE_THREADED_LINEAR) context is a caller error, not a silent
    fall-through to the flat shape."""
    linear = _linear_ctx()
    assert linear.branch_index is None
    with pytest.raises(ValueError, match="branch"):
        compose_branch_step_action_id(linear, 0)
    with pytest.raises(ValueError, match="branch"):
        compose_branch_path(linear)


# ---------------------------------------------------------------------------
# U-CP-82 — BufferingLedgerWriter (buffers, does not write through)
# ---------------------------------------------------------------------------


def test_buffering_writer_satisfies_ledger_writer_like() -> None:
    """The buffer is a structural LedgerWriterLike (the same Protocol the driver
    consumes) so a branch's ctx.ledger_writer swaps to it with no entry-
    construction change."""
    writer = BufferingLedgerWriter(actor=_ACTOR, branch_index=0)
    assert isinstance(writer, LedgerWriterLike)


def test_buffering_writer_buffers_instead_of_writing_through() -> None:
    """append() buffers (D1.b); the entries are retrievable in step order; the
    write is deferred to the drain."""
    writer = BufferingLedgerWriter(actor=_ACTOR, branch_index=2)
    assert writer.is_genesis is True
    assert writer.entry_count == 0
    writer.append("a", "k1")
    writer.append("b", "k2")
    assert writer.entry_count == 2
    assert writer.is_genesis is False
    assert writer.buffered_entries == [("a", "k1"), ("b", "k2")]
    assert writer.branch_index == 2


# ---------------------------------------------------------------------------
# U-CP-82 — deterministic branch-index-ordered drain (the functional AC)
# ---------------------------------------------------------------------------


async def test_branches_join_at_bounded_barrier_then_drain_in_branch_index_order() -> None:
    """U-CP-82 functional AC: branches run concurrently and persist in BRANCH-
    INDEX order at the barrier drain, NOT completion order — even when the
    barrier input order is deliberately not branch-index order. The single real
    writer is the sole drain sink (D1 single-parent linear)."""
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3", step_index=3)
    real = _RecordingWriter(actor=_ACTOR)
    # Input order to the barrier is (2, 0, 1) — NOT branch-index order — and the
    # fastest branch (2) is listed first; the drain must still persist 0,1,2.
    buffers = await bounded_barrier(
        [
            _run_branch(
                parent, branch_index=2, n_steps=2, per_step_delay=0.004, run_idempotency_key="rik"
            ),
            _run_branch(
                parent, branch_index=0, n_steps=2, per_step_delay=0.020, run_idempotency_key="rik"
            ),
            _run_branch(
                parent, branch_index=1, n_steps=2, per_step_delay=0.012, run_idempotency_key="rik"
            ),
        ],
        deadline_seconds=5.0,
    )
    drained = drain_branch_buffers(real, buffers)
    assert drained == 6
    persisted = [str(payload.action_id) for payload, _key in real.appended]
    assert persisted == _expected_branch_index_order((0, 1, 2), n_steps=2)


async def test_drain_order_is_invariant_under_completion_order() -> None:
    """The determinism boundary (§25.12.2): the persisted order is a PURE
    function of branch_index — identical for the real (scrambled) completion
    order, its reverse, and the sorted order. 'first to finish wins' is
    forbidden."""
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3", step_index=3)
    coros = [
        _run_branch(
            parent, branch_index=bi, n_steps=2, per_step_delay=delay, run_idempotency_key="rik"
        )
        for bi, delay in ((0, 0.020), (1, 0.012), (2, 0.004))
    ]
    # Collect in REAL completion order (as_completed yields fastest-first).
    completed = [await fut for fut in asyncio.as_completed(coros)]
    expected = _expected_branch_index_order((0, 1, 2), n_steps=2)
    for ordering in (
        completed,
        list(reversed(completed)),
        sorted(completed, key=lambda b: b.branch_index),
    ):
        real = _RecordingWriter(actor=_ACTOR)
        drain_branch_buffers(real, ordering)
        assert [str(payload.action_id) for payload, _key in real.appended] == expected


# ---------------------------------------------------------------------------
# U-CP-82 — bounded barriers (§25.11)
# ---------------------------------------------------------------------------


async def test_bounded_barrier_joins_fast_branches_within_deadline() -> None:
    """All branches finishing within the deadline join cleanly; results return in
    input (branch) order."""

    async def fast(value: int) -> int:
        await asyncio.sleep(0.001)
        return value

    results = await bounded_barrier([fast(0), fast(1), fast(2)], deadline_seconds=2.0)
    assert results == [0, 1, 2]


async def test_bounded_barrier_raises_on_stuck_branch_does_not_strand() -> None:
    """A stuck branch hitting the barrier deadline raises
    BranchBarrierDeadlineExceededError rather than stranding the parent
    indefinitely (§25.11 bounded barriers)."""

    async def stuck() -> str:
        await asyncio.sleep(10)
        return "never"

    async def ok() -> str:
        return "ok"

    with pytest.raises(BranchBarrierDeadlineExceededError) as exc_info:
        await bounded_barrier([ok(), stuck()], deadline_seconds=0.05)
    assert exc_info.value.deadline_seconds == 0.05


async def test_bounded_barrier_cancels_pending_siblings_when_a_branch_raises() -> None:
    """Leak-freedom (§25.15.2 obligation 8): when a branch raises before the
    deadline, the still-pending sibling tasks are cancelled + awaited — no
    orphaned branch keeps dispatching effects past the barrier — and the branch
    exception propagates UNCHANGED (the run-level cascade reaction is U-CP-85,
    not this primitive)."""
    sibling_cancelled = False

    async def sibling() -> str:
        nonlocal sibling_cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            sibling_cancelled = True
            raise
        return "never"

    async def failing() -> str:
        await asyncio.sleep(0.01)  # let the sibling enter its sleep first
        raise RuntimeError("branch boom")

    with pytest.raises(RuntimeError, match="branch boom"):
        await bounded_barrier([sibling(), failing()], deadline_seconds=5.0)
    assert sibling_cancelled is True  # not left dispatching in the background


async def test_bounded_barrier_preserves_branch_local_timeout_error() -> None:
    """A branch raising TimeoutError ON ITS OWN (e.g. a provider client timeout)
    before the barrier deadline propagates UNCHANGED — it is NOT misclassified as
    the barrier deadline (BranchBarrierDeadlineExceededError). The asyncio.timeout
    context's .expired() disambiguates the two."""

    async def branch_timeout() -> str:
        await asyncio.sleep(0.01)
        raise TimeoutError("provider client timeout inside the branch")

    async def ok() -> str:
        await asyncio.sleep(0.02)
        return "ok"

    with pytest.raises(TimeoutError) as exc_info:
        await bounded_barrier([branch_timeout(), ok()], deadline_seconds=5.0)
    # The raw branch TimeoutError — NOT the barrier-deadline error subclass.
    assert not isinstance(exc_info.value, BranchBarrierDeadlineExceededError)
    assert "provider client timeout" in str(exc_info.value)


# ---------------------------------------------------------------------------
# CONCURRENT-sibling-drain timestamp gap (xfail — the runtime concurrency fork)
# ---------------------------------------------------------------------------
#
# `drain_branch_buffers` re-stamps each buffered entry to a `drain_timestamp`
# captured ONCE at drain entry — OUTSIDE the IS writer's `_WRITE_LOCK`. For a
# SINGLE drain (the only path reachable today) physical-append-order ==
# timestamp-order. But two CONCURRENT sibling drains (two `SUB_AGENT_DISPATCH`
# children on separate fan-out threads, each draining into the ONE shared real
# writer) each capture their OWN `drain_timestamp` outside the lock; the lock can
# then serialize their physical appends in capture-OPPOSITE order, so a drain that
# captured the EARLIER timestamp physically appends AFTER one that captured a
# later timestamp → the zero-tolerance writer rejects it (`NonMonotonicTimestamp
# Error`). Found independently by BOTH decorrelated reviewers (codex P1 +
# adversarial F1-01). Unreachable today (the runtime sync/async-bridge deadlock
# blocks concurrent sub-agent recursion end-to-end) and EQUALLY broken under the
# prior fan-out-start-timestamp policy (NOT a U-CP-89 regression). The clean fix
# is timestamp-authority INSIDE `_WRITE_LOCK` (an IS write-path change, contract-
# touching) belonging to the same arc as the deadlock; see
# `.harness/runtime_defect_sub_agent_inference_child_loop_bridge_deadlock.md` §8.


class _SequencedClock:
    """Substitute for `workflow_driver.datetime`: `.now(tz)` returns strictly-
    increasing real UTC datetimes, one per call (call N → base + N seconds), so
    the FIRST drain to capture its `drain_timestamp` gets a strictly-EARLIER value
    than the SECOND — removing the wall-clock race so the inversion is
    deterministic, not timing-dependent."""

    _base = datetime(2026, 1, 1, tzinfo=UTC)

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def now(self, tz: object = None) -> datetime:
        with self._lock:
            ts = self._base + timedelta(seconds=self._n)
            self._n += 1
            return ts


class _InterleavingRealWriter:
    """Wraps the REAL `append_ledger_entry` (real dedup + zero-tolerance
    monotonicity check + hash-chain + JSONL persistence) and deterministically
    interleaves two concurrent sibling drains: the FIRST drain to reach `append`
    is PARKED until the SECOND has physically appended, so the first-capturing
    drain (earlier `drain_timestamp`) physically appends SECOND. This is the
    capture-opposite-order serialization the real `_WRITE_LOCK` permits because
    `drain_branch_buffers` captures `drain_timestamp` OUTSIDE that lock. When the
    IS-write-path fix lands (timestamp-authority inside `_WRITE_LOCK`), the wrapped
    real `append_ledger_entry` assigns the timestamp in physical-append order →
    no inversion → this test flips to XPASS (the strict-xfail signal)."""

    def __init__(self, *, handle: JsonlLedgerHandle) -> None:
        self._handle = handle
        self._arrivals = 0
        self._arrival_lock = threading.Lock()
        self.first_parked = threading.Event()
        self.second_appended = threading.Event()

    def append(self, payload: Any, write_key: Any) -> None:
        with self._arrival_lock:
            self._arrivals += 1
            mine = self._arrivals
        if mine == 1:
            # Drain A: announce arrival (it has already captured its EARLIER
            # drain_timestamp), park until B physically appends, THEN append 2nd.
            self.first_parked.set()
            assert self.second_appended.wait(timeout=5.0), "sibling B never appended"
            append_ledger_entry(self._handle, payload, write_key)
        else:
            # Drain B: append 1st (LATER timestamp), then release A.
            append_ledger_entry(self._handle, payload, write_key)
            self.second_appended.set()


def _one_entry_buffer(
    parent: StepExecutionContext, *, branch_index: int, run_idempotency_key: str = "rik"
) -> BufferingLedgerWriter:
    """A branch buffer holding one real `EntryPayload` (the drain re-stamps its
    buffer-time placeholder timestamp). Sibling branches compose DISTINCT
    action_ids + idempotency keys (no IDEMPOTENT_NOOP collapse)."""
    child = compose_branch_child_context(
        parent, branch_index=branch_index, agent_role=AgentRole("w")
    )
    branch_path = compose_branch_path(child)
    buffer = BufferingLedgerWriter(actor=parent.parent_actor, branch_index=branch_index)
    idempotency_key = _compute_step_idempotency_key(run_idempotency_key, 0, branch_path)
    payload = EntryPayload(
        action_id=Identifier(compose_branch_step_action_id(child, 0)),
        idempotency_key=Identifier(idempotency_key),
        actor=parent.parent_actor,
        timestamp=datetime.now(UTC),  # buffer-time placeholder; the drain overrides
    )
    # A real WriteKey (as production's append_branch_step_ledger_entry composes) so
    # the wrapped real append_ledger_entry exercises its true write contract.
    write_key = WriteKey(
        thread_id=Identifier(child.workflow_id),
        step_id=Identifier(f"{branch_index}:0"),
        idempotency_key=Identifier(idempotency_key),
    )
    buffer.append(payload, write_key)
    return buffer


@pytest.mark.xfail(
    strict=True,
    reason=(
        "concurrent-sibling-drain timestamp inversion: drain_branch_buffers "
        "captures drain_timestamp OUTSIDE the IS writer's _WRITE_LOCK, so two "
        "concurrent sibling drains can serialize their physical appends in "
        "capture-opposite order → NonMonotonicTimestampError. Found by BOTH "
        "decorrelated reviewers (codex P1 + adversarial F1-01). Unreachable today "
        "behind the runtime sync/async-bridge deadlock; clean fix is timestamp-"
        "authority inside _WRITE_LOCK (IS write-path change, same arc as the "
        "deadlock fork). Flips to XPASS when that fix lands — see "
        ".harness/runtime_defect_sub_agent_inference_child_loop_bridge_deadlock.md §8."
    ),
)
def test_concurrent_sibling_drains_invert_timestamp(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent sibling drains into the ONE shared REAL writer must keep the
    ledger non-decreasing (the CORRECT behavior this asserts). They currently do
    NOT: the first-capturing drain (earlier drain_timestamp) is forced to append
    SECOND, tripping the zero-tolerance monotonicity check — the gap both
    reviewers flagged. Deterministic via `_SequencedClock` (ordered captures) +
    `_InterleavingRealWriter` (capture-opposite physical-append order)."""
    monkeypatch.setattr(workflow_driver, "datetime", _SequencedClock())
    handle = JsonlLedgerHandle(
        canonical_path=tmp_path / "ledger.jsonl", exists=False, entry_count=0
    )
    writer = _InterleavingRealWriter(handle=handle)
    parent = _linear_ctx(parent_action_id="workflow:wf-1:step:3", step_index=3)
    buffer_a = _one_entry_buffer(parent, branch_index=0)
    buffer_b = _one_entry_buffer(parent, branch_index=1)

    errors: list[BaseException] = []

    def _drain(buf: BufferingLedgerWriter) -> None:
        try:
            drain_branch_buffers(writer, [buf])
        except BaseException as exc:  # record any drain failure for the assert
            errors.append(exc)

    thread_a = threading.Thread(target=_drain, args=(buffer_a,))
    thread_b = threading.Thread(target=_drain, args=(buffer_b,))
    thread_a.start()
    assert writer.first_parked.wait(timeout=5.0), "drain A never reached its append"
    thread_b.start()  # B captures its LATER drain_timestamp now (after A parked)
    thread_b.join(timeout=5.0)
    thread_a.join(timeout=5.0)

    # CORRECT behavior (the xfail target): no monotonicity error, both persisted.
    assert errors == []
    assert len(read_ledger(handle)) == 2
