---
name: ship-pr
description: Use when the operator says /ship-pr, ship it, open the PR, land this, or a completed arhugula-v2 arc must pass review, CI, merge, refresh, and cleanup.
---

# Ship PR

Close an arc through the same fixed-point workflow used by the Claude runner. Read
`AGENTS.md`, `.codex/notes/discipline-digest.md`, and the live `CLAUDE.md` §12.2/§12.2.1
before executing; those authorities win if this summary drifts.

## Preflight and local gates

1. Confirm the absolute worktree path, branch, status, and merge-base diff. Never mutate
   the shared root checkout and never use `git add -A`.
2. Recompute affected roadmap, register, ledger, and cite surfaces before committing.
3. Run the narrow witness first, then `just codex-check` for a substantive PR. Run
   `just overlay-check` for cite/CXA-bearing changes and `bash -n` for shell changes.
   Cache-warmth handoff (U-SR-07/WR-14): at >400k context, before any background wait
   expected to outlast the prompt-cache TTL (this gate, the CI/door waits later in this
   skill), prefer closing out to a handoff over idling through the expiry — one cold
   re-warm re-reads the whole context (≈0.7M IET on the [B] F4 baseline); "the wait
   costs nothing" bills the next call.
4. Run `just codex-closeout`. If the diff changes afterward, re-run every affected gate.
5. Review the actual diff, stage only explicit paths, and inspect the staged diff.

Use the real worktree as `cwd` rather than `git -C` so durable command-prefix approvals
match. The normal command shapes are:

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short --branch
git diff --merge-base main
git add -- <explicit-path> [<explicit-path> ...]
git diff --cached --stat
git diff --cached
```

## Grounding pass (U-WT-01)

Before the first out-of-family review round: re-read every `file:line` cite in the diff
and PR body against the exact content under review (the staged/worktree diff — in this
flow local gates and review precede the commit, so HEAD does not yet contain it) — never
from recall; recompute every count/arithmetic claim from the actual source rather than
restating it; confirm every `#NNN` reference actually is that PR; confirm local gates ran
against the *current* staged/worktree fingerprint and re-record the pass if the diff
changes afterward; state in the PR body that this pass ran. First drafts historically
burn 5–10 review rounds on exactly these defect classes.

## Per-round mechanical self-check — `just leg-selfcheck`

Run it BEFORE EVERY PUSH, not once per arc (`--uncommitted` before committing). It
re-resolves every `file:line` cite the arc ADDED, reports count claims that DISAGREE
for the same subject, surfaces a minted `§label` already used elsewhere in the artifact
family, and asserts a touched register row renders a prose body under
`--detail` (a YAML-only row prints its heading and nothing else; a NEW row must also
carry a `**Current state.**` bullet and must not lead with an instruction).

Why per-round: the `B-71` spec leg took ten review rounds, and rounds 7–10 found defects
the *absorption rounds themselves* introduced. Running the global checks once per arc
cannot catch defects introduced per round.

## Authorship-dependent out-of-family review

- When Codex authored the change, run `just gemini-review`. Despite its legacy recipe
  name, it invokes the Antigravity `agy` subscription CLI with provider API environment
  variables stripped. Require exit 0, non-empty output, and final `VERDICT: APPROVE`.
- When Claude authored the change, `just codex-review` is the out-of-family gate.

A BLOCK means fix, add a regression witness where applicable, re-run local gates, and
review the new diff again. Never count self-review by the authoring model as decorrelated.

## Commit, PR, and CI

1. Commit the explicit staged scope and push the topic branch. This carrier does not run
   `just codex-context-check-ci`, the Claude carrier's CI-shaped pre-push guard (U-HE-42,
   C-HE-33): an active Codex loop binds its pre-closeout gates to the pre-commit HEAD, so
   `check` reports `CODEX_LOOP_INCOMPLETE` from the commit until every ship gate is
   recorded, a local-only finding CI never sees.
