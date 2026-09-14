---
name: two-lane
description: Operator-invoked pilot recipe for running two or more (N ≥ 2) roadmap arcs in parallel worktrees while keeping every merge and every terminating refresh strictly serial through ship-pr. Use when the operator explicitly asks for two lanes ("/two-lane", "run two arcs in parallel", "open a second lane") — it is never auto-selected from roadmap-continue or ship-pr. It is a recipe, not machinery: no spawner, no merge queue, no conflict automation.
---

# two-lane — N ≥ 2 arcs building in parallel, one merging at a time

N ≥ 2 lanes build concurrently in isolated worktrees, each with its own gates and reviewers,
and land through exactly one merge door, one arc at a time (C-HE-01 §1). Arcs can be *built*
concurrently; they cannot be *landed* concurrently: CLAUDE.md §12.2.1 gives the merge lane a
single fixed point, and every landing has to pass through it in order. N is a dial, not a
constant (C-HE-01 §2). The recipe below walks two lanes, A and B, because the serial rule is
pairwise: a third lane waits behind B exactly as B waits behind A.
Throughput: well under N×; merges serialize; trailing lanes re-gate on head change —
**prior, not measurement** until AC#10 (C-HE-28) produces a baseline. "Four lanes, four times
the arcs" is exactly the claim this forbids; nothing measured supports it yet.
This recipe is the whole feature — a discipline written down, running on machinery that already
exists (`git worktree`, `ship-pr`, `tools/hooks/safe-worktree-remove.sh`). Nothing here is
automated, deliberately (see the CUTs at the end).

## Opt-in only

**Opt-in only — never auto-invoked from `roadmap-continue` or `ship-pr`.** It fires on an
explicit operator request. Declared `scope.files` is a scheduling HINT (the forward register
carries no such keys); the gate is `tools/arc_disjoint_check.py` (U-WT-07, landed at
U-HE-36) at selection — `roadmap-continue` merge-trees the candidate against every other
lane's live head before `reserve` — plus `BASE_TOCTOU` at landing (C-HE-13 §5).

## Lane setup

Each lane gets its own worktree under `.codex-worktrees/<slug>` — the existing, already-ignored
worktree root (`.gitignore:103`), and one of the two roots the permission guard admits for both
worktree creation and guarded removal (`tools/hooks/permission-guard.sh:179` and `:203`):

```bash
git fetch origin
git rev-parse origin/main           # pin ONCE — note the SHA it prints
git worktree add <repo-root>/.codex-worktrees/<slug-a> -b <branch-a> <that-literal-sha>
git worktree add <repo-root>/.codex-worktrees/<slug-b> -b <branch-b> <that-literal-sha>
```

At lane start, inside each worktree: `source tools/hooks/lane-init.sh` (exports `HARNESS_LANE_ID`,
`HARNESS_LANE_INDEX`; C-HE-11). The index is what keeps the two lanes' Docker project names,
host ports and volumes disjoint — without it both lanes bring up the same stack and the second
`docker compose up` fails on a port bind.

Use the LITERAL SHA in both adds, not a shell variable: in loop mode the permission guard
rejects multiline commands and `$var`/command-substitution forms, and separate Bash calls do
not share shell locals — a `"$base"` setup stalls non-interactively even on an admitted path.

Both lanes branch off the **same** pinned base SHA (resolving `origin/main` twice is a TOCTOU:
any other session's SessionStart hook fetches on every start and can advance the ref between
the two adds). Each lane then runs its arc's BUILD
half normally — build, test, `just codex-check` (ALWAYS launch it `run_in_background` and poll
the task, U-SR-05/WR-12: the suite outlasts the Bash tool's foreground timeout, and a foreground
launch cost a 10-minute dead gap at [B] F6; cache-warmth handoff, U-SR-07/WR-14: at >400k
context, before any background wait expected to outlast the prompt-cache TTL, prefer closing
out to a handoff over idling through the expiry — one cold re-warm re-reads the whole context
at ≈0.7M IET ([B] F4), and "the wait costs nothing" bills the next call), grounding pass, out-of-family review — entirely
inside its own worktree. Nothing about the build half is serialized. **`merge-gate` and the
final-head CI check are NOT build-half steps**: the gate runs immediately before merge, appends
the shared `.harness/merge-gate-log.md`, and its approvals must cover the branch that will
actually merge — so lane B runs them only inside its own merge sequence, after lane A's
terminating refresh has landed (below).

## The merge lane — strictly serial

