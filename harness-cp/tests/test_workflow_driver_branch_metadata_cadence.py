"""Tests for U-CP-84 — `branch_metadata.terminal_status` write-cadence.

Per `Implementation_Plan_Control_Plane_v2_32.md` §3.2 (U-CP-84) + CP spec v1.32
§25.13 + IS spec v1.8 §5.4 + runtime spec v1.48 §2.2(c). U-CP-84 is the CP
**producer**-cadence by which the `WorkflowDriver` populates the IS
`branch_metadata` sidecar (the carrier U-IS-19 authored): per-step branch entries
carry causality-only `branch_metadata` (`terminal_status=None`); a branch's
terminal disposition (`cancelled`/`completed`/`timed_out`) is written at a fresh
terminal entry appended at the barrier drain — append-only, never by mutating a
prior entry.

The load-bearing assertions round-trip real `EntryPayload`s through the REAL IS
`append_ledger_entry` (idempotent dedup + zero-tolerance timestamp monotonicity)
+ `verify_chain` — NOT a recording fake, which would hide (a) a terminal-entry
idempotency-key collision (the arc-9 dedup defect class) and (b) the
determinism-boundary ⟂ IS-monotonicity timestamp interaction.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_cp.cp_shared_types import AgentRole
from harness_cp.gate_level_rule import GateLevel
from harness_cp.workflow_driver import (
    BufferingLedgerWriter,
    _compute_step_idempotency_key,
    append_branch_step_ledger_entry,
    append_branch_terminal_ledger_entry,
    bounded_barrier,
    drain_branch_buffers,
)
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    compose_branch_child_context,
    compose_branch_metadata,
    compose_branch_path,
    compose_branch_step_action_id,
    compose_branch_terminal_action_id,
    compose_branch_terminal_path,
)
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import (
    EntryPayload,
    NonMonotonicTimestampError,
    WriteKey,
    WriteResult,
    append_ledger_entry,
    read_ledger,
)

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-branch-cadence")
_PARENT_ACTION_ID = "workflow:wf-1:step:3"
_SPAWN_STEP_INDEX = 3
_FAN_OUT_TS = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _linear_ctx(
    *,
    parent_action_id: str = _PARENT_ACTION_ID,
    step_index: int = _SPAWN_STEP_INDEX,
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


def _branch(branch_index: int) -> StepExecutionContext:
    return compose_branch_child_context(
        _linear_ctx(), branch_index=branch_index, agent_role=AgentRole("w")
    )


class _RealLedgerWriter:
    """A `LedgerWriterLike` drain sink backed by the REAL IS writer.

    `drain_branch_buffers` forwards each buffered `(payload, write_key)` to
    `.append`, which routes through `append_ledger_entry` — so dedup,
    timestamp-monotonicity, hash-chain construction, and JSONL persistence are all
    exercised (a recording fake exercises none of them). Records each
    `WriteResult` so a silent `IDEMPOTENT_NOOP` drop is observable.
    """

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


def _handle(tmp_path: Path) -> JsonlLedgerHandle:
    return JsonlLedgerHandle(canonical_path=tmp_path / "state.jsonl", exists=False, entry_count=0)


def _expected_action_ids(branch_indices: tuple[int, ...], n_steps: int) -> list[str]:
    """Persisted action_ids in branch-index order: each branch's step entries (in
    step order) followed by its single fresh terminal entry."""
    out: list[str] = []
    for bi in sorted(branch_indices):
        out.extend(f"{_PARENT_ACTION_ID}:branch:{bi}:step:{local}" for local in range(n_steps))
        out.append(f"{_PARENT_ACTION_ID}:branch:{bi}:terminal")
    return out


async def _run_branch(
    *,
    branch_index: int,
    n_steps: int,
    per_step_delay: float,
    run_idempotency_key: str,
    terminal_status: str = "completed",
    fan_out_ts: datetime = _FAN_OUT_TS,
) -> BufferingLedgerWriter:
    """Stand-in for a U-CP-86+ strategy's per-branch control flow: execute
    `n_steps` (buffering each step's causality-only entry) then write the fresh
    terminal entry. `per_step_delay` scrambles completion order across branches;
    `fan_out_ts` is the buffer-time placeholder the drain overrides (monotonicity
    is realized at `drain_branch_buffers`'s re-stamp, not by this value)."""
    child = _branch(branch_index)
    buffer = BufferingLedgerWriter(actor=_ACTOR, branch_index=branch_index)
    for local in range(n_steps):
        await asyncio.sleep(per_step_delay)  # the 'model call' — returns out of order
        append_branch_step_ledger_entry(
            branch_writer=buffer,
            branch_context=child,
            run_idempotency_key=run_idempotency_key,
            local_step_index=local,
            timestamp=fan_out_ts,
        )
    append_branch_terminal_ledger_entry(
        branch_writer=buffer,
        branch_context=child,
        run_idempotency_key=run_idempotency_key,
        terminal_status=terminal_status,  # type: ignore[arg-type]
        timestamp=fan_out_ts,
    )
    return buffer


# ---------------------------------------------------------------------------
# compose_branch_metadata — the causality + disposition carrier composition.
# ---------------------------------------------------------------------------


def test_compose_branch_metadata_causality_only_default() -> None:
    """A per-step branch entry composes causality-only metadata: parent_action_id
    + branch_index from the child context, terminal_status defaulting None."""
    bm = compose_branch_metadata(_branch(2))
    assert bm.parent_action_id == _PARENT_ACTION_ID
    assert bm.branch_index == 2
    assert bm.terminal_status is None


@pytest.mark.parametrize("status", ["cancelled", "completed", "timed_out"])
def test_compose_branch_metadata_terminal_disposition(status: str) -> None:
    """A terminal entry composes the caller-supplied disposition verbatim."""
    bm = compose_branch_metadata(_branch(0), terminal_status=status)  # type: ignore[arg-type]
    assert bm.terminal_status == status


def test_compose_branch_metadata_rejects_linear_context() -> None:
    """The linear (SINGLE_THREADED_LINEAR) path composes NO branch_metadata — a
    linear context is a caller error, not a silent causality-less fall-through."""
    with pytest.raises(ValueError, match="branch composer requires a branch child context"):
        compose_branch_metadata(_linear_ctx())


# ---------------------------------------------------------------------------
# Terminal-marker identity — distinct action_id + distinct idempotency path.
# ---------------------------------------------------------------------------


def test_compose_branch_terminal_action_id_shape() -> None:
    assert compose_branch_terminal_action_id(_branch(0)) == f"{_PARENT_ACTION_ID}:branch:0:terminal"


def test_terminal_action_id_distinct_from_every_step_action_id() -> None:
    """The fresh terminal marker's action_id collides with no per-step branch
    action_id (`:terminal` vs `:step:{int}`) — the entry is genuinely fresh."""
    child = _branch(1)
    terminal = compose_branch_terminal_action_id(child)
    step_ids = {compose_branch_step_action_id(child, local) for local in range(20)}
    assert terminal not in step_ids


def test_terminal_path_distinct_from_step_branch_path() -> None:
    child = _branch(1)
    assert compose_branch_terminal_path(child) == f"{_PARENT_ACTION_ID}:1:terminal"
    assert compose_branch_terminal_path(child) != compose_branch_path(child)


def test_terminal_composers_reject_linear_context() -> None:
    with pytest.raises(ValueError, match="branch composer requires"):
        compose_branch_terminal_action_id(_linear_ctx())
    with pytest.raises(ValueError, match="branch composer requires"):
        compose_branch_terminal_path(_linear_ctx())


def test_terminal_idempotency_key_distinct_from_all_step_keys() -> None:
    """The arc-9 dedup-collision defect class: the terminal entry's idempotency
    key must differ from EVERY step key of the same branch, or the IS dedup would
    drop the terminal entry as an idempotent no-op and the disposition would
    silently vanish."""
    child = _branch(1)
    rik = "rik-distinct"
    step_keys = {
        _compute_step_idempotency_key(rik, local, compose_branch_path(child)) for local in range(20)
    }
    terminal_key = _compute_step_idempotency_key(
        rik, child.step_index, compose_branch_terminal_path(child)
    )
    assert terminal_key not in step_keys


def _nested_branch() -> StepExecutionContext:
    """A LEVEL-2 branch whose spawning context's action_id is itself a level-1
    branch-step action_id — i.e. a fan-out spawned from inside another branch
    (HIERARCHICAL_DELEGATION recursion, U-CP-89)."""
    level1 = _branch(0)  # parent_action_id = workflow:wf-1:step:3
    inner_spawn = compose_branch_step_action_id(level1, 7)  # ...:branch:0:step:7
    inner_ctx = level1.model_copy(
        update={"parent_action_id": inner_spawn, "branch_index": None, "agent_role": None}
    )
    return compose_branch_child_context(inner_ctx, branch_index=2, agent_role=AgentRole("w2"))


def test_nested_fan_out_action_ids_and_keys_unique_at_every_depth() -> None:
    """Regression insurance for the composers' explicit recursive-uniqueness claim
    (IS §5.4: action_id globally unique at every nesting depth) + the integration
    AC's branch-*tree* reconstruction. A level-2 branch's action_ids + idempotency
    keys carry both nesting segments and are distinct from the level-1 ones."""
    level1, level2 = _branch(0), _nested_branch()
    rik = "rik-nested"
    # Level-2 step action_id carries BOTH branch segments (recursively composed).
    l2_step = compose_branch_step_action_id(level2, 0)
    assert l2_step == f"{_PARENT_ACTION_ID}:branch:0:step:7:branch:2:step:0"
    l2_terminal = compose_branch_terminal_action_id(level2)
    assert l2_terminal == f"{_PARENT_ACTION_ID}:branch:0:step:7:branch:2:terminal"
    # Distinct from every level-1 action_id (steps + terminal).
    l1_ids = {compose_branch_step_action_id(level1, n) for n in range(20)}
    l1_ids.add(compose_branch_terminal_action_id(level1))
    assert l2_step not in l1_ids
    assert l2_terminal not in l1_ids
    # Idempotency keys are likewise cross-level distinct (the branch_path carries
    # the globally-unique nested parent_action_id).
    l1_keys = {
        _compute_step_idempotency_key(rik, n, compose_branch_path(level1)) for n in range(20)
    }
    l1_keys.add(
        _compute_step_idempotency_key(rik, level1.step_index, compose_branch_terminal_path(level1))
    )
    l2_step_key = _compute_step_idempotency_key(rik, 0, compose_branch_path(level2))
    l2_terminal_key = _compute_step_idempotency_key(
        rik, level2.step_index, compose_branch_terminal_path(level2)
    )
    assert l2_step_key not in l1_keys
    assert l2_terminal_key not in l1_keys
    assert l2_step_key != l2_terminal_key


# ---------------------------------------------------------------------------
# append helpers — buffer-level cadence (causality-only step / disposition terminal).
# ---------------------------------------------------------------------------


def test_append_branch_step_buffers_causality_only_entry() -> None:
    child = _branch(2)
    buffer = BufferingLedgerWriter(actor=_ACTOR, branch_index=2)
    append_branch_step_ledger_entry(
        branch_writer=buffer,
        branch_context=child,
        run_idempotency_key="rik",
        local_step_index=5,
        timestamp=_FAN_OUT_TS,
    )
    assert buffer.entry_count == 1
    payload, write_key = buffer.buffered_entries[0]
    assert payload.action_id == f"{_PARENT_ACTION_ID}:branch:2:step:5"
    assert payload.branch_metadata is not None
    assert payload.branch_metadata.parent_action_id == _PARENT_ACTION_ID
    assert payload.branch_metadata.branch_index == 2
    assert payload.branch_metadata.terminal_status is None  # causality only
    assert write_key.idempotency_key == payload.idempotency_key


def test_append_branch_terminal_buffers_disposition_entry() -> None:
    child = _branch(2)
    buffer = BufferingLedgerWriter(actor=_ACTOR, branch_index=2)
    append_branch_terminal_ledger_entry(
        branch_writer=buffer,
        branch_context=child,
        run_idempotency_key="rik",
        terminal_status="timed_out",
        timestamp=_FAN_OUT_TS,
    )
    payload, _wk = buffer.buffered_entries[0]
    assert payload.action_id == f"{_PARENT_ACTION_ID}:branch:2:terminal"
    assert payload.branch_metadata is not None
    assert payload.branch_metadata.terminal_status == "timed_out"


def test_append_helpers_thread_procedural_tier_snapshot_ref() -> None:
    """R-003 forward-completeness: the active-workflow-context
    `procedural_tier_snapshot_ref` sidecar (IS §5.1) is caller-injectable on both
    helpers so the U-CP-86 strategy (which holds the DriverContext resolver) can
    honor the population invariant; omitting it defaults None (resolver-less paths)."""
    child = _branch(0)
    buffer = BufferingLedgerWriter(actor=_ACTOR, branch_index=0)
    snapshot = Identifier("ptsr-deadbeef")
    append_branch_step_ledger_entry(
        branch_writer=buffer,
        branch_context=child,
        run_idempotency_key="rik",
        local_step_index=0,
        timestamp=_FAN_OUT_TS,
        procedural_tier_snapshot_ref=snapshot,
    )
    append_branch_terminal_ledger_entry(
        branch_writer=buffer,
        branch_context=child,
        run_idempotency_key="rik",
        terminal_status="completed",
        timestamp=_FAN_OUT_TS,
        procedural_tier_snapshot_ref=snapshot,
    )
    step_payload, _ = buffer.buffered_entries[0]
    terminal_payload, _ = buffer.buffered_entries[1]
    assert step_payload.procedural_tier_snapshot_ref == snapshot
    assert terminal_payload.procedural_tier_snapshot_ref == snapshot
    # The default (omitted) path stays None — backward-compatible with the §5.1
    # omit-when-None canonicalization.
    bare = BufferingLedgerWriter(actor=_ACTOR, branch_index=1)
    append_branch_step_ledger_entry(
        branch_writer=bare,
        branch_context=_branch(1),
        run_idempotency_key="rik",
        local_step_index=0,
        timestamp=_FAN_OUT_TS,
    )
    assert bare.buffered_entries[0][0].procedural_tier_snapshot_ref is None


# ---------------------------------------------------------------------------
# Round-trip through the REAL IS writer + chain re-verify (the functional AC).
# ---------------------------------------------------------------------------


async def test_branch_entries_round_trip_real_writer(tmp_path: Path) -> None:
    """Functional AC: each branch step entry persists with terminal_status==None;
    each branch's fresh terminal entry persists with the disposition; the §6.3
    chain re-verifies. Two branches, scrambled completion, branch-index drain,
    drain-time re-stamp, REAL writer."""
    real = _RealLedgerWriter(handle=_handle(tmp_path), actor=_ACTOR)
    buffers = await bounded_barrier(
        [
            _run_branch(branch_index=1, n_steps=2, per_step_delay=0.004, run_idempotency_key="rik"),
            _run_branch(branch_index=0, n_steps=2, per_step_delay=0.018, run_idempotency_key="rik"),
        ],
        deadline_seconds=5.0,
    )
    drained = drain_branch_buffers(real, buffers)
    assert drained == 6  # (2 steps + 1 terminal) * 2 branches
    assert all(r is WriteResult.APPENDED for r in real.results)  # nothing dedup-dropped

    entries = read_ledger(real._handle)
    assert [e.action_id for e in entries] == _expected_action_ids((0, 1), n_steps=2)
    for e in entries:
        assert e.branch_metadata is not None
        is_terminal = e.action_id.endswith(":terminal")
        if is_terminal:
            assert e.branch_metadata.terminal_status == "completed"
        else:
            assert e.branch_metadata.terminal_status is None  # step entry: causality only

    result = verify_chain(entries)
    assert result.status == VerificationStatus.VALID
    assert result.entries_verified == 6


async def test_terminal_entry_is_fresh_append_not_mutation(tmp_path: Path) -> None:
    """The disposition is recorded at a FRESH terminal entry — never by mutating an
    already-persisted step entry. Each branch yields step-count + 1 persisted
    entries; the distinct terminal action_id is present; the chain re-verifies
    (which would FAIL if any prior entry had been re-hashed)."""
    real = _RealLedgerWriter(handle=_handle(tmp_path), actor=_ACTOR)
    buffer = await _run_branch(
        branch_index=0, n_steps=3, per_step_delay=0.0, run_idempotency_key="rik"
    )
    drain_branch_buffers(real, [buffer])
    entries = read_ledger(real._handle)
    assert len(entries) == 4  # 3 step entries + 1 fresh terminal entry (no mutation)
    assert f"{_PARENT_ACTION_ID}:branch:0:terminal" in {e.action_id for e in entries}
    assert verify_chain(entries).status == VerificationStatus.VALID


async def test_ran_and_errored_branch_terminal_is_completed(tmp_path: Path) -> None:
    """§25.15.2 obl. 3/4: a branch whose in-flight step ran-and-errored is
    `completed` (dispatch-boundary disposition), NOT `failed` — its step failure
    lives at the step's own entry. The carrier's closed set carries no `failed`."""
    real = _RealLedgerWriter(handle=_handle(tmp_path), actor=_ACTOR)
    # The branch's last step "errored" — but its dispatch attempt completed, so the
    # terminal disposition the producer is handed is `completed`.
    buffer = await _run_branch(
        branch_index=0,
        n_steps=1,
        per_step_delay=0.0,
        run_idempotency_key="rik",
        terminal_status="completed",
    )
    drain_branch_buffers(real, [buffer])
    terminal = read_ledger(real._handle)[-1]
    assert terminal.branch_metadata is not None
    assert terminal.branch_metadata.terminal_status == "completed"


# ---------------------------------------------------------------------------
# Determinism ⟂ IS-monotonicity — the timestamp interaction (REAL writer).
# ---------------------------------------------------------------------------


async def test_scrambled_completion_persists_branch_index_order(tmp_path: Path) -> None:
    """The §25.12 determinism boundary AND the IS zero-tolerance monotonicity
    invariant hold together: with drain-time re-stamping, scrambled-completion
    concurrent branches drain in branch-index order (steps then terminal per
    branch) and EVERY entry APPENDs (none rejected as non-monotonic) — independent
    of which branch's 'model call' returned first."""
    real = _RealLedgerWriter(handle=_handle(tmp_path), actor=_ACTOR)
    # Collect buffers in REAL completion order (fastest-first), then drain.
    coros = [
        _run_branch(branch_index=bi, n_steps=2, per_step_delay=delay, run_idempotency_key="rik")
        for bi, delay in ((0, 0.020), (1, 0.010), (2, 0.004))
    ]
    completed = [await fut for fut in asyncio.as_completed(coros)]
    assert [b.branch_index for b in completed] != [0, 1, 2]  # completion ≠ branch-index order
    drain_branch_buffers(real, completed)
    entries = read_ledger(real._handle)
    assert [e.action_id for e in entries] == _expected_action_ids((0, 1, 2), n_steps=2)
    assert all(r is WriteResult.APPENDED for r in real.results)
    assert verify_chain(entries).status == VerificationStatus.VALID


def test_branch_index_drain_sanitizes_buffer_time_timestamps(tmp_path: Path) -> None:
    """Drain-time stamping is the IS-monotonicity realization (monotonic BY
    CONSTRUCTION), superseding the earlier caller-supplied shared-fan-out-timestamp
    policy. Even when a branch buffers an EXECUTION-time wall-clock that would
    invert branch-index order (branch 0 — drained FIRST — stamped LATER than branch
    1), `drain_branch_buffers` re-stamps every entry to ONE drain-moment value at
    its actual append point, so the REAL zero-tolerance writer APPENDs all of them
    (no `NonMonotonicTimestampError`) and the persisted timestamps are
    non-decreasing. (Previously: the caller owned the timestamp and the writer
    rejected the inversion; the safety-net for the DIRECT append paths is preserved
    by `test_direct_decreasing_timestamp_still_rejected_by_real_writer`.)"""
    real = _RealLedgerWriter(handle=_handle(tmp_path), actor=_ACTOR)
    late = datetime(2026, 6, 13, 12, 0, 5, tzinfo=UTC)
    early = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
    b0 = BufferingLedgerWriter(actor=_ACTOR, branch_index=0)
    b1 = BufferingLedgerWriter(actor=_ACTOR, branch_index=1)
    # Branch 0 (drained FIRST) buffered LATER than branch 1 (drained second) — the
    # inversion the OLD caller-owns policy relied on the writer to reject.
    append_branch_step_ledger_entry(
        branch_writer=b0,
        branch_context=_branch(0),
        run_idempotency_key="rik",
        local_step_index=0,
        timestamp=late,
    )
    append_branch_step_ledger_entry(
        branch_writer=b1,
        branch_context=_branch(1),
        run_idempotency_key="rik",
        local_step_index=0,
        timestamp=early,
    )
    drain_branch_buffers(real, [b0, b1])
    assert all(r is WriteResult.APPENDED for r in real.results)
    persisted = [e.timestamp for e in read_ledger(real._handle)]
    # The drain collapsed the inverted buffer-time stamps to one drain-moment
    # value — neither the buffered `late`/`early` survived.
    assert persisted == sorted(persisted)
    assert len(set(persisted)) == 1
    assert late not in persisted and early not in persisted


def test_direct_decreasing_timestamp_still_rejected_by_real_writer(tmp_path: Path) -> None:
    """The zero-tolerance writer's safety net is intact for the DIRECT (non-drain)
    append paths — the linear inline `_append_step_ledger_entry` and the runtime
    audit / cost writers, which stamp at their own append moment. A direct append
    whose timestamp precedes the prior entry's is still rejected. Drain-time
    re-stamping sanitizes ONLY the buffered fan-out drain; it does not weaken the
    writer (the inverse guarantee to the sibling above)."""
    real = _RealLedgerWriter(handle=_handle(tmp_path), actor=_ACTOR)
    late = datetime(2026, 6, 13, 12, 0, 5, tzinfo=UTC)
    early = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)

    def _direct(ts: datetime, n: int) -> tuple[EntryPayload, WriteKey]:
        return (
            EntryPayload(
                action_id=Identifier(f"workflow:wf:step:{n}"),
                idempotency_key=Identifier(f"idem-{n}"),
                actor=_ACTOR,
                timestamp=ts,
            ),
            WriteKey(
                thread_id=Identifier("wf"),
                step_id=Identifier(str(n)),
                idempotency_key=Identifier(f"idem-{n}"),
            ),
        )

    real.append(*_direct(late, 0))
    with pytest.raises(NonMonotonicTimestampError):
        real.append(*_direct(early, 1))