2. Disjointness re-check at ship (U-HE-36; B-228) — AFTER that commit, so `HEAD` carries
   the arc's changes (codex u-he-36 r8: before it, staged work is invisible to the gate):
   run `uv run python tools/arc_disjoint_check.py check --candidate HEAD` (the same
   guard-allowlisted shape as at arc open). Exit 1 names the live sibling whose head
   textually conflicts — rebase or resolve, re-commit, and re-run before opening the PR
   (the door's `BASE_TOCTOU` refuses the landing otherwise; C-HE-13 §5); exit 2 → surface
   the printed cause.
3. Open or update the PR. Its body names the tracking surfaces updated (or why none apply),
   exact verification results, skipped checks, and design/back-flow posture.
4. Watch every required PR check on the final PR HEAD and every required check on the current
   base `main` HEAD to a terminal green conclusion. Pending, missing, empty, cancelled, or
   skipped-required checks are not green. Inventory open PR and remote topic branches before
   merge; if a stale prior branch exists, inspect its PR/worktree/unique commits and reconcile
   it without deleting or overwriting work.
5. Run the `merge-gate` skill for every substantive code or hook PR after PR CI is green
   and out-of-family review has converged. This is the fresh three-lens gate, not a second
   generic review. A documentation-only or terminating-refresh PR may take a proportional
   skip, but the skip and evidence must be logged.
6. Append the result to `.harness/merge-gate-log.md`, commit and push that row, then wait
   for CI on the final PR HEAD to be green again. If a code fix follows any approval,
   re-run out-of-family review and the affected lens against the delta.

Merge only when the operator's request or standing autonomous-loop authorization includes
merge. All three lenses must approve and final-HEAD CI must be green. Never use `--admin` to
bypass protection.

```bash
git commit -m "<conventional title>"
git push -u origin <topic-branch>
gh pr create --base main --head <topic-branch> --title "<title>" --body-file <body-file>
gh pr checks <PR#> --watch
gh pr merge <PR#> --squash --match-head-commit <final-head-sha>
```

Validate each external call before using its output: exit 0, non-empty JSON where expected,
and the requested PR/head SHA. Do not interpolate an empty PR number, SHA, branch, or run id.

## Post-merge fixed point

1. Record the merge SHA and wait for the merge commit's own main CI run to conclude green.
   PR CI is not a substitute. Do not begin forward work while main CI is pending or red.
   Discover runs with `gh run list --branch main --commit <merge-sha> --event push
   --json databaseId,status,conclusion,headSha`; require at least one matching run, watch every
   returned run with `gh run watch <run-id> --exit-status`, then re-query and require every
   conclusion to equal `success`.
2. Perform branch hygiene only against the exact merged PR/head OID. Recheck remote state;
   do not infer squash-merge ancestry. Preserve any branch with new or unmerged work.
   Force pushes are denied to an unattended lane by design: record the deferral with
   `bash tools/04-loop/defer.sh <arc-id> "branch hygiene close-out pending: <branch> (PR #<N>, merged <merge-sha>, main run green) and roadmap-refresh-post-<N> (PR #<refresh-N>, merged <refresh-merge-sha>, main run green)"`
   — both branches in the ONE row, in exactly this shape (`tools/branch_hygiene_batch.py`'s
   `parse_pending` is the authority on it; B-230 Task 4). In the next interactive session run
   `just branch-hygiene-pending`, paste the printed push (one approval clears every verified
   row), then run `just branch-hygiene-resolve` — it appends the `RESOLVED-HIL` row for each
   item whose branches are gone on origin, which is what makes the reducer stop presenting
   them; it is safe to rerun.
3. **Never put a `roadmap_status.md` edit in a substantive content PR.** A commit on
   `main` that touches it without being a *verified* terminating refresh satisfies
   neither guard exception (`_lag_expected` wants the refresh shape; `_owed_lag` requires
   HEAD *not* to touch the file), so main CI hard-fails `ROADMAP_STATUS_DRIFT`.
4. Archive the SUPERSEDED round (N-1) inside the substantive content PR:
   `python3 tools/roadmap_status_refresh.py --archive-superseded`. It reads the
   most recent no-longer-live round from the head's git history and appends it to
   `.harness/roadmap-next-action-archive.md` as `Prior`. Archive-only write, so
   the content merge stays the single non-refresh commit `_owed_lag` tolerates.
   Archive N-1, never the live round: that is `B-168` exit (iii), which satisfies
   the prior-only invariant, the one-file refresh rule and the lag tolerance all
   UNCHANGED. The archive lags one arc by design; git history is lossless.
5. Then run the terminating refresh, which installs the new pointer AND refreshes
   the anchor in ONE single-file write:
   `python3 tools/roadmap_status_refresh.py --refresh --pr "PR #<N>" --date
   <YYYY-MM-DD> --notes "<summary>" --next-action "<new pointer prose>"`, then
   `--check`. NEVER hand-edit the next action -- that is what reddened `main` on
   2026-08-13. `--refresh` REFUSES to write a second file; if it says a drift-log
   trim is owed, run `--trim-drift-log` as its own step, and if the overflow comes
   from a NEW drift event pass it there
   (`--trim-drift-log --drift-source ... --drift-resolution ... --date ...`).
6. Land the terminating refresh as the immediate next commit/PR. Its title starts exactly
   `ops: roadmap status refresh ` and its closed changed-file set is only
   `.harness/roadmap_status.md`. Do not recurse on a terminating refresh.
7. Wait for the refresh merge's own main CI to be green and fast-forward local `main`.
   Order the arc exit report before removing the arc worktree and pruning the topic branch.
   The ledger itself is no longer at risk from disposition — since U-HE-29 it is the SHARED
   venue (`$(loop_status_path)`; default `~/.gstack/projects/arhugula-v2/loop_status.md`),
   outside every worktree, and no disposition path deletes it. The report is still ordered
   first because it records the worktree's own closure facts.
6. Record loop gates through `worktree_disposition` (the disposition itself now happens
   after the exit report) and require `just codex-loop-check`.

## Reflect and checkpoint

Before the next arc, reflect on new recurrent lessons and run the `context-save-lean` skill
(`.agents/skills/context-save-lean/`, the workspace copy of the gstack save flow — U-SR-08/WR-15;
the gstack-level `context-save` skill re-injects ~54 KB of preamble per call).
Update durable agent memory only when the operator explicitly requests it and the active host
memory policy permits it; context-save-lean itself remains mandatory. A checkpoint's remaining-work
prose is advisory, so verify it against HEAD when resuming. After the fixed point **and the
arc exit report below**, create the next isolated worktree and run
`just codex-autonomous-arc <next-arc-id>` — launching the next arc first would let it alter
loop/checkpoint state before the prior arc's report is collected. Skip reflection only
for a pure terminating-refresh PR.

Facts-brief handoff for a heavy next item (U-SR-07/WR-14): if the next action is a heavy
audit or document, the closing session writes the facts brief only — the findings, cites,
and decisions the deliverable needs, written BEFORE running context-save-lean so the
checkpoint carries it — and a fresh session authors from the brief; "I already have the
context loaded" is the trap ([B] F10: authored at 540k context cost 0.93M IET against
≈0.3M fresh).

