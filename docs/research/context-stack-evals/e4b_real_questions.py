"""E4b — session-history index queried with operator-shaped questions (handoff-s2 §2 C).

E4 queried the index with each memory's own one-line description and scored the origin
SESSION (hit@1 6/30, hit@5 14/30). This run asks the questions an operator would actually
type, and scores both the session and the exact TURN where the fact was stated.

Inputs: `E4b-questions.json` beside this file — a list of
    {"id", "question", "session", "turn", "quote", "memory"}
where `turn` is the index `e4_turn.py` prints and `quote` is ≥ 12 consecutive words of that
turn (checked here before anything is embedded — a wrong key is a loud exit, never a
silently wrong score).

Verdict threshold, written before the run (handoff-s2 §2 C asks for it up front):
    session hit@1 ≥ 10/20 AND turn hit@5 ≥ 8/20  →  memory entries may become POINTERS
    (session + turn) with the index as the recall tier;
    session hit@5 ≥ 10/20 but below the line above →  the index is an AUGMENT only —
    memory stays the authority, the index answers "where did that come from";
    below both →  not adopted.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import embed, top_k
from e4_session_history_index import CHUNK, PROJ, turns

HERE = Path(__file__).parent
QUESTIONS = HERE / "E4b-questions.json"
OUT = HERE / "E4b-results.md"
K = 5


def load_questions() -> list[dict]:
    qs = json.loads(QUESTIONS.read_text())
    bad = []
    for q in qs:
        path = PROJ / f"{q['session']}.jsonl"
        texts = [
            t
            for i, t in enumerate(turns(path))
            if i == q["turn"] and len(t) >= 80 and not t.startswith("<")
        ]
        if not texts or q["quote"] not in texts[0]:
            bad.append(q["id"])
    if bad:  # [LAW:no-silent-failure] a wrong answer key must not become a wrong score
        raise SystemExit(f"E4b: quote not found at (session, turn) for {bad}")
    return qs


def build_index() -> tuple[list[str], list[tuple[str, int]]]:
    chunks, where = [], []
    for f in sorted(PROJ.glob("*.jsonl")):
        sid = f.stem
        for i, t in enumerate(turns(f)):
            if len(t) < 80 or t.startswith("<"):
                continue
            for j in range(0, len(t), CHUNK):
                chunks.append(t[j : j + CHUNK])
                where.append((sid, i))
    return chunks, where


def main() -> None:
    qs = load_questions()
    t0 = time.perf_counter()
    chunks, where = build_index()
    vecs = embed(chunks)
    t_index = time.perf_counter() - t0
    qv = embed([q["question"] for q in qs])
    s1 = s5 = t1 = t5 = 0
    rows = []
    for q, v in zip(qs, qv, strict=True):
        nn = top_k(v, vecs, K)
        found = [where[i] for i, _ in nn]
        sess_hit1 = found[0][0] == q["session"]
        sess_hit5 = any(s == q["session"] for s, _ in found)
        turn_hit1 = found[0] == (q["session"], q["turn"])
        turn_hit5 = (q["session"], q["turn"]) in found
        s1 += sess_hit1
        s5 += sess_hit5
        t1 += turn_hit1
        t5 += turn_hit5
        marks = ["✓" if hit else "" for hit in (sess_hit1, sess_hit5, turn_hit1, turn_hit5)]
        rows.append(
            f"| {q['id']} | {q['question'][:70]} | " + " | ".join(marks) + f" | {nn[0][1]:.2f} |"
        )
    n = len(qs)
    if s1 >= 10 and t5 >= 8:
        verdict = (
            "POINTERS — memory entries may carry (session, turn) anchors; index is the recall tier."
        )
    elif s5 >= 10:
        verdict = (
            "AUGMENT — memory stays the authority; the index answers 'where did that come from'."
        )
    else:
        verdict = "NOT ADOPTED."
    lines = [
        "# E4b — session-history index, operator-shaped questions",
        "",
        f"Index: {len({s for s, _ in where})} sessions, {len(chunks)} chunks, "
        f"built in {t_index:.0f}s. Questions: {n}, ground truth = (session, turn) "
        "verified by verbatim quote before the run.",
        "",
        "Threshold (written before the run): POINTERS iff session hit@1 ≥ 10/20 and "
        "turn hit@5 ≥ 8/20; AUGMENT iff session hit@5 ≥ 10/20; else not adopted.",
        "",
        f"**session hit@1 = {s1}/{n}, session hit@5 = {s5}/{n}; "
        f"turn hit@1 = {t1}/{n}, turn hit@5 = {t5}/{n}**",
        "",
        f"**Verdict: {verdict}**",
        "",
        "| id | question | s@1 | s@5 | t@1 | t@5 | top sim |",
        "|---|---|---|---|---|---|---|",
        *rows,
    ]
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT}; session@1={s1}/{n} session@5={s5}/{n} turn@1={t1}/{n} turn@5={t5}/{n}")


if __name__ == "__main__":
    main()
