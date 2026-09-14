---
name: merge-gate
description: Decorrelated 3-lens pre-merge review gate — launches three parallel Agent-tool subagents (concurrency/race-conditions, spec-conformance-against-ledgers, test-witness-adequacy) against a PR's diff, each returning a structured APPROVE/BLOCK verdict. Use right after CI is confirmed green and `just codex-review` has converged, before running `gh pr merge`, for any code-touching PR in the /loop continue → ship-pr flow. Do NOT use on doc-only or `ops: roadmap status refresh` PRs — skip those entirely; the gate only fires when the diff touches `harness-*/src|tests` (or equivalent code surface). Merge only when all three verdicts are APPROVE; any BLOCK or split verdict halts the merge and surfaces the disagreement via AskUserQuestion.
---

# merge-gate — decorrelated 3-lens pre-merge review

An **addition** to the existing pre-merge apparatus (`just review-with-failover` — the fail-closed
`codex-review` wrapper with the `gemini-review` failover — + the §13.1 transcript-brief
review), not a replacement. It composes with `ship-pr`'s pre-flight — see the wiring note at the end.

## Honesty caveat — read this before trusting an all-approve

The three subagents below are Claude Agent-tool subagents: **lens-decorrelated, not
vendor-decorrelated.** Same model family, same training-time blind spots. Their value is that
each goes *deep on one specialty* the generic pass skips, not that they're independent eyes in
the way Codex (`just codex-review`, out-of-family, $0 subscription) or the
transcript-brief review (the §13.1 transcript-aware half) are. This gate does **not** replace either — it runs alongside them. If a
finding matters and you want real cross-vendor confidence, that's still Codex's job.

This gate is **instruction-level, not a hard hook.** `permission-guard.sh` is a shell script —
it cannot launch subagents. Enforcement here is the loop agent following this documented
procedure, exactly as reliable as that compliance. It is not a mechanical block the way the
deny-list is.

## Scope gate — skip non-code PRs

Before doing anything else, check the PR's changed files (`gh pr diff <PR#> --name-only` or
equivalent). If the diff is **doc-only** or is a **terminating refresh PR**
(`ops: roadmap status refresh …`, touching only `.harness/roadmap_status.md`) — **skip this
gate entirely.** Running a concurrency/test-witness reviewer on a status-refresh PR is pure
waste and a spurious-block risk. Fire only when the diff touches `harness-*/src/**`,
`harness-*/tests/**`, or equivalent runtime code (hooks, tools scripts with real logic).

## Pre-flight — blast radius (run BEFORE launching the reviewers)

A recurring defect class here is not "the changed code is wrong" but **"the change was
right and a consumer of it was missed"** — `[[unconditional-precondition-tightening-ripples-broadly]]`
(B-65-A: grep ALL constructors of the now-illegal shape),
`[[soft-budget-must-never-gate-a-hard-path]]` (grep every consumer on demotion),
`[[shared-is-shape-change-ripples-cross-axis-field-asserts]]`. Each reviewer below sees only
what you write into its prompt, so a consumer nobody enumerated is a consumer no lens checks.

Enumerate it mechanically instead of from memory:

```bash
gh pr diff <PR#> --name-only                 # changed files
graft skeleton <changed-file>                # symbols + spans in each (~200 tok/file)
graft callers <symbol> --depth 2             # who reaches it, annotated by depth
```

Read `--depth 2` output for the **production** consumers — the listing is usually dominated
by test callers, which are not the blast radius. For a rename, signature change, or
precondition tightening, use `--depth all` instead: it surfaces the sibling and downstream
files a single-file edit misses.

Then **paste the resulting consumer list verbatim into Reviewers 1 and 2** (see the prompt
inserts below). Naming the consumers is the point; a reviewer told "check for missed
consumers" without the list just re-derives it worse.

**Fail posture — this is not optional and must not be silently skipped.** The graph lives at
`graft/.graph/wiring.json`, which is gitignored and per-checkout, so a fresh worktree may not
have one. If `graft` is absent or the graph is unbuilt, run `graft build` (it is `$0`,
deterministic, no key, and takes seconds). If it still cannot run, **say so explicitly in the
gate outcome and in the log row** — record `blast-radius: NOT RUN (<reason>)`. A pre-flight
that quietly did nothing reads downstream as a pre-flight that found nothing, which is the
`[[gate-cannot-tell-empty-from-unlooked]]` failure this gate exists to avoid.

