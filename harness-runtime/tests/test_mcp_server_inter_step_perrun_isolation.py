"""B-INTERSTEP-PERRUN-ISOLATION — per-run ContextVar isolation of the run-scoped
inter-step output channel + cost accumulator (runtime spec §14.21 C-RT-34
invariant 7; B-INTERSTEP fork §3/§5).

SUPERSEDES the B-INTERSTEP per-loop single-flight lock. Daemon-client mode
(U-RT-108) reuses ONE bootstrapped `HarnessContext` across many `run_workflow`
invocations; B-INTERSTEP bounded the no-cross-run-leak invariant to (a) sequential
reuse (per-run `reset()`) + (b) concurrent-within-timeout (a single-flight lock),
with (c) the `drain_timeout_seconds` TIMEOUT-ZOMBIE explicitly NOT closed — a
timed-out run's non-cancellable `asyncio.to_thread` worker keeps writing the
SHARED holder after the lock releases. This arc closes all three by scoping each
holder per run via a `ContextVar`: the `run_workflow` handler sets a fresh holder
per run, which propagates into the `to_thread` worker via `contextvars.copy_
context()`, so a zombie writes only the holder captured in ITS context copy.

The deliverable is the data isolation, not the proxy object — so each test pairs
the isolated case with a NEGATIVE CONTROL that reproduces the cross-run
corruption WITHOUT isolation (a shared holder), proving the mechanism is
load-bearing (not riding on incidental scheduling). The timeout-zombie tests
exercise the REAL `asyncio.to_thread` copy_context propagation that closes
invariant 7c.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from harness_runtime.lifecycle.inter_step_output_channel import (
    INTER_STEP_CHANNEL_VAR,
    InterStepOutputChannel,
    RunScopedInterStepOutputChannel,
)
from harness_runtime.types import (
    COST_ACCUM_VAR,
    CostRecordAccumulator,
    RunScopedCostRecordAccumulator,
)

# ---------------------------------------------------------------------------
# Proxy resolution — the ctx-bound proxy delegates to the current run's holder
# (the var), falling back to its bootstrap default when no run is active.
# ---------------------------------------------------------------------------


def test_channel_proxy_resolves_contextvar_then_falls_back_to_default() -> None:
    proxy = RunScopedInterStepOutputChannel()
    # No run active → falls back to the bound bootstrap default (empty).
    assert proxy.most_recent_output() is None
    assert len(proxy) == 0

    per_run = InterStepOutputChannel()
    token = INTER_STEP_CHANNEL_VAR.set(per_run)
    try:
        proxy.record("s1", {"v": 1})
        # The proxy wrote the run-scoped channel, not its default.
        assert per_run.most_recent_output() == {"v": 1}
        assert proxy.most_recent_output() == {"v": 1}
        assert len(proxy) == 1
    finally:
        INTER_STEP_CHANNEL_VAR.reset(token)

    # After the run, the proxy resolves the default again — the per-run write did
    # NOT leak into the default.
    assert proxy.most_recent_output() is None


def test_cost_proxy_resolves_contextvar_then_falls_back_to_default() -> None:
    proxy = RunScopedCostRecordAccumulator()
    assert proxy.records == []

    per_run = CostRecordAccumulator()
    token = COST_ACCUM_VAR.set(per_run)
    try:
        # `.append` (used as the sink) routes to the run-scoped accumulator.
        proxy.append("rec-1")  # type: ignore[arg-type]  # opaque sentinel for the test
        assert per_run.records == ["rec-1"]
        # `.records` (read by `_build_run_result`) resolves the same list.
        assert proxy.records == ["rec-1"]
    finally:
        COST_ACCUM_VAR.reset(token)

    assert proxy.records == []


# ---------------------------------------------------------------------------
# Concurrent per-run isolation — two overlapping runs, each with its own holder
# in the var, see ONLY their own data WITHOUT any serialization lock.
# ---------------------------------------------------------------------------


async def _channel_run(token: str, *, isolated: bool, shared: InterStepOutputChannel) -> Any:
    """One run: (when isolated) set a fresh per-run channel in the var; record;
    yield so a concurrent run can interleave; read back the upstream output."""
    if isolated:
        INTER_STEP_CHANNEL_VAR.set(InterStepOutputChannel())
        chan = INTER_STEP_CHANNEL_VAR.get()
        assert chan is not None
    else:
        chan = shared  # negative control: both runs share one channel
    chan.record(token, {"owner": token})
    await asyncio.sleep(0)  # the exact interleave window
    return chan.most_recent_output()


@pytest.mark.asyncio
async def test_concurrent_runs_each_isolated_channel_see_only_own() -> None:
    """Two overlapping runs each set their own per-run channel in the var (each in
    its own asyncio task → own context copy). No lock, yet each reads back ONLY
    its own output."""
    shared = InterStepOutputChannel()  # unused on the isolated path
    results = await asyncio.gather(
        asyncio.create_task(_channel_run("run-A", isolated=True, shared=shared)),
        asyncio.create_task(_channel_run("run-B", isolated=True, shared=shared)),
    )
    assert results[0] == {"owner": "run-A"}
    assert results[1] == {"owner": "run-B"}


@pytest.mark.asyncio
async def test_negative_control_shared_channel_concurrent_overlap_corrupts() -> None:
    """The load-bearing control: WITHOUT per-run isolation (one shared channel,
    the pre-arc bootstrap-shared exposure) the same overlap corrupts — the
    first-scheduled run's read sees the second's output. Proves the isolation in
    the test above is doing real work."""
    shared = InterStepOutputChannel()
    results = await asyncio.gather(
        asyncio.create_task(_channel_run("run-A", isolated=False, shared=shared)),
        asyncio.create_task(_channel_run("run-B", isolated=False, shared=shared)),
    )
    assert {results[0]["owner"], results[1]["owner"]} == {"run-B"}, (
        f"expected the shared-channel overlap to corrupt to the latest owner, got "
        f"{results!r} — if it no longer corrupts, re-derive the interleave window."
    )


# ---------------------------------------------------------------------------
# Timeout-zombie (invariant 7c) — the registered residual B-INTERSTEP could NOT
# close with a lock. A timed-out run's non-cancellable `to_thread` worker keeps
# recording; with per-run isolation it writes only ITS OWN (captured) channel,
# never a following run's. Exercises real `asyncio.to_thread` copy_context.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_zombie_writes_only_its_own_per_run_channel() -> None:
    """Run A: a fresh per-run channel in the var + a `to_thread` worker that
    records, blocks, then records a LATE (zombie) entry. Run A times out while the
    worker is blocked. Run B then establishes its OWN per-run channel and records.
    Releasing A's zombie records into channel A (captured in the worker's context
    copy) — NOT channel B. Closes invariant 7c."""
    late_gate = threading.Event()

    def worker_a() -> None:
        chan = INTER_STEP_CHANNEL_VAR.get()  # the var value copied into this thread
        assert chan is not None
        chan.record("A-early", {"owner": "A"})
        late_gate.wait(timeout=5.0)
        chan.record("A-late-zombie", {"owner": "A-zombie"})  # the zombie write

    async def run_a() -> InterStepOutputChannel:
        INTER_STEP_CHANNEL_VAR.set(InterStepOutputChannel())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.to_thread(worker_a), timeout=0.05)
        chan = INTER_STEP_CHANNEL_VAR.get()
        assert chan is not None
        return chan

    # Each run in its own task → own context copy (mirrors distinct run_workflow
    # invocations on the reused ctx).
    chan_a = await asyncio.create_task(run_a())  # times out; worker still blocked

    async def run_b() -> InterStepOutputChannel:
        INTER_STEP_CHANNEL_VAR.set(InterStepOutputChannel())
        chan = INTER_STEP_CHANNEL_VAR.get()
        assert chan is not None
        chan.record("B", {"owner": "B"})
        return chan

    chan_b = await asyncio.create_task(run_b())

    # Release A's zombie; it records its LATE entry now (after B recorded).
    late_gate.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(chan_a) == 2:
            break

    assert chan_a is not chan_b
    # The zombie wrote channel A (captured), proving it really ran late...
    assert chan_a.outputs_by_step_id().get("A-late-zombie") == {"owner": "A-zombie"}
    # ...and channel B is UNcorrupted: B's read still sees only B.
    assert chan_b.most_recent_output() == {"owner": "B"}
    assert "A-late-zombie" not in chan_b.outputs_by_step_id()


@pytest.mark.asyncio
async def test_negative_control_shared_channel_timeout_zombie_corrupts() -> None:
    """The control for invariant 7c: with ONE shared channel (the pre-arc
    exposure), A's timed-out zombie keeps writing the shared channel, so a
    following B read is corrupted by A's late write — exactly what a released lock
    could not fence."""
    shared = InterStepOutputChannel()
    late_gate = threading.Event()

    def worker_a() -> None:
        shared.record("A-early", {"owner": "A"})
        late_gate.wait(timeout=5.0)
        shared.record("A-late-zombie", {"owner": "A-zombie"})

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.to_thread(worker_a), timeout=0.05)

    # Run B uses the SAME shared channel; records + reads while A's zombie blocks.
    shared.record("B", {"owner": "B"})
    assert shared.most_recent_output() == {"owner": "B"}

    # Release A's zombie — its late write now corrupts the shared channel.
    late_gate.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if shared.most_recent_output() == {"owner": "A-zombie"}:
            break
    assert shared.most_recent_output() == {"owner": "A-zombie"}, (
        "expected the shared-channel zombie to corrupt the following run's read"
    )


# ---------------------------------------------------------------------------
# Cost accumulator per-run isolation (§82 Class-3 — the IDENTICAL exposure).
# ---------------------------------------------------------------------------


async def _cost_run(tokens: list[str], *, isolated: bool, shared: CostRecordAccumulator) -> Any:
    if isolated:
        COST_ACCUM_VAR.set(CostRecordAccumulator())
        acc = COST_ACCUM_VAR.get()
        assert acc is not None
    else:
        acc = shared
    for tok in tokens:
        acc.append(tok)  # type: ignore[arg-type]  # opaque sentinel for the test
        await asyncio.sleep(0)
    return list(acc.records)


@pytest.mark.asyncio
async def test_concurrent_runs_each_isolated_cost_accumulator() -> None:
    """Two overlapping runs each accumulate ONLY their own cost records — no
    cross-run mixing (the daemon-reuse wrong-rollup hazard)."""
    shared = CostRecordAccumulator()
    results = await asyncio.gather(
        asyncio.create_task(_cost_run(["A1", "A2"], isolated=True, shared=shared)),
        asyncio.create_task(_cost_run(["B1", "B2"], isolated=True, shared=shared)),
    )
    assert results[0] == ["A1", "A2"]
    assert results[1] == ["B1", "B2"]


@pytest.mark.asyncio
async def test_negative_control_shared_accumulator_mixes_across_runs() -> None:
    """The control: a shared accumulator (pre-arc bootstrap exposure) interleaves
    both runs' records → a wrong per-run rollup."""
    shared = CostRecordAccumulator()
    results = await asyncio.gather(
        asyncio.create_task(_cost_run(["A1", "A2"], isolated=False, shared=shared)),
        asyncio.create_task(_cost_run(["B1", "B2"], isolated=False, shared=shared)),
    )
    # Each run reads back the SHARED list — which contains BOTH runs' records
    # (cross-run contamination), not just its own.
    assert set(results[0]) == {"A1", "A2", "B1", "B2"}
    assert sorted(shared.records) == ["A1", "A2", "B1", "B2"]


