# Context-stack program — session-2 handoff (2026-09-16, evening)

Read this first; it supersedes §4 of `context-stack-handoff-2026-09-16.md` for ordering, and
that file's §5 records the grounding this session did. Everything below is bound to
`main` at `5785338cd` (refresh #1539) unless a sha says otherwise.

## 1. State at handoff

| Fact | Value |
|---|---|
| Landed this session | PR #1538 → `a436658ac` (main run green); terminating refresh #1539 → `5785338cd` |
| Arc | `preflight-finding-intake`, reservation `merged`, lane `Roberts-MacBook-Pro-arhugula-v2-d56be5f6` |
| Roadmap pointer | installed by the door from `.harness/.next-action-draft`; ends "then U-HE-43" |
| Untracked in the root checkout | `docs/research/` (this program's whole record), `docs/diagrams/code-loop/visual-check/` (pre-existing, not ours) |
| Checkpoints | `~/.gstack/projects/arhugula-v2/checkpoints/20260916-165858-preflight-finding-intake-landed-1538.md` (arc close) and the one written after this file |
| Memory touched | `context-stack-program-state-2026-09-16`, `prose-obligation-needs-an-enforcing-venue` (new), `in-place-bg-job-venue-traps`, `bash-concurrency-and-parsing-gotchas` |
| Branch hygiene owed | `feat/preflight-finding-intake` (#1538) and `roadmap-refresh-post-1538` (#1539) — deferred row filed; run `just branch-hygiene-pending` in an interactive session |

What #1538 is, in one paragraph, so you can reason about it without reading the diff:
`refresh-classes.py classify` is a pure verb (JSON findings in, `{finding_id: [class names]}`
out). `just review-template-sweep` stamps each outstanding reviewer finding with its classes
and pre-fills `intake: TODO(answer)` under an UNMATCHED one. `just review-attest-sweep`
parses the slot (`class-extended <n> …` | `new-class <name> …` | `instance-only <reason>`),
re-classifies with the class table **as committed at HEAD**, refuses a repair claim the
table does not show, and records `instance-only` ids on the attestation. Class 15 "bound
bytes are not the executed bytes" was opened by this mechanism on its own codex round.
The end-to-end witness is `tools/test_review_loop_gate.py::test_one_finding_flows_into_the_class_table_end_to_end`.

## 2. Remaining work, in order

### A. Doc-only PR committing `docs/research/` — do this first

Why first: it is the durable record of the whole program and is one accidental `git clean`
from gone; and every later item edits files inside it.

Constraints, each learned the hard way this session:
- **Doc-only. Never bundle with code.** The stop-gate classifies `docs/**` as a design
  surface; untracked docs beside a code diff already tripped `DESIGN_IMPL_MIX` in the
  in-place parity lane. A doc-only PR skips the merge-gate; codex one round (lean protocol).
- **Fix the scripts' absolute paths before committing them, or exclude the scripts.**
  `e1_duplicate_findings.py:20`, `e3_cross_spec_drift.py:19`, `e4_session_history_index.py:24`,
  `e5_duckdb_ledgers.py:19`, `e7_preload_brief.sh:10,12` hard-code `/Users/robertrhu/…`.
  Resolve from `Path(__file__)` / `git rev-parse --show-toplevel`; the E7 scratch path
  points into a job tmp dir that no longer exists.
- **Interpreter is not the workspace's.** `common.py` imports `sentence_transformers` on MPS;
  the README says scripts run under `~/.local/share/uv/tools/pixeltable/bin/python` and E5
  under `uv run --with duckdb --with pyyaml`. Say so in the README (it does) and do not add
  these deps to the workspace `pyproject.toml` (torch-free rule for the workspace).
- **Large binaries.** `E2/` carries two PR diffs (135 K, 158 K) and four ~140–160 K prompt
  files; `E6-vault/` is 619 notes. Decide per directory: commit `E*-results.md`, the README,
  the audit, both handoffs, and the small scripts; consider leaving `E2/prompt_*.txt` and
  `E6-vault/` out (state the exclusion in the README).
- Recommended commit set: `context-stack-audit-2026-09-16.md`, both handoffs,
  `context-stack-evals/README.md`, `E1/E3/E4/E5/E7/E8` results, `common.py`, the five
  scripts (paths fixed), `E2/reviewer_prompt.txt` + `E2/result_*.md` + the two `*.blast.md`.

Acceptance: PR merged; `git status` clean of `docs/research/`; README's run instructions
execute from a fresh clone path.

### B. Class 2 `drift` extension (data-only, ~10 minutes)

At the 2026-09-16 corpus, 30 finding rows carry the word `drift` and match no class-2
vocabulary (`stale|close_out|mis-cite|cite|count|narrat|docstring claim|partition`).
Add `\bdrift` to class 2's row in
`.claude/skills/defect-class-preflight/scripts/refresh-classes.py` and a sentence to
SKILL.md §2; measure before/after with the no-verb report (`python3 <script>`), and name the
new class-2 count bound to the corpus date. The table↔SKILL parity test
(`tools/test_refresh_classes.py::test_every_class_row_has_its_skill_section_and_vice_versa`)
will pass unchanged (no new number). Run the sweep attestation on your own arc — this is a
tooling PR, so one codex round, no lens.

Also cheap, same PR: the witness lens's P3 — `test_report_counts_agree_with_classify` should
feed one input through both the `classify` verb and the report and compare.

### C. E4 with real questions (handoff-1 §4 A)

Reuse `e4_session_history_index.py` (index: 15,214 chunks / 243 sessions, ~100 s to embed).
Replace the 30 one-line memory descriptions with 20 operator-shaped questions, ground truth
= session id **and turn**, taken from `~/.claude/projects/-Users-robertrhu-Projects-arhugula-v2/*.jsonl`.
Add hit@1 on the turn. Two example questions from handoff-1: "when did we decide Floor B was
terminal and why", "what did codex say about the merge_door re-adoption check". Verdict
threshold to write down before running: what hit@1 would justify memory entries becoming
pointers.

### D. E2 on code with callers (handoff-1 §4 B)

Pick two merged PRs touching `harness-*/src` whose `graft blast` at depth 2 is non-empty
(the two tried, #1527/#1528, had zero dependents, which is why E2 was "not supported").
Same four-reviewer design (`E2/reviewer_prompt.txt`), verify every finding, report pack vs
no-pack. Then one `just codex-review` trial with the pack (subscription, $0 metered) —
Codex is the production transcript-less reviewer.

### E. E3 reconfiguration (handoff-1 §4 C)

Define drift as a passage a `§`-cite or "relocated byte-verbatim" header claims identical
to another and is not. Exclude version-token-only deltas and relocation headers. Compare
governance-pack paragraphs against the root guide's pointer text and
`claude-artifact-pointers.md`; each `Project_Workflow` delta body against its predecessor's
same-numbered section. Score only claimed-identical pairs; then ask whether embeddings add
anything over a normalised diff.

### F. Then U-HE-43 per the roadmap pointer.

## 3. Mechanics a fresh session will hit

- **Venue.** If the session is an in-place background job, `just codex-check` reds exactly
  `tools/test_codex_stop_gate.py::test_stop_gate_emits_valid_stop_hook_json`
  (`ROOT_CHECKOUT_EDIT`, plus `DESIGN_IMPL_MIX` while `docs/research/` is untracked). CI's own
  checkout passes; state it in the PR body. `just codex-context-check-ci` on the committed
  range is the authority.
- **Local pyright.** Run `uv sync --all-packages` once; before it `uv run pyright` reported
  11,699 errors from unsynced members (CI shape is 0).
- **Arc ceremony that worked, in order:** `arc_disjoint_check.py check --candidate HEAD` →
  `reservations.py reserve` → commit → `review-template-preflight` / fill / `review-attest-preflight`
  → `review-with-failover-logged .harness/tmp/<arc>-rounds/r1.log` (background, 600 s timeout
  was enough) → adjudicate each finding **as a literal id, one call each** (a zsh `for id in
  $IDS` over newline-joined ids passed all ids as one argument) → absorb, commit →
  `review-template-sweep` / fill / `review-attest-sweep` (your own arc's findings now get the
  intake treatment) → push once → `gh pr create` → `reservations.py update --set pr=…` →
  `merge-gate-binding <lens>` → lens Agent (sonnet) → ask the lens to write its full message
  to `.harness/tmp/merge-gate-lens-<id>.txt` (a stale file from an earlier arc sits at that
  path; check the mtime before emitting) → `merge-gate-emit --lens …` → narrative md row →
  commit gate rows → push by sha → CI green → `reservations.py update --set head_sha base_sha
  attested_merge_tree` → `transition --to open` → `.harness/.next-action-draft` →
  `safe-merge.sh <pr>` (background; it merged, waited main, opened and merged the refresh) →
  `/context-save-lean` → `just arc-close …` → `defer.sh` branch-hygiene row.
- **Lean protocol for tooling arcs** (memory `feedback-b230-lean-protocol-one-codex-round`):
  one codex round, absorb P1/contract findings, single witness lens only when gate mechanics
  change, no round 2. Items B–E above are all tooling/doc arcs.
- **Do not run `graft build` in the shared checkout**; it regenerates four tracked files
  (memory `graft-build-regenerates-tracked-files`).
- **Operator rules in force:** no metered API calls (subscription and local only); no
  unilateral paid calls or secret relocation in a background job; workspace deps stay
  torch-free (the Pixeltable tool env is where torch lives).

## 4. Open questions only the operator can answer

- Whether `E6-vault/` and the ~600 K of E2 prompt files belong in git at all (item A).
- Whether `instance-only` intakes should carry a cap or a review trigger; today the rate is
  recorded on the attestation and nothing acts on it.
