"""E1 — Would an embedding index over the merge-gate ledger flag repeat findings at review time?

Method: embed every finding (observed_evidence + location). For the 20 most recent
findings, find the nearest EARLIER finding from a different arc. Emit the pair and the
cosine score for a human to judge as duplicate / same-class / unrelated. Also report, for
the whole ledger, how many findings have a prior neighbour above 0.90 and 0.95.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import embed

# [LAW:one-source-of-truth] the checkout this file lives in, not a hard-coded home
REPO = Path(__file__).resolve().parents[3]
LEDGER = REPO / ".harness" / "merge-gate-log.jsonl"
OUT = Path(__file__).with_name("E1-results.md")


def load() -> list[dict]:
    rows = []
    malformed = 0
    for line in LEDGER.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1  # [LAW:no-silent-failure] reported on stderr below, never hidden
            continue
        if d.get("record_kind", "finding") == "finding" and d.get("observed_evidence"):
            rows.append(d)
    print(f"E1: skipped {malformed} malformed ledger lines", file=sys.stderr)
    return rows


def main() -> None:
    rows = load()
    total_lines = len(LEDGER.read_text().splitlines())
    rows.sort(key=lambda r: str(r.get("ts", "")))
    texts = [f"{r.get('location', '')} :: {r['observed_evidence']}" for r in rows]
    vecs = embed(texts)
    arc = [str(r.get("arc_id") or r.get("pr") or "") for r in rows]
    n = len(rows)
    # nearest EARLIER finding from a different arc, for every finding
    best = np.full(n, -1.0)
    best_i = np.full(n, -1)
    for i in range(1, n):
        s = vecs[:i] @ vecs[i]
        for j in range(i):
            if arc[j] == arc[i]:
                s[j] = -1.0
        j = int(np.argmax(s))
        best[i], best_i[i] = s[j], j
    lines = [
        "# E1 — duplicate-finding detection over the merge-gate ledger",
        "",
        f"Ledger lines: {total_lines}; findings with evidence: {n}; embedder bge-small-en-v1.5.",
        "",
        "## Whole-ledger recurrence (nearest earlier finding from a different arc)",
        "",
        f"- findings with a prior neighbour ≥ 0.95: {(best >= 0.95).sum()} "
        f"({(best >= 0.95).mean():.1%})",
        f"- findings with a prior neighbour ≥ 0.90: {(best >= 0.90).sum()} "
        f"({(best >= 0.90).mean():.1%})",
        f"- findings with a prior neighbour ≥ 0.85: {(best >= 0.85).sum()} "
        f"({(best >= 0.85).mean():.1%})",
        "",
        "## The 20 most recent findings and their nearest earlier neighbour",
        "",
        "Judge each pair: DUP (same defect re-found), CLASS (same class, different instance), NO.",
        "",
    ]
    for i in range(n - 20, n):
        j = int(best_i[i])
        lines += [
            f"### {rows[i].get('ts', '')[:10]} · arc {arc[i]} · sim {best[i]:.3f} → "
            f"arc {arc[j]} ({rows[j].get('ts', '')[:10]})",
            "",
            f"- NEW: `{rows[i].get('location', '')}` — {rows[i]['observed_evidence'][:300]}",
            f"- PRIOR: `{rows[j].get('location', '')}` — {rows[j]['observed_evidence'][:300]}",
            "- JUDGMENT: ",
            "",
        ]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT}; n={n}; >=0.90: {(best >= 0.90).sum()}")


if __name__ == "__main__":
    main()
