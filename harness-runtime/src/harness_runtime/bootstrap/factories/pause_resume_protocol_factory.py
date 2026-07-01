"""U-RT-88 — PauseResumeProtocol stage-5 LOOP_INIT factory.

Implements runtime spec v1.21 §14.14.1 factory signature + §14.14.2 per-factory
invocation discipline + §14.14.3 stage-5 LOOP_INIT placement + §14.14.4
failure-mode taxonomy + §14.14.5 invariants.

Narrow-scope CP composer authoring arc (operator-ratified AskUserQuestion
2026-05-24, "driver-invocation-only" scope):

- Opt-out branch (`config.pause_resume_protocol_config is None`) returns `None`
  unconditionally — preserves v1.20 production-default behavior; the
  workflow_driver per-step pre-entry pause-trigger detection branch evaluates
  False.
- Opt-in branch (non-None config) constructs the CP-canonical
  `PauseResumeProtocol` class body from `harness_cp.pause_resume_protocol`,
  bound to `ctx.ledger_writer` + `ctx.ledger_reader` (stage-1 IS prerequisites)
  + a `pause_context_reader` callable composed at the factory invocation site
  per spec §14.14.2 invariant 4.
- MVP `_make_default_pause_context_reader(ctx)` helper provides the
  implementer-discretion composition body at v1.21 Reading A scope per spec
  §14.14.7 — returns a minimal placeholder StateSummary + a constant anchor
  sentinel. The richer state-summary-from-driver-context composition is a
  follow-on arc when the workflow_driver supplies the per-step accumulated
  state to the reader at capture-time.
- Construction failure raises `PauseResumeStageMaterializeError` (fail class
  `RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE`, permanent severity → bootstrap
  rollback per C-RT-02).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from harness_cp.handoff_context import StateSummary
from harness_cp.pause_resume_protocol import PauseContextReader, PauseResumeProtocol
from harness_is.state_ledger_entry_schema import Identifier

from harness_runtime.lifecycle.durable_pause_resume_protocol import (
    DurablePauseResumeProtocol,
)
from harness_runtime.lifecycle.journal_workflow_pause_store import (
    JournalWorkflowPauseStore,
    pause_journal_dir_for,
)
from harness_runtime.types import RuntimeConfig

if TYPE_CHECKING:
    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext


# MVP placeholder anchor sentinel per spec §14.14.7 deferred-discretion.
# Constant across the v1.21 narrow-scope arc — the actual state-ledger anchor
# composition (reading current ledger-head entry_hash via ctx.ledger_reader)
# is a follow-on arc when the LedgerReader exposes a "read_latest" primitive
# beyond the current `read_by_idempotency_key` surface per state_ledger.py:156.
_MVP_PAUSE_ANCHOR_SENTINEL: str = "0" * 64


class PauseResumeStageMaterializeError(Exception):
    """Raised when `materialize_pause_resume_protocol_stage` cannot produce a
    `PauseResumeProtocol` instance.

    Fail class: `RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE` per spec v1.21 §14.14.4.
    Permanent severity — triggers bootstrap rollback of stages 0..4 per
    C-RT-02. Surfaces on opt-in branch only; opt-out branch returns `None`
    unconditionally and cannot raise this class.
    """


def _make_default_pause_context_reader(
    ctx: _MutableHarnessContext,
) -> PauseContextReader:
    """Compose the MVP `PauseContextReader` for the factory invocation site.

    Per spec §14.14.2 invariant 4 + §14.14.7 deferred-discretion: the
    composition body is implementer-discretion at C-RT-24 landing arc. The
    v1.21 Reading A scope MVP returns a minimal placeholder StateSummary +
    a constant anchor sentinel — sufficient for the binding chain to operate
    end-to-end at the narrow-scope arc (the actual state-tracking semantics
    are placeholder; the richer state-summary-from-driver composition lands
    at a follow-on arc).

    The closure captures `ctx` per spec §14.14.7 option (i) closure-over-ctx
    (mirrors the validator-framework-factory + memory-tool-registry-factory
    closure-over-ctx pattern at sibling stage-bucket factories).
    """
    _ = ctx  # MVP placeholder — richer composition body would read
    # ctx.ledger_reader for the current head entry_hash. v1.21 narrow-scope
    # arc defers this per §14.14.7.

    def reader() -> tuple[StateSummary, str]:
        """Return (minimal placeholder StateSummary, constant anchor sentinel).

        v1.21 MVP shape per spec §14.14.7 deferred-discretion. Follow-on arcs
        may compose a richer reader that reads the current ledger head via
        `ctx.ledger_reader` + serializes the workflow-driver-tracked
        accumulated state via a per-workflow current-state-summary provider.
        """
        summary = StateSummary(
            relevant_entries=(),
            summary_text="",
            summary_hash="0" * 64,
            idempotency_key=Identifier(""),
            external_references=(),
        )
        return (summary, _MVP_PAUSE_ANCHOR_SENTINEL)

    return reader


async def materialize_pause_resume_protocol_stage(
    config: RuntimeConfig,
    ctx: _MutableHarnessContext,
    *,
    pause_context_reader: Callable[[], tuple[StateSummary, str]] | None = None,
) -> PauseResumeProtocol | DurablePauseResumeProtocol | None:
    """Construct the stage-5 `PauseResumeProtocol` instance from operator-supplied
    config, or return `None` when the operator has not opted in.

    Per spec v1.21 §14.14.1 + §14.14.2.

    Parameters
    ----------
    config : RuntimeConfig
        Runtime config; `config.pause_resume_protocol_config` is the operator
        opt-in signal.
    ctx : _MutableHarnessContext
        Mutable bootstrap context; `ctx.ledger_writer` + `ctx.ledger_reader`
        (from stage 1 IS) are consumed as PauseResumeProtocol constructor-refs
        per CP spec v1.13 §26.3.
    pause_context_reader : Callable[[], tuple[StateSummary, str]] | None
        Composed at the factory invocation site per spec §14.14.2 invariant 4.
        If `None` (the default), the factory composes the MVP
        `_make_default_pause_context_reader(ctx)` helper per spec §14.14.7
        deferred-discretion (Reading A scope).

    Returns
    -------
    PauseResumeProtocol | None
        `None` when `config.pause_resume_protocol_config is None` — the
        operator has not opted in; `ctx.pause_resume_protocol` binds to
        `None`; the workflow_driver per-step pre-entry pause-trigger
        detection branch evaluates False (the v1.20 production-default
        state preserved per spec §14.14.5 invariant 2).

        Non-`None` when the operator has supplied a `PauseResumeProtocolConfig`
        instance — Reading A scope returns the CP-canonical
        `PauseResumeProtocol` class bound to `ctx.ledger_writer` +
        `ctx.ledger_reader` + the composed `pause_context_reader`. When
        `config.pause_resume_protocol_config.durable` is `True` (runtime spec
        v1.46, R-CC-1 arc #3 cascade step 2), that protocol is wrapped in a
        `DurablePauseResumeProtocol` backed by a `JournalWorkflowPauseStore`
        co-located under the resolved `STATE_LEDGER` directory, so captured
        snapshots survive a process restart for `api.resume(resume_handle=...)`.

    Raises
    ------
    PauseResumeStageMaterializeError
        Fail class `RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE` per spec §14.14.4.
        Triggered when `ctx.ledger_writer` / `ctx.ledger_reader` are not
        populated at invocation (stage-1 IS prerequisite absence) — per spec
        §14.14.2 invariant 3.
    """
    if config.pause_resume_protocol_config is None:
        # Empty-sentinel branch. Operator opted out; ctx.pause_resume_protocol
        # binds to None; driver per-step pre-entry pause-trigger detection
        # branch sibling to drained_flag.is_set() evaluates False. Preserves
        # v1.20 production-default behavior per spec §14.14.5 invariant 2.
        return None

    # Operator opt-in branch. Verify stage-1 IS prerequisites are populated
    # per spec §14.14.2 invariant 3.
    if ctx.ledger_writer is None or ctx.ledger_reader is None:
        raise PauseResumeStageMaterializeError(
            "RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE: "
            "ctx.ledger_writer / ctx.ledger_reader is None at stage 5 — stage 1 "
            "IS did not populate the state-ledger substrate required by "
            "CP spec v1.13 §26.3 PauseResumeProtocol constructor-refs."
        )

    # Compose the default pause_context_reader at the factory invocation site
    # if the caller did not supply one (spec §14.14.2 invariant 4 +
    # §14.14.7 deferred-discretion: implementer-selected composition body).
    reader = (
        pause_context_reader
        if pause_context_reader is not None
        else _make_default_pause_context_reader(ctx)
    )

    # Durable opt-in (runtime spec v1.46 §14.14, R-CC-1 arc #3 cascade step 2):
    # return the DURABLE subclass so captured snapshots persist to a harness-owned
    # journal co-located under the resolved STATE_LEDGER dir (design §7b D2-bis
    # — `<state_ledger_dir>/pause-journal`). The STATE_LEDGER directory is
    # `ctx.ledger_writer.handle.canonical_path.parent` (the parent of the resolved
    # `state.jsonl` file). `DurablePauseResumeProtocol` IS-A `PauseResumeProtocol`,
    # so the frozen HarnessContext + the driver consume it unchanged. Non-durable
    # opt-in returns the bare CP protocol (v1.21 behavior preserved).
    if config.pause_resume_protocol_config.durable:
        state_ledger_dir = ctx.ledger_writer.handle.canonical_path.parent
        store = JournalWorkflowPauseStore(journal_dir=pause_journal_dir_for(state_ledger_dir))
        return DurablePauseResumeProtocol(
            state_ledger_writer=ctx.ledger_writer,
            state_ledger_reader=ctx.ledger_reader,
            pause_context_reader=reader,
            store=store,
        )

    # Construct the CP-canonical PauseResumeProtocol per spec §14.14.5
    # invariant 3 (CP-canonical class satisfaction).
    return PauseResumeProtocol(
        state_ledger_writer=ctx.ledger_writer,
        state_ledger_reader=ctx.ledger_reader,
        pause_context_reader=reader,
    )
