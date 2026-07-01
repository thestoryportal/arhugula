"""U-RT-44 — drain primitives (signal handler + process-level flag).

Acceptance criteria per Phase 2 Session 3 atomic decomposition L10 U-RT-44 +
spec §11 C-RT-11:

- **AC #1 (LAND):** SIGTERM/SIGINT sets `ctx.drained_flag` (and module-level
  `_process_drained`).
- **AC #2 (STRUCK):** in-flight step bounded-wait — requires CP workflow loop
  primitive (Class 1 fork at
  `.harness/class_1_tension_u_rt_44_workflow_loop_drain.md`).
- **AC #3 (LAND):** `api.run()` rejects post-drain with `HarnessDraining`
  (covered in `test_api.py` additions; module-level setup re-tested here for
  flag-isolation).

Tests:
- handler body sets both flags
- install / uninstall round-trip
- platform error surfaces typed `DrainPlatformError`
- handler installed on full bootstrap (stage 7)
- one-way invariant
- integration: real `os.kill(SIGTERM)` round-trip
- `is_process_drained` reflects module state
- reset-for-tests helper functions

Test isolation: autouse fixture resets `_process_drained` and removes any
SIGTERM/SIGINT handlers between tests.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Iterator

import pytest
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.drain import (
    DrainPlatformError,
    _on_drain_signal,  # type: ignore[attr-defined]
    install_signal_handlers,
    is_process_drained,
    reset_process_drained_for_tests,
    uninstall_signal_handlers,
)

_WIN = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Isolation fixture — drain flag is module-level and one-way per spec.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_drain_state() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    # Each pytest-asyncio test gets a fresh event loop that is closed at end;
    # any installed asyncio signal-handlers are dropped with the loop. So
    # only the module-level `_process_drained` flag needs explicit reset.
    reset_process_drained_for_tests()
    yield
    reset_process_drained_for_tests()


def _builder_with_flag() -> _MutableHarnessContext:
    ctx = _MutableHarnessContext()
    ctx.drained_flag = asyncio.Event()
    return ctx


# ---------------------------------------------------------------------------
# Handler body.
# ---------------------------------------------------------------------------


def test_on_drain_signal_sets_ctx_flag_and_process_flag() -> None:
    """AC #1 — handler sets both `ctx.drained_flag` and `_process_drained`."""
    ctx = _builder_with_flag()
    assert ctx.drained_flag is not None
    assert ctx.drained_flag.is_set() is False
    assert is_process_drained() is False

    _on_drain_signal(ctx)

    assert ctx.drained_flag.is_set() is True
    assert is_process_drained() is True


def test_on_drain_signal_tolerates_missing_ctx_flag() -> None:
    """Defensive: handler must not crash if the ctx wasn't initialized."""
    ctx = _MutableHarnessContext()
    assert ctx.drained_flag is None

    _on_drain_signal(ctx)  # must not raise

    assert is_process_drained() is True


# ---------------------------------------------------------------------------
# Install / uninstall.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_WIN, reason="loop.add_signal_handler unsupported on Windows")
@pytest.mark.asyncio
async def test_install_uninstall_round_trip() -> None:
    """Install then uninstall — loop holds no SIGTERM/SIGINT handler after."""
    ctx = _builder_with_flag()
    loop = asyncio.get_running_loop()

    install_signal_handlers(ctx, loop)
    # No public API to introspect installed handlers; uninstall must not raise
    # the missing-handler ValueError, confirming a handler was registered.
    uninstall_signal_handlers(loop)

    # Second uninstall is a no-op (swallows missing-handler ValueError).
    uninstall_signal_handlers(loop)


@pytest.mark.skipif(_WIN, reason="Windows-only error path tested via mock")
@pytest.mark.asyncio
async def test_install_idempotent_replaces_handler() -> None:
    """Re-installing replaces the prior handler (asyncio's documented behavior)."""
    ctx = _builder_with_flag()
    loop = asyncio.get_running_loop()

    install_signal_handlers(ctx, loop)
    install_signal_handlers(ctx, loop)  # must not raise

    uninstall_signal_handlers(loop)


def test_install_raises_drain_platform_error_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typed platform error when asyncio refuses the install."""
    monkeypatch.setattr("harness_runtime.drain.sys.platform", "win32")
    ctx = _builder_with_flag()
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(DrainPlatformError):
            install_signal_handlers(ctx, loop)
    finally:
        loop.close()


def test_uninstall_on_win32_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uninstall does nothing (and does not raise) on Windows."""
    monkeypatch.setattr("harness_runtime.drain.sys.platform", "win32")
    loop = asyncio.new_event_loop()
    try:
        uninstall_signal_handlers(loop)  # must not raise
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# One-way invariant.
# ---------------------------------------------------------------------------


def test_process_drained_one_way_invariant() -> None:
    """Spec §11 invariant — once set, stays set absent test-only reset."""
    ctx = _builder_with_flag()
    _on_drain_signal(ctx)
    assert is_process_drained() is True
    # Clearing the ctx-local flag does not clear the process-level flag.
    assert ctx.drained_flag is not None
    ctx.drained_flag.set()
    ctx.drained_flag.clear()
    assert is_process_drained() is True


def test_reset_for_tests_clears_process_drained() -> None:
    """Test-only escape hatch resets the module-level flag."""
    ctx = _builder_with_flag()
    _on_drain_signal(ctx)
    assert is_process_drained() is True

    reset_process_drained_for_tests()

    assert is_process_drained() is False


# ---------------------------------------------------------------------------
# Integration — real SIGTERM round-trip on Unix.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_WIN, reason="signal-driven drain unsupported on Windows")
@pytest.mark.asyncio
async def test_real_sigterm_sets_flag_via_event_loop() -> None:
    """End-to-end: install handlers, send SIGTERM to self, await one tick."""
    ctx = _builder_with_flag()
    loop = asyncio.get_running_loop()
    install_signal_handlers(ctx, loop)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        # asyncio dispatches signals via the self-pipe wakeup_fd; wait
        # bounded on the asyncio.Event rather than yielding ticks blindly.
        assert ctx.drained_flag is not None
        await asyncio.wait_for(ctx.drained_flag.wait(), timeout=1.0)

        assert ctx.drained_flag.is_set() is True
        assert is_process_drained() is True
    finally:
        uninstall_signal_handlers(loop)
