"""Tests for U-AS-33 — AS-axis substrate seam exports manifest (C-AS-16 §16)."""

from __future__ import annotations

import re

from harness_as.as_substrate_seam_exports import (
    AS_SUBSTRATE_SEAM_EXPORTS,
    ASConsumingAxis,
    ASSeamId,
    ASSubstrateSeamExport,
    as_seam_carrier_units,
    as_seam_consuming_axes,
    as_seam_export_surface,
)

# Filed AS units U-AS-01 .. U-AS-33 (terminal exporter excluded as a carrier).
_FILED_AS_UNITS = {f"U-AS-{n:02d}" for n in range(1, 33)}


def test_as_substrate_seam_exports_cardinality_seven() -> None:
    """Acceptance #1 — AS_SUBSTRATE_SEAM_EXPORTS enumerates exactly 7 entries."""
    assert len(AS_SUBSTRATE_SEAM_EXPORTS) == 7
    assert {e.seam_id for e in AS_SUBSTRATE_SEAM_EXPORTS} == set(ASSeamId)


def test_as_seam_carrier_units_cite_filed_units() -> None:
    """Acceptance #2 — every carrier unit resolves to a filed U-AS-01..32 unit."""
    for export in AS_SUBSTRATE_SEAM_EXPORTS:
        assert export.carrier_units
        for unit in export.carrier_units:
            assert unit in _FILED_AS_UNITS


def test_as_seam_consuming_axes_per_spec() -> None:
    """Acceptance #3 — each seam's consuming axes are AS consuming-axis members."""
    for export in AS_SUBSTRATE_SEAM_EXPORTS:
        assert export.consuming_axes
        for axis in export.consuming_axes:
            assert axis in ASConsumingAxis


def test_as_seam_spec_citation_form() -> None:
    """Acceptance #4 — each spec_citation has the form `C-AS-16 §16.X`, X in 1..7."""
    pattern = re.compile(r"^C-AS-16 §16\.[1-7]$")
    for export in AS_SUBSTRATE_SEAM_EXPORTS:
        assert pattern.match(export.spec_citation)


def test_as_substrate_seam_exports_declarative_only() -> None:
    """Acceptance #5 — the manifest is declarative records only."""
    for export in AS_SUBSTRATE_SEAM_EXPORTS:
        assert isinstance(export, ASSubstrateSeamExport)


def test_as_seam_accessors_resolve() -> None:
    """The three seam accessors resolve for every seam id."""
    for seam in ASSeamId:
        assert as_seam_carrier_units(seam)
        assert as_seam_consuming_axes(seam)
        assert as_seam_export_surface(seam)


def test_secret_fetch_audit_export_consumed_by_od() -> None:
    """Acceptance #3 — the secret-fetch audit export is consumed by OD."""
    axes = as_seam_consuming_axes(ASSeamId.SECRET_FETCH_AUDIT_EXPORT)
    assert ASConsumingAxis.OPERATIONAL_DISCIPLINE in axes
