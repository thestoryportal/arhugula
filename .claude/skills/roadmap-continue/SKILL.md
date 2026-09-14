---
name: roadmap-continue
description: Run one iteration of the workspace roadmap loop — the "continue" ritual — end to end. Use when the operator says "/roadmap-continue", "continue", "drive the roadmap", "next action", or otherwise asks the agent to pick up and advance the next roadmap item. Codifies the §12 loop (session-start audit → derive next action → ground empirically → implement with tests → PR) so it runs the same way every time. Do NOT use to merely report status (that is read-only) — use it to actually advance an item.
---

# roadmap-continue — one turn of the roadmap loop (U-HK-23)

The single-command form of the "continue" ritual. This skill does **not** re-state the
protocol — it **executes the canonical §12 protocol** so the recipe can never drift from
the source of truth (the §10.5 stale-carry failure mode). Read the cited sections live.

**Lane model (C-HE-01).** One turn of this loop is one lane's arc.
N ≥ 2 lanes build concurrently in isolated worktrees, each with its own gates and reviewers,
and land through exactly one merge door, one arc at a time (C-HE-01 §1); N is a dial (§2). The
step-2 reservation and disjointness gate are what let a lane run beside its siblings.
Throughput: well under N×; merges serialize; trailing lanes re-gate on head change —
**prior, not measurement** until AC#10 (C-HE-28) produces a baseline.

## The loop (each step is governed by a canonical section — follow it there)

1. **Session-start audit — CLAUDE.md §12.1.** Read `.harness/roadmap_status.md`; recompute
   `workspace_state_hash` (recipe at `Project_Roadmap_v1.md` §7.1); compare to the recorded
   value. **Mismatch → HALT + reconcile (§12.3).** Honor the §12.1 step-6 fixed-point
   carve-out: a one-commit lag after a terminating refresh PR is *expected*, not drift —
   silently update, do not spawn a refresh. (The SessionStart hook usually pre-injects the
   `[ROADMAP]` block; if so, trust it.)