**Known limits — state them alongside the list, don't imply completeness.** These are static
call edges: they do not model reference-passing (a callback registered rather than invoked),
`getattr`, string-keyed registries, or plugin loaders. The list is a floor on the blast
radius, never a ceiling. `tools/graft_reachability.py` documents this same limit in detail.

## Binding — publish BEFORE launching (C-HE-15 §4, U-HE-13; bindings by file, WR-09)

Each lens verdict is bound to the exact tree it reviewed. For each lens id —
`merge-gate-concurrency`, `merge-gate-spec-conformance`, `merge-gate-witness-adequacy` — run
`just merge-gate-binding <id>` (base `main`) on the checked-out PR head. It writes the six
values (`head_sha`, `base_sha`, `diff_digest`, `reviewer_identity`, `prompt_version`,
`config_hash`) to a file and prints **only that path**. Name the printed path in that lens's
prompt and tell the lens to READ it — never copy a value through this turn. Both round-3 lens
corruptions were orchestrator transcription errors (a truncated `head_sha` → one re-emit; a
spliced `base_sha` → a full 0.38M-IET lens rerun, ≈5 min on the critical path) [B] F3, and a
value you never handle is a value you cannot corrupt. The lens copies the six VERBATIM out of
that file into its fenced JSON block; `emit` (below) recomputes them and refuses a verdict
whose values differ (a moved head, a swapped lens) — the verdict is then NOT recorded and
does not count.

## Prompt authoring — delegate under `laws:prompt`

**Subagent prompts are authored under `laws:prompt` (U-SR-03, charter WR-08).** A subagent
sees only the prompt you write — no transcript, no CLAUDE.md, no user requirement unless you
put it there. Delegate the authoring to an agent that adopts `laws:prompt` and use the prompt
it returns; composing one inline is legal ONLY when instantiating a skill-canonical template
with literal values. A freehand prompt written in a `laws:code` session is the defect this
rule exists to stop: the passive memory (`[[feedback-subagent-prompts-are-laws-prompt-medium]]`)
failed twice in 48h, and delegating costs ~1m13s / 0.11M IET — about 3% of one lens run. The
`agent-prompt-advisory` PreToolUse hook restates this at every `Agent` call; it is advisory
and never denies. The delegate's OWN invocation is the base case: launching the
laws:prompt authoring agent uses the one-line brief `Adopt laws:prompt and author the
subagent prompt described below; return only the finished prompt.` plus the task
description, and needs no further delegation -- without a named base case the rule
recurses forever, since every authoring agent would itself need an authored prompt.

The three reviewer prompts below **are** the skill-canonical template: instantiating them
with this PR's literal values (PR number, branch, blast-radius list, binding-file path) is
the sanctioned inline path. Departing from them — a new lens, a re-worded specialty, an
extra instruction — is authoring, and goes through the delegate.

## The three reviewers — launch in ONE message, three parallel Agent calls

Each prompt must be **self-contained** (a subagent sees only what you write — no conversation
context) and must include the PR's diff or a pointer to fetch it
(`gh pr diff <PR#>`), the branch name, and the specific lens. Generic "review this PR" prompts
just triplicate what Codex already does — go deep on the specialty, explicitly forbid a
generic pass, and demand a machine-parseable verdict line. **Every prompt also demands, immediately
before the `VERDICT:` line, ONE fenced ```` ```json ```` block matching
`tools/review_schemas/merge-gate.schema.json`: keys `verdict` (APPROVE|BLOCK), `findings`
(array of `{severity: P1|P2|P3, location, message}`, empty on APPROVE, non-empty on BLOCK) and
the six binding values copied verbatim — no other keys.** Append that sentence to each of the
three prompts below, naming the **binding-file path** the recipe printed for that lens and
instructing the lens to read the six values from it; the values themselves never appear in
the prompt you write.

**Reviewer 1 — concurrency / race conditions:**
> Review this diff for concurrency defects only — do not do a general code review. Diff:
> `gh pr diff <PR#>` on branch `<branch>`. Look specifically for: race conditions on shared
> state; TOCTOU races in file/git operations; deadlock/livelock potential; incorrect
> `asyncio.timeout`/cancellation handling (CancelledError raised *inside* the timeout block vs
> a bare `TimeoutError` from an inner coroutine outside it — these are different failure
> shapes); non-atomic check-then-act patterns; daemon-reused context leaking across concurrent
> runs (needs per-run isolation, not a shared frozen object); fence/step-id keys that could
> double-fire on a changed step_id. These call sites reach the changed symbols — check each
> for the above, and treat the list as a floor, not a complete set (static call edges miss
> reference-passing, `getattr`, and string-keyed registries): `<paste blast-radius list>`.
> For each concern found, cite file:line and a concrete
> interleaving that breaks. End your response with exactly one line:
> `VERDICT: APPROVE` or `VERDICT: BLOCK: <one-sentence reason>`.

