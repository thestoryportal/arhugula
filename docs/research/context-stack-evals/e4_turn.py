"""Print one transcript turn by (session id, turn index) — the enumeration E4b indexes.

Ground truth for E4b is authored against THIS numbering, so the index and the answer
key cannot disagree about what "turn 107" is ([LAW:one-source-of-truth]: `turns()` is
imported from the E4 script, never re-implemented here).

usage: e4_turn.py <session-id> <turn-index> [--around N]   (N neighbouring turns each side)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e4_session_history_index import PROJ, turns


def indexed_turns(session: str) -> list[tuple[int, str]]:
    """(turn index, text) for every indexable text block of one session, in E4 order."""
    out = []
    for i, t in enumerate(turns(PROJ / f"{session}.jsonl")):
        if len(t) >= 80 and not t.startswith("<"):
            out.append((i, t))
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    session, want = sys.argv[1], int(sys.argv[2])
    around = int(sys.argv[sys.argv.index("--around") + 1]) if "--around" in sys.argv else 0
    hits = [(i, t) for i, t in indexed_turns(session) if abs(i - want) <= around]
    if not hits:
        print(f"no indexable turn {want} in session {session}", file=sys.stderr)
        return 1
    for i, t in hits:
        print(f"===== turn {i} ({len(t)} chars) =====")
        print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
