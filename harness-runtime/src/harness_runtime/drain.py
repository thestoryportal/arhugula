"""`harness_runtime.drain` — runtime-owned drain primitives (U-RT-44).

Per `Spec_Harness_Runtime_v1.md` v1.1 §11 (C-RT-11 — Drain semantics).

**Full-land at Lane 6 (2026-05-20).** C-RT-11 commits 3 drain surfaces; all
three are LANDED post-Lane-6:

1. ✅ `drained_flag` set by signal handler — landed here.
2. ✅ CP workflow lifecycle loop polls flag at boundaries — landed via
   delegation. `api.run()` delegates execution to `harness_cp.workflow_
   driver.execute_workflow()` (C-CP-25 §25.4 + U-CP-57); the driver polls
   `ctx.drained_flag.is_set()` at driver-entry / per-step-pre-entry /
   per-step-post-step boundaries. In-flight step bounded-wait
   (U-RT-44 AC #2) materializes via the runtime's `asyncio.to_thread`
   composition: signal handler sets flag → driver returns DRAINED at
   next boundary → thread future resolves. Closes
   `[[fork-u-rt-44-workflow-loop-drain]]`.
3. ✅ `harness_runtime.run()` rejects new invocations with `HarnessDraining`
   — landed at `harness_runtime.api`.

**Signal handler installation.** Spec §11 suggests stage 7 INGRESS_ACCEPT as
the install site; this module exposes `install_signal_handlers(ctx, loop)` /
`uninstall_signal_handlers(loop)` and stage 7 calls them. Uses
`loop.add_signal_handler(SIG, callback, ctx)` — asyncio's signal-aware
primitive that thread-safely fires the callback on the next event-loop tick,
allowing the callback to mutate `asyncio.Event` state without signal-handler
restrictions.

**Process-level drain flag.** The spec invariant ("once set, it stays set for
the remaining process lifetime; a new harness invocation requires process
restart") requires state outside any single `HarnessContext` lifetime. Track A
is bootstrap-per-call — each call constructs a fresh ctx with a fresh
`drained_flag`. The module-level `_process_drained` flag survives across
calls; `api.run()` checks it pre-bootstrap and rejects with `HarnessDraining`.

**Second-SIGTERM escalation deferred.** Spec §11 marks this "deferred to
implementation discretion." Not implemented at U-RT-44; tracked at the unit
plan as out-of-scope.

**Platform support.** `loop.add_signal_handler` is not supported on Windows
per asyncio docs. Production target is Linux/macOS per the stack commitment
(`Target_Stack_Commitment_v1.md`). Callers on Windows surface a typed error.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext


__all__ = [
    "DRAIN_SIGNALS",
    "DrainPlatformError",
    "install_signal_handlers",
    "is_process_drained",
    "reset_process_drained_for_tests",
    "uninstall_signal_handlers",
]


# ---------------------------------------------------------------------------
# Typed errors.
# ---------------------------------------------------------------------------


class DrainPlatformError(RuntimeError):
    """Signal-handler installation unsupported on the current platform.

    `loop.add_signal_handler` raises `NotImplementedError` on Windows per
    asyncio docs. The runtime catches and re-raises as this typed surface so
    operator diagnosis is unambiguous.
    """


# ---------------------------------------------------------------------------
# Module-level process-drained flag (C-RT-11 one-way invariant).
# ---------------------------------------------------------------------------


_process_drained: bool = False


def is_process_drained() -> bool:
    """Return True once any SIGTERM/SIGINT has set the process-level drain flag.

    The flag is one-way per spec §11 invariant: once set, it stays set for the
    remaining process lifetime. A new harness invocation requires process
    restart.
    """
    return _process_drained


def reset_process_drained_for_tests() -> None:
    """Reset the module-level flag — test-only escape hatch.

    Pytest fixtures must reset between tests because the spec one-way
    invariant otherwise persists across the test session. Production callers
    must not invoke this; the function name encodes the contract.
    """
    global _process_drained
    _process_drained = False


# ---------------------------------------------------------------------------
# Signals carried by C-RT-11.
# ---------------------------------------------------------------------------


# Tuple form (not a set) so iteration order is stable for install/uninstall.
DRAIN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)


# ---------------------------------------------------------------------------
# Signal handler body.
# ---------------------------------------------------------------------------


def _on_drain_signal(ctx: _MutableHarnessContext) -> None:
    """Fired by asyncio's signal-handler dispatcher on SIGTERM / SIGINT.

    Two side effects:
    1. Set the ctx-local `drained_flag` (asyncio.Event) — the CP workflow
       loop, when it eventually lands, polls this at lifecycle boundaries.
    2. Set the module-level `_process_drained` flag — `api.run()` checks
       this pre-bootstrap to reject new invocations with `HarnessDraining`.

    No I/O, no allocations beyond the .set() / module-write. Runs on the
    event-loop thread (asyncio's signal-handler invariant) so the writes are
    free of signal-handler restrictions.
    """
    global _process_drained
    if ctx.drained_flag is not None:
        ctx.drained_flag.set()
    _process_drained = True


# ---------------------------------------------------------------------------
# Install / uninstall.
# ---------------------------------------------------------------------------


def install_signal_handlers(
    ctx: _MutableHarnessContext,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Install SIGTERM + SIGINT handlers that set `ctx.drained_flag` on fire.

    Called by stage 7 INGRESS_ACCEPT after `_MutableHarnessContext.freeze()`
    succeeds — the spec §11 suggested install site. Idempotent at the
    asyncio level: `loop.add_signal_handler` replaces a prior handler for
    the same signal if one is installed.

    Raises
    ------
    DrainPlatformError
        Platform does not support `loop.add_signal_handler` (Windows).
    """
    if sys.platform == "win32":
        raise DrainPlatformError(
            "signal-driven drain is unsupported on Windows; "
            "loop.add_signal_handler raises NotImplementedError per asyncio docs"
        )

    for sig in DRAIN_SIGNALS:
        try:
            loop.add_signal_handler(sig, _on_drain_signal, ctx)
        except NotImplementedError as exc:
            raise DrainPlatformError(
                f"loop.add_signal_handler({sig.name}) raised NotImplementedError on this platform"
            ) from exc


def uninstall_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Remove SIGTERM + SIGINT handlers from the event loop.

    Called by `_rollback_ingress` for rollback-symmetry. Defensive: in
    practice stage 7 is the last stage and freeze is its last act, so a
    failure that triggers ingress rollback is unreachable. Best-effort —
    swallows the missing-handler `ValueError` that `remove_signal_handler`
    raises when no handler is installed.
    """
    if sys.platform == "win32":
        return
    for sig in DRAIN_SIGNALS:
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, ValueError):
            # NotImplementedError = platform; ValueError = no handler installed.
            # Both are no-ops for uninstall semantics.
            pass
