# Context-stack program — session handoff (2026-09-16)

*Consolidated record of one session's work on the proposed context-management stack (Pydantic · Pixeltable · Graft · Graphify · DuckDB · Obsidian). Read this first in the next session; it points at every artifact and states every decision, environment change and open item. Posture: mode-agnostic ops. Nothing under `design-substrate/**` was touched.*

## 1. Artifacts, in reading order

| File | What it holds |
|---|---|
| `docs/research/context-stack-audit-2026-09-16.md` | The full audit: machine facts, what the repo already had, per-component verdicts, all measurements (Graft timings, Pixeltable pilot, Graphify local/frontier/per-file, DuckDB, decisive test), the eight-application summary |
| `docs/research/context-stack-evals/README.md` | Verdict table for the eight applications E1–E8, plus side findings |
| `docs/research/context-stack-evals/E*-results.md`, `E2/`, `E7/`, `E6-vault/`, `*.py`, `*.sh` | Scripts, raw runs, results, reviewer prompts and outputs, the Obsidian vault |
| `~/.claude/jobs/8cde37b5/tmp/` (ephemeral, cleaned when the job is deleted) | Decisive-test rows `results.tsv`, `questions.json`, the four regime prompts `prompts/R0..R3.txt`, `compliance.py`, Pixeltable pilot scripts `pxt_pilot.py` / `pxt_index.py`, Graphify scratch corpora `gpf/`, `g30/`, `g30h/`, `gall/` | 

All of `docs/research/` is untracked. Commit it if it should outlive the working tree.

## 2. Findings, in one place

**Machine.** Apple M3 Max (the chip string; the ask said Pro), 48 GB. torch, sentence-transformers, DuckDB and tree-sitter all resolve as arm64 wheels; the Intel-era blocker is gone. Ollama serves nomic-embed-text, mxbai-embed-large, llama3.1:8b, llama3.2, mistral, and now gemma3:27b (pulled this session, 17 GB).

**What the repo already had.** Graft integrated since PR #1386/#1388 (hooks, MCP, skill, reachability tool) but dead on this machine until this session; a deterministic spec-to-code overlay (`tools/semantic_overlay/`); a June 2026 written rejection of an LLM-built code graph plus dashboard (Understand-Anything); no vector store, DuckDB, Pixeltable, Graphify or Obsidian anywhere.

**Per component.**
- Graft: check 10.6 s, incremental build 12.5 s, ask 0.64 s, callers 0.11 s over 23,231 symbols. Deep layer never built. Its self-reported "tokens saved" is not evidence (claims 600k on a 76k run).
- Pixeltable: upgraded in place to 0.7.7 with torch; winning-defense catalog verified intact (936 docs, 27,691 chunks). Pilot: 39,256 chunks; Ollama 137 chunks/s, bge-small on MPS 285 chunks/s; 5/5 paraphrased questions to the right document. Full index over 670 files: 42,046 chunks in 142 s; needs a 200-char chunk floor or table-row slivers dominate. Cold start 9.4 s. Markdown parser rejects some files; staged as text.
- Graphify: local 8B/27B extraction unusable; frontier chunked runs collapse to document level; per-file Haiku over 30 governance/skill files gave 590 nodes / 693 edges, 9/10 section-level answers, at 2.7 M subscription tokens and 93 min. Code layer at parity with Graft on speed (28 s full, 11 s incremental) but no exact callers, blast or skeleton. Sonnet vs Haiku: Haiku's graph was more useful.
- DuckDB: 27.8 MB wiring graph loads in under 2 s; callers-only-from-tests 35 ms, blast radius 11 ms. Verdict moved from defer to adopt as query engine (not as vector store).
- Obsidian, Pydantic, "Integrated Context Compiler": human surface only; already the stack; does not exist as a product.
- Codex route: no Graphify backend; subscription cannot be an OpenAI-compatible endpoint; metered gpt-5.6-luna would be cents but is unproven. Operator ruled out API for now.

**Decisive test (64 subagent runs, 16 questions × 4 regimes).** Baseline `rg`+overlay 16/16 in 3.0 tool calls; Graft 16/16, 3.0; Pixeltable 15/16, 3.44 (one wrong on a code question); Graphify 16/16, 3.44. Agents invoked the offered tool on 8, 11 and 4 of 16 runs despite "try first". Graft's only measured win: exact callers. Verdict: keep Graft, Pixeltable on demand for prose, no Graphify for agent retrieval.

**Eight applications (E1–E8).** Supported: E7 card brief halves subagent context at equal correctness; E1 finding-ledger index catches class-level recurrence (16/20); E5 DuckDB ledger analytics. Partial: E4 session-history retrieval (hit@5 14/30 from one-line queries). Not supported: E2 blast packs on tool PRs, E3 embedding drift as configured, E8 product oracle. E6 vault built, untested.

## 3. Environment changes made this session (all reversible)

1. `graft` symlinked to `~/.local/bin/graft` and `/opt/homebrew/bin/graft`; package symlinked into `/opt/homebrew/lib/node_modules/@nanonets` so hooks resolve. Graft MCP will connect from the next session start.
2. Pixeltable tool env: `uv tool install --force pixeltable --with torch --with sentence-transformers --with ollama --with mistune --with tiktoken` (0.7.7, Python 3.11 env). MCP server env rebuilt from git at 0.7.7. Backup of the case catalog at `~/.pixeltable-pgdata-bak-20260916/` and `~/.pixeltable-config-bak-20260916.toml`. The stale Postgres from the old tree was stopped; a new one starts on demand. winning-defense launchd agents were unloaded and reloaded by the operator.
3. `ollama pull gemma3:27b`.
4. `graft build` in-repo rewrote four tracked files (`.claude/settings.json`, both graft helpers, the graft skill); **restored from HEAD**. Do not run `graft build` in the shared checkout without re-checking `git status` afterwards.
5. Ten detached worktrees under job temp dirs removed via `tools/hooks/safe-worktree-remove.sh` after attaching `archive/wt-*` branches (four mine, six from earlier jobs: wt-1470, wt-clean, wt-refresh-1473, wt-refresh-1479, wt-usr01, wt-1498). Recoverable with `git worktree add <path> archive/wt-<name>-<sha9>`.

