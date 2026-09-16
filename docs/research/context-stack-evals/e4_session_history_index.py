"""E4 — Can an embedding index over session transcripts answer "when did we decide X?"

Corpus: user and assistant TEXT turns from every session transcript of this project
(tool results and tool inputs excluded), chunked at 1,500 characters.
Ground truth: memory files that record their origin session id. Query = the memory's
description; hit@5 = the origin session appears among the 5 nearest chunks' sessions.
Baseline: the memory files themselves, which is what a session has today.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import embed, top_k

# [LAW:one-source-of-truth] the checkout this file lives in, not a hard-coded home
REPO = Path(__file__).resolve().parents[3]
# Claude Code's per-project transcript dir
PROJ = Path.home() / ".claude" / "projects" / str(REPO).replace("/", "-")
OUT = Path(__file__).with_name("E4-results.md")
CHUNK = 1500


def turns(path: Path):
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or d.get("type") not in ("user", "assistant"):
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    yield b["text"]


def main() -> None:
    t0 = time.perf_counter()
    chunks, sess = [], []
    for f in sorted(PROJ.glob("*.jsonl")):
        sid = f.stem
        for t in turns(f):
            if len(t) < 80 or t.startswith("<"):
                continue
            for i in range(0, len(t), CHUNK):
                chunks.append(t[i : i + CHUNK])
                sess.append(sid)
    t_read = time.perf_counter() - t0
    vecs = embed(chunks)
    t_embed = time.perf_counter() - t0 - t_read

    mems = []
    for m in sorted((PROJ / "memory").glob("*.md")):
        txt = m.read_text(errors="replace")
        sid = re.search(r"originSessionId:\s*([0-9a-f-]{36})", txt)
        desc = re.search(r"^description:\s*(.+)$", txt, re.M)
        if sid and desc and sid.group(1) in set(sess):
            mems.append((m.name, desc.group(1).strip().strip('"'), sid.group(1)))
    rng = np.random.default_rng(7)
    sample = [mems[i] for i in rng.choice(len(mems), size=min(30, len(mems)), replace=False)]
    qv = embed([d for _, d, _ in sample])
    hits1 = hits5 = 0
    rows = []
    for (name, _desc, sid), q in zip(sample, qv, strict=False):
        nn = top_k(q, vecs, 5)
        found = [sess[i] for i, _ in nn]
        h5 = sid in found
        h1 = found[0] == sid
        hits5 += h5
        hits1 += h1
        rows.append(f"| {name[:48]} | {'✓' if h1 else ''} | {'✓' if h5 else ''} | {nn[0][1]:.2f} |")
    lines = [
        "# E4 — session-history index as a second memory tier",
        "",
        f"Transcripts: {len(set(sess))} sessions; text chunks: {len(chunks)}; "
        f"read {t_read:.0f}s, embed {t_embed:.0f}s.",
        f"Memories with a recoverable origin session: {len(mems)}; sampled {len(sample)} (seed 7).",
        "",
        f"**hit@1 = {hits1}/{len(sample)}, hit@5 = {hits5}/{len(sample)}** "
        f"— the memory's own description, "
        "used as the query, retrieves a chunk from the session that produced it.",
        "",
        "| memory | @1 | @5 | top sim |",
        "|---|---|---|---|",
        *rows,
        "",
        "Sessions by chunk count (top 5): "
        + ", ".join(f"{s[:8]}:{c}" for s, c in Counter(sess).most_common(5)),
    ]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT}; chunks={len(chunks)} hit@5={hits5}/{len(sample)}")


if __name__ == "__main__":
    main()
