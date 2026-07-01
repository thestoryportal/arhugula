"""B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — CP-side classifier units.

`_fence_unrecoverable_maybe_ran_indices` gains a SECOND recovery mechanism alongside the
TOOL_STEP / MANAGED_AGENTS fence-recovery: a maybe-ran SUB_AGENT_DISPATCH worker recovers by
re-dispatching its child under the deterministic child run_id (the child's own crash-resume
auto-resumes, result-faithfully). It is recoverable ONLY when the child was RECOVERABLE
({ESR,WAL,SAVE_POINT,RECONCILER} ∧ LINEAR ∧ leaf) BOTH at dispatch
(`subagent_recoverable_indexes`, the marker) AND in the RESUMED manifest
(`resumed_subagent_recoverable_indexes`) — the [P1-b] dual gate (the #746 `6930e7ef` Codex [P1])
— AND with the SAME child engine class at dispatch + resume (`dispatched_child_engines` ==
`resumed_child_engines`, the cross-engine-class swap guard, out-of-family Codex [P1] on the
…-RECONCILER-CHILD arc). Requiring all three closes the changed-manifest hole (a child edited
recoverable→non-recoverable between dispatch + resume has durable records but the re-dispatch
runs the non-recoverable child fresh → double-fire / suffix-only corruption) AND the
cross-engine-class swap hole (a same-step_id RECONCILER→SAVE_POINT swap keeps the same
engine-class-agnostic `compose_child_run_id_seed` seed → re-dispatches against the SAME durable
store through a DIFFERENT recovery mechanism, bypassing the RECONCILER CAS at-most-once
protection).
"""

from __future__ import annotations

from collections.abc import Collection

from harness_cp.workflow_driver import _fence_unrecoverable_maybe_ran_indices
from harness_cp.workflow_driver_types import StepKind

_SUB = StepKind.SUB_AGENT_DISPATCH.value
_TOOL = StepKind.TOOL_STEP.value
_DECL = StepKind.DECLARATIVE_STEP.value
_ESR = "event-sourced-replay"
_SAVE = "save-point-checkpoint"
_RECON = "reconciler-loop"


def _classify(
    marker: str | None,
    resumed: str | None,
    *,
    marker_recoverable: bool,
    resumed_recoverable: bool,
    branch_count: int = 2,
    marker_step_id: str | None = "s0",
    resumed_step_id: str | None = "s0",
    marker_engine: str | None = _ESR,
    resumed_engine: str | None = _ESR,
) -> set[int]:
    """UNRECOVERABLE subset of {0} for a single maybe-ran branch at ordinal 0 with the given
    dispatch-marker + resumed kinds, the dispatch-time / resumed-manifest child recoverability, and
    the dispatch-time / resumed child engine class. Empty ⟹ recoverable. `marker_engine` /
    `resumed_engine` default to the SAME recoverable engine (the same-engine conjunct holds unless a
    test varies them — the cross-engine-class swap guard)."""
    subagent_dispatch: Collection[int] = {0} if marker_recoverable else set()
    subagent_resumed: Collection[int] = {0} if resumed_recoverable else set()
    resumed_map: dict[int, str] = {0: resumed} if resumed is not None else {}
    resumed_sids: dict[int, str] = {0: resumed_step_id} if resumed_step_id is not None else {}
    marker_engines: dict[int, str | None] = {0: marker_engine} if marker_engine is not None else {}
    resumed_engines: dict[int, str | None] = (
        {0: resumed_engine} if resumed_engine is not None else {}
    )
    return _fence_unrecoverable_maybe_ran_indices(
        {0},
        {0: marker},
        resumed_map,
        branch_count,
        dispatched_step_ids={0: marker_step_id},
        resumed_step_ids=resumed_sids,
        subagent_recoverable_indexes=subagent_dispatch,
        resumed_subagent_recoverable_indexes=subagent_resumed,
        dispatched_child_engines=marker_engines,
        resumed_child_engines=resumed_engines,
    )


def test_subagent_recoverable_both_dispatch_and_resumed_is_recoverable() -> None:
    """marker SUB_AGENT + resumed SUB_AGENT, child recoverable BOTH at dispatch AND resume →
    RECOVERABLE (the new capability — re-dispatch auto-resumes the recoverable child)."""
    assert _classify(_SUB, _SUB, marker_recoverable=True, resumed_recoverable=True) == set()


def test_subagent_recoverable_at_dispatch_but_not_resumed_fails_closed() -> None:
    """[P1-b] — child recoverable at DISPATCH (durable records exist) but NON-recoverable in the
    RESUMED manifest (operator edited it {ESR}→PURE_PATTERN / LINEAR→fan-out / added a nested
    sub-agent) → the re-dispatch runs the non-recoverable child FRESH → double-fire / suffix-only
    corruption. The resumed-side conjunct fails it closed."""
    assert _classify(_SUB, _SUB, marker_recoverable=True, resumed_recoverable=False) == {0}


def test_subagent_recoverable_at_resumed_but_not_dispatch_fails_closed() -> None:
    """The dispatch-time marker is the at-most-once changed-manifest authority: a child
    NON-recoverable at dispatch (no durable child records to auto-resume from) but recoverable in
    the resumed manifest STILL fails closed — there is no durable prefix to reconstruct from."""
    assert _classify(_SUB, _SUB, marker_recoverable=False, resumed_recoverable=True) == {0}


def test_subagent_non_recoverable_both_fails_closed() -> None:
    """A non-recoverable child (e.g. a PURE_PATTERN / fan-out / non-leaf child) at BOTH dispatch and
    resume → fail closed (the PURE_PATTERN-child / fan-out-child / nested-child residuals)."""
    assert _classify(_SUB, _SUB, marker_recoverable=False, resumed_recoverable=False) == {0}