**Reviewer 2 — spec conformance against ledgers:**
> Review this diff for conformance against the canonical design-substrate and this workspace's
> ledgers only — do not do a general code review. Diff: `gh pr diff <PR#>` on branch
> `<branch>`. Check: (a) does the diff cite a spec/plan version, and does that version exist
> and say what's claimed — read the actual file, don't trust the cite; (b) does it silently
> extend H_T design at Phase-7 execution time (X-AL-3) — i.e. does it introduce a new
> primitive/contract not already in a cleared spec; (c) does it match the disposition already
> recorded in `.harness/substitutions.yaml`, `.harness/arc-ledger.yaml`, or
> `.harness/forward-register.yaml`, or does it silently diverge from a recorded row; (d) grep
> sibling per-axis specs/plans for a stale cite-shape this diff should have updated too;
> (e) these files consume the changed symbols — for each, check whether it encodes an
> assumption this diff invalidates (a cardinality, an enum arm, a precondition, a count in a
> docstring or spec table) and was not updated: `<paste blast-radius list>`. That list is a
> floor, not a complete set. For
> each concern found, cite file:line + the exact ledger/spec line it conflicts with. End your
> response with exactly one line: `VERDICT: APPROVE` or `VERDICT: BLOCK: <one-sentence
> reason>`.

**Reviewer 3 — test-witness adequacy:**
> Review this diff's tests for witness adequacy only — do not do a general code review. Diff:
> `gh pr diff <PR#>` on branch `<branch>`. For each new or changed test: (a) does it exercise
> the real path a consumer/production entry point would take, or is it a half-proof against an
> isolated seam that the real path never reaches; (b) reason through a mutation probe WITHOUT
> actually editing the tree — if the load-bearing line this test claims to pin were deleted or
> inverted, would the test actually fail, or would it stay green regardless; (c) is the
> verification shape matched to the claim — an e2e/integration check for behavior-over-time
> claims, not just a grep/presence check standing in for one. You are read-only: reason about
> the mutation, do not perform it. For each gap found, cite file:line + what mutation would
> slip through undetected. End your response with exactly one line: `VERDICT: APPROVE` or
> `VERDICT: BLOCK: <one-sentence reason>`.

## Parsing — fail closed

A raw `Agent` fan-out cannot enforce an output schema (that's what the `Workflow` tool's
`schema` option is for, not used here) — the parse discipline below is the only guard:

- The verdict is valid **only** if the response ends with exactly one line matching
  `VERDICT: APPROVE` or `VERDICT: BLOCK: <reason>`.
- **Missing, malformed, or ambiguous → treat as `BLOCK: unparseable verdict`.** Never read a
  silent/truncated/off-format response as approval — this is the same silent-failure trap
  documented for Codex's non-interactive streaming-capture limitation
  (`[[codex-out-of-family-reviewer]]`); it applies just as much to a raw subagent reply.
