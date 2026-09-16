# Class 1 fork — C-HE-33 §4's outcome measure is mandated over an input that does not exist, under a "no new store" constraint that forbids creating one

**Status:** ✅ RESOLVED — ratified 2026-09-15 under **Reading C** (operator deferred the choice; see §5). Plan `:7473` amended to stop assigning the measure to U-HE-42 and to point at C-HE-28 §4; traceability row `:7836` records §4 as UNOWNED against **B-245**. U-HE-42's parity half is complete and green and carries no residual claim to C-HE-33 §4.
**Superseded recommendation:** this filing originally recommended **Reading A** (amend the plan to permit the capture, then build both measures). That recommendation is withdrawn — see §5 for why, recorded rather than silently replaced.
**Filed:** 2026-09-15, during U-HE-42 implementation (out-of-family codex review round 9 on the arc branch, `492c456ef`). The finding is ACCEPTED; this fork is its resolution.
**Class:** 1 (architectural — the plan's step is unsatisfiable on its own terms: it names both the input and the prohibition that rules that input out. The fix changes the plan's instruction or the spec's measure, not the implementation).
**Blocks:** U-HE-42's claim to C-HE-33 coverage. The arc's parity mechanism (recipe + guard + tests) is unaffected and stands.
**Cites (each re-read at filing time, untruncated):**
- `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md:7473` — the U-HE-42 step assigning the measure.
- `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md:7474` — Step 4, the manifest row it asks for.
- `.harness/spec/Spec_HE_Loop_Lanes_v1.md` `## C-HE-33` Contract §4 and Verification.
- `.harness/spec/Spec_HE_Loop_Lanes_v1.md` `## C-HE-28 - Cohort comparison and the value AC (AC#10)`.
- `tools/arc_metrics.py:528-574` (`ci_metrics`), `:698-702` (call site), `:243-244` (`ArcRow.ci_runs` / `ci_wall_s`), `:190-197` + `:558` + `:569` (`conclusion` fetched, used once, never stored).

---

## 1. The instruction as cleared

Plan `:7473`, inside U-HE-42's step block, reads in full:

> `ship-pr`: *"Before the single push: `just codex-context-check-ci` (parity with CI's guard job — converge locally, push once)."* Track the outcome measure (≥ 6-CI-run branch share; CANCELLED share) as two lines in `summary` (`arc_metrics.py`) over `ci_runs` (already on the row) — **no new store**.

That is a build instruction, not an aside: it names the measure, the file, the function, the input field, and the constraint. Spec C-HE-33 Contract §4 states the measure itself:

> Outcome measure: "converge locally, push once" — the share of branches burning ≥ 6 CI runs (20% today **[C]**) and CANCELLED-run share are the tracked cohorts (C-HE-28).

## 2. The defect

Both named inputs fail, and the constraint forbids the only honest repair.

**(a) `ci_runs` cannot yield a per-BRANCH share.** `ci_metrics` is called as `ci_metrics(row.merge_sha)` (`:698`) and returns `len(hit)` where `hit` is runs whose `headSha` matches that **merge commit** (`:562-563`, `:574`). So `ci_runs` counts runs on one commit, not the runs a branch burned across its life. "The share of branches burning ≥ 6 CI runs" has no branch-level denominator anywhere on the row.

**(b) CANCELLED share has no stored input.** `conclusion` is requested from `gh` (`:558`) and used exactly once — `if not ci_is_green(r.get("conclusion")): continue` (`:569`) — to exclude non-green runs from timing. Nothing persists it. The stored fields are `ci_runs` (all runs) and `ci_wall_s` (green durations only), so `ci_runs - len(ci_wall_s)` yields a **non-green** count, which conflates CANCELLED with `failure`, `timed_out` and the rest. C-HE-19 makes that distinction load-bearing elsewhere ("CANCELLED is INCOMPLETE, never green"), so collapsing it here would be a measurement that quietly answers a different question.

**(c) The constraint rules out the fix.** Both (a) and (b) are repairable only by capturing something new — a branch-level run count, and a per-run conclusion. The same sentence says **no new store**.

An implementation cannot satisfy (a)+(b)+(c) simultaneously. This is not an implementation gap; the step is unsatisfiable as written.

## 3. Readings

**Reading A — amend the plan to permit the capture (RECOMMENDED).** Replace "over `ci_runs` (already on the row) — no new store" with the two inputs the measure actually needs: a branch-scoped CI run count and a per-run conclusion (or a CANCELLED count) on the C-HE-25 row. This keeps the spec's measure intact and makes the plan buildable. Cost: a new captured field, which is what "no new store" was trying to avoid — so the amendment should say why the avoidance no longer holds.

**Reading B — amend the spec's measure to what the row can answer.** Redefine §4 as a *non-green* share over the merge commit, which is derivable today. *Rejected as a silent substitute:* it answers a different question than "branches burning ≥ 6 CI runs", and C-HE-19's CANCELLED/failure distinction exists precisely because those are not interchangeable. Legitimate only as an explicit spec amendment that says the measure changed.

**Reading C — reassign the measure to C-HE-28.** Contract §4's own words are "are the tracked cohorts (**C-HE-28**)", and C-HE-28 is the cohort-comparison contract. If the intent was that C-HE-28 owns the tracking, then U-HE-42 owes only the parity half and plan `:7473` should not have assigned it. *This is the cheapest reading and may be the true one* — but it contradicts `:7473`'s explicit file/function/field instruction, so it needs ratification rather than assumption. (An earlier pass in this arc reached for this reading from a truncated copy of `:7473`; read in full, the line does not support it without a plan change.)

## 4. Owed on ratification

- Amend plan `:7473` (Reading A or C) or spec C-HE-33 §4 (Reading B), with the change-note discipline the artifact family uses.
- Re-scope or restore the manifest row at `tools/lanes_verify.py:477-488`. It currently reads `"C-HE-32/33 §3"`, narrowed during round 7 so it would not claim §4 coverage it lacks; plan `:7474` asks for the unscoped `"C-HE-32/33"`. Whichever reading ratifies decides which is correct — the row should not be flipped again before then.
- Update forward-register row **B-245**, whose drafted text already states the falsified "already on the row" premise for both lines. It becomes this fork's tracking row rather than a standalone finding.
- If Reading A: build the two `summary` lines and register a witness for them.

## 5. Ratification (2026-09-15) — Reading C, and why the recommendation changed

**How it was decided.** The operator deferred the choice to me rather than selecting a reading ("I defer to your recommendation"). What follows is therefore my call, recorded with its grounds so it can be overturned on the record rather than re-derived.

**Ratified: Reading C** — the measure belongs to C-HE-28, and U-HE-42 owes only the parity half.

**Reading A is withdrawn.** The filing recommended it an hour earlier. Two things changed between filing and ratification, neither of them a new fact about the defect:

1. **The plan contradicts itself, which the filing under-weighted.** U-HE-42's own `**Spec linkage.**` line (`:7452`) already says "outcome measure **via C-HE-28 cohorts**", while `:7473` said to build it here. Reading C is therefore not an override of a cleared instruction — it reconciles two lines of the same plan in favour of the one that agrees with the spec. Spec C-HE-33 §4's own words are "are the tracked cohorts (**C-HE-28**)", and C-HE-33's Verification section names exactly one witness: the parity test, which this arc built and which is green.
2. **Cost, once it had to be executed rather than proposed.** Reading A means new captured inputs (branch-scoped run count, per-run conclusion), changes to `ci_metrics`, two new `summary` cohort lines and witnesses for all of it — substantial new scope in an arc already at 12 of 17 review rounds with six BLOCK rounds behind it, three of which found defects introduced by the previous round's own fix. Choosing A would have meant opening that surface again to satisfy a step the spec does not require of this unit.

**What ratification does NOT do.** It does not delete the measure. C-HE-33 §4 still names it, and it remains unbuilt and unowned — recorded as **B-245**, re-scoped to C-HE-28 §4, and written into the plan's traceability row (`:7836`) as UNOWNED rather than quietly dropped. Closing B-245 still requires what Reading A described: a branch-level CI run count and a per-run conclusion on the C-HE-25 row. Reading C moves the obligation to its correct owner; it does not discharge it.

**Correction carried forward.** An earlier pass in this arc argued for Reading C from a copy of `:7473` truncated at 180 characters, immediately before "as two lines in `summary`" — and on that basis I told the operator the round-9 finding was refutable. It was not; the finding is correct and is adjudicated `accepted`. The reading is the same, but it now rests on the full text and on the `:7452` contradiction rather than on a line that appeared to say nothing.

**Manifest row.** `tools/lanes_verify.py:477-488` stays as round 7 left it (`"C-HE-32/33 §3"`). Under Reading C that scoping is now correct rather than provisional: §4 is not this unit's to witness. Plan `:7474`'s unscoped `Row("C-HE-32/33", …)` is superseded by this ratification.

## 5a. Addendum (2026-09-15) — a SECOND, separate C-HE-33 defect, measured while closing round 10

Round 10 surfaced a distinct problem from the §4 one this fork exists for, so it is recorded here rather than folded in: **C-HE-33 §3's parity claim is false in the checkout shape CI actually produces**, and the registered witness cannot observe it.

`actions/checkout` leaves a TWO-PARENT synthetic merge at HEAD on `pull_request` (`.github/workflows/ci.yml:430-436`); `test_local_ci_parity` builds a ONE-PARENT child of the PR head. Measured at `32a4f26f4` over two fixtures differing only in parentage:

| shape | symmetric difference | outside `PARITY_EXCLUSIONS` |
|---|---|---|
| one-parent (what the witness builds) | `{ROADMAP_STATUS_DRIFT_ALLOWED, ROADMAP_STATUS_BRANCH_DIVERGED}` | **empty** |
| two-parent (what CI checks out) | adds `TRACKING_SURFACES_REVIEW_REQUIRED`, CI-side only | **`{TRACKING_SURFACES_REVIEW_REQUIRED}`** |

Mechanism: the code is `warn` (`tools/codex_context_guard.py:1594`) and fires on `mode in {closeout, check} and state.changed_files and not _has_tracking_changes(...)` (`:1587-1591`). `changed_files` is the one field honouring `--head-ref`, so the CI shape reads the committed `base..pr_head` range while local `codex-context-check` reads `--include-branch-diff` against live HEAD — on a merge commit those differ.

**Round 10's stated cause does not reproduce.** It predicted `ROADMAP_STATUS_LAG_EXPECTED` via `_owed_lag`'s first-parent inspection (`tools/codex_context_guard.py:464-469` — the `rev-list --parents` read, the `len(parents) < 2` bail, and the `_is_verified_refresh_point(root, parents[1])` return); probed, `lag_expected` and `owed_lag` are both False in the merge fixture and that code appears in neither shape. The finding's conclusion is right; its mechanism is not. Recorded so the next reader does not chase the wrong one.

**Disposition.** Registered as **B-246** (draft at `~/.claude/jobs/8cde37b5/tmp/b-246-parity-break-row.txt`; the register file itself is blocked on the held U-HE-40 branch, per this arc's standing note). NOT absorbed: the arc stood at 13 of 17 rounds after nine BLOCKs, three of which (r7, r8, r10) were defects in fixtures written to close the previous round. Closing B-246 means rebuilding the witness over a real `git merge --no-ff` fixture, then either naming the code a CI-only exclusion with its reason or making the shapes agree — **not** widening the exclusion set without a case that drives the merge shape, which was the r8 defect.

## 6. Why this was filed rather than absorbed

Absorbing it would mean one of three silent acts: building a measure over an input that does not mean what the name says (a wrong number, permanently); redefining the spec's measure inside an implementation commit (a spec change with no back-flow); or narrowing the manifest row until the contract appears covered while the mandated work is absent (which round 7 flagged and round 9 re-flagged from the opposite side — the two findings together are the tell that no implementation-side edit resolves this).

CLAUDE.md §4.3 routes exactly this to design-phase back-flow, and §4.4 forbids the silent alternative. The arc's parity work is complete and verified; this fork isolates the one half that cannot be finished under its own instruction.
