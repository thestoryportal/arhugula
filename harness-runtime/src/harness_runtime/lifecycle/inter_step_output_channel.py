"""Run-scoped inter-step output channel — the shared run-context a dispatcher reads.

R-FS-1 standalone `B-*` arc **B-INTERSTEP** (design-fork-first per X-AL-3;
`.harness/class_1_fork_b_interstep_data_flow.md`; spine ledger
`.harness/beyond-mvp-capability-boundary-ledger.md`). Canonical contract:
runtime spec **§14.21 C-RT-34** (new at v1.59).

Why this exists
---------------
At HEAD the workflow driver threads **no data between steps** for any topology:
the `SINGLE_THREADED_LINEAR` path builds `accumulated` step outputs but never
passes them into a subsequent `dispatch(...)`, and the `StepDispatcher.dispatch`
signature (`binding, step, *, step_context`) carries no prior-output parameter
(`harness_cp.workflow_driver` §25.3.3.4 — the *step body is opaque to the
driver*). So an EVALUATOR_OPTIMIZER evaluator never *sees* the generate draft and
a regenerate never *sees* the evaluator's feedback (the §25.11 "regenerate-with-
feedback" content flow is, at the driver, control-flow only). The
`workflow_driver.py` §25.11 comment names the intended mechanism verbatim:

    Inter-step data flow is ... a runtime/dispatcher concern (**a shared run
    context the dispatcher reads** ...), the SAME for every topology — not a B1
    EO driver concern.

This module is that shared run context.

Mechanism (CA #625 by-reference holder)
---------------------------------------
A plain (NON-Pydantic) holder so `HarnessContext` stores it **by reference**
under `arbitrary_types_allowed` — exactly like `CostRecordAccumulator` /
`asyncio.Event`. A typed field of a known container (e.g. `list` / `dict`) would
be **copied** by Pydantic v2 at `freeze()`, silently disconnecting the driver
(which records onto the mutable builder's holder pre-freeze) from the dispatcher
(which reads the frozen ctx) → the channel would read empty. Arbitrary types are
stored opaquely, so the holder survives `freeze()` as the same object.

The driver `record(...)`s each completed step's output; a dispatcher that needs
upstream context reads `most_recent_output()` (the immediately-prior step's
output — append-ordered, so an EO loop's re-dispatched generate step still reads
the *evaluator's* output, not its own stale prior output). Append-then-read is on
the driver thread for the wired scope (SINGLE_THREADED_LINEAR + EVALUATOR_OPTIMIZER
are sequential per ADR-F2 single-threaded-write), so no lock is needed.

Opt-in / regression-safety
--------------------------
The channel is constructed + bound only when `RuntimeConfig.inter_step_data_flow`
is `True` (stage 5 LOOP_INIT). When `False` (default), `ctx.inter_step_output_
channel is None` → the driver records nothing and the LLM dispatcher injects
nothing → byte-identical to pre-v1.59. (Contrast `CostRecordAccumulator`, which
is always-on because reading it for a cost rollup is additive observability;
inter-step injection *changes* the dispatched payload, so it must be opt-in.)

Registered forward scope (NOT built here — honest, not silent defer)
--------------------------------------------------------------------
- **Concurrent fan-out writes.** The 4 remaining non-linear strategies'
  inter-step recording (PARALLELIZATION siblings, ORCHESTRATOR_WORKERS,
  DECENTRALIZED_HANDOFF, HIERARCHICAL_DELEGATION) — concurrent sibling writes
  need the #648 buffered-branch drain path (ADR-F2). Sequential downstream reads
  (orchestrator synthesis post-join) compose once recording lands.
- **Cross-step resume rehydration.** On a skip-prefix resume the replayed prefix
  is NOT re-dispatched, so a downstream consumer reads an empty channel —
  fresh-run ≠ resumed-run. Closing this needs the output-carrying event-history
  substrate (the `B-ENGINE-OUTPUT-REPLAY` arc; the F2 `EntryPayload` carries only
  a `response_hash` digest today, not the activity output). EO's data flow lives
  *inside* one driver invocation's loop (atomic, no resume boundary crossed), so
  the wired consumer is resume-safe; cross-step linear data flow is fresh-run
  correct and resume-correct only once `B-ENGINE-OUTPUT-REPLAY` lands.
"""

from __future__ import annotations

import contextvars
from collections.abc import Mapping
from typing import Any

__all__ = [
    "INTER_STEP_CHANNEL_VAR",
    "InterStepOutputChannel",
    "RunScopedInterStepOutputChannel",
]


