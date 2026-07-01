"""JSONL event ledger file lifecycle — U-IS-05.

Implements C-IS-03 §3 (the JSONL event ledger sub-role of the combined git
tier). Manages file existence + structural validation of the JSONL event
ledger at workflow open / resume.

Scope boundary: this unit does **not** write or read ledger *entries* (C-IS-07
territory) and does **not** compute hashes (C-IS-06 territory). It validates
JSON-syntactic parseability only — not the six-field entry shape.

The U-IS-02 `resolve_path` primitive is a declared Input (IS plan v2.3 §2.2
U-IS-05 Inputs). The plan-grade signatures name only `workflow_class` /
`deployment_surface`; at implementation grade the `PathResolver` carrying that
input is threaded as the leading parameter.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.2 U-IS-05
(REVISED — R2); Spec_Information_Substrate_v1.md C-IS-03 §3.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from harness_core import DeploymentSurface, WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver


class LedgerFormatValidationResult(StrEnum):
    """Outcome of a JSONL event ledger structural validation (U-IS-05)."""

    VALID = "valid"
    EMPTY = "empty"
    MALFORMED_LINE = "malformed_line"
    IO_ERROR = "io_error"


class JsonlLedgerHandle(BaseModel):
    """A handle to a JSONL event ledger file — its canonical path + state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_path: Path
    exists: bool
    entry_count: int


STATE_LEDGER_JSONL_FILENAME = "state.jsonl"
"""Canonical filename for the JSONL event ledger within the STATE_LEDGER
directory (IS spec v1.3 §1 amendment, 2026-05-20)."""


def initialize_jsonl_event_ledger(
    resolver: PathResolver,
    workflow_class: WorkloadClass,
    deployment_surface: DeploymentSurface,
) -> JsonlLedgerHandle:
    """Resolve and open the JSONL event ledger for a workflow (U-IS-05).

    Resolves the canonical directory via `resolve_path(PathClass.STATE_LEDGER,
    workflow_class, deployment_surface)` per IS spec v1.3 §1 amendment
    (resolves to a directory; JSONL file at `<dir>/state.jsonl`). Creates the
    directory if absent, then the file. A file absent at that path is created
    empty (`entry_count=0`); a file present is line-counted and returned
    unmodified (acceptance #2/#3/#5).
    """
    directory = resolver.resolve_path(PathClass.STATE_LEDGER, workflow_class, deployment_surface)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_LEDGER_JSONL_FILENAME
    if not path.exists():
        path.touch()
        return JsonlLedgerHandle(canonical_path=path, exists=True, entry_count=0)
    entry_count = sum(1 for line in path.read_text().splitlines() if line.strip())
    return JsonlLedgerHandle(canonical_path=path, exists=True, entry_count=entry_count)


def validate_jsonl_event_ledger_format(
    handle: JsonlLedgerHandle,
) -> LedgerFormatValidationResult:
    """Structurally validate a JSONL event ledger file (U-IS-05).

    `EMPTY` for a zero-length file; `VALID` if every non-empty line parses as
    JSON; `MALFORMED_LINE` if any non-empty line fails JSON parse; `IO_ERROR`
    on a filesystem access failure (acceptance #4). Entry-shape validation is
    NOT performed — only JSON-syntactic parseability (acceptance #6).
    """
    try:
        if handle.canonical_path.stat().st_size == 0:
            return LedgerFormatValidationResult.EMPTY
        text = handle.canonical_path.read_text()
    except OSError:
        return LedgerFormatValidationResult.IO_ERROR
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return LedgerFormatValidationResult.MALFORMED_LINE
    return LedgerFormatValidationResult.VALID