# ---------------------------------------------------------------------------
# Handler reset discipline (Codex pre-merge [P2]) — the `run_workflow` handler
# must RESET the holder vars it SELF-set in its `finally`, so a later invocation
# on the SAME asyncio task does not skip its fresh allocation and share a sink.
# Tested in the codebase's established "handler-simulant + finally-reset +
# post-condition None" style (cf. test_lifecycle_mcp_server.py
# ::test_concurrent_set_current_tool_ctx_is_task_isolated, which tests the
# sibling `_CURRENT_TOOL_CTX` binding the same way — NOT through the full FastMCP
# tool, since the daemon-path cost is not observable in the tool result).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_resets_self_set_cost_var_so_sequential_reuse_isolates() -> None:
    """Mirrors the handler's daemon-path cost discipline (no caller-set var):
    set-if-`None` + capture token + reset in `finally`. Two sequential
    invocations on the SAME task each allocate a FRESH accumulator (the reset
    lets the second's `if None` fire) — no cross-run sink sharing — and the var
    is back to `None` after, leaking nothing into later same-task code."""
    seen: list[CostRecordAccumulator] = []

    async def _handler_simulant(tok: str) -> None:
        cost_token = None
        if COST_ACCUM_VAR.get() is None:
            cost_token = COST_ACCUM_VAR.set(CostRecordAccumulator())
        try:
            acc = COST_ACCUM_VAR.get()
            assert acc is not None
            acc.append(tok)  # type: ignore[arg-type]
            seen.append(acc)
        finally:
            if cost_token is not None:
                COST_ACCUM_VAR.reset(cost_token)

    assert COST_ACCUM_VAR.get() is None
    await _handler_simulant("run-1")
    await _handler_simulant("run-2")

    assert len(seen) == 2
    assert seen[0] is not seen[1], "the reset must let the 2nd invocation allocate fresh"
    assert seen[0].records == ["run-1"]
    assert seen[1].records == ["run-2"]
    assert COST_ACCUM_VAR.get() is None, "the handler-set var must not leak past the run"


