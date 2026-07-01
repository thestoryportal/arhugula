"""U-RT-47 — `harness-inspect` admin CLI tests.

Acceptance criteria per Phase 2 Session 3 atomic decomposition L10 U-RT-47 +
spec §13 C-RT-13:

- AC #1: runs against a stopped harness — LAND
- AC #2: returns ledger head — LAND
- AC #3: returns last N spans — STRUCK per `[[fork-trace-storage-pathclass-gap]]`

C-RT-13 invariant: `harness-inspect` MUST NOT write to any file.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import (
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)
from harness_is.state_ledger_write import (
    EntryPayload,
    WriteKey,
    append_ledger_entry,
)
from harness_runtime.admin.inspect import build_parser, main

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="harness-runtime")


def _write_n_entries(ledger_path: Path, n: int) -> list[StateLedgerEntry]:
    """Build a ledger at `ledger_path` with `n` chain-valid entries.

    Uses the real `append_ledger_entry` API so chain construction matches
    production semantics.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.touch()
    handle = JsonlLedgerHandle(
        canonical_path=ledger_path,
        exists=True,
        entry_count=0,
    )
    entries: list[StateLedgerEntry] = []
    for i in range(n):
        payload = EntryPayload(
            action_id=Identifier(f"action-{i}"),
            idempotency_key=Identifier(f"idem-{i}"),
            actor=_ACTOR,
            timestamp=datetime(2026, 5, 20, 12, 0, i, tzinfo=UTC),
        )
        write_key = WriteKey(
            thread_id=Identifier(f"thread-{i}"),
            step_id=Identifier(f"step-{i}"),
            idempotency_key=Identifier(f"idem-{i}"),
        )
        append_ledger_entry(handle, payload, write_key)
        # Re-read handle counter so chain construction picks up prior entries.
        new_count = sum(1 for line in ledger_path.read_text().splitlines() if line.strip())
        handle = JsonlLedgerHandle(
            canonical_path=ledger_path,
            exists=True,
            entry_count=new_count,
        )
    # Final read for test-side reference.
    from harness_is.state_ledger_write import read_ledger

    entries.extend(read_ledger(handle))
    return entries


# ---------------------------------------------------------------------------
# Parser.
# ---------------------------------------------------------------------------


def test_parser_defaults() -> None:
    ns = build_parser().parse_args([])
    assert ns.ledger_path == Path(".harness/state.jsonl")
    assert ns.last_n == 10
    assert ns.json is False
    assert ns.collector_path is None


def test_parser_flags() -> None:
    ns = build_parser().parse_args(["--ledger-path", "/tmp/x.jsonl", "--last-n", "3", "--json"])
    assert ns.ledger_path == Path("/tmp/x.jsonl")
    assert ns.last_n == 3
    assert ns.json is True


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_inspect_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "state.jsonl"
    entries = _write_n_entries(ledger, 2)

    code = main(["--ledger-path", str(ledger)])
    out = capsys.readouterr().out

    assert code == 0
    assert "harness-inspect" in out
    assert entries[-1].response_hash.hex() in out