2. **Derive the next action — `Project_Roadmap_v1.md` §4.** Take `roadmap_status.md`'s
   `## Next action` (the live pointer only — grep `.harness/roadmap-next-action-archive.md`
   for a specific prior round if one is genuinely needed, never read it wholesale). If the
   auto-`ACTIVE` queue is empty, apply the **no-parking directive (§12.4.1)**: pick the
   highest-value forward item, do NOT stop citing "operator-owned."

   **Arc open (C-HE-03 §4) — the instant the unit is chosen, BEFORE any work.** Selection
   IS arc open: mint the `pending` reservation now, with the `arc_type` declared now
   (C-HE-26 §1 — the open-time capture point, never inferred at close). Every value below
   is a LITERAL you type — no `$( )`, no chaining (the permission guard auto-allows only
   single clean invocations), and shell exports do NOT survive across Bash tool calls, so
   the two `HARNESS_*` ids must be restated inline on every later command that reads them:
   ```bash
   source tools/hooks/lane-init.sh                      # exports HARNESS_LANE_ID + HARNESS_LANE_INDEX
   uv run python tools/reservations.py selectable --arc-id <arc-id>
   ```
   **Lane id is minted ONCE per worktree and persisted, and `tools/hooks/lane-init.sh` is
   its ONE writer** (U-HE-31; C-HE-11 §1). Source it at lane start and read
   `$HARNESS_LANE_ID` — do NOT mint, and do NOT `Write` `.harness/.lane-id` yourself: a
   second writer is a second authority, and the non-atomic path is exactly the identity
   race lane-init's temp-then-`ln` publication removes (it also allocates
   `HARNESS_LANE_INDEX`, which the per-lane Docker project and ports depend on). Where the
   env var is not in scope (a later Bash tool call — shell exports do not survive across
   them), re-read `.harness/.lane-id` rather than trusting memory; its content IS this
   lane's id, and a fresh mint per session would generate a new random suffix and make the
   same-lane resume below misclassify this lane's own reservation as another's.
   - `selectable` exit 0 (free) → run the selection-time disjointness gate FIRST (U-HE-36;
     C-HE-13 §5: declared `scope.files` is a scheduling hint, actual-write is the gate —
     the candidate head is merge-tree'd against every other lane's live head, i.e. the
     branch of every `pending`/`open` reservation; guard-allowlisted in exactly this shape):
     ```bash
     uv run python tools/arc_disjoint_check.py check --candidate HEAD
     ```
     Exit 0 (`disjoint: …`) → reserve below. Exit 1 → each
     `CONFLICT <arc> [<lane>] <branch>: <path>` line names a live sibling whose head no
     longer merges cleanly with the candidate: textual conflict with an open lane — do NOT
     reserve; re-derive and pick the next unit. (At selection the candidate is `main`, so
     the conflict is that sibling's WIP against what landed since it branched; it fences
     selection until the sibling rebases or is abandoned, and `BASE_TOCTOU` at landing
     catches what selection-time cannot — including a sibling that reserves between this
     scan and your `reserve`, and sibling work still uncommitted: the gate reads committed
     branch tips only.) Exit 2 (`UNRESOLVED …` / `INCOMPLETE …`) → the
     gate could not run — never read it as clean: surface the printed cause (a sibling
     reservation whose branch has no local or origin ref is a C-HE-03 §5 HITL case) and do
     not reserve.
   - gate exit 0 → reserve it, then export for this shell:
     ```bash
     uv run python tools/reservations.py reserve --arc-id <arc-id> --lane-id <lane-id> --branch <branch> --arc-type <inventing|applying>
     export HARNESS_ARC_ID=<arc-id>   # same-shell only — restate inline later
     ```
     If `reserve` itself FAILS because another lane won the selectable→reserve window
     (the store's exclusive-create CAS refuses a second head — a lost race, not an
     error), treat it exactly like the exit-1 path below: `show` and branch on the
     head's `lane_id`.
   - `selectable` exit 1 (a head exists) → `uv run python tools/reservations.py show --arc-id <arc-id>` and branch on its `state` FIRST, then `lane_id`:
     - `state` is terminal (`merged`/`abandoned`) → the arc already ran, whoever's
       lane it was — NEVER resume a terminal head: re-derive and pick the next unit;
     - `state` is `pending`/`open` AND the head's `lane_id` equals the persisted
       `.harness/.lane-id` content (crash/compaction re-entry) → resume
       WITHOUT re-reserving (a second `reserve` refuses any existing head);
     - `state` is `pending`/`open` held by anyone else → the unit is taken:
       do NOT reserve — re-derive and pick the next unit.

   A second lane's selection of the same unit fails here (duplicate *scheduling* is
   prevented at open; duplicate *append* by C-HE-03 §6). `ship-pr` back-fills
   `pr`/`head_sha`/`base_sha`/`attested_merge_tree` on this same record. When a later
   command needs the ids in its environment (the review wrapper's C-HE-24/25 rows join
   the reservation through `HARNESS_ARC_ID`/`HARNESS_LANE_ID`), prefix them inline:
   `HARNESS_ARC_ID=<arc-id> HARNESS_LANE_ID=<lane-id> just review-with-failover`.
   *Gap CLOSED at U-HE-25 (the reviewed permission-guard unit): the guard now
   auto-allows (loop mode) the reservations.py carrier verbs
   `selectable|show|reserve|update|mint-lane-id|phase` exact-shape, the sourced
   `source tools/hooks/lane-init.sh` (U-HE-31, exact shape, no arguments), `transition` ONLY in the
   two-token `--to open` form (token-parsed — `=`-forms/abbreviations reject; terminal
   transitions and `gc` still surface to the operator), the leading
   `HARNESS_ARC_ID=`/`HARNESS_LANE_ID=` bareword prefix forms, and `git merge-tree` —
   so headless arcs CAN reserve at open. The degradation clause below survives as the
   generic fallback for any OTHER refusal (a non-loop venue, a future guard change):
   if ANY arc-open command above (`source tools/hooks/lane-init.sh`, `selectable`, `reserve`) is refused
   by the permission layer, proceed with the arc UNRESERVED and say so in the PR body;
   append safety still holds (the U-HE-19 drain bootstrap mints the reservation at
   closure and the C-HE-03 §6 holder gate fences the ledger). The same rule downstream:
   a refused PREFIXED review invocation degrades to the bare `just review-with-failover`
   (allowlisted; writes the pre-U-HE-21 fallback ids — witnessed as guard-ALLOW), and
   the ship-pr back-fills are skipped per its unreserved-arc clause.*

   **Queue + execute span edges (U-HE-34; C-HE-27 §1/§3).** Immediately after the reserve
   (or the same-lane resume) succeeds, record the queue-phase start edge; when grounding
   ends and implementation work actually begins, close queue and open execute — each a
   LITERAL arc id, its own single command in exactly this flag order (guard-allowlisted;
   legal on the still-`pending` head — accretion refuses only terminal states — and
   replay-idempotent, so a crash/compaction re-entry may safely re-run any of them):
   ```bash
   uv run python tools/reservations.py phase --arc-id <arc-id> --phase queue --edge start --lane-id <lane-id>
   uv run python tools/reservations.py phase --arc-id <arc-id> --phase queue --edge end --lane-id <lane-id>
   uv run python tools/reservations.py phase --arc-id <arc-id> --phase execute --edge start --lane-id <lane-id>
   ```
   The trailing `--lane-id` is mandatory (record_phase refuses a lane that is not the
   head's holder — the guard validates only the command's form). An unreserved arc
   (degradation above) skips these: there is no head to accrete on, and an absent span
   reads as null downstream, never as a measured zero.
