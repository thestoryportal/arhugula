"""`harness-inspect` admin CLI — read-only state-ledger summary (U-RT-47).

Per `Spec_Harness_Runtime_v1.md` v1.1 §13 C-RT-13:

> `harness-inspect` (read-only). Opens state ledger (`.harness/state.jsonl`)
> and collector sqlite (path resolved via PATH_CLASS_REGISTRY per C-IS-01
> §1) in read-only mode. No writes. Dumps a summary: ledger head hash +
> last N entries (N from CLI flag, default 10); last N spans from collector
> (default 10); current cost-attribution rollup if available. Runs against
> a stopped harness; does not modify any state. Exits 0 on success;
> nonzero on file-not-found or read-error.

**AC mapping per session-3 atomic decomposition** (U-RT-47 acceptance:
"runs against a stopped harness; returns ledger head and last N spans"):

- runs against stopped harness — LAND (filesystem read, no process)
- returns ledger head — LAND (last entry `response_hash.hex()`)
- returns last N spans — **STRUCK**; extends
  `[[fork-trace-storage-pathclass-gap]]` (collector sqlite is in-memory-only
  per U-RT-30 PARTIAL-LAND); output names the gap explicitly

**Read-only invariant** (C-RT-13 invariant #1): this module MUST NOT
write to any file. The read path uses `Path.read_text()` + direct
`JsonlLedgerHandle` construction (no bootstrap-time side effects from
`initialize_jsonl_event_ledger`). Tested via chmod-readonly fixture.

**Framework discipline** (spec §13 deferred-to-discretion): argparse
stdlib only. No click / typer per workspace-root CLAUDE.md §3.2
framework-pull discipline.

**Cost-attribution rollup** (spec §13): "if available" — at HEAD the cost
chain is stateless-by-design per U-RT-31 (
`.harness/class_3_drift_u_rt_45_cost_chain_stateless.md`). Reported as
N/A in the summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import StateLedgerEntry
from harness_is.state_ledger_write import read_ledger

__all__ = ["build_parser", "main"]


# ---------------------------------------------------------------------------
# Defaults.
# ---------------------------------------------------------------------------


_DEFAULT_LEDGER_PATH = Path(".harness/state.jsonl")
_DEFAULT_LAST_N = 10
_EXIT_OK = 0
_EXIT_INSPECT_PATH = 2  # RT-FAIL-INSPECT-PATH per C-RT-13


_SPANS_UNAVAILABLE_REASON = (
    "in-memory collector storage at HEAD; on-disk sqlite path resolution "
    "STRUCK per [[fork-trace-storage-pathclass-gap]] (U-RT-30 PARTIAL-LAND)"
)

_COST_UNAVAILABLE_REASON = (
    "cost-attribution chain is stateless-by-design per U-RT-31; spec §10 "
    "step 2 over-specification documented at "
    "class_3_drift_u_rt_45_cost_chain_stateless.md"
)


# ---------------------------------------------------------------------------
# argparse.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Factored for unit-testability."""
    parser = argparse.ArgumentParser(
        prog="harness-inspect",
        description=(
            "Read-only state-ledger inspector (Track A admin stub per "
            "Spec_Harness_Runtime_v1.md §13 C-RT-13)."
        ),
    )
    parser.add_argument(
        "--ledger-path",
        type=Path,
        default=_DEFAULT_LEDGER_PATH,
        help=f"Path to the state ledger JSONL (default: {_DEFAULT_LEDGER_PATH}).",
    )
    parser.add_argument(
        "--collector-path",
        type=Path,
        default=None,
        help=(
            "Path to the collector sqlite — accepted but unused at HEAD "
            "(in-memory storage only; fork-trace-storage-pathclass-gap)."
        ),
    )
    parser.add_argument(
        "--last-n",
        type=int,
        default=_DEFAULT_LAST_N,
        help=f"Number of recent entries to dump (default: {_DEFAULT_LAST_N}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output. Default is human-readable.",
    )
    return parser


# ---------------------------------------------------------------------------
# Read path.
# ---------------------------------------------------------------------------


