"""U-RT-08 — `CollectorConfig` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L1:
- Placement matrix selected (CollectorPlacement field present + typed).
- Thresholds bounded (all numeric thresholds > 0).
- Defaults match OD spec (C-OD-19 §19.1 BSP constants).
"""

from __future__ import annotations

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_od.local_first_otlp_collector import (
    BATCH_SPAN_PROCESSOR_BATCH_SIZE,
    BATCH_SPAN_PROCESSOR_WINDOW_SECONDS,
)
from harness_od.per_cell_collector_placement_matrix import CollectorPlacement
from harness_runtime.types import CollectorConfig
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Placement matrix selection (plan AC).
# ---------------------------------------------------------------------------


def test_placement_defaults_in_process() -> None:
    """Default placement is `IN_PROCESS` per F-P2-5 (runtime owns the daemon)."""
    cfg = CollectorConfig()
    assert cfg.placement is CollectorPlacement.IN_PROCESS


def test_all_seven_placement_classes_acceptable() -> None:
    """The full C-OD-20 §20.1 7-class enum is selectable."""
    for placement in CollectorPlacement:
        cfg = CollectorConfig(placement=placement)
        assert cfg.placement is placement


def test_bootstrap_sandbox_tier_defaults_tier1_process() -> None:
    """Default bootstrap reachability preserves host-process runtime semantics."""
    cfg = CollectorConfig()
    assert cfg.bootstrap_sandbox_tier is SandboxTier.TIER_1_PROCESS


def test_all_bootstrap_sandbox_tiers_acceptable() -> None:
    """Deployment bindings can declare the actual bootstrap sandbox tier."""
    for sandbox_tier in SandboxTier:
        cfg = CollectorConfig(bootstrap_sandbox_tier=sandbox_tier)
        assert cfg.bootstrap_sandbox_tier is sandbox_tier


# ---------------------------------------------------------------------------
# Defaults match OD spec (plan AC).
# ---------------------------------------------------------------------------


def test_batch_window_defaults_to_od_spec_value() -> None:
    """`batch_window_seconds` default = `BATCH_SPAN_PROCESSOR_WINDOW_SECONDS` (5)."""
    cfg = CollectorConfig()
    assert cfg.batch_window_seconds == BATCH_SPAN_PROCESSOR_WINDOW_SECONDS
    assert cfg.batch_window_seconds == 5


def test_batch_size_defaults_to_od_spec_value() -> None:
    """`batch_size` default = `BATCH_SPAN_PROCESSOR_BATCH_SIZE` (512)."""
    cfg = CollectorConfig()
    assert cfg.batch_size == BATCH_SPAN_PROCESSOR_BATCH_SIZE
    assert cfg.batch_size == 512


def test_ring_buffer_default_sensible() -> None:
    """`ring_buffer_size` default is bounded and sensible."""
    cfg = CollectorConfig()
    assert cfg.ring_buffer_size > 0
    assert cfg.ring_buffer_size == 4096


def test_sqlite_rotation_defaults_sensible() -> None:
    """SQLite rotation thresholds default to bounded sensible values."""
    cfg = CollectorConfig()
    assert cfg.sqlite_rotation_max_rows == 100_000
    assert cfg.sqlite_rotation_max_bytes == 100_000_000


def test_tail_keep_buffer_bounds_default_sensible() -> None:
    """OD spec v1.28 §9.3 tail-keep buffer ceilings default bounded + sensible."""
    cfg = CollectorConfig()
    assert cfg.tail_keep_max_buffered_traces == 4096
    assert cfg.tail_keep_max_spans_per_trace == 4096


# ---------------------------------------------------------------------------
# Thresholds bounded > 0 (plan AC).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    [
        "ring_buffer_size",
        "sqlite_rotation_max_rows",
        "sqlite_rotation_max_bytes",
        "batch_window_seconds",
        "batch_size",
        "tail_keep_max_buffered_traces",
        "tail_keep_max_spans_per_trace",
    ],
)
def test_threshold_rejects_zero(field_name: str) -> None:
    """All numeric thresholds reject 0."""
    with pytest.raises(ValidationError):
        CollectorConfig(**{field_name: 0})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "field_name",
    [
        "ring_buffer_size",
        "sqlite_rotation_max_rows",
        "sqlite_rotation_max_bytes",
        "batch_window_seconds",
        "batch_size",
        "tail_keep_max_buffered_traces",
        "tail_keep_max_spans_per_trace",
    ],
)
def test_threshold_rejects_negative(field_name: str) -> None:
    """All numeric thresholds reject negative values."""
    with pytest.raises(ValidationError):
        CollectorConfig(**{field_name: -1})  # pyright: ignore[reportArgumentType]


def test_threshold_accepts_one_as_lower_bound() -> None:
    """`> 0` is the lower bound; 1 is the smallest valid value."""
    cfg = CollectorConfig(
        ring_buffer_size=1,
        sqlite_rotation_max_rows=1,
        sqlite_rotation_max_bytes=1,
        batch_window_seconds=1,
        batch_size=1,
    )
    assert cfg.ring_buffer_size == 1


# ---------------------------------------------------------------------------
# Config invariants.
# ---------------------------------------------------------------------------


def test_collector_config_is_frozen() -> None:
    """`CollectorConfig` is frozen per C-RT-03 invariant."""
    assert CollectorConfig.model_config.get("frozen") is True


def test_collector_config_rejects_unknown_keys() -> None:
    """`extra='forbid'` per C-RT-03."""
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({"unknown_field": "x"})


def test_collector_config_round_trips() -> None:
    """`CollectorConfig` survives model_dump/model_validate byte-equal."""
    original = CollectorConfig(
        placement=CollectorPlacement.SIDECAR,
        bootstrap_sandbox_tier=SandboxTier.TIER_4_FULL_VM,
        ring_buffer_size=8192,
        batch_size=256,
    )
    rebuilt = CollectorConfig.model_validate(original.model_dump())
    assert rebuilt == original