**Merges and terminating refreshes are STRICTLY SERIAL through `ship-pr`. §12 is unchanged by
this skill** — this recipe adds no governance, weakens no gate, and introduces no alternative
landing path. The §12.2 post-merge audit and the §12.2.1 terminating-refresh fixed point apply
to each lane's PR exactly as they do to a single-lane arc.

**One lane holds the `ship-pr` fixed point at a time.** Concretely, lane B does not begin its
merge sequence until lane A's *terminating refresh PR has merged* — not merely lane A's content
PR. The refresh is what returns `.harness/roadmap_status.md` to the fixed point; a second merge
landing between lane A's content PR and its refresh leaves an accumulated two-commit drift that
hard-fails `tools/codex_context_guard.py` for everyone (CLAUDE.md §12.2.1, CI-side
generalization). So the order is: A content → A refresh → B content → B refresh. Lane B keeps
building while it waits; only its merge sequence blocks.

**Lane B's merge sequence starts with a replay, then the FULL ship-pr flow — not a jump to the
gate.** Once lane A's terminating refresh has merged AND its `main`-push CI has concluded green
(a pending or red refresh CI means the base is unverified — wait), lane B fetches and replays
onto the refreshed `origin/main` (fresh branch off refreshed main + re-apply, per the conflict
section below — this is unconditional, not just for visible conflicts: lane A's merge-gate run
appended the shared `.harness/merge-gate-log.md`, so stale lane B's own log append would
conflict at the table tail every time). Then lane B restarts the whole `ship-pr` sequence on
the replayed branch: a **replacement PR** — where an original PR exists at all (a lane B that
had not yet reached ship-pr's open-PR phase has none, and these close/replacement steps then
reduce to simply opening the PR on the replayed branch); the original PR is attached to the
abandoned branch and never merges — **close the original PR first** with a comment pointing at
the replacement,
and route its stale REMOTE branch to the operator via the tracked prune row — which carries
BOTH the local and the remote deletion commands — because ship-pr's stale-branch flow requires
a MERGED PR with a merge commit and structurally cannot reconcile a closed unmerged one; so
the replacement doesn't merge around an open ghost in the in-flight inventory), out-of-family review of the **current** diff (conflict-driven redone edits
were never covered by the pre-replay review), `merge-gate`, and final-head CI. Gate approvals,
reviews, and CI are branch-and-HEAD-bound evidence; recording any of them before the replay
would attest to a branch that never merges.

## On merge conflict — abandon and rebase

If lane B's PR conflicts with the now-refreshed `main`, **abandon the second lane's branch and
rebase it on the refreshed main — no merge-order heuristics, no cherry-pick rescue of a
conflicted merge.** Do not reorder the lanes to make the conflict go away, do not try to land
B first because it looks smaller, do not hand-resolve a merge commit.

Two guard facts shape how that is actually executed:

- `git rebase` is hard-denied by the permission guard (`tools/hooks/permission-guard.sh:370-371`,
  "git history rewrite"). The rebase is therefore realized as: branch fresh from the refreshed
  `origin/main` and re-apply lane B's work there (`git cherry-pick <sha>...` on the *clean* new
  branch, or simply redo the edits — the arc is small by construction).
- Deleting the abandoned branch is a privileged operation: `git branch -d` refuses an unmerged
  branch and forced `-D` is hard-denied (`tools/hooks/permission-guard.sh:372-373`). Abandoning
  means *not merging it*; leave the branch in place and hand the operator the deletion command
  if it needs to go.
- **The durable prune reminder is a TRACKED row, shipped in the replacement PR:** append the
  abandoned branch's name + the operator's exact prune command to
  `.harness/two-lane-pending-prunes.md` (create the file on first use; the operator deletes
  the row when pruning). No gitignored, worktree-local surface can carry this reminder — the
  loop ledger in particular is deleted with the reaped worktree or wiped at the next loop
  activation, and the merged-refs-only hygiene sweeps cannot see an unmerged branch — so a
  tracked file in the repo is the only home that survives until the operator acts. Until the
  row is cleared, it also answers the pre-merge branch-hygiene precondition: the branch is
  accounted for, not stray.

## Reaping a lane

A finished lane's worktree is **reaped ONLY via `tools/hooks/safe-worktree-remove.sh`**:

```bash
just r420-self-hosted-stack-down                 # FIRST, from inside the lane, if it ran a stack
bash tools/hooks/safe-worktree-remove.sh <repo-root>/.codex-worktrees/<slug>
```

Bring the lane's stack down **before** reaping. A successful removal frees the lane's
`QUEUE_DIR/lanes/<k>` claim (C-HE-11 §1), and the next lane to take that index inherits its
project name, ports and volumes — containers left running would hold them, and that lane's
`up` fails on a port bind. The removal hook cannot do this itself without making teardown
depend on a Docker daemon that is usually absent.

