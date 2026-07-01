"""Tests for U-IS-17 — IS substrate seam exports manifest (C-IS-10 §10.1-§10.6).

Test set per the U-IS-17 `Tests:` field — 8 tests covering acceptance #1-#8.
"""

from __future__ import annotations

import re

from harness_is.substrate_seam_exports import (
    ADR_BODY_CITATION_VERSIONS,
    IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    ConsumingAxis,
    SeamId,
    SubstrateSeamExport,
)


def test_substrate_seam_exports_completeness() -> None:
    """Acceptance #1 — exactly 6 seam exports, one per SeamId."""
    assert len(IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST) == 6
    assert {x.seam_id for x in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST} == set(SeamId)


def test_carrier_units_resolve() -> None:
    """Acceptance #2 — every carrier unit is a U-IS-NN with NN in 1..16."""
    for export in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST:
        for unit in export.carrier_units:
            match = re.fullmatch(r"U-IS-(\d{2})", unit)
            assert match is not None
            assert 1 <= int(match.group(1)) <= 16


def test_carrier_units_cover_export_surface() -> None:
    """Acceptance #2 — each export cites at least one carrier unit."""
    for export in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST:
        assert len(export.carrier_units) >= 1


def test_consuming_axes_match_spec() -> None:
    """Acceptance #3 — consuming axes per seam match the manifest content."""
    by_seam = {x.seam_id: x.consuming_axes for x in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST}
    assert by_seam[SeamId.STATE_LEDGER_ENTRY_SHAPE_EXPORT] == (
        ConsumingAxis.CONTROL_PLANE,
        ConsumingAxis.OPERATIONAL_DISCIPLINE,
        ConsumingAxis.ACTION_SURFACE,
    )
    assert by_seam[SeamId.HASH_CHAIN_CONSTRUCTION_DISCIPLINE_EXPORT] == (
        ConsumingAxis.OPERATIONAL_DISCIPLINE,
    )
    assert by_seam[SeamId.WORKLOAD_CLASS_OPT_IN_MANIFEST_EXPORT] == (ConsumingAxis.CONTROL_PLANE,)


def test_spec_citation_stable_anchor() -> None:
    """Acceptance #4 — each spec_citation is `C-IS-10 §10.X`, X in 1..6."""
    for export in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST:
        match = re.fullmatch(r"C-IS-10 §10\.(\d)", export.spec_citation)
        assert match is not None
        assert 1 <= int(match.group(1)) <= 6


def test_f2_12_carry_forward_preserved() -> None:
    """Acceptance #6 — the F2-12 carry-forward note is preserved at the
    IDEMPOTENCY_KEY_JOIN_EXPORT seam."""
    [seam] = [
        x
        for x in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST
        if x.seam_id is SeamId.IDEMPOTENCY_KEY_JOIN_EXPORT
    ]
    joined = " ".join(seam.composition_references)
    assert "F2-12 carry-forward" in joined


def test_adr_body_citation_versions_aligned() -> None:
    """Acceptance #7 — the ADR body-citation versions are the v2.1-filed set,
    and the version-bearing ADR citations in the composition references carry
    no contradicting version."""
    assert ADR_BODY_CITATION_VERSIONS == {
        "F1": "v1.2",
        "F2": "v1.2",
        "F3": "v1.1",
        "D1": "v1.1",
        "D2": "v1.1",
        "D3": "v1.2",
        "D4": "v1.1",
        "D5": "v1.3",
        "D6": "v1.1",
    }
    text = " ".join(
        ref for x in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST for ref in x.composition_references
    )
    # Every "D{n} v1.X" / "F1 v1.X" citation in the text matches acceptance #7.
    for adr, version in re.findall(r"\b([DF]\d) (v1\.\d)", text):
        assert ADR_BODY_CITATION_VERSIONS[adr] == version


def test_manifest_no_executable_behavior() -> None:
    """Acceptance #5 — the manifest is declarative: every entry is a frozen
    `SubstrateSeamExport` record, no executable behavior."""
    for export in IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST:
        assert isinstance(export, SubstrateSeamExport)
        assert export.model_config.get("frozen") is True