- **Record the three lens verdicts through the structured sibling (C-HE-23 §2) in ONE
  call (B-230 Task 5).** Write each lens's full response to
  `.harness/tmp/merge-gate-lens-<id>.txt` (in-worktree, gitignored — the permission guard
  auto-allows the wrapper only on in-worktree paths) and, once all three files exist, run
  `HARNESS_LANE_ID=<lane-id> just merge-gate-emit-all --pr <PR#> --arc-id <arc-id> --concurrency-json .harness/tmp/merge-gate-lens-concurrency.txt --spec-json .harness/tmp/merge-gate-lens-spec-conformance.txt --witness-json .harness/tmp/merge-gate-lens-witness-adequacy.txt`
  (`--arc-id` is the RESERVATION id, e.g. `u-he-34` — omitting it defaults the row's
  `arc_id` to `pr-<N>`, which breaks the join N6 and the reservation phase rows key on;
  U-HE-34 r6. `<lane-id>` is this lane's `.harness/.lane-id` content, typed as a literal:
  a bare call records the `-nolane` fallback in every row's `lane_id`, which C-HE-24 §6
  requires to name the lane — every lens row recorded before U-HE-39 carries that
  fallback). It records concurrency, spec-conformance and witness-adequacy in that
  order, ALWAYS all three (a recorded BLOCK is a result, not an abort), and exits with the
  worst of the three per-lens codes below. **Repair, never re-run:** if one lens exits 2,
  re-run THAT lens and record it alone with the per-lens form
  `HARNESS_LANE_ID=<lane-id> just merge-gate-emit --pr <PR#> --arc-id <arc-id> --lens <id> --verdict-json .harness/tmp/merge-gate-lens-<id>.txt`
  — re-running `emit-all` would mint a fresh round for the two lenses that were fine (the
  duplicate-gate-round noise Task 0 measured). There is no resumption: the JSONL is the only
  record, and nothing is skipped on its say-so.
  Each lens's record parses the fenced JSON against the schema, holds it to the binding, requires the final
  `VERDICT:` line to agree with it (exact-line match), and writes the
  `.harness/merge-gate-log.jsonl` rows FIRST and a structured `.harness/merge-gate-log.md`
  line second. Exit 0 = APPROVE recorded, 1 = BLOCK recorded, **2 = NOT recorded (no schema
  block / binding mismatch / unwritable JSONL) — the lens verdict does not count; treat as
  BLOCK-equivalent and re-run that lens** (a bound `reviewer_unavailable` marker is written
  when the JSONL is writable). A verdict that was never recorded is not a verdict.

## Gate outcome

- **All three `APPROVE`** → proceed to merge without HIL (consistent with the standing
  `[[feedback-merge-without-hil-once-ci-green]]` directive — CI-green is a precondition, this
  gate is now an additional one for code-touching PRs).
- **Any `BLOCK`, or a split verdict** → do **not** merge. If the block names a concrete,
  narrow, fixable defect: fix it, then re-run the logged review invocation (`just review-with-failover-logged .harness/tmp/<arc-id>-rounds/r<N>.log` -- the U-HE-34 canonical form; the bare recipe produces no round log) to convergence and re-run
  this gate. **Absorption adjudication (C-HE-24 §5, U-HE-47):** when a gate `finding` row's
  fix is absorbed (or the finding is refuted), append its disposition —
  `HARNESS_ARC_ID=<arc-id> just merge-gate-adjudicate --finding-id <id> --disposition accepted|rejected --actor claude_absorber`
  (the `HARNESS_ARC_ID=` prefix is REQUIRED for the guard's auto-allow and is
  holder-bound: the CLI refuses an arc this lane's reservation does not hold, and a
  target row from any other arc; the `finding_id` is on the emitted JSONL row;
  `--actor` must differ from the lens producer, write-time enforced; exit 2 = not
  recorded, re-run). Rejected dispositions
  keep a `unique_catch=true` row from counting (C-HE-29 §2). **Every ten rounds is a
  recorded-decision checkpoint, not a cap** (period set by operator decision, 2026-08-01;
  recast under C-HE-21 §1, v1.5 X5, ratified 2026-08-25): an eleventh substantive
  disagreement stops automatic fix-and-re-gate until a recorded decision continues or holds.
  Continuation is unbounded — the next checkpoint falls ten rounds later — and the loop never
  grants its own. Both failure shapes are real: without the checkpoint, auto-fix-and-re-gate
  is an infinite loop in autonomous mode; with a cap, review is shortened exactly when it is
  still paying. At round ten you will think "this is looping; call it and merge on what we
  have." That is the moment the checkpoint exists for — ask, don't stop: PR #1034 produced
  genuine findings through round 48 of 49, and round two's measured P1 rate (75%) ran above
  round one's (62%).
