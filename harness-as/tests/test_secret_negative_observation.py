"""Tests for U-AS-21 — negative-observation invariant enforcement (C-AS-05 §5.3)."""

from __future__ import annotations

from harness_as.secret_negative_observation import (
    validate_no_secret_in_audit_ledger_entry,
    validate_no_secret_in_span_attributes,
    validate_no_secret_in_static_prefix,
    verify_sole_resolution_path,
)

_MARKERS = frozenset({"sk-live-deadbeef"})


def test_validate_no_secret_in_static_prefix_detects_known_pattern() -> None:
    """Acceptance #1/#2 — a known secret pattern in the prefix is a violation."""
    violation = validate_no_secret_in_static_prefix(
        "system prompt ... sk-live-deadbeef ...", _MARKERS
    )
    assert violation is not None


def test_validate_no_secret_in_static_prefix_passes_clean_prefix() -> None:
    """Acceptance #1 — a clean prefix passes."""
    assert validate_no_secret_in_static_prefix("system prompt only", _MARKERS) is None


def test_validate_no_secret_in_span_attributes_composes_with_u_as_17_exclusions() -> None:
    """Acceptance #3 — an exclusion-set attribute name is a violation."""
    violation = validate_no_secret_in_span_attributes({"secret_value": "x"})
    assert violation is not None


def test_validate_no_secret_in_audit_ledger_entry_detects_value_content() -> None:
    """Acceptance #1 — a secret value in a ledger-entry field is a violation."""
    violation = validate_no_secret_in_audit_ledger_entry(
        {"note": "leaked sk-live-deadbeef"}, _MARKERS
    )
    assert violation is not None


def test_verify_sole_resolution_path_rejects_manifest_arrival() -> None:
    """Acceptance #4 — a secret arriving via the manifest is a violation."""
    assert verify_sole_resolution_path("workflow_manifest") is not None


def test_verify_sole_resolution_path_accepts_fetch_secret_arrival() -> None:
    """Acceptance #4 — a secret arriving via fetch_secret is permitted."""
    assert verify_sole_resolution_path("fetch_secret") is None