## 4. Next session — plan

**A. E4 with real questions.** Replace the one-line memory descriptions with 20 operator-shaped questions ("when did we decide Floor B was terminal and why", "what did codex say about the merge_door re-adoption check"), ground truth = session id + turn, from `~/.claude/projects/-Users-robertrhu-Projects-arhugula-v2/*.jsonl`. Reuse `e4_session_history_index.py`; add a hit@1 of the turn, not just the session. Decide whether memory entries can become pointers.

**B. E2 on code with callers.** Pick two merged PRs touching `harness-*/src` with non-empty `graft blast` (dependents at depth 2 > 0). Same four-reviewer design (`E2/reviewer_prompt.txt`); verify every finding; report pack vs no-pack. Also try the pack on `just codex-review` (subscription) once, since Codex is the production transcript-less reviewer.

**C. E3 reconfiguration.** Define drift as: a passage that a `§`-cite or a "relocated byte-verbatim" header claims identical to another, and is not. Exclude the delta-chain change-note boilerplate (paragraphs whose only difference is a version token) and relocation headers. Compare each governance pack body paragraph against the root guide's pointer text and the archived `claude-artifact-pointers.md`; and each `Project_Workflow` delta body against its predecessor's same-numbered section. Score only pairs the text itself claims should match. Then see whether embeddings add anything over a normalised diff.

**D. Self-evolving skills: ground, then repair with the stack.** The inventory (this session) found: `defect-class-preflight` only recounts against a hand-kept regex table (`scripts/refresh-classes.py`) and never proposes classes or edits its skill; `arc-lever-report` is read-only with an n≥5 gate; `capture-failure.sh` emits nudges no consumer promotes to memory or skills; `register-pr-prose` has no refresh script; `optimize-claude-md` and `self-heal` consume no aggregated outcomes. `.harness/spec/Workflow_Repair_Charter_v1.md:11-14` states the circuit is "broken at both ends" ("nothing flows in"; "the measurement out is corrupted") and enumerates WR-01..WR-16. Next session: (1) read the charter and `git log` to establish which WR items have landed; (2) map E1 (finding index → candidate classes for `refresh-classes.py`), E5 (DuckDB over arc-metrics → the measurement half), and Graft's callers (reachability class) onto the charter's repairs; (3) propose the smallest change that makes one finding flow in end to end, as a PR, under the repo's normal gates.

**E. Housekeeping.** Decide whether `docs/research/` is committed. Pin how `graft build` runs (never in the shared checkout, or re-restore the four files). Consider `just doc-search` from `pxt_index.py` if Pixeltable on demand is kept.

## 5. Self-evolving skills — grounding and the first repair (2026-09-16, session 2)

**Charter state, from git log.** Every WR-01..WR-16 item landed as U-SR-01..U-SR-08 (PRs #1475, #1477, #1479, #1481, #1483, #1485, #1487, #1489; the R2 mechanical sweep as U-SR-09, #1493). The charter's repair list is exhausted. What remains is the circuit the charter §1 called broken at the intake end: the skill's own obligation to classify every reviewer-caught miss and repair the skill in the absorption commit lived only in SKILL.md prose and executed zero times; `refresh-classes.py` printed the ten most recent unmatched findings to nobody; 650 of 2,092 ledger findings match no class today.

**Mapping the stack onto the remaining gap.**

| Evidence | Where it lands |
|---|---|
| E1 — 16 of the last 20 findings recur at CLASS level against an earlier arc | Confirms the class table is the right unit of repair and that the intake venue should name a finding's class (or lack of one) at review time. The embedding neighbour is the *upgrade* for the "which existing class is nearest" hint; the shipped version uses the regex table itself so the gate stays stdlib. |
| E5 — DuckDB over `arc-metrics.jsonl` / gate log | The measurement half: `intake_instance_only` on each sweep attestation is the new column to join — an arc whose unmatched findings are all dispositioned `instance-only` has satisfied the letter of the loop and none of its intent. |
| Graft callers (reachability) | Not a class-table repair. Its natural carrier is the preflight grounding step (WR-11's mechanism-precedent search), not the intake. Deferred; no code. |

**The first end-to-end repair (branch `feat/preflight-finding-intake`).** `refresh-classes.py classify` (pure, JSON in/out) → `just review-template-sweep` stamps every outstanding finding with its classes and marks the unmatched ones with a required `intake:` slot → `just review-attest-sweep` parses the slot into one of `class-extended` / `new-class` / `instance-only`, re-classifies with the live table, and refuses a repair claim the table does not show. Witness: `tools/test_review_loop_gate.py::test_one_finding_flows_into_the_class_table_end_to_end` runs the real classifier — a finding with novel vocabulary is UNMATCHED, a `new-class` claim is refused, one data row added to `CLASSES` makes the same attestation pass with no intake owed.

**Not decided here.** Whether `docs/research/` is committed (recommendation: commit the audit, the handoff, and the `E*-results.md` files; keep the scripts' absolute paths and the embedder dependency out of the tree until E4/E2/E3 are re-run).
