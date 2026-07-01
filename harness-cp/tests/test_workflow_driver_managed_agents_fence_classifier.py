"""B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL (R-FS-1) — CP-side classifier units.

`_fence_unrecoverable_maybe_ran_indices` decides which maybe-ran fan-out branch ordinals
on a strict-tier crash-resume can RECOVER by re-dispatching into the effect fence vs which
STILL fail closed. This arc adds MANAGED_AGENTS to the recoverable set (now that its
vendor-session dispatch is fenced at its own §14.22 sink) AND a same-kind-equality guard
that closes the cross-kind hole opened by having >1 recoverable kind (a marker TOOL_STEP /
resumed MANAGED_AGENTS swap would otherwise pass both set-membership conjuncts but
re-dispatch into a DIFFERENT fence sink → the original effect's ambiguity silently
abandoned + a fresh effect fired).
"""

from __future__ import annotations

from harness_cp.workflow_driver import _fence_unrecoverable_maybe_ran_indices
from harness_cp.workflow_driver_types import StepKind

_TOOL = StepKind.TOOL_STEP.value
_MANAGED = StepKind.MANAGED_AGENTS.value
_DECL = StepKind.DECLARATIVE_STEP.value


def _classify(
    marker: str | None,
    resumed: str | None,
    *,
    branch_count: int = 2,
    marker_step_id: str | None = "s0",
    resumed_step_id: str | None = "s0",
) -> set[int]:
    """Return the UNRECOVERABLE subset of {0} for a single maybe-ran branch at ordinal 0
    with the given dispatch-marker + resumed-manifest kinds (and step_ids). Empty ⟹
    fence-recoverable. step_ids default to a MATCHING pair so the kind-focused tests isolate the
    kind conjuncts; the FENCE-STEP-ID step_id conjunct is exercised by `*_step_id_*` tests below."""
    dispatched: dict[int, str | None] = {0: marker}
    resumed_map: dict[int, str] = {0: resumed} if resumed is not None else {}
    dispatched_sids: dict[int, str | None] = {0: marker_step_id}
    resumed_sids: dict[int, str] = {0: resumed_step_id} if resumed_step_id is not None else {}
    return _fence_unrecoverable_maybe_ran_indices(
        {0},
        dispatched,
        resumed_map,
        branch_count,
        dispatched_step_ids=dispatched_sids,
        resumed_step_ids=resumed_sids,
    )


def test_managed_same_kind_is_fence_recoverable() -> None:
    """marker MANAGED_AGENTS + resumed MANAGED_AGENTS (same-kind, in range) → RECOVERABLE
    (the new capability — the vendor-session dispatch is fenced)."""
    assert _classify(_MANAGED, _MANAGED) == set()


def test_tool_same_kind_still_fence_recoverable() -> None:
    """Regression — the prior TOOL_STEP→TOOL_STEP recoverability is preserved (the
    same-kind equality conjunct was implied by the old singleton set; it is a no-op here)."""
    assert _classify(_TOOL, _TOOL) == set()


def test_cross_kind_tool_marker_managed_resumed_fails_closed() -> None:
    """marker TOOL_STEP + resumed MANAGED_AGENTS — BOTH in the recoverable set, but a
    CROSS-KIND swap → re-dispatch would reach the managed-agents sink (a DIFFERENT
    idempotency-key namespace) → the original tool effect's ambiguity silently abandoned.
    The same-kind guard fails it closed."""
    assert _classify(_TOOL, _MANAGED) == {0}


def test_cross_kind_managed_marker_tool_resumed_fails_closed() -> None:
    """The reverse cross-kind swap (marker MANAGED_AGENTS + resumed TOOL_STEP) also fails
    closed."""
    assert _classify(_MANAGED, _TOOL) == {0}


def test_managed_marker_changed_to_non_recoverable_fails_closed() -> None:
    """marker MANAGED_AGENTS + resumed DECLARATIVE_STEP (changed to a non-recoverable kind
    that reaches no fence sink) → fail closed (the changed-kind guard)."""
    assert _classify(_MANAGED, _DECL) == {0}


def test_managed_marker_missing_resumed_fails_closed() -> None:
    """marker MANAGED_AGENTS but no resumed kind at the ordinal (out of range / torn) →
    `resumed_kinds.get` is None ∉ the set → fail closed (presence ≠ validity)."""
    assert _classify(_MANAGED, None) == {0}


def test_managed_marker_out_of_range_ordinal_fails_closed() -> None:
    """A same-kind MANAGED branch whose ordinal is OUTSIDE [0, branch_count) (a stale /
    corrupt extra marker) fails closed regardless of kind."""
    out = _fence_unrecoverable_maybe_ran_indices(
        {3},
        {3: _MANAGED},
        {3: _MANAGED},
        2,
        dispatched_step_ids={3: "s3"},
        resumed_step_ids={3: "s3"},
    )
    assert out == {3}


def test_none_marker_managed_resumed_fails_closed() -> None:
    """A pre-arc / torn marker (None recorded kind) + resumed MANAGED_AGENTS fails closed —
    the marker kind cannot be trusted as MANAGED."""
    assert _classify(None, _MANAGED) == {0}


def test_same_kind_changed_step_id_fails_closed() -> None:
    """FENCE-STEP-ID (#742) — marker TOOL_STEP + resumed TOOL_STEP (same kind, in range) but a
    CHANGED step_id → re-dispatch would compose a DIFFERENT fence key (the key includes step_id) →
    miss the held claim → double-fire. The step_id conjunct fails it closed even though both kind
    conjuncts pass."""
    assert _classify(_TOOL, _TOOL, marker_step_id="s0", resumed_step_id="s0-renamed") == {0}


def test_same_kind_matching_step_id_is_fence_recoverable() -> None:
    """FENCE-STEP-ID — marker + resumed kind AND step_id all match → RECOVERABLE (the fence key
    is reproduced exactly)."""
    assert _classify(_TOOL, _TOOL, marker_step_id="s0", resumed_step_id="s0") == set()


def test_none_marker_step_id_fails_closed() -> None:
    """FENCE-STEP-ID — a torn marker with no recorded step_id (None) cannot prove the original
    fence key → fail closed even with a matching kind + a present resumed step_id."""
    assert _classify(_TOOL, _TOOL, marker_step_id=None, resumed_step_id="s0") == {0}