def _read_entries(ledger_path: Path) -> list[StateLedgerEntry]:
    """Read every entry from `ledger_path` via the IS reader.

    Constructs a `JsonlLedgerHandle` directly — no
    `initialize_jsonl_event_ledger` (which would create the file if absent
    and `path.parent.mkdir(parents=True, exist_ok=True)`, violating the
    read-only invariant). At HEAD `read_ledger` only reads
    `canonical_path.read_text()`.
    """
    text = ledger_path.read_text()
    entry_count = sum(1 for line in text.splitlines() if line.strip())
    handle = JsonlLedgerHandle(
        canonical_path=ledger_path,
        exists=True,
        entry_count=entry_count,
    )
    return read_ledger(handle)


def _entry_head_hash(entry: StateLedgerEntry) -> str:
    """Lowercase hex of `entry.response_hash` (the C-IS-06 chain head)."""
    return entry.response_hash.hex()


def _entry_summary(entry: StateLedgerEntry) -> dict[str, Any]:
    """One-entry summary dict — JSON-serializable + human-readable source.

    `Timestamp` is `datetime` per C-IS-05 §5; serialized as ISO-8601 string.
    """
    return {
        "action_id": str(entry.action_id),
        "idempotency_key": str(entry.idempotency_key),
        "actor_class": entry.actor.actor_class.value,
        "actor_id": entry.actor.actor_id,
        "timestamp": entry.timestamp.isoformat(),
        "response_hash": entry.response_hash.hex(),
        "prior_event_hash": entry.prior_event_hash.hex(),
    }


# ---------------------------------------------------------------------------
# Output formatters.
# ---------------------------------------------------------------------------


def _format_human(
    *,
    ledger_path: Path,
    entries: list[StateLedgerEntry],
    last_n: int,
) -> str:
    lines: list[str] = []
    lines.append("harness-inspect — read-only summary")
    lines.append(f"  ledger:        {ledger_path}")
    lines.append(f"  total_entries: {len(entries)}")

    if not entries:
        lines.append("  head_hash:     (genesis — no entries)")
    else:
        lines.append(f"  head_hash:     {_entry_head_hash(entries[-1])}")

    lines.append("")
    lines.append(f"Last {min(last_n, len(entries))} entries:")
    for entry in entries[-last_n:]:
        s = _entry_summary(entry)
        lines.append(
            f"  - action_id={s['action_id']!s} "
            f"actor={s['actor_class']}/{s['actor_id']} "
            f"ts={s['timestamp']} "
            f"response_hash={s['response_hash'][:16]}…"
        )

    lines.append("")
    lines.append(f"Spans: N/A — {_SPANS_UNAVAILABLE_REASON}")
    lines.append(f"Cost rollup: N/A — {_COST_UNAVAILABLE_REASON}")
    return "\n".join(lines) + "\n"


def _format_json(
    *,
    ledger_path: Path,
    entries: list[StateLedgerEntry],
    last_n: int,
) -> str:
    head = _entry_head_hash(entries[-1]) if entries else None
    payload: dict[str, Any] = {
        "ledger_path": str(ledger_path),
        "total_entries": len(entries),
        "head_hash": head,
        "entries": [_entry_summary(e) for e in entries[-last_n:]],
        "spans": None,
        "spans_unavailable_reason": _SPANS_UNAVAILABLE_REASON,
        "cost_rollup": None,
        "cost_rollup_unavailable_reason": _COST_UNAVAILABLE_REASON,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`harness-inspect` entry point.

    Returns the process exit code (0 success; 2 RT-FAIL-INSPECT-PATH).
    Wired by `[project.scripts]` in `harness-runtime/pyproject.toml`.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    ledger_path: Path = args.ledger_path
    last_n: int = args.last_n

    try:
        entries = _read_entries(ledger_path)
    except FileNotFoundError:
        print(
            f"harness-inspect: RT-FAIL-INSPECT-PATH — ledger not found: {ledger_path}",
            file=sys.stderr,
        )
        return _EXIT_INSPECT_PATH
    except OSError as exc:
        print(
            f"harness-inspect: RT-FAIL-INSPECT-PATH — read error on {ledger_path}: {exc}",
            file=sys.stderr,
        )
        return _EXIT_INSPECT_PATH

    if args.json:
        output = _format_json(ledger_path=ledger_path, entries=entries, last_n=last_n)
    else:
        output = _format_human(ledger_path=ledger_path, entries=entries, last_n=last_n)

    sys.stdout.write(output)
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover — invoked via console_script
    raise SystemExit(main())
