# E1 — duplicate-finding detection over the merge-gate ledger

Ledger lines: 2980; findings with evidence: 2092; embedder bge-small-en-v1.5.

## Whole-ledger recurrence (nearest earlier finding from a different arc)

- findings with a prior neighbour ≥ 0.95: 15 (0.7%)
- findings with a prior neighbour ≥ 0.90: 28 (1.3%)
- findings with a prior neighbour ≥ 0.85: 201 (9.6%)

## The 20 most recent findings and their nearest earlier neighbour

Judge each pair: DUP (same defect re-found), CLASS (same class, different instance), NO.

### 2026-09-16 · arc u-he-42 · sim 0.830 → arc u-he-35 (2026-08-26)

- NEW: `tools/codex_context_guard.py:1791` — This hard-fails every pull-request run: actions/checkout leaves HEAD at the synthetic merge ref (explicitly documented in .github/workflows/ci.yml:430), while the guard receives github.event.pull_request.head.sha at .github/workflows/ci.yml:644. Those commits normally differ, so HEAD_MOVED_DURING_CH
- PRIOR: `tools/reviewer_concurrency_probe.py:186` — The fixed-diff guard discards the final base_sha and diff_digest and compares only head_sha. Because every child receives the mutable base ref, that ref can move to a different merge base while HEAD remains unchanged; calls then review different bytes, yet the probe can report GREEN and all rows ret
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.783 → arc u-he-28 (2026-08-22)