@pytest.mark.asyncio
async def test_caller_set_cost_var_survives_handler_then_caller_resets() -> None:
    """The api.run/resume path: the CALLER sets the cost var, so the handler's
    `if None` guard is False → the handler does NOT reset it (captured no token);
    the caller's post-run read resolves the SAME accumulator the handler-scope
    appended to, and the CALLER resets it."""
    caller_token = COST_ACCUM_VAR.set(CostRecordAccumulator())
    try:

        async def _handler_simulant() -> None:
            # Handler sees a caller-set var → does NOT set/reset (no token).
            cost_token = None
            if COST_ACCUM_VAR.get() is None:  # False on this path
                cost_token = COST_ACCUM_VAR.set(CostRecordAccumulator())
            try:
                acc = COST_ACCUM_VAR.get()
                assert acc is not None
                acc.append("from-handler")  # type: ignore[arg-type]
            finally:
                if cost_token is not None:
                    COST_ACCUM_VAR.reset(cost_token)

        await _handler_simulant()
        # The caller's accumulator received the handler-scope append (same var).
        caller_acc = COST_ACCUM_VAR.get()
        assert caller_acc is not None
        assert caller_acc.records == ["from-handler"]
    finally:
        COST_ACCUM_VAR.reset(caller_token)
    assert COST_ACCUM_VAR.get() is None