class InterStepOutputChannel:
    """Append-ordered, run-scoped, by-reference inter-step step-output sink.

    Records each completed step's output (a `Mapping`) in driver-execution order.
    A consumer dispatcher reads `most_recent_output()` (the immediately-prior
    step's output) — the minimal genuine inter-step DATA channel.

    Stored by reference on the frozen `HarnessContext` (arbitrary type, never a
    Pydantic-copied container). A fresh channel per run; recorded outputs are
    copied in (`dict(output)`) so a later mutation of the caller's mapping cannot
    retroactively alter a recorded entry.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        # (step_id, output) in driver-execution (append) order. Append order —
        # NOT a step_id→output dict — is load-bearing: an EO loop re-dispatches
        # the same generate `step_id` across iterations, so a dict keyed by
        # step_id would make `most_recent_output()` return the wrong (stale,
        # overwritten-in-place) value on regenerate.
        self._records: list[tuple[str, Mapping[str, Any]]] = []

    def record(self, step_id: str, output: Mapping[str, Any]) -> None:
        """Append a completed step's output (copied defensively)."""
        self._records.append((str(step_id), dict(output)))

    def reset(self) -> None:
        """Clear all recorded outputs — called at the per-run boundary (the
        `run_workflow` tool handler) so a REUSED `HarnessContext` (daemon-client
        mode, U-RT-108, where one bootstrapped ctx serves many `run_workflow`
        invocations) does NOT leak a prior run's step outputs into a later run's
        first dispatch. The channel is run-scoped state on a bootstrap-scoped
        carrier, so the run boundary — not the bootstrap boundary — owns its
        lifecycle (Codex review)."""
        self._records.clear()

    def most_recent_output(self) -> Mapping[str, Any] | None:
        """The most-recently-recorded step output (the upstream output for the
        next dispatch), or `None` if no step has completed yet."""
        if not self._records:
            return None
        return self._records[-1][1]

    def outputs_by_step_id(self) -> Mapping[str, Mapping[str, Any]]:
        """A last-wins, insertion-ordered view of recorded outputs keyed by
        `step_id` (a re-dispatched step's latest output wins). Read-only copy."""
        result: dict[str, Mapping[str, Any]] = {}
        for step_id, output in self._records:
            result[step_id] = output
        return result

    def __len__(self) -> int:
        return len(self._records)


# B-INTERSTEP-PERRUN-ISOLATION (runtime spec §14.21 C-RT-34 invariant 7;
# B-INTERSTEP fork §3/§5) — the per-run channel ContextVar. The frozen
# `HarnessContext` binds a stable `RunScopedInterStepOutputChannel` proxy (opt-in
# only); the proxy resolves the *current run's* channel from this var. The
# `run_workflow` tool handler sets a fresh channel per run, which propagates into
# the `asyncio.to_thread` driver worker via `contextvars.copy_context()` (verified
# empirically: caller-set → handler → worker). Two concurrent `run_workflow`
# invocations on the ONE reused bootstrap `HarnessContext` (daemon-client mode,
# U-RT-108) each run in their own asyncio task → their own context copy → their
# own channel, so they cannot interleave. A run that exceeds `drain_timeout_
# seconds` leaves a non-cancellable `to_thread` zombie, but the zombie writes only
# the channel captured in ITS context copy — never a following run's — so the
# (7b) single-flight lock is no longer needed and the (7c) timeout-zombie is
# closed. Default `None` → the proxy falls back to its bound bootstrap default
# (direct-stage / partial-bootstrap test paths with no active run).
INTER_STEP_CHANNEL_VAR: contextvars.ContextVar[InterStepOutputChannel | None] = (
    contextvars.ContextVar("harness.inter_step_output_channel", default=None)
)


class RunScopedInterStepOutputChannel(InterStepOutputChannel):
    """Stable ctx-bound proxy that resolves the per-run `InterStepOutputChannel`
    from `INTER_STEP_CHANNEL_VAR` at every call (B-INTERSTEP-PERRUN-ISOLATION).

    Bound on the frozen `HarnessContext.inter_step_output_channel` ONLY when
    `RuntimeConfig.inter_step_data_flow` is True (so opt-out stays `None` →
    byte-identical to pre-v1.59). It IS-A `InterStepOutputChannel` (the field type)
    but stores no records itself: every `record` / `most_recent_output` /
    `outputs_by_step_id` / `reset` / `len()` delegates to `_current()` — the
    run-scoped channel in the ContextVar, or a bound bootstrap default when no run
    is active. The LLM dispatcher + CP driver hold this one stable instance and
    transparently read/write the current run's channel.
    """

    __slots__ = ("_default",)

    def __init__(self) -> None:
        # Deliberately NOT calling super().__init__() — the inherited `_records`
        # slot is never used (all access delegates). The bootstrap default is a
        # real channel used only when no per-run channel is bound in the var.
        self._default = InterStepOutputChannel()

    def _current(self) -> InterStepOutputChannel:
        current = INTER_STEP_CHANNEL_VAR.get()
        return current if current is not None else self._default

    def record(self, step_id: str, output: Mapping[str, Any]) -> None:
        self._current().record(step_id, output)

    def reset(self) -> None:
        self._current().reset()

    def most_recent_output(self) -> Mapping[str, Any] | None:
        return self._current().most_recent_output()

    def outputs_by_step_id(self) -> Mapping[str, Mapping[str, Any]]:
        return self._current().outputs_by_step_id()

    def __len__(self) -> int:
        return len(self._current())
