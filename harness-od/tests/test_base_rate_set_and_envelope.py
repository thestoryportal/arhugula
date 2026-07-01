"""Tests for U-OD-12 — 13-entry base-rate-sampled set + per-cell envelope.

Test set per the U-OD-12 §3.4.2 (v2.8) `Tests:` field — covers acceptance
#1-#6 against C-OD-10 §10.1 / §10.2 / §10.3 + §9.2. acc #2 re-scoped per v2.8
D-4; the v2.5 unconditional-disjointness test is replaced by the
non-`kind`-discriminated disjointness test plus the two dual-regime tests.
"""

from __future__ import annotations

from harness_core import DeploymentSurface, PersonaTier
from harness_od.base_rate_set_and_envelope import (
    BASE_RATE_SAMPLED_EVENT_CLASSES,
    DUAL_REGIME_EVENT_CLASSES,
    PER_CELL_BASE_RATE_ENVELOPE,
    TAIL_KEEP_RULES,
)
from harness_od.observability_matrix import ACTIVE_CELLS, CellID
from harness_od.sampling_mode import ALWAYS_SAMPLED_EVENT_CLASSES

_EXPECTED_BASE_RATE: frozenset[str] = frozenset(
    {
        "chat",
        "execute_tool",
        "sandbox.enter",
        "sandbox.exit",
        "tool.call",
        "retrieval",
        "cache.events",
        "embeddings",
        "text_completion",
        "files.operation",
        "memory.operation",
        "lease.acquired_released",
        "retry.attempt.first",
    }
)

_SOLO = PersonaTier.SOLO_DEVELOPER
_TEAM = PersonaTier.TEAM_BINDING
_MTC = PersonaTier.MULTI_TENANT_COMPLIANCE


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


# --- acc #1 ----------------------------------------------------------------
def test_base_rate_set_cardinality_thirteen() -> None:
    """`BASE_RATE_SAMPLED_EVENT_CLASSES` has cardinality 13 per §10.1."""
    assert len(BASE_RATE_SAMPLED_EVENT_CLASSES) == 13


def test_base_rate_event_members_byte_exact_per_10_1() -> None:
    """Member set is byte-exact against the §10.1 table (13 rows)."""
    assert BASE_RATE_SAMPLED_EVENT_CLASSES == _EXPECTED_BASE_RATE


# --- acc #2 (v2.8 D-4 re-scoped) -------------------------------------------
def test_regime_disjoint_over_non_kind_discriminated_classes() -> None:
    """For non-`kind`-discriminated classes, regimes are disjoint (§9.2/§10.1).

    Every event class other than `files.operation` / `memory.operation` is a
    member of exactly one of `BASE_RATE_SAMPLED_EVENT_CLASSES` /
    `ALWAYS_SAMPLED_EVENT_CLASSES`.
    """
    overlap = BASE_RATE_SAMPLED_EVENT_CLASSES & ALWAYS_SAMPLED_EVENT_CLASSES
    assert overlap == DUAL_REGIME_EVENT_CLASSES


def test_files_operation_dual_regime_routed_by_kind() -> None:
    """`files.operation` is a dual-regime class — in both regimes (§9.2/§10.1)."""
    assert "files.operation" in DUAL_REGIME_EVENT_CLASSES
    assert "files.operation" in BASE_RATE_SAMPLED_EVENT_CLASSES
    assert "files.operation" in ALWAYS_SAMPLED_EVENT_CLASSES


def test_memory_operation_dual_regime_routed_by_kind() -> None:
    """`memory.operation` is a dual-regime class — in both regimes (§9.2/§10.1)."""
    assert "memory.operation" in DUAL_REGIME_EVENT_CLASSES
    assert "memory.operation" in BASE_RATE_SAMPLED_EVENT_CLASSES
    assert "memory.operation" in ALWAYS_SAMPLED_EVENT_CLASSES


# --- acc #3 ----------------------------------------------------------------
def test_per_cell_envelope_cardinality_eight() -> None:
    """`PER_CELL_BASE_RATE_ENVELOPE` has cardinality 8 — one per ACTIVE cell."""
    assert len(PER_CELL_BASE_RATE_ENVELOPE) == 8
    assert set(PER_CELL_BASE_RATE_ENVELOPE) == set(ACTIVE_CELLS)


def test_solo_cells_default_rate_one_point_zero() -> None:
    """solo-developer cells default to 1.0 per §10.3."""
    for ds in DeploymentSurface:
        envelope = PER_CELL_BASE_RATE_ENVELOPE[_cell(_SOLO, ds)]
        assert envelope.default_rate == 1.0


def test_team_cells_default_rate_in_envelope() -> None:
    """team-binding cells carry the §10.3 defaults within their envelopes."""
    for ds in DeploymentSurface:
        envelope = PER_CELL_BASE_RATE_ENVELOPE[_cell(_TEAM, ds)]
        assert envelope.min_rate <= envelope.default_rate <= envelope.max_rate


def test_multi_tenant_cells_default_rate_in_envelope() -> None:
    """multi-tenant-compliance cells carry §10.3 defaults within their envelopes."""
    for ds in (DeploymentSurface.SELF_HOSTED_SERVER, DeploymentSurface.MANAGED_CLOUD):
        envelope = PER_CELL_BASE_RATE_ENVELOPE[_cell(_MTC, ds)]
        assert envelope.default_rate == 0.2
        assert envelope.min_rate == 0.1
        assert envelope.max_rate == 0.5


# --- acc #4 ----------------------------------------------------------------
def test_envelope_invariant_min_default_max() -> None:
    """`min_rate <= default_rate <= max_rate` per cell (§10.3 envelope invariant)."""
    for envelope in PER_CELL_BASE_RATE_ENVELOPE.values():
        assert envelope.min_rate <= envelope.default_rate <= envelope.max_rate


# --- acc #5 ----------------------------------------------------------------
def test_envelope_tightening_across_bridging_arc() -> None:
    """`target.max_rate <= source.max_rate` along the persona-tier axis (§10.3).

    The §10.3 envelope tightens monotonically along solo → team → multi-tenant
    at fixed deployment surface.
    """
    for ds in (DeploymentSurface.SELF_HOSTED_SERVER, DeploymentSurface.MANAGED_CLOUD):
        solo_max = PER_CELL_BASE_RATE_ENVELOPE[_cell(_SOLO, ds)].max_rate
        team_max = PER_CELL_BASE_RATE_ENVELOPE[_cell(_TEAM, ds)].max_rate
        mtc_max = PER_CELL_BASE_RATE_ENVELOPE[_cell(_MTC, ds)].max_rate
        assert team_max <= solo_max
        assert mtc_max <= team_max


# --- acc #6 ----------------------------------------------------------------
def test_tail_keep_rules_apply_post_classification() -> None:
    """`TAIL_KEEP_RULES` declares ALWAYS_KEEP for failed-trace triggers (§10.2)."""
    triggers = {rule.classification_attribute for rule in TAIL_KEEP_RULES}
    assert triggers == {
        "validator.fail.permanent",
        "sandbox.violation",
        "breaker.tripped",
    }
    for rule in TAIL_KEEP_RULES:
        assert rule.keep_decision == "ALWAYS_KEEP"
