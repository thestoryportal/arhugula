# Context-stack evaluations — eight applications beyond retrieval (2026-09-16)

Durable evidence for the eight stack applications proposed after the decisive test in
`../context-stack-audit-2026-09-16.md`. Each evaluation is a script or run here, its results
file sits beside it, and every judgment marked "author-judged" was made by reading the cited
source. Nothing here used a metered API: embeddings and DuckDB ran locally; reviewer and
`claude -p` runs used the subscription. Scripts run with the Pixeltable tool environment's
Python (`~/.local/share/uv/tools/pixeltable/bin/python`), which carries sentence-transformers
on MPS; E5 runs under `uv run --with duckdb --with pyyaml`.
Scripts find the repository from their own location and run from any clone; E4 reads this
project's Claude Code transcripts under `~/.claude/projects/`, so it needs a machine that has
them.

Kept out of git (`.gitignore` here, decision recorded in
`../context-stack-handoff-2026-09-16-s2.md` §2 A): `E2/*.diff` and `E2/prompt_*.txt`, the
reviewer inputs, about 900 KB, each prompt being `reviewer_prompt.txt` followed by the PR's
diff and, for the pack condition, its Graft `blast` output; `E6-vault/`, the 619-note Obsidian
vault; the `*.log` run logs; and the empty `E7/*.err` files. The results files beside them are
the record.

| # | Application | Instrument | Result | Verdict |
|---|---|---|---|---|
| E1 | Duplicate-finding detection in the review loop | `e1_duplicate_findings.py` → `E1-results.md` | 2,092 findings embedded. Of the 20 most recent, 2 are the same defect re-found in another arc, 16 the same class, 2 unrelated (author-judged). Whole ledger: 1.3% have a prior neighbour ≥ 0.90, 9.6% ≥ 0.85. | **Supported.** Recurrence is class-level and frequent; an index surfaces the sibling a reviewer should read. E2's no-pack reviewer re-found a shape already on the ledger four times, live proof of the gap. |
| E2 | Structural context packs for transcript-less reviewers | `E2/` — two merged PRs, Graft `blast` packs, four fresh Sonnet reviewers (`reviewer_prompt.txt`, `result_*.md`) | PR 1528: pack 1 verified finding, no-pack 2. PR 1527: pack 1, no-pack 2. Both packs said "nothing outside this diff depends on it", which was true; one reviewer said the pack changed its attention, one said it did not. | **Not supported on these PRs.** Tool-and-test diffs have no dependents for a blast radius to show. Untested on `harness-*/src` changes with real callers; that is where a pack could matter. |
| E3 | Cross-spec drift by passage embedding | `e3_cross_spec_drift.py` → `E3-results.md` | 372 paragraphs across the root guide, governance packs and the workflow delta chain; 38 non-identical pairs ≥ 0.95, 76 ≥ 0.90. The top 15 are all intentional: version-number-only deltas and shared "relocated byte-verbatim" headers. | **Not supported as configured.** The ranking surfaces expected structure before defects; a drift detector needs boilerplate exclusion and a target definition first. |
| E4 | Session history as a second memory tier | `e4_session_history_index.py` → `E4-results.md` | 15,214 text chunks from 243 sessions, embedded in 100 s. Querying with a memory's one-line description finds its origin session at hit@1 6/30, hit@5 14/30. | **Partially supported.** Half of memory facts are re-findable from a one-line query with no memory file at all; a fuller question would score higher. Worth a real question set before adoption. |
| E4b | E4 with operator-shaped questions (handoff-s2 §2 C) | `e4b_real_questions.py` + `E4b-questions.json` (20 questions, ground truth = session **and turn**, each verified by a verbatim quote through `e4_turn.py`) → `E4b-results.md` | 249 sessions, 15,586 chunks. Session hit@1 9/20, hit@5 12/20; turn hit@1 7/20, hit@5 8/20. When the session is right at rank 1 the exact turn is too in 7 of 9. Threshold written before the run: POINTERS needed session@1 ≥ 10 and turn@5 ≥ 8. | **AUGMENT**, by the pre-committed rule: one short of the pointer line on session@1. Memory stays the authority; the index answers "where did that come from" and, when it lands the session, lands the turn. Questions were authored by a fresh Sonnet agent from the memory bodies, not the descriptions (token overlap with the description ≤ 0.56, median 0.25). |
| E5 | DuckDB over the ledgers | `e5_duckdb_ledgers.py` → `E5-results.md` | arc-metrics loads in 22 ms, cohort medians in 4 ms; forward-register loads and answers status counts in one query. DuckDB's cohort counts (25/2, 19/2) differ from the lever tool's (13/1) because the tool's exclusion rules are the substance. | **Supported as an analytics layer**, provided the tool's row-eligibility rules are ported as SQL predicates, not re-derived. |
| E6 | Operator comprehension via Obsidian | `E6-vault/` (619 notes, 2,114 wikilinks, `graph.canvas`) from the per-file Haiku governance graph | Artifact produced; the comprehension test is the operator's to run by opening the vault. | **Untested by construction**; the artifact exists. |
| E7 | Subagent preload: card brief vs full context | `e7_preload_brief.sh` → `E7/`, `E7-results.md` | Clean pairs (Q10, Q15): full 162k tokens and ~56k first-turn context vs brief 88k and ~28k, same correct answers. Brief 4/4 correct. Two full-context runs were derailed by the repo's own Stop-hook lint gate (8 and 27 turns, up to 2.6 M tokens) and answered nothing. | **Supported**, with the strongest single number in this set: a card brief halves subagent context at equal correctness. The derailment is a second finding: in-repo `claude -p` agents inherit hooks that can consume a run. |
| E8 | Dev-side oracle for the product memory substrate | `E8-product-eval-oracle.md` | C-MEM-11 ranks by scope, recency, confidence, authority, pinning and filters, not similarity; C-MEM-20 asks for determinism and denial tests. | **Not a fit; do not build.** |

