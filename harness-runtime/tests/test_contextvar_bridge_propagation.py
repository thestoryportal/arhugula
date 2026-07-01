"""Discriminating test — does a ContextVar set in a main-loop task propagate
through `asyncio.to_thread` → `asyncio.run_coroutine_threadsafe` → coroutine
on the main loop?

This test exists to settle whether the per-session ctx isolation refactor
can use a `contextvars.ContextVar` (lightweight) OR must use a session-keyed
dict (heavier; threads session_id through the workflow driver).

The production docstring at `harness_runtime/lifecycle/mcp_server.py:32-40`
claims contextvar propagation across this bridge is "unreliable." CPython
docs suggest both `asyncio.to_thread` (uses `copy_context().run`) and
`run_coroutine_threadsafe` (via `call_soon_threadsafe` → Handle with
`copy_context()`) preserve the context. This test settles the question
empirically. If it passes, the refactor uses ContextVar; if it fails,
the refactor pivots to session-keyed dict.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

_PROBE_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "test.bridge.probe", default=None
)


async def _elicit_simulant() -> str | None:
    """Simulates the HITL elicit callback on the main loop after the
    `run_coroutine_threadsafe` bridge. Reads whatever ContextVar value
    the bridge propagated."""
    return _PROBE_CV.get()


def _worker_thread_sync(loop: asyncio.AbstractEventLoop) -> str | None:
    """Simulates the CP workflow driver running in `asyncio.to_thread`.
    Bridges back to the main loop via `run_coroutine_threadsafe`."""
    fut = asyncio.run_coroutine_threadsafe(_elicit_simulant(), loop)
    return fut.result(timeout=5.0)


async def _tool_handler_simulant(value: str) -> str | None:
    """Simulates the `run_workflow` tool handler frame on the main loop.
    Sets the ContextVar, dispatches through to_thread → run_coroutine_threadsafe."""
    _PROBE_CV.set(value)
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(_worker_thread_sync, loop)


@pytest.mark.asyncio
async def test_contextvar_propagates_through_to_thread_and_run_coro_threadsafe() -> None:
    """Single-task baseline: ContextVar set in the tool handler frame is
    visible to the elicit coroutine after the to_thread + run_coroutine_threadsafe
    bridge."""
    observed = await _tool_handler_simulant("alpha")
    assert observed == "alpha", (
        f"expected 'alpha' but observed {observed!r} — ContextVar did NOT propagate "
        f"through the asyncio.to_thread → run_coroutine_threadsafe bridge"
    )


@pytest.mark.asyncio
async def test_concurrent_tool_handlers_see_isolated_contextvar_values() -> None:
    """Two concurrent tool handler tasks set DIFFERENT ContextVar values and
    each bridges through to_thread + run_coroutine_threadsafe. Assert each
    elicit-simulant sees its OWN task's value, not the other's.

    THIS IS THE DISCRIMINATING TEST. If it passes, ContextVar is sufficient
    for per-session ctx isolation. If it fails (cross-talk between tasks),
    the refactor must use a session-keyed dict instead.
    """
    results = await asyncio.gather(
        _tool_handler_simulant("alpha"),
        _tool_handler_simulant("beta"),
    )
    assert results == ["alpha", "beta"], (
        f"expected ['alpha', 'beta'] but observed {results!r} — concurrent "
        f"tool handlers crossed-talked through the bridge; ContextVar isolation "
        f"failed at the asyncio.to_thread + run_coroutine_threadsafe boundary"
    )


@pytest.mark.asyncio
async def test_many_concurrent_tool_handlers_see_isolated_values() -> None:
    """Stress variant: 20 concurrent tool handler tasks, each with a unique
    value. Asserts isolation holds under contention (catches race conditions
    that a 2-task test might miss by timing accident)."""
    values = [f"task-{i:02d}" for i in range(20)]
    results = await asyncio.gather(*[_tool_handler_simulant(v) for v in values])
    assert results == values, (
        f"isolation broke under contention — submitted {values!r} got back {results!r}"
    )