def test_inspect_reports_head_hash_lowercase_hex(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = tmp_path / "state.jsonl"
    entries = _write_n_entries(ledger, 3)

    main(["--ledger-path", str(ledger)])
    out = capsys.readouterr().out

    head = entries[-1].response_hash.hex()
    assert head in out
    assert head == head.lower()  # lowercase invariant
    assert len(head) == 64  # SHA-256


def test_inspect_default_last_n_is_10(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "state.jsonl"
    _write_n_entries(ledger, 15)

    main(["--ledger-path", str(ledger)])
    out = capsys.readouterr().out

    # First 5 entries (action-0..action-4) should NOT appear; entries 5..14 should.
    assert "action-0 " not in out and "action-0'" not in out
    assert "action-14" in out


def test_inspect_respects_last_n_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "state.jsonl"
    _write_n_entries(ledger, 5)

    main(["--ledger-path", str(ledger), "--last-n", "3"])
    out = capsys.readouterr().out

    # action-2, action-3, action-4 only (last 3 of 5).
    assert "action-4" in out
    assert "action-3" in out
    assert "action-2" in out
    assert "action-1" not in out
    assert "action-0" not in out


# ---------------------------------------------------------------------------
# JSON output.
# ---------------------------------------------------------------------------


def test_inspect_json_flag_outputs_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "state.jsonl"
    _write_n_entries(ledger, 2)

    main(["--ledger-path", str(ledger), "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert payload["total_entries"] == 2
    assert isinstance(payload["head_hash"], str)
    assert len(payload["head_hash"]) == 64
    assert payload["spans"] is None
    assert "fork-trace-storage-pathclass-gap" in payload["spans_unavailable_reason"]
    assert payload["cost_rollup"] is None
    assert len(payload["entries"]) == 2


def test_inspect_json_empty_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "state.jsonl"
    ledger.write_text("")

    main(["--ledger-path", str(ledger), "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert payload["total_entries"] == 0
    assert payload["head_hash"] is None
    assert payload["entries"] == []


# ---------------------------------------------------------------------------
# Human output references the struck spans surface.
# ---------------------------------------------------------------------------


def test_inspect_human_output_names_struck_spans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "state.jsonl"
    _write_n_entries(ledger, 1)

    main(["--ledger-path", str(ledger)])
    out = capsys.readouterr().out

    assert "Spans:" in out
    assert "fork-trace-storage-pathclass-gap" in out
    assert "U-RT-30 PARTIAL-LAND" in out


def test_inspect_human_output_names_struck_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "state.jsonl"
    _write_n_entries(ledger, 1)

    main(["--ledger-path", str(ledger)])
    out = capsys.readouterr().out

    assert "Cost rollup:" in out
    assert "U-RT-31" in out


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_inspect_missing_path_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "does-not-exist.jsonl"

    code = main(["--ledger-path", str(ledger)])

    assert code == 2
    err = capsys.readouterr().err
    assert "RT-FAIL-INSPECT-PATH" in err


def test_inspect_empty_ledger_reports_genesis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "state.jsonl"
    ledger.write_text("")

    code = main(["--ledger-path", str(ledger)])
    out = capsys.readouterr().out

    assert code == 0
    assert "genesis" in out.lower()


# ---------------------------------------------------------------------------
# Read-only invariant (C-RT-13 invariant #1).
# ---------------------------------------------------------------------------


def test_inspect_succeeds_against_readonly_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """chmod 0o444 the ledger; inspect must still read it cleanly."""
    ledger = tmp_path / "state.jsonl"
    _write_n_entries(ledger, 2)
    # Strip write bits.
    ledger.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        code = main(["--ledger-path", str(ledger)])
        assert code == 0
    finally:
        # Restore to clean up tmp_path.
        ledger.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_inspect_does_not_open_anything_for_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sentinel-monkey-patch `Path.open` and `os.open` to detect write attempts."""
    ledger = tmp_path / "state.jsonl"
    _write_n_entries(ledger, 1)

    from typing import Any

    real_path_open: Any = Path.open
    real_os_open: Any = os.open
    write_attempts: list[str] = []

    def _spy_path_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(c in mode for c in ("w", "a", "x", "+")):
            write_attempts.append(f"Path.open({self}, mode={mode!r})")
        return real_path_open(self, mode, *args, **kwargs)

    def _spy_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        if flags & write_flags:
            write_attempts.append(f"os.open({path}, flags={flags})")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(Path, "open", _spy_path_open)
    monkeypatch.setattr(os, "open", _spy_os_open)

    code = main(["--ledger-path", str(ledger)])

    assert code == 0
    assert write_attempts == [], f"unexpected write attempts: {write_attempts}"


# ---------------------------------------------------------------------------
# pyproject scripts entry.
# ---------------------------------------------------------------------------


def test_pyproject_scripts_entry_present() -> None:
    """`harness-inspect = harness_runtime.admin.inspect:main` activated."""
    # Locate harness-runtime/pyproject.toml relative to this test file.
    here = Path(__file__).resolve()
    pyproject = here.parent.parent / "pyproject.toml"
    assert pyproject.exists()
    text = pyproject.read_text()
    assert 'harness-inspect    = "harness_runtime.admin.inspect:main"' in text, (
        f"harness-inspect script entry missing in {pyproject}"
    )


# ---------------------------------------------------------------------------
# Entry point via `python -m`-style direct call (sanity that main is importable).
# ---------------------------------------------------------------------------


def test_main_module_callable() -> None:
    import inspect as stdlib_inspect

    from harness_runtime.admin import inspect as admin_inspect

    assert callable(admin_inspect.main)
    sig = stdlib_inspect.signature(admin_inspect.main)
    params = list(sig.parameters.keys())
    assert params == ["argv"]