3. **Ground first.** Before authoring, empirically verify the item's premise at HEAD
   (`[[r-cxa-seam-wiring-is-producer-discovery]]`, `[[grounding-reveals-claude-closeable-slice-close-honestly]]`). Grounding usually reveals a real Claude-closeable slice inside a
   nominally "gated" item — or reveals the genuine gate. When the premise involves a
   `C-*`/`U-*`/seam/`H_T-*` cite, resolve it with `just overlay-query` (the `overlay-query`
   skill, R-IF-112) before ad-hoc grep. Read-before-grep (U-SR-07/WR-14): for a file you
   will read anyway, Read it once — or run ONE script over it — instead of a chain of
   `sed -n`/grep probes; every probe is a full API call, and the U-HE-35 baseline burned
   134 of them that way (33 `sed -n` + 101 grep-shaped, [B] F5/d3). "One more targeted
   grep is cheaper than reading it" is the trap — it stops being true the moment the
   file is one you will open regardless. Run the §13.1 transcript-brief review (a fresh-context
   Agent reviewer briefed on the session) before substantive cross-axis work.
4. **Implement with tests.** Posture-correct edits (§11). Adopt the
   `defect-class-preflight` skill and run its sweep on the diff BEFORE every commit —
   its ten classes are the distilled findings history of this workspace's own
   reviewers, and a swept diff turns review rounds into confirmations (skipping it is
   how arcs run 9–17 rounds). Hermetic test per new unit;
   `just codex-check` (the superset gate — `just check` omits `codex-parity-check`, so it
   never executes the `tools/hooks/test_*.sh` + `tools/statusline/test_*.sh` shell suites
   that lane runs; U-SR-05/WR-12: ALWAYS launch it `run_in_background` and poll the task —
   the suite outlasts the Bash tool's foreground timeout, and a foreground launch cost a
   10-minute dead gap at [B] F6; "it'll probably finish in time" is the trap). Cache-warmth
   handoff (U-SR-07/WR-14): at >400k context, before ANY background wait expected to
   outlast the prompt-cache TTL, prefer closing out to a handoff over idling through the
   expiry — one cold re-warm re-reads the whole context at ≈0.7M IET ([B] F4); "the wait
   costs nothing, I'm just sleeping" is the trap — the expiry bills the NEXT call. Commit the
   arc's edits (the out-of-family
   reviewer reads the committed
   branch diff — an uncommitted tree makes HEAD-bound checks stale); **grounding pass** (re-read every
   file:line cite at the now-current HEAD, recompute every count, verify every #NNN,
   confirm `just codex-check` ran at the *current* HEAD, state the pass in the PR body — per
   ship-pr U-WT-01) then out-of-family
   `HARNESS_ARC_ID=<arc-id> HARNESS_LANE_ID=<lane-id> just review-with-failover-logged .harness/tmp/<arc-id>-rounds/r<N>.log` to
   convergence (§13.1; the LOGGED variant is canonical — U-HE-34: its in-recipe log publisher is
   how a guarded venue produces the round log arc-metrics later reads; the inline
   prefix is the step-2 arc-open ids — a bare invocation
   writes `branch-*`/`-nolane` fallback ids into the C-HE-24/25 rows. U-HE-49,
   C-HE-21 §1 X6b: the recipe evaluates gate admission BEFORE launching — a refusal
   exits 3 having spent no reviewer call and claimed no round name — and publishes
   each attempt under its own minted name `r<N>-a<K>.log`, so pass the plain `r<N>.log`
   round name and NEVER pre-create or reuse log files; a refused attempt's retry keeps
   the same `r<N>` and the publisher lands it cleanly) —
   the fail-closed `codex-review` wrapper (C-HE-18) with the `gemini-review` D-C failover
   (C-HE-17); a verdict counts only on its schema parse (C-HE-15), never on exit code or
   silence. *Invariant #3 (restated, C-HE-17 §3): out-of-family review covers Codex-authored
   work as before, AND serves as the D-C failover for Claude-authored diffs at the identical
   bar. Exit 2 (`REVIEWER_UNAVAILABLE` on both channels) blocks the arc; record both reasons.*

   **Interim review-span carrier (U-HE-50; C-HE-27 §5 X6a).** The logged wrapper now
   emits the `verify` start/end edges at its own process boundaries — never emit
   `verify` by hand (a hand re-emission is only a harmless no-op replay; the durable
   pair is the round-1 window by first-write-wins). The absorb/edit edges key off
   events only the session observes (classification begin, the first fix edit, the
   absorption commit), so until a wrapper-internal emitter lands (B-218) YOU emit
   them — the ship-pr "Phase-span edges" block stays the definition; each command is
   a single literal-id invocation in the canonical flag order, replay-idempotent,
   skipped entirely on an unreserved arc:
   - On a BLOCK verdict, when finding classification begins:
     `uv run python tools/reservations.py phase --arc-id <arc-id> --phase absorb --edge start --lane-id <lane-id>`
     and when fixing starts: the same command with `--edge end`.
   - At the first fix edit: `--phase edit --edge start`; after the FINAL fix commit:
     `--phase edit --edge end`.
   - On wrapper exit 2: `--phase verify_unavailable --edge start` on observing the
     exit, `--edge end` when review attempts resume or the arc is held (the wrapper
     already closed the verify window at process exit).
   - If the wrapper WARNed that the verify END emission failed on the arc's FINAL
     round (no next round will repair it), re-run the end command by hand before
     ship — the head accretes until terminal, so the repaired end records the named
     upper bound. A START that failed on the final round is NOT repairable: it stays
     null (the spec'd absent disposition) — a post-verdict start would fabricate a
     late window.
   This copy is the interim carrier only (skill prose is not an emission site —
   C-HE-27 §5): it is deleted in the same PR that completes wrapper emission of
   the absorb/edit edges.
5. **Surface only the genuine gate.** Per §12.4.1: a real architectural/scoping decision, a
   credential, a paid-call authorization, or an irreversible action → ONE batched
   `AskUserQuestion` (§14.2). Never fire a paid call / relocate a secret unilaterally
   (`[[feedback-background-agent-no-unilateral-paid-calls-or-secret-relocation]]`). Otherwise
   default to doing + reporting.
6. **Ship.** First close the execute span opened at step 2 (U-HE-34; same literal-id,
   replay-idempotent, skip-if-unreserved rules as the start edge):
   ```bash
   uv run python tools/reservations.py phase --arc-id <arc-id> --phase execute --edge end --lane-id <lane-id>
   ```
   Then hand off to the `ship-pr` skill (the PR + fixed-point-refresh half) — including its
   mandatory reflect + `/context-save-lean` step at arc close. Do not hand off to the next arc
   (`ScheduleWakeup`, `/loop-stop`, or ending the turn) before that step has run.
   Facts-brief handoff (U-SR-07/WR-14): if the NEXT item is a heavy audit or document,
   do not author it in this session — "I already have all the context loaded" is exactly
   the trap; the loaded context is what every call re-bills. Write a facts brief — the
   findings, cites, and decisions the deliverable needs, nothing else — into the
   handoff, and let a fresh session author from it: the S3 audit authored at 540k
   context cost 0.93M IET against ≈0.3M fresh ([B] F10).

## When the queue is genuinely slice-exhausted

If grounding shows the highest-value item reaches a real gate with no buildable slice left
(creds / infra / a HELD operator decision / a dispositioned design arc), that is the
**no-parking-compliant terminal** — report the gate honestly, do not invent busywork. This
is reaching the gate after honest grounding, not parking.

## Notes

- This skill is the WHEN/HOW-TO-LOOP; the canonical text is §12 + `Project_Roadmap_v1.md`.
  If they disagree with anything written here, **the canonical sections win** — re-read them.
- Pairs with `ship-pr` (the close + refresh half) and the SessionStart roadmap hook.