## Known defects in the scripts (codex review, 2026-09-16, not absorbed)

The scripts are committed as they ran, so the results above are reproducible from them. The
out-of-family review of this commit found seven defects; none changes a verdict, and each is
left in place so the record matches the runs. Anyone re-running an evaluation should fix the
relevant one first.

- `e7_preload_brief.sh:31` — the full-context `claude -p` runs execute inside the shared
  checkout with the repo's hooks; `E7/full_Q01.json` and `full_Q06.json` show those runs
  invoking ruff fixes and attempting edits. Re-run in an isolated worktree.
- `e7_preload_brief.sh:31` — only `set -u`; a nonzero `claude` exit is ignored and the script
  still prints `E7 runs complete`. Check each exit status and each JSON before trusting a pair.
- `E2/reviewer_prompt.txt:4` — the reviewer is pointed at a fixed live checkout, not a tree at
  the reviewed PR's head. Bind the tree to each head before reusing the prompt.
- `e5_duckdb_ledgers.py:39` — the cohort CASE puts every row without a target lever into
  `baseline`, including undeclared and unrelated-lever rows the reference tool excludes; this
  is the 25/19-vs-13 gap the E5 row records.
- `e5_duckdb_ledgers.py:42` — `median(p1_rounds)` is a median over a list column, not over each
  row's P1 count (`len(p1_rounds)` in the reference tool); the `med_p1` column in
  `E5-results.md` is wrong.
- `e5_duckdb_ledgers.py:49` — the reference-tool subprocess's exit code and JSON shape are not
  checked; a failure would be embedded as comparison output.
- `e5_duckdb_ledgers.py:82` — a fixed `_register.json` temp path; concurrent runs would collide.

## Side findings recorded during the runs

- `graft build` inside the repo (v0.18.0) rewrote four tracked files: `.claude/settings.json`
  (hook timeouts to milliseconds, the repo's post-edit matcher replaced), both graft helper
  scripts and the graft skill. Restored from HEAD. This is the regeneration hazard
  `.harness/graft-integration-notes.md` describes; any adoption step must pin how and where
  `graft build` runs.
- Graft's Stop hook reports self-attributed savings ("~600,582 tokens this turn") on runs that
  cost 76k in total; the figure is not usable as evidence.
- Ten detached worktrees under job temp directories (four of mine, six from earlier jobs) were
  removed through `tools/hooks/safe-worktree-remove.sh` after attaching `archive/wt-*` branches,
  so every removed head is still reachable.
