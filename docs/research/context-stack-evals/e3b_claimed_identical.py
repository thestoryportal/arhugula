"""E3b — drift scored only where the text itself claims identity (handoff-s2 §2 E).

E3 ranked every cross-file paragraph pair by cosine and found expected structure first
(version-token deltas, shared relocation headers). This run defines drift as: a passage that
a `§`-cite or a "relocated byte-verbatim" header CLAIMS identical to another, and is not —
and scores only those pairs. Four claim sets exist in this corpus:

  A. Governance packs: "*Relocated BYTE-VERBATIM from Root `CLAUDE.md` §…*" — the § set is
     taken from the CLAIM, not from what the pack happens to contain; a claimed § the pack
     lacks is drift. Each present § is compared byte-exact (heading line included) and,
     separately, whitespace-normalised; then pack § at HEAD vs at relocation.
  B. Root pointer stubs: root `CLAUDE.md` keeps one sentence per relocated subsection with
     "Body at `docs/governance/<pack>.md §N.N`". No identity is claimed; consistency is.
     Scored by embedding cosine AND by lexical containment; the agreement of the two
     rankings is computed (Spearman rho, bottom-5 overlap), not asserted.
  C. `.harness/claude-artifact-pointers.md`: "split byte-preservingly by family" — the
     pre-split file's paragraphs vs the union of the family files at the split commit,
     then line level for anything missing, scaffolding separated from lineage.
  D. Project_Workflow deltas: each "Sections preserved verbatim" block (or, for a delta
     without the block, its inline PRESERVED VERBATIM sentences) names §s the delta must
     not re-table; a named § that the same delta re-tables as a heading is a violation.

Everything but B is a normalised diff. B is the only question an embedding can answer.
Commits are pinned (found by `git log --diff-filter=A`, 2026-09-16), read at the boundary
via `git show` ([LAW:effects-at-boundaries]); comparison is pure over the strings.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# [LAW:one-source-of-truth] the checkout this file lives in, not a hard-coded home
REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).with_name("E3b-results.md")

RELOCATION = "0f05512ca"  # U-CTX-13 / R-CTX-1 Arc 5: packs created, root bodies removed
RELOCATION_PARENT = "f30eae667"
SPLIT = "50771f16c"  # U-CTX-06 / R-CTX-1 Arc 2: artifact-pointers split by family
PACKS = "docs/governance"
HEADING = re.compile(r"^(#{2,4}) §?(\d+(?:\.\d+)*)\.? (.*)$")
SECNUM = re.compile(r"§(\d+(?:\.\d+)*)")
RANGE = re.compile(r"§(\d+(?:\.\d+)*)[–-]§(\d+(?:\.\d+)*)")


def git_show(ref: str, path: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{ref}:{path}"], capture_output=True, text=True
    )
    if proc.returncode != 0:  # [LAW:no-silent-failure] a missing historical file is loud
        raise SystemExit(f"git show {ref}:{path} failed: {proc.stderr.strip()}")
    return proc.stdout


def sections(text: str) -> dict[str, str]:
    """§-number → the section's raw text, HEADING LINE INCLUDED, up to the next heading of
    any level. Raw so that a byte-exact comparison is a byte-exact comparison."""
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        m = HEADING.match(line)
        if m:
            if cur is not None:
                out[cur] = "".join(buf)
            cur, buf = m.group(2), [line]
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "".join(buf)
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def diff_stat(a: str, b: str) -> tuple[int, int]:
    """(lines only in a, lines only in b) after whitespace normalisation per line."""
    la = [norm(x) for x in a.splitlines() if norm(x)]
    lb = [norm(x) for x in b.splitlines() if norm(x)]
    sm = difflib.SequenceMatcher(a=la, b=lb, autojunk=False)
    only_a = only_b = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            only_a += i2 - i1
        if tag in ("replace", "insert"):
            only_b += j2 - j1
    return only_a, only_b


def claimed_sections(claim: str) -> list[str]:
    """§ numbers a claim names, ranges like `§10.1–§10.9` expanded on the last component."""
    nums: list[str] = []
    for lo, hi in RANGE.findall(claim):
        head_lo, last_lo = lo.rsplit(".", 1) if "." in lo else ("", lo)
        head_hi, last_hi = hi.rsplit(".", 1) if "." in hi else ("", hi)
        if head_lo == head_hi:
            for k in range(int(last_lo), int(last_hi) + 1):
                nums.append(f"{head_lo}.{k}" if head_lo else str(k))
    nums += SECNUM.findall(RANGE.sub(" ", claim))
    seen: list[str] = []
    for n in nums:
        if n not in seen:
            seen.append(n)
    return sorted(seen, key=lambda k: [int(x) for x in k.split(".")])


def claim_a() -> tuple[list[str], int, int]:
    """Returns (report lines, claimed-section count, drift count)."""
    root_then = sections(git_show(RELOCATION_PARENT, "CLAUDE.md"))
    lines = [
        "## A. Packs relocated byte-verbatim (the claimed § set, tested at the relocation)",
        "",
        "| pack | claimed § | at relocation | since (lines −/+) |",
        "|---|---|---|---|",
    ]
    claimed = drift = 0
    for pack in sorted((REPO / PACKS).glob("*.md")):
        head = git_show(RELOCATION, f"{PACKS}/{pack.name}")
        m = re.search(r"Relocated BYTE-VERBATIM from Root `CLAUDE.md` (.+?) by U-CTX-13", head)
        if not m:
            continue
        then_secs = sections(head)
        now_secs = sections(pack.read_text())
        for num in claimed_sections(m.group(1)):
            claimed += 1
            if num not in then_secs:
                drift += 1
                lines.append(f"| {pack.name} | §{num} | **CLAIMED BUT ABSENT from the pack** | — |")
                continue
            if num not in root_then:
                drift += 1
                lines.append(f"| {pack.name} | §{num} | **root had no §{num}** | — |")
                continue
            if root_then[num] == then_secs[num]:
                at = "byte-identical"
            elif norm(root_then[num]) == norm(then_secs[num]):
                at = "identical after whitespace normalisation (NOT byte-identical)"
            else:
                d = diff_stat(root_then[num], then_secs[num])
                drift += 1
                at = f"**DIFFERS {d[0]}/{d[1]}**"
            since = diff_stat(then_secs[num], now_secs.get(num, ""))
            lines.append(f"| {pack.name} | §{num} | {at} | {since[0]}/{since[1]} |")
    return [*lines, ""], claimed, drift


def spearman(x: list[float], y: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = float(pos)
        return r

    rx, ry = ranks(x), ranks(y)
    n = len(x)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry, strict=True))
    return 1 - 6 * d2 / (n * (n * n - 1)) if n > 2 else float("nan")


def claim_b() -> tuple[list[str], int, float, int]:
    """Returns (report lines, stubs below both thresholds, rho, bottom-5 overlap)."""
    from common import embed  # the one embedding use in this evaluation

    root = (REPO / "CLAUDE.md").read_text()
    pairs: list[tuple[str, str, str, str]] = []  # (stub, pack, §, section text)
    for line in root.splitlines():
        for pack, num in re.findall(r"`docs/governance/([a-z-]+\.md)` ?§(\d+(?:\.\d+)*)", line):
            body = sections((REPO / PACKS / pack).read_text()).get(num)
            if body is not None:
                pairs.append((line.strip(), pack, num, body))
    sv, bv = embed([p[0] for p in pairs]), embed([p[3] for p in pairs])
    cos = (sv * bv).sum(axis=1)

    def toks(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z][a-z0-9_-]{3,}", s.lower())}

    rows = []
    for (stub, pack, num, body), c in zip(pairs, cos, strict=True):
        st = toks(stub) - {"body", "docs", "governance", "detail", "full", "links"}
        contain = len(st & toks(body)) / max(1, len(st))
        rows.append((float(c), contain, pack, num, stub))
    by_cos = sorted(rows, key=lambda r: r[0])
    by_con = sorted(rows, key=lambda r: r[1])
    rho = spearman([r[0] for r in rows], [r[1] for r in rows])
    overlap = len({r[3] for r in by_cos[:5]} & {r[3] for r in by_con[:5]})
    lines = [
        f"## B. Root pointer stubs vs the pack section they name ({len(rows)} pairs)",
        "",
        "Lowest embedding cosine first; `contain` = share of the stub's content tokens present "
        "in the section.",
        "",
        "| cosine | contain | pack | § | stub (truncated) |",
        "|---|---|---|---|---|",
    ]
    for c, contain, pack, num, stub in by_cos:
        lines.append(f"| {c:.2f} | {contain:.2f} | {pack} | §{num} | {stub[:90]} |")
    both = sum(1 for r in rows if r[0] < 0.6 and r[1] < 0.5)
    lines += [
        "",
        f"Rows with cosine < 0.60 AND contain < 0.50: {both}. Rank agreement of the two "
        f"scores: Spearman rho = {rho:.2f}; bottom-5 sets share {overlap} of 5 rows "
        f"(cosine bottom-5: {', '.join('§' + r[3] for r in by_cos[:5])}; containment "
        f"bottom-5: {', '.join('§' + r[3] for r in by_con[:5])}).",
        "",
    ]
    return lines, both, rho, overlap


def claim_c() -> tuple[list[str], int]:
    """Returns (report lines, lineage lines lost)."""
    pre = git_show(f"{SPLIT}^", ".harness/claude-artifact-pointers.md")
    fam = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "--name-only", SPLIT, ".harness/artifact-pointers/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    post = "\n\n".join(git_show(SPLIT, f) for f in fam)

    def paras(t: str) -> list[str]:
        return [norm(p) for p in re.split(r"\n\s*\n", t) if norm(p)]

    a, b = paras(pre), paras(post)
    missing = [p for p in a if p not in set(b)]
    extra = [p for p in b if p not in set(a)]
    union_lines = {norm(x) for x in post.splitlines() if norm(x)}
    lost_lines = sorted(
        {
            norm(raw)
            for p in missing
            for raw in pre.splitlines()
            if norm(raw) and norm(raw) in p and norm(raw) not in union_lines
        }
    )
    # The source's own scaffolding (title, section headings, its preamble paragraph) is what
    # the stub file replaced; only a lost LINEAGE line would falsify the claim.
    scaffold = {norm(x) for x in pre.splitlines() if norm(x).startswith("#")} | {
        norm(x) for x in a[1].split(". ") if norm(x)
    }
    lost_lineage = [x for x in lost_lines if x not in scaffold and x.rstrip(".") not in scaffold]
    lines = [
        "## C. Artifact pointers split byte-preservingly by family",
        "",
        f"Pre-split file at {SPLIT}^: {len(pre):,} B, {len(a)} paragraphs. "
        f"Family files at {SPLIT}: {len(fam)} files, {len(post):,} B, {len(b)} paragraphs.",
        f"Paragraphs of the original missing from the union: **{len(missing)}**; "
        f"paragraphs in the union not in the original: {len(extra)}.",
        f"Lines of those missing paragraphs absent from the union at line level: "
        f"**{len(lost_lines)}**, of which lineage (not the source's own title/headings/"
        f"preamble that the stub replaced): **{len(lost_lineage)}**. A re-cut table keeps "
        "its rows; a lost lineage row would be drift.",
        "",
    ]
    for p in missing[:8]:
        lines.append(f"- MISSING paragraph: {p[:160]}")
    for x in lost_lines[:12]:
        lines.append(f"- LOST line: {x[:200]}")
    for p in extra[:4]:
        lines.append(f"- EXTRA: {p[:160]}")
    return [*lines, ""], len(lost_lineage)


def claim_d() -> tuple[list[str], int]:
    """Returns (report lines, violations = preserved §s the same delta re-tables)."""
    lines = ["## D. Project_Workflow deltas: sections preserved verbatim", ""]
    total = 0
    for f in sorted((REPO / "design-substrate").glob("Project_Workflow_v1_*.md")):
        text = f.read_text()
        block = re.search(
            r"^## §\d+ Sections preserved verbatim.*?(?=^---|^## )", text, re.M | re.S
        )
        if block:
            claim_text, source = block.group(0), "block"
        else:
            sentences = [s for s in re.split(r"(?<=[.;])\s", text) if "PRESERVED VERBATIM" in s]
            claim_text, source = " ".join(sentences), f"inline ({len(sentences)} sentences)"
        # A mention is not a claim: drop refs prefixed by a version token ("v1.13 §1" names
        # that delta's own local section), and refs in sentences that call the § NEW or a
        # sibling (the delta is announcing an addition, not preserving it).
        kept = []
        for sentence in re.split(r"(?<=[.;])\s|\n", claim_text):
            if re.search(r"\b(NEW|sibling|authored|authoring)\b", sentence):
                continue
            kept.append(re.sub(r"v1\.\d+\s*§\d+(?:\.\d+)*", " ", sentence))
        preserved = claimed_sections(" ".join(kept))
        # A delta's own scaffolding headings reuse the baseline's numbers (its "§1 Amendment"
        # is not the baseline's §1); only a re-tabled heading carrying a baseline body counts.
        scaffold = re.compile(
            r"(Amendment|NEW sub-section|Sections preserved|Adjacent observations|"
            r"Cross-artifact|Empirical lineage|Filing footer|audit-template|sub-species|"
            r"species|Sub-species|Amended|Retirement-tier|Self-application|catalogue is OPEN)"
        )
        retabled = {
            m.group(2)
            for m in (HEADING.match(ln) for ln in text.splitlines())
            if m and not scaffold.search(m.group(3))
        }
        violations = [p for p in preserved if p in retabled]
        total += len(violations)
        tail = (" — " + ", ".join("§" + v for v in violations)) if violations else ""
        lines.append(
            f"- {f.name}: claim source {source}; preserved §s named: {len(preserved)}; "
            f"re-tabled headings: {len(retabled)}; preserved-yet-re-tabled: "
            f"**{len(violations)}**{tail}"
        )
    lines += [
        "",
        "A preserved § the same delta re-tables as a heading contradicts its own block. "
        "Re-tabled headings that are new sub-sections (the §1 amendment bodies) are not "
        "preserved §s and do not count.",
        "",
    ]
    return lines, total


def main() -> None:
    a, claimed_a, drift_a = claim_a()
    c, lost_c = claim_c()
    d, viol_d = claim_d()
    b, low_b, rho, overlap = claim_b()
    drift_found = drift_a > 0 or lost_c > 0 or viol_d > 0
    agree = rho >= 0.5 and overlap >= 3
    lines = [
        "# E3b — drift scored only on claimed-identical pairs",
        "",
        f"Repo {REPO.name}; relocation {RELOCATION} (parent {RELOCATION_PARENT}); split {SPLIT}.",
        "",
        "## Verdict (computed)",
        "",
        f"- A: claimed sections {claimed_a}; not byte-identical or absent: **{drift_a}**.",
        f"- C: lineage lines lost by the split: **{lost_c}** "
        "(the source's own title, headings and preamble were replaced by the stub, by design).",
        f"- D: preserved-yet-re-tabled sections across the delta chain: **{viol_d}**.",
        f"- B: stubs below both read thresholds: **{low_b}** of the pointer set; "
        f"cosine vs containment rank agreement rho = {rho:.2f}, bottom-5 overlap {overlap}/5.",
        "- Drift, as defined (a claimed-identical pair that is not): "
        + ("**found, see A/C/D above**." if drift_found else "**none found**."),
        "- What the embedding added over the normalised diff: nothing for A, C, D "
        "(identity questions). For B "
        + (
            "the two rankings agree, and only the lexical score names the missing words."
            if agree
            else "the two rankings DISAGREE (see rho/overlap); read both bottom-5 sets."
        ),
        "",
        *a,
        *c,
        *d,
        *b,
    ]
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