## Arc exit report (U-WT-03/04)

Run this as the last REPORTING step — after the reflect and `context-save-lean` step above, never
before it, BEFORE the arc worktree's disposition, and BEFORE launching the next arc. (The
pending-HIL ledger it reads is the SHARED venue since U-HE-29, so disposition no longer
destroys it — the ordering now exists for the closure facts below, not to outrun a delete.)
Only
at that point do the merge SHA, the post-merge main CI conclusion, the terminating
refresh commit, and the checkpoint just written all exist, so the report records the
arc's real final checkpoint instead of a stale one. Skip entirely for a pure
terminating-refresh PR (not an arc; no report owed).

It is NOT the last step of the arc: the arc-metrics capture below still has to run,
and it has to run BEFORE this worktree is disposed and BEFORE the next arc is
launched. Disposing or moving on after the report — which an earlier reading of
"last within the arc" permitted — drops this arc from the ledger entirely, which is
exactly the Codex-cohort gap the capture section exists to close.

```bash
just arc-close <NNN> <merge-sha> <the-path-context-save-lean-just-reported> \
  --arc-id <arc-id> --arc-type <inventing|applying> --decisions <N> \
  --round-logs '<glob for THIS arc's round logs>' --levers <lever-ids-or-omit>
```

This ONE call (B-230 Task 3) runs the exit report, then `arc-metrics queue` with everything
after the three positionals forwarded verbatim; `just` stops at the first non-zero exit,
so a failed report never queues a metrics row. The queue's argument rules are in the
arc-metrics section below.