def test_subagent_marker_tool_resumed_cross_kind_fails_closed() -> None:
    """A maybe-ran SUB_AGENT branch re-supplied at the same ordinal as a TOOL_STEP — even with both
    recoverable flags set — fails closed: marker SUB_AGENT ≠ resumed TOOL → the SUB_AGENT recovery
    disjunct requires both kinds be SUB_AGENT, and the TOOL disjunct requires both be TOOL."""
    assert _classify(_SUB, _TOOL, marker_recoverable=True, resumed_recoverable=True) == {0}


def test_tool_marker_subagent_resumed_cross_kind_fails_closed() -> None:
    """The reverse cross-kind swap (marker TOOL_STEP + resumed SUB_AGENT_DISPATCH) also fails
    closed."""
    assert _classify(_TOOL, _SUB, marker_recoverable=True, resumed_recoverable=True) == {0}


def test_subagent_changed_to_non_recoverable_kind_fails_closed() -> None:
    """marker SUB_AGENT + resumed DECLARATIVE (a non-recovery kind) → fail closed even with the
    recoverability flags set (the changed-kind guard; resumed must be SUB_AGENT)."""
    assert _classify(_SUB, _DECL, marker_recoverable=True, resumed_recoverable=True) == {0}


def test_subagent_recoverable_changed_step_id_fails_closed() -> None:
    """The COMMON changed-step_id guard applies to the SUB_AGENT path too: same kind + recoverable
    both, but a RENAMED step_id at this ordinal → fail closed (a different branch was re-supplied)."""
    assert _classify(
        _SUB,
        _SUB,
        marker_recoverable=True,
        resumed_recoverable=True,
        marker_step_id="s0",
        resumed_step_id="s0-renamed",
    ) == {0}


def test_subagent_recoverable_out_of_range_ordinal_fails_closed() -> None:
    """A recoverable same-kind SUB_AGENT branch whose ordinal is OUTSIDE [0, branch_count) (a
    stale / corrupt extra marker) fails closed regardless of recoverability."""
    out = _fence_unrecoverable_maybe_ran_indices(
        {3},
        {3: _SUB},
        {3: _SUB},
        2,
        dispatched_step_ids={3: "s3"},
        resumed_step_ids={3: "s3"},
        subagent_recoverable_indexes={3},
        resumed_subagent_recoverable_indexes={3},
    )
    assert out == {3}


def test_subagent_default_empty_recoverable_sets_fail_closed() -> None:
    """Back-compat: callers that DON'T pass the two new sets (the default empty frozensets) leave
    every SUB_AGENT branch fail-closed — the pre-arc behavior (a store that can't answer
    recoverability never auto-recovers a sub-agent)."""
    out = _fence_unrecoverable_maybe_ran_indices(
        {0},
        {0: _SUB},
        {0: _SUB},
        2,
        dispatched_step_ids={0: "s0"},
        resumed_step_ids={0: "s0"},
    )
    assert out == {0}


def test_subagent_same_engine_reconciler_recoverable() -> None:
    """Positive control for the RECONCILER engine + the same-engine conjunct: marker RECONCILER +
    resumed RECONCILER, both recoverable, same step_id → RECOVERABLE (the re-dispatched RECONCILER
    child runs its OWN crash-resume; the same-engine conjunct holds)."""
    assert (
        _classify(
            _SUB,
            _SUB,
            marker_recoverable=True,
            resumed_recoverable=True,
            marker_engine=_RECON,
            resumed_engine=_RECON,
        )
        == set()
    )


def test_subagent_cross_engine_swap_reconciler_to_savepoint_fails_closed() -> None:
    """THE WITNESS (out-of-family Codex [P1], …-RECONCILER-CHILD arc) — RED without the same-engine
    guard. Marker child engine RECONCILER but resumed child engine SAVE_POINT, BOTH recoverable, SAME
    step_id: both [P1-b] booleans + the changed-step_id guard pass, yet `compose_child_run_id_seed`
    is engine-class-agnostic → the swap would re-dispatch the child against the SAME durable store
    through SAVE_POINT recovery instead of the RECONCILER CAS path, bypassing the at-most-once
    protection. The same-engine conjunct fails it closed."""
    assert _classify(
        _SUB,
        _SUB,
        marker_recoverable=True,
        resumed_recoverable=True,
        marker_engine=_RECON,
        resumed_engine=_SAVE,
    ) == {0}


def test_subagent_cross_engine_swap_savepoint_to_reconciler_fails_closed() -> None:
    """The reverse swap (marker SAVE_POINT, resumed RECONCILER) also fails closed — the same-engine
    conjunct is symmetric."""
    assert _classify(
        _SUB,
        _SUB,
        marker_recoverable=True,
        resumed_recoverable=True,
        marker_engine=_SAVE,
        resumed_engine=_RECON,
    ) == {0}


def test_subagent_marker_engine_missing_fails_closed() -> None:
    """A torn / pre-arc marker with NO recorded child engine class (`marker_engine=None` → the engine
    map omits the ordinal → `.get(0)` is None) fails closed even with both recoverable + same
    step_id: cannot prove the dispatch-time engine, so the same-engine conjunct (marker engine is not
    None) fails."""
    assert _classify(
        _SUB,
        _SUB,
        marker_recoverable=True,
        resumed_recoverable=True,
        marker_engine=None,
        resumed_engine=_RECON,
    ) == {0}
