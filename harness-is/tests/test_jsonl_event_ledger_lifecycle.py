"""Tests for U-IS-05 — JSONL event ledger file lifecycle (C-IS-03 §3).

Test set per the U-IS-05 `Tests:` field — covers acceptance #1-#6.

**IS spec v1.3 §1 amendment (2026-05-20)** — `PathClass.STATE_LEDGER`
resolves to a DIRECTORY containing the JSONL file (`state.jsonl`). Tests
below bind the resolver to the directory path; the file lives inside.
Resolves `[[fork-state-ledger-path-dir-vs-file]]` Path A.
"""

from __future__ import annotations

from pathlib import Path

from harness_core import DeploymentSurface, WorkloadClass
from harness_is.jsonl_event_ledger_lifecycle import (
    JsonlLedgerHandle,
    LedgerFormatValidationResult,
    initialize_jsonl_event_ledger,
    validate_jsonl_event_ledger_format,
)
from harness_is.path_binding import load_path_binding
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver

_WORKFLOW = WorkloadClass.SOFTWARE_ENGINEERING
_SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT


def _resolver(directory: Path) -> PathResolver:
    """A resolver bound so `(STATE_LEDGER, software-engineering, local-development)`
    maps to `directory` — the canonical state-ledger directory per IS v1.3
    spec. The JSONL file lives at `directory / "state.jsonl"`."""
    return PathResolver(
        load_path_binding(
            [
                {
                    "path_class": PathClass.STATE_LEDGER.value,
                    "workflow_class": _WORKFLOW.value,
                    "deployment_surface": _SURFACE.value,
                    "path": str(directory),
                }
            ]
        )
    )


def test_initialize_creates_file_if_absent(tmp_path: Path) -> None:
    """Acceptance #1/#2 — file absent ⇒ created empty, handle entry_count=0."""
    directory = tmp_path / "ledger"
    handle = initialize_jsonl_event_ledger(_resolver(directory), _WORKFLOW, _SURFACE)
    expected = directory / "state.jsonl"
    assert expected.exists()
    assert handle.exists is True
    assert handle.entry_count == 0
    assert handle.canonical_path == expected


def test_initialize_returns_handle_if_present(tmp_path: Path) -> None:
    """Acceptance #3 — file present ⇒ handle with line-counted entry_count."""
    directory = tmp_path / "ledger-dir"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.jsonl").write_text('{"a": 1}\n{"b": 2}\n')
    handle = initialize_jsonl_event_ledger(_resolver(directory), _WORKFLOW, _SURFACE)
    assert handle.entry_count == 2


def test_validate_returns_valid_for_well_formed_jsonl(tmp_path: Path) -> None:
    """Acceptance #4 — every non-empty line parses as JSON ⇒ VALID."""
    ledger = tmp_path / "state.jsonl"
    ledger.write_text('{"a": 1}\n{"b": 2}\n')
    handle = JsonlLedgerHandle(canonical_path=ledger, exists=True, entry_count=2)
    assert validate_jsonl_event_ledger_format(handle) is LedgerFormatValidationResult.VALID


def test_validate_returns_malformed_line_for_bad_jsonl(tmp_path: Path) -> None:
    """Acceptance #4 — a line failing JSON parse ⇒ MALFORMED_LINE."""
    ledger = tmp_path / "state.jsonl"
    ledger.write_text('{"a": 1}\nnot json at all\n')
    handle = JsonlLedgerHandle(canonical_path=ledger, exists=True, entry_count=2)
    assert validate_jsonl_event_ledger_format(handle) is LedgerFormatValidationResult.MALFORMED_LINE


def test_validate_returns_empty_for_zero_length_file(tmp_path: Path) -> None:
    """Acceptance #4 — zero-length file ⇒ EMPTY."""
    ledger = tmp_path / "state.jsonl"
    ledger.touch()
    handle = JsonlLedgerHandle(canonical_path=ledger, exists=True, entry_count=0)
    assert validate_jsonl_event_ledger_format(handle) is LedgerFormatValidationResult.EMPTY


def test_lifecycle_does_not_append_entries(tmp_path: Path) -> None:
    """Acceptance #5 — initialize on a present file does not modify contents."""
    directory = tmp_path / "ledger-dir"
    directory.mkdir(parents=True, exist_ok=True)
    ledger = directory / "state.jsonl"
    original = '{"a": 1}\n{"b": 2}\n'
    ledger.write_text(original)
    initialize_jsonl_event_ledger(_resolver(directory), _WORKFLOW, _SURFACE)
    assert ledger.read_text() == original