Direct `git worktree remove` is denied for **every** parsed removal
(`tools/hooks/permission-guard.sh:56-70`; the only bypass is the deliberate
`HARNESS_ALLOW_LIVE_WORKTREE_REMOVE=1` escape hatch) — the liveness check refines only the
denial *reason*, never gates the deny, because a negative liveness check can itself race a new
SessionStart (`:52-54`). Removal and SessionStart lease registration must share one mutex, or
the removal orphans a live session.
A nonzero exit is a real refusal, not a retry prompt: 3 = live session, 4 = local state,
7 = retained process references (`tools/hooks/safe-worktree-remove.sh:45-57`). Surface it; never
force it.

## Deliberate CUTs

**No spawner script, no merge-queue lock, no conflict automation.** Each is cut for a reason,
not for lack of time:

- **No merge-queue lock.** The §12.2.1 fixed point already admits exactly one in-flight landing,
  so the queue is *structurally depth-1 — a lock is ceremony* on top of an invariant the roadmap
  protocol enforces anyway.
- **No spawner.** Two `git worktree add` lines are not a program.
- **No conflict automation.** The abandon-and-rebase rule above is total; automating it would
  encode merge-order heuristics, which is precisely what the rule forbids.

**Two distinct gates, and conflating them is the AC#1 ambiguity C-HE-13 §3 exists to
resolve.** Phase 0 green (`just lanes-phase0-check`, which `just lanes-pilot` refuses to
start without) gates running N ≥ 2 lanes **at all** — the pilots are simply the first N-lane
runs and therefore sit behind it too. The ≥ 3 pilot runs at 3–4 lanes gate **only follow-on
lane orchestration** (automated spawning), never the right to keep running N lanes manually.
Follow-on orchestration is
registered only after ≥3 manual pilot runs surface a named recurring pain — a specific,
repeated, described friction, not a hypothetical one; in C-HE-13 §3's terms a `cause_signature`
appearing in ≥ 2 of the ≥ 3 pilots, or one occurrence the operator rates independently severe.
`just lanes-pilot-report <run-id>` prints the PASS/FAIL and the friction rows it is judged on.
Until then this file is the whole feature.

## Rejected and blocked mechanisms (C-HE-14)

The spec's normative "do not build" list, copied byte-exact from C-HE-14 in
`.harness/spec/Spec_HE_Loop_Lanes_v1.md` — the row cites are the spec's own, as of its
clearance, not re-verified here. When lanes contend, one of these will look like the obvious
fix: a lock held across the drain, a small coordinator process, a union merge driver for the
shared logs. Each was weighed and refused. Building one is a design change that routes to
design-phase back-flow (CLAUDE.md §4.3), not a lane-level tweak.

| Mechanism | Disposition | Reason |
|---|---|---|
| Full-lifetime `flock` across drain | **Rejected** (R-6) | Two remote calls inside the proposed window (`gh_pr` `:284`, `ci_metrics` `:376` **[C]**); auto-releases on death; mechanism-family change |
| Daemon / coordinator / spawner / merge-queue lock | **Foreclosed** (L-2) | D3 fail-fast exists so no one builds daemon-shaped arbitration unnamed |
| Local base CAS via raw `PATCH /git/refs` (item 20) | **Blocked, stays blocked** | Squash/ancestry: auto-close never fires and ship-pr aborts on non-`MERGED`; **and** trust boundary: a raw ref PATCH is content-blind (C10). Only the read-only `local-base-cas-check` survives (C-HE-06 step ii) |
| Integration-lens gate (item 21) | **Blocked** until its contract survives review; when built it MUST route through `just codex-review` on the **merge-tree diff**, not another Claude subagent (v1 AC#6) |
| `loop_status.d/` fragment split | **Rejected** (P3, R-16) | C-HE-09 |
| Post-hoc first-parent assertion **instead of** the lease | **Rejected** as a fence (R-8); **kept as a detection** (C-HE-12) | Detect-after-landing |
| A third durable store for landing state | **Rejected** (L-5, D-B) | `merge_attempted_at` folds into the lease |
| sqlite as the durable record | **Rejected** (L-5) | New DB+WAL surface, no correctness gain, loses git-diffability |
| `merge=union` | **Rejected** | git concedes arbitrary line order; `.gitattributes` ties LF forcing to hash-chain determinism |
| Optimistic stale-base merge (D1(c)) | **Rejected** by every reviewer | Abandons combination testing |
| Removing `gh pr merge` from the allowlist **without** a wrapper | **Rejected** (R-19) | Superseded by C-HE-07 |