- NEW: `justfile:113` — The pre-push gate can succeed for a commit other than the one subsequently pushed. The recipe snapshots `head`, but if HEAD advances while the guard runs, the new code emits only informational `CHECKED_HEAD_NOT_LIVE_HEAD` and still exits 0; `test_guard_discloses_but_does_not_fail_when_head_differs_f
- PRIOR: `tools/merge_door.py:1340` — Moved-head re-adoption checks only the title prefix and changed-file list; it never verifies that the refresh PR still targets the expected main base or is bound to this content landing. A refresh PR retargeted while receiving its documented fix commit can therefore be adopted and merged into anothe
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.799 → arc u-he-26 (2026-08-21)

- NEW: `tools/test_codex_context_guard.py:2468` — The bare remote is initialized without pointing its HEAD at refs/heads/main, yet this clone omits --branch. When Git's configured default is master, the remote contains only main but advertises an unborn master, so the clone checks out no branch and the subsequent commit/push of main raises CalledPr
- PRIOR: `tools/hooks/permission-guard.sh:273` — The checked-out-branch test runs only when there is at most one positional. `git push origin HEAD` has two positionals; Git resolves the colonless `HEAD` refspec to the current branch, but the scanner does not recognize it, so this command is auto-allowed on main.
- JUDGMENT: NO

### 2026-09-16 · arc u-he-42 · sim 0.789 → arc u-he-34 (2026-08-26)

- NEW: `.claude/skills/ship-pr/SKILL.md:33` — The CI-shaped gate is placed before the grounding and review loop, but later instructions explicitly require committing fixes after every BLOCK. Such a commit changes HEAD after this gate, and the eventual push is not required to rerun it, so the final pushed commit can remain unchecked. Require thi
- PRIOR: `.claude/skills/merge-gate/SKILL.md:176` — Post-gate-BLOCK instruction still prescribes bare 'just review-with-failover' (also :207) while ship-pr/SKILL.md in this same PR makes review-with-failover-logged CANONICAL and reserves the bare form for venues that cannot take the log path — post-gate fix rounds run bare produce no round logs for a
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.856 → arc u-he-38 (2026-09-04)

- NEW: `tools/lanes_verify.py:478` — This manifest row claims coverage of all C-HE-33 using only test_local_ci_parity, while C-HE-33 section 4 also requires tracking the share of branches with at least six CI runs and the CANCELLED-run share. This diff does not add either measure to arc_metrics, so the registered witness remains green 
- PRIOR: `tools/lanes_verify.py:430` — The manifest marks C-HE-28 §2 covered even though this diff explicitly documents that no runtime emitter persists ROADMAP_STATUS_DRIFT rows. The registered test fabricates schema-incomplete rows, so CI stays green while production can only report the source as UNWIRED. Wire a validated durable emitt
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.895 → arc u-he-49 (2026-08-27)

- NEW: `.harness/merge-gate-log.jsonl:2927` — The four terminal-block findings appended at lines 2927-2930 have no later finding_adjudication rows. Because the append-only reducer treats the last row for each finding_id as authoritative, all four remain outstanding even though later commits purport to absorb them, leaving the durable review sta
- PRIOR: `.harness/merge-gate-log.jsonl:1788` — The round-4 findings on lines 1788 and 1789 have no subsequent finding_adjudication rows, even though this HEAD explicitly rejects the first and absorbs the second. C-HE-24 §5 requires absorption to append those dispositions; readers currently reduce both findings to disposition=null, corrupting acc
- JUDGMENT: DUP

### 2026-09-16 · arc u-he-42 · sim 0.787 → arc u-he-35 (2026-08-26)

- NEW: `tools/test_codex_context_guard.py:2269` — The parity witness omits CHECKED_HEAD_NOT_LIVE_HEAD from PARITY_EXCLUSIONS even though the new guard deliberately emits it on every pull_request job where the synthetic merge HEAD differs from the supplied PR head SHA. test_local_ci_parity keeps HEAD equal to the supplied head, so it stays green whi
- PRIOR: `tools/reviewer_concurrency_probe.py:186` — The fixed-diff guard discards the final base_sha and diff_digest and compares only head_sha. Because every child receives the mutable base ref, that ref can move to a different merge base while HEAD remains unchanged; calls then review different bytes, yet the probe can report GREEN and all rows ret
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.848 → arc u-he-21 (2026-08-21)

- NEW: `tools/lanes_verify.py:478` — The new manifest entry explicitly scopes coverage away from C-HE-33 §4 because this change does not implement the required >=6-CI-run branch share or CANCELLED-run share. However, the authoritative implementation plan assigns both measures to U-HE-42 (Implementation_Plan_HE_Loop_Lanes_v1.md:7473), a
- PRIOR: `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md:3869` — The permission failure is routed to the wrong future unit. U-HE-23 modifies only `merge_door.py`, its tests, and `lanes_verify.py`; the actual guard unit U-HE-25 only handles the safe-merge wrapper. No planned unit here allows the reservation or environment-prefixed commands, so the claimed deferred
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.826 → arc u-he-28 (2026-08-22)

- NEW: `tools/test_codex_context_guard.py:2413` — This fixture does not create GitHub's two-parent synthetic merge; it creates a one-parent child of the PR head. That distinction is load-bearing because _owed_lag() inspects HEAD's first parent: for a multi-commit PR whose base is a verified refresh, real CI sees the base as the merge commit's first
- PRIOR: `tools/roadmap_status_refresh.py:1092` — The pushed-branch recovery path adopts any same-repository remote ref named roadmap-refresh-post-<PR> without verifying its parent or provenance, then creates the trusted refresh PR around it. Downstream checks bind title/base/head and require a one-file, CI-green diff, but the title is supplied her
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.787 → arc b-235 (2026-09-04)

- NEW: `justfile:111-121` — The recipe's HEAD-moved refusal (line 120) only covers the window inside its own process; the subsequent `git push` is a separate command (per .claude/skills/ship-pr/SKILL.md:33-43 and .agents/skills/ship-pr/SKILL.md:76-77) not bundled atomically with the check. A commit landing between the recipe's
- PRIOR: `tools/leg_selfcheck.py:1128-1152 (check_register_rows) crossing into tools/forward_register.py:198-201,512 (load/prose read)` — TOCTOU: the dirty-carrier guard snapshots `git status --porcelain` once, then falls through to a loop that spawns N separate `forward_register.py --detail <rid>` subprocesses, each of which reads the live on-disk working tree directly (no git-blob read anywhere in forward_register.py). A concurrent 
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.784 → arc branch-he-lanes-s1 (2026-08-19)

- NEW: `tools/codex_context_guard.py:1794-1803` — The new CHECKED_HEAD_NOT_LIVE_HEAD disclosure takes a second, independent `git rev-parse HEAD` read (line 1794) at a point materially later than state.head8's capture inside derive() (line 564), separated by a `gh` network call and the full validate() pass. If a commit lands in that window, live_hea
- PRIOR: `tools/codex_review.py:283` — The wrapper accepts and emits the parsed verdict without recomputing the binding or confirming that HEAD still equals binding['head_sha']. A concurrent commit during the potentially 1260-second review therefore lets an APPROVE for the old commit return exit 0 for an unreviewed current HEAD.
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.855 → arc pr-1409 (2026-08-20)

- NEW: `tools/lanes_verify.py:483 (depends on .harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md via PR #1530)` — The scoped Row("C-HE-32/33 §3", ...) is correct only under the plan amendment landing in #1530 (unamended main still assigns U-HE-42 the §4 measure and an unscoped Row at :7473-7474). #1530 adds the fork-doc back-flow the X-AL-3 CI guard requires but omits the .harness/clearance/implementation-plan-
- PRIOR: `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md U-HE-19 rev-note (items i-xi) + .harness/clearance/implementation-plan-he-loop-lanes-v1-u-he-19-as-built-rev-cleared-2026-08-20.md` — The round-2 delta (9d4018ef2 merge-gate-r1 backfill-pending hold guard + swept-leftover-claim transfer witness; eb9b29c28 codex r20 stash-time transfer + mint-time pr stamp; e54bf54a2 codex r21 + f643a660e codex r22 reconciliation tri-state stops on unreadable/corrupt committed history) touches only
- JUDGMENT: NO

### 2026-09-16 · arc u-he-42 · sim 0.803 → arc u-he-33 (2026-08-25)

- NEW: `tools/test_codex_context_guard.py:2280-2301,2382,2429,2678` — PARITY_EXCLUSIONS declares OPEN_PRS_UNAVAILABLE as a real CI/local divergence, but monkeypatch.setattr(cg, "_open_prs", _open_prs_available) neutralizes _open_prs identically for both the CI and local argv in every in-process test in this block. The code path that would actually produce this finding
- PRIOR: `tools/codex_context_guard.py:1186` — _gh_pr_state accepts every nonempty state, while its caller handles only OPEN, MERGED, and CLOSED. A malformed or newly introduced state therefore silently suppresses orphan detection instead of producing DETECTIONS_UNAVAILABLE.
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.821 → arc u-he-36 (2026-09-02)

- NEW: `tools/test_codex_context_guard.py:49-67` — _GIT_IDENTITY_ENV (added to fix a real CI 'Author identity unknown' failure) is never proven load-bearing: no test forces an environment without global/system git identity. Verified empirically that this machine's global git config (user.name=Robert Rhu, user.email=robert@thestoryportal.org) is set;
- PRIOR: `tools/arc_disjoint_check.py:252` — `git commit-tree` requires author and committer identity, but this call supplies neither identity environment variables nor `-c user.name/user.email`. The scratch-test helper's `_ID` options are command-local and are not persisted, while CI configures no Git identity, so the newly registered histori
- JUDGMENT: DUP

### 2026-09-16 · arc u-he-42 · sim 0.900 → arc u-he-28 (2026-08-22)

- NEW: `justfile:52` — `git fetch origin main` fetches the branch into `.git/FETCH_HEAD` but does not reliably update the `refs/remotes/origin/main` tracking branch (this behavior depends on the Git version and fetch refspec configuration). Because the script subsequently assigns `base="$(git rev-parse origin/main)"`, it 
- PRIOR: `tools/roadmap_status_refresh.py:1197` — `git fetch origin main` uses a source-only refspec, which updates `FETCH_HEAD` but does not guarantee `refs/remotes/origin/main` advances. The next line can therefore create the terminating-refresh branch from the pre-merge tracking ref after `gh pr merge`; strict protection then rejects the behind 
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.824 → arc pr-1418 (2026-08-22)

- NEW: `tools/test_codex_context_guard.py:2280-2286` — PARITY_EXCLUSIONS licenses CONTEXT_CHECKPOINT_MISSING and CONTEXT_CHECKPOINT_STALE without any test driving that divergence -- the same 'declared but never driven' pattern round 2 fixed for OPEN_PRS_UNAVAILABLE (see the CHECKED_HEAD_NOT_LIVE_HEAD comment's own stated principle at :365, 'an exclusion
- PRIOR: `tools/main_protection.py:169-174 (verify()); tools/test_main_protection.py (no covering test)` — The _OPTIONAL_CONTROLS 'always compared' loop in verify() — added per the code's own comment to catch a live lock_branch/block_creations/allow_fork_syncing/required_conversation_resolution enabled while the target payload omits it (codex r3 P2; this is the exact failure mode of 'verify PASSes while 
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.831 → arc u-he-28 (2026-08-22)

- NEW: `tools/test_codex_context_guard.py:2536` — This fixture does not reproduce GitHub's two-parent synthetic merge: it creates a one-parent child of the PR head. That hides a real parity failure because _owed_lag() inspects HEAD's first parent. When the PR base is a verified refresh and the branch has multiple commits, CI's merge commit has that
- PRIOR: `tools/roadmap_status_refresh.py:1092` — The pushed-branch recovery path adopts any same-repository remote ref named roadmap-refresh-post-<PR> without verifying its parent or provenance, then creates the trusted refresh PR around it. Downstream checks bind title/base/head and require a one-file, CI-green diff, but the title is supplied her
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.808 → arc u-he-21 (2026-08-21)

- NEW: `tools/lanes_verify.py:475` — The new manifest entry deliberately scopes U-HE-42 to C-HE-33 §3 and defers the >=6-CI-run and CANCELLED-run outcome measures, but the committed implementation plan still assigns C-HE-33 §1-§4 and both measures to U-HE-42. No plan change authorizes that deferral, so this commit registers a partial w
- PRIOR: `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md:3869` — The permission failure is routed to the wrong future unit. U-HE-23 modifies only `merge_door.py`, its tests, and `lanes_verify.py`; the actual guard unit U-HE-25 only handles the safe-merge wrapper. No planned unit here allows the reservation or environment-prefixed commands, so the claimed deferred
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.842 → arc u-he-36 (2026-09-03)

- NEW: `PR #1527 body — "Review trail" / "Budget" sections` — Body is stale relative to HEAD 85d83a7d0: it narrates codex rounds 1-11 and merge-gate round 3, but omits codex round 12 (commits 38fc66d4b/85d83a7d0, timestamps 2026-09-16T03:35-03:36Z, after merge-gate round 3/4's 03:23-03:31Z) — 2 P2 findings (bb5c9c85e030, e812d0317a46), both rejected. The state
- PRIOR: `PR #1497 body, "Codex verdict summary" line (end of "Review rounds (codex, logged)" section)` — Body claims "24 findings across r1–r10 — 16 accepted (all absorbed with witnesses), 8 rejected on contract". The authoritative ledger (.harness/merge-gate-log.jsonl, record_kind=finding_adjudication, arc_id=u-he-36, producer=codex_review_wrapper) has 27 rows: 19 accepted, 8 rejected, across rounds 1
- JUDGMENT: CLASS

### 2026-09-16 · arc u-he-42 · sim 0.850 → arc u-he-37-plan-record (2026-09-04)

- NEW: `PR #1527 body (no merge-order statement); rationale only in commit 38fc66d4b` — The round-12 rejection of e812d0317a46 (lanes_verify §3-scoping vs. the plan's committed §1-§4 assignment) is valid only because PR #1530's plan amendment (:7473/:7838, Reading C) reassigns C-HE-33 §4 to C-HE-28 -- and commit 38fc66d4b says so explicitly ("PR #1530, which lands before this PR"). Thi
- PRIOR: `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md:6657` — This checks Step 2–3 complete even though the added record itself proves the promised C-HE-13 §3 iff-clause remains unsatisfied: an already externally merged PR can receive merge_sha and PASS, and a malformed live LEASE is treated as absent so report() may PASS before post-merge checks finish. Regis
- JUDGMENT: CLASS

## Judgment tally (author-read, 2026-09-16): {'DUP': 2, 'CLASS': 16, 'NO': 2}

Reading: DUP = the same defect shape re-found in a different arc (e.g. ledger findings without adjudication rows; `git fetch origin main` not updating the tracking ref). CLASS = same defect class (TOCTOU on a moved HEAD, manifest coverage over-claim, fixture not reproducing the real shape), different instance. The ledger recurs at the CLASS level in 16 of the last 20 findings; that is the signal a review-time index would surface, and it is the signal `defect-class-preflight` was hand-distilled to carry.