"""E3 — Can passage embeddings find same-meaning-but-diverged text across the governance corpus?

Corpus: root CLAUDE.md, docs/governance/*.md (relocated bodies of CLAUDE.md sections), and
the Project_Workflow delta chain v1.8 → v1.19. Paragraphs are embedded; pairs from different
files with cosine in [0.85, 0.995) and non-identical normalised text are drift candidates.
Emits the count per band and the 15 highest-scoring candidates with both texts for a human
to classify: INTENTIONAL (delta chain / relocation), DRIFT (should be identical, is not), NO.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import embed

# [LAW:one-source-of-truth] the checkout this file lives in, not a hard-coded home
REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).with_name("E3-results.md")


def paragraphs(path: Path) -> list[str]:
    text = path.read_text(errors="replace")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in paras if 200 <= len(p) <= 2000 and not p.startswith("|")]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def main() -> None:
    files = [
        REPO / "CLAUDE.md",
        *sorted((REPO / "docs/governance").glob("*.md")),
        *sorted((REPO / "design-substrate").glob("Project_Workflow_v1_*.md")),
    ]
    items: list[tuple[str, str]] = []
    for f in files:
        items += [(str(f.relative_to(REPO)), p) for p in paragraphs(f)]
    vecs = embed([p for _, p in items])
    n = len(items)
    sims = vecs @ vecs.T
    cands = []
    for i in range(n):
        for j in range(i + 1, n):
            if items[i][0] == items[j][0]:
                continue
            s = float(sims[i, j])
            if 0.85 <= s < 0.995 and norm(items[i][1]) != norm(items[j][1]):
                cands.append((s, i, j))
    cands.sort(reverse=True)
    bands = {b: sum(1 for s, _, _ in cands if s >= b) for b in (0.95, 0.90, 0.85)}
    lines = [
        "# E3 — cross-spec drift candidates by passage embedding",
        "",
        f"Files: {len(files)}; paragraphs (200–2000 chars, non-table): {n}; "
        f"embedder bge-small-en-v1.5.",
        "",
        "## Candidate pairs (different files, non-identical text, cosine band)",
        "",
        *[f"- cosine ≥ {b}: {c} pairs" for b, c in bands.items()],
        "",
        "## Top 15 candidates — classify INTENTIONAL / DRIFT / NO",
        "",
    ]
    for s, i, j in cands[:15]:
        lines += [
            f"### sim {s:.3f} — `{items[i][0]}` ↔ `{items[j][0]}`",
            "",
            f"- A: {items[i][1][:400]}",
            f"- B: {items[j][1][:400]}",
            "- CLASS: ",
            "",
        ]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT}; paragraphs={n}; candidates>=0.90: {bands[0.90]}")


if __name__ == "__main__":
    main()