- At the checkpoint, or immediately for a judgment-call disagreement (not a mechanical defect):
  surface via **one batched `AskUserQuestion`** showing all three verdicts verbatim, which
  ones disagreed, and the late-round evidence above. Let the operator decide — this is a real
  fork per §12.4.1, not routine progress to auto-resolve.
- **Always report the three verdicts**, even on a clean all-approve — the `emit-all` call
  above (plus any per-lens repair) is the machine record (JSONL first, structured md line
  second). **After a per-lens repair the gate outcome is the worst of the three RECORDED
  rows at the reviewed head, never the last exit code you saw** — a repaired APPROVE
  beside an earlier recorded BLOCK is a split verdict, not all-approve; `just
  merge-gate-log-check` is the C-HE-23 §2 consistency reducer); additionally append the
  narrative row to `.harness/merge-gate-log.md` (`PR#`, date, branch, three verdicts, outcome, plus
  `blast-radius: <n consumers>` or `blast-radius: NOT RUN (<reason>)`) so "report where
  they disagreed" is auditable after the fact, not just stated in the turn's response. The
  blast-radius field is logged even when it is `NOT RUN`: a missing field and a field
  recording that the pre-flight could not run are different facts, and only one of them is
  recoverable later.
- **Commit the gate rows before merging.** The emitted `.harness/merge-gate-log.jsonl` +
  `.harness/merge-gate-log.md` rows are TRACKED records: commit + push them on the PR branch
  (a gate-row-only delta — the same practice as today's narrative row; it never re-opens the
  lens verdicts, which are bound to the reviewed head), wait for CI at that final head, and
  only then merge. A record left as dirty local state is lost with the worktree and is not a
  record (mirror of the Codex carrier's "commit and push the gate-log row before merge").
  **The approvals transfer to that final head ONLY if `just merge-gate-landing-delta
  <reviewed-head>` exits 0** (the reviewed..final diff names nothing but the two gate-log
  files); any other file in that delta is unreviewed change — re-run the gate.

## Standing constraints — live carriers, no round cap (C-HE-21, C-HE-35)

Invariants bind by live carriage (C-HE-21 §2), not by an appeal to their number. Each one this
gate leans on names the text that carries it today:

- **#5 is live** (no verdict inferred from absence) in this skill's `## Parsing — fail closed`
  and in `ship-pr`'s `## Pre-merge gate — CI green + decorrelated 3-lens review (before the
  merge door)`.
- **#14 is C-HE-19**: CANCELLED is INCOMPLETE, never green — carried by `ship-pr`'s post-merge
  CI check and by `tools/merge_door.py`.
- **invariant #16 is void**: C-HE-21 §2 found no concurrent-reviewer-cap carrier in `.claude/`,
  `tools/`, `justfile` or CLAUDE.md. It throttles neither lenses nor lanes.

A later appeal to a numbered invariant cites its live carrier the same way, or it does not
bind. No flat round cap anywhere (C-HE-21 §1): the checkpoint in the gate outcome above
punctuates review and never shortens it.
No eval-harness / model-judge as a governance gate (C-HE-21 §4).

Grounding-gate dispositions this gate inherits (C-HE-35):

- **K5 —** structured findings require location, observed evidence, expected contract and a
  reproduction basis (the C-HE-24 shape); admissible alternatives are optional, not mandated.
- **K6 —** dropped: a reviewer never acquires authority to suppress its own finding by
  self-classifying its scope. A lens may record scope metadata; suppression belongs to a second
  decorrelated lens, a deterministic rule, or a logged operator override, and every suppression
  leaves an audit row. Ambiguous scope blocks.
- **K7 —** routing by arc type and finding class is deferred until their predictiveness is
  measured (C-HE-26 §3); shadow mode only, if run at all.
- **K8 —** a blocking post-edit hook is admitted only for fast, deterministic, low-false-positive
  checks at a stable boundary (C-HE-31 §3), not on every intermediate edit.

## Wiring into `ship-pr` / the loop

`ship-pr/SKILL.md` invokes this skill in its pre-merge section — after CI green is confirmed
and the logged review invocation (`review-with-failover-logged`, U-HE-34 canonical) has converged, before the actual `gh pr merge`. `roadmap-continue` →
`ship-pr` is the loop path this composes into; no changes needed to `loop-start`/`loop-stop`
(the gate is a step inside `ship-pr`, not a separate autonomy tier).