Loop-mode note (same as the Claude carrier): the permission guard auto-allows this call
only with explicit round-log paths (no `*` — the `just` token grammar has none;
`--round-logs` takes many paths); the quoted glob above surfaces one approval, as
`arc-metrics queue` always did. Headless, pass the paths.

The third positional is the checkpoint path — pass it explicitly, never a guess. The roadmap authorizes a parallel frontier, so another live
session can write a checkpoint between this arc's save and this collection; mtime cannot
discriminate ownership, so an unbound run reports the workspace-newest file as an
unconfirmed heuristic and `checkpoint.confirmed` stays `false`. Only the path this arc's own
`context-save-lean` step reported binds the report to this arc.

Validate the call before using its output: require exit 0 and the named report path. The
recipe writes `.harness/.checkpoints/arc-exit-report-pr<NNN>.md` (gitignored, PR-keyed, so a
re-run overwrites rather than orphaning a sibling) and appends one `EXIT-REPORT` row to
the shared `loop_status.md` (`$(loop_status_path)`; default `~/.gstack/projects/arhugula-v2/loop_status.md`). Paste the emitted `yaml` block into the final response as the
arc's machine-readable closure record. Every field is collected or explicitly null — a null
`refresh_commit`, a `main_ci.conclusion` other than `success`, or `checkpoint.confirmed:
false` each mean a closeout obligation above is still open.


## Arc-metrics capture (B-170)

Capture is two steps that sit in DIFFERENT arcs: this arc queues its inputs, the next arc
folds them into the ledger. Skip both on a terminating roadmap-status refresh -- a refresh
is not an arc. This is runner-parallel with the Claude carrier at
`.claude/skills/ship-pr/SKILL.md`; an arc shipped from either runner must appear in the
ledger, or every Codex-run arc is silently missing from the baseline.

Step 1 -- queue, at closure, after the exit report (writes NOTHING to the repo): runs as
the second step of the `just arc-close` call above. Pass `--arc-id <arc-id>` (the ARC id
from the reservation, never the PR number: a `pr-<N>` default breaks the `arc_id` join
between the ledger row, the reservation and the gate log).

OMIT `--transcript` on this runner: Codex session transcripts are date-partitioned
rollout JSONL under `~/.codex/sessions`, a shape `tools/arc_cost.py` does not parse
(it reads Claude session transcripts), so there is no truthful cost input to pass --
the C-HE-25 X6e cost fields read as null on Codex-shipped arcs, an honest
could-not-look, never a guessed transcript's numbers. This is a NAMED cohort gap
(the cost baseline under-covers the Codex runner) until a rollout-shaped extractor
exists; the Claude carrier passes the flag.

One file per arc, in a queue directory OUTSIDE the repo. That placement is load-bearing:
writing the tracked ledger from inside a topic worktree leaves a dirty file that both
strands the row when the worktree is disposed and blocks the disposal itself. `--arc-type`,
`--decisions` and `--levers` are DECLARED judgements, required here because only the closing
session knows them; omitting `--levers` records the empty baseline cohort `[]`, which is
itself the claim that no wall-clock lever was live. The globs are resolved at closure and the
derived metrics frozen, so later edits to those logs cannot change what this arc measured.

Step 2 -- drain, early in the NEXT arc, before opening its PR:

```
HARNESS_LANE_ID=<lane-id> just arc-metrics drain
```

`<lane-id>` is the persisted `.harness/.lane-id` content, typed as a literal: drain folds a
`merged` reservation's row only for its holder lane, and a bare call runs under a fallback id,
so every merged arc stays held.

Folds queued arcs into `.harness/arc-metrics.jsonl`, committed inside that arc's own PR
(stage the path explicitly; never `git add -A`). `drain` exits NON-ZERO while anything is
outstanding, and that is not a failure: a capture is released only once its row reaches
MERGED history, so the normal sequence is drain (exit 1, entry held) -> commit and merge ->
the next arc's drain releases it (exit 0). Only exit 0 means nothing is left to fold.
