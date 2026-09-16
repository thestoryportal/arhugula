# Implementation Plan — H_E Autonomous Loop + Parallel Lanes v1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this workspace each unit lands through `/roadmap-continue` → `/ship-pr` (CLAUDE.md §12); the per-unit commit steps below are the commits inside that arc, not a substitute for it.

**Goal:** Build the H_E autonomous loop's correctness floor and measurement substrate so N ≥ 2 lanes can build concurrently in isolated worktrees and land through exactly one merge door, exactly as `.harness/spec/Spec_HE_Loop_Lanes_v1.md` (35 contracts, `C-HE-01`…`C-HE-35`) commits.

**Architecture:** Filesystem-CAS coordination only (exclusive create + atomic rename under the shared `QUEUE_DIR`), no daemon, no lock. Three durable coordination records — closure-time queue entries (existing), generation-versioned per-arc reservations (new, `tools/reservations.py`), and a single merge-door lease with a transition-marker family (new, `tools/merge_door.py`) — plus one finding record (`tools/finding_record.py`) that every gate, reviewer wrapper, and detection emits into `.harness/merge-gate-log.jsonl`. Reviewer verdicts count only under a positive schema parse in a fail-closed wrapper (`tools/review_wrapper_common.py`); the merge door is fenced structurally by `tools/hooks/safe-merge.sh` + `permission-guard.sh` and server-side by branch protection.

**Tech Stack:** Python 3.12 stdlib (`json`, `os`, `subprocess`, `hashlib`, `dataclasses`), `jsonschema` 4.26 (already a workspace dep) for verdict/record schemas, `pyyaml` for reading `ci.yml`; bash for hooks/skills (`tools/hooks/*.sh`); `just` recipes; `pytest` for `tools/test_*.py`; the existing `tools/mutation_probe.py` for RED-first witnesses. **No new frameworks** (CLAUDE.md §3.2).

## Global Constraints

Copied verbatim from the spec; every unit's requirements implicitly include these.

- Namespace: `C-HE-*` governs **H_E dev tooling only**; nothing here extends the H_T design (spec §0.2). Posture: **mode-agnostic** workspace-operational (CLAUDE.md §11.2). No `design-substrate/**` edits anywhere in this plan.
- "All cross-lane coordination state MUST be established by filesystem CAS: atomic exclusive create (`publish_exclusive`, `arc_metrics.py:516`, or `os.link` onto a fresh name) and atomic rename (`os.replace`). No `flock`/`fcntl` locks" (C-HE-02 §1). Invariant: `rg -c 'flock|fcntl' tools/arc_metrics.py tools/merge_door.py tools/reservations.py` → 0.
- "Every coordination file MUST live `QUEUE_DIR`-adjacent (shared, outside `REPO`) and MUST NOT live under `REPO`" (C-HE-02 §2). `QUEUE_DIR` = `ARC_METRICS_QUEUE_DIR`, default `~/.gstack/projects/arhugula-v2/arc-metrics-queue`.
- "No exclusive gate MAY be held across an unbounded network call" (C-HE-02 §3): every `gh` call made under a hold carries a bounded timeout and reconciles by ground truth on timeout.
- "Coordination MUST NOT use a daemon, spawner, coordinator process, or merge-queue lock" (C-HE-01 §3, L-2).
- "**No tier reclaims on elapsed time**" (C-HE-03 §5, C-HE-20 §2, D8): the 24 h TTL is a notification threshold only.
- "A verdict COUNTS only if the channel's output parses to that channel's declared schema. Exit code is never a completion signal" (C-HE-15 §1). Terminal states of a review invocation are exactly `{APPROVE, BLOCK, REVIEWER_UNAVAILABLE}` (C-HE-16 §3).
- "CANCELLED is INCOMPLETE, never green" (C-HE-19 §1).
- "No new **authority**: no new hash-chained findings ledger" (C-HE-23 §1); `.harness/arc-metrics.jsonl` carries only `record_kind=arc` rows; finding-class rows live in `.harness/merge-gate-log.jsonl` (C-HE-24 §2).
- `lane_id`, `producer`, `reviewer_identity`, `deterministic_check_id` MUST NOT contain `:` (C-HE-03 §3, C-HE-24 §2).
- "No flat round cap anywhere" (C-HE-21 §1); "No mechanized check is ever cited as grounds for a round cap" (C-HE-31 inv).
- Every verification line marked **mutation-probe** MUST go RED against the unfixed guard and GREEN after the fix, confirmed via `just mutation-probe` (spec §0.3). `just mutation-probe-coverage-check` asserts coverage before a contract closes (§8.1).
- Skip policy (§8.1): only `docker-daemon-absent`, `provider-login-absent`, `gh-auth-absent` may skip, each with the named reason; a skipped **phase0** row fails `just lanes-phase0-check`. No row may skip on "slow".
- The plan MUST cite `C-HE-NN`, never the design corpus (spec §13); it MUST NOT be consumed before the clearance marker `.harness/clearance/spec-he-loop-lanes-v1-cleared-<date>.md` exists (spec §14).
- Repo conventions: `uv run pytest tools/test_X.py -q` for Python tests; `bash tools/hooks/test_X.sh` for hook tests; every new `tools/test_*.py` MUST be added to `tools/codex-parity-check.sh` (the coverage guard `tools/tools_test_coverage_guard.py` fails CI otherwise); conventional commits, feature branch off `main`, never `git add -A`.

---

## Status block

| Field | Value |
|---|---|
| Status | **Accepted on merge of PR #1393, with the review record in §7 items 4–8** — exit gate = five out-of-family `just codex-review` rounds on the PR (40 P1 / 22 P2 in total, every finding absorbed in-plan before the next round; one spec-internal tension registered, not absorbed); item 8 is the terminal record and states the residual classes honestly (yield did NOT converge to zero — it is carried by unit execution's RED-first + per-PR codex + merge-gate). The spec's clearance marker (same PR) admits the plan only under this recorded gate. |
| Version | v1.0 + **rev 2026-08-19 (spec v1.3 X3 absorption, U-HE-14 only)**: the U-HE-14 audit template's family table is re-headed "Derived families + new-fact carriers" with a `Relation` column (`derived` / `part of store N` / `sole carrier (new fact)`) and the `.seq` allocator, transition-marker `fresh_lease`, and LEASE sidecars are classified per the as-landed audit — the S4 units (U-HE-17/22/23/30) land against that classification, not the v1.0 "no new authority" template. No other unit changed. + **rev 2026-08-19 (S4a execution correction, U-HE-15 Step 4b only)**: no-upstream teardown residue clause replaced by spec-exact `@{u}..HEAD` scope with the no-upstream composition registered as a residual (rationale at Step 4b). + **rev 2026-08-26 (workflow-repair program — spec v1.6 X6a–X6f + `Workflow_Repair_Charter_v1` absorption)**: new §8 with units U-HE-46…51 (bucket 1: contract surfaces, citing the X6-tagged clauses) and U-SR-01…09 (bucket 2: skill-suite + governance, citing charter WR-ids — a new small family, minted because these units' surfaces are skill bodies, hooks, and eval cases the spec deliberately does not own), R0→R3 sequencing (instruments before levers), an item-coverage table over the two U-HE-35 audits' recommendation ids, and two open operator decisions surfaced (not decided at authoring; **both ratified arm (a) 2026-08-26 at §8.5, U-HE-46 arc**). No pre-existing unit changed; §§1–7 preserved verbatim except the §1 unit-index rows appended. + **rev 2026-09-02 (execution correction, U-HE-36 only)**: (i) "every other lane's current head" = the branch of every NON-terminal reservation (`pending` + `open`): C-HE-03 §4 keeps a lane `pending` for its whole build and flips it `open` only at drain start, so the snippet's `state == "open"` would see no lane until it was already landing — spec v1.7 X7 conforms C-HE-13 §5 to the same set; (ii) `merge-tree --name-only` output is parsed to the first empty entry — the snippet's "every non-blank line after the OID" would report the trailing `Auto-merging …` / `CONFLICT (content) …` messages as paths — and on git 2.39.5 an unresolvable ref ALSO exits 1 (with no tree OID), so the conflict verdict is exit 1 WITH an OID, anything else an error; (iii) git 2.39.5 `merge-tree` has no `--merge-base`, so `historical` replays each pair by applying B's own patch (`b^..b`, per file) onto A's tree in a temp index and merge-tree'ing the result — parented at A^ — against A, all under a scratch `GIT_OBJECT_DIRECTORY`; a file whose patch no longer applies (an intermediate moved its context) is reported MASKED, never dropped, so the O3 rate is an interval; the four P-R3 governance files are skipped; (iv) `.harness/plan/o3-colliding-pairs.txt` did not exist — `derive-pairs` re-derives it from the P-R3 window (#1239..#1391 by mergedAt, 4-lane window, four-file exclusion) and reproduces the 172; (v) `tools/hooks/permission-guard.sh` allowlists the `check` verb (U-HE-25 precedent) so the selection gate runs headless. No other unit changed. |
| Date | 2026-08-18 |
| Repo | `17011f89c` (every `file:line` below is pinned there, matching the spec's `[V]` set; re-verified in this authoring session) |
| Path | `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md` |
| Skill | `writing-plans` (structure) + `implementation-planner` (atomic-decomposition discipline §2–§7) |
| Source-set | `Spec_HE_Loop_Lanes_v1.md` (35 contracts, §5 files table, §6 build order, §8.1 manifest, §11 open items); `.harness/adr/ADR-HE-1..4` (context only — the spec is the canonical input) |
| Entry authorization | `/context-restore` → operator: "the specification … is complete. Now we move to the next phase to /writing-plans" |
| Exit gate | Plan review = lean protocol on the doc-only PR: `just codex-review` rounds, absorbed round by round (§7 items 4–8 are the durable record; item 8 states the terminal verdict + residual classes). No separate marker for the plan (H_E tooling; spec §14's marker is for the spec, and it names this gate) |

## Shape decision (front-matter)

**Milestone-led**, mirroring spec §6's unified build order S1–S8 exactly. Grounding: §6 is declared as "the **single order the plan decomposes from**", carries two gate columns (lane safety / measurement) and a dependency DAG. Units are numbered `U-HE-NN` in topological order; each S-step is one plan section. Two plan-level refinements of §6, both stated here so they are reviewable and neither changes a contract:

1. **`U-HE-01` (finding record) precedes the S1 wrapper units.** §6 places S1 and S2 as parallel roots. C-HE-18 §3 requires the codex wrapper to emit a finding row *on the C-HE-24 record*, so the record schema is the true foundational unit. Ordering S2's record ahead of S1's wrapper satisfies both roots' dependencies without violating either.
2. **`U-HE-29` (`loop_status.md` venue + row shape + `NOTIFY` kind, S4d) precedes S4b/S4c.** C-HE-03 §5, C-HE-06 §4/§10 and C-HE-11 §5 all *emit `NOTIFY` rows*, whose kind and structured column are defined only in C-HE-09 §3/§5. §6 lists S4d parallel to S4b; the plan serialises the venue/row-shape unit first so the emitters have a target. Coalescing (C-HE-10) and env isolation (C-HE-11) stay where §6 puts them.

## §1 Plan summary

45 atomic units realise all 35 contracts (the v1.0 set; the rev-2026-08-26 workflow-repair program at §8 adds U-HE-46…51 + U-SR-01…09 against the spec v1.6 X6 clauses and the repair charter — 60 units total). Foundational anchors: `U-HE-01` finding record (C-HE-24), `U-HE-05` verification manifest scaffold (§8.1), `U-HE-17` reservation record (C-HE-03), `U-HE-22` merge-door lease primitive (C-HE-06), `U-HE-29` loop-status venue (C-HE-09). Every unit cites its contract by ID **and section**; the coverage matrix (§4) shows every contract row marked and every unit column marked. New files created: `tools/{finding_record,reservations,merge_door,review_wrapper_common,codex_review,arc_disjoint_check,reviewer_concurrency_probe,shadow_trial,lanes_verify,main_protection}.py`, `tools/mechanized_checks/`, `tools/review_schemas/*.schema.json`, `tools/hooks/{safe-merge.sh,lane-init.sh}`, `.harness/spec/store-audit-he-loop-lanes.md`, `.harness/merge-gate-log.jsonl`, `.harness/mechanized-checks-state.json`, and their tests. Every path is exact (this workspace's execution posture resolves nothing lazily).

**Honest aggregate (spec §6).** Phase 0 = S1–S4 = `U-HE-01`…`U-HE-33`, roughly double v1's Phase 0. It is unconditional; nothing in it is deferrable.

### Unit index (topological order)

| Unit | Step | Title | Contracts (primary) |
|---|---|---|---|
| U-HE-01 | S2 | Finding record: 8-field core + envelope + schema + reducer + `Finding` projection | C-HE-24, C-HE-23 §2-3 |
| U-HE-02 | S1 | Review wrapper common: terminal states, classifier table, schema parse, binding byte-compare, retry loop | C-HE-15, C-HE-16 |
| U-HE-03 | S1 | Review verdict schemas (`codex`, `gemini`, `merge-gate`) | C-HE-15 §4 |
| U-HE-04 | S1 | `tools/codex_review.py` fail-closed wrapper + session-artifact discovery + `just codex-review` reroute | C-HE-18 |
| U-HE-05 | S2 | Verification manifest scaffold: `tools/lanes_verify.py`, `just lanes-verify` / `lanes-phase0-check` / `mutation-probe-coverage-check` | §8.1, §0.3, C-HE-13 §1 |
| U-HE-06 | S1 | `agy_review.py` adopts the common module (classifier, gemini schema, retry) | C-HE-17 §4, C-HE-16 |
| U-HE-07 | S1 | Failover chain + `just review-with-failover` + skill carriers + invariant #3 restatement | C-HE-17 |
| U-HE-08 | S1 | CI terminal states (`CANCELLED` explicit) in `arc_metrics.py` + `ship-pr` | C-HE-19 |
| U-HE-09 | S1 | HITL TTL re-surface (24 h notification, never reclaim) in `loop_lib.sh` | C-HE-20 |
| U-HE-10 | S2 | `ARC_METRICS_REPO` / `ARC_METRICS_LEDGER` env overrides | C-HE-05 |
| U-HE-11 | S2 | Arc-row field extension + null-safe cohort split | C-HE-25 |
| U-HE-12 | S2 | `arc_type_open/close/declared_at` on the single arc row (ledger side of open-time capture) | C-HE-26 §2 |
| U-HE-13 | S2 | `merge-gate` emits `.harness/merge-gate-log.jsonl` first, markdown second; consistency reducer | C-HE-23 §2 |
| U-HE-14 | S3 | Durable store audit one-pager + `tools/test_store_audit.py` | C-HE-30 |
| U-HE-15 | S4a | Drain fault isolation: three FNF guards, per-arc isolation incl. `_claim_arc`, systemic abort, `KEPT QUEUED` only after durable restore | C-HE-04 §1, §3, §7 |
| U-HE-16 | S4a | E9 capture re-publish + `ARC_METRICS_TEST_KILL_AFTER` + takeover unit + `flock` grep witness | C-HE-04 §4, C-HE-02 |
| U-HE-17 | S4b | `tools/reservations.py`: generation-CAS record, `alloc_seq`, `reserve` (requires `arc_type`), `transition` re-validate, `walk_terminal`, `gc`, `mint_lane_id` | C-HE-03 §1-4, §8; C-HE-26 §1 |
| U-HE-18 | S4b | Reservation ground truth + staleness (`reconcile`), selection refusal, `concurrent_lanes_at_open` sensor | C-HE-03 §5, §7; C-HE-20 §1 |
| U-HE-19 | S4b | Drain ⇄ reservation integration: flip-before-append, holder-gated `append`, dead-claim holder transfer, local-row reconciliation, phases fold | C-HE-04 §2, §4, §5; C-HE-03 §4, §6; C-HE-27 §3 |
| U-HE-20 | S4b | AC#2 subprocess harness `tools/test_arc_metrics_lanes.py` (six interleavings + cross-latency) | C-HE-03/04 verification, AC#2(a)(b) |
| U-HE-21 | S4b | Reservation CLI + `roadmap-continue` / `ship-pr` carrier wiring (open-time `pending` + `arc_type`; `pr`/`head_sha`/`base_sha`/`attested_merge_tree` back-fill) | C-HE-03 §3-4, C-HE-26 §1 |
| U-HE-22 | S4c | `tools/merge_door.py` lease primitive: acquire (fail-fast, rate limit, holder invariant), transition marker, release/reclaim/self-resume/unblock, poison-pill completion, GC | C-HE-06 §2, §3, §6, §7 |
| U-HE-23 | S4c | Merge-door landing steps (ii)–(ix), reconcile, `MERGE_DOOR_TEST_KILL_AFTER`, caller backoff, gate rows, attestation + cross-carrier `NOTIFY` | C-HE-06 §1, §4, §5, §8, §9, §10; C-HE-19 §2 |
| U-HE-24 | S4c | `ci.yml` concurrency keyed by SHA for `main` pushes | C-HE-06 §4 (CI concurrency) |
| U-HE-25 | S4c | `tools/hooks/safe-merge.sh` + guard deny-raw / allow-wrapper + test inversion | C-HE-07 |
| U-HE-26 | S4c | Push-to-`main` client-side `emit_deny` predicates | C-HE-08 §1 |
| U-HE-27 | S4c | `tools/main_protection.py` + `just main-protection-{show,apply,rollback,tiebreaker,verify}` | C-HE-08 §2-5 |
| U-HE-28 | S4c | `just merge-door-unblock` + `ship-pr` merge-door steps + refresh continuation | C-HE-06 §1, §4(viii), §6 |
| U-HE-29 | S4d | `loop_status.md` shared venue, structured column, `NOTIFY`/`COALESCE-DELIVERED` kinds, ACTIVATE scoping, rendered `[lane_id]`, pointer sweep | C-HE-09 |
| U-HE-30 | S4d | Gate coalescing by `cause_signature`, 10 min window, pull-based delivery | C-HE-10 |
| U-HE-31 | S4d | `tools/hooks/lane-init.sh` (`HARNESS_LANE_ID`, `HARNESS_LANE_INDEX`, `gc.auto 0` once, RAM probe) + compose port variables + `-p` recipes | C-HE-11 §1, §2, §4, §5 |
| U-HE-32 | S4d | git ref-lock bounded retry helper | C-HE-11 §3 |
| U-HE-33 | S4d | Emitting detections: `SPLIT_BRAIN_LEDGER`, `ORPHANED_RESERVATION`, `BASE_TOCTOU` + lane field + CI split-brain job | C-HE-12 |
| U-HE-34 | S5 | Phase spans: durable accretion, `result_capture` split, N6 formula, no-delta static witness | C-HE-27 |
| U-HE-35 | S6 | `tools/reviewer_concurrency_probe.py` | C-HE-22 |
| U-HE-36 | S6 | `tools/arc_disjoint_check.py` + selection-time refusal | C-HE-13 §4-5 |
| U-HE-37 | S6 | Pilot gate: `just lanes-pilot`, `lanes-pilot-report`, O1/O3 recipe, pilot-runner unit | C-HE-13 §1-3 |
| U-HE-38 | S6 | Cohort report joint on `(concurrent_lanes_at_open, arc_type)`, drift join, correlational header | C-HE-28 |
| U-HE-39 | S6 | Skill-carrier doc sweep: N ≥ 2 wording, blocked list, live-carrier cites, no round cap, scope hint, non-goals, K5–K8 | C-HE-01, 13 §5, 14, 21, 34, 35 |
| U-HE-40 | S7 | `tools/mechanized_checks/` seven classes + `just mech-check` + state file + promotion/demotion machine | C-HE-31 |
| U-HE-41 | S7 | Equivalence-proof rows + removal of proven double-runs | C-HE-32 |
| U-HE-42 | S7 | Local/CI parity for `codex_context_guard` (`just codex-context-check-ci`) | C-HE-33 |
| U-HE-43 | S8 | `tools/shadow_trial.py`: scoring reducer, `no_finding` markers, kill rule n=30/<2, OC table, HITL delivery | C-HE-29 |
| U-HE-44 | close | Forward-register rows for spec §11 items #3, #4, #9, #10, #11, #12; plan evidence log | §11 |
| U-HE-45 | close | Roadmap wiring: register the S1–S8 arcs as `B-*` rows and the pilot bar in `.harness/roadmap_status.md` | CLAUDE.md §12 |
| U-HE-46 | §8 R0 | Arc-metrics round derivation from round-log content; `GATE_REFUSED` excluded; round-id keyed | C-HE-25 (v1.6 X6c) |
| U-HE-47 | §8 R0 | Attribution writers: emit-time `cause_attribution`/`unique_catch` on merge-gate rows + adjudication append at absorption | C-HE-24 §2/§5 (v1.6 X6d) |
| U-HE-48 | §8 R0 | `tools/arc_cost.py` + `just arc-cost <transcript>` per-arc cost extractor | C-HE-25 (v1.6 X6e) |
| U-HE-49 | §8 R0 | Launch-admission guard + per-attempt round-log names (refused launch consumes no round identity) | C-HE-21 §1 (v1.6 X6b) |
| U-HE-50 | §8 R1 | C-HE-27 span emission from `review-with-failover-logged`; interim block in `roadmap-continue` | C-HE-27 §5 (v1.6 X6a) |
| U-HE-51 | §8 R3 | Mid-budget lens-trial instrumentation on the eval arc | C-HE-28 §4 (v1.6 X6f) |
| U-SR-01 | §8 R1 | Preflight: three new classes + four grep shapes + planted-defect evals for both P1 shapes | Charter WR-01/02/03/07 |
| U-SR-02 | §8 R1 | Preflight meta-rules: fix-sweep sharpening, contract-derived bounds, hold-policy promotion | Charter WR-04/05/06 |
| U-SR-03 | §8 R1 | laws:prompt durable wiring + binding values by file | Charter WR-08/09 |
| U-SR-04 | §8 R1 | Attest labels-before-answers + mechanism-precedent search into preflight grounding | Charter WR-10/11 |
| U-SR-05 | §8 R1 | codex-check background shape + attestation-artifact handling in the stop-gate | Charter WR-12 |
| U-SR-06 | §8 R1 | `advisor()` reconciliation (decision #1 RATIFIED 2026-08-26: REWRITE arm) | Charter WR-13 |
| U-SR-07 | §8 R1 | Session-shape codification: facts-brief handoff, cache-warmth note, read-before-grep | Charter WR-14 |
| U-SR-08 | §8 R1 | Context-noise deletions: context-save preamble trim + Bash-hook emit policy | Charter WR-15/16 |
| U-SR-09 | §8 R2 | Mechanical sweep: pin scope, rtk rewrite shapes, edit-hook timing | Charter §2 ("deliberately absent") + [B] b1/b4/b5 |

### Conventions used in every unit below

- **Files** lists exact paths; `Modify:` carries the pinned line range at `17011f89c`.
- **Steps** are the TDD cycle: write the failing test → run it RED → implement → run it GREEN → (mutation-probe where the spec marks one) → register in `tools/codex-parity-check.sh` and the §8.1 manifest → commit. Commands are exact; expected output is stated.
- **Manifest registration** means appending a `Row(...)` to `MANIFEST` in `tools/lanes_verify.py` (`U-HE-05`).
- **mutation-probe** invocations use the tool that exists at HEAD: `just mutation-probe --file <F> --lines <A-B> --test "<cmd>"` (`justfile:259-260`, `tools/mutation_probe.py`). The `--lines` range is the guard being reverted; the plan names the guard, the executor reads the exact line numbers from the file they just wrote. **Order (Codex round-5 P1):** the tool REFUSES an untracked or dirty target, so every unit's cycle is: write tests + code → GREEN → **commit** (`feat:`/`test:`) → run the probes → commit the probe log + manifest registration (`test(he-lanes): U-HE-NN probes pinned + manifest rows`). Where a unit's steps below list "probe" before "commit", read them in this order.
- Python style: stdlib + `jsonschema`; `from __future__ import annotations`; dataclasses; no third-party retry/lock libraries; `ruff` clean.
- **Never `git add -A`.** Stage the exact paths each commit step names.

---
## §2 Atomic units

# S2 (foundational half) — the finding record everything emits into

### U-HE-01: Finding record — 8-field core + envelope + schema + reducer + `Finding` projection

**Scope.** Create `tools/finding_record.py`: the ratified 8-field finding core, the row envelope, a JSON schema (`additionalProperties: false`), write-time checks (`disposition_actor ≠ producer`, `:`-free identifiers, `record_kind` union), append-only emission into `.harness/merge-gate-log.jsonl`, the `finding_id` reducer (last row wins), and the projection to `codex_context_guard.Finding`.

**Spec linkage.** C-HE-24 §1 (core), §2 (envelope + `record_kind` union + `disposition_actor`), §3 (projection + `code` triple), §4 (`finding_id`), §5 (append-only adjudication), §6 (`lane_id`/`arc_id` on every row); C-HE-23 §1 (JSONL stays the format), §3 (no hash chain); C-HE-16 §3 (`fail_class` vocabulary carried in `finding_type`).

**Files.**
- Create: `tools/finding_record.py`
- Create: `tools/review_schemas/finding_record.schema.json`
- Create: `tools/test_finding_record.py`
- Modify: `tools/codex-parity-check.sh:13-36` (add the test file)

**Interfaces.**
- Consumes: `codex_context_guard.Finding` (`tools/codex_context_guard.py:113-117`, frozen dataclass `severity, code, message`).
- Produces (later units rely on these exact names):
  ```python
  RECORD_KINDS = ("finding", "finding_adjudication", "no_finding", "equivalence_proof", "gate_demotion", "reviewer_unavailable")
  DISPOSITIONS = (None, "accepted", "rejected", "suppressed")
  SEVERITIES = ("P1", "P2", "P3", "hard", "warn", "info")
  GATE_LOG_JSONL = REPO / ".harness" / "merge-gate-log.jsonl"
  class RecordError(ValueError)
  @dataclass(frozen=True) class FindingCore(finding_id, location, observed_evidence, expected_contract, severity, finding_type, lineage_claim, producer)
  @dataclass(frozen=True) class Envelope(record_kind, ts, arc_id, lane_id, head_sha, base_sha, diff_digest, round_n, cause_attribution=None, disposition=None, disposition_actor=None, unique_catch=None)
  def make_finding_id(producer: str, head_sha: str, location: str, n: int) -> str
  def make_row(core: FindingCore, env: Envelope) -> dict
  def validate(row: dict) -> None                       # raises RecordError
  def append_row(row: dict, path: Path = GATE_LOG_JSONL) -> None
  def read_rows(path: Path = GATE_LOG_JSONL) -> list[dict]
  def reduce_last_by_finding_id(rows: list[dict]) -> dict[str, dict]
  def to_guard_finding(row: dict) -> Finding
  def now_iso() -> str
  ```

**Depends on.** (none) — foundational.

**Reading of the spec applied here (stated, not invented).** C-HE-24 §3 derives projection `severity` "from `finding_type`/fail-class" and `code` = `<check>:<fail_class>:<cause_attribution>`; C-HE-16 §3 says every C-HE-24 row a reviewer failure produces carries `fail_class`. The plan therefore carries the fail-class vocabulary **in `finding_type`** (values such as `transient-retry`, `permanent-fail-exit`, `HITL-recoverable`, `terminal-…`, `Reflexion-…`, `equivalence_proof`) and `check` = `producer`. `severity` accepts the reviewer triple `P1|P2|P3` (C-HE-15 §4) **and** the projection triple `hard|warn|info` (C-HE-31 §4(c) writes `severity=warn` on `gate_demotion` rows). If the reviewer prefers a single vocabulary, that is a v1.1 spec note, not a plan change.

- [ ] **Step 1: Write the failing tests**

`tools/test_finding_record.py`:

```python
"""C-HE-24 finding record: schema, write-time checks, reducer, projection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import finding_record as fr
from codex_context_guard import Finding


def _core(**over):
    base = dict(
        finding_id="merge-gate:abc123:0000deadbeef:1",
        location="tools/x.py:12",
        observed_evidence="lease acquired without pr",
        expected_contract="C-HE-06 §3",
        severity="P1",
        finding_type="terminal-block",
        lineage_claim="fresh",
        producer="merge-gate",
    )
    base.update(over)
    return fr.FindingCore(**base)


def _env(**over):
    base = dict(
        record_kind="finding",
        ts="2026-08-18T00:00:00Z",
        arc_id="pr-1",
        lane_id="host-wt-abcdef01",
        head_sha="a" * 40,
        base_sha="b" * 40,
        diff_digest="c" * 64,
        round_n=1,
    )
    base.update(over)
    return fr.Envelope(**base)


def test_row_validates_against_schema():
    row = fr.make_row(_core(), _env())
    fr.validate(row)  # no raise
    assert set(row) == set(fr.SCHEMA["properties"])


def test_schema_is_closed():
    row = fr.make_row(_core(), _env())
    row["extra"] = 1
    with pytest.raises(fr.RecordError):
        fr.validate(row)


def test_record_kind_union_enforced():
    with pytest.raises(fr.RecordError):
        fr.validate(fr.make_row(_core(), _env(record_kind="arc")))


# mutation-probe: drop the `disposition_actor == producer` check in validate()
def test_self_disposition_rejected_at_write(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    fr.append_row(fr.make_row(_core(), _env()), p)                       # the original finding
    row = fr.make_row(
        _core(),
        _env(record_kind="finding_adjudication", disposition="accepted", disposition_actor="merge-gate"),
    )
    with pytest.raises(fr.RecordError, match="disposition_actor"):
        fr.append_row(row, p)
    assert len(fr.read_rows(p)) == 1


def test_adjudication_requires_actor():
    with pytest.raises(fr.RecordError):
        fr.validate(fr.make_row(_core(), _env(record_kind="finding_adjudication", disposition="accepted")))


# mutation-probe: drop the ':' charset check in validate()
@pytest.mark.parametrize("field", ["producer", "lane_id"])
def test_colon_in_identifier_rejected(field):
    kwargs = {field: "bad:id"}
    if field == "producer":
        row = fr.make_row(_core(**kwargs), _env())
    else:
        row = fr.make_row(_core(), _env(**kwargs))
    with pytest.raises(fr.RecordError, match=":"):
        fr.validate(row)


# mutation-probe: drop _check_adjudication_against_original() from append_row()
def test_adjudication_cannot_change_core_or_evade_self_disposition(tmp_path: Path):
    p = tmp_path / "g.jsonl"; fid = "merge-gate:abc:loc:1"
    fr.append_row(fr.make_row(_core(finding_id=fid), _env()), p)
    with pytest.raises(fr.RecordError, match="core field"):
        fr.append_row(fr.make_row(_core(finding_id=fid, location="elsewhere"), _env(record_kind="finding_adjudication", disposition="accepted", disposition_actor="operator")), p)
    with pytest.raises(fr.RecordError, match="original producer"):   # producer swapped to evade the self-disposition ban
        fr.append_row(fr.make_row(_core(finding_id=fid, producer="operator"), _env(record_kind="finding_adjudication", disposition="accepted", disposition_actor="merge-gate")), p)
    with pytest.raises(fr.RecordError, match="core field"):          # envelope fields are immutable too (round-3 P2)
        fr.append_row(fr.make_row(_core(finding_id=fid), _env(lane_id="other-lane", record_kind="finding_adjudication", disposition="accepted", disposition_actor="operator")), p)
    with pytest.raises(fr.RecordError, match="unknown finding_id"):
        fr.append_row(fr.make_row(_core(finding_id="never-seen"), _env(record_kind="finding_adjudication", disposition="accepted", disposition_actor="operator")), p)
    fr.append_row(fr.make_row(_core(finding_id=fid), _env(record_kind="finding_adjudication", disposition="accepted", disposition_actor="operator")), p)  # legal


def test_reducer_last_row_wins(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    fid = "merge-gate:abc:loc:1"
    fr.append_row(fr.make_row(_core(finding_id=fid), _env(ts="2026-08-18T00:00:00Z")), p)
    fr.append_row(
        fr.make_row(_core(finding_id=fid), _env(ts="2026-08-18T00:00:01Z", record_kind="finding_adjudication",
                                                disposition="rejected", disposition_actor="operator")), p)
    fr.append_row(
        fr.make_row(_core(finding_id=fid), _env(ts="2026-08-18T00:00:02Z", record_kind="finding_adjudication",
                                                disposition="accepted", disposition_actor="operator")), p)
    last = fr.reduce_last_by_finding_id(fr.read_rows(p))
    assert last[fid]["disposition"] == "accepted"


def test_finding_id_shape():
    fid = fr.make_finding_id("codex_review_wrapper", "a" * 40, "tools/x.py:1", 3)
    parts = fid.split(":")
    assert parts[0] == "codex_review_wrapper" and parts[1] == "a" * 40 and parts[3] == "3"
    assert len(parts[2]) == 12


# mutation-probe: change the `code` join in to_guard_finding() to a different delimiter
def test_projection_code_triple_and_severity_map():
    row = fr.make_row(
        _core(producer="merge-door-lease-acquire", finding_type="transient-retry", severity="P3"),
        _env(cause_attribution="lease_contended"),
    )
    f = fr.to_guard_finding(row)
    assert isinstance(f, Finding)
    assert f.code == "merge-door-lease-acquire:transient-retry:lease_contended"
    assert f.severity == "warn"
    assert f.message == row["observed_evidence"]
    hard = fr.to_guard_finding(fr.make_row(_core(finding_type="permanent-fail-exit"), _env()))
    assert hard.severity == "hard"


def test_projection_round_trip_keeps_existing_codes_byte_identical():
    """Pre-existing guard codes are never re-shaped by the projection layer."""
    f = Finding("hard", "ROADMAP_STATUS_DRIFT", "x")
    assert json.dumps(f.__dict__) == '{"severity": "hard", "code": "ROADMAP_STATUS_DRIFT", "message": "x"}'
```

- [ ] **Step 2: Run to verify RED**

Run: `uv run pytest tools/test_finding_record.py -q`
Expected: `ImportError`/`ModuleNotFoundError: finding_record` (collection error).

- [ ] **Step 3: Write the schema file**

`tools/review_schemas/finding_record.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "C-HE-24 finding record (8-field core + envelope)",
  "type": "object",
  "additionalProperties": false,
  "required": ["finding_id", "location", "observed_evidence", "expected_contract", "severity", "finding_type",
               "lineage_claim", "producer", "record_kind", "ts", "arc_id", "lane_id", "head_sha", "base_sha",
               "diff_digest", "round_n", "cause_attribution", "disposition", "disposition_actor", "unique_catch"],
  "properties": {
    "finding_id": {"type": "string", "minLength": 1},
    "location": {"type": "string"},
    "observed_evidence": {"type": "string"},
    "expected_contract": {"type": "string"},
    "severity": {"enum": ["P1", "P2", "P3", "hard", "warn", "info"]},
    "finding_type": {"type": "string", "minLength": 1},
    "lineage_claim": {"type": "string"},
    "producer": {"type": "string", "pattern": "^[^:]+$"},
    "record_kind": {"enum": ["finding", "finding_adjudication", "no_finding", "equivalence_proof", "gate_demotion", "reviewer_unavailable"]},
    "ts": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
    "arc_id": {"type": "string", "minLength": 1},
    "lane_id": {"type": "string", "pattern": "^[^:]+$"},
    "head_sha": {"type": ["string", "null"]},
    "base_sha": {"type": ["string", "null"]},
    "diff_digest": {"type": ["string", "null"]},
    "round_n": {"type": ["integer", "null"], "minimum": 0},
    "cause_attribution": {"type": ["string", "null"]},
    "disposition": {"enum": [null, "accepted", "rejected", "suppressed"]},
    "disposition_actor": {"type": ["string", "null"], "pattern": "^[^:]+$"},
    "unique_catch": {"type": ["boolean", "null"]}
  }
}
```

- [ ] **Step 4: Write the module**

`tools/finding_record.py`:

```python
#!/usr/bin/env python3
"""C-HE-24 common finding record and its `Finding` projection.

Every finding-class row (reviewer verdicts, deterministic checks, adjudications,
shadow-trial markers, equivalence proofs, gate demotions) carries the ratified
8-field core plus the row envelope and is appended to
``.harness/merge-gate-log.jsonl`` (C-HE-23 §2). ``.harness/arc-metrics.jsonl``
carries only ``record_kind=arc`` rows and never uses this module's emitter.

Adjudication is append-only (C-HE-24 §5): a later disposition is a new
``finding_adjudication`` row with the same ``finding_id``; readers reduce to
the last row. The write-time checks below are the enforcement of the "reviewer
is never authoritative for disposition" rule -- not prose.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import jsonschema

from codex_context_guard import Finding

REPO = Path(__file__).resolve().parent.parent
GATE_LOG_JSONL = REPO / ".harness" / "merge-gate-log.jsonl"
SCHEMA_PATH = REPO / "tools" / "review_schemas" / "finding_record.schema.json"
SCHEMA: dict = json.loads(SCHEMA_PATH.read_text())

RECORD_KINDS = tuple(SCHEMA["properties"]["record_kind"]["enum"])
DISPOSITIONS = tuple(SCHEMA["properties"]["disposition"]["enum"])
SEVERITIES = tuple(SCHEMA["properties"]["severity"]["enum"])

#: C-HE-24 §3 projection: fail-class (carried in ``finding_type``) -> guard severity.
_HARD_PREFIXES = ("terminal-", "permanent-fail-exit")
_WARN_PREFIXES = ("transient-retry", "Reflexion-", "HITL-recoverable")


class RecordError(ValueError):
    """A row that must not be written. Never swallowed."""


@dataclass(frozen=True)
class FindingCore:
    finding_id: str
    location: str
    observed_evidence: str
    expected_contract: str
    severity: str
    finding_type: str
    lineage_claim: str
    producer: str


@dataclass(frozen=True)
class Envelope:
    record_kind: str
    ts: str
    arc_id: str
    lane_id: str
    head_sha: str | None
    base_sha: str | None
    diff_digest: str | None
    round_n: int | None
    cause_attribution: str | None = None
    disposition: str | None = None
    disposition_actor: str | None = None
    unique_catch: bool | None = None


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_finding_id(producer: str, head_sha: str, location: str, n: int) -> str:
    """``<producer>:<head_sha>:<location-hash>:<n>`` (C-HE-24 §4). Not stable across head_sha."""
    loc = hashlib.sha1(location.encode()).hexdigest()[:12]
    return f"{producer}:{head_sha}:{loc}:{n}"


def make_row(core: FindingCore, env: Envelope) -> dict:
    return {**asdict(core), **asdict(env)}


def validate(row: dict) -> None:
    try:
        jsonschema.validate(row, SCHEMA)
    except jsonschema.ValidationError as exc:
        raise RecordError(f"finding record schema: {exc.message}") from exc
    for key in ("producer", "lane_id", "disposition_actor"):
        val = row.get(key)
        if isinstance(val, str) and ":" in val:
            raise RecordError(f"{key} must not contain ':' (finding_id/code delimiter): {val!r}")
    if row["record_kind"] == "finding_adjudication":
        if row["disposition"] is None or row["disposition_actor"] is None:
            raise RecordError("finding_adjudication rows require disposition and disposition_actor")
    if row["disposition_actor"] is not None and row["disposition_actor"] == row["producer"]:
        raise RecordError(
            f"disposition_actor {row['disposition_actor']!r} equals producer -- "
            "a reviewer never disposes its own finding (C-HE-24 §5)"
        )


_CORE_IMMUTABLE = ("location", "observed_evidence", "expected_contract", "severity", "finding_type", "lineage_claim", "producer",
                   "arc_id", "lane_id", "head_sha", "base_sha", "diff_digest", "round_n", "cause_attribution")   # everything but ts/record_kind/disposition/disposition_actor/unique_catch


def _check_adjudication_against_original(row: dict, path: Path) -> None:
    """C-HE-24 §5 invariant at WRITE time: two rows with one finding_id differ only by ts / record_kind /
    disposition / disposition_actor / unique_catch. The self-disposition ban is checked against the ORIGINAL
    producer, so an adjudication cannot smuggle a new producer in to evade it (Codex round-1 P2)."""
    if row["record_kind"] != "finding_adjudication":
        return
    prior = [r for r in read_rows(path) if r["finding_id"] == row["finding_id"]]
    if not prior:
        raise RecordError(f"adjudication for unknown finding_id {row['finding_id']!r}")
    orig = prior[0]
    for k in _CORE_IMMUTABLE:
        if row[k] != orig[k]:
            raise RecordError(f"adjudication may not change core field {k!r} ({orig[k]!r} -> {row[k]!r})")
    if row["disposition_actor"] == orig["producer"]:
        raise RecordError(f"disposition_actor {row['disposition_actor']!r} equals the finding's original producer")


def append_row(row: dict, path: Path | None = None) -> None:
    """Validate (incl. the same-core invariant for adjudications), then append one line with a single write.
    `path` defaults to GATE_LOG_JSONL resolved AT CALL TIME (not bound at def time) so tests may monkeypatch it
    and production writes never leak into a test's tree (Codex round-5 P1)."""
    path = path or GATE_LOG_JSONL
    validate(row)
    _check_adjudication_against_original(row, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True) + "\n"
    with path.open("a") as fh:
        fh.write(line)


def read_rows(path: Path | None = None) -> list[dict]:
    path = path or GATE_LOG_JSONL                         # call-time resolution (see append_row)
    if not path.exists():
        return []
    rows: list[dict] = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecordError(f"{path}:{n} is not valid JSON: {exc}") from exc
    return rows


def reduce_last_by_finding_id(rows: list[dict]) -> dict[str, dict]:
    """Last row per finding_id by (ts, file order). Append-only makes this monotonic."""
    out: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r["ts"]):  # stable sort keeps file order on ties
        out[row["finding_id"]] = row
    return out


def to_guard_finding(row: dict) -> Finding:
    """C-HE-24 §3 projection for the CI surface. Existing guard codes are untouched."""
    ft = row["finding_type"]
    if ft.startswith(_HARD_PREFIXES):
        severity = "hard"
    elif ft.startswith(_WARN_PREFIXES):
        severity = "warn"
    else:
        severity = "info"
    code = f"{row['producer']}:{ft}:{row['cause_attribution'] or '-'}"
    return Finding(severity, code, row["observed_evidence"])


def main(argv: list[str] | None = None) -> int:
    """`python tools/finding_record.py validate <jsonl>` -- schema-check every row."""
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2 or args[0] != "validate":
        print("usage: finding_record.py validate <path.jsonl>", file=sys.stderr)
        return 2
    bad = 0
    for n, row in enumerate(read_rows(Path(args[1])), start=1):
        try:
            validate(row)
        except RecordError as exc:
            bad += 1
            print(f"row {n}: {exc}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run to verify GREEN**

Run: `uv run pytest tools/test_finding_record.py -q`
Expected: `12 passed`.

- [ ] **Step 6: Commit the code first (the probe tool refuses untracked/dirty targets)**

```bash
git add tools/finding_record.py tools/review_schemas/finding_record.schema.json tools/test_finding_record.py
git commit -m "feat(he-lanes): U-HE-01 finding record — 8-field core + envelope + projection (C-HE-24)"
```

- [ ] **Step 6b: Mutation probes (spec-marked)**

Run each; expected `PROBE PINNED` (exit 0):
- `just mutation-probe --file tools/finding_record.py --lines <the disposition_actor==producer raise block> --test "uv run pytest tools/test_finding_record.py::test_self_disposition_rejected_at_write -q"`
- `just mutation-probe --file tools/finding_record.py --lines <the _check_adjudication_against_original call in append_row> --test "uv run pytest tools/test_finding_record.py::test_adjudication_cannot_change_core_or_evade_self_disposition -q"`
- `just mutation-probe --file tools/finding_record.py --lines <the ':' charset loop> --test "uv run pytest tools/test_finding_record.py::test_colon_in_identifier_rejected -q"`

- [ ] **Step 7: Register + commit the evidence**

Add `tools/test_finding_record.py \` to the list in `tools/codex-parity-check.sh` (after `tools/test_arc_metrics.py \`). Run `bash tools/codex-parity-check.sh` → all green.

```bash
git add tools/codex-parity-check.sh .harness/mutation-probe-log.jsonl
git commit -m "test(he-lanes): U-HE-01 probes pinned + parity registration"
```

**Acceptance (functional).** All Step-1 tests green; both mutation probes pinned; `python tools/finding_record.py validate` exits 0 on an empty/absent file. **Manifest rows (added in U-HE-05):** `tools/test_finding_record.py` → tag `phase0`, mutation-probe `yes`.

---
# S1 — Verdict validity, terminal states, failover, wrapper (live at N=1)

### U-HE-02: Review wrapper common module — terminal states, classifier table, schema parse, binding byte-compare, retry loop

**Scope.** Create `tools/review_wrapper_common.py`: the shared fail-closed core every reviewer channel wrapper uses — `{APPROVE, BLOCK, REVIEWER_UNAVAILABLE}` terminal states, the per-CLI transient/permanent classifier table (module-level, unit-tested row by row), fenced-JSON extraction + JSON-Schema parse, independent computation and byte-compare of the six binding fields, the `550 s × 2 / 1260 s` retry loop with the dynamic second timeout, and the outcome→finding-row emitter.

**Spec linkage.** C-HE-15 §1 (parse-to-schema counts; exit code never a signal), §2 (missing/empty/truncated/malformed → `REVIEWER_UNAVAILABLE`), §3 (six binding fields; no reuse across `head_sha`), §4 (schema files, fenced block, out-of-enum severity → unavailable, wrapper computes expected binding and byte-compares); C-HE-16 §1–§4 (classification, permanent skips retry, terminal-state triple, retry parameters, table in code); C-HE-21 §2 (#5 invariant live-carried).

**Files.**
- Create: `tools/review_wrapper_common.py`
- Create: `tools/test_review_wrapper.py`
- Modify: `tools/codex-parity-check.sh` (add the test)

**Interfaces.**
- Consumes: `agy_review.run_bounded`, `agy_review.TOTAL_REVIEW_TIMEOUT_SECONDS` (`tools/agy_review.py:22, 99-183`); `finding_record` (U-HE-01).
- Produces:
  ```python
  TERMINAL_STATES = ("APPROVE", "BLOCK", "REVIEWER_UNAVAILABLE")
  BINDING_FIELDS = ("head_sha", "base_sha", "diff_digest", "reviewer_identity", "prompt_version", "config_hash")
  PER_ATTEMPT_TIMEOUT_S = 550.0; MAX_ATTEMPTS = 2; TOTAL_BUDGET_S = 1260.0; SECOND_ATTEMPT_MARGIN_S = 30.0
  CLASSIFIER: tuple[tuple[str, re.Pattern[str], str], ...]
  @dataclass class Attempt(stdout: str, stderr: str, returncode: int | None, timed_out: bool)
  @dataclass class ReviewOutcome(terminal, channel, failure_class, reason, findings, binding, source)
  def classify(channel: str, text: str) -> str            # "permanent" | "transient"
  def compute_binding(repo: Path, base: str, *, channel: str, prompt_version: str, config_hash: str) -> dict[str, str]
  def load_schema(channel: str) -> dict
  def extract_fenced_json(text: str) -> str | None
  def parse_verdict(channel: str, text: str, expected: dict[str, str], *, source: str = "stdout") -> ReviewOutcome
  def run_with_retry(invoke, *, channel, expected, deadline, clock=time.monotonic) -> ReviewOutcome
  def run_with_failover(primary, failover) -> tuple[ReviewOutcome, ReviewOutcome | None]
  def outcome_rows(outcome, *, producer, arc_id, lane_id, round_n) -> list[dict]
  def exit_code(outcome: ReviewOutcome) -> int              # APPROVE 0 / BLOCK 1 / REVIEWER_UNAVAILABLE 2
  def env_arc_and_lane() -> tuple[str, str]
  ```

**Depends on.** U-HE-01, U-HE-03 (schemas; author U-HE-03 first — it is data).

- [ ] **Step 1: Write the failing tests**

`tools/test_review_wrapper.py` (the per-channel battery of C-HE-15 verification + classifier table + retry + failover; C-HE-18's session-artifact fixture is added in U-HE-04):

```python
"""C-HE-15/16/17 fail-closed review wrapper battery. CLIs are mocked; no skip."""
from __future__ import annotations

import json
from itertools import count

import pytest

import review_wrapper_common as rw

EXPECTED = {
    "head_sha": "a" * 40, "base_sha": "b" * 40, "diff_digest": "c" * 64,
    "reviewer_identity": "codex-review", "prompt_version": "pv1", "config_hash": "ch1",
}


def _block(verdict="APPROVE", findings=None, **over):
    body = {"verdict": verdict, "findings": findings or [], **EXPECTED, **over}
    return "chatter\n```json\n" + json.dumps(body) + "\n```\ntrailer\n"


# ── C-HE-15 §1/§2: only a positive schema parse counts ──────────────────────
# mutation-probe: make parse_verdict() return APPROVE when text is empty (exit-code keying)
def test_empty_stdout_exit0_is_unavailable():
    out = rw.parse_verdict("codex", "", EXPECTED)
    assert out.terminal == "REVIEWER_UNAVAILABLE"


def test_truncated_json_is_unavailable():
    text = _block()[:-12]  # cut inside the fence
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_no_fenced_block_is_unavailable():
    assert rw.parse_verdict("codex", "VERDICT: APPROVE", EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_out_of_enum_severity_is_unavailable():
    text = _block("BLOCK", [{"severity": "P0", "location": "x", "message": "m"}])
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_extra_property_on_finding_is_unavailable():
    text = _block("BLOCK", [{"severity": "P1", "location": "x", "message": "m", "extra": 1}])
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_well_formed_parses():
    out = rw.parse_verdict("codex", _block("BLOCK", [{"severity": "P1", "location": "x", "message": "m"}]), EXPECTED)
    assert out.terminal == "BLOCK" and len(out.findings) == 1 and out.binding == EXPECTED


# ── C-HE-15 §3/§4: binding byte-compare, all six fields ─────────────────────
@pytest.mark.parametrize("field", rw.BINDING_FIELDS)
def test_binding_mismatch_is_unavailable(field):
    text = _block(**{field: "different"})
    out = rw.parse_verdict("codex", text, EXPECTED)
    assert out.terminal == "REVIEWER_UNAVAILABLE" and field in out.reason


def test_schema_requires_all_six_binding_fields():
    schema = rw.load_schema("codex")
    assert set(rw.BINDING_FIELDS) <= set(schema["required"])


# ── C-HE-16 §4: classifier table, row by row; unknown → transient ───────────
@pytest.mark.parametrize("channel,text,expected", [
    ("codex", "codex-cli requires a newer version of Codex", "permanent"),
    ("codex", "Error: not logged in", "permanent"),
    ("codex", "HTTP 401 Unauthorized", "permanent"),
    ("codex", "bash: codex: command not found", "permanent"),
    ("codex", "rate limit exceeded (429)", "transient"),
    ("codex", "read ETIMEDOUT", "transient"),
    ("gemini", "antigravity CLI not logged in", "permanent"),
    ("gemini", "RESOURCE_EXHAUSTED", "transient"),
    ("codex", "some brand new vendor error text", "transient"),
])
def test_classifier_table(channel, text, expected):
    assert rw.classify(channel, text) == expected


# ── C-HE-16 §2/§3: retry parameters ─────────────────────────────────────────
def test_permanent_failure_skips_retry():
    calls = count()

    def invoke(timeout):
        next(calls)
        return rw.Attempt(stdout="", stderr="Error: not logged in", returncode=0, timed_out=False)

    out = rw.run_with_retry(invoke, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0)
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "permanent"
    assert next(calls) == 1  # exactly one attempt made


def test_transient_then_success_uses_two_attempts_and_dynamic_second_timeout():
    seen: list[float] = []
    now = {"t": 0.0}

    def invoke(timeout):
        seen.append(timeout)
        now["t"] += 800.0  # first attempt burned 800 s of the 1260 s budget
        if len(seen) == 1:
            return rw.Attempt(stdout="", stderr="", returncode=0, timed_out=False)  # empty first attempt
        return rw.Attempt(stdout=_block(), stderr="", returncode=0, timed_out=False)

    out = rw.run_with_retry(invoke, channel="codex", expected=EXPECTED,
                            deadline=rw.TOTAL_BUDGET_S, clock=lambda: now["t"])
    assert out.terminal == "APPROVE"
    assert seen[0] == rw.PER_ATTEMPT_TIMEOUT_S
    # attempt 2 timeout = min(550, remaining - 30) = min(550, 1260-800-30) = 430
    assert seen[1] == pytest.approx(430.0)


def test_empty_on_second_attempt_is_unavailable_transient():
    def invoke(timeout):
        return rw.Attempt(stdout="", stderr="", returncode=0, timed_out=False)

    out = rw.run_with_retry(invoke, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0)
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "transient"


def test_budget_exhaustion_is_hitl_recoverable():
    def invoke(timeout):
        return rw.Attempt(stdout="", stderr="", returncode=None, timed_out=True)

    out = rw.run_with_retry(invoke, channel="codex", expected=EXPECTED, deadline=1.0, clock=lambda: 5.0)
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.reason.startswith("HITL-recoverable")


# ── C-HE-17: failover chain ─────────────────────────────────────────────────
def _unavail(cls):
    return rw.ReviewOutcome("REVIEWER_UNAVAILABLE", "codex", cls, "x", [], None, None)


def test_failover_invoked_once_on_primary_unavailable_and_blocks():
    n = count()

    def failover():
        next(n)
        return rw.ReviewOutcome("BLOCK", "gemini", None, "", [{"severity": "P1", "location": "l", "message": "m"}], EXPECTED, "stdout")

    p, f = rw.run_with_failover(lambda: _unavail("permanent"), failover)
    assert f is not None and f.terminal == "BLOCK" and next(n) == 1


def test_failover_unavailable_blocks_with_both_reasons():
    p, f = rw.run_with_failover(lambda: _unavail("permanent"), lambda: rw.ReviewOutcome("REVIEWER_UNAVAILABLE", "gemini", "transient", "y", [], None, None))
    assert f is not None and rw.exit_code(p) == 2 and rw.exit_code(f) == 2


def test_failover_not_invoked_when_primary_terminal():
    p, f = rw.run_with_failover(lambda: rw.ReviewOutcome("APPROVE", "codex", None, "", [], EXPECTED, "stdout"), lambda: pytest.fail("must not run"))
    assert f is None


# ── outcome rows: reviewer_unavailable → C-HE-24 rows with fail_class ───────
def test_unavailable_outcome_row_carries_fail_class():
    rows = rw.outcome_rows(_unavail("permanent"), producer="codex_review_wrapper", arc_id="pr-1", lane_id="h-w-1", round_n=1)
    assert rows[0]["record_kind"] == "reviewer_unavailable" and rows[0]["finding_type"] == "permanent-fail-exit"
    rows = rw.outcome_rows(_unavail("transient"), producer="codex_review_wrapper", arc_id="pr-1", lane_id="h-w-1", round_n=1)
    assert rows[0]["finding_type"] == "transient-retry"
```

- [ ] **Step 2: Run to verify RED**

Run: `uv run pytest tools/test_review_wrapper.py -q` → `ModuleNotFoundError: review_wrapper_common`.

- [ ] **Step 3: Write the module**

`tools/review_wrapper_common.py`:

```python
#!/usr/bin/env python3
"""Fail-closed core shared by every reviewer channel wrapper (C-HE-15/16/17).

A verdict COUNTS only when the channel's output parses to its declared JSON
schema AND every one of the six binding fields byte-equals the value this
module computed for the invocation. Exit codes are never a completion signal.
Terminal states are exactly APPROVE / BLOCK / REVIEWER_UNAVAILABLE.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

import finding_record as fr
from agy_review import TOTAL_REVIEW_TIMEOUT_SECONDS

REPO = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO / "tools" / "review_schemas"

TERMINAL_STATES = ("APPROVE", "BLOCK", "REVIEWER_UNAVAILABLE")
BINDING_FIELDS = ("head_sha", "base_sha", "diff_digest", "reviewer_identity", "prompt_version", "config_hash")

#: C-HE-16 §3 retry parameters. total budget reuses agy_review's shared deadline.
PER_ATTEMPT_TIMEOUT_S = 550.0
MAX_ATTEMPTS = 2
TOTAL_BUDGET_S = TOTAL_REVIEW_TIMEOUT_SECONDS  # 1260.0
SECOND_ATTEMPT_MARGIN_S = 30.0

#: C-HE-16 §4 -- per-CLI classifier, ONE ROW PER (channel, regex, class). First match wins.
#: Unknown text -> transient (fail-safe toward retry-then-block, never toward APPROVE).
#: This table WILL drift with vendor error text; every row is unit-tested in
#: tools/test_review_wrapper.py::test_classifier_table.
CLASSIFIER: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("codex", re.compile(r"requires a newer version of Codex"), "permanent"),
    ("codex", re.compile(r"not logged in|login|unauthorized|401|403", re.I), "permanent"),
    ("codex", re.compile(r"command not found"), "permanent"),
    ("codex", re.compile(r"rate limit|429|timed out|ETIMEDOUT|ECONNRESET", re.I), "transient"),
    ("gemini", re.compile(r"antigravity .* not (installed|logged in)|unauthorized", re.I), "permanent"),
    ("gemini", re.compile(r"RESOURCE_EXHAUSTED|429|deadline", re.I), "transient"),
)

_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.S)


@dataclass
class Attempt:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool


@dataclass
class ReviewOutcome:
    terminal: str                       # APPROVE | BLOCK | REVIEWER_UNAVAILABLE
    channel: str
    failure_class: str | None           # permanent | transient | None
    reason: str
    findings: list[dict] = field(default_factory=list)
    binding: dict[str, str] | None = None
    source: str | None = None           # stdout | session-artifact | None


def classify(channel: str, text: str) -> str:
    for ch, rx, cls in CLASSIFIER:
        if ch == channel and rx.search(text):
            return cls
    return "transient"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def compute_binding(repo: Path, base: str, *, channel: str, prompt_version: str, config_hash: str) -> dict[str, str]:
    """The wrapper's OWN values for the six binding fields (C-HE-15 §4). Never read from the channel."""
    head_sha = _git(repo, "rev-parse", "HEAD")
    base_sha = _git(repo, "merge-base", base, "HEAD")
    diff = subprocess.run(["git", "-C", str(repo), "diff", base_sha, "HEAD"], check=True, capture_output=True, text=True).stdout
    return {
        "head_sha": head_sha,
        "base_sha": base_sha,
        "diff_digest": hashlib.sha256(diff.encode()).hexdigest(),
        "reviewer_identity": channel if channel.endswith("-review") else f"{channel}-review",
        "prompt_version": prompt_version,
        "config_hash": config_hash,
    }


def load_schema(channel: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{channel}.schema.json").read_text())


def extract_fenced_json(text: str) -> str | None:
    blocks = _FENCE_RE.findall(text)
    return blocks[-1] if blocks else None


def parse_verdict(channel: str, text: str, expected: dict[str, str], *, source: str = "stdout") -> ReviewOutcome:
    """Positive parse or REVIEWER_UNAVAILABLE. Nothing in here maps absence to APPROVE."""
    def unavailable(reason: str) -> ReviewOutcome:
        return ReviewOutcome("REVIEWER_UNAVAILABLE", channel, None, reason, [], None, source)

    if not text or not text.strip():
        return unavailable("empty output")
    raw = extract_fenced_json(text)
    if raw is None:
        return unavailable("no fenced json block")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        return unavailable(f"malformed json: {exc.msg}")
    try:
        jsonschema.validate(body, load_schema(channel))
    except jsonschema.ValidationError as exc:
        return unavailable(f"schema: {exc.message}")
    for key in BINDING_FIELDS:
        if body[key] != expected[key]:
            return unavailable(f"binding mismatch on {key}: got {body[key]!r} expected {expected[key]!r}")
    return ReviewOutcome(body["verdict"], channel, None, "", list(body["findings"]), dict(expected), source)


def run_with_retry(
    invoke: Callable[[float], Attempt],
    *,
    channel: str,
    expected: dict[str, str],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> ReviewOutcome:
    """C-HE-16 §3: 550 s x 2 under a 1260 s shared deadline; permanent skips retry.

    ``deadline`` is an absolute value on ``clock``'s axis. Attempt 2's timeout is
    min(550, remaining - 30) computed at attempt time. Exhaustion of the budget
    is HITL-recoverable (a wedged reviewer login is human-fixable), not permanent.
    """
    last_reason = "no attempt made"
    for attempt_n in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - clock()
        timeout = PER_ATTEMPT_TIMEOUT_S if attempt_n == 1 else min(PER_ATTEMPT_TIMEOUT_S, remaining - SECOND_ATTEMPT_MARGIN_S)
        if timeout <= 0:
            return ReviewOutcome("REVIEWER_UNAVAILABLE", channel, "transient",
                                 f"HITL-recoverable: review budget exhausted before attempt {attempt_n} ({last_reason})")
        att = invoke(timeout)
        combined = (att.stdout or "") + "\n" + (att.stderr or "")
        if att.timed_out:
            last_reason = f"attempt {attempt_n} timed out after {timeout:.0f}s"
            continue  # transient by definition
        outcome = parse_verdict(channel, att.stdout, expected)
        if outcome.terminal != "REVIEWER_UNAVAILABLE":
            return outcome
        cls = classify(channel, combined)
        last_reason = f"attempt {attempt_n}: {outcome.reason}"
        if cls == "permanent":
            return ReviewOutcome("REVIEWER_UNAVAILABLE", channel, "permanent", last_reason)
        # transient (incl. empty first attempt) -> one bounded re-invocation, no backoff
    if clock() >= deadline:
        return ReviewOutcome("REVIEWER_UNAVAILABLE", channel, "transient", f"HITL-recoverable: {last_reason}")
    return ReviewOutcome("REVIEWER_UNAVAILABLE", channel, "transient", last_reason)


def run_with_failover(
    primary: Callable[[], ReviewOutcome],
    failover: Callable[[], ReviewOutcome],
) -> tuple[ReviewOutcome, ReviewOutcome | None]:
    """C-HE-17: on primary REVIEWER_UNAVAILABLE invoke the failover ONCE under the identical bar."""
    p = primary()
    if p.terminal != "REVIEWER_UNAVAILABLE":
        return p, None
    return p, failover()


def exit_code(outcome: ReviewOutcome) -> int:
    return {"APPROVE": 0, "BLOCK": 1, "REVIEWER_UNAVAILABLE": 2}[outcome.terminal]


def env_arc_and_lane() -> tuple[str, str]:
    """arc_id/lane_id for wrapper rows. Lane-init (U-HE-31) exports HARNESS_LANE_ID; before it, a
    host-cwd fallback keeps the row valid (never empty, never ':')."""
    arc_id = os.environ.get("HARNESS_ARC_ID") or f"branch-{_git(Path.cwd(), 'rev-parse', '--abbrev-ref', 'HEAD')}"
    lane_id = os.environ.get("HARNESS_LANE_ID") or f"{socket.gethostname().split('.')[0]}-{Path.cwd().name}-nolane"
    return arc_id.replace(":", "_"), lane_id.replace(":", "_")


def record_round_outcome_if_reserved(arc_id: str, round_n: int, *, channel: str, terminal: str, finding_count: int) -> None:
    """C-HE-25: persist the per-round terminal outcome on the arc's reservation (folded into the arc row at drain,
    U-HE-19; consumed by N6's REVIEWER_UNAVAILABLE exclusion, U-HE-34). No reservation (pre-S4b or backfill) → no-op."""
    try:
        import reservations as rs
        if rs.current(arc_id) is not None:
            rs.record_round_outcome(arc_id, round_n, channel=channel, terminal=terminal, finding_count=finding_count)
    except Exception as exc:  # noqa: BLE001 -- the review outcome itself is already recorded in the gate log
        print(f"review wrapper: round outcome not persisted ({exc})", file=sys.stderr)


def outcome_rows(outcome: ReviewOutcome, *, producer: str, arc_id: str, lane_id: str, round_n: int) -> list[dict]:
    """C-HE-24 rows for one outcome: one `finding` row per finding, or one `reviewer_unavailable` row."""
    b = outcome.binding or {}
    env_common = dict(ts=fr.now_iso(), arc_id=arc_id, lane_id=lane_id, head_sha=b.get("head_sha"),
                      base_sha=b.get("base_sha"), diff_digest=b.get("diff_digest"), round_n=round_n)
    if outcome.terminal == "REVIEWER_UNAVAILABLE":
        ft = "permanent-fail-exit" if outcome.failure_class == "permanent" else "transient-retry"
        core = fr.FindingCore(
            finding_id=fr.make_finding_id(producer, b.get("head_sha") or "nohead", outcome.channel, 0),
            location=outcome.channel, observed_evidence=outcome.reason,
            expected_contract="C-HE-15 §1 positive schema parse", severity="hard" if ft.startswith("permanent") else "warn",
            finding_type=ft, lineage_claim="wrapper", producer=producer,
        )
        return [fr.make_row(core, fr.Envelope(record_kind="reviewer_unavailable",
                                              cause_attribution=f"reviewer_unavailable_{outcome.failure_class}", **env_common))]
    rows = []
    for n, f in enumerate(outcome.findings, start=1):
        core = fr.FindingCore(
            finding_id=fr.make_finding_id(producer, b["head_sha"], f["location"], n),
            location=f["location"], observed_evidence=f["message"], expected_contract="reviewer finding",
            severity=f["severity"], finding_type=f"terminal-{outcome.terminal.lower()}", lineage_claim="fresh",
            producer=producer,
        )
        rows.append(fr.make_row(core, fr.Envelope(record_kind="finding", **env_common)))
    return rows
```

- [ ] **Step 4: Run to verify GREEN**

Run: `uv run pytest tools/test_review_wrapper.py -q` → all pass (26 tests incl. parametrizations).

- [ ] **Step 5: Mutation probe (spec-marked, C-HE-15 verification)**

`just mutation-probe --file tools/review_wrapper_common.py --lines <the `if not text or not text.strip(): return unavailable("empty output")` lines> --test "uv run pytest tools/test_review_wrapper.py::test_empty_stdout_exit0_is_unavailable -q"` → PINNED. (Removing the guard makes the empty string fall to "no fenced json block", still unavailable — so ALSO probe the `no fenced json block` return: the test must go red only when *both* are removed; the spec's target is "revert to exit-code keying → empty output reads APPROVE". Implement the probe by pointing `--lines` at the whole `parse_verdict` unavailable-path block through the fenced-block check; the executor records which range went RED.)

- [ ] **Step 6: Register + commit**

```bash
git add tools/review_wrapper_common.py tools/test_review_wrapper.py tools/codex-parity-check.sh
git commit -m "feat(he-lanes): U-HE-02 fail-closed review wrapper core — schema parse, classifier, retry (C-HE-15/16)"
```

**Acceptance (functional).** Battery green; classifier table has ≥ 1 test per row; `python -c "import review_wrapper_common as rw; assert rw.classify('codex','???')=='transient'"`. **Manifest rows (U-HE-05):** `tools/test_review_wrapper.py` phase0, mutation-probe yes; `::test_failover_*` phase0.

---

### U-HE-03: Review verdict schemas for `codex`, `gemini`, `merge-gate`

**Scope.** Declare each mandatory channel's output schema as a JSON-Schema file: `verdict ∈ {APPROVE, BLOCK}`, `findings[]` items `{severity ∈ {P1,P2,P3}, location, message}` with `additionalProperties: false`, and all six binding fields required.

**Spec linkage.** C-HE-15 §4; C-HE-17 §4 (gemini must carry the same schema-shape rule); C-HE-24 §3 (this severity triple is distinct from the guard's `hard/warn/info`).

**Files.** Create `tools/review_schemas/codex.schema.json`, `tools/review_schemas/gemini.schema.json`, `tools/review_schemas/merge-gate.schema.json`. Test lives in U-HE-02 (`test_schema_requires_all_six_binding_fields`) plus one extra:

- [ ] **Step 1: Add to `tools/test_review_wrapper.py`**

```python
@pytest.mark.parametrize("channel", ["codex", "gemini", "merge-gate"])
def test_channel_schema_shape(channel):
    s = rw.load_schema(channel)
    assert s["additionalProperties"] is False
    assert s["properties"]["verdict"]["enum"] == ["APPROVE", "BLOCK"]
    item = s["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["severity"]["enum"] == ["P1", "P2", "P3"]
    assert set(rw.BINDING_FIELDS) <= set(s["required"])
```

- [ ] **Step 2: RED** — `FileNotFoundError` for the schema files.

- [ ] **Step 3: Write the three files** — identical body except `reviewer_identity`:

`tools/review_schemas/codex.schema.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "codex-review verdict (C-HE-15 §4)",
  "type": "object",
  "additionalProperties": false,
  "required": ["verdict", "findings", "head_sha", "base_sha", "diff_digest", "reviewer_identity", "prompt_version", "config_hash"],
  "properties": {
    "verdict": {"enum": ["APPROVE", "BLOCK"]},
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["severity", "location", "message"],
        "properties": {
          "severity": {"enum": ["P1", "P2", "P3"]},
          "location": {"type": "string"},
          "message": {"type": "string"}
        }
      }
    },
    "head_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
    "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
    "diff_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "reviewer_identity": {"const": "codex-review"},
    "prompt_version": {"type": "string", "minLength": 1},
    "config_hash": {"type": "string", "minLength": 1}
  }
}
```
`gemini.schema.json`: same, `"reviewer_identity": {"const": "gemini-review"}`, title `gemini-review verdict`.
`merge-gate.schema.json`: same, `"reviewer_identity": {"pattern": "^merge-gate-[a-z-]+$"}` (one per lens), title `merge-gate lens verdict`.

- [ ] **Step 4: GREEN** — `uv run pytest tools/test_review_wrapper.py -q -k schema`.

- [ ] **Step 5: Commit**
```bash
git add tools/review_schemas/codex.schema.json tools/review_schemas/gemini.schema.json tools/review_schemas/merge-gate.schema.json tools/test_review_wrapper.py
git commit -m "feat(he-lanes): U-HE-03 declared verdict schemas per channel (C-HE-15 §4)"
```

**Acceptance.** Three files exist; shape test green. (The test file for U-HE-02 depends on these; in practice land U-HE-03's files inside U-HE-02's commit if executing serially — coherent rollback is preserved either way.)

---

### U-HE-04: `tools/codex_review.py` fail-closed wrapper + session-artifact discovery + `just codex-review` reroute

**Scope.** Create the codex channel wrapper: bounded `codex review --base <base> "<instructions>"` invocation with exit-code-independent capture, prompt that requires the fenced JSON block with the six binding values, `run_with_retry`, session-artifact discovery when stdout is inconclusive (newest file under `~/.codex/sessions/` modified after the wrapper's start containing the invocation's `head_sha`, ≤ 130 s after exit), positive parse from whichever source, finding rows on silent death, and rerouting `just codex-review` through it.

**Spec linkage.** C-HE-18 §1 (mirror `agy_review` hardening; `just codex-review` routes through it), §2 (session artifact discovery, 130 s), §3 (zero-byte/auth-only → finding row `producer=codex_review_wrapper`); C-HE-15 §4 (fenced block required by the channel prompt); C-HE-16 §3 (retry); C-HE-24 (rows).

**Files.**
- Create: `tools/codex_review.py`
- Modify: `tools/test_review_wrapper.py` (add the artifact fixture + wrapper tests)
- Modify: `justfile:593-595` (`codex-review` recipe body → `uv run python tools/codex_review.py --base {{base}}`)

**Interfaces.**
- Consumes: `review_wrapper_common` (U-HE-02), `agy_review.run_bounded` (`tools/agy_review.py:99`).
- Produces:
  ```python
  SESSIONS_DIR = Path.home() / ".codex" / "sessions"
  ARTIFACT_LAG_S = 130.0
  PROMPT_VERSION = "codex-review-v1"
  def review_instructions(binding: dict[str, str]) -> str
  def build_command(base: str, instructions: str) -> list[str]
  def find_session_artifact(head_sha: str, *, started_at: float, now: float, root: Path = SESSIONS_DIR) -> Path | None
  def run_codex_review(repo: Path, base: str, *, invoke=None, clock=time.monotonic) -> ReviewOutcome
  def main(argv: list[str] | None = None) -> int
  ```

**Depends on.** U-HE-02, U-HE-03, U-HE-01.

**Interface probe (done at planning time, re-run at execution):** `codex review --help` shows `Usage: codex review [OPTIONS] [PROMPT]` — "Custom review instructions" — so the instructions ride as the positional argument. Session artifacts live at `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` (verified 2026-08-18).

- [ ] **Step 1: Add failing tests to `tools/test_review_wrapper.py`**

```python
import time
from pathlib import Path

import codex_review as cr


def _artifact_tree(tmp_path: Path, head: str, mtime: float) -> Path:
    d = tmp_path / "2026" / "08" / "18"
    d.mkdir(parents=True)
    p = d / "rollout-2026-08-18T00-00-00-abc.jsonl"
    # real shape: the assistant text (fenced block, newlines ESCAPED inside the string) nested in a JSONL envelope
    p.write_text(json.dumps({"type": "response_item", "payload": {"role": "assistant", "content": [{"type": "output_text", "text": _block(head_sha=head)}]}}) + "\n")
    import os
    os.utime(p, (mtime, mtime))
    return p


def test_artifact_text_decodes_jsonl_envelopes(tmp_path):
    p = _artifact_tree(tmp_path, "a" * 40, mtime=1.0)
    raw = p.read_text(); decoded = cr.artifact_text(p)
    assert cr.rw.extract_fenced_json(raw) is None            # the raw envelope hides the fence behind escaping
    assert cr.rw.extract_fenced_json(decoded) is not None    # decoding exposes it


def test_session_artifact_discovery_newest_after_start_containing_head(tmp_path):
    old = _artifact_tree(tmp_path / "a", "a" * 40, mtime=100.0)
    hit = _artifact_tree(tmp_path / "b", "a" * 40, mtime=200.0)
    # roots differ; search each
    assert cr.find_session_artifact("a" * 40, started_at=150.0, now=210.0, root=tmp_path / "b") == hit
    assert cr.find_session_artifact("a" * 40, started_at=150.0, now=210.0, root=tmp_path / "a") is None  # too old
    assert old is not None


def test_log_frozen_but_artifact_has_verdict_parses_from_artifact(tmp_path, monkeypatch):
    """PR #1386 mode: stdout inconclusive, session artifact carries the verdict."""
    head = "a" * 40
    _artifact_tree(tmp_path, head, mtime=time.time() + 1)
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(cr, "_binding", lambda repo, base: {**EXPECTED, "head_sha": head})

    def invoke(timeout):
        return rw.Attempt(stdout="working...\n", stderr="", returncode=0, timed_out=False)

    out = cr.run_codex_review(Path("."), "main", invoke=invoke)
    assert out.terminal == "APPROVE" and out.source == "session-artifact"


def test_artifact_polling_capped_by_shared_deadline(tmp_path, monkeypatch):
    """Two 550 s attempts + artifact polling must not exceed the 1260 s budget (Codex round-1 P2)."""
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(cr.time, "time", lambda: fake_now["t"])
    monkeypatch.setattr(cr.time, "sleep", lambda s: fake_now.__setitem__("t", fake_now["t"] + s))
    clock = {"m": 0.0}
    def invoke(timeout):
        clock["m"] += timeout; fake_now["t"] += timeout
        return rw.Attempt("", "", 0, False)
    out = cr.run_codex_review(Path("."), "main", invoke=invoke, clock=lambda: clock["m"])
    assert out.terminal == "REVIEWER_UNAVAILABLE"
    assert fake_now["t"] - 1000.0 <= rw.TOTAL_BUDGET_S + 1e-6


def test_zero_byte_output_emits_finding_row(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    monkeypatch.setattr(cr.fr, "GATE_LOG_JSONL", tmp_path / "gate.jsonl")
    monkeypatch.setattr(cr, "ARTIFACT_LAG_S", 0.0)
    rc = cr.main(["--base", "main", "--invoke-test-empty"])
    assert rc == 2
    rows = cr.fr.read_rows(tmp_path / "gate.jsonl")
    assert rows and rows[0]["producer"] == "codex_review_wrapper" and rows[0]["record_kind"] == "reviewer_unavailable"


def test_wrapper_persists_round_outcome_on_reservation(monkeypatch):
    calls = []
    import reservations as rs
    monkeypatch.setattr(rs, "current", lambda arc_id: (1, {"state": "open"}))
    monkeypatch.setattr(rs, "record_round_outcome", lambda arc_id, n, **kw: calls.append((arc_id, n, kw)))
    rw.record_round_outcome_if_reserved("pr-1", 2, channel="codex", terminal="REVIEWER_UNAVAILABLE", finding_count=0)
    assert calls == [("pr-1", 2, {"channel": "codex", "terminal": "REVIEWER_UNAVAILABLE", "finding_count": 0})]


def test_build_command_is_codex_review_with_positional_instructions():
    cmd = cr.build_command("main", "INSTR")
    assert cmd[:2] == ["codex", "review"] and "--base" in cmd and cmd[-1] == "INSTR"
```

- [ ] **Step 2: RED** — `ModuleNotFoundError: codex_review`.

- [ ] **Step 3: Write the wrapper**

`tools/codex_review.py`:

```python
#!/usr/bin/env python3
"""Fail-closed wrapper for the Codex review channel (C-HE-18).

Mirrors tools/agy_review.py's hardening: bounded timeout, exit-code-independent
capture, declared-schema parse, permanent/transient classification, and a
REVIEWER_UNAVAILABLE terminal on ANY parse failure. When stdout is inconclusive
it reads the channel's own session artifact (the PR #1386 mode: log frozen at
313 bytes for 130 s after process exit while the real verdict sat only in
~/.codex/sessions/) -- and still requires a positive schema parse from it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import finding_record as fr
import review_wrapper_common as rw
from agy_review import run_bounded

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
ARTIFACT_LAG_S = 130.0           # measured PR #1386 lag (C-HE-18 §2)
PROMPT_VERSION = "codex-review-v1"
CHANNEL = "codex"
PRODUCER = "codex_review_wrapper"


def review_instructions(binding: dict[str, str]) -> str:
    return (
        "Review the diff for correctness defects. When done, print ONE fenced ```json block, "
        "and nothing after it, with exactly these keys: verdict (APPROVE|BLOCK), findings "
        "(array of {severity: P1|P2|P3, location, message}), and copy these six values VERBATIM: "
        + ", ".join(f"{k}={binding[k]}" for k in rw.BINDING_FIELDS)
        + ". No other keys. A missing or altered value invalidates the review."
    )


def build_command(base: str, instructions: str) -> list[str]:
    return ["codex", "review", "-c", 'preferred_auth_method="chatgpt"', "--base", base, instructions]


def _config_hash() -> str:
    cfg = Path.home() / ".codex" / "config.toml"
    body = cfg.read_bytes() if cfg.exists() else b""
    return hashlib.sha256(body).hexdigest()[:16]


def _binding(repo: Path, base: str) -> dict[str, str]:
    return rw.compute_binding(repo, base, channel=CHANNEL, prompt_version=PROMPT_VERSION, config_hash=_config_hash())


def artifact_text(path: Path) -> str:
    """Session artifacts are JSONL envelopes: the assistant text (with its fenced block) sits INSIDE string fields,
    newline-escaped. Deserialize every line and collect all string values so the fenced JSON is visible to the parser
    (Codex round-2 P1: raw `read_text()` can never match `_FENCE_RE`)."""
    out: list[str] = []
    def collect(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)
        elif isinstance(v, list):
            for x in v:
                collect(x)
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            collect(json.loads(line))
        except json.JSONDecodeError:
            out.append(line)          # a non-JSON line is kept verbatim; the schema parse decides
    return "\n".join(out)


def find_session_artifact(head_sha: str, *, started_at: float, now: float, root: Path = SESSIONS_DIR) -> Path | None:
    """Newest file under root modified after started_at whose content contains head_sha (C-HE-18 §2)."""
    if not root.is_dir():
        return None
    candidates = sorted(
        (p for p in root.rglob("*.jsonl") if p.stat().st_mtime >= started_at and p.stat().st_mtime <= now),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for p in candidates:
        try:
            if head_sha in p.read_text(errors="replace"):
                return p
        except OSError:
            continue
    return None


def _default_invoke(repo: Path, base: str, instructions: str) -> Callable[[float], rw.Attempt]:
    def invoke(timeout: float) -> rw.Attempt:
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        try:
            proc = run_bounded(build_command(base, instructions), cwd=repo, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return rw.Attempt("", "", None, True)
        return rw.Attempt(proc.stdout or "", proc.stderr or "", proc.returncode, False)
    return invoke


def run_codex_review(repo: Path, base: str, *, invoke=None, clock: Callable[[], float] = time.monotonic) -> rw.ReviewOutcome:
    binding = _binding(repo, base)
    instructions = review_instructions(binding)
    invoke = invoke or _default_invoke(repo, base, instructions)
    started_wall = time.time()
    deadline = clock() + rw.TOTAL_BUDGET_S
    wall_deadline = started_wall + rw.TOTAL_BUDGET_S
    used_artifact = False

    def attempt_with_artifact(timeout: float) -> rw.Attempt:
        nonlocal used_artifact
        att = invoke(timeout)
        if rw.parse_verdict(CHANNEL, att.stdout, binding).terminal == "REVIEWER_UNAVAILABLE" and not att.timed_out:
            # stdout inconclusive: wait up to ARTIFACT_LAG_S for the session artifact
            end = min(time.time() + ARTIFACT_LAG_S, wall_deadline)      # never past the shared 1260 s budget (Codex round-1 P2)
            while True:
                art = find_session_artifact(binding["head_sha"], started_at=started_wall, now=time.time(), root=SESSIONS_DIR)
                if art is not None:
                    text = artifact_text(art)
                    if rw.parse_verdict(CHANNEL, text, binding).terminal != "REVIEWER_UNAVAILABLE":
                        used_artifact = True
                        return rw.Attempt(text, att.stderr, att.returncode, False)
                if time.time() >= end:
                    break
                time.sleep(min(2.0, max(0.0, end - time.time())))
        return att

    outcome = rw.run_with_retry(attempt_with_artifact, channel=CHANNEL, expected=binding, deadline=deadline, clock=clock)
    if used_artifact and outcome.terminal != "REVIEWER_UNAVAILABLE":
        outcome.source = "session-artifact"
    return outcome


def _emit_rows(outcome: rw.ReviewOutcome, *, producer: str = PRODUCER, channel: str = CHANNEL) -> None:
    arc_id, lane_id = rw.env_arc_and_lane()
    round_n = int(os.environ.get("HARNESS_ROUND_N", "0"))
    for row in rw.outcome_rows(outcome, producer=producer, arc_id=arc_id, lane_id=lane_id, round_n=round_n):
        fr.append_row(row)
    rw.record_round_outcome_if_reserved(arc_id, round_n, channel=channel, terminal=outcome.terminal, finding_count=len(outcome.findings))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="main")
    p.add_argument("--invoke-test-empty", action="store_true", help=argparse.SUPPRESS)  # test seam: zero-byte channel
    args = p.parse_args(argv)
    invoke = (lambda timeout: rw.Attempt("", "", 0, False)) if args.invoke_test_empty else None
    outcome = run_codex_review(Path.cwd(), args.base, invoke=invoke)
    _emit_rows(outcome)
    for f in outcome.findings:
        print(f"- [{f['severity']}] {f['location']}: {f['message']}")
    print(f"codex-review: {outcome.terminal}" + (f" ({outcome.failure_class}: {outcome.reason})" if outcome.reason else ""), file=sys.stderr)
    return rw.exit_code(outcome)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Reroute the recipe** — `justfile:593-595`:

```make
# Out-of-family review of the current branch vs BASE (default main), subscription auth.
# Routed through the fail-closed wrapper (C-HE-18): schema-parsed verdict, session-artifact
# fallback, REVIEWER_UNAVAILABLE on any parse failure. Exit 0 APPROVE / 1 BLOCK / 2 UNAVAILABLE.
codex-review base='main': _require-codex-subscription
    uv run python tools/codex_review.py --base {{base}}
```

- [ ] **Step 5: GREEN** — `uv run pytest tools/test_review_wrapper.py -q`; then a live smoke: `just codex-review` on a trivial branch prints a terminal state and exits 0/1/2 (never silently 0 on empty).

- [ ] **Step 6: Commit**
```bash
git add tools/codex_review.py tools/test_review_wrapper.py justfile
git commit -m "feat(he-lanes): U-HE-04 codex_review.py fail-closed wrapper + session-artifact fallback; just codex-review routes through it (C-HE-18)"
```

**Acceptance.** Battery + artifact fixture green; `just codex-review` on a real diff yields a parsed verdict or `REVIEWER_UNAVAILABLE` with a `reviewer_unavailable` row in `.harness/merge-gate-log.jsonl`. **Manifest:** part of `tools/test_review_wrapper.py` (phase0).

---
### U-HE-05: Verification manifest scaffold — `tools/lanes_verify.py`, `just lanes-verify` / `lanes-phase0-check` / `mutation-probe-coverage-check`

**Scope.** Create the umbrella runner that owns spec §8.1's manifest as data: one `Row` per test artifact with `contract`, `tag`, `runs_in`, `mutation_probe`, allowed skip reasons; `lanes-verify` runs every row; `lanes-phase0-check` runs `phase0` rows and treats any skip as failure; `mutation-probe-coverage-check` asserts every `mutation_probe=True` row has a PINNED probe result logged. Later units append their rows here.

**Spec linkage.** §8.1 (manifest, skip policy, `phase0` meta row), §0.3 (probe RED-first), C-HE-13 §1 (mechanical pilot gate consumes `lanes-phase0-check`), C-HE-30 (`tools/test_store_audit.py` is a phase0 row).

**Files.**
- Create: `tools/lanes_verify.py`, `tools/test_lanes_verify.py`
- Modify: `tools/mutation_probe.py` (append a result line to `.harness/mutation-probe-log.jsonl` on every exit — a run log, derived; listed in the U-HE-14 store audit as a derived family)
- Modify: `justfile` (three recipes after `mutation-probe`)
- Modify: `tools/codex-parity-check.sh`

**Interfaces.**
```python
@dataclass(frozen=True)
class Row: contract: str; artifact: str; tag: str; runs_in: str; mutation_probe: bool; skip_reasons: tuple[str, ...] = (); depends: str = ""
TAGS = ("phase0", "phase1", "measurement", "layer2", "env", "operator-gated")
ALLOWED_SKIP_REASONS = ("docker-daemon-absent", "provider-login-absent", "gh-auth-absent")
MANIFEST: list[Row]
@dataclass class Result: row: Row; status: str  # pass|fail|skip ; reason: str
def run_row(row: Row, *, runner=subprocess.run) -> Result
def phase0_rows() -> list[Row]
def coverage_gaps(log_path: Path = PROBE_LOG) -> list[Row]
def main(argv) -> int   # subcommands: verify | phase0 | coverage
```
Artifact grammar: `pytest:<nodeid>` → `uv run pytest -q -rs <nodeid>`; `shell:<path>` → `bash <path>`; `just:<recipe>` → `just <recipe>`; `live:<desc>` → not runnable here (operator-gated), reported as such and never counted as pass.

**Depends on.** (none) for the runner; rows reference tests from other units.

- [ ] **Step 1: Failing tests** — `tools/test_lanes_verify.py`:

```python
from __future__ import annotations
import json
from pathlib import Path
import lanes_verify as lv


def _row(tag="phase0", art="pytest:tools/test_x.py::t", mp=False, skips=()):
    return lv.Row("C-HE-99", art, tag, "local + CI", mp, tuple(skips))


def test_manifest_rows_well_formed():
    for r in lv.MANIFEST:
        assert r.tag in lv.TAGS and r.artifact.split(":", 1)[0] in {"pytest", "shell", "just", "live"}
        assert set(r.skip_reasons) <= set(lv.ALLOWED_SKIP_REASONS)


def test_phase0_skip_counts_as_fail(monkeypatch):
    def fake_run(cmd, **kw):
        class P: returncode = 0; stdout = "1 skipped\nSKIPPED [1] x.py:1: docker-daemon-absent\n"; stderr = ""
        return P()
    res = lv.run_row(_row(tag="env", skips=("docker-daemon-absent",)), runner=fake_run)
    assert res.status == "skip"
    assert lv.phase0_verdict([lv.Result(_row(tag="phase0"), "skip", "docker-daemon-absent")]) == 1


# mutation-probe: match pinned node ids by substring instead of exact equality in coverage_gaps()
def test_coverage_gap_when_probe_never_pinned(tmp_path: Path, monkeypatch):
    log = tmp_path / "mp.jsonl"
    monkeypatch.setattr(lv, "MANIFEST", [_row(mp=True, art="pytest:tools/test_x.py::t")])
    assert lv.coverage_gaps(log) and lv.coverage_gaps(log)[0][1].endswith("::t")
    log.write_text(json.dumps({"test": "uv run pytest tools/test_x.py::t -q", "rc": 0}) + "\n")
    assert lv.coverage_gaps(log) == []


def test_file_level_row_requires_every_annotation_exactly(tmp_path: Path, monkeypatch):
    """One pinned test in a file must NOT count as coverage for the file's other annotated probes."""
    monkeypatch.setattr(lv, "REPO", tmp_path)
    (tmp_path / "tools").mkdir(); (tmp_path / "tools" / "test_y.py").write_text(
        "# mutation-probe: a\ndef test_a(): pass\n\n# mutation-probe: b\n@pytest.mark.x\ndef test_b(): pass\n")
    monkeypatch.setattr(lv, "MANIFEST", [_row(mp=True, art="pytest:tools/test_y.py")])
    log = tmp_path / "mp.jsonl"; log.write_text(json.dumps({"test": "uv run pytest tools/test_y.py::test_a -q", "rc": 0}) + "\n")
    assert [n for _, n in lv.coverage_gaps(log)] == ["tools/test_y.py::test_b"]
    log.write_text(log.read_text() + json.dumps({"test": "uv run pytest tools/test_y.py::test_b -q", "rc": 0}) + "\n")
    assert lv.coverage_gaps(log) == []
    log.write_text(json.dumps({"test": "uv run pytest tools/test_y.py -q", "rc": 0}) + "\n")   # whole-file run is NOT per-probe evidence
    assert len(lv.coverage_gaps(log)) == 2


def test_just_args_tokenized_and_placeholder_is_live():
    assert lv._command(_row(tag="phase1", art="just:main-protection-verify")) == ["just", "main-protection-verify"]
    assert lv._command(_row(tag="phase1", art="just:lanes-pilot-report <run-id>")) is None
    assert lv._command(_row(tag="phase0", art="shell:tools/hooks/test_x.sh")) == ["bash", "tools/hooks/test_x.sh"]


def test_shell_probe_rows_can_be_covered(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lv, "MANIFEST", [_row(mp=True, art="shell:tools/hooks/test_loop_lib.sh")])
    log = tmp_path / "mp.jsonl"
    assert [n for _, n in lv.coverage_gaps(log)] == ["tools/hooks/test_loop_lib.sh"]
    log.write_text(json.dumps({"test": "bash tools/hooks/test_loop_lib.sh", "rc": 0}) + "\n")
    assert lv.coverage_gaps(log) == []


def test_unknown_skip_reason_is_fail(monkeypatch):
    def fake_run(cmd, **kw):
        class P: returncode = 0; stdout = "SKIPPED [1] x.py:1: slow\n"; stderr = ""
        return P()
    assert lv.run_row(_row(tag="env", skips=("docker-daemon-absent",)), runner=fake_run).status == "fail"
```

- [ ] **Step 2: RED** — `ModuleNotFoundError: lanes_verify`.

- [ ] **Step 3: Write `tools/lanes_verify.py`**

```python
#!/usr/bin/env python3
"""Spec §8.1 verification manifest as data + the umbrella runners.

`just lanes-verify` runs every row. `just lanes-phase0-check` runs rows tagged
phase0 and treats a skip as a failure (C-HE-13 §1: an implicit precondition is
not a gate). `just mutation-probe-coverage-check` asserts every row marked
mutation_probe has a PINNED probe result in .harness/mutation-probe-log.jsonl.
Only the three named environment skip reasons are legal; "slow" is never one.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE_LOG = REPO / ".harness" / "mutation-probe-log.jsonl"
TAGS = ("phase0", "phase1", "measurement", "layer2", "env", "operator-gated")
ALLOWED_SKIP_REASONS = ("docker-daemon-absent", "provider-login-absent", "gh-auth-absent")
_SKIP_RE = re.compile(r"^SKIPPED \[\d+\] [^:]+:\d+: (.+)$", re.M)


@dataclass(frozen=True)
class Row:
    contract: str
    artifact: str            # pytest:<nodeid> | shell:<path> | just:<recipe> | live:<desc>
    tag: str
    runs_in: str
    mutation_probe: bool
    skip_reasons: tuple[str, ...] = ()
    depends: str = ""


@dataclass
class Result:
    row: Row
    status: str              # pass | fail | skip | live
    reason: str = ""


#: Rows are appended by the unit that lands each artifact. Keep in §8.1 order.
MANIFEST: list[Row] = [
    Row("C-HE-24", "pytest:tools/test_finding_record.py", "phase0", "local + CI", True),
    Row("C-HE-15/16/18", "pytest:tools/test_review_wrapper.py", "phase0", "local + CI", True),
    Row("C-HE-17", "pytest:tools/test_review_wrapper.py::test_failover_invoked_once_on_primary_unavailable_and_blocks", "phase0", "local + CI", False),
    Row("§8.1", "pytest:tools/test_lanes_verify.py", "phase0", "local + CI", True),
    Row("§0.3", "just:mutation-probe-coverage-check", "phase0", "local + CI", False),
]


def _command(row: Row) -> list[str] | None:
    kind, _, target = row.artifact.partition(":")
    if "<" in target and ">" in target:
        return None               # a placeholder argument (e.g. `just:lanes-pilot-report <run-id>`) is a LIVE row
    if kind == "pytest":
        return ["uv", "run", "pytest", "-q", "-rs", target]
    if kind == "shell":
        return ["bash", *target.split()]
    if kind == "just":
        return ["just", *target.split()]      # recipe + controlled args, tokenized (Codex round-5 P1)
    return None  # live


def run_row(row: Row, *, runner=subprocess.run) -> Result:
    cmd = _command(row)
    if cmd is None:
        return Result(row, "live", "operator-gated live step; recorded in the plan evidence log")
    proc = runner(cmd, cwd=REPO, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    skips = _SKIP_RE.findall(out)
    if proc.returncode != 0:
        return Result(row, "fail", out[-2000:])
    if skips:
        bad = [s for s in skips if s.strip() not in row.skip_reasons]
        if bad:
            return Result(row, "fail", f"skip with unlisted reason: {bad}")
        return Result(row, "skip", ";".join(s.strip() for s in skips))
    return Result(row, "pass")


def phase0_rows() -> list[Row]:
    return [r for r in MANIFEST if r.tag == "phase0"]


def phase0_verdict(results: list[Result]) -> int:
    """0 iff every phase0 row passed. A skip is NOT a pass here."""
    return 0 if all(r.status == "pass" for r in results) else 1


_ANNOT = re.compile(r"# mutation-probe: .*\n(?:@[^\n]*\n)*def (test_\w+)")


def _pinned_nodeids(log_path: Path) -> set[str]:
    """Node ids of PINNED probes: the token after `pytest` in the logged test command (normalized, exact)."""
    if not log_path.exists():
        return set()
    out = set()
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("rc") != 0:
            continue
        toks = e["test"].split()
        if "pytest" in toks:
            out.add(toks[toks.index("pytest") + 1])
        elif toks[:1] == ["bash"] and len(toks) > 1:
            out.add(toks[1])                  # shell rows: the probed test script itself is the target (round-5 P1)
    return out


def required_probes(row: Row) -> list[str]:
    """Every `# mutation-probe:` annotation the row's artifact carries -> its exact node id (Codex round-3 P1:
    substring matching let one pinned test cover a whole file). A node-id artifact requires exactly itself."""
    kind, _, target = row.artifact.partition(":")
    if kind == "shell":
        return [target.split()[0]] if row.mutation_probe else []
    if kind != "pytest":
        return [target] if row.mutation_probe else []
    file_part, _, node = target.partition("::")
    if node:
        return [target]
    path = REPO / file_part
    if not path.exists():
        return [target]           # not yet landed: counts as a gap until the file exists and its probes are pinned
    return [f"{file_part}::{name}" for name in _ANNOT.findall(path.read_text())]


def coverage_gaps(log_path: Path = PROBE_LOG) -> list[tuple[Row, str]]:
    pinned = _pinned_nodeids(log_path)
    gaps = []
    for r in MANIFEST:
        if not r.mutation_probe:
            continue
        for node in required_probes(r):
            if node not in pinned:
                gaps.append((r, node))
    return gaps


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = args[0] if args else "verify"
    if mode == "coverage":
        gaps = coverage_gaps()
        for row, node in gaps:
            print(f"UNPROBED {row.contract} {node}")
        return 1 if gaps else 0
    rows = phase0_rows() if mode == "phase0" else MANIFEST
    results = [run_row(r) for r in rows]
    for r in results:
        print(f"{r.status.upper():5} {r.row.contract:14} {r.row.artifact} {('— ' + r.reason) if r.reason else ''}")
    if mode == "phase0":
        return phase0_verdict(results)
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Log probe results** — in `tools/mutation_probe.py` `main()`, at the single exit point, append:

```python
    log = Path(__file__).resolve().parent.parent / ".harness" / "mutation-probe-log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(json.dumps({"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "file": str(args.file),
                             "lines": args.lines, "test": args.test, "rc": rc}, sort_keys=True) + "\n")
```
(`rc` = the exit code about to be returned; add `import json` / `from datetime import UTC, datetime` if absent.) Add `.harness/mutation-probe-log.jsonl` to git (tracked run log) with an initial empty file.

- [ ] **Step 5: Recipes** — after `mutation-probe` in `justfile`:

```make
# ─── §8.1 verification manifest (spec-he-loop-lanes) ─────────────────────────
lanes-verify:
    uv run python tools/lanes_verify.py verify

# Phase-0 gate: every phase0 row must PASS at HEAD; a skip counts as NOT passed.
lanes-phase0-check:
    uv run python tools/lanes_verify.py phase0

# Every manifest row marked mutation-probe must have a PINNED result logged.
mutation-probe-coverage-check:
    uv run python tools/lanes_verify.py coverage
```

- [ ] **Step 6: GREEN** — `uv run pytest tools/test_lanes_verify.py -q`; `just lanes-verify` runs the seeded rows green.

- [ ] **Step 7: Register + commit**
```bash
git add tools/lanes_verify.py tools/test_lanes_verify.py tools/mutation_probe.py .harness/mutation-probe-log.jsonl justfile tools/codex-parity-check.sh
git commit -m "feat(he-lanes): U-HE-05 §8.1 verification manifest runner + phase0 gate + probe coverage check"
```

**Acceptance.** `just lanes-phase0-check` exits 1 when any phase0 row skips (unit-pinned); `just mutation-probe-coverage-check` lists unprobed rows. Coherent rollback: one module + recipes.

---

### U-HE-06: `agy_review.py` adopts the common module — schema-parsed final verdict, classifier, per-segment retry

**Scope.** Make the gemini channel satisfy C-HE-15/16 through the shared module: the final output (single-segment or synthesis) MUST also carry a fenced JSON block matching `gemini.schema.json` with the six binding values (parsed by `rw.parse_verdict`; the existing marker/`VERDICT:` protocol stays as agy's internal truncation guard); process failures classify via `rw.classify("gemini", …)`; each segment invocation gets one bounded transient re-invocation with the dynamic second timeout; outcomes emit C-HE-24 rows with `producer=gemini_review_wrapper`.

**Spec linkage.** C-HE-17 §4 (hardened reference; silent-death modes covered by the same schema parse), C-HE-16 §3–§4, C-HE-15 §3–§4, C-HE-18 §3 (rows).

**Files.** Modify `tools/agy_review.py` (`review_prompt` `:274-364`, `synthesis_prompt` `:367-390`, `report_process_failure` `:449-458`, `run_review` `:466-607`, `main` `:610-620`); modify `tools/test_agy_review.py` (fixture helper).

**Depends on.** U-HE-02, U-HE-03.

- [ ] **Step 1: Failing tests** — add to `tools/test_agy_review.py`:

```python
import review_wrapper_common as rw

def _final_output(verdict: str, binding: dict) -> str:
    body = {"verdict": verdict, "findings": [], **binding}
    return "```json\n" + json.dumps(body) + "\n```\nARTIFACT: COMPLETE\nVERDICT: " + verdict


def test_final_output_without_fenced_json_is_unavailable(tmp_path, monkeypatch, fake_agy):
    """Marker + VERDICT line alone no longer counts (C-HE-15 §1)."""
    fake_agy(stdout="ARTIFACT: COMPLETE\nVERDICT: APPROVE")
    assert agy_review.run_review(tmp_path, "main") == 2


def test_final_output_with_schema_block_and_binding_counts(tmp_path, monkeypatch, fake_agy):
    b = agy_review.gemini_binding(tmp_path, "main")
    fake_agy(stdout=_final_output("APPROVE", b))
    assert agy_review.run_review(tmp_path, "main") == 0


def test_permanent_stderr_text_classifies_permanent(capsys):
    proc = subprocess.CompletedProcess([], 1, "", "antigravity CLI not logged in")
    assert agy_review.report_process_failure(proc) == 2
    assert "permanent" in capsys.readouterr().err


def test_transient_segment_failure_gets_one_bounded_retry(tmp_path, monkeypatch, fake_agy_sequence):
    """First segment call returns empty (transient) → exactly one re-invocation with min(550, remaining-30)."""
    seen = fake_agy_sequence(["", _final_output("APPROVE", agy_review.gemini_binding(tmp_path, "main"))])
    assert agy_review.run_review(tmp_path, "main") == 0
    assert len(seen) == 2 and seen[1].timeout <= rw.PER_ATTEMPT_TIMEOUT_S
```
(`fake_agy` / `fake_agy_sequence` are the file's existing `run_bounded` monkeypatch fixtures — extend `fake_agy_sequence` to record the `timeout=` kwarg per call.) Every existing fixture that returns a final `…COMPLETE\nVERDICT: X` string is updated to `_final_output(X, binding)` in the same sweep (mechanical; ~40 sites; `rg 'VERDICT: (APPROVE|BLOCK)"' tools/test_agy_review.py` enumerates them).

- [ ] **Step 2: RED** — the four new tests fail (`gemini_binding` missing; JSON-less output still returns 0).

- [ ] **Step 3: Implement**

Add near the top of `agy_review.py` (after constants):
```python
import review_wrapper_common as rw   # C-HE-15/16 shared core
import finding_record as fr

GEMINI_PROMPT_VERSION = "gemini-review-v2-json"
PRODUCER = "gemini_review_wrapper"


def gemini_binding(repo: Path, base: str) -> dict[str, str]:
    cfg_hash = hashlib.sha256(f"{MODEL_ARGUMENT}|{MAX_AGY_PRINT_TIMEOUT_SECONDS}".encode()).hexdigest()[:16]
    return rw.compute_binding(repo, base, channel="gemini", prompt_version=GEMINI_PROMPT_VERSION, config_hash=cfg_hash)


def json_block_instruction(binding: dict[str, str]) -> str:
    return (
        " Immediately BEFORE the completion marker, print ONE fenced ```json block with exactly these "
        "keys: verdict (APPROVE|BLOCK), findings (array of {severity: P1|P2|P3, location, message}), and "
        "these six values copied VERBATIM: " + ", ".join(f"{k}={binding[k]}" for k in rw.BINDING_FIELDS)
        + ". No other keys."
    )
```
- `review_prompt(...)` and `synthesis_prompt(...)` gain a keyword `binding: dict[str, str]` and append `json_block_instruction(binding)` before the completion-marker sentence (single-segment) / before "After every numbered result was read completely" (synthesis). Segment prompts (segment_index set) do NOT require the block — only the artifact-complete output does.
- `report_process_failure`: after computing `detail`, add
```python
    cls = rw.classify("gemini", (proc.stdout or "") + "\n" + detail)
    print(f"agy-review: reviewer unavailable ({cls}): {detail}", file=sys.stderr)
    return 2
```
replacing the two-branch print (exit 2 for both; the classification is what changed).
- In `run_review`, compute `binding = gemini_binding(repo, base)` once after `collect_diff`; pass it into the two prompt builders. Wrap each `run_bounded(...)` call in the per-spec loop and the synthesis call with:
```python
def _bounded_with_retry(cmd_for, deadline, *, cwd, env, marker):
    """C-HE-16 §3: one bounded re-invocation on a transient failure; timeout 2 = min(550, remaining-30)."""
    last = None
    for n in (1, 2):
        remaining = deadline - time.monotonic()
        timeout = min(rw.PER_ATTEMPT_TIMEOUT_S, remaining - (0 if n == 1 else rw.SECOND_ATTEMPT_MARGIN_S))
        if timeout <= PARENT_TIMEOUT_GRACE_SECONDS:
            return None
        proc = run_bounded(cmd_for(timeout), cwd=cwd, timeout=timeout, env=env)
        ok, _ = validate_review_output(proc.stdout.strip(), marker)
        if proc.returncode == 0 and ok is None:
            return proc
        if rw.classify("gemini", (proc.stdout or "") + "\n" + (proc.stderr or "")) == "permanent":
            return proc
        last = proc
    return last
```
and, at the final-verdict site (both the synthesis branch and the single-segment `else:` branch), replace the `verdict == "VERDICT: BLOCK"` decision with:
```python
    outcome = rw.parse_verdict("gemini", output, binding)
    if outcome.terminal == "REVIEWER_UNAVAILABLE":
        print(f"agy-review: reviewer unavailable: {outcome.reason}", file=sys.stderr)
        _emit(outcome); return 2
    if "VERDICT: BLOCK" in accepted_verdicts and outcome.terminal != "BLOCK":
        print("agy-review: synthesis contradicted a blocking segment verdict", file=sys.stderr); return 2
    _emit(outcome)
    print(f"agy-review: effective model: {EXPECTED_MODEL_LABEL}"); print(output)
    if outcome.terminal == "BLOCK":
        print("agy-review: blocking findings require resolution", file=sys.stderr); return 1
    return 0
```
with `_emit(outcome)` = `for row in rw.outcome_rows(outcome, producer=PRODUCER, arc_id=a, lane_id=l, round_n=n): fr.append_row(row)` followed by `rw.record_round_outcome_if_reserved(a, n, channel="gemini", terminal=outcome.terminal, finding_count=len(outcome.findings))`, where `a, l = rw.env_arc_and_lane()` and `n = int(os.environ.get("HARNESS_ROUND_N","0"))` (C-HE-25 per-round outcome; Codex round-1 P1).

- [ ] **Step 4: GREEN** — `uv run pytest tools/test_agy_review.py tools/test_review_wrapper.py -q`.

- [ ] **Step 5: Commit**
```bash
git add tools/agy_review.py tools/test_agy_review.py
git commit -m "feat(he-lanes): U-HE-06 gemini channel adopts the fail-closed core — schema block, classifier, bounded retry (C-HE-17 §4)"
```

**Acceptance.** `just gemini-review` on a diff whose output lacks the JSON block exits 2 with `reviewer unavailable`; a compliant output exits 0/1; `.harness/merge-gate-log.jsonl` gains `producer=gemini_review_wrapper` rows.

---

### U-HE-07: Failover chain — `just review-with-failover`, skill carriers, invariant #3 restatement

**Scope.** Wire D-C: on `codex-review` → `REVIEWER_UNAVAILABLE`, invoke `just gemini-review` once under the identical bar; its verdict blocks; both rows recorded. Add the recipe, make `ship-pr` and `roadmap-continue` call it as the review step, and restate invariant #3 in those carriers.

**Spec linkage.** C-HE-17 §1–§5; C-HE-16 inv (permanent never retries same channel; triggers failover); C-HE-21 §2 (live-carrier rule).

**Files.**
- Modify: `tools/codex_review.py` (add `--failover`), `tools/test_review_wrapper.py` (one integration test)
- Modify: `justfile` (recipe after `gemini-review`)
- Modify: `.claude/skills/ship-pr/SKILL.md` (review step), `.claude/skills/roadmap-continue/SKILL.md` (review step + #3 restatement)

**Depends on.** U-HE-04, U-HE-06.

- [ ] **Step 1: Failing test** (in `tools/test_review_wrapper.py`):
```python
def test_codex_review_failover_flag_runs_gemini_once_and_blocks(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cr, "run_codex_review", lambda repo, base, **kw: rw.ReviewOutcome("REVIEWER_UNAVAILABLE", "codex", "permanent", "not logged in"))
    monkeypatch.setattr(cr, "_run_gemini_failover", lambda repo, base: (calls.append(1) or rw.ReviewOutcome("BLOCK", "gemini", None, "", [{"severity": "P1", "location": "l", "message": "m"}], EXPECTED, "stdout")))
    monkeypatch.setattr(cr.fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    assert cr.main(["--base", "main", "--failover"]) == 1
    assert calls == [1]
    kinds = [r["record_kind"] for r in cr.fr.read_rows(tmp_path / "g.jsonl")]
    assert kinds == ["reviewer_unavailable"]                 # primary's row only; the gemini subprocess is the sole emitter of its own rows


def test_failover_preflight_failure_still_records_both_reasons(monkeypatch, tmp_path):
    """`just gemini-review` dies at `_require-antigravity` → the subprocess wrote nothing → the failover path emits."""
    monkeypatch.setattr(cr.fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "ERROR: agy (Antigravity CLI) not found on PATH."))
    monkeypatch.setattr(cr.rw, "compute_binding", lambda *a, **k: dict(EXPECTED))
    monkeypatch.setattr(cr, "_gemini_config_hash", lambda: "x")
    out = cr._run_gemini_failover(Path("."), "main")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "permanent"
    rows = cr.fr.read_rows(tmp_path / "g.jsonl")
    assert len(rows) == 1 and rows[0]["producer"] == "gemini_review_wrapper" and rows[0]["record_kind"] == "reviewer_unavailable"
```
- [ ] **Step 2: RED** — `--failover` unrecognised.
- [ ] **Step 3: Implement** in `tools/codex_review.py`:
```python
def _run_gemini_failover(repo: Path, base: str) -> rw.ReviewOutcome:
    """Identical bar (C-HE-17 §1): the gemini wrapper's own schema parse; exit code read back as terminal."""
    before = sum(1 for r in fr.read_rows() if r["producer"] == "gemini_review_wrapper")
    proc = subprocess.run(["just", "gemini-review", base], cwd=repo, capture_output=True, text=True)
    binding = rw.compute_binding(repo, base, channel="gemini", prompt_version="gemini-review-v2-json", config_hash="failover")
    outcome = rw.parse_verdict("gemini", proc.stdout, {**binding, "config_hash": _gemini_config_hash()})
    if outcome.terminal == "REVIEWER_UNAVAILABLE":
        outcome.failure_class = rw.classify("gemini", proc.stdout + "\n" + proc.stderr)
    after = sum(1 for r in fr.read_rows() if r["producer"] == "gemini_review_wrapper")
    if after == before:
        # The recipe died at its `_require-antigravity` preflight (or the wrapper never ran): no row was written by the
        # subprocess, so BOTH reasons would not be on record (C-HE-17). Emit here -- and only here (Codex round-3 P2).
        _emit_rows(outcome, producer="gemini_review_wrapper", channel="gemini")
    return outcome
```
(`_gemini_config_hash()` imports `agy_review.gemini_binding`'s hash rule so the binding matches — factor it as `agy_review.gemini_config_hash()` in U-HE-06 and import it here.) In `main()`: add `p.add_argument("--failover", action="store_true")`; when set:
```python
    primary, fo = rw.run_with_failover(lambda: run_codex_review(Path.cwd(), args.base, invoke=invoke),
                                       lambda: _run_gemini_failover(Path.cwd(), args.base))
    _emit_rows(primary)
    if fo is not None:
        final = fo            # NO second emission: `just gemini-review` (agy_review, U-HE-06) already wrote its rows
    else:                     # and its round outcome (Codex round-2 P2: no duplicated findings in the ledger)
        final = primary
```
and print/return `rw.exit_code(final)` (a failover `REVIEWER_UNAVAILABLE` → 2 with both reasons on stderr).
- [ ] **Step 4: Recipe** (justfile, after `gemini-review`):
```make
# D-C failover chain (C-HE-17): codex-review, then gemini-review ONCE on REVIEWER_UNAVAILABLE,
# identical bar; the failover verdict blocks. Exit 0/1/2 as codex-review.
# NO `_require-codex-subscription` prerequisite here (Codex round-2 P1): a missing binary / stale login is exactly the
# permanent failure the wrapper classifies (C-HE-16 §4) and that MUST reach the failover (C-HE-17). The wrapper is the
# loud-failure surface: exit 2 + a `reviewer_unavailable` row -- never a silent metered fallback.
review-with-failover base='main':
    uv run python tools/codex_review.py --base {{base}} --failover
```
- [ ] **Step 5: Carriers.** In `.claude/skills/ship-pr/SKILL.md` and `.claude/skills/roadmap-continue/SKILL.md`, replace the review-step instruction that invokes `just codex-review` with `just review-with-failover` and add this sentence under the review step: *"Invariant #3 (restated, C-HE-17 §3): out-of-family review covers Codex-authored work as before, AND serves as the D-C failover for Claude-authored diffs at the identical bar. Exit 2 (`REVIEWER_UNAVAILABLE` on both channels) blocks the arc; record both reasons."*
- [ ] **Step 6: GREEN + commit**
```bash
git add tools/codex_review.py tools/test_review_wrapper.py justfile .claude/skills/ship-pr/SKILL.md .claude/skills/roadmap-continue/SKILL.md
git commit -m "feat(he-lanes): U-HE-07 D-C failover chain + carriers + invariant #3 restatement (C-HE-17)"
```
**Acceptance.** Integration test green; `rg 'review-with-failover' .claude/skills/{ship-pr,roadmap-continue}/SKILL.md` ≥ 1 each; AC#6 both channels carry gate contracts (schemas from U-HE-03).

---

### U-HE-08: CI terminal states — `CANCELLED` explicit in `arc_metrics.py` and `ship-pr`

**Scope.** Introduce the exact CI outcome enum `{SUCCESS, FAILURE, CANCELLED}` and a single predicate `ci_is_green(conclusion)`; name `CANCELLED` explicitly as INCOMPLETE in `ship-pr`'s post-merge acceptance logic (the merge door consumes the same predicate in U-HE-23).

**Spec linkage.** C-HE-19 §1–§3; C-HE-06 §4(vii) consumer.

**Files.** Modify `tools/arc_metrics.py` (new constants near `MERGED_REF` `:79`), `tools/test_arc_metrics.py`, `.claude/skills/ship-pr/SKILL.md:199-204`.

**Depends on.** (none).

- [ ] **Step 1: Failing test** (`tools/test_arc_metrics.py`):
```python
# mutation-probe: add "cancelled" to CI_GREEN in arc_metrics
@pytest.mark.parametrize("conclusion,green", [("success", True), ("failure", False), ("cancelled", False), ("", False), (None, False), ("SUCCESS", True)])
def test_ci_state_cancelled_incomplete(conclusion, green):
    assert am.ci_is_green(conclusion) is green
    assert am.CI_TERMINAL == ("SUCCESS", "FAILURE", "CANCELLED")
```
- [ ] **Step 2: RED**; **Step 3: Implement** after `MERGED_REF`:
```python
#: C-HE-19: CI outcomes are exactly these; CANCELLED is INCOMPLETE, never green.
CI_TERMINAL = ("SUCCESS", "FAILURE", "CANCELLED")
CI_GREEN = frozenset({"SUCCESS"})


def ci_is_green(conclusion: str | None) -> bool:
    """Only an exact `success` counts. Named explicitly (not by whitelist omission): CANCELLED -> False."""
    if not conclusion:
        return False
    c = conclusion.upper()
    if c == "CANCELLED":
        return False
    return c in CI_GREEN
```
- [ ] **Step 4:** In `.claude/skills/ship-pr/SKILL.md:199-204` add the sentence: *"Accepted set is exactly `{success}`; `cancelled` and `failure` are named INCOMPLETE (C-HE-19). Do not infer green from the absence of a failure."*
- [ ] **Step 5: GREEN, probe** `just mutation-probe --file tools/arc_metrics.py --lines <the CANCELLED early-return> --test "uv run pytest tools/test_arc_metrics.py::test_ci_state_cancelled_incomplete -q"` → PINNED. **Register manifest row** `Row("C-HE-19/20", "pytest:tools/test_arc_metrics.py::test_ci_state_cancelled_incomplete", "phase0", "local + CI", True)`.
- [ ] **Step 6: Commit** — `git add tools/arc_metrics.py tools/test_arc_metrics.py .claude/skills/ship-pr/SKILL.md tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-08 CI terminal states — CANCELLED is INCOMPLETE (C-HE-19)"`.

---

### U-HE-09: HITL TTL re-surface — 24 h notification, never reclaim

**Scope.** Add `loop_hil_ttl_resurface()` to `loop_lib.sh`: for every pending `DEFERRED-HIL` row older than `HARNESS_HIL_TTL_S` (default 86400), append one `NOTIFY` row naming the item as TTL-expired (once per 24 h window per item). It touches no reservation, lease, or marker.

**Spec linkage.** C-HE-20 §1 (existing HITL queue; `NOTIFY` for informational), §2 (TTL is a notification threshold; MUST NOT reclaim/release/transition); C-HE-09 §5 (`NOTIFY` kind — landed by U-HE-29; until then the row is written with the legacy 3-column shape and U-HE-29's reducer treats it as legacy).

**Files.** Modify `tools/hooks/loop_lib.sh` (new function after `loop_pending_hil_summary`), `tools/hooks/test_loop_lib.sh`, `tools/hooks/session-start.sh` (call it once at start).

**Depends on.** (none). (U-HE-29 later upgrades the row shape.)

- [ ] **Step 1: Failing test** (`tools/hooks/test_loop_lib.sh`, using the file's `loop_now` stub idiom):
```bash
# C-HE-20: TTL re-surfaces, never reclaims
: > "$(loop_status_path)"; loop_activate "ttl test" >/dev/null
loop_now() { echo "2026-08-17T00:00:00Z"; }; loop_defer B-1 "old deferral"
loop_now() { echo "2026-08-18T00:00:01Z"; }
HARNESS_HIL_TTL_S=86400 loop_hil_ttl_resurface
grep -q '| NOTIFY | .*B-1 .*ttl-expired' "$(loop_status_path)" && ok "TTL expiry emits NOTIFY" || bad "no NOTIFY on TTL expiry"
[ "$(loop_skip_set)" = "B-1" ] && ok "TTL does not resolve/reclaim the deferral" || bad "TTL changed skip-set: $(loop_skip_set)"
n_before=$(grep -c '| NOTIFY |' "$(loop_status_path)"); HARNESS_HIL_TTL_S=86400 loop_hil_ttl_resurface
[ "$(grep -c '| NOTIFY |' "$(loop_status_path)")" = "$n_before" ] && ok "second pass within window is idempotent" || bad "duplicate NOTIFY"
```
- [ ] **Step 2: RED** (`loop_hil_ttl_resurface: command not found`).
- [ ] **Step 3: Implement** (append to `loop_lib.sh` after `loop_pending_hil_summary`):
```bash
# C-HE-20: TTL is a NOTIFICATION threshold. Re-surface pending deferrals older than
# HARNESS_HIL_TTL_S (default 24h) as NOTIFY rows -- at most once per TTL window per item.
# This function MUST NOT resolve, reclaim, release, or transition anything (D8).
loop_hil_ttl_resurface() {
  local p ttl now_s; p=$(loop_status_path); [ -f "$p" ] || return 0
  ttl="${HARNESS_HIL_TTL_S:-86400}"; now_s=$(date -u -j -f %Y-%m-%dT%H:%M:%SZ "$(loop_now)" +%s 2>/dev/null || date -u -d "$(loop_now)" +%s)
  awk -F'|' -v now="$now_s" -v ttl="$ttl" '
    function epoch(ts,   c) { gsub(/^[ \t]+|[ \t]+$/, "", ts); c = "date -u -j -f %Y-%m-%dT%H:%M:%SZ " ts " +%s 2>/dev/null || date -u -d " ts " +%s"; c | getline e; close(c); return e }
    { k = $3; gsub(/^[ \t]+|[ \t]+$/, "", k) }
    k == "DEFERRED-HIL" || k == "RESOLVED-HIL" { s = $4; if (s ~ /^[ \t]*lane=/) s = $5; sub(/^[ \t]+/, "", s); split(s, a, /[ \t]/); tok = a[1]
      if (k == "DEFERRED-HIL") { state[tok] = "PENDING"; at[tok] = epoch($2) } else { state[tok] = "RESOLVED" } }
    k == "NOTIFY" { d = $4; if (d ~ /^[ \t]*lane=/) d = $5; sub(/^[ \t]+/, "", d); split(d, b, /[ \t]+/)
      if (b[1] == "ttl-expired") last[b[2]] = epoch($2) }
    END { for (t in state) if (state[t] == "PENDING" && now - at[t] >= ttl && (!(t in last) || now - last[t] >= ttl)) print t }
  ' "$p" 2>/dev/null | while IFS= read -r item; do
    [ -n "$item" ] && loop_log NOTIFY "ttl-expired ${item} — pending > ${ttl}s; re-surfaced, state unchanged"
  done
}
```
(Executor note: the `date` invocation must work on macOS (`-j -f`) and Linux (`-d`); the two-form fallback above is the file's convention for portable date parsing — verify on both with `bash tools/hooks/test_loop_lib.sh` locally and in CI. The reducer is already shape-aware for the structured column U-HE-29 introduces (`$4 ~ /^lane=/` → detail is `$5`) so it keeps keying on the HIL item token after the venue change; U-HE-29's test file re-runs this block against structured rows.)
- [ ] **Step 4:** In `tools/hooks/session-start.sh`, after the existing pending-HIL summary line, add `loop_hil_ttl_resurface`.
- [ ] **Step 5: GREEN, probe.** `just mutation-probe --file tools/hooks/loop_lib.sh --lines <the "state unchanged" contract: replace loop_log NOTIFY with loop_resolve in a scratch mutation is not a line-removal; instead probe by removing the `(!(t in last) || …)` idempotence clause> --test "bash tools/hooks/test_loop_lib.sh"` → PINNED. The spec's stated probe ("add a reclaim-on-TTL → red") is a *positive* mutation the tool cannot express; the second assertion (`loop_skip_set` unchanged) is the witness that a reclaim would turn red — record this in the evidence log. **Register** `Row("C-HE-19/20", "shell:tools/hooks/test_loop_lib.sh", "phase0", "local + CI", True)`.
- [ ] **Step 6: Commit** — `git add tools/hooks/loop_lib.sh tools/hooks/test_loop_lib.sh tools/hooks/session-start.sh tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-09 HITL TTL re-surface as NOTIFY, never reclaim (C-HE-20)"`.

---
# S2 (remainder) — record extension, env overrides, arc_type at open, gate-log sibling

### U-HE-10: `ARC_METRICS_REPO` / `ARC_METRICS_LEDGER` env overrides

**Scope.** `REPO` and `LEDGER` in `arc_metrics.py` become env-overridable, mirroring the existing `ARC_METRICS_QUEUE_DIR` / `ARC_METRICS_MERGED_REF` pattern; production defaults unchanged when unset. This is the prerequisite for every subprocess-based AC#2 probe.

**Spec linkage.** C-HE-05 §1–§3 + Invariants; C-HE-04 verification (AC#2(a) needs it).

**Files.** Modify `tools/arc_metrics.py:44-45`; `tools/test_arc_metrics.py`.

**Depends on.** (none).

- [ ] **Step 1: Failing test**
```python
def test_env_overrides(tmp_path):
    """Two subprocesses with different ARC_METRICS_REPO observe different LEDGER paths and one QUEUE_DIR."""
    import os, subprocess, sys
    q = tmp_path / "queue"
    code = "import arc_metrics as am, json; print(json.dumps({'ledger': str(am.LEDGER), 'queue': str(am.QUEUE_DIR)}))"
    outs = []
    for name in ("wt-a", "wt-b"):
        env = {**os.environ, "ARC_METRICS_REPO": str(tmp_path / name), "ARC_METRICS_QUEUE_DIR": str(q), "PYTHONPATH": "tools"}
        outs.append(json.loads(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout))
    assert outs[0]["ledger"] != outs[1]["ledger"] and outs[0]["queue"] == outs[1]["queue"] == str(q)
    assert outs[0]["ledger"].endswith("wt-a/.harness/arc-metrics.jsonl")


def test_env_override_defaults_unchanged(monkeypatch):
    monkeypatch.delenv("ARC_METRICS_REPO", raising=False); monkeypatch.delenv("ARC_METRICS_LEDGER", raising=False)
    import importlib, arc_metrics as am
    importlib.reload(am)
    assert am.LEDGER == am.REPO / ".harness" / "arc-metrics.jsonl" and am.REPO.name == "arhugula-v2"
```
- [ ] **Step 2: RED**; **Step 3: Implement** (`arc_metrics.py:44-45`):
```python
#: Per-process overrides (C-HE-05). Mirrors ARC_METRICS_QUEUE_DIR / ARC_METRICS_MERGED_REF so
#: two subprocess lanes can hold DIFFERENT worktree ledgers over ONE shared queue. Defaults
#: are the checkout root and its tracked ledger -- production behaviour is unchanged when unset.
REPO = Path(os.environ.get("ARC_METRICS_REPO", Path(__file__).resolve().parent.parent))
LEDGER = Path(os.environ.get("ARC_METRICS_LEDGER", REPO / ".harness" / "arc-metrics.jsonl"))
```
- [ ] **Step 4: GREEN**; register `Row("C-HE-05", "pytest:tools/test_arc_metrics.py::test_env_overrides", "phase0", "local + CI", False)`.
- [ ] **Step 5: Commit** — `git add tools/arc_metrics.py tools/test_arc_metrics.py tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-10 ARC_METRICS_REPO/LEDGER env overrides (C-HE-05)"`.

---

### U-HE-11: Arc-row field extension + null-safe cohort split

**Scope.** Extend `ArcRow` with the C-HE-25 fields; assert `.harness/arc-metrics.jsonl` carries only `record_kind=arc` rows; make the cohort split tolerate `null` on historical rows and group by `concurrent_lanes_at_open`.

**Spec linkage.** C-HE-25 (fields; single arc row per `arc_id`; additive-safe reads `:765-775,810-832`); C-HE-24 §2 (arc rows only here); C-HE-28 §1 (cohort key shape).

**Files.** Modify `tools/arc_metrics.py` (`ArcRow` `:87-131`, `summary` `:807-915`), `tools/test_arc_metrics.py`.

**Depends on.** U-HE-10.

- [ ] **Step 1: Failing tests**
```python
NEW_FIELDS = ["record_kind", "reviewer_identity", "prompt_version", "config_hash", "arc_type_open", "arc_type_close",
              "arc_type_declared_at", "round_outcomes", "head_sha", "base_sha", "lane_id", "concurrent_lanes_at_open",
              "concurrent_lanes_min", "concurrent_lanes_max", "phases"]

def test_arc_row_schema_has_c_he_25_fields():
    row = am.ArcRow(arc_id="pr-1")
    d = asdict(row)
    for f in NEW_FIELDS:
        assert f in d, f
    assert d["record_kind"] == "arc" and d["phases"] == {} and d["round_outcomes"] == {}


def test_ledger_rows_are_all_record_kind_arc():
    for r in am.read_ledger():
        assert r.get("record_kind", "arc") == "arc"


def test_cohort_split_null_safe(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "l.jsonl"
    rows = [{"arc_id": "a", "levers_active": [], "arc_span_s": 60.0, "review_rounds": 1, "round_completeness": "complete"},
            {"arc_id": "b", "levers_active": [], "arc_span_s": 120.0, "review_rounds": 2, "round_completeness": "complete",
             "concurrent_lanes_at_open": 2}]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(am, "LEDGER", ledger)
    assert am.summary(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "concurrent_lanes_at_open=null" in out and "concurrent_lanes_at_open=2" in out
```
- [ ] **Step 2: RED**; **Step 3: Implement** — add to `ArcRow` after `notes`:
```python
    # -- C-HE-25 extension (all additive; historical rows read as null) --
    record_kind: str = "arc"
    reviewer_identity: str | None = None
    prompt_version: str | None = None
    config_hash: str | None = None
    arc_type_open: str | None = None
    arc_type_close: str | None = None
    arc_type_declared_at: str | None = None      # open | close
    round_outcomes: dict[str, dict] = field(default_factory=dict)  # {round_n: {channel, terminal, finding_count}}
    head_sha: str | None = None
    base_sha: str | None = None
    lane_id: str | None = None
    concurrent_lanes_at_open: int | None = None  # derived sensor (C-HE-03 §7); the cohort key
    concurrent_lanes_min: int | None = None
    concurrent_lanes_max: int | None = None
    phases: dict[str, dict] = field(default_factory=dict)  # {phase: {start, end}} (C-HE-27)
```
In `summary()`, after the lever cohorts block, add a second grouping printed as its own section:
```python
    by_lanes: dict[str, list[dict]] = {}
    for r in rows:
        by_lanes.setdefault(f"concurrent_lanes_at_open={r.get('concurrent_lanes_at_open')}", []).append(r)
    for label in sorted(by_lanes):
        print(f"-- LANES [{label}] (n={len(by_lanes[label])}) " + "-" * 20)
```
(`None` renders as `null` via `json.dumps(r.get(...))`; use that instead of `f"{...}"` so the label is `concurrent_lanes_at_open=null`.)
- [ ] **Step 4: GREEN**; add `am.summary` invocation to the ledger schema test list; register `Row("C-HE-23–26", "pytest:tools/test_arc_metrics.py::test_arc_row_schema_has_c_he_25_fields", "phase0", "local + CI", False)`.
- [ ] **Step 5: Commit** — `git add tools/arc_metrics.py tools/test_arc_metrics.py tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-11 arc-row field extension + null-safe lane cohort (C-HE-25)"`.

---

### U-HE-12: `arc_type_open / arc_type_close / arc_type_declared_at` on the single arc row (ledger side of open-time capture)

**Scope.** `queue`/`extract` gain `--arc-type-declared-at {open,close}` and populate `arc_type_open`/`arc_type_close`; a close-time relabel updates the single arc row's `arc_type_close` in place (never a second row). The open-time *capture point* (reservation payload) is U-HE-17/U-HE-21.

**Spec linkage.** C-HE-26 §2 (close-time change updates the single row), §1 (open-time capture is the reservation — deferred to S4b by the S2 hand-off contract: S2-GREEN = schema-present only); C-HE-25 (`arc_type_declared_at ∈ {open, close}`).

**Files.** Modify `tools/arc_metrics.py` (`extract` `:280-392`, `queue_capture` `:418-501`, `main` `:928-968`), `tools/test_arc_metrics.py`.

**Depends on.** U-HE-11.

- [ ] **Step 1: Failing test**
```python
def test_arc_type_at_open(tmp_path, monkeypatch):
    """Close-time relabel: ONE arc row, arc_type_open != arc_type_close, no duplicate arc_id."""
    ledger = tmp_path / "l.jsonl"; monkeypatch.setattr(am, "LEDGER", ledger)
    row = am.ArcRow(arc_id="pr-9", merged_at="2026-08-18T00:00:00Z", merge_sha="x", arc_type_open="inventing",
                    arc_type_declared_at="open")
    am.append(row)
    am.relabel_arc_type_close("pr-9", "applying")
    rows = am.read_ledger()
    assert len(rows) == 1 and rows[0]["arc_type_open"] == "inventing" and rows[0]["arc_type_close"] == "applying"
```
- [ ] **Step 2: RED**; **Step 3: Implement** — add after `append()`:
```python
def relabel_arc_type_close(arc_id: str, arc_type_close: str) -> None:
    """C-HE-26 §2: a close-time relabel updates the SINGLE arc row in place. Never a second row
    (that would trip SPLIT_BRAIN_LEDGER). Rewrite is whole-file atomic (temp + os.replace) and
    touches only this arc's row."""
    if arc_type_close not in ("inventing", "applying"):
        raise AbortError(f"arc_type_close must be inventing|applying, got {arc_type_close!r}")
    rows = read_ledger()
    hits = [r for r in rows if r.get("arc_id") == arc_id]
    if len(hits) != 1:
        raise AbortError(f"{arc_id}: expected exactly one arc row, found {len(hits)}")
    hits[0]["arc_type_close"] = arc_type_close
    tmp = LEDGER.with_name(f".{LEDGER.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    os.replace(tmp, LEDGER)
```
and in `extract()`: set `arc_type_open=args.arc_type if getattr(args, "arc_type_declared_at", "close") == "open" else None`, `arc_type_close=args.arc_type if declared_at == "close" else None`, `arc_type_declared_at=declared_at`. In `main()`: `ex.add_argument("--arc-type-declared-at", choices=["open", "close"], default="close")`, same on `q`; add subcommand `relabel --arc-id --arc-type-close` → `relabel_arc_type_close`.
- [ ] **Step 4: GREEN**; register `Row("C-HE-23–26", "pytest:tools/test_arc_metrics.py::test_arc_type_at_open", "phase0", "local + CI", False)`.
- [ ] **Step 5: Commit** — `feat(he-lanes): U-HE-12 arc_type_open/close on the single arc row (C-HE-26 §2)`.

---

### U-HE-13: `merge-gate` emits `.harness/merge-gate-log.jsonl` first, markdown second; consistency reducer

**Scope.** The merge-gate skill's log-row emission writes the JSONL row (via `finding_record.append_row`) **before** the markdown row; a failed markdown write logs a `warn` finding and leaves the JSONL row; a failed JSONL write fails the gate step. Add the consistency reducer to `lanes-verify`: every markdown row has a JSONL sibling with the same `(pr, head_sha, verdict)`; a JSONL row with neither a markdown sibling nor a matching `warn` finding is its own orphan class, reconciled by re-emitting markdown on the next gate run.

**Spec linkage.** C-HE-23 §2 (sibling; write order; failure semantics; consistency check + orphan class), C-HE-24 §2 (`record_kind` union for this file); C-HE-15 §1 (unrecordable verdict does not count).

**Files.**
- Create: `tools/merge_gate_log.py` (emitter + reducer used by the skill and by `lanes-verify`), `tools/test_merge_gate_log.py`, `.harness/merge-gate-log.jsonl` (tracked, initially empty)
- Modify: `.claude/skills/merge-gate/SKILL.md` (log-row emission step calls `python tools/merge_gate_log.py emit …`), `tools/lanes_verify.py` (`Row("C-HE-23", "just:merge-gate-log-check", "phase0", "local + CI", False)`), `justfile` (`merge-gate-log-check` recipe)

**Interfaces.**
```python
def emit_gate_row(*, pr: int, head_sha: str, base_sha: str, diff_digest: str, lens: str, verdict: str,
                  findings: list[dict], arc_id: str, lane_id: str, round_n: int,
                  md_path: Path = GATE_LOG_MD, jsonl_path: Path = fr.GATE_LOG_JSONL) -> None
def read_md_rows(md_path) -> list[dict]      # parses the existing markdown table: pr, head_sha, verdict, lens
def consistency_report(md_path, jsonl_path) -> dict  # {"missing_jsonl": [...], "orphan_jsonl": [...]}
def reconcile_orphans(md_path, jsonl_path) -> int     # re-emits markdown rows for orphan JSONL rows
```

**Depends on.** U-HE-01.

- [ ] **Step 1: Failing tests** — `tools/test_merge_gate_log.py`:
```python
def test_jsonl_written_before_markdown_and_md_failure_leaves_jsonl(tmp_path, monkeypatch):
    md = tmp_path / "ro" / "log.md"; jl = tmp_path / "log.jsonl"
    # markdown parent missing + unwritable → md write fails; jsonl row must stand + a warn finding appended
    tmp_path.joinpath("ro").mkdir(); tmp_path.joinpath("ro").chmod(0o500)
    try:
        mgl.emit_gate_row(pr=1, head_sha="a"*40, base_sha="b"*40, diff_digest="c"*64, lens="merge-gate-concurrency",
                          verdict="APPROVE", findings=[], arc_id="pr-1", lane_id="h-w-1", round_n=1, md_path=md, jsonl_path=jl)
    finally:
        tmp_path.joinpath("ro").chmod(0o700)
    rows = fr.read_rows(jl)
    kinds = [r["record_kind"] for r in rows]
    assert kinds[0] in ("finding", "no_finding") and any(r["severity"] == "warn" and "markdown" in r["observed_evidence"] for r in rows)


# mutation-probe: swap the two writes so markdown goes first
def test_jsonl_failure_fails_the_gate_step(tmp_path, monkeypatch):
    md = tmp_path / "log.md"; jl = tmp_path / "ro" / "log.jsonl"
    tmp_path.joinpath("ro").mkdir(); tmp_path.joinpath("ro").chmod(0o500)
    try:
        with pytest.raises(mgl.GateLogError):
            mgl.emit_gate_row(pr=1, head_sha="a"*40, base_sha="b"*40, diff_digest="c"*64, lens="merge-gate-concurrency",
                              verdict="APPROVE", findings=[], arc_id="pr-1", lane_id="h-w-1", round_n=1, md_path=md, jsonl_path=jl)
    finally:
        tmp_path.joinpath("ro").chmod(0o700)
    assert not md.exists()


def test_consistency_orphan_class_and_reconcile(tmp_path):
    md = tmp_path / "log.md"; jl = tmp_path / "log.jsonl"
    mgl.emit_gate_row(pr=2, head_sha="a"*40, base_sha="b"*40, diff_digest="c"*64, lens="merge-gate-spec", verdict="BLOCK",
                      findings=[{"severity": "P1", "location": "x", "message": "m"}], arc_id="pr-2", lane_id="h", round_n=1, md_path=md, jsonl_path=jl)
    md.write_text("")  # simulate crash between the two writes
    rep = mgl.consistency_report(md, jl)
    assert rep["orphan_jsonl"] and not rep["missing_jsonl"]
    assert mgl.reconcile_orphans(md, jl) == 1
    assert mgl.consistency_report(md, jl) == {"missing_jsonl": [], "orphan_jsonl": []}
```
- [ ] **Step 2: RED**; **Step 3: Write `tools/merge_gate_log.py`**
```python
#!/usr/bin/env python3
"""C-HE-23 §2: the merge-gate log's structured sibling. JSONL first, markdown second.

The sibling is a machine projection of the same fact written by the same producer in the
same step -- not a second authority. A failed markdown write leaves the JSONL row and logs a
warn finding; a failed JSONL write fails the gate step (a verdict that cannot be recorded does
not count, C-HE-15 §1).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import finding_record as fr

REPO = Path(__file__).resolve().parent.parent
GATE_LOG_MD = REPO / ".harness" / "merge-gate-log.md"
_MD_ROW = re.compile(r"^\|\s*(?P<ts>[^|]+)\|\s*#?(?P<pr>\d+)\s*\|\s*(?P<head>[0-9a-f]{7,40})\s*\|\s*(?P<lens>[^|]+)\|\s*(?P<verdict>APPROVE|BLOCK)\s*\|")


class GateLogError(RuntimeError):
    """The gate step must fail: the machine record could not be written."""


def _rows_for(pr, head_sha, base_sha, diff_digest, lens, verdict, findings, arc_id, lane_id, round_n) -> list[dict]:
    env = dict(ts=fr.now_iso(), arc_id=arc_id, lane_id=lane_id, head_sha=head_sha, base_sha=base_sha,
               diff_digest=diff_digest, round_n=round_n)
    if not findings:
        core = fr.FindingCore(fr.make_finding_id(lens, head_sha, f"pr-{pr}", 0), f"pr-{pr}", f"verdict {verdict}",
                              "merge-gate lens", "info", f"terminal-{verdict.lower()}", "fresh", lens)
        return [fr.make_row(core, fr.Envelope(record_kind="no_finding", **env))]
    out = []
    for n, f in enumerate(findings, 1):
        core = fr.FindingCore(fr.make_finding_id(lens, head_sha, f["location"], n), f["location"], f["message"],
                              "merge-gate lens", f["severity"], f"terminal-{verdict.lower()}", "fresh", lens)
        out.append(fr.make_row(core, fr.Envelope(record_kind="finding", **env)))
    return out


def _md_line(ts, pr, head_sha, lens, verdict, findings) -> str:
    return f"| {ts} | #{pr} | {head_sha[:12]} | {lens} | {verdict} | {len(findings)} finding(s) |\n"


def emit_gate_row(*, pr, head_sha, base_sha, diff_digest, lens, verdict, findings, arc_id, lane_id, round_n,
                  md_path: Path = GATE_LOG_MD, jsonl_path: Path = fr.GATE_LOG_JSONL) -> None:
    rows = _rows_for(pr, head_sha, base_sha, diff_digest, lens, verdict, findings, arc_id, lane_id, round_n)
    try:
        for r in rows:
            fr.append_row(r, jsonl_path)          # (1) machine record FIRST
    except (OSError, fr.RecordError) as exc:
        raise GateLogError(f"gate verdict could not be recorded: {exc}") from exc
    try:
        with md_path.open("a") as fh:               # (2) human view second
            fh.write(_md_line(rows[0]["ts"], pr, head_sha, lens, verdict, findings))
    except OSError as exc:
        warn = fr.FindingCore(fr.make_finding_id("merge_gate_log", head_sha, str(md_path), 0), str(md_path),
                              f"markdown write failed: {exc}", "C-HE-23 §2 markdown sibling", "warn",
                              "transient-retry", "wrapper", "merge_gate_log")
        fr.append_row(fr.make_row(warn, fr.Envelope(record_kind="finding", ts=fr.now_iso(), arc_id=arc_id, lane_id=lane_id,
                                                    head_sha=head_sha, base_sha=base_sha, diff_digest=diff_digest,
                                                    round_n=round_n, cause_attribution="markdown_write_failed")), jsonl_path)


def read_md_rows(md_path: Path = GATE_LOG_MD) -> list[dict]:
    if not md_path.exists():
        return []
    out = []
    for line in md_path.read_text().splitlines():
        m = _MD_ROW.match(line)
        if m:
            out.append({"pr": int(m["pr"]), "head_sha": m["head"], "lens": m["lens"].strip(), "verdict": m["verdict"]})
    return out


def _key(row) -> tuple:
    return (int(row["arc_id"].split("-")[-1]) if row["arc_id"].startswith("pr-") else -1, (row["head_sha"] or "")[:12],
            row["finding_type"].removeprefix("terminal-").upper())


def consistency_report(md_path: Path = GATE_LOG_MD, jsonl_path: Path = fr.GATE_LOG_JSONL) -> dict:
    md_keys = {(r["pr"], r["head_sha"][:12], r["verdict"]) for r in read_md_rows(md_path)}
    jl = [r for r in fr.read_rows(jsonl_path) if r["record_kind"] in ("finding", "no_finding") and r["producer"].startswith("merge-gate")]
    warned = {r["head_sha"] for r in fr.read_rows(jsonl_path) if r["producer"] == "merge_gate_log"}
    jl_keys = {_key(r): r for r in jl}
    missing = [k for k in md_keys if k not in jl_keys]
    orphan = [r for k, r in jl_keys.items() if k not in md_keys and r["head_sha"] not in warned]
    return {"missing_jsonl": missing, "orphan_jsonl": orphan}


def reconcile_orphans(md_path: Path = GATE_LOG_MD, jsonl_path: Path = fr.GATE_LOG_JSONL) -> int:
    n = 0
    for r in consistency_report(md_path, jsonl_path)["orphan_jsonl"]:
        pr = int(r["arc_id"].split("-")[-1])
        with md_path.open("a") as fh:
            fh.write(_md_line(r["ts"], pr, r["head_sha"], r["producer"], r["finding_type"].removeprefix("terminal-").upper(), []))
        n += 1
    return n


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["check"]:
        rep = consistency_report()
        for k in rep["missing_jsonl"]:
            print(f"MISSING-JSONL {k}")
        for r in rep["orphan_jsonl"]:
            print(f"ORPHAN-JSONL {r['finding_id']}")
        return 1 if rep["missing_jsonl"] else 0   # orphans are reconciled next gate run, not a red
    if args[:1] == ["reconcile"]:
        print(f"reconciled {reconcile_orphans()} orphan row(s)"); return 0
    print("usage: merge_gate_log.py check|reconcile", file=sys.stderr); return 2
```
(Executor: `emit` as a CLI subcommand with `--pr --head-sha --base-sha --diff-digest --lens --verdict --findings-json --arc-id --lane-id --round-n` is what the skill calls; add it mirroring the function args. Historical markdown rows predate the JSONL — `consistency_report` MUST only compare rows whose `ts` ≥ the first JSONL row's `ts`; add that filter and a test for it.)
- [ ] **Step 4:** `.claude/skills/merge-gate/SKILL.md` log-row step → *"Emit each lens verdict with `uv run python tools/merge_gate_log.py emit …` (JSONL first, markdown second, C-HE-23 §2). If it exits non-zero the lens verdict does not count — treat as BLOCK-equivalent."* Recipe: `merge-gate-log-check:\n    uv run python tools/merge_gate_log.py check`.
- [ ] **Step 5: GREEN, probe** (`--lines` = the JSONL `for r in rows: fr.append_row` block ordering — verify by the swap test), register rows, commit:
```bash
git add tools/merge_gate_log.py tools/test_merge_gate_log.py .harness/merge-gate-log.jsonl .claude/skills/merge-gate/SKILL.md tools/lanes_verify.py justfile tools/codex-parity-check.sh
git commit -m "feat(he-lanes): U-HE-13 merge-gate JSONL sibling — jsonl-first write order + consistency reducer (C-HE-23 §2)"
```

---

# S3 — Durable store audit

### U-HE-14: Durable store audit one-pager + `tools/test_store_audit.py`

**Scope.** Author `.harness/spec/store-audit-he-loop-lanes.md` listing exactly the eight stores of C-HE-30's table plus the families the clearance fold adds (reservation generation dirs + `.seq` allocator, `transition.<token>` markers + `released.*/reclaimed.*` + `LEASE.<token>.*` sidecars, `QUEUE_DIR/lanes/<k>`, `hil-deliveries/`, `merge-door/attempts/` + `tier-clean-cycles/`, `.harness/mechanized-checks-state.json`, `.harness/mutation-probe-log.jsonl`), each classified `derived` / `part of store N` / `sole carrier (new fact)` (spec v1.3 X3) with one authority per fact; and the static test that greps every `QUEUE_DIR`/`.harness` path literal in the four coordination modules and asserts each is listed. *As landed (PR for U-HE-14): the test also parses the eight-store and family tables (one row per store, authority cells bound to the C-HE-30 facts, Relation cells pinned, no fact owned twice) and the extractor covers pathlib / f-string / chained / call-root joins, `glob`, `with_suffix`, `.tmp` stagers and the shell `$(..)/<name>` idiom; S4 units MUST move their module from `PENDING` to `LANDED` in `tools/test_store_audit.py` when it lands.*

**Spec linkage.** C-HE-30 (table, invariants, verification: `tools/test_store_audit.py` phase0); §7; §11 #1.

**Files.** Create `.harness/spec/store-audit-he-loop-lanes.md`, `tools/test_store_audit.py`. Modify `tools/codex-parity-check.sh`, `tools/lanes_verify.py` (`Row("C-HE-30", "pytest:tools/test_store_audit.py", "phase0", "local + CI", False)`).

**Depends on.** U-HE-01, U-HE-05 (the audit names the sibling + probe log). It MUST land before U-HE-17 and U-HE-22 (spec §6: S3 before S4b/S4c).

- [ ] **Step 1: Failing test** — `tools/test_store_audit.py`:
```python
"""C-HE-30: one authority per fact; every store the coordination modules touch is listed."""
from __future__ import annotations
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / ".harness" / "spec" / "store-audit-he-loop-lanes.md"
MODULES = ["tools/arc_metrics.py", "tools/merge_door.py", "tools/reservations.py", "tools/hooks/loop_lib.sh"]
EIGHT = ["Queue entries", "Reservation files", "Merge-door lease", "arc-metrics.jsonl", "merge-gate-log", "loop_status.md",
         "Finding emission", "Committed history on"]
DERIVED = ["reservations/<arc_id>/<gen>.json", "transition.<lease_token>", "released.", "reclaimed.", "lanes/<k>",
           "tier-clean-cycles", "hil-deliveries", "mechanized-checks-state.json", "mutation-probe-log.jsonl", "merge-gate-log.jsonl"]
_PATH_LIT = re.compile(r'(QUEUE_DIR\s*/\s*"([^"]+)"|\.harness/[A-Za-z0-9_.\-]+|/\.harness/|"(reservations|merge-door|lanes|LEASE|transition\.|released\.|reclaimed\.)[^"]*")')


def test_audit_exists_and_lists_eight_plus_derived():
    text = AUDIT.read_text()
    for name in EIGHT + DERIVED:
        assert name in text, name
    assert text.count("| Authority for |") == 1


def test_every_path_literal_in_modules_is_listed():
    text = AUDIT.read_text()
    for m in MODULES:
        p = REPO / m
        if not p.exists():
            continue  # merge_door / reservations land in S4; the row re-runs then
        for hit in _PATH_LIT.finditer(p.read_text()):
            lit = next(g for g in hit.groups() if g) if any(hit.groups()) else hit.group(0)
            token = lit.strip('"').split("/")[-1].split(".")[0] or lit
            assert token in text, f"{m}: store literal {lit!r} not in audit"
```
- [ ] **Step 2: RED**; **Step 3: Author the audit** — `.harness/spec/store-audit-he-loop-lanes.md`:

```markdown
# Durable store audit — H_E loop + lanes (C-HE-30)

Repo `17011f89c` · 2026-08-18 · owed before C-HE-03 / C-HE-06 code lands (spec §6 S3).
Rule: exactly one authority per fact. Any fact found with two authorities is resolved by demoting one to a derived copy before the corresponding contract is implemented.

## The eight stores

| Store | Venue | Authority for |
|---|---|---|
| Queue entries (`*.json` / `*.taken`) | `QUEUE_DIR` | "capture exists and is not yet in committed history" |
| Reservation files (`reservations/<arc_id>/<gen>.json`, immutable full snapshots; head = highest gen) | `QUEUE_DIR` | arc landing state (`pending/open/terminal`), `concurrent_lanes_at_open` sensor, `arc_type` at open, accreted `phases` |
| Merge-door lease (`merge-door/LEASE`) | `QUEUE_DIR`-adjacent | who is landing now; `merge_attempted_at`; `state ∈ {held, blocked}` |
| `.harness/arc-metrics.jsonl` (per-worktree until committed) | `REPO` | arc rows (`record_kind=arc`, one per `arc_id`), per-round outcomes, phases (folded at drain) |
| `.harness/merge-gate-log.md` + structured sibling `.harness/merge-gate-log.jsonl` | `REPO` | gate verdicts and every finding-class row (human + machine views of ONE fact; the sibling is written by the same producer in the same step — a projection, not a second authority) |
| `loop_status.md` (HIL/NOTIFY rows at the shared venue `QUEUE_DIR/../loop_status.md`; control markers per-lane under `hook_project_dir()`) | shared / per-lane | operator-attention state; run-scoped skip-set |
| Finding emission (`codex_context_guard.Finding` projection) | CI/stdout | derived from the 8-field record — never authored independently |
| Committed history on `MERGED_REF` | git | the only proof that a row is durable |

## Derived families + new-fact carriers (no second authority for an existing fact)

*(v1.0 template headed "Derived families (no new authority)"; re-headed per spec v1.3 X3 — each row carries a `Relation` cell: `derived` / `part of store N` / `sole carrier (new fact)`. The as-landed page is canonical for the classifications; this template is illustrative.)*

| Family | Path | Relation | Derived from / sole carrier of |
|---|---|---|---|
| Reservation generations | `QUEUE_DIR/reservations/<arc_id>/<gen>.json`, `.<gen>.<pid>.tmp` | part of store 2 | the reservation files ARE store 2; head = highest gen, below-head = immutable history (GC prunes below head) |
| Reservation sequence allocator | `QUEUE_DIR/reservations/.seq/<n>` | sole carrier (new fact) | "the highest `seq` ever allocated" (`alloc_seq` exclusive-creates before the generation that carries it is written) |
| Lease transition marker | `QUEUE_DIR/merge-door/transition.<lease_token>` (payload `{pid, host, target_action, created_at}` + `fresh_lease` on reclaim) | sole carrier (new fact) | "who won the transition of this token, toward what — and the fresh lease to publish" (C-HE-06 §6); completion is read from the history below |
| Lease per-token sidecars | `QUEUE_DIR/merge-door/LEASE.<token>.<attempted|blocked|refresh>` | part of store 3 | `read_lease` merges them into the lease view (`merge_attempted_at`, blocked state, refresh continuation) |
| Lease transition history | `QUEUE_DIR/merge-door/released.<token>`, `reclaimed.<token>` | part of store 3 | the renamed-aside `LEASE` payload = the lease's completed-transition record, read by recovery (GC 30 d) |
| Door attempt rate-window | `QUEUE_DIR/merge-door/attempts/<lane_id>/<ts>` | sole carrier (new fact) | "attempts per lane in the window" (C-HE-06 rate limit) |
| Attestation-tier counter | `QUEUE_DIR/merge-door/tier-clean-cycles/<token>` | sole carrier (new fact) | "consecutive clean cycles" (C-HE-06 §10) |
| Lane index registry | `QUEUE_DIR/lanes/<k>` | sole carrier (new fact) | "which lane indices are taken" (lane-init exclusive create; released at teardown) |
| HIL coalescing delivery claims | `QUEUE_DIR/hil-deliveries/<gen-id>` | sole carrier (new fact) | "one deliverer has claimed this generation" (`loop_hil_deliver`; the `COALESCE-DELIVERED` row is the delivery's audit twin) |
| Loop control markers (per-lane) | `.harness/.loop-active`, `.loop-iter`, `.loop-halt` | part of store 6 | pre-existing Wave-2 control state the C-HE-30 row 6 names ("control markers per-lane") |
| Mechanized-check runtime state | `.harness/mechanized-checks-state.json` | sole carrier (new fact) | "each check's live `kind` + demotion window" (C-HE-31 §4d; a promotion is recorded only here) |
| Mutation-probe run log | `.harness/mutation-probe-log.jsonl` | sole carrier (new fact) | "which probe ran, when, against which digests" (probe-run evidence; annotations name which probes are required) |
| Structured gate sibling | `.harness/merge-gate-log.jsonl` | part of store 5 | listed above with its markdown twin (JSONL-first, same step) |

## Two-authority checks performed

- "Is arc X landed?" — reservation state (`merged`) is authoritative for the *state machine*; `gh pr view` is ground truth the reservation is reconciled *from*; the arc row's `merged_at` is a capture. No conflict: reservation ← gh; row ← reservation at drain.
- "Who may append arc X?" — the `open` reservation's holder (`lane_id`), not the queue claim's pid/host (claim = seconds-scale liveness only; the named D2 exception transfers the holder in the same recovery step).
- "Is the door held?" — `LEASE` presence + `state`; `merge_attempted_at` folds into the lease (no third store).
- "Which HIL items are pending?" — the shared `loop_status.md` reduction; per-lane `.loop-active`/ACTIVATE are control markers, not HIL state.
```
- [ ] **Step 4: GREEN**; commit:
```bash
git add .harness/spec/store-audit-he-loop-lanes.md tools/test_store_audit.py tools/codex-parity-check.sh tools/lanes_verify.py
git commit -m "docs(he-lanes): U-HE-14 durable store audit one-pager + phase0 witness (C-HE-30)"
```
**Acceptance.** Test green at HEAD and re-green after U-HE-17/U-HE-22 land (the row re-runs; a new path literal not in the audit fails it — that is the "no runtime path creates a store the audit does not list" invariant).

---
# S4a — Primitive + drain guards + capture durability

### U-HE-15: Drain fault isolation — three FNF guards, per-arc isolation incl. `_claim_arc`, systemic abort, restore-or-republish, `KEPT QUEUED` only after durable restore

**Scope.** Restructure `drain()` around a per-entry `_drain_one()` so an exception while processing one entry — including inside `_claim_arc` — never abandons the rest; guard the three check-then-act `os.replace` sites with the file's `except FileNotFoundError` idiom; distinguish a systemic `OSError` (permission / I/O on `QUEUE_DIR`) which aborts the loop with one message; make every restore "succeed or re-publish from the in-memory capture" (E9/E21) so `KEPT QUEUED`/`kept` only follow a durable restore; add the `ARC_METRICS_TEST_KILL_AFTER=<step>` seam; and make `safe-worktree-remove.sh` (via `hook_worktree_local_state`) refuse to dispose a worktree that has committed-but-unpushed commits (`git rev-list @{u}..HEAD` non-empty), alongside the existing uncommitted-ledger refusal.

**Spec linkage.** C-HE-04 §1 (guards at `:666`, `:746`, `:754`), §3 (fault isolation; systemic vs per-arc), §4 first sentence (re-publish on vanished entry), §7 (`AbortError` branch: restore succeeds or re-publishes; `KEPT QUEUED` only after durably back); §6 (teardown contract: refuse on uncommitted `.harness/arc-metrics.jsonl` — already caught — OR committed-but-unpushed commits — not checked today); C-HE-04 Invariants (no `FileNotFoundError` escapes; two-state invariant); C-HE-02 §4–§5 unchanged doctrine.

**Files.** Modify `tools/arc_metrics.py` (`_recover_dead_claims` `:643-667`, `drain` `:670-762`; add helpers), `tools/test_arc_metrics.py`; **teardown guard (C-HE-04 §6):** `tools/hooks/lib.sh:483-497` (`hook_worktree_local_state` gains the ahead-of-`@{u}` refusal), `tools/hooks/test_lib.sh`.

**Interfaces.**
```python
def _kill_after(step: str) -> None                      # os._exit(137) if ARC_METRICS_TEST_KILL_AFTER == step
def _restore_or_republish(taken: Path, path: Path, entry: dict) -> None
def _is_systemic(exc: OSError) -> bool
def _drain_one(path: Path, entry: dict, arc_id: str, committed: set[str], local: set[str]) -> str  # released|held|outstanding|added
```
Kill-step names (used by U-HE-20): `claim`, `extract`, `append`, `restore`, `restore-abort`.

**Depends on.** U-HE-10 (subprocess tests use the overrides).

- [ ] **Step 1: Failing tests** (`tools/test_arc_metrics.py`; the file's existing `queue`/`drain` fixtures apply):
```python
def _queue_entries(am, tmp_path, monkeypatch, n):
    q = tmp_path / "queue"; q.mkdir(); monkeypatch.setattr(am, "QUEUE_DIR", q)
    monkeypatch.setattr(am, "LEDGER", tmp_path / "l.jsonl")
    for i in range(1, n + 1):
        (q / f"pr-{i}.json").write_text(json.dumps({"pr": i, "arc_id": f"pr-{i}", "arc_type": "inventing", "decisions": 1}))
    return q


# mutation-probe: remove the try/except around _claim_arc inside _drain_one (let the exception escape drain)
def test_drain_fault_isolation(tmp_path, monkeypatch):
    """Entry 1 raises INSIDE _claim_arc; entries 2..n are still processed."""
    q = _queue_entries(am, tmp_path, monkeypatch, 3)
    real_claim = am._claim_arc
    def boom(path, entry):
        if entry["pr"] == 1:
            raise am.AbortError("cannot claim pr-1: injected")
        return real_claim(path, entry)
    monkeypatch.setattr(am, "_claim_arc", boom)
    monkeypatch.setattr(am, "committed_arc_ids", lambda: set())
    monkeypatch.setattr(am, "extract", lambda a: am.ArcRow(arc_id=a.arc_id, merged_at="t", merge_sha="s"))
    rc = am.drain(argparse.Namespace())
    ledger = [r["arc_id"] for r in am.read_ledger()]
    assert ledger == ["pr-2", "pr-3"] and rc == 1  # pr-1 kept; two appended (held pending commit)
    assert (q / "pr-1.json").exists()


def test_drain_systemic_oserror_aborts_once(tmp_path, monkeypatch, capsys):
    _queue_entries(am, tmp_path, monkeypatch, 3)
    def perm(path, entry):
        raise PermissionError(13, "queue dir read-only")
    monkeypatch.setattr(am, "_claim_arc", perm)
    monkeypatch.setattr(am, "committed_arc_ids", lambda: set())
    rc = am.drain(argparse.Namespace())
    err = capsys.readouterr()
    assert rc == 2 and (err.out + err.err).count("systemic") == 1


def test_recover_dead_claims_fnf_guarded(tmp_path, monkeypatch):
    q = tmp_path / "queue"; q.mkdir(); monkeypatch.setattr(am, "QUEUE_DIR", q)
    taken = q / "pr-7.taken"
    taken.write_text(json.dumps({"pr": 7, "_claim": {"pid": 999999, "host": socket.gethostname()}}))
    monkeypatch.setattr(am, "_process_is_alive", lambda pid: False)
    real_replace = os.replace
    def vanish(src, dst):
        Path(src).unlink()            # a peer restored it first
        return real_replace(src, dst)  # raises FileNotFoundError
    monkeypatch.setattr(am.os, "replace", vanish)
    am._recover_dead_claims()          # must NOT raise


# mutation-probe: replace _restore_or_republish's publish_exclusive fallback with a bare os.replace
def test_e9_capture_republish(tmp_path, monkeypatch):
    """A drain that appended must not return with the arc's queue entry absent."""
    q = _queue_entries(am, tmp_path, monkeypatch, 1)
    monkeypatch.setattr(am, "committed_arc_ids", lambda: set())
    def extract_and_steal(a):
        (q / "pr-1.taken").unlink()   # peer removes the winner's .taken between append and restore (E9)
        return am.ArcRow(arc_id="pr-1", merged_at="t", merge_sha="s")
    monkeypatch.setattr(am, "extract", extract_and_steal)
    am.drain(argparse.Namespace())
    assert (q / "pr-1.json").exists(), "entry re-published from the in-memory capture"
    assert [r["arc_id"] for r in am.read_ledger()] == ["pr-1"]


def test_abort_branch_restores_before_kept_queued(tmp_path, monkeypatch, capsys):
    q = _queue_entries(am, tmp_path, monkeypatch, 1)
    monkeypatch.setattr(am, "committed_arc_ids", lambda: set())
    def abort(a):
        (q / "pr-1.taken").unlink()
        raise am.AbortError("no round logs")
    monkeypatch.setattr(am, "extract", abort)
    am.drain(argparse.Namespace())
    assert (q / "pr-1.json").exists() and "KEPT QUEUED" in capsys.readouterr().err
```
- [ ] **Step 2: RED** — the FNF and systemic tests raise/`FileNotFoundError` escapes; E9 test finds no entry.
- [ ] **Step 3: Implement.** Add near `_recover_dead_claims`:
```python
def _kill_after(step: str) -> None:
    """Test seam (C-HE-04 verification (vi)): ARC_METRICS_TEST_KILL_AFTER=<step> exits 137 right after
    the named step -- a real process death, not an exception a `finally` could tidy."""
    if os.environ.get("ARC_METRICS_TEST_KILL_AFTER") == step:
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(137)


def _is_systemic(exc: OSError) -> bool:
    """A queue-dir permission / I/O / disk fault -- not a per-arc content fault, not a lost race."""
    return isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EROFS, errno.EIO, errno.ENOSPC}


def _restore_or_republish(taken: Path, path: Path, entry: dict) -> None:
    """Put the queue entry back DURABLY (C-HE-04 §4/§7, E9/E21).

    The held `.taken` can vanish under us (a peer judged us dead and took over, `_claim_arc:624-626`);
    a bare os.replace then raises and the appended-but-uncommitted row's declarations exist nowhere
    else. Re-publish from the in-memory capture instead. FileExistsError means a peer already put
    the entry back -- the capture is durable either way."""
    try:
        os.replace(taken, path)
        return
    except FileNotFoundError:
        pass
    payload = json.dumps({k: v for k, v in entry.items() if k != "_claim"}, sort_keys=True)
    try:
        publish_exclusive(path, payload)
    except FileExistsError:
        pass
```
Replace `_recover_dead_claims`'s `os.replace(claim, restored)` with:
```python
        try:
            os.replace(claim, restored)
        except FileNotFoundError:
            print(f"  {claim.name}: a peer recovered it first; leaving it")
            continue
        print(f"  recovered claim from a dead owner -> {restored.name}")
```
Extract the per-entry body of `drain()` (`:698-757`) into:
```python
def _drain_one(path: Path, entry: dict, arc_id: str, committed: set[str], local: set[str]) -> str:
    if arc_id in committed:
        print(f"  {arc_id}: in committed ledger, releasing queue entry")
        path.unlink(missing_ok=True)
        return "released"
    if arc_id in local:
        print(f"  {arc_id}: row appended locally, awaiting commit -- entry held")
        return "held"
    taken = _claim_arc(path, entry)
    _kill_after("claim")
    if taken is None:
        print(f"  {arc_id}: claimed by a concurrent drain, still outstanding")
        return "outstanding"
    args = argparse.Namespace(pr=entry["pr"], arc_id=entry.get("arc_id"), arc_type=entry.get("arc_type"),
                              decisions=entry.get("decisions"), round_snapshot=entry.get("round_snapshot"),
                              round_logs=None, levers=entry.get("levers"), notes=entry.get("notes", ""))
    try:
        row = extract(args)
        _kill_after("extract")
        append(row)
        _kill_after("append")
    except AbortError:
        _restore_or_republish(taken, path, entry)   # durable BEFORE we report KEPT QUEUED
        _kill_after("restore-abort")
        raise
    _restore_or_republish(taken, path, entry)
    _kill_after("restore")
    print(f"  {arc_id}: appended (entry held until the row is committed)")
    return "added"
```
and the loop in `drain()` becomes:
```python
    for i, (path, entry) in enumerate(pending):
        arc_id = entry.get("arc_id") or f"pr-{entry['pr']}"
        try:
            outcome = _drain_one(path, entry, arc_id, committed, local)
        except AbortError as exc:
            print(f"  {arc_id}: KEPT QUEUED -- {exc}", file=sys.stderr)
            kept += 1
            continue
        except OSError as exc:
            if _is_systemic(exc):
                remaining = len(pending) - i
                print(f"ABORT: systemic queue fault on {QUEUE_DIR}: {exc}; {remaining} entr(y/ies) not processed", file=sys.stderr)
                return 2
            print(f"  {arc_id}: KEPT QUEUED -- {exc}", file=sys.stderr)
            kept += 1
            continue
        if outcome == "added":
            added += 1
            kept += 1
        elif outcome in ("held", "outstanding"):
            kept += 1
```
(`import errno` at top. `_claim_arc` already converts non-race `OSError` into `AbortError` at `:629-633`; the systemic classification therefore has to happen **before** that conversion — change `_claim_arc`'s `except OSError as exc:` to `if _is_systemic(exc): raise` first, then the existing `raise AbortError(...)`.)
- [ ] **Step 4: GREEN**, probes: fault-isolation (`--lines` = the `except AbortError` clause in the loop) → PINNED; E9 (`--lines` = the `publish_exclusive` fallback in `_restore_or_republish`) → PINNED. Register `Row("C-HE-04", "pytest:tools/test_arc_metrics.py::test_drain_fault_isolation", "phase0", "local + CI", True)` and `Row("C-HE-04", "pytest:tools/test_arc_metrics.py::test_e9_capture_republish", "phase0", "local + CI", True)`.
- [ ] **Step 4b: Teardown guard (C-HE-04 §6) — rev 2026-08-19 (S4a execution correction, spec-exact scope).** Failing test in `tools/hooks/test_lib.sh` (the file's scratch-repo idiom): create a worktree with an upstream, commit locally without pushing → `hook_worktree_local_state <wt>` returns 0 with a residue line `ahead-of-upstream: 1 commit(s)`; push → returns 1 (clean); a worktree with NO upstream keeps today's clean verdict. *The v1.0 body additionally prescribed `no-upstream` as unconditional fail-closed residue; grounded at S4a execution that clause is unimplementable without flipping five existing safe-removal witnesses (test_lib.sh POST_SCAN rc 0 / HELD_CWD rc 7 / OPEN_UNKNOWN rc 9 / POST_IDENTITY rc 10 / the signal-recovery cases — `hook_worktree_local_state` gates removal before each of those paths) and without refusing every local-only scratch worktree in production. Spec C-HE-04 §6's MUST is scoped to `rev-list @{u}..HEAD` non-empty and is implemented exactly; the never-pushed-branch composition (unpushed local branch + a later manual branch delete — worktree disposal itself never deletes the branch, and branch-prune only prunes gh-merged head-refs) is a REGISTERED RESIDUAL of this unit. An upstream that RESOLVES but whose ahead-count fails stays fail-closed residue.* Implement in `lib.sh` `hook_worktree_local_state`, after the porcelain loop and before `[ -n "$residue" ] || return 1`:
```bash
  # C-HE-04 §6: committed-but-unpushed commits are capture a later branch prune
  # could lose -- with an upstream, ahead-of-@{u} is refusal residue. No upstream
  # keeps today's behavior (spec scopes the check to @{u}; residual registered).
  local ahead
  if git -C "$wt" rev-parse --verify -q '@{u}' >/dev/null 2>&1; then
    if ahead=$(git -C "$wt" rev-list --count '@{u}..HEAD' 2>/dev/null); then
      [ "${ahead:-0}" -gt 0 ] && residue="${residue}${residue:+
}ahead-of-upstream: ${ahead} commit(s)"
    else
      residue="${residue}${residue:+
}cannot count ahead-of-upstream (fail-closed)"
    fi
  fi
```
`safe-worktree-remove.sh` already refuses on any residue (rc 4/5 paths) — no change there. Mutation-probe: `just mutation-probe --file tools/hooks/lib.sh --lines <the ahead block> --test "bash tools/hooks/test_lib.sh"` → PINNED (committed-unpushed worktree would be removed). Register `Row("C-HE-04", "shell:tools/hooks/test_lib.sh", "phase0", "local + CI", True)`.

- [ ] **Step 5: Commit** — `git add tools/arc_metrics.py tools/test_arc_metrics.py tools/hooks/lib.sh tools/hooks/test_lib.sh tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-15 drain fault isolation + FNF guards + E9 restore-or-republish + kill seam + teardown ahead-of-upstream guard (C-HE-04 §1/§3/§4/§6/§7)"`.

---

### U-HE-16: C-HE-02 witnesses — dead-owner takeover unit, `flock` grep witness, kill-seam unit

**Scope.** Add the C-HE-02 static and unit witnesses: the scoped `flock|fcntl` grep over the three coordination modules (with the seven pre-existing `tools/hooks/**` users allowlisted by name), the two-simulated-dead-owner takeover unit (exactly one wins the second `publish_exclusive`), and a unit for the kill seam.

**Spec linkage.** C-HE-02 §1 (no locks), §6 (liveness-predicate takeover; mutual exclusion by the second `publish_exclusive`), Invariants (grep witness scoped; allowlist), Verification (static + unit); C-HE-04 verification (vi) seam.

**Files.** Modify `tools/test_arc_metrics.py`, `tools/lanes_verify.py`.

**Depends on.** U-HE-15.

- [ ] **Step 1: Tests**
```python
COORD_MODULES = ["tools/arc_metrics.py", "tools/merge_door.py", "tools/reservations.py"]
LOCK_ALLOWLIST = {"tools/hooks/capture-failure.sh", "tools/hooks/subagent-validate.sh", "tools/hooks/loop-gc.sh", "tools/hooks/lib.sh",
                  "tools/hooks/test_capture_failure.sh", "tools/hooks/test_loop_gc.sh", "tools/hooks/test_lib.sh"}

def test_no_flock_fcntl_in_coordination_modules():
    """C-HE-02 invariant: rg -c 'flock|fcntl' over the three lane-coordination modules -> 0."""
    for m in COORD_MODULES:
        p = REPO / m
        if p.exists():
            assert not re.search(r"flock|fcntl", p.read_text()), m


# mutation-probe: make _claim_owner_is_dead return True for a live pid (unknown → dead)
def test_takeover_token_compare(tmp_path, monkeypatch):
    """Two dead-owner takeovers on one claim: exactly one wins the second publish_exclusive; the loser yields."""
    q = tmp_path / "queue"; q.mkdir(); monkeypatch.setattr(am, "QUEUE_DIR", q)
    entry = {"pr": 5, "arc_id": "pr-5"}
    (q / "pr-5.json").write_text(json.dumps(entry))
    (q / "pr-5.taken").write_text(json.dumps({**entry, "_claim": {"pid": 999999, "host": socket.gethostname()}}))
    monkeypatch.setattr(am, "_process_is_alive", lambda pid: pid == os.getpid())
    wins = [am._claim_arc(q / "pr-5.json", entry), am._claim_arc(q / "pr-5.json", entry)]
    assert sum(w is not None for w in wins) == 1
    assert am._claim_owner_is_dead(q / "pr-5.taken") is False  # the winner (this pid) is alive


def test_kill_after_seam_exits_137(tmp_path):
    code = "import os; os.environ['ARC_METRICS_TEST_KILL_AFTER']='x'; import arc_metrics as am; am._kill_after('x'); print('alive')"
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": "tools"}, capture_output=True, text=True)
    assert r.returncode == 137 and "alive" not in r.stdout
```
- [ ] **Step 2–3:** RED only for the seam test if U-HE-15's `_kill_after` were absent; otherwise these are witnesses that go GREEN immediately — run the takeover probe to confirm it pins (`--lines` = the `not isinstance(pid, int) or host != socket.gethostname()` unknown-is-live guard in `_claim_owner_is_dead` `:596-598`) → PINNED.
- [ ] **Step 4:** Register `Row("C-HE-02", "pytest:tools/test_arc_metrics.py::test_takeover_token_compare", "phase0", "local + CI", True)` and `Row("C-HE-02", "pytest:tools/test_arc_metrics.py::test_no_flock_fcntl_in_coordination_modules", "phase0", "local + CI", False)`.
- [ ] **Step 5: Commit** — `git add tools/test_arc_metrics.py tools/lanes_verify.py && git commit -m "test(he-lanes): U-HE-16 C-HE-02 witnesses — takeover, lock-free grep, kill seam"`.

---
# S4b — Arc reservation (three-state, PR-tagged, generation-versioned)

### U-HE-17: `tools/reservations.py` — generation-CAS record, `alloc_seq`, `reserve`, `transition` with re-validate, `update_payload`, `transfer_holder`, `walk_terminal`, `gc`, `mint_lane_id`

**Scope.** Create the reservation module: one immutable full-snapshot JSON per generation under `QUEUE_DIR/reservations/<arc_id>/<gen>.json`, every mutation an exclusive-create CAS of `<n+1>.json` that re-validates the intended transition against the re-read head on `FileExistsError` (≤ 8), no rename/replace ever, filesystem-derived `seq`, `superseded_by` chain walk (depth 5, cycle raise), `arc_type` required at open, `lane_id` minting, GC of sub-head gens + orphaned tmp files.

**Spec linkage.** C-HE-03 §1 (location, snapshot, CAS, re-validate, GC), §2 (states, `superseded_by`, chain), §3 (payload; `_provenance` not read by the state machine; `:`-free ids; `seq` allocator), §4 (transitions incl. selection-time refusal), §6 (holder rule; named D2 exception → `transfer_holder`), §8 (ordering key); C-HE-26 §1 (`arc_type` at open); C-HE-02 §1–§2 (CAS family; `QUEUE_DIR`-adjacent); C-HE-27 §3 (`phases` map accretes here — `record_phase`).

**Files.** Create `tools/reservations.py`, `tools/test_reservations.py`. Modify `tools/codex-parity-check.sh`, `tools/lanes_verify.py`.

**Interfaces.**
```python
STATES = ("pending", "open", "merged", "abandoned"); TERMINAL = frozenset({"merged", "abandoned"})
LEGAL_TRANSITIONS = frozenset({("pending", "open"), ("open", "merged"), ("open", "abandoned"), ("pending", "abandoned")})
CAS_RETRIES = 8; SEQ_RETRIES = 64; CHAIN_DEPTH_CAP = 5; STALE_AFTER_S = 86400; GC_KEEP_DAYS = 30
ARC_TYPES = ("inventing", "applying")
class ReservationError(RuntimeError); class ReservationHeld(ReservationError); class IllegalTransition(ReservationError); class ChainError(ReservationError)
def reservations_root() -> Path
def now_iso() -> str
def mint_lane_id(worktree: Path) -> str                     # <host-short>-<worktree-basename>-<8-hex>
def alloc_seq() -> int
def current(arc_id: str) -> tuple[int, dict] | None
def reserve(arc_id: str, *, lane_id: str, branch: str, arc_type: str, arc_type_declared_at: str = "open") -> dict
def transition(arc_id: str, to_state: str, *, lane_id: str, updates: dict | None = None, superseded_by: str | None = None) -> dict
def update_payload(arc_id: str, updates: dict) -> dict
def transfer_holder(arc_id: str, *, from_lane_id: str, to_lane_id: str) -> dict
def record_phase(arc_id: str, phase: str, edge: str, ts: str | None = None) -> dict
def holder(arc_id: str) -> str | None
def selectable(arc_id: str) -> bool
def sibling_open_count(exclude_arc_id: str) -> int
def walk_terminal(arc_id: str) -> dict
def gc(*, now: datetime | None = None) -> list[Path]
```

**Depends on.** U-HE-14 (store audit lists this store first), U-HE-15 (`arc_metrics` helpers stable).

- [ ] **Step 1: Failing tests** — `tools/test_reservations.py`:
```python
"""C-HE-03 reservation record: generation CAS, transitions, chain, seq, gc."""
from __future__ import annotations
import json, os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
import reservations as rs


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    q = tmp_path / "queue"; q.mkdir()
    monkeypatch.setattr(rs, "QUEUE_DIR", q)
    return q


def test_reserve_creates_gen1_pending_full_snapshot(qdir):
    p = rs.reserve("pr-1", lane_id="h-wt-1", branch="b1", arc_type="inventing")
    assert p["state"] == "pending" and p["generation"] == 1 and p["arc_type"] == "inventing" and p["arc_type_declared_at"] == "open"
    assert set(p) >= {"arc_id", "generation", "prev_generation", "state", "lane_id", "branch", "pr", "head_sha", "base_sha",
                      "attested_merge_tree", "arc_type", "arc_type_declared_at", "reserved_at", "transitioned_at", "seq",
                      "superseded_by", "concurrent_lanes_at_open", "phases", "_provenance"}
    assert (qdir / "reservations" / "pr-1" / "1.json").exists()
    assert p["_provenance"]["reachable_from_state_machine"] is False


def test_reserve_requires_arc_type(qdir):
    with pytest.raises(rs.ReservationError, match="arc_type"):
        rs.reserve("pr-2", lane_id="h", branch="b", arc_type=None)  # type: ignore[arg-type]


# mutation-probe: drop the pending/open refusal in reserve()
def test_second_lane_selection_refused_while_pending_or_open(qdir):
    rs.reserve("pr-3", lane_id="A", branch="b", arc_type="applying")
    with pytest.raises(rs.ReservationHeld):
        rs.reserve("pr-3", lane_id="B", branch="b2", arc_type="applying")
    rs.transition("pr-3", "open", lane_id="A")
    with pytest.raises(rs.ReservationHeld):
        rs.reserve("pr-3", lane_id="B", branch="b2", arc_type="applying")
    assert rs.selectable("pr-3") is False and rs.selectable("pr-new") is True


def test_transition_is_new_gen_never_rename(qdir):
    rs.reserve("pr-4", lane_id="A", branch="b", arc_type="inventing")
    rs.transition("pr-4", "open", lane_id="A")
    d = qdir / "reservations" / "pr-4"
    assert sorted(p.name for p in d.glob("*.json")) == ["1.json", "2.json"]
    g1 = json.loads((d / "1.json").read_text()); g2 = json.loads((d / "2.json").read_text())
    assert g1["state"] == "pending" and g2["state"] == "open" and g2["prev_generation"] == 1 and g2["seq"] > g1["seq"]


# mutation-probe: drop the re-validation in _cas_next's retry (re-apply the stale payload)
def test_cas_loser_revalidates_and_raises(qdir, monkeypatch):
    """Two writers read gen n (open) with different intents; loser re-validates and RAISES; head stays merged."""
    rs.reserve("pr-5", lane_id="A", branch="b", arc_type="inventing"); rs.transition("pr-5", "open", lane_id="A")
    real_write = rs._write_gen
    fired = {"done": False}
    def racing_write(arc_id, gen, payload):
        if not fired["done"] and payload["state"] == "abandoned":
            fired["done"] = True
            real_write(arc_id, gen, {**payload, "state": "merged", "superseded_by": None})  # the other writer wins first
        return real_write(arc_id, gen, payload)
    monkeypatch.setattr(rs, "_write_gen", racing_write)
    with pytest.raises(rs.IllegalTransition):
        rs.transition("pr-5", "abandoned", lane_id="A", superseded_by="pr-6")
    assert rs.current("pr-5")[1]["state"] == "merged"


# mutation-probe: drop the holder check in transition.build for open->terminal
def test_only_holder_terminalizes_open_reservation(qdir):
    rs.reserve("pr-7b", lane_id="A", branch="b", arc_type="inventing"); rs.transition("pr-7b", "open", lane_id="A")
    with pytest.raises(rs.IllegalTransition, match="requires the holder"):
        rs.transition("pr-7b", "merged", lane_id="B")
    with pytest.raises(rs.IllegalTransition, match="requires the holder"):
        rs.transition("pr-7b", "abandoned", lane_id="B", superseded_by="pr-8")
    assert rs.transition("pr-7b", "merged", lane_id="A")["state"] == "merged"
    rs.reserve("pr-7c", lane_id="A", branch="b", arc_type="inventing")
    assert rs.transition("pr-7c", "abandoned", lane_id="OTHER", superseded_by="pr-9")["state"] == "abandoned"   # pending: any lane


def test_abandoned_requires_superseded_by(qdir):
    rs.reserve("pr-7", lane_id="A", branch="b", arc_type="inventing"); rs.transition("pr-7", "open", lane_id="A")
    with pytest.raises(rs.ReservationError, match="superseded_by"):
        rs.transition("pr-7", "abandoned", lane_id="A")


def test_chain_walk_cap_and_cycle(qdir):
    for i in range(1, 8):
        rs.reserve(f"c-{i}", lane_id="A", branch="b", arc_type="inventing")
    for i in range(1, 6):   # c-1..c-5 abandoned → c-(i+1); c-6 pending (5-hop resolves)
        rs.transition(f"c-{i}", "abandoned", lane_id="A", superseded_by=f"c-{i+1}")
    assert rs.walk_terminal("c-1")["arc_id"] == "c-6"
    rs.transition("c-6", "abandoned", lane_id="A", superseded_by="c-7")   # 6 hops → raises
    with pytest.raises(rs.ChainError, match="depth"):
        rs.walk_terminal("c-1")
    rs.reserve("x", lane_id="A", branch="b", arc_type="inventing"); rs.reserve("y", lane_id="A", branch="b", arc_type="inventing")
    rs.transition("x", "abandoned", lane_id="A", superseded_by="y"); rs.transition("y", "abandoned", lane_id="A", superseded_by="x")
    with pytest.raises(rs.ChainError, match="cycle"):
        rs.walk_terminal("x")


def test_seq_is_filesystem_derived_and_monotonic(qdir):
    a, b, c = rs.alloc_seq(), rs.alloc_seq(), rs.alloc_seq()
    assert a < b < c and (qdir / "reservations" / ".seq" / str(c)).exists()


def test_identifiers_reject_colon(qdir):
    with pytest.raises(rs.ReservationError, match=":"):
        rs.reserve("pr-8", lane_id="bad:lane", branch="b", arc_type="inventing")
    assert ":" not in rs.mint_lane_id(Path("/tmp/wt-x"))


# mutation-probe: replace the PAYLOAD_MUTABLE allowlist check with the old `_STATE_KEYS or lane_id` blocklist
def test_update_and_transition_allowlists(qdir):
    rs.reserve("pr-8b", lane_id="A", branch="b", arc_type="inventing")
    for bad in ({"lane_id": "B"}, {"arc_type": "applying"}, {"arc_type_declared_at": "close"}, {"reserved_at": "x"}, {"superseded_by": "pr-9"}, {"phases": {}}):
        with pytest.raises(rs.ReservationError, match="may not set"):
            rs.update_payload("pr-8b", bad)
    rs.update_payload("pr-8b", {"pr": 8, "head_sha": "h" * 40, "pilot_run_id": "p1"})          # allowed
    with pytest.raises(rs.ReservationError, match="may not set"):
        rs.transition("pr-8b", "open", lane_id="A", updates={"lane_id": "EVIL"})
    assert rs.transition("pr-8b", "open", lane_id="A", updates={"concurrent_lanes_at_open": 0})["lane_id"] == "A"


def test_transfer_holder_only_from_named_lane(qdir):
    rs.reserve("pr-9", lane_id="DEAD", branch="b", arc_type="inventing"); rs.transition("pr-9", "open", lane_id="DEAD")
    rs.transfer_holder("pr-9", from_lane_id="DEAD", to_lane_id="B")
    assert rs.holder("pr-9") == "B"
    with pytest.raises(rs.IllegalTransition):
        rs.transfer_holder("pr-9", from_lane_id="DEAD", to_lane_id="C")   # stale precondition


def test_record_round_outcome_accretes(qdir):
    rs.reserve("pr-10b", lane_id="A", branch="b", arc_type="inventing")
    rs.record_round_outcome("pr-10b", 1, channel="codex", terminal="REVIEWER_UNAVAILABLE", finding_count=0)
    p = rs.record_round_outcome("pr-10b", 2, channel="gemini", terminal="BLOCK", finding_count=3)
    assert p["round_outcomes"] == {"1": {"channel": "codex", "terminal": "REVIEWER_UNAVAILABLE", "finding_count": 0},
                                   "2": {"channel": "gemini", "terminal": "BLOCK", "finding_count": 3}}
    with pytest.raises(rs.ReservationError):
        rs.record_round_outcome("pr-10b", 3, channel="codex", terminal="MAYBE", finding_count=0)


def test_record_phase_accretes(qdir):
    rs.reserve("pr-10", lane_id="A", branch="b", arc_type="inventing")
    rs.record_phase("pr-10", "execute", "start", ts="2026-08-18T00:00:00Z")
    p = rs.record_phase("pr-10", "execute", "end", ts="2026-08-18T00:10:00Z")
    assert p["phases"]["execute"] == {"start": "2026-08-18T00:00:00Z", "end": "2026-08-18T00:10:00Z"}


# mutation-probe: cut off by each file's mtime instead of the terminal head's transitioned_at
def test_gc_prunes_below_head_only_after_terminal_plus_30d_and_sweeps_tmp(qdir, monkeypatch):
    rs.reserve("pr-11", lane_id="A", branch="b", arc_type="inventing"); rs.transition("pr-11", "open", lane_id="A")
    d = qdir / "reservations" / "pr-11"
    old = datetime.now(UTC) - timedelta(days=40)
    for p in d.glob("*.json"):                                          # gens 1-2 are 40 days old...
        os.utime(p, (old.timestamp(), old.timestamp()))
    rs.transition("pr-11", "merged", lane_id="A")                        # ...but terminalization is NOW
    (d / ".2.12345.tmp").write_text("{}"); os.utime(d / ".2.12345.tmp", (old.timestamp(), old.timestamp()))
    removed = rs.gc()
    assert (d / "1.json").exists() and (d / "2.json").exists(), "retention runs from terminalization, not file age"
    assert not (d / ".2.12345.tmp").exists()
    assert rs.gc(now=datetime.now(UTC) + timedelta(days=31)) and not (d / "1.json").exists() and (d / "3.json").exists()
    assert rs.current("pr-11")[1]["state"] == "merged"
```
- [ ] **Step 2: RED** — `ModuleNotFoundError: reservations`.
- [ ] **Step 3: Write `tools/reservations.py`**
```python
#!/usr/bin/env python3
"""Arc reservation record (C-HE-03): three-state, PR-tagged, generation-versioned.

One reservation per arc_id at QUEUE_DIR/reservations/<arc_id>/<gen>.json. Each file is an
IMMUTABLE FULL SNAPSHOT created by exclusive create; the current record is the highest gen.
Every mutation is one CAS: read head n -> build the complete new payload -> exclusive-create
<n+1>.json. Losing the CAS means re-read, RE-VALIDATE the intended transition against the new
head's state, then retry (<= 8). There is no rename or replace on reservation records, ever.

States: pending -> open -> {merged | abandoned}. No tier reclaims on elapsed time (D8):
staleness is reconciled from ground truth or escalated to a human (see reconcile(), U-HE-18).
`_provenance.pid/host` MUST NOT be read by any state-machine decision -- the reservation spans
an hours-long handoff; liveness and validity are decoupled -- with the single named exception
of dead-claim recovery transfer (transfer_holder, C-HE-03 §6).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arc_metrics import QUEUE_DIR, REPO, AbortError, _process_is_alive, publish_exclusive

STATES = ("pending", "open", "merged", "abandoned")
TERMINAL = frozenset({"merged", "abandoned"})
LEGAL_TRANSITIONS = frozenset({("pending", "open"), ("open", "merged"), ("open", "abandoned"), ("pending", "abandoned")})
CAS_RETRIES = 8
SEQ_RETRIES = 64
CHAIN_DEPTH_CAP = 5
STALE_AFTER_S = 24 * 3600
GC_KEEP_DAYS = 30
ARC_TYPES = ("inventing", "applying")
PHASES = ("queue", "execute", "capture", "absorb", "edit", "verify",
          "result_capture_process_exit", "result_capture_log_write", "verify_unavailable")
_STATE_KEYS = frozenset({"state", "generation", "prev_generation", "seq", "arc_id", "_provenance"})
#: The ONLY payload fields a caller may set after `reserve()` (Codex round-1 P1): back-fills + sensors + pilot tag.
#: `lane_id` moves only via transition(pending->open) or transfer_holder(); `arc_type`/`arc_type_declared_at`/
#: `reserved_at`/`superseded_by`/`phases`/`round_outcomes` have their own dedicated writers.
PAYLOAD_MUTABLE = frozenset({"pr", "head_sha", "base_sha", "attested_merge_tree", "merge_sha", "concurrent_lanes_min",
                             "concurrent_lanes_max", "pilot_run_id"})
TRANSITION_MUTABLE = PAYLOAD_MUTABLE | {"concurrent_lanes_at_open"}


class ReservationError(RuntimeError):
    """Named, fail-closed. Never swallowed."""


class ReservationHeld(ReservationError):
    """A pending/open reservation exists: a second lane's selection MUST fail (C-HE-03 §4)."""


class IllegalTransition(ReservationError):
    """The intended transition is not legal from the (re-read) head state."""


class ChainError(ReservationError):
    """superseded_by chain cycle or depth > CHAIN_DEPTH_CAP."""


class LoopStatusWriteError(ReservationError):
    """The shared loop_status.md could not be written -- an operator recovery signal would be lost."""


def reservations_root() -> Path:
    return QUEUE_DIR / "reservations"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_id(name: str, value: str | None) -> None:
    if value is not None and ":" in value:
        raise ReservationError(f"{name} must not contain ':' (finding_id/code delimiter): {value!r}")


def mint_lane_id(worktree: Path) -> str:
    host = socket.gethostname().split(".")[0].replace(":", "-")
    return f"{host}-{worktree.name}-{secrets.token_hex(4)}".replace(":", "-")


def _dir(arc_id: str) -> Path:
    if "/" in arc_id or arc_id in ("", ".", ".."):
        raise ReservationError(f"bad arc_id {arc_id!r}")
    return reservations_root() / arc_id


def alloc_seq() -> int:
    """Filesystem-derived monotonic counter (never date-sourced, C-HE-03 §3)."""
    d = reservations_root() / ".seq"
    d.mkdir(parents=True, exist_ok=True)
    for _ in range(SEQ_RETRIES):
        existing = [int(p.name) for p in d.iterdir() if p.name.isdigit()]
        n = (max(existing) if existing else 0) + 1
        try:
            publish_exclusive(d / str(n), "")
            return n
        except FileExistsError:
            continue
    raise ReservationError(f"seq allocation lost {SEQ_RETRIES} races")


def current(arc_id: str) -> tuple[int, dict] | None:
    d = _dir(arc_id)
    if not d.is_dir():
        return None
    gens = sorted((int(p.stem) for p in d.glob("*.json") if p.stem.isdigit()), reverse=True)
    if not gens:
        return None
    head = gens[0]
    return head, json.loads((d / f"{head}.json").read_text())


def _provenance() -> dict:
    return {"pid": os.getpid(), "host": socket.gethostname(), "reachable_from_state_machine": False}


def _write_gen(arc_id: str, gen: int, payload: dict) -> None:
    d = _dir(arc_id)
    d.mkdir(parents=True, exist_ok=True)
    publish_exclusive(d / f"{gen}.json", json.dumps(payload, sort_keys=True))


def reserve(arc_id: str, *, lane_id: str, branch: str, arc_type: str, arc_type_declared_at: str = "open") -> dict:
    """(none) -> pending at arc OPEN. Refuses if a pending/open reservation exists (selection-time fence)."""
    _check_id("lane_id", lane_id)
    if arc_type not in ARC_TYPES:
        raise ReservationError(f"arc_type is required at open and must be one of {ARC_TYPES} (C-HE-26 §1); got {arc_type!r}")
    cur = current(arc_id)
    if cur is not None:
        state = cur[1]["state"]
        if state in ("pending", "open"):
            raise ReservationHeld(f"{arc_id}: reservation is {state} (held by {cur[1]['lane_id']}) -- selection refused")
        raise ReservationError(f"{arc_id}: reservation already terminal ({state}); arc_id reuse is not a path")
    ts = now_iso()
    payload = {
        "arc_id": arc_id, "generation": 1, "prev_generation": None, "state": "pending", "lane_id": lane_id,
        "branch": branch, "pr": None, "head_sha": None, "base_sha": None, "attested_merge_tree": None,
        "arc_type": arc_type, "arc_type_declared_at": arc_type_declared_at, "reserved_at": ts, "transitioned_at": ts,
        "seq": alloc_seq(), "superseded_by": None, "concurrent_lanes_at_open": None, "phases": {}, "round_outcomes": {}, "merge_sha": None,
        "_provenance": _provenance(),
    }
    try:
        _write_gen(arc_id, 1, payload)
    except FileExistsError as exc:
        raise ReservationHeld(f"{arc_id}: another lane reserved it first") from exc
    return payload


def _cas_next(arc_id: str, build: Callable[[dict], dict]) -> dict:
    """Read head -> build complete new payload -> exclusive-create <n+1>. Loser re-reads and re-validates."""
    for _ in range(CAS_RETRIES):
        cur = current(arc_id)
        if cur is None:
            raise ReservationError(f"{arc_id}: no reservation")
        gen, head = cur
        new = build(dict(head))          # MUST re-validate against THIS head; raises if now illegal
        new.update(generation=gen + 1, prev_generation=gen, seq=alloc_seq(), _provenance=_provenance())
        try:
            _write_gen(arc_id, gen + 1, new)
            return new
        except FileExistsError:
            continue                     # lost the CAS: loop re-reads the new head and re-validates
    raise ReservationError(f"{arc_id}: CAS lost {CAS_RETRIES} times")


def transition(arc_id: str, to_state: str, *, lane_id: str, updates: dict | None = None,
               superseded_by: str | None = None) -> dict:
    _check_id("lane_id", lane_id)
    if to_state == "abandoned" and not superseded_by:
        raise ReservationError("superseded_by is MANDATORY on abandoned (C-HE-03 §2)")

    def build(head: dict) -> dict:
        if (head["state"], to_state) not in LEGAL_TRANSITIONS:
            raise IllegalTransition(f"{arc_id}: {head['state']}->{to_state} is illegal from head gen {head['generation']}")
        if head["state"] == "open" and head["lane_id"] != lane_id:
            # holder-only: an `open` reservation is terminalized only by the lane that holds it (C-HE-03 §6;
            # Codex round-3 P1). pending->abandoned by a superseding arc has no holder yet and stays open to any lane.
            raise IllegalTransition(f"{arc_id}: {head['state']}->{to_state} requires the holder ({head['lane_id']}), not {lane_id}")
        head["state"] = to_state
        head["transitioned_at"] = now_iso()
        head["superseded_by"] = superseded_by or head.get("superseded_by")
        if to_state == "open":
            head["lane_id"] = lane_id     # the holder = the draining lane
        if updates:
            bad = set(updates) - TRANSITION_MUTABLE
            if bad:
                raise ReservationError(f"transition() may not set {sorted(bad)}; allowed: {sorted(TRANSITION_MUTABLE)}")
            head.update(updates)
        return head

    return _cas_next(arc_id, build)


def update_payload(arc_id: str, updates: dict) -> dict:
    """Payload-only CAS restricted to PAYLOAD_MUTABLE (pr / head_sha / base_sha / attested_merge_tree /
    concurrent_lanes_min|max / pilot_run_id). Never a state change, never the holder, never the open-time labels."""
    def build(head: dict) -> dict:
        bad = set(updates) - PAYLOAD_MUTABLE
        if bad:
            raise ReservationError(f"update_payload may not set {sorted(bad)}; allowed: {sorted(PAYLOAD_MUTABLE)}")
        head.update(updates)
        return head
    return _cas_next(arc_id, build)


def transfer_holder(arc_id: str, *, from_lane_id: str, to_lane_id: str) -> dict:
    """The NAMED D2 exception (C-HE-03 §6): dead-claim recovery transfers an `open` reservation's holder
    to the recovering lane in the same recovery step. Precondition re-validated on the head."""
    _check_id("lane_id", to_lane_id)

    def build(head: dict) -> dict:
        if head["state"] != "open" or head["lane_id"] != from_lane_id:
            raise IllegalTransition(f"{arc_id}: transfer precondition stale (state={head['state']}, holder={head['lane_id']})")
        head["lane_id"] = to_lane_id
        head["transitioned_at"] = now_iso()
        return head
    return _cas_next(arc_id, build)


def record_phase(arc_id: str, phase: str, edge: str, ts: str | None = None) -> dict:
    if phase not in PHASES or edge not in ("start", "end"):
        raise ReservationError(f"bad phase/edge {phase!r}/{edge!r}")

    def build(head: dict) -> dict:
        head.setdefault("phases", {}).setdefault(phase, {})[edge] = ts or now_iso()
        return head
    return _cas_next(arc_id, build)


def record_round_outcome(arc_id: str, round_n: int, *, channel: str, terminal: str, finding_count: int) -> dict:
    """C-HE-25 per-round terminal outcome, accreted on the reservation during the open window (like phases)
    and folded into the arc row at drain. `terminal` MUST be one of the C-HE-16 §3 triple."""
    if terminal not in ("APPROVE", "BLOCK", "REVIEWER_UNAVAILABLE"):
        raise ReservationError(f"terminal must be APPROVE|BLOCK|REVIEWER_UNAVAILABLE, got {terminal!r}")

    def build(head: dict) -> dict:
        head.setdefault("round_outcomes", {})[str(int(round_n))] = {"channel": channel, "terminal": terminal, "finding_count": int(finding_count)}
        return head
    return _cas_next(arc_id, build)


def holder(arc_id: str) -> str | None:
    cur = current(arc_id)
    return cur[1]["lane_id"] if cur and cur[1]["state"] == "open" else None


def selectable(arc_id: str) -> bool:
    cur = current(arc_id)
    return cur is None


def sibling_open_count(exclude_arc_id: str) -> int:
    root = reservations_root()
    if not root.is_dir():
        return 0
    n = 0
    for d in root.iterdir():
        if d.name.startswith(".") or d.name == exclude_arc_id:
            continue
        cur = current(d.name)
        if cur and cur[1]["state"] == "open":
            n += 1
    return n


def walk_terminal(arc_id: str) -> dict:
    """Follow superseded_by reservation-to-reservation. Repeated arc_id -> cycle (raise); depth cap 5."""
    seen: set[str] = set()
    depth = 0
    while True:
        if arc_id in seen:
            raise ChainError(f"superseded_by cycle at {arc_id}")
        seen.add(arc_id)
        cur = current(arc_id)
        if cur is None:
            raise ChainError(f"chain points at missing reservation {arc_id}")
        head = cur[1]
        if head["state"] != "abandoned":
            return head
        depth += 1
        if depth > CHAIN_DEPTH_CAP:
            raise ChainError(f"superseded_by depth > {CHAIN_DEPTH_CAP}")
        arc_id = head["superseded_by"]


def gc(*, now: datetime | None = None) -> list[Path]:
    """Prune gens strictly below the head older than terminal + 30 d; sweep orphaned .<gen>.<pid>.tmp
    (pid dead on this host and > 1 h old). The head is NEVER pruned."""
    now = now or datetime.now(UTC)
    removed: list[Path] = []
    root = reservations_root()
    if not root.is_dir():
        return removed
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        cur = current(d.name)
        if cur is None:
            continue
        head_gen, head = cur
        # Retention = terminalization + 30 d (C-HE-03 §1): derived from the TERMINAL head's transitioned_at,
        # never from each historical file's own age (a long-open arc that terminalizes today keeps its history).
        if head["state"] in TERMINAL:
            terminal_at = datetime.strptime(head["transitioned_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            if now - terminal_at > timedelta(days=GC_KEEP_DAYS):
                for p in d.glob("*.json"):
                    if p.stem.isdigit() and int(p.stem) < head_gen:
                        p.unlink(); removed.append(p)
        for tmp in d.glob(".*.tmp"):
            parts = tmp.name.split(".")
            pid = int(parts[-2]) if len(parts) >= 3 and parts[-2].isdigit() else None
            old = datetime.fromtimestamp(tmp.stat().st_mtime, UTC) < now - timedelta(hours=1)
            if old and (pid is None or not _process_is_alive(pid)):
                tmp.unlink(); removed.append(tmp)
    return removed


def emit_loop_row(kind: str, lane_id: str, cause: str, detail: str) -> None:
    """Append a structured row to the SHARED loop_status.md through loop_lib.sh -- one writer of the
    ledger format (C-HE-09 §3, U-HE-29 `loop_log_structured`). RAISES LoopStatusWriteError on write failure."""
    script = 'source tools/hooks/lib.sh; source tools/hooks/loop_lib.sh; loop_log_structured "$1" "$2" "$3" "$4"'
    try:
        proc = subprocess.run(["bash", "-c", script, "_", kind, lane_id, cause, detail], cwd=REPO, check=False, timeout=10, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoopStatusWriteError(f"loop row not written ({exc})") from exc
    if proc.returncode != 0:
        # An unrecorded DEFERRED-HIL / NOTIFY is a lost operator signal: propagate (Codex round-3 P2). Callers that
        # hold durable state elsewhere (a blocked lease sidecar) still surface this as a hard error to stderr + exit.
        raise LoopStatusWriteError(f"loop row not written: {proc.stderr.strip() or 'loop_log_structured failed'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reservations", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reserve"); r.add_argument("--arc-id", required=True); r.add_argument("--lane-id", required=True)
    r.add_argument("--branch", required=True); r.add_argument("--arc-type", choices=ARC_TYPES, required=True)
    t = sub.add_parser("transition"); t.add_argument("--arc-id", required=True); t.add_argument("--to", choices=STATES, required=True)
    t.add_argument("--lane-id", required=True); t.add_argument("--superseded-by"); t.add_argument("--set", nargs="*", default=[])
    u = sub.add_parser("update"); u.add_argument("--arc-id", required=True); u.add_argument("--set", nargs="+", required=True)
    ph = sub.add_parser("phase"); ph.add_argument("--arc-id", required=True); ph.add_argument("--phase", choices=PHASES, required=True)
    ph.add_argument("--edge", choices=["start", "end"], required=True)
    ro = sub.add_parser("round"); ro.add_argument("--arc-id", required=True); ro.add_argument("--round", type=int, required=True)
    ro.add_argument("--channel", required=True); ro.add_argument("--terminal", choices=["APPROVE", "BLOCK", "REVIEWER_UNAVAILABLE"], required=True)
    ro.add_argument("--findings", type=int, default=0)
    s = sub.add_parser("show"); s.add_argument("--arc-id", required=True)
    h = sub.add_parser("holder"); h.add_argument("--arc-id", required=True)
    se = sub.add_parser("selectable"); se.add_argument("--arc-id", required=True)
    sub.add_parser("gc")
    ml = sub.add_parser("mint-lane-id"); ml.add_argument("--worktree", default=".")
    args = p.parse_args(argv)
    kv = lambda items: {k: (json.loads(v) if v[:1] in '0123456789{["n' else v) for k, v in (i.split("=", 1) for i in items)}
    try:
        if args.cmd == "reserve":
            out = reserve(args.arc_id, lane_id=args.lane_id, branch=args.branch, arc_type=args.arc_type)
        elif args.cmd == "transition":
            out = transition(args.arc_id, args.to, lane_id=args.lane_id, updates=kv(args.set) or None, superseded_by=args.superseded_by)
        elif args.cmd == "update":
            out = update_payload(args.arc_id, kv(args.set))
        elif args.cmd == "phase":
            out = record_phase(args.arc_id, args.phase, args.edge)
        elif args.cmd == "round":
            out = record_round_outcome(args.arc_id, args.round, channel=args.channel, terminal=args.terminal, finding_count=args.findings)
        elif args.cmd == "show":
            cur = current(args.arc_id); out = cur[1] if cur else None
        elif args.cmd == "holder":
            print(holder(args.arc_id) or ""); return 0
        elif args.cmd == "selectable":
            return 0 if selectable(args.arc_id) else 1
        elif args.cmd == "gc":
            out = [str(x) for x in gc()]
        else:
            print(mint_lane_id(Path(args.worktree).resolve())); return 0
        print(json.dumps(out, sort_keys=True))
        return 0
    except ReservationError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```
- [ ] **Step 4: GREEN**; probes: selection refusal (`--lines` = the `if state in ("pending","open"): raise ReservationHeld` block) and CAS re-validate (`--lines` = the `if (head["state"], to_state) not in LEGAL_TRANSITIONS: raise` inside `transition.build`) → both PINNED. Register `Row("C-HE-03", "pytest:tools/test_reservations.py", "phase0", "local + CI", True)`.
- [ ] **Step 5: Store-audit re-run** — `uv run pytest tools/test_store_audit.py -q` must stay green (every path literal is listed). **Commit**:
```bash
git add tools/reservations.py tools/test_reservations.py tools/codex-parity-check.sh tools/lanes_verify.py
git commit -m "feat(he-lanes): U-HE-17 generation-CAS arc reservation record (C-HE-03 §1-4/§6/§8, C-HE-26 §1)"
```

---

### U-HE-18: Reservation ground truth + staleness (`reconcile`), selection refusal wiring, `concurrent_lanes_at_open` sensor

**Scope.** Add `reconcile(arc_id, *, gh_view, superseded_by=None, now=None)` — `open` + MERGED → `merged`; CLOSED with pointer → `abandoned`; stuck > 24 h (PR OPEN or `pr` null) → `NOTIFY` + `DEFERRED-HIL`, state unchanged; `pending` aged > 24 h → `NOTIFY` + HITL, state unchanged; `gh` failure → "still open, not reclaimable"; add `open_with_sensor(arc_id, lane_id)` that flips `pending→open` recording `concurrent_lanes_at_open`.

**Spec linkage.** C-HE-03 §5 (ground truth; HITL never TTL), §7 (sensor, `derived`), §4 (`open→merged` on confirmed merge); C-HE-20 §1 (HITL queue via `DEFERRED-HIL`; `NOTIFY` for informational), §2 (never reclaim); C-HE-09 §5 (`NOTIFY` kind — U-HE-29).

**Files.** Modify `tools/reservations.py`, `tools/test_reservations.py`, `tools/lanes_verify.py`.

**Depends on.** U-HE-17, U-HE-29 (structured `NOTIFY` row shape; tests monkeypatch `emit_loop_row`).

**Rev 2026-08-20 (U-HE-18 execution corrections, as-built).** *(i) Superseder-must-exist:* the Step-1 literal test abandons `pr-22` with `superseded_by="pr-23"` never reserved; U-HE-17's landed round-6 validation (`transition.build` raises on a missing superseding reservation, C-HE-03 §2 chain resolvability) rejects that — the as-built test reserves `pr-23` first. *(ii) Probe substitution:* the Step-1 annotation ("add a reclaim-on-age") and Step 4's discussion name a positive mutation the deletion-only probe tool cannot express; the as-built deletion-expressible pins are: ground-truth test → drop the pending-aged `NOTIFY`/`DEFERRED-HIL` emission (rows assertions red); `test_ttl_never_reclaims` → drop `reconcile()`'s final stuck-open `return "open"` (terminus pinned); sensor test → drop the `sibling_open_count` snapshot line. *(iii) Hook path:* the session-start caller is `tools/roadmap-audit/session-start.sh` (Step 5's `tools/hooks/session-start.sh` does not exist); the pass is DETACHED (codex r1 P1: codex-session-start wraps the audit in an 8 s `hook_bounded` slice — an inline gh-backed pass could starve the audit emit), ACTIVATION-GATED on `loop_log_structured` existing in `loop_lib.sh` (codex r5 P2: dormant until U-HE-29 lands its writer, then self-activates — no emitter-less unattended pass), pre-probed on the reservations dir mirroring `arc_metrics.QUEUE_DIR`'s default, with durable outcomes surfacing via the loop ledger at the next engagement (pre-U-HE-29: the store-owned O_NOFOLLOW `.reconcile.log` — codex r2 P1 — surfaced as a `resv=ERR` context token by the NEXT session-start; loop-ledger rows take over at U-HE-29; the log is an operational pass record, NOT an escalation store — C-HE-20 §1 stands: escalation rows belong to the existing loop ledger, and until U-HE-29 lands its writer, emit_loop_row fails CLOSED per U-HE-17's landed contract, loud + in-band + exit 2). *(iv) Fault isolation:* `reconcile_all` isolates per-arc `ReservationError`s in-band (`ERROR: ...` value; CLI exit 2) so one arc's fail-closed `emit_loop_row` (loud until U-HE-29 lands `loop_log_structured`) cannot abandon the remaining pass; the merge-lane call lands with U-HE-22. *(v) Registered residuals (codex r4):* an unsupervised detached-launch failure (uv/import crash before the log write) leaves the prior log standing un-aged — supervising it inline would reintroduce the r1 budget P1, so the durable closure is U-HE-29's ledger rows + U-HE-22's synchronous merge-lane caller; and the plan carries an INTERNAL ordering contradiction this rev names rather than absorbs (codex r11): the §1 'Unit index (topological order)' + §3 deps table serialize U-HE-18 (S4b, #18) before U-HE-29 (S4d, #29) — the order #1405 executed (U-HE-17's landed emit_loop_row docstring: 'the first callers arrive with U-HE-18/U-HE-29') and the order the post-#1405 roadmap next-action prescribes — while the §0 ordering-rationale note item 2 (line ~52) says U-HE-29 'precedes S4b/S4c' so emitters have a target. The landed activation gate + fail-closed emitter make the executed order safe under either reading (no emitter-less unattended pass can fire); the note-vs-index contradiction itself is REGISTERED for reconciliation in the U-HE-29 body — full C-HE-20 row durability arrives with U-HE-29, not by re-sequencing; and (codex r7) RESOLVED-HIL auto-minting when a deferred-stale reservation later terminalizes cleanly belongs to U-HE-29's ledger-writer semantics (the DEFERRED/RESOLVED pairing is the ledger contract's, and pre-U-HE-29 reconcile's emits fail closed anyway) — U-HE-29 MUST also witness the session-start activation gate going live; and (merge-gate r1 concurrency P3) the CLOSED-without-pointer branch emits DEFERRED-HIL unconditionally per pass — spec-literal (C-HE-03 §5 Verification names no aged gate) and inert pre-U-HE-29; per-item dedup of repeated deferral rows is the ledger writer's keyed state machine (U-HE-29), not an aged-gate this unit may add without changing §5 semantics.*

- [ ] **Step 1: Failing tests**
```python
def _gh_raises(pr):
    raise RuntimeError("gh transient")

# mutation-probe: add `if aged: transition(..., "abandoned", ...)` to the pending-aged branch (reclaim on age)
def test_reservation_ground_truth(qdir, monkeypatch):
    rows = []
    monkeypatch.setattr(rs, "emit_loop_row", lambda k, l, c, d: rows.append((k, c, d)))
    rs.reserve("pr-20", lane_id="A", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-20", "A")
    rs.update_payload("pr-20", {"pr": 20})
    assert rs.reconcile("pr-20", gh_view=_gh_raises) == "open"                              # fail safe: not reclaimable
    assert rs.reconcile("pr-20", gh_view=lambda pr: {"state": "MERGED"}) == "merged"
    assert rs.reconcile("pr-20", gh_view=lambda pr: {"state": "MERGED"}) == "merged"        # idempotent, no second transition
    rs.reserve("pr-21", lane_id="A", branch="b", arc_type="inventing")
    later = datetime.now(UTC) + timedelta(hours=25)
    assert rs.reconcile("pr-21", gh_view=lambda pr: {"state": "OPEN"}, now=later) == "pending"   # aged; state unchanged
    assert any(k == "NOTIFY" for k, _, _ in rows) and any(k == "DEFERRED-HIL" for k, _, _ in rows)
    rs.reserve("pr-22", lane_id="A", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-22", "A"); rs.update_payload("pr-22", {"pr": 22})
    assert rs.reconcile("pr-22", gh_view=lambda pr: {"state": "CLOSED"}) == "open"           # closed, no pointer → HITL, unchanged
    assert rs.reconcile("pr-22", gh_view=lambda pr: {"state": "CLOSED"}, superseded_by="pr-23") == "abandoned"


def test_concurrent_lanes_at_open_sensor(qdir):
    for i, lane in enumerate(("A", "B", "C")):
        rs.reserve(f"s-{i}", lane_id=lane, branch="b", arc_type="inventing")
    rs.open_with_sensor("s-0", "A"); rs.open_with_sensor("s-1", "B")
    p = rs.open_with_sensor("s-2", "C")
    assert p["concurrent_lanes_at_open"] == 2 and rs.current("s-0")[1]["concurrent_lanes_at_open"] == 0
```
- [ ] **Step 2: RED**; **Step 3: Implement** (append to `reservations.py`):
```python
def open_with_sensor(arc_id: str, lane_id: str) -> dict:
    """pending -> open at drain start, recording the best-effort sibling-open snapshot (C-HE-03 §7, `derived`)."""
    n = sibling_open_count(arc_id)
    return transition(arc_id, "open", lane_id=lane_id, updates={"concurrent_lanes_at_open": n})


def _aged(ts: str, now: datetime) -> bool:
    return (now - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)).total_seconds() > STALE_AFTER_S


def reconcile(arc_id: str, *, gh_view: Callable[[int], dict], superseded_by: str | None = None,
              now: datetime | None = None) -> str:
    """Staleness by GROUND TRUTH -- HITL, never TTL (C-HE-03 §5, D8). Returns the head state after the pass."""
    now = now or datetime.now(UTC)
    cur = current(arc_id)
    if cur is None:
        raise ReservationError(f"{arc_id}: no reservation")
    head = cur[1]
    lane = head["lane_id"]
    if head["state"] in TERMINAL:
        return head["state"]
    if head["state"] == "pending":
        if _aged(head["reserved_at"], now):
            emit_loop_row("NOTIFY", lane, "reservation-stale:HITL-recoverable:pending_aged", f"{arc_id} pending > 24h; state unchanged")
            emit_loop_row("DEFERRED-HIL", lane, "reservation-stale:HITL-recoverable:pending_aged", f"{arc_id} — aged pending reservation needs operator disposition (RESOLVED-HIL or superseding arc)")
        return "pending"
    # open
    if head["pr"] is None:
        if _aged(head["transitioned_at"], now):
            emit_loop_row("NOTIFY", lane, "reservation-stale:HITL-recoverable:open_no_pr", f"{arc_id} open > 24h with no PR; state unchanged")
            emit_loop_row("DEFERRED-HIL", lane, "reservation-stale:HITL-recoverable:open_no_pr", f"{arc_id} — open reservation with no PR needs operator disposition")
        return "open"
    try:
        view = gh_view(int(head["pr"]))
    except Exception as exc:  # noqa: BLE001 -- ANY gh failure fails safe to "still open, not reclaimable"
        print(f"reservations: gh transient for {arc_id}: {exc}; still open, not reclaimable", file=sys.stderr)
        return "open"
    state = (view or {}).get("state")
    if state == "MERGED":
        transition(arc_id, "merged", lane_id=lane)
        return "merged"
    if state == "CLOSED":
        if superseded_by:
            transition(arc_id, "abandoned", lane_id=lane, superseded_by=superseded_by)
            return "abandoned"
        emit_loop_row("DEFERRED-HIL", lane, "reservation-stale:HITL-recoverable:closed_no_pointer", f"{arc_id} — PR #{head['pr']} CLOSED without a superseding pointer; confirm abandonment")
        return "open"
    if _aged(head["transitioned_at"], now):
        emit_loop_row("NOTIFY", lane, "reservation-stale:HITL-recoverable:open_stuck", f"{arc_id} open > 24h, PR #{head['pr']} still OPEN; state unchanged")
        emit_loop_row("DEFERRED-HIL", lane, "reservation-stale:HITL-recoverable:open_stuck", f"{arc_id} — stuck open reservation; operator disposition needed")
    return "open"
```
Add `reconcile` subcommand (`--arc-id`, `--superseded-by`) using `arc_metrics.gh_pr`-style `gh pr view <pr> --json state,mergedAt` (bounded: `subprocess.run(..., timeout=30)`); a `reconcile-all` subcommand iterating every non-terminal reservation (called by `session-start.sh` and by the merge lane).
- [ ] **Step 4: GREEN**, probe (the pending-aged branch must contain no transition — the tool can only remove lines, so probe by removing the `return "pending"` early-return guard that keeps state unchanged? That would fall through to `open` handling and still not transition. Record: the spec's probe is a *positive* mutation; the executable witness is `test_reservation_ground_truth`'s state assertions, and `test_ttl_never_reclaims` (below) — evidence log entry.) Add:
```python
def test_ttl_never_reclaims(qdir, monkeypatch):
    monkeypatch.setattr(rs, "emit_loop_row", lambda *a: None)
    rs.reserve("pr-30", lane_id="A", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-30", "A"); rs.update_payload("pr-30", {"pr": 30})
    far = datetime.now(UTC) + timedelta(days=30)
    assert rs.reconcile("pr-30", gh_view=lambda pr: {"state": "OPEN"}, now=far) == "open"
    assert rs.current("pr-30")[1]["state"] == "open"
```
Register `Row("C-HE-19/20", "pytest:tools/test_reservations.py::test_ttl_never_reclaims", "phase0", "local + CI", True)`.
- [ ] **Step 5: Commit** — `git add tools/reservations.py tools/test_reservations.py tools/lanes_verify.py tools/hooks/session-start.sh && git commit -m "feat(he-lanes): U-HE-18 reservation ground-truth reconcile + sensor; HITL never TTL (C-HE-03 §5/§7, C-HE-20)"`.

---

### U-HE-19: Drain ⇄ reservation integration — flip-before-append, holder-gated `append`, dead-claim holder transfer, local-row reconciliation, phases fold

**Scope.** Wire `arc_metrics` to the reservation: at drain, per claimed entry (i) flip `pending→open` with holder = this lane (sensor), (ii) `append()` refuses unless this lane holds the `open` reservation, (iii) restore/hold; `_recover_dead_claims` transfers the holder to the recovering lane in the same step; drain start drops local uncommitted rows whose reservation is held/merged by another lane; the arc row takes `phases`, `concurrent_lanes_at_open`, `arc_type_open`, `lane_id`, `head_sha`, `base_sha` from the reservation.

**Spec linkage.** C-HE-04 §2 (order + holder rule), §4 (holder transfer at recovery), §5 (local-row reconciliation), Invariants; C-HE-03 §4 (`pending→open` at drain start), §6 (holder rule; named D2 exception); C-HE-27 §3 (fold at drain, after the flip); C-HE-25 (`lane_id`, sensor on the row); C-HE-26 §1 (`arc_type_open` joins via `arc_id`).

**Files.** Modify `tools/arc_metrics.py` (`append`, `_recover_dead_claims`, `_drain_one`, `drain`, `cmd_extract`), `tools/test_arc_metrics.py`.

**Depends on.** U-HE-15, U-HE-17, U-HE-18.

**Rev 2026-08-20 (U-HE-19 execution corrections, as-built).** *(i) `record_phase` ts domain:* the Step-1 fold test's `ts="t0"` is rejected by U-HE-17's landed round-12 ISO-8601 validation; the as-built test records a valid `2026-08-20T00:00:00Z` edge. *(ii) Per-arc reservation-fault isolation:* the bootstrap `rs.emit_loop_row` NOTIFY fail-closes until U-HE-29 lands `loop_log_structured` (U-HE-17's landed contract); the as-built drain adds `rs.ReservationError` to both per-arc fault tuples (`_drain_one`'s restore tuple and `drain()`'s loop catch) so a raised emit — or a lost reserve race / a holder refusal — is a loud per-arc KEPT-QUEUED outcome, never a whole-drain abort (C-HE-04 §3's isolation doctrine, mirroring U-HE-18's `reconcile_all`). Reserve-before-emit means the legacy entry drains on the NEXT pass without re-emitting; witnessed by `test_bootstrap_emit_failure_is_per_arc_and_next_drain_proceeds`. *(iii) Holder transfer at BOTH dead-owner restore sites:* C-HE-04 §4's MUST covers the orphaned-aside restore path as well as the move-aside path; the as-built factors `_transfer_reservation_to_recoverer` and calls it at both — the deadness adjudication stays at the restore site, per the U-HE-17 `transfer_holder` docstring. *(iv) `_reconcile_local_rows` ordering:* early-return on an empty local ledger before the `committed_arc_ids()` git call (behavior-equal; avoids a per-drain git invocation on the empty path). *(v) Test-suite adaptation:* the legacy `_queue_entry` fixture gains `arc_type` (the bootstrap reserve enforces the C-HE-26 §1 domain), an autouse fixture isolates the reservation store + stubs the fail-closed emitter per test, and pre-reservation-era direct `append()` unit tests pass `require_holder=False` (they witness OTHER guards; the holder gate's witness is `test_append_refuses_unless_holder`, mutation-probe PINNED).* *(vi) Review-round corrections (codex r1–r4, each with a dedicated witness):* lane-stamped queue claims (`_claim` gains `lane_id`) so the D2 recovery transfer fires only when the dead claimant provably WAS the holder; transfer runs from the aside bytes BEFORE the restored name is public; the recoverer-dies-after-transfer double-fault stalls to the C-HE-03 §5 HITL escalation (fail-toward-stall, documented at the helper); `_reconcile_local_rows` runs under the ledger claim, guards the WHOLE per-row judgment, drops divergent duplicates of COMMITTED arcs (committed-line comparison is the §5 witness), and keeps merged-without-committed-row captures loudly; the drain-side `merged` branch holds (never releases) another lane's capture; the fold re-reads a FRESH head after `extract()` and stamps `arc_type_declared_at="open"` when the reservation is the capture point; the fallback lane id is stable per (host, worktree), trimming name+host never the path digest; the manual backfill mints its own reservation (reserve→open→append→merged) so the race fence is the store's exclusive-create CAS — no holder bypass remains, and backfill now requires `--arc-type` (C-HE-26 §1 parity with queue). *(vii) REGISTERED contradiction (codex r3 P1 vs r4 P2 oscillation — a genuine spec-internal tension, not a fix-loop):* C-HE-04 §2(ii)/C-HE-03 §6 authorize append for the OPEN holder only, while C-HE-06 §4(vi) flips `open→merged` at the merge door BEFORE the closure capture drains — under the post-U-HE-22 timeline a spec-literal open-only gate makes every normal capture undrainable. Landed reading: C-HE-03 §6 forbids *re*-append of a merged arc_id, not the merged HOLDER's own first capture — the gate admits `merged` heads whose `lane_id` is this lane (witness: `test_merged_holder_own_capture_drains_normally`; the ledger duplicate guard still forecloses second rows). Final wording routes to the U-HE-22 merge-lane landing (which owns the flip) for spec-side reconciliation.* *(viii) Later-round corrections (codex r5–r11, each witnessed):* drain terminalizes the open head it appended (generation-bound; a lost race re-folds the still-uncommitted local row via `_refold_local_row`, and the held-pass merged-ours branch heals a failed refold idempotently); the backfill (`cmd_extract`) is scoped to reservation-less history or reservations it minted itself (`branch="historical-backfill"` + recorded pr/arc_type agreement), validates merged-fields and declared-at BEFORE minting, and terminalizes before append so a crash self-heals through the merged-holder gate; the merged-path append admission is fenced by a TRI-STATE committed-history read (unreadable or unparseable committed lines HOLD, never fail open); local-row reconciliation discriminates by reservation (peer-superseded rows converge to the committed canonical line; pending relabels and baseline content are kept; a committed aid keeps exactly one occurrence); the C-HE-04 manifest carries one row per contract section. *(ix) HELD residuals (registered, fail-toward-stall per D8):* a lane dying between the open→merged flip and its first append leaves a merged headless capture NO peer can transfer (`transfer_holder` is open-only) — the entry holds loudly to HITL; and the committed-history fence reads the LOCAL `MERGED_REF` snapshot, so a stale remote-tracking ref bounds its freshness (fetch cadence is the operator envelope). Both route to the U-HE-22 merge-lane landing with the item-(vii) contradiction; additionally (r14) the backfill sentinel `BACKFILL_BRANCH` is spoofable through the reservations CLI's free-form `branch` argument — accepted under the store's documented cooperative-trust posture (the U-HE-17 `transfer_holder` docstring: a cooperative-coordination CAS, not a security fence, exactly as `transition()` trusts its caller's `lane_id`); a store-side domain refusal belongs to a reservations-module rev, not this unit. *(x) Adjudicated reviewer flip (r16 vs r18):* r16 demanded the backfill row fold its minted reservation's `lane_id`/sensor; r18 correctly reversed — those are false derived data measured long after a historical arc ran, and C-HE-25's own model is additive-null for historical rows. As-built: the backfill adopts ONLY the operator-declared classification; lane/sensor stay null (witness: `test_cmd_extract_backfill_reserves_first_and_holder_rule_stands`). With that adjudication and the item-(vii)/(ix) registered holds, the out-of-family loop reached its terminal state at r18 (12 consecutive rounds re-raising the registered merged-gate class; per the reviewer-oscillation register-and-hold discipline the remaining BLOCK carries only held/adjudicated classes and final adjudication passes to the 3-lens merge gate). *(xi) r19 terminal-round adjudications:* the bootstrap's honoring of a closure entry's `--arc-type-declared-at open` (r13's own demand) was re-raised at r19 as provenance fabrication — HELD as-built: arc-type declarations are operator judgements by the row model ('declared by operator, never inferred'), the CLI option exists for pre-reservation flows that knew the open-time label, and the reservation becomes sole authority only once U-HE-21 mints real open-time records; and the backfill resume fence binding (pr + arc_type, not the full declarations payload) stays within the cooperative-trust posture — two racing manual invocations resolve to exactly ONE row via the ledger claim + duplicate guard, the loser is refused loudly, and close-time corrections go through `relabel_arc_type_close`. *(xii) Post-terminal rounds (codex r20–r22 + merge-gate rounds 1–2), each witnessed:* the dead-claim takeover transfers from the stashed evidence at STASH time (adjudicated r15-vs-r20 tension: a live self-holder progresses on its own next pass; death degrades to the §5 stall posture); the backfill stamps `pr` at mint time (shrinking the resume fence's pr-null window to the reserve→stamp instant); `_reconcile_local_rows` gains its own TRI-STATE stops — unreadable AND unparseable committed history reconcile NOTHING (distinct from the merged-append fence's tri-state, item viii); the drain fold's sha provenance is witnessed (`head_sha`/`base_sha` asserted); the drain HOLDS (never consumes) a `BACKFILL_BRANCH` reservation in EVERY state — pending, open, or merged — since same-worktree lane identity cannot distinguish the backfill's flow from drain's (merge-gate r1 pending, r2 open/merged); and the FOURTH dead-owner consumption site (the swept-leftover-claim branch) carries its own transfer witness (merge-gate r1 witness lens).*

- [ ] **Step 1: Failing tests**
```python
# mutation-probe: drop the holder check in append()
def test_append_refuses_unless_holder(tmp_path, monkeypatch, qdir_res):
    monkeypatch.setattr(am, "LEDGER", tmp_path / "l.jsonl"); monkeypatch.setattr(am, "LANE_ID", "B")
    rs.reserve("pr-40", lane_id="A", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-40", "A")
    with pytest.raises(am.AbortError, match="holder"):
        am.append(am.ArcRow(arc_id="pr-40", merged_at="t", merge_sha="s"))
    monkeypatch.setattr(am, "LANE_ID", "A")
    am.append(am.ArcRow(arc_id="pr-40", merged_at="t", merge_sha="s"))


def test_drain_flips_before_append_and_folds_reservation_fields(tmp_path, monkeypatch, qdir_res):
    q = _queue_entries(am, tmp_path, monkeypatch, 1); monkeypatch.setattr(am, "LANE_ID", "A")
    rs.reserve("pr-1", lane_id="A", branch="b", arc_type="applying"); rs.record_phase("pr-1", "execute", "start", ts="t0")
    rs.record_round_outcome("pr-1", 1, channel="codex", terminal="APPROVE", finding_count=0)
    monkeypatch.setattr(am, "committed_arc_ids", lambda: set())
    order = []
    real_append = am.append
    monkeypatch.setattr(am, "append", lambda row: (order.append(("append", rs.current("pr-1")[1]["state"])), real_append(row))[1])
    monkeypatch.setattr(am, "extract", lambda a: am.ArcRow(arc_id="pr-1", merged_at="t", merge_sha="s"))
    am.drain(argparse.Namespace())
    assert order == [("append", "open")]                                   # flip happened BEFORE append
    row = am.read_ledger()[0]
    assert row["arc_type_open"] == "applying" and row["lane_id"] == "A" and row["phases"]["execute"]["start"] == "t0"
    assert row["round_outcomes"] == {"1": {"channel": "codex", "terminal": "APPROVE", "finding_count": 0}}
    assert row["concurrent_lanes_at_open"] == 0


# mutation-probe: drop transfer_holder() from _recover_dead_claims
def test_recover_transfers_holder_to_recoverer(tmp_path, monkeypatch, qdir_res):
    q = tmp_path / "queue"; monkeypatch.setattr(am, "QUEUE_DIR", q); monkeypatch.setattr(rs, "QUEUE_DIR", q); q.mkdir(exist_ok=True)
    monkeypatch.setattr(am, "LANE_ID", "B")
    rs.reserve("pr-50", lane_id="A", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-50", "A")
    (q / "pr-50.taken").write_text(json.dumps({"pr": 50, "arc_id": "pr-50", "_claim": {"pid": 999999, "host": socket.gethostname()}}))
    monkeypatch.setattr(am, "_process_is_alive", lambda pid: False)
    am._recover_dead_claims()
    assert (q / "pr-50.json").exists() and rs.holder("pr-50") == "B"


def test_local_row_reconciliation_drops_superseded_rows(tmp_path, monkeypatch, qdir_res):
    ledger = tmp_path / "l.jsonl"; monkeypatch.setattr(am, "LEDGER", ledger); monkeypatch.setattr(am, "LANE_ID", "A")
    monkeypatch.setattr(am, "QUEUE_DIR", tmp_path / "queue"); (tmp_path / "queue").mkdir(exist_ok=True)
    ledger.write_text(json.dumps({"arc_id": "pr-60", "record_kind": "arc"}) + "\n" + json.dumps({"arc_id": "pr-61", "record_kind": "arc"}) + "\n")
    rs.reserve("pr-60", lane_id="B", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-60", "B")   # held by another lane
    rs.reserve("pr-61", lane_id="A", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-61", "A")   # ours
    monkeypatch.setattr(am, "committed_arc_ids", lambda: set())
    am._reconcile_local_rows()
    assert [r["arc_id"] for r in am.read_ledger()] == ["pr-61"]
```
(`qdir_res` fixture: sets `rs.QUEUE_DIR` and `am.QUEUE_DIR` to one tmp dir.)
- [ ] **Step 2: RED**; **Step 3: Implement** in `arc_metrics.py`:
```python
import reservations as rs   # after the module constants; reservations imports arc_metrics' primitives, so import lazily
```
(Circular import: `reservations` imports `arc_metrics` at module load. Break it by importing `reservations` INSIDE the functions that need it — `append`, `_recover_dead_claims`, `_drain_one`, `_reconcile_local_rows` — via `import reservations as rs` at the top of each function body. State this in a comment; the U-HE-16 grep witness is unaffected.)
```python
#: This process's lane identity (C-HE-03 §3). Lane-init (U-HE-31) exports HARNESS_LANE_ID; the
#: fallback mints a stable per-process id so a lane never appears as ':'-bearing or empty.
LANE_ID = os.environ.get("HARNESS_LANE_ID") or f"{socket.gethostname().split('.')[0]}-{REPO.name}-{os.getpid():08x}"[:64].replace(":", "-")


def append(row: ArcRow, *, require_holder: bool = True) -> None:
    ...existing unmerged/duplicate guards...
    if require_holder:
        import reservations as rs
        h = rs.holder(row.arc_id)
        if h != LANE_ID:
            raise AbortError(f"{row.arc_id}: this lane ({LANE_ID}) is not the reservation holder ({h!r}) -- append refused (C-HE-04 §2)")
    ...write...
```
`cmd_extract` (manual/historical backfill, `:918-925`) calls `append(row, require_holder=False)` and prints `note: historical backfill, reservation holder check bypassed`.

In `_recover_dead_claims`, after a successful `os.replace(claim, restored)`:
```python
        import reservations as rs
        arc_id = json.loads(restored.read_text()).get("arc_id") or f"pr-{json.loads(restored.read_text())['pr']}"
        dead_lane = (rs.current(arc_id) or (0, {}))[1].get("lane_id")
        if dead_lane and rs.holder(arc_id) == dead_lane and dead_lane != LANE_ID:
            try:
                rs.transfer_holder(arc_id, from_lane_id=dead_lane, to_lane_id=LANE_ID)   # the NAMED D2 exception
                print(f"  {arc_id}: reservation holder transferred {dead_lane} -> {LANE_ID}")
            except rs.IllegalTransition as exc:
                print(f"  {arc_id}: holder transfer skipped ({exc})")
```
In `_drain_one`, before `extract`:
```python
    import reservations as rs
    cur = rs.current(arc_id)
    if cur is None:
        # Transitional bootstrap for entries queued before reservations existed (migration, plan §6 open item):
        rs.reserve(arc_id, lane_id=LANE_ID, branch=entry.get("branch", "unknown"), arc_type=entry["arc_type"], arc_type_declared_at="close")
        rs.emit_loop_row("NOTIFY", LANE_ID, "reservation-bootstrap:transient-retry:legacy_queue_entry", f"{arc_id} reservation created at drain (legacy entry)")
        cur = rs.current(arc_id)
    state = cur[1]["state"]
    if state == "pending":
        cur = (cur[0], rs.open_with_sensor(arc_id, LANE_ID))
    elif state == "open" and cur[1]["lane_id"] != LANE_ID:
        _restore_or_republish(taken, path, entry)
        print(f"  {arc_id}: open reservation held by {cur[1]['lane_id']}; not appendable by this lane -- entry held")
        return "held"
    elif state == "merged":
        path.unlink(missing_ok=True); taken.unlink(missing_ok=True)
        print(f"  {arc_id}: reservation merged; releasing queue entry")
        return "released"
    res = cur[1]
```
and after `row = extract(args)`, fold (rev. at U-HE-17 landing, codex r19: the reservation carries composite `"<round>/<channel>"` keys; `rs.fold_round_outcomes` is the committed projection to the C-HE-25 numeric arc-row shape): `row.phases = res.get("phases", {}); row.round_outcomes = rs.fold_round_outcomes(res.get("round_outcomes", {})); row.concurrent_lanes_at_open = res.get("concurrent_lanes_at_open"); row.arc_type_open = res.get("arc_type") if res.get("arc_type_declared_at") == "open" else row.arc_type_open; row.lane_id = LANE_ID; row.head_sha = res.get("head_sha"); row.base_sha = res.get("base_sha")`.

Add and call at the top of `drain()` (after `_recover_dead_claims()`):
```python
def _reconcile_local_rows() -> None:
    """C-HE-04 §5: drop this worktree's uncommitted rows whose reservation is held or merged by ANOTHER lane
    (we died after append; a peer superseded us via §4). Atomic whole-file rewrite; committed rows untouched."""
    import reservations as rs
    committed = committed_arc_ids()
    rows = read_ledger()
    keep, dropped = [], []
    for r in rows:
        aid = r.get("arc_id")
        cur = rs.current(aid) if aid and aid not in committed else None
        if cur and cur[1]["state"] in ("open", "merged") and cur[1]["lane_id"] != LANE_ID:
            dropped.append(aid); continue
        keep.append(r)
    if dropped:
        tmp = LEDGER.with_name(f".{LEDGER.name}.{os.getpid()}.tmp")
        tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in keep)); os.replace(tmp, LEDGER)
        print(f"  dropped {len(dropped)} orphaned local row(s) superseded by another lane: {', '.join(dropped)}")
```
- [ ] **Step 4: GREEN**, probes (holder check; transfer_holder call) → PINNED. Register `Row("C-HE-04", "pytest:tools/test_arc_metrics.py::test_append_refuses_unless_holder", "phase0", "local + CI", True)`.
- [ ] **Step 5: Commit** — `git add tools/arc_metrics.py tools/test_arc_metrics.py tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-19 drain⇄reservation — flip-before-append, holder-gated append, recovery transfer, local-row reconciliation, phases fold (C-HE-04 §2/§4/§5)"`.

---

### U-HE-20: AC#2 subprocess harness — `tools/test_arc_metrics_lanes.py` (six interleavings + cross-latency)

**Scope.** Real-subprocess lanes (`subprocess.Popen`, own git-inited worktree per lane, own `ARC_METRICS_REPO`/`ARC_METRICS_LEDGER`, shared `QUEUE_DIR`), a filesystem rendezvous barrier bounded 30 s, the six parametrized interleavings of AC#2(a), the sequential AC#2(b) cross-latency case, assertions over the **union** of lane ledgers. Never threads, never `multiprocessing` fork.

**Spec linkage.** C-HE-04 Verification (AC#2(a) i–vi, seam, union assertion, NOT threads/fork), C-HE-03 Verification (AC#2(b)), §8 AC#2, C-HE-05 §2–§3.

**Files.** Create `tools/test_arc_metrics_lanes.py`. Modify `tools/arc_metrics.py` (`_hold_after` seam — sibling of `_kill_after`), `tools/codex-parity-check.sh`, `tools/lanes_verify.py`.

**Depends on.** U-HE-19, U-HE-16.

- [ ] **Step 1: Seam** — beside `_kill_after` in `arc_metrics.py`:
```python
def _hold_after(step: str) -> None:
    """Test seam: ARC_METRICS_TEST_HOLD_AFTER=<step> -> touch <HOLD_DIR>/<step>.reached and wait (<= 30 s) for
    <HOLD_DIR>/<step>.go. Lets a test interleave a peer action at an exact point (C-HE-04 (iii)/(iv)/(v))."""
    if os.environ.get("ARC_METRICS_TEST_HOLD_AFTER") != step:
        return
    hold = Path(os.environ["ARC_METRICS_TEST_HOLD_DIR"])
    (hold / f"{step}.reached").touch()
    deadline = time.monotonic() + 30
    while not (hold / f"{step}.go").exists():
        if time.monotonic() > deadline:
            raise AbortError(f"hold seam timeout at {step}")
        time.sleep(0.02)
```
and call `_hold_after(step)` immediately after each `_kill_after(step)` in `_drain_one` (same step names).
- [ ] **Step 2: Write the harness** — `tools/test_arc_metrics_lanes.py`:
```python
"""AC#2 (a)/(b): real subprocess lanes over one shared QUEUE_DIR. NOT threads (module globals cannot diverge
per thread -- a false-GREEN certificate), NOT multiprocessing fork. Every case asserts over the UNION of lane
ledgers: one row per arc_id, and the C-HE-04 two-state invariant."""
from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path
import pytest

TOOLS = Path(__file__).resolve().parent
LANE_MAIN = (
    "import sys, argparse, arc_metrics as am; sys.exit(am.drain(argparse.Namespace()))"
)
BARRIER_TIMEOUT_S = 30.0


def _git_init(wt: Path) -> None:
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / ".harness").mkdir()
    (wt / ".harness" / "arc-metrics.jsonl").write_text("")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True)


def _lane_env(wt: Path, q: Path, lane_id: str, **extra) -> dict:
    env = {**os.environ, "ARC_METRICS_REPO": str(wt), "ARC_METRICS_LEDGER": str(wt / ".harness" / "arc-metrics.jsonl"),
           "ARC_METRICS_QUEUE_DIR": str(q), "HARNESS_LANE_ID": lane_id, "PYTHONPATH": str(TOOLS),
           "ARC_METRICS_MERGED_REF": "origin/main"}   # fresh repo has no origin/main -> committed_arc_ids() == set() through the REAL path
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _spawn(wt, q, lane_id, barrier: Path | None = None, **extra) -> subprocess.Popen:
    pre = ""
    if barrier is not None:
        pre = (f"import time,os,sys; d=time.monotonic()+{BARRIER_TIMEOUT_S};"
               f"\nwhile not os.path.exists({str(barrier)!r}):\n  time.sleep(0.01)\n  assert time.monotonic()<d, 'rendezvous timeout — peer leg did not reach the barrier'\n")
    return subprocess.Popen([sys.executable, "-c", pre + LANE_MAIN], env=_lane_env(wt, q, lane_id, **extra),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _entry(q: Path, arc="pr-1"):
    (q / f"{arc}.json").write_text(json.dumps({"pr": 1, "arc_id": arc, "arc_type": "inventing", "decisions": 1,
                                                "round_snapshot": {"review_rounds": 1, "round_wall_s": [1.0], "p1_rounds": [0]}}))


def _rows(wt: Path) -> list[dict]:
    p = wt / ".harness" / "arc-metrics.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _wait_for(path: Path, timeout=BARRIER_TIMEOUT_S):
    d = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < d, f"timeout waiting for {path.name}"
        time.sleep(0.01)


@pytest.fixture
def lanes(tmp_path, monkeypatch):
    q = tmp_path / "queue"; q.mkdir()
    a, b = tmp_path / "wt-a", tmp_path / "wt-b"
    _git_init(a); _git_init(b)
    # extract() shells to gh; stub it via a fake `gh` on PATH that answers the two queries drain needs
    fake = tmp_path / "bin"; fake.mkdir()
    (fake / "gh").write_text("#!/usr/bin/env bash\ncase \"$*\" in *'pr view'*) echo '{\"additions\":1,\"deletions\":1,\"changedFiles\":1,\"commits\":[{\"oid\":\"x\"}],\"createdAt\":\"2026-08-18T00:00:00Z\",\"mergedAt\":\"2026-08-18T00:10:00Z\",\"mergeCommit\":{\"oid\":\"deadbeef\"}}';; *'run list'*) echo '[]';; esac\n")
    (fake / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    return q, a, b, tmp_path


def _reserve(q, arc="pr-1", lane="seed", state="pending", holder_pid=None):
    env = {**os.environ, "ARC_METRICS_QUEUE_DIR": str(q), "PYTHONPATH": str(TOOLS)}
    subprocess.run([sys.executable, "-m", "reservations", "reserve", "--arc-id", arc, "--lane-id", lane, "--branch", "b", "--arc-type", "inventing"], env=env, check=True, capture_output=True)
    if state == "open":
        subprocess.run([sys.executable, "-m", "reservations", "transition", "--arc-id", arc, "--to", "open", "--lane-id", lane], env=env, check=True, capture_output=True)


def _union_ok(a, b, q, arc="pr-1"):
    rows = _rows(a) + _rows(b)
    assert [r["arc_id"] for r in rows] == [arc], f"union must hold exactly one row: {rows}"
    # two-state invariant: entry still queued (row uncommitted) -- there is no origin/main here, so (a) must hold
    assert (q / f"{arc}.json").exists() or (q / f"{arc}.taken").exists()
    return rows[0]


@pytest.mark.parametrize("interleaving", ["i-both-claim", "ii-both-recover", "iii-peer-removes-taken", "iv-restore-vs-claim", "v-abort-vs-claim", "vi-killed-after-append"])
def test_ac2_a_same_instant(lanes, interleaving):
    q, a, b, root = lanes
    hold = root / "hold"; hold.mkdir()
    barrier = root / ".go"
    _entry(q)
    if interleaving == "i-both-claim":
        _reserve(q)
        pa, pb = _spawn(a, q, "lane-a", barrier), _spawn(b, q, "lane-b", barrier)
        barrier.touch(); pa.wait(60); pb.wait(60)
        _union_ok(a, b, q)
    elif interleaving == "ii-both-recover":
        _reserve(q, lane="dead-lane", state="open")
        (q / "pr-1.json").rename(q / "pr-1.taken")
        d = json.loads((q / "pr-1.taken").read_text()); d["_claim"] = {"pid": 999999, "host": os.uname().nodename}
        (q / "pr-1.taken").write_text(json.dumps(d))
        for _pass in range(3):   # eventual: recoverer holds; claim race may go to the other lane once
            barrier.unlink(missing_ok=True)
            pa, pb = _spawn(a, q, "lane-a", barrier), _spawn(b, q, "lane-b", barrier)
            barrier.touch(); pa.wait(60); pb.wait(60)
        row = _union_ok(a, b, q)
        holder = subprocess.run([sys.executable, "-m", "reservations", "holder", "--arc-id", "pr-1"], env={**os.environ, "ARC_METRICS_QUEUE_DIR": str(q), "PYTHONPATH": str(TOOLS)}, capture_output=True, text=True).stdout.strip()
        assert holder == row["lane_id"] and holder in ("lane-a", "lane-b")
    elif interleaving == "iii-peer-removes-taken":
        _reserve(q)
        pa = _spawn(a, q, "lane-a", None, ARC_METRICS_TEST_HOLD_AFTER="append", ARC_METRICS_TEST_HOLD_DIR=hold)
        _wait_for(hold / "append.reached")
        (q / "pr-1.taken").unlink()          # B removes A's .taken (E9)
        (hold / "append.go").touch(); pa.wait(60)
        assert (q / "pr-1.json").exists(), "A re-published the entry from its in-memory capture"
        _union_ok(a, b, q)
    elif interleaving == "iv-restore-vs-claim":
        _reserve(q)
        pa = _spawn(a, q, "lane-a", None, ARC_METRICS_TEST_HOLD_AFTER="restore", ARC_METRICS_TEST_HOLD_DIR=hold)
        _wait_for(hold / "restore.reached")
        pb = _spawn(b, q, "lane-b"); pb.wait(60)          # B claims the just-restored entry; open held by A -> held
        (hold / "restore.go").touch(); pa.wait(60)
        _union_ok(a, b, q)
    elif interleaving == "v-abort-vs-claim":
        _reserve(q)
        pa = _spawn(a, q, "lane-a", None, ARC_METRICS_TEST_HOLD_AFTER="restore-abort", ARC_METRICS_TEST_HOLD_DIR=hold, ARC_METRICS_TEST_ABORT_EXTRACT="1")
        _wait_for(hold / "restore-abort.reached")
        pb = _spawn(b, q, "lane-b"); pb.wait(60)
        (hold / "restore-abort.go").touch(); pa.wait(60)
        assert _rows(a) + _rows(b) == [] and (q / "pr-1.json").exists()   # nothing appended; entry durable
        pa2 = _spawn(a, q, "lane-a"); pa2.wait(60)                        # A (holder) re-drains and appends
        _union_ok(a, b, q)
    else:  # vi-killed-after-append
        _reserve(q)
        pa = _spawn(a, q, "lane-a", None, ARC_METRICS_TEST_KILL_AFTER="append"); pa.wait(60)
        assert pa.returncode == 137 and (q / "pr-1.taken").exists()
        pb = _spawn(b, q, "lane-b"); pb.wait(60)                            # recovers dead claim, transfers holder, appends
        assert [r["arc_id"] for r in _rows(b)] == ["pr-1"]
        pa2 = _spawn(a, q, "lane-a"); pa2.wait(60)                          # A resumes: drops its orphaned local row
        assert _rows(a) == []
        _union_ok(a, b, q)


def test_ac2_b_cross_latency(lanes):
    """A drains and restores pending merge; B drains the same QUEUE_DIR while A's row is unmerged: B MUST NOT re-append.
    RED against unfixed HEAD with no fault injection: fresh tmp_path has no origin/main so committed_arc_ids()==set()."""
    q, a, b, _ = lanes
    _entry(q); _reserve(q)
    pa = _spawn(a, q, "lane-a"); pa.wait(60)
    assert [r["arc_id"] for r in _rows(a)] == ["pr-1"]
    pb = _spawn(b, q, "lane-b"); pb.wait(60)
    assert _rows(b) == [], "B re-appended across PR-merge latency (X4)"
    _union_ok(a, b, q)
```
(`ARC_METRICS_TEST_ABORT_EXTRACT=1` — add a two-line seam in `_drain_one` before `extract`: `if os.environ.get("ARC_METRICS_TEST_ABORT_EXTRACT"): raise AbortError("test: extract abort")`.)
- [ ] **Step 3: RED first.** Run against a checkout with U-HE-19 reverted (`git stash` is forbidden by workspace rule — use a scratch worktree at the pre-U-HE-19 commit): `test_ac2_b_cross_latency` and `vi` MUST fail there. Record both RED runs in the evidence log (this is the spec's "RED against unfixed HEAD with no fault injection").
- [ ] **Step 4: GREEN** on HEAD: `uv run pytest tools/test_arc_metrics_lanes.py -q` → 7 passed. Then the mutation probes the spec marks: revert C-HE-04 §4's re-publish (`--lines` in `_restore_or_republish`) with `--test "uv run pytest tools/test_arc_metrics_lanes.py -k iii -q"` → PINNED; drop the holder transfer with `-k ii` → PINNED; drop the holder check with `-k iv` → PINNED.
- [ ] **Step 5: Register** `Row("C-HE-03/04", "pytest:tools/test_arc_metrics_lanes.py::test_ac2_a_same_instant", "phase0", "local + CI", True)` and `Row("C-HE-03/04", "pytest:tools/test_arc_metrics_lanes.py::test_ac2_b_cross_latency", "phase0", "local + CI", True)` — **no skip** allowed. Add to `tools/codex-parity-check.sh`. **Commit**:
```bash
git add tools/test_arc_metrics_lanes.py tools/arc_metrics.py tools/codex-parity-check.sh tools/lanes_verify.py
git commit -m "test(he-lanes): U-HE-20 AC#2 subprocess harness — six interleavings + cross-latency, RED-first (C-HE-03/04)"
```

**Rev 2026-08-20 (U-HE-20 execution corrections, as-built).** *(i) RED evidence (Step 3):* scratch worktree at `8638f2e7` (pre-U-HE-19, via `git worktree add`; disposed through `safe-worktree-remove.sh` after attaching `scratch/he20-red-disposable` — the detached-HEAD refusal is the U-HE-15 guard working as designed): `test_ac2_b_cross_latency` FAILED there with "B re-appended across PR-merge latency (X4)" (the spec's RED with no fault injection) and `test_ac2_a_same_instant[vi-killed-after-append]` FAILED at the reconciliation witness; both pass at HEAD (7/7). *(ii) Fixture corrections against HEAD:* the sketch's `round_snapshot` lacked `round_log_source`/`first_round_at`/`last_round_at` (extract reads them unconditionally); the fake `gh` `mergeCommit.oid` must be 40 chars (`ci_metrics` refuses short SHAs); lane worktrees are FRESH repos with no `origin/main` (spec-literal; `committed_arc_ids()` returns `set()` through the real unreadable-`MERGED_REF` path, and a regression there surfaces through the lane-termination check) — (vi) ALONE publishes a one-BASELINE-row committed ledger at `origin/main` (codex U-HE-20 r2 P2), because its final legs judge local rows against committed content; an EMPTY committed ledger is indistinguishable from an unreadable one through `run()`'s non-empty-output validation (`tools/arc_metrics.py:212-213`), so `_committed_ledger_lines()` returns None and reconciliation correctly holds (fail-safe; registered as an observation — a brand-new repo whose committed ledger is empty never reconciles local rows until a first row lands). *(iii) Interleaving (ii):* the dead claim's pid is a REAPED child's, provably dead on every platform (codex U-HE-20 r2 P3 — a fixed sentinel can be live where pid_max exceeds it); the dead `.taken` claim must carry `lane_id` (`_transfer_from_dead_claim` refuses an unstamped claim — U-HE-19 rev item (vi)); after the same-instant recovery race, a deterministic completion leg re-drains the transferred holder (the claim race may go to the non-holder in the barrier pass; a fixed 3-pass retry was a flake window), asserting the holder moved off `dead-lane` and the appended row's `lane_id` equals the reservation's. *(iv) Probe re-siting:* `append()`'s `_require_reservation_holder` is a SECOND serial guard, so commenting a drain-side elif alone still yields zero rows — (iv)/(v)/(b) therefore assert the drain-side classification MESSAGE, which makes the elif mutations killable; the sketch's "holder check with `-k iv`" pin lands at `-k v` (at the `restore` hold point the reservation is already `merged` — terminalization precedes the restore in `_drain_one` — so (iv) exercises the merged-held elif, (v) the open-held elif); pins: re-publish fallback ↦ (iii), `transfer_holder` ↦ (ii), open-held elif ↦ (v), merged-held elif ↦ AC#2(b). *(v) Interleaving (vi) vs the landed C-HE-04 §5:* the sketch's "A's resumed drain drops its orphaned local row" predates U-HE-19's as-built refinement (rev items (vi)/(ix), codex r2 P1): a merged-by-other row with NO committed replacement row is KEPT LOUDLY ("local row kept pending reconciliation" — merged state alone does not prove the replacement row exists; dropping could discard the only capture). The as-built test witnesses the FULL landed story: kill → recovery + holder transfer → B's row lands → A's resume keeps loudly (transient two-copy union, entry held, no double-append) → B's canonical row promoted into A's committed history → A's next drain converges the stale copy to the committed canonical line and releases the entry. The drop-at-committed-point reading routes with the U-HE-19 item-(vii)/(ix) residuals to the U-HE-22 merge-lane landing for spec-side reconciliation of the (vi) sentence. HELD against codex U-HE-20 r1 P2 → r2 P1 (the reviewer re-raises the spec-literal immediate-drop reading): asserting the immediate drop would RED against the 22-round-reviewed landed §5 — the safety property AC#2 protects is merged-HISTORY uniqueness (C-HE-03 Invariants: at most one row for the arc_id ever reaches merged history), which the convergence leg asserts exactly; the transient two-local-copy state satisfies the C-HE-04 two-state invariant (entry held, rows uncommitted). Adjudication passes to the 3-lens merge gate per the register-and-hold discipline. *(r6 additions:)* the (vi) leg carries its own PINNED behavioral kill (comment the §5 replaced-with-committed convergence block → the final convergence assertion goes red), so kill-after-append has HEAD-behavioral RED evidence beyond the process-level scratch-worktree record — a cross-commit RED cannot be a probe pin by construction (the tool mutates HEAD only), and the rc≠0 log rows the reviewer read as "the RED record" are refused/mis-ranged invocations the append-only run log keeps honestly (never counted by `_pin_is_live`); the (ii) dead-pid in-test reuse window is closed by asserting neither racing lane was allocated the reaped pid (the external window is production's own D2 seconds-scale liveness posture). 20 probes PINNED, coverage 0, at the terminal HEAD. *(v-bis) Mid-restore concurrency (codex U-HE-20 r3 P2):* a `restore-link` hold seam lands INSIDE `_restore_or_republish` (between the exclusive re-link and the claim unlink) so (iv) interleaves B's claim DURING A's restore — the takeover is refused by A's LIVE claim (exclusive create, never timing; B classifies it outstanding and yields), then B's post-restore claim witnesses the merged-held elif; (v) likewise interleaves mid-abort-restore at the same seam, then witnesses the open-holder elif on a post-restore claim (codex r4 P2); the same-instant barrier is TWO-PHASE (each lane reports `<lane>.ready` before polling `.go`; the parent releases only after both — codex r4 P2); the (ii) dead pid is a reaped child's, re-verified dead by `kill(0)` at stamp time with the residual reuse window documented as production's own (codex r4 P3). *(vi) Lane termination validated:* `_finish` asserts every lane exits through the documented outcome classes (0/1; the kill leg 137) with no traceback — the losing racer logs and yields (C-HE-04 §1). *(vii) C-HE-30 audit:* the `_hold_after` rendezvous artifacts (`<HOLD_DIR>/<step>.reached`/`<step>.go`) are listed in the ephemeral-artifacts table of `store-audit-he-loop-lanes.md` (test-only, never production env). *(viii) §8.1 row-count note:* the spec's §8.1 lanes row says "(5 interleavings)" while the C-HE-04 Verification body (clearance fold G6) enumerates six — the stale count is informational; the Verification body governs and six are parametrized.*

---

### U-HE-21: Reservation CLI wiring into `roadmap-continue` and `ship-pr`

**Scope.** `roadmap-continue` creates the `pending` reservation with `--arc-type` the instant it selects the roadmap unit (refusing selection when `selectable` says no); `ship-pr` back-fills `pr`, `head_sha`, `base_sha` at PR creation / final gate and `attested_merge_tree` at final-gate time; both export `HARNESS_ARC_ID`; `session-start.sh` runs `reconcile-all`.

**Spec linkage.** C-HE-03 §3 (back-fill fields; `lane_id` from `HARNESS_LANE_ID`), §4 (`(none)→pending` at arc open, before any work; second lane's selection fails), C-HE-26 §1 (open-time capture point is this reservation, not the closure-time queue); C-HE-06 §4(ii) consumer of `attested_merge_tree`.

**Files.** Modify `.claude/skills/roadmap-continue/SKILL.md`, `.claude/skills/ship-pr/SKILL.md`, `tools/hooks/session-start.sh`; add `tools/hooks/test_skill_reservation_wiring.sh` (grep witness that the skills carry the exact commands).

**Depends on.** U-HE-17, U-HE-18.

- [ ] **Step 1: Failing test** — `tools/hooks/test_skill_reservation_wiring.sh` (same `ok/bad` idiom as `test_skill_two_lane.sh`):
```bash
RC=.claude/skills/roadmap-continue/SKILL.md; SP=.claude/skills/ship-pr/SKILL.md
grep -q 'python tools/reservations.py reserve --arc-id' "$RC" && ok "roadmap-continue reserves at selection" || bad "no reserve step"
grep -q 'python tools/reservations.py selectable --arc-id' "$RC" && ok "selection checks selectable" || bad "no selectable check"
grep -q -- '--arc-type' "$RC" && ok "arc_type declared at open" || bad "arc_type not at open"
grep -q 'reservations.py update --arc-id .* --set pr=' "$SP" && ok "ship-pr back-fills pr" || bad "no pr back-fill"
grep -q 'attested_merge_tree=' "$SP" && ok "ship-pr records attested_merge_tree" || bad "no attested tree"
grep -q 'export HARNESS_ARC_ID' "$RC" && ok "HARNESS_ARC_ID exported" || bad "no HARNESS_ARC_ID"
grep -q 'reservations.py reconcile-all' tools/hooks/session-start.sh && ok "session-start reconciles" || bad "no reconcile-all"
```
- [ ] **Step 2: RED**; **Step 3: Carrier edits.**
  - `roadmap-continue/SKILL.md`, in the "derive next action" step, insert after the unit is chosen: *"**Arc open (C-HE-03 §4).** Before any work: `ARC_ID=<unit-id-or-pr-slug>`; `uv run python tools/reservations.py selectable --arc-id "$ARC_ID" || { echo 'reserved by another lane — pick the next unit'; <re-derive>; }`; then `uv run python tools/reservations.py reserve --arc-id "$ARC_ID" --lane-id "$HARNESS_LANE_ID" --branch "$(git branch --show-current)" --arc-type <inventing|applying>` (declare the type NOW — C-HE-26 §1); `export HARNESS_ARC_ID="$ARC_ID"`."*
  - `ship-pr/SKILL.md`: after `gh pr create` → `uv run python tools/reservations.py update --arc-id "$HARNESS_ARC_ID" --set pr=<N> head_sha=$(git rev-parse HEAD) base_sha=$(git rev-parse origin/main)`; at final gate (before the merge door) → `uv run python tools/reservations.py update --arc-id "$HARNESS_ARC_ID" --set head_sha=$(git rev-parse HEAD) base_sha=$(git rev-parse origin/main) attested_merge_tree=$(git merge-tree --write-tree origin/main HEAD)`.
  - `session-start.sh`: `uv run python tools/reservations.py reconcile-all >/dev/null 2>&1 || true` (advisory at start; the merge lane runs it blocking).
- [ ] **Step 4: GREEN**, register `Row("C-HE-03", "shell:tools/hooks/test_skill_reservation_wiring.sh", "phase0", "local + CI", False)`, commit:
```bash
git add .claude/skills/roadmap-continue/SKILL.md .claude/skills/ship-pr/SKILL.md tools/hooks/session-start.sh tools/hooks/test_skill_reservation_wiring.sh tools/lanes_verify.py
git commit -m "feat(he-lanes): U-HE-21 reservation carriers — open-time pending + arc_type, ship-pr back-fill (C-HE-03 §3-4, C-HE-26 §1)"
```

**Rev 2026-08-20 (U-HE-21 execution corrections, as-built).** *(i) Session-start carrier path + already-landed pass:* the Files line's `tools/hooks/session-start.sh` does not exist — the real SessionStart carrier is `tools/roadmap-audit/session-start.sh` (registered at the U-HE-18 rev item (iii)), and the C-HE-03 §5 `reconcile-all` pass itself LANDED with U-HE-18 (detached, activation-gated, `--log-to-store`) — this unit's Step-3 hook edit is therefore a no-op; the as-built Step-1 witness greps the real path and pins the landed invocation against removal. *(ii) Lane-id mint fallback:* `HARNESS_LANE_ID` is exported by U-HE-31's lane-init, not yet landed; the carrier mints one when unset (`reservations.py mint-lane-id`, the CLI U-HE-17 shipped) so open-time reservations never block on the not-yet-built lane-init. *(iii) Same-lane re-entry:* `selectable()` is false for ANY existing head — including this lane's own `pending` after a crash/compaction re-entry — so the carrier adds a `show`-based same-lane resume clause (continue WITHOUT re-reserving when the head's `lane_id` is ours); the C-HE-03 §4 second-lane refusal is unchanged. *(iv) Line-based witness:* the Step-1 grep patterns are line-based by construction; the PR-creation back-fill lands as a single-line command in ship-pr. *(v) Go-live witness (closes the U-HE-19 rev item (xi) hold):* this arc dogfooded its own wiring — the first REAL open-time reservation was minted for `u-he-21` itself (gen 1, `pending`, `arc_type=applying` with `arc_type_declared_at=open`, seq 1) before the carrier edits were committed; with `HARNESS_ARC_ID` now exported at open and ship-pr back-filling the merge tuple, the review-wrapper `branch-*` arc-id fallback, the R17-a/b same-(producer, round) wrapper residual (flow-excluded by the arc-serial holder discipline), and the `update_payload` merge-tuple single-writer-by-flow note (codex round-2 P2) all have their real producers; the door-side consumer of `attested_merge_tree` remains U-HE-22 (recording starts at this unit, byte-compare at the door lands there).* *(vi) Round-1 codex corrections (each witnessed by the strengthened grep suite):* shell exports do not survive across Bash tool invocations, so the carriers now pass every id/SHA as a LITERAL (no `$( )`, single clean invocations) with `export HARNESS_ARC_ID` scoped same-shell and the inline `HARNESS_ARC_ID=<arc> HARNESS_LANE_ID=<lane> just review-with-failover` form for the wrapper's env consumers; the arc-open block is a real two-branch flow (`selectable` exit 0 → reserve; exit 1 → `show`, same-lane head resumes WITHOUT re-reserving since `reserve()` refuses any existing head, other-lane head re-derives — the sketch's unconditional reserve-after-show always raised); the clearance marker is renamed to the `...-v1-u-he-21-as-built-rev-...` shape so `artifact_heads.py`'s filename tie-break (same numeric v1.0 + same date as the U-HE-20 marker) resolves THIS rev as the family head. *REGISTERED residual (→ **U-HE-25**, the guard-modification unit — the codex-r3 correction of r1's mis-route to U-HE-23, which touches only `merge_door.py`):* the guard's allowlist covers neither `uv run python tools/reservations.py`, nor leading `HARNESS_*=` env-prefix assignments, nor `git merge-tree`, so in loop mode each mandatory reservation/review command surfaces one approval prompt; the three allowlist additions are now REGISTERED VERBATIM in U-HE-25's Scope line (this session's classifier also hard-blocks guard self-modification from a background job — consistent with routing to the planned reviewed guard unit).* *(vii) Round-2 codex corrections:* the lane id is minted ONCE per worktree and persisted at gitignored `.harness/.lane-id` (a per-session re-mint's fresh random suffix would make same-lane resume misclassify its own reservation — the wrapper's stable fallback and C-HE-03 §3's "minted at lane init" both presume persistence; durable minting remains U-HE-31's); ship-pr's PRE-FLIGHT review invocation carries the inline `HARNESS_*` prefix too (ship-pr is independently invocable — a bare invocation would write `branch-*`/`-nolane` fallback ids into the C-HE-24/25 rows); and the reviewer's door-timing P1 — the reservation is still `pending` at ship-pr's final gate while C-HE-06 acquisition demands an `open` holder (the only production `pending→open` flip is drain, which runs post-merge) — is REGISTERED into the existing flip-timing contradiction class routed to the U-HE-22 merge-lane landing (U-HE-19 rev item (vii): C-HE-04 §2(ii)/C-HE-03 §6 vs C-HE-06 §4(vi)); the door carrier owns where the flip lands, this unit's payload-only `update` is valid on a pending head either way. *HELD (rounds 2–3 re-raise of the registered guard-friction class):* the loop-mode approval prompts stand until U-HE-25's reviewed guard edit (three allowlist additions now registered verbatim in its Scope; U-HE-25 sits in the same S4c cluster, two units ahead in the §3 topological order); the carrier is fully usable interactively today, the shapes are never DENIED (witnessed — a deny would structurally block the loop; an ask merely pauses it), and the classifier block on background-session guard self-modification makes the in-arc alternative unavailable by policy, not by omission — adjudication passes to the 3-lens merge gate per the register-and-hold discipline.* *(viii) Round-4 codex corrections:* the reviewer's headless citation is CONFIRMED (`tools/04-loop/run.sh:18` — ask→deny in the headless runner), which upgrades the friction to "headless arcs cannot reserve at open pre-U-HE-25"; the carrier now states the explicit DEGRADATION rather than a stall: a permission-refused reserve → proceed UNRESERVED + note in the PR body — append safety is already held by the LANDED U-HE-19 drain bootstrap (reservation minted at closure) + the C-HE-03 §6 holder gate; only §4 selection-time scheduling dedup defers to U-HE-25 for headless lanes (attended lanes reserve today, one prompt per command). Also absorbed: the selectable→reserve TOCTOU (a lost `reserve` race is handled identically to the occupied-selectable path — `show` and branch); the lane-id mint write race (file content is authoritative: never overwrite, re-read after write and adopt the winner; same-worktree concurrent sessions are out-of-discipline — one worktree IS one lane — and atomic exclusive-create minting is registered to U-HE-31 lane-init); and the guard witness leg is hardened per the gate-cannot-tell-empty-from-unlooked rule — pipeline failure is loud (`PIPELINE_FAILURE` ≠ ask), and a positive control (force-push → deny) runs FIRST so an inert guard cannot silently satisfy the never-denied floor.* *(ix) Round-5 codex corrections:* the CANONICAL step-4 review invocation now carries the inline `HARNESS_*` prefix (round 2 had fixed only the arc-open paragraph's example — the loop body's own command was still bare, so following the skill verbatim wrote fallback ids; the witness now asserts EVERY `review-with-failover` mention in both carriers is prefixed, not merely that one prefixed example exists); the resume branches check `state` BEFORE `lane_id` (a terminal head owned by this lane satisfied the equality branch first — a completed/abandoned arc could be resumed; terminal heads are now never resumed regardless of owner); and ship-pr's back-fills gain the unreserved-skip clause (`show` reports no reservation → skip both back-fills + PR-body note — `update` on a nonexistent reservation aborts, and the closure-time reservation is the drain bootstrap's), making the headless degradation path coherent END-TO-END rather than only at open. *HELD (round-5 re-raises of registered/held classes):* require-allow in the witness would gate this unit on U-HE-25's registered scope (ask is the documented pre-U-HE-25 state; deny is the pinned regression); the lane-id mint's atomic exclusive-create belongs to U-HE-31 lane-init (registered — the file-content-authoritative rule bounds the crash-overlap edge, and same-worktree concurrent sessions are out-of-discipline); the land-guard-support-now demand remains classifier-blocked in this venue and registered verbatim at U-HE-25.* *(x) Round-6 codex corrections:* BOTH U-HE-25 registration matchers narrowed to exact shapes (r6's two live P2s — the bare `tools/reservations.py` prefix would have auto-approved `transition`/`gc`; the generic `HARNESS_[A-Z0-9_]+` strip would have let `HARNESS_FAILOVER_CHILD=1 just gemini-review` skip outcome persistence — now `selectable|show|reserve|update|mint-lane-id` verbs and `HARNESS_ARC_ID=`/`HARNESS_LANE_ID=` names only); the headless degradation trigger broadened to ANY refused arc-open command (`mint-lane-id`/`selectable`/`reserve` — r6 P1a: handling only `reserve` left the clause unreachable since the earlier commands deny first), with a refused PREFIXED review degrading to the bare allowlisted `just review-with-failover` (fallback ids = the pre-U-HE-21 posture; its ALLOW floor is now witnessed so losing the allowlist entry cannot silently strand headless review); and the flip-timing class (r6 P1b re-raise) now has a REGISTERED CARRIER LINE in U-HE-22's own Scope — the door unit must either admit the `pending` holder-elect at acquire or open pre-acquire at ship-pr's final gate, reconciling C-HE-03 §4/§6 vs C-HE-06 §4(vi) there (it is no longer only a rev-note residual).* *(xi) Terminal round (codex r7 — the guard class's 7th consecutive raise; register-and-hold terminal, as at U-HE-20):* absorbed — `mint-lane-id` added to the guard-adjudication matrix (an explicit deny of lane-id initialization would otherwise stay green), and the headless degradation is documented as a DISTINCT EXPLICIT CONTRACT at the witness's adjudication section (the reviewer's accepted alternative formulation). HELD with terminal adjudication passing to the 3-lens merge gate: the r7 P1 frames the degradation as "failing open" on C-HE-03 §4 dedup, but pre-U-HE-21 headless lanes had NO selection-time dedup at all — the degradation is byte-identical to the baseline posture for a venue that cannot yet execute the new commands (no allowlisted wrapper exists without a guard edit; the guard edit is classifier-blocked from this venue and registered exact-shape at U-HE-25) — so the carrier is a strict coverage INCREASE (attended lanes gain §4 dedup now, headless lanes at U-HE-25 — which the post-#1411 roadmap execution order reaches in S4c, before the still-unlanded S4d activation-gate unit U-HE-29 whose go-live witness the roadmap routes there); and the lane-id exclusive-create re-raise (4th) remains registered to U-HE-31 lane-init with the file-content-authoritative rule bounding the crash-overlap edge.*

---
# S4c — Merge-door lease + wrapper + X9 fences

### U-HE-22: `tools/merge_door.py` lease primitive — acquire (fail-fast, rate limit, holder invariant), transition marker, release / reclaim / self-resume / unblock, poison-pill completion, GC

**Scope.** Create the merge-door lease: single global `QUEUE_DIR/merge-door/LEASE` (immutable JSON created by exclusive create, payload per C-HE-06 §3), token-named sidecars for `merge_attempted_at` and `blocked` (payload CAS = temp + `os.link` onto a token-named name), the `transition.<lease_token>` marker family through which every release / reclaim / unblock / self-resume passes, third-party idempotent completion of a dead creator's marker, the K=5/60 s per-lane rate limit, and GC of markers + history (30 d). **Registered from U-HE-21 (codex r2/r6 on that PR — the flip-timing class, joint with U-HE-19 rev item (vii)):** a normally-opened arc's reservation is still `pending` at ship-pr's final gate (the only landed `pending→open` flip is the drain's, which runs at closure AFTER merge), while `acquire()`'s holder invariant as sketched admits only an `open` holder — wired as sketched, the door rejects every normal arc. This unit MUST resolve the acquire-time state contract: either `acquire()` admits the arc's `pending` holder-elect (flipping at step (vi) with `merged`), or the ship-pr final gate performs the open flip pre-acquire — and the C-HE-03 §4/§6 vs C-HE-06 §4(vi) spec wording reconciles HERE with the rest of the registered class.

**Spec linkage.** C-HE-06 §2 (primitive; fail-fast; rate limit not counted against budget), §3 (payload), §6 (marker; two-step reclaim; poison-pill; self-resume via reclaim; unblock keyed to `blocked_at_sha`; reclaim transfers merge authority, not reservation ownership), §7 (lease-holder invariant P2), Invariants (no lease without `pr`/`head_sha`/`reservation_id`/`lease_token`; no path-only unlink); C-HE-02 §1–§3.

**Files.** Create `tools/merge_door.py`, `tools/test_merge_door.py`. Modify `tools/codex-parity-check.sh`, `tools/lanes_verify.py`.

**Interfaces.**
```python
DOOR = QUEUE_DIR / "merge-door"; LEASE = DOOR / "LEASE"
RATE_K = 5; RATE_WINDOW_S = 60; GC_KEEP_DAYS = 30
class LeaseError(RuntimeError); class LeaseHeld(LeaseError); class RateLimited(LeaseError); class HolderInvariant(LeaseError); class MarkerLost(LeaseError); class DoorBlocked(LeaseError)
def read_lease() -> dict | None                                # LEASE + sidecars merged into one view (state, merge_attempted_at, blocked_*)
def acquire(*, lane_id, arc_id, pr, head_sha, base_sha, now=None) -> dict
def win_marker(token: str, target_action: str) -> Path | None
def mark_attempted(lease: dict, *, suffix: str = "") -> None
def mark_blocked(lease: dict, *, sha: str, reason: str) -> None
def release(lease: dict) -> None
def reclaim(lease: dict, *, lane_id: str, ground_state: str) -> dict
def unblock(*, pr: int, blocked_at_sha: str, lane_id: str) -> None
def complete_dead_marker(marker: Path) -> bool
def gc(*, now=None) -> list[Path]
```

**Depends on.** U-HE-17 (holder invariant reads the reservation), U-HE-14.

- [ ] **Step 1: Failing tests** — `tools/test_merge_door.py` (first half):
```python
"""C-HE-06 merge-door lease. gh is mocked with a call log; no skip."""
from __future__ import annotations
import json, os, subprocess
from pathlib import Path
import pytest
import merge_door as md
import reservations as rs


@pytest.fixture
def door(tmp_path, monkeypatch):
    q = tmp_path / "queue"; q.mkdir()
    monkeypatch.setattr(md, "QUEUE_DIR", q); monkeypatch.setattr(md, "DOOR", q / "merge-door"); monkeypatch.setattr(md, "LEASE", q / "merge-door" / "LEASE")
    monkeypatch.setattr(rs, "QUEUE_DIR", q)
    rs.reserve("pr-1", lane_id="A", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-1", "A")
    return q


def _acq(lane="A", arc="pr-1", pr=1, now=1000.0):
    return md.acquire(lane_id=lane, arc_id=arc, pr=pr, head_sha="h" * 40, base_sha="b" * 40, now=now)


def test_acquire_payload_and_required_fields(door):
    l = _acq()
    for k in ("lease_token", "lane_id", "reservation_id", "pr", "head_sha", "base_sha", "acquired_at", "pid", "host", "merge_attempted_at", "state", "blocked_at_sha", "blocked_reason"):
        assert k in l
    assert len(l["lease_token"]) == 32 and l["state"] == "held" and l["reservation_id"] == "pr-1"


# mutation-probe: replace the FileExistsError→LeaseHeld with a sleep-and-retry inside acquire()
def test_contention_fail_fast(door):
    _acq()
    with pytest.raises(md.LeaseHeld):
        _acq(lane="B", arc="pr-1")


def test_lease_holder_invariant(door):
    rs.reserve("pr-2", lane_id="B", branch="b", arc_type="inventing")   # pending, not open
    with pytest.raises(md.HolderInvariant):
        md.acquire(lane_id="B", arc_id="pr-2", pr=2, head_sha="h" * 40, base_sha="b" * 40)
    with pytest.raises(md.HolderInvariant):
        md.acquire(lane_id="B", arc_id="pr-1", pr=1, head_sha="h" * 40, base_sha="b" * 40)  # open but held by A


def test_rate_counter_ignores_tmp_remnants(door):
    (md.DOOR / "attempts" / "A").mkdir(parents=True)
    (md.DOOR / "attempts" / "A" / ".1000.000000.4242.tmp").write_text("")     # a crashed publish_exclusive left this
    _acq(now=1000.0)                                                            # must not raise ValueError


def test_rate_limit_sixth_refused(door):
    _acq(now=0.0)                          # attempt 1 succeeds -> lease held
    for i in range(4):                     # attempts 2..5 contend (LeaseHeld, counted)
        with pytest.raises(md.LeaseHeld):
            _acq(lane="A", now=1.0 + i)
    with pytest.raises(md.RateLimited):
        _acq(lane="A", now=10.0)           # 6th within 60 s -> refused (lease_acquire_rate_exceeded), NOT LeaseHeld
    with pytest.raises(md.LeaseHeld):
        _acq(lane="A", now=61.5)           # window slid -> ordinary contention again


# mutation-probe: drop win_marker() from release() (path-only unlink)
def test_marker_race_exactly_one_wins(door):
    l = _acq()
    assert md.win_marker(l["lease_token"], "release") is not None
    assert md.win_marker(l["lease_token"], "reclaim") is None
    with pytest.raises(md.MarkerLost):
        md.release(l)                    # holder lost the marker → must stop driving


def test_release_then_history_file(door):
    l = _acq(); md.release(l)
    assert md.read_lease() is None and (md.DOOR / f"released.{l['lease_token']}").exists()


def test_reclaim_two_step_and_transfers_merge_authority_only(door):
    l = _acq(lane="A")
    with pytest.raises(md.LeaseError, match="live"):
        md.reclaim(l, lane_id="A", ground_state="OPEN")             # same lane, holder pid ALIVE → refused (round-2 P1)
    dead = {**l, "pid": 999999}
    new = md.reclaim(dead, lane_id="B", ground_state="OPEN")
    assert new["lease_token"] != l["lease_token"] and new["lane_id"] == "B" and new["pr"] == 1
    assert rs.holder("pr-1") == "A"                                  # reservation ownership NOT transferred (P2)
    assert (md.DOOR / f"reclaimed.{l['lease_token']}").exists()


# mutation-probe: return `read_lease() or fresh` in reclaim() without checking the token
def test_reclaim_never_adopts_a_foreign_lease(door, monkeypatch):
    l = _acq(lane="A"); dead = {**l, "pid": 999999}
    real_publish = md._publish_fresh
    def sneak_in(fresh):                       # another lane grabs the free door in the move->publish window
        rs.reserve("pr-9", lane_id="C", branch="b", arc_type="inventing"); rs.open_with_sensor("pr-9", "C")
        md.acquire(lane_id="C", arc_id="pr-9", pr=9, head_sha="h"*40, base_sha="b"*40)
        real_publish(fresh)                    # FileExistsError swallowed inside
    monkeypatch.setattr(md, "_publish_fresh", sneak_in)
    with pytest.raises(md.LeaseError, match="lost the door"):
        md.reclaim(dead, lane_id="B", ground_state="OPEN")
    assert md.read_lease()["lane_id"] == "C"    # the foreign lease is untouched; nothing drove pr 1


# mutation-probe: drop the `_publish_fresh(m["fresh_lease"])` completion in complete_dead_marker()
def test_crashed_reclaimer_completed_by_third_party_publishes_fresh_lease(door):
    """Reclaimer wins the marker (payload carries the fresh lease), moves the old lease aside, then dies before
    publishing. A third party completing the marker MUST publish the fresh token -- otherwise the door reads free and
    the attempted-state continuation is lost (Codex round-2 P1)."""
    l = _acq(lane="A"); md.mark_attempted(l)
    dead = {**l, "pid": 999999}
    fresh = {**dead, "lease_token": "f" * 32, "lane_id": "B", "pid": 999998, "state": "held", "merge_attempted_at": md.read_lease()["merge_attempted_at"]}
    m = md.win_marker(l["lease_token"], "reclaim", extra={"fresh_lease": fresh})
    body = json.loads(m.read_text()); body["pid"] = 999999; m.write_text(json.dumps(body))   # creator died
    md._move_lease(l["lease_token"], "reclaimed")                     # ...after moving the old lease aside
    assert md.read_lease() is None                                   # door LOOKS free: the hazard
    assert md.complete_dead_marker(m) is True
    got = md.read_lease(); assert got and got["lease_token"] == "f" * 32 and got["merge_attempted_at"] is not None
    assert md.complete_dead_marker(m) is False                       # idempotent


# mutation-probe: remove complete_dead_marker's rename (third-party completion) → door locked after a reclaimer crash
def test_dead_marker_completed_by_third_party(door):
    l = _acq()
    m = md.win_marker(l["lease_token"], "release")
    body = json.loads(m.read_text()); body["pid"] = 999999; m.write_text(json.dumps(body))   # creator died mid-release
    assert md.complete_dead_marker(m) is True
    assert md.read_lease() is None                                   # door open again
    assert md.complete_dead_marker(m) is False                       # idempotent: already done


def test_mark_attempted_is_crash_safe_sidecar(door):
    l = _acq(); md.mark_attempted(l)
    assert (md.DOOR / f"LEASE.{l['lease_token']}.attempted").exists()
    assert md.read_lease()["merge_attempted_at"] is not None
    md.mark_attempted(l)   # idempotent


def test_blocked_and_unblock_through_marker(door):
    l = _acq(); md.mark_blocked(l, sha="m" * 40, reason="post_merge_ci_not_green")
    assert md.read_lease()["state"] == "blocked"
    with pytest.raises(md.LeaseError):
        md.unblock(pr=1, blocked_at_sha="x" * 40, lane_id="A")      # keyed to blocked_at_sha
    md.unblock(pr=1, blocked_at_sha="m" * 40, lane_id="A")
    assert md.read_lease() is None
```
- [ ] **Step 2: RED**; **Step 3: Write `tools/merge_door.py` (primitive half)**
```python
#!/usr/bin/env python3
"""Merge-door lease (C-HE-06): the single-writer landing fence.

QUEUE_DIR/merge-door/LEASE is created by exclusive create and NEVER mutated in place. Payload
changes (`merge_attempted_at`, `blocked`) are token-named sidecars published by temp + os.link so a
crash cannot leave a marker half-written. Every release / reclaim / unblock / self-resume first wins
the exclusive create of transition.<lease_token>; only the marker winner may os.rename(LEASE, ...).
There is no path-only unlink of LEASE anywhere in this file. Acquire is fail-fast: one attempt, the
CALLER decides retry (D3); arbitration never moves into the primitive.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import reservations as rs
from arc_metrics import QUEUE_DIR, REPO, _process_is_alive, ci_is_green, publish_exclusive

DOOR = QUEUE_DIR / "merge-door"
LEASE = DOOR / "LEASE"
RATE_K = 5
RATE_WINDOW_S = 60
GC_KEEP_DAYS = 30
MERGE_TIMEOUT_S = 120.0
POST_MERGE_CI_BOUND_S = 45 * 60
REFRESH_BOUND_S = 45 * 60
BACKOFF = {"base_s": 30.0, "factor": 2.0, "cap_s": 600.0, "max_attempts": 12}
KILL_STEPS = ("acquire", "verify", "attempted", "merge", "confirm", "reservation-merged", "post-ci", "refresh-attempted", "refresh-merged", "release")


class LeaseError(RuntimeError): ...
class LeaseHeld(LeaseError): ...
class RateLimited(LeaseError): ...
class HolderInvariant(LeaseError): ...
class MarkerLost(LeaseError): ...
class DoorBlocked(LeaseError): ...
class DoorFailed(LeaseError): ...


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kill_after(step: str) -> None:
    if os.environ.get("MERGE_DOOR_TEST_KILL_AFTER") == step:
        sys.stdout.flush(); sys.stderr.flush(); os._exit(137)


def _sidecar(token: str, name: str) -> Path:
    return DOOR / f"LEASE.{token}.{name}"


def read_lease() -> dict | None:
    """The LEASE view: base payload + sidecars (attempted / blocked / refresh) merged in."""
    if not LEASE.exists():
        return None
    try:
        lease = json.loads(LEASE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tok = lease["lease_token"]
    att = _sidecar(tok, "attempted")
    if att.exists():
        lease["merge_attempted_at"] = json.loads(att.read_text())["merge_attempted_at"]
    blk = _sidecar(tok, "blocked")
    if blk.exists():
        b = json.loads(blk.read_text()); lease.update(state="blocked", blocked_at_sha=b["blocked_at_sha"], blocked_reason=b["blocked_reason"], blocked_at=b["blocked_at"])
    ref = _sidecar(tok, "refresh")
    if ref.exists():
        lease["refresh"] = json.loads(ref.read_text())
        ratt = _sidecar(tok, "refresh.attempted")
        if ratt.exists():
            lease["refresh"]["merge_attempted_at"] = json.loads(ratt.read_text())["merge_attempted_at"]
    return lease


def _rate_check(lane_id: str, now: float) -> None:
    """K acquire attempts per lane per 60 s. Refusals never touch the caller's §8 budget."""
    d = DOOR / "attempts" / lane_id
    d.mkdir(parents=True, exist_ok=True)
    def _ts(p: Path) -> float | None:
        try:
            return float(p.name)
        except ValueError:
            return None                          # `.<ts>.<pid>.tmp` remnants of a crashed publish_exclusive: not attempts
    recent = [p for p in d.iterdir() if (_ts(p) is not None and now - _ts(p) <= RATE_WINDOW_S)]
    for junk in d.glob(".*.tmp"):
        if now - junk.stat().st_mtime > 3600:
            junk.unlink(missing_ok=True)
    if len(recent) >= RATE_K:
        raise RateLimited(f"{lane_id}: > {RATE_K} lease acquire attempts in {RATE_WINDOW_S}s (cause_attribution: lease_acquire_rate_exceeded)")
    for _ in range(8):
        try:
            publish_exclusive(d / f"{now:.6f}", ""); break
        except FileExistsError:
            now += 1e-6


def acquire(*, lane_id: str, arc_id: str, pr: int, head_sha: str, base_sha: str, now: float | None = None) -> dict:
    """Fail-fast, one attempt. Verifies the P2 holder invariant (reservation open AND held by this lane)."""
    now = time.time() if now is None else now
    _rate_check(lane_id, now)
    cur = rs.current(arc_id)
    # P2 (C-HE-06 §7) is enforced HERE, at acquisition, as its text says ("Acquisition MUST verify the reservation
    # state"). During the G4 continuation the reservation legitimately reads `merged` (C-HE-03 §4 flips it on
    # confirmed merge) while the lease is still held through post-merge CI + the refresh -- so the §7 Invariants
    # bullet "no lease exists whose reservation is not open" cannot be read literally across the continuation
    # window. That wording mismatch is registered as a v1.1 change-note candidate (plan §6 item 13); it is NOT
    # silently absorbed. Nothing acquires against a non-open reservation.
    if cur is None or cur[1]["state"] != "open" or cur[1]["lane_id"] != lane_id:
        raise HolderInvariant(f"{arc_id}: reservation must be open and held by {lane_id} (P2); got {cur and cur[1]['state']!r} held by {cur and cur[1]['lane_id']!r}")
    if not (pr and head_sha and base_sha):
        raise LeaseError("pr, head_sha, base_sha are REQUIRED on the lease (C-HE-06 §3)")
    payload = {"lease_token": secrets.token_hex(16), "lane_id": lane_id, "reservation_id": arc_id, "pr": int(pr),
               "head_sha": head_sha, "base_sha": base_sha, "acquired_at": _now_iso(), "pid": os.getpid(),
               "host": socket.gethostname(), "merge_attempted_at": None, "state": "held", "blocked_at_sha": None, "blocked_reason": None}
    DOOR.mkdir(parents=True, exist_ok=True)
    try:
        publish_exclusive(LEASE, json.dumps(payload, sort_keys=True))
    except FileExistsError as exc:
        raise LeaseHeld("merge door held (cause_attribution: lease_contended)") from exc
    _kill_after("acquire")
    return payload


def win_marker(token: str, target_action: str, *, extra: dict | None = None) -> Path | None:
    """One marker per token, ever. The winner alone may move LEASE. `extra` (e.g. the reclaim's fresh lease) rides in
    the marker so a dead creator's declared action can be COMPLETED, not just archived (C-HE-06 §6 poison-pill)."""
    m = DOOR / f"transition.{token}"
    try:
        publish_exclusive(m, json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "target_action": target_action, "created_at": _now_iso(), **(extra or {})}, sort_keys=True))
        return m
    except FileExistsError:
        return None


def mark_attempted(lease: dict, *, suffix: str = "") -> None:
    """Payload CAS: temp + os.link onto a token-named sidecar BEFORE the merge request leaves the process."""
    name = ("refresh." if suffix == "refresh" else "") + "attempted"
    try:
        publish_exclusive(_sidecar(lease["lease_token"], name), json.dumps({"merge_attempted_at": _now_iso()}))
    except FileExistsError:
        pass  # already set: idempotent


def mark_blocked(lease: dict, *, sha: str, reason: str) -> None:
    try:
        publish_exclusive(_sidecar(lease["lease_token"], "blocked"), json.dumps({"blocked_at_sha": sha, "blocked_reason": reason, "blocked_at": _now_iso()}))
    except FileExistsError:
        pass


def _move_lease(token: str, dest_prefix: str) -> None:
    try:
        os.rename(LEASE, DOOR / f"{dest_prefix}.{token}")
    except FileNotFoundError:
        pass  # already moved: fail-closed idempotency (an os.rename on a moved source is "already done")


def release(lease: dict) -> None:
    if win_marker(lease["lease_token"], "release") is None:
        raise MarkerLost(f"lease {lease['lease_token']}: transition marker already taken -- stop driving, reconcile by ground truth")
    _move_lease(lease["lease_token"], "released")
    _kill_after("release")


def reclaim(lease: dict, *, lane_id: str, ground_state: str) -> dict:
    """Two-step: (1) the holder pid is PROVABLY dead on this host -- same-lane self-resume included (a live twin
    presenting the same lane_id must NOT displace a working holder; Codex round-2 P1); (2) caller-supplied ground truth
    (MERGED/OPEN from gh). Wins the OLD token's marker -- whose payload carries the FRESH lease so a crashed reclaimer
    can be completed idempotently by a third party -- moves LEASE aside, publishes the fresh LEASE (new token).
    Transfers merge-driving authority for `pr` -- never reservation ownership."""
    if lease["host"] != socket.gethostname() or _process_is_alive(int(lease["pid"])):
        raise LeaseError("holder is live or unverifiable; not reclaimable (self-resume requires the old pid to be dead)")
    if ground_state not in ("MERGED", "OPEN"):
        raise LeaseError(f"reclaim requires ground truth MERGED|OPEN, got {ground_state!r}")
    fresh = {**lease, "lease_token": secrets.token_hex(16), "lane_id": lane_id, "acquired_at": _now_iso(), "pid": os.getpid(),
             "host": socket.gethostname(), "state": "held", "blocked_at_sha": None, "blocked_reason": None}
    fresh.pop("refresh", None)
    if win_marker(lease["lease_token"], "reclaim", extra={"fresh_lease": fresh}) is None:
        raise MarkerLost("reclaim marker already taken")
    _move_lease(lease["lease_token"], "reclaimed")
    _publish_fresh(fresh)
    live = read_lease()
    if not live or live["lease_token"] != fresh["lease_token"]:
        # The move→publish window is not atomic on POSIX (no portable two-name swap): another acquirer may have taken
        # the momentarily-free door. NEVER adopt an unrelated lease (Codex round-3 P1): fail loud; the caller re-gates.
        raise LeaseError(f"reclaim lost the door to another acquirer (holder {live and live['lane_id']}); not resumed")
    return live


def _publish_fresh(fresh: dict) -> None:
    """Idempotent: a twin (or a third party completing our marker) may already have published this exact token."""
    try:
        publish_exclusive(LEASE, json.dumps(fresh, sort_keys=True))
    except FileExistsError:
        pass
    if fresh.get("merge_attempted_at"):
        try:
            publish_exclusive(_sidecar(fresh["lease_token"], "attempted"), json.dumps({"merge_attempted_at": fresh["merge_attempted_at"]}))
        except FileExistsError:
            pass


def unblock(*, pr: int, blocked_at_sha: str, lane_id: str) -> None:
    """Operator-confirmed reclaim through the marker CAS, keyed to blocked_at_sha. Never a path-only unlink."""
    lease = read_lease()
    if lease is None or lease.get("state") != "blocked":
        raise LeaseError("no blocked lease to unblock")
    if int(lease["pr"]) != int(pr) or lease.get("blocked_at_sha") != blocked_at_sha:
        raise LeaseError(f"unblock key mismatch: lease is pr={lease['pr']} blocked_at_sha={lease.get('blocked_at_sha')}")
    if win_marker(lease["lease_token"], "unblock") is None:
        raise MarkerLost("unblock marker already taken")
    _move_lease(lease["lease_token"], "reclaimed")


def complete_dead_marker(marker: Path) -> bool:
    """Poison-pill guard: a third party MAY complete a dead creator's declared target_action idempotently."""
    try:
        m = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if m["host"] != socket.gethostname() or _process_is_alive(int(m["pid"])):
        return False
    token = marker.name.removeprefix("transition.")
    action = m["target_action"]
    done = False
    if LEASE.exists() and json.loads(LEASE.read_text()).get("lease_token") == token:
        _move_lease(token, "released" if action == "release" else "reclaimed"); done = True
    if action == "reclaim" and "fresh_lease" in m:
        # The reclaimer may have died AFTER moving the old lease and BEFORE publishing the fresh one: finish it.
        # `_publish_fresh` is idempotent (FileExistsError = the fresh token, or a later acquirer, already holds the door).
        before = read_lease()
        _publish_fresh(m["fresh_lease"])
        after = read_lease()
        done = done or (before is None and after is not None and after["lease_token"] == m["fresh_lease"]["lease_token"])
    return done


def gc(*, now: datetime | None = None) -> list[Path]:
    now = now or datetime.now(UTC)
    removed = []
    if not DOOR.is_dir():
        return removed
    cutoff = now - timedelta(days=GC_KEEP_DAYS)
    for p in DOOR.iterdir():
        if p.name.startswith(("transition.", "released.", "reclaimed.", "LEASE.")) and datetime.fromtimestamp(p.stat().st_mtime, UTC) < cutoff:
            live = read_lease()
            if live and live["lease_token"] in p.name:
                continue
            p.unlink(); removed.append(p)
    att = DOOR / "attempts"
    if att.is_dir():
        for lane in att.iterdir():
            for f in lane.iterdir():
                try:
                    age = now.timestamp() - float(f.name)
                except ValueError:                # `.tmp` remnant of a crashed publish_exclusive (round-5 P2)
                    age = now.timestamp() - f.stat().st_mtime
                if age > 3600:
                    f.unlink(); removed.append(f)
    return removed
```
- [ ] **Step 4: GREEN**; probes: contention (`--lines` = the `except FileExistsError: raise LeaseHeld` clause) → note: removing it makes `publish_exclusive` raise `FileExistsError` — still a failure, still fail-fast; the pinned witness is `test_contention_fail_fast` expecting `LeaseHeld` specifically — PINNED; marker (`--lines` = the `win_marker` guard in `release`) → PINNED; third-party completion (`--lines` = `_move_lease` call in `complete_dead_marker`) → PINNED. Register `Row("C-HE-06", "pytest:tools/test_merge_door.py::test_lease_holder_invariant", "phase0", "local + CI", True)`, `::test_contention_fail_fast`, `::test_marker_race_exactly_one_wins`, `::test_rate_limit_sixth_refused` (all phase0). Store-audit re-run green.
- [ ] **Step 5: Commit** — `git add tools/merge_door.py tools/test_merge_door.py tools/codex-parity-check.sh tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-22 merge-door lease primitive — fail-fast acquire, transition marker, reclaim/unblock, poison-pill (C-HE-06 §2/§3/§6/§7)"`.

**Rev 2026-08-20 (U-HE-22 execution corrections, as-built — primitive half).** *(i) Contention test defect in the sketch:* `_acq(lane="B", arc="pr-1")` trips the P2 HolderInvariant on A's arc BEFORE reaching the door and never exercises contention; the as-built test reserves+opens `pr-2` for B so B passes P2 and hits the held LEASE (LeaseHeld). *(ii) Deterministic deadness:* the fixture patches `md._process_is_alive` to "alive iff this process" — the sketch's fixed sentinel pid 999999 can be a LIVE pid where pid_max exceeds it (Linux CI default 4194304; the U-HE-20 r2 P3 class), so the sentinel never reaches the real `kill(0)` probe. *(iii) tools-test import idiom:* `sys.path.insert(0, <tools dir>)` before `import merge_door` (the repo's `--import-mode=importlib` posture; same as test_reservations), and `tools/test_merge_door.py` is registered in `codex-parity-check.sh`'s named pytest list. *(iv) Module surface:* imports trimmed to the primitive half's actual uses (U-HE-23 re-adds `subprocess`/`ci_is_green`/`REPO` etc. with the land driver); the §4/§8 constants (`MERGE_TIMEOUT_S`, bounds, `BACKOFF`, `KILL_STEPS`) land now as declared; exception names keep the plan's verbatim signatures under `# noqa: N818`, the `reservations.py` precedent. *(v) Probe re-siting (deletion-expressibility):* the contention pin is the `raise LeaseHeld` line alone (its `msg =` line keeps the except-block syntactically valid, so the mutation falls through to a successful-looking return — killable); the third-party-completion pin is the `_move_lease` line alone (the sketch's two-line range emptied the if-body and was refused); reclaim's foreign-adoption and crashed-reclaimer annotations are re-shaped to deletion-expressible ranges (the post-publish token re-check; the `_publish_fresh` completion line). 7 probes PINNED, coverage 0. *(vi) Store audit:* `tools/merge_door.py` promoted PENDING→LANDED in `test_store_audit.py` (its path literals were pre-declared at the U-HE-14/v1.3 audit; the audit re-runs green). *(vii) Spec v1.4 discharge (same PR):* the four registered wording classes routed to this landing — flip-timing (C-HE-03 §4 merge-lane pre-acquire opener + C-HE-06 acquisition-scoped open-ness invariant, closing plan §6 item 13's candidate), merged-gate carve-out (C-HE-03 §6 + C-HE-04 §2(ii)), keep-loudly/converge (C-HE-04 §5 + Verification (vi), incl. the MERGED_REF freshness bound + merged-headless stall), and the §8.1 six-interleavings count — land as the spec v1.4 change-note with marker `spec-he-loop-lanes-v1.4-cleared-2026-08-20.md`; `acquire()`'s P2 comment now cites the v1.4 note instead of an unresolved candidate.* *(viii) Review-round corrections (codex r1–r2, each witnessed + probe-pinned):* reclaim AND release adjudicate from the PERSISTED lease, never the caller's dict (a copied dict with a substituted pid must not displace a live holder; a stale dict must never move ANOTHER lane's lease aside — `_move_lease` renames the CURRENT LEASE while the marker namespace is per-token); a `blocked` door resumes/releases ONLY through the operator-keyed `unblock` (both verbs refuse with `DoorBlocked`); reclaim re-checks the LINKED RESERVATION (`open`/`merged` only — an operator-abandoned arc whose PR is still OPEN on GitHub must not regain merge authority); the reclaim self-resume preserves the refresh continuation as sidecars under the new token (`_publish_fresh` strips view keys from the base payload and republishes `refresh` + `refresh.attempted`); dead-marker completion is ground-truth-gated to `open` OR `merged` (r1's open-only gate would refuse a post-merge reclaimer crash mid-continuation and let another lane acquire — the r2 correction of an r1 fix; the residual stale-head window is bounded by the land driver's step-(ii) re-verification, U-HE-23); `_rate_check` validates lane_id containment (a path-escaping lane id would relocate the attempts store — the non-kill of the first containment probe was itself a finding: the weak witness accepted any downstream LeaseError, and the probe run leaked /tmp/evil, cleaned) and records-then-counts (threshold `> RATE_K`) so a concurrent burst cannot under-count a not-yet-recorded peer (the limiter bounds sustained rates; the LEASE CAS is the safety fence); `gc()` never follows attempts symlinks (a planted `attempts/<lane>` symlink could delete files outside QUEUE_DIR) and tolerates concurrent-collector `FileNotFoundError` per the log-and-yield idiom. 15 probes PINNED, coverage 0.* *(ix) Round-3 corrections (codex r3, each witnessed + probe-pinned):* `unblock()` mints a REPLACEMENT lease for the named lane (the door typically blocks DURING the §4(vii)–(viii) continuation, when the reservation reads `merged` and `acquire()` refuses — clearing without a successor stranded the continuation behind an unacquirable door; the continuation sidecars carry over, a lane wanting the door free releases the successor normally, and `complete_dead_marker` completes dead unblock markers exactly like reclaim ones); the reclaim reservation gate reads the PERSISTED `reservation_id` (a caller keeping the valid token but substituting another active arc's id bypassed the terminated-arc refusal); `gc()` also refuses a planted symlink at `attempts/` ITSELF (the r2 fix covered only per-lane symlinks — its ordinary-looking children passed the lane check while living outside QUEUE_DIR; the guard is restructured `if att.is_symlink(): att = None` for deletion-expressibility); `acquire()` re-validates the reservation AFTER the exclusive create (the pre-check and publish are separate operations — a concurrent reconciliation terminalizing in between left a live lease on a terminal arc; on divergence the fresh lease self-releases through the marker discipline, never a path-only unlink); `_move_lease` re-stamps the history file's mtime (rename preserved the ACQUISITION mtime, so a >30 d-blocked lease was GC-eligible the moment its transition completed — the retention clock now starts at the transition). Second non-kill finding: the r3 post-publication re-validation made the P2 PRE-check redundant to its witness (both raise HolderInvariant) — the witness now discriminates by asserting the pre-check path never publishes (no self-heal `released.*` artifact), same one-enforcement-point discipline as the r1 lane-id non-kill. 18 probes PINNED, coverage 0.* *(x) Round-4 corrections (codex r4, each witnessed + probe-pinned):* `_publish_fresh` publishes SIDECARS FIRST, LEASE LAST (a crash between a published LEASE and its not-yet-republished sidecars presented an apparently-refresh-free lease — a later self-resume would lose then re-issue the recorded refresh/attempt state; token-named sidecars ahead of their LEASE are invisible orphans, so the order is crash-safe both ways; the refresh-sidecar publisher is hoisted to `_publish_refresh_sidecars` for probe deletion-expressibility); `acquire()` cross-checks its inputs against the reservation's back-filled merge tuple (C-HE-03 §3 — unrelated caller inputs or a not-yet-back-filled reservation refuse; the reservation-to-lease authority link); `unblock` accepts the REFRESH continuation's PR as its key too (a §4(viii) refresh-CI block records the refresh PR, and the documented recovery command passes that number — the lease-pr-only key always mismatched); `_rate_check` mirrors gc's parent-symlink refusal on the WRITE path (a planted `attempts` symlink must not receive attempt files or lend rate authority); the marker action set is CLOSED at both ends (`win_marker` refuses unknown actions; `complete_dead_marker` fails closed on a malformed persisted marker instead of archiving a live lease as a pseudo-reclaim); `_move_lease` re-stamps the token's SIDECARS alongside the history file (same r3 P3 retention clock). *HELD (r4 P1a, plan-documented design):* the reclaim/unblock move→publish window transiently exposes an empty door — POSIX offers no two-name atomic swap; the landed design is the documented fail-loud + caller-re-gate (`lost the door` — witnessed by `test_reclaim_never_adopts_a_foreign_lease`), and an acquirer that slips in holds a VALID lease for its own arc (per-arc serialization is the reservation holder invariant, not the door's emptiness). 23 probes PINNED, coverage 0.* *(xi) Round-5 corrections (codex r5, each witnessed + probe-pinned):* `complete_dead_marker` serializes concurrent completers on an exclusive-create `completed.<token>` claim (two callers could both validate the old token — the loser's rename after a foreign acquire would strip the NEW holder's live fence; the claim is taken after the cheap refusals so a refused completion stays retryable, and gc ages `completed.*` with the history); `unblock` re-checks the linked reservation exactly like reclaim (a blocked-then-abandoned arc could regain merge authority); `gc` refuses a symlinked `merge-door` DIR itself (`is_dir()` follows links — a planted QUEUE_DIR/merge-door symlink could delete history outside QUEUE_DIR); `_rate_check` refuses a symlinked `attempts/<lane>` too (`mkdir(exist_ok=True)` follows a symlink-to-dir silently). *HELD (r5 P1a re-raise of the r4-held move→publish window):* unchanged adjudication — POSIX has no two-name atomic swap; fail-loud + caller-re-gate is the plan-documented design and per-arc serialization rests on the reservation holder invariant. 27 probes PINNED, coverage 0.* *(xii) Round-6 corrections (codex r6, each witnessed + probe-pinned):* the PRODUCTION `pending→open` flip lands in ship-pr's final-gate block (`reservations.py transition --to open` after the attested-tuple back-fill, pre-acquire — wiring the v1.4 X4a sentence into its carrier; the reviewer correctly held that the spec wording alone left normal arcs failing `acquire()`'s HolderInvariant with only the test fixture's `open_with_sensor` masking it; the CLI transition skips the `derived`-optional §7 sensor; skip clauses for unreserved/already-open passes; witnessed by the wiring witness's new needle); `_check_door()` refuses a symlinked `merge-door` root on EVERY write path (rate store, markers, sidecars — gc's read-side guard alone left the writers following the link); `complete_dead_marker` BREAKS a provably-dead claimant's completion claim (a completer dying between claim and act would have stranded completion forever — same poison-pill logic as the marker; the next pass completes); the `completed.<token>` family is filed in the C-HE-30 store audit (sole carrier of the completion-claim fact; the audit's literal-coverage test caught the omission deterministically). 29 probes PINNED, coverage 0.* *(xiii) Round-7 corrections (codex r7, each witnessed + probe-pinned):* the dead-claimant claim break is an ADJUDICATE-AFTER-RENAME CAS (read-dead-then-unlink-by-path let a second breaker unlink a freshly recreated LIVE claim; the rename is the atomic single-winner, the moved bytes are the adjudicated evidence, a displaced live claim is restored via `os.link` which yields politely to any newer claim; the `.completed.<token>.broken.*` aside is filed in the audit's ephemeral table); gc retires a `transition.<token>` marker STRICTLY BEFORE its `completed.<token>` tombstone (pass-start marker snapshot — same-pass iterdir order or a mid-pass crash could leave the dead marker executable with its tombstone gone: stale-authority resurrection; the tombstone goes on the next pass); the attempts junk-sweep tolerates a concurrent cleaner's `FileNotFoundError` (log-and-yield). 31 probes PINNED, coverage 0.* *(xiv) Round-8 corrections (codex r8, each witnessed + probe-pinned):* a `_retract_if_terminal` post-publish re-check lands at ALL THREE successor-publishing sites (reclaim / unblock / dead-marker completion) — the reservation gate is check-then-act, so an arc terminalized between `rs.current()` and the successor publish now self-releases through the marker discipline instead of standing with restored authority (the mirror of acquire's r3 post-publication re-validation); the claim-break gains a LIVE-PRE-READ gate before the r7 rename-CAS (renaming a believed-live claim would blank the pathname and admit a third concurrent completer; only a read-dead claim enters the rename, whose moved bytes are then re-adjudicated); `complete_dead_marker` gains the containment preamble (`_check_door()` + symlink refusal + DOOR-parent resolve + `transition.` name check — a planted symlink named like a marker could move the current LEASE without a legitimate `win_marker` publication). 33 probes PINNED, coverage 0.* *(xv) Terminal round (codex r9–r10 — register-and-hold at the 10-round cap):* absorbed — the §7 Contract sentence aligned to the v1.4 X4a acquisition-scoped reading (the change-note's Invariants edit had left the same contradiction at its second carrier); `refresh.attempted` filed in the audit's sidecar family; an uncompleted completion claim is RELINQUISHED (a live completer's retained claim after a lost publish would refuse every later pass until process turnover — and the reservation-gate early-return that bypassed the relinquish tail was itself caught by the new witness assertion); a parseable-but-malformed marker (missing keys / non-integer pid) refuses fail-closed instead of aborting reconciliation. HELD classes at terminal (adjudication to the 3-lens merge gate): (a) the move→publish empty-door window — 5th raise; POSIX two-name-swap limit, plan-documented fail-loud + caller-re-gate, per-arc serialization rests on the reservation holder invariant; (b) the completion-claim break ABA chain (r5→r7→r8→r10 — each hardening narrows the interleaving that defeats it; the residual is a THREE-actor recovery-path-only race, bounded in production by the arc-serial door-reconcile venue, and the hardening sequence exhibits the workspace's documented non-convergent adversarial-hardening arms race — further rounds trade one interleaving for another); (c) symlink-swap TOCTOU between the containment checks and path use — closing it requires dirfd/O_NOFOLLOW filesystem plumbing across every store writer, a REGISTERED design candidate for the U-HE-23 landing (which owns the door's remaining I/O surface), not an incremental guard; (d) the sequential marker-race witness — the REAL concurrency witnesses for the door are U-HE-23's AC#2(c) subprocess crash-resume suite per the plan's own Step-1 (second half), where the marker race is exercised cross-process. 34 probes PINNED, coverage 0.* *(xvi) Merge-gate round 1 (PR #1413; concurrency BLOCK + witness BLOCK + spec APPROVE), each fix witnessed:* `_move_lease` re-stamps the SOURCE (LEASE + sidecars) BEFORE the rename, so the history record is BORN with the fresh transition-time mtime — the r3/r4 re-stamp fix had itself opened a rename→utime window in which a concurrent gc() could stat a >30d-stale dest and unlink the record early, plus an unguarded FileNotFoundError (concurrency P2; sidecars are stamped while the live-lease guard still protects them); the cross-host liveness branch is now WITNESSED at all its surfaces (witness P2: every prior fixture set host=gethostname(), so collapsing the host-mismatch OR-clause to pid-only liveness — a split-brain enabler — passed all 44 tests; `_forge_holder` forges a foreign host at the lease, the marker, and the claim). REGISTERED (concurrency P3 → U-HE-23, which owns the door's driver flow): `mark_blocked` publishes outside the transition-marker namespace, so a block landing between release/reclaim's persisted-state read and its `win_marker` is silently overridden — a post-win re-verify would brick the one-shot token on refusal, so the fix belongs in the driver's ordering (or a marker-namespace-aware blocked publication), not an incremental guard here. 35 probes PINNED, coverage 0.*

---

### U-HE-23: Merge-door landing steps (ii)–(ix), reconcile, `MERGE_DOOR_TEST_KILL_AFTER`, caller backoff policy, gate rows, attestation + cross-carrier `NOTIFY`

**Scope.** Add the landing driver `land(pr, …)`: verify head/base against `gh`, `local-base-cas-check` (`git merge-tree --write-tree origin/main <head_sha>` byte-equal to the reservation's `attested_merge_tree`), `mark_attempted` before a bounded 120 s `gh pr merge <pr> --squash --match-head-commit <head_sha>`, confirm via `gh pr view`, flip the reservation to `merged`, hold the lease through the merge SHA's own `main` run (45 min) and drive the terminating refresh as a continuation under the same lease (45 min), release; timeout/crash reconciliation that never re-issues after MERGED and re-issues at most once per pass on OPEN; the caller's backoff+jitter policy; the §9 gate rows as C-HE-24 findings; attestation-tier and cross-carrier `NOTIFY`s; CI queue-depth `NOTIFY`.

**Spec linkage.** C-HE-06 §1 (acquire before construct), §4 (steps i–ix; CI concurrency NOTIFY), §5 (reconcile), §8 (caller policy: yield + numbers), §9 (gate rows), §10 (tiering + cross-carrier), Invariants (never re-invoke after MERGED; `merge_attempted_at` before first byte); C-HE-19 §2 (`ci_is_green`; CANCELLED blocks the door); C-HE-03 §4 (`open→merged` on confirmed merge); C-HE-12 §2 (`BASE_TOCTOU` first-parent check emitted here after merge — the detection code lands in U-HE-33; this unit calls the hook if present).

**Files.** Modify `tools/merge_door.py`, `tools/test_merge_door.py`, `tools/lanes_verify.py`.

**Interfaces.**
```python
@dataclass class Ground:  # injected gh/git; production defaults shell out with bounded timeouts
    gh_view: Callable[[int], dict]                              # {state, mergedAt, headRefOid, baseRefOid, mergeCommit:{oid}}
    gh_merge: Callable[[int, str, float], subprocess.CompletedProcess]
    gh_runs_for_sha: Callable[[str], list[dict]]                # [{status, conclusion, event}]
    gh_main_runs_in_progress: Callable[[], int]
    git_merge_tree: Callable[[str, str], str]
    git_first_parent: Callable[[str], str]
    clock: Callable[[], float] = time.monotonic; sleep: Callable[[float], None] = time.sleep
    codex_worktree_present: Callable[[], bool]
def local_base_cas_check(head_sha, attested_tree, ground) -> None
def verify_head_base(lease, ground) -> None
def wait_post_merge_ci(sha, ground, *, bound_s) -> str          # "success" | "blocked:<reason>"
def reconcile(lease, ground) -> str                             # "MERGED" | "OPEN"
def land(pr, *, lane_id, arc_id, ground, refresh: Callable[[], tuple[int, str]] | None, emit=...) -> str
def wait_for_door(try_acquire, *, clock, sleep, rng) -> dict    # §8 numbers; raises BudgetExhausted
def default_ground() -> Ground
```

**Depends on.** U-HE-22, U-HE-08, U-HE-18, U-HE-29 (NOTIFY rows), U-HE-01 (gate rows).

- [ ] **Step 1: Failing tests** (second half of `tools/test_merge_door.py`):
```python
class FakeGround:
    """In-memory gh/git with a call log; `merge_calls` is THE mutation-probe target for 'never re-issue after MERGED'."""
    def __init__(self, *, head="h"*40, base="b"*40, tree="t"*40, hang_merge=False, ci="success"):
        self.state = {"state": "OPEN", "headRefOid": head, "baseRefOid": base, "mergedAt": None, "mergeCommit": None}
        self.tree, self.hang_merge, self.ci, self.merge_calls, self.t = tree, hang_merge, ci, [], 0.0
        self.notifies = []
    def gh_view(self, pr): return dict(self.state)
    def gh_merge(self, pr, head, timeout):
        self.merge_calls.append((pr, head))
        if self.hang_merge:
            self.t += timeout + 1; raise subprocess.TimeoutExpired("gh", timeout)
        self.state.update(state="MERGED", mergedAt="now", mergeCommit={"oid": "m"*40}); return subprocess.CompletedProcess([], 0, "", "")
    def gh_runs_for_sha(self, sha): return [{"status": "completed", "conclusion": self.ci, "event": "push"}]
    def gh_main_runs_in_progress(self): return 1
    def git_merge_tree(self, base, head): return self.tree
    def git_first_parent(self, sha): return self.state["baseRefOid"]
    def clock(self): return self.t
    def sleep(self, s): self.t += s
    def codex_worktree_present(self): return False


def _land(door, g, **kw):
    rs.update_payload("pr-1", {"pr": 1, "head_sha": "h"*40, "base_sha": "b"*40, "attested_merge_tree": "t"*40})
    return md.land(1, lane_id="A", arc_id="pr-1", ground=g, refresh=None, **kw)


def test_happy_path_lands_holds_through_ci_and_releases(door):
    g = FakeGround()
    assert _land(door, g) == "released"
    assert g.merge_calls == [(1, "h"*40)] and rs.current("pr-1")[1]["state"] == "merged" and md.read_lease() is None


def test_local_base_cas_check_fails_door_on_tree_mismatch(door):
    g = FakeGround(tree="x"*40)
    with pytest.raises(md.DoorFailed, match="attested"):
        _land(door, g)
    assert g.merge_calls == [] and md.read_lease() is None    # released via §6, re-gate


def test_head_base_mismatch_releases_and_regates(door):
    g = FakeGround(head="z"*40)
    with pytest.raises(md.DoorFailed, match="head/base"):
        _land(door, g)
    assert md.read_lease() is None


# mutation-probe: drop the `if state == "MERGED": ... return` guard in reconcile() (blind re-issue)
def test_timeout_reconcile_merged_calls_once(door):
    """gh pr merge hangs past 120 s but the server landed it: ground truth MERGED -> call log stays 1."""
    g = FakeGround()
    def merge_hang(pr, head, timeout):
        g.merge_calls.append((pr, head))
        g.state.update(state="MERGED", mergedAt="now", mergeCommit={"oid": "m"*40})
        g.t += timeout + 1
        raise subprocess.TimeoutExpired("gh", timeout)
    g.gh_merge = merge_hang
    assert _land(door, g) == "released"
    assert len(g.merge_calls) == 1                              # never re-issued after MERGED


def test_timeout_reconcile_open_reissues_exactly_once(door):
    g = FakeGround()
    n = {"k": 0}
    def merge_first_hangs(pr, head, timeout):
        n["k"] += 1; g.merge_calls.append((pr, head))
        if n["k"] == 1:
            g.t += timeout + 1; raise subprocess.TimeoutExpired("gh", timeout)
        g.state.update(state="MERGED", mergedAt="now", mergeCommit={"oid": "m"*40}); return subprocess.CompletedProcess([], 0, "", "")
    g.gh_merge = merge_first_hangs
    assert _land(door, g) == "released" and len(g.merge_calls) == 2


# mutation-probe: decide from the in-memory `lease` dict instead of read_lease() in the DoorFailed handler
def test_failure_after_attempt_blocks_never_releases(door):
    """Both merge attempts time out and ground truth stays OPEN → reissue exhausted AFTER the attempted marker:
    the door must BLOCK (HITL), not release (Codex round-2 P1)."""
    g = FakeGround()
    def always_hang(pr, head, timeout):
        g.merge_calls.append((pr, head)); g.t += timeout + 1; raise subprocess.TimeoutExpired("gh", timeout)
    g.gh_merge = always_hang
    with pytest.raises(md.DoorBlocked, match="merge_reissue_exhausted"):
        _land(door, g)
    l = md.read_lease(); assert l is not None and l["state"] == "blocked" and len(g.merge_calls) == 2


def test_inflight_first_attempt_then_reissue(door):
    """T6: a delayed first landing -- exactly one MERGED outcome; nothing proceeds past (v) on an error path."""
    g = FakeGround()
    def delayed(pr, head, timeout):
        g.merge_calls.append((pr, head)); g.t += timeout + 1
        # server lands it a moment AFTER our timeout fires:
        g.state.update(state="MERGED", mergedAt="later", mergeCommit={"oid": "m"*40})
        raise subprocess.TimeoutExpired("gh", timeout)
    g.gh_merge = delayed
    assert _land(door, g) == "released" and len(g.merge_calls) == 1


# mutation-probe: make wait_post_merge_ci treat "cancelled" as green (drop the ci_is_green gate)
def test_post_merge_ci_blocked_and_unblock(door):
    g = FakeGround(ci="cancelled")
    with pytest.raises(md.DoorBlocked):
        _land(door, g)
    l = md.read_lease(); assert l["state"] == "blocked" and l["blocked_reason"] == "post_merge_ci_not_green"
    md.unblock(pr=1, blocked_at_sha=l["blocked_at_sha"], lane_id="A")
    assert md.read_lease() is None


# mutation-probe: drop the mark_blocked/raise after the first-parent mismatch (emit-only)
def test_base_toctou_blocks_door(door):
    g = FakeGround()
    g.git_first_parent = lambda sha: "z" * 40                    # landed on a base other than the verified one
    with pytest.raises(md.DoorBlocked, match="base_toctou"):
        _land(door, g)
    l = md.read_lease(); assert l["state"] == "blocked" and l["blocked_reason"] == "base_toctou_first_parent_mismatch"
    assert rs.current("pr-1")[1]["state"] == "merged"           # the fact is recorded; the DOOR is what blocks


def test_refresh_ci_failure_emits_hitl_and_blocks(door, monkeypatch):
    rows = []; monkeypatch.setattr(rs, "emit_loop_row", lambda k, l, c, d: rows.append((k, c)))
    g = FakeGround()
    rs.update_payload("pr-1", {"pr": 1, "head_sha": "h"*40, "base_sha": "b"*40, "attested_merge_tree": "t"*40})
    calls = {"n": 0}
    def runs(sha):
        calls["n"] += 1
        return [{"status": "completed", "conclusion": "success" if calls["n"] == 1 else "failure", "event": "push"}]   # main run green, refresh run red
    g.gh_runs_for_sha = runs
    def refresh():
        g.state = {"state": "OPEN", "headRefOid": "r"*40, "baseRefOid": "m"*40, "mergedAt": None, "mergeCommit": None}; return 2, "r"*40
    with pytest.raises(md.DoorBlocked):
        md.land(1, lane_id="A", arc_id="pr-1", ground=g, refresh=refresh)
    assert md.read_lease()["blocked_reason"] == "refresh_ci_not_green"
    assert ("DEFERRED-HIL", "merge-door-post-merge-ci:HITL-recoverable:refresh_ci_not_green") in rows


# mutation-probe: force a second acquire() call before the refresh PR merge
def test_continuation_no_reacquire(door, monkeypatch):
    g = FakeGround()
    acquires = []
    real = md.acquire
    monkeypatch.setattr(md, "acquire", lambda **kw: (acquires.append(1), real(**kw))[1])
    rs.update_payload("pr-1", {"pr": 1, "head_sha": "h"*40, "base_sha": "b"*40, "attested_merge_tree": "t"*40})
    def refresh():
        g.state = {"state": "OPEN", "headRefOid": "r"*40, "baseRefOid": "m"*40, "mergedAt": None, "mergeCommit": None}
        return 2, "r"*40
    assert md.land(1, lane_id="A", arc_id="pr-1", ground=g, refresh=refresh) == "released"
    assert acquires == [1] and [c[0] for c in g.merge_calls] == [1, 2]


# mutation-probe: drop the `recorded is not None` branch (always call refresh())
def test_resume_uses_recorded_refresh_never_a_second_pr(door, monkeypatch):
    g = FakeGround()
    calls = []
    rs.update_payload("pr-1", {"pr": 1, "head_sha": "h"*40, "base_sha": "b"*40, "attested_merge_tree": "t"*40})
    def refresh():
        calls.append(1); g.state = {"state": "OPEN", "headRefOid": "r"*40, "baseRefOid": "m"*40, "mergedAt": None, "mergeCommit": None}; return 2, "r"*40
    monkeypatch.setenv("MERGE_DOOR_TEST_KILL_AFTER", "refresh-attempted")
    monkeypatch.setattr(md.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))   # in-process stand-in for the kill
    with pytest.raises(SystemExit):
        md.land(1, lane_id="A", arc_id="pr-1", ground=g, refresh=refresh)
    monkeypatch.delenv("MERGE_DOOR_TEST_KILL_AFTER")
    lease = md.read_lease(); assert lease["refresh"] == {"pr": 2, "head_sha": "r"*40, "merge_attempted_at": lease["refresh"]["merge_attempted_at"]}
    g.gh_merge = lambda pr, head, timeout: (g.state.update(state="MERGED", mergedAt="now", mergeCommit={"oid": "m"*40}), subprocess.CompletedProcess([], 0, "", ""))[1]
    assert md.land(1, lane_id="A", arc_id="pr-1", ground=g, refresh=refresh, lease=lease) == "released"
    assert calls == [1]                                              # refresh() called ONCE across crash + resume


def test_wait_for_door_backoff_numbers_and_budget(door):
    t = {"now": 0.0}; sleeps = []
    def try_acquire():
        raise md.LeaseHeld("held")
    with pytest.raises(md.BudgetExhausted, match="lease_acquire_budget_exhausted"):
        md.wait_for_door(try_acquire, clock=lambda: t["now"], sleep=lambda s: (sleeps.append(s), t.__setitem__("now", t["now"] + s)), rng=lambda: 1.0)
    assert len(sleeps) == 11 and sleeps[0] == 30.0 and sleeps[1] == 60.0 and max(sleeps) == 600.0
    # rate-limit refusals do not count against the 12
    k = {"n": 0}
    def rl():
        k["n"] += 1
        if k["n"] <= 3: raise md.RateLimited("rate")
        raise md.LeaseHeld("held")
    sleeps.clear()
    with pytest.raises(md.BudgetExhausted):
        md.wait_for_door(rl, clock=lambda: t["now"], sleep=lambda s: sleeps.append(s), rng=lambda: 1.0)
    assert len(sleeps) == 11 + 3   # 3 rate-limited waits happen but don't consume the 12 attempts


def _fake_gh(bindir: Path, state: Path, log: Path, tree: str) -> None:
    """A `gh` + `git` shim on PATH: pr view answers from state.json; pr merge appends to merge-calls.log and flips
    state.json to MERGED; run list reports success; git merge-tree returns the attested tree; other git calls pass through."""
    (bindir / "gh").write_text(f"""#!/usr/bin/env bash
case "$*" in
  *"pr view"*"headRefOid"*) cat "{state}" ;;
  *"pr view"*"state,mergedAt"*) cat "{state}" ;;
  *"pr merge"*) echo "$*" >> "{log}"; python3 - <<'PY2'
import json; p="{state}"; s=json.load(open(p)); s.update(state="MERGED", mergedAt="now", mergeCommit={{"oid": "m"*40}}); json.dump(s, open(p, "w"))
PY2
  ;;
  *"run list"*"--commit"*) echo '[{{"status":"completed","conclusion":"success","event":"push"}}]' ;;
  *"run list"*"in_progress"*) echo '[]' ;;
  *) echo "fake gh: unhandled $*" >&2; exit 1 ;;
esac
""")
    (bindir / "git").write_text(f"""#!/usr/bin/env bash
if [ "$1" = "-C" ]; then shift 2; fi
case "$1 $2" in
  "merge-tree --write-tree") echo "{tree}" ;;
  "rev-parse "*) echo "{'b'*40}" ;;
  "worktree list") echo "worktree /x" ;;
  *) exec /usr/bin/git "$@" ;;
esac
""")
    for f in ("gh", "git"):
        (bindir / f).chmod(0o755)


def _land_cmd(kill: str | None) -> list[str]:
    return [sys.executable, str(TOOLS / "merge_door.py"), "land", "1", "--lane-id", "A", "--arc-id", "pr-1", "--no-refresh"]


@pytest.mark.parametrize("kill,expect_merge_calls,resume_state", [
    ("attempted", 1, "released"),          # killed after merge_attempted_at, before merge → restart sees OPEN → exactly one merge call
    ("confirm", 1, "released"),            # killed after merge success, before (vi) → restart sees MERGED → continues; call log 1
    ("reservation-merged", 1, "released"), # killed after (vi), before (vii) → lease still held; restart resumes the CI wait
    ("release", 1, "no-lease"),            # killed after release → restart finds no lease: nothing to land (rc 0)
])
def test_ac2_c_crash_resume(door, tmp_path, monkeypatch, kill, expect_merge_calls, resume_state):
    """AC#2(c): real subprocess killed at the named step (os._exit 137), then resumed; the merge is issued at most once."""
    q = door
    bindir = tmp_path / "bin"; bindir.mkdir(); state = tmp_path / "state.json"; log = tmp_path / "merge-calls.log"
    state.write_text(json.dumps({"state": "OPEN", "headRefOid": "h"*40, "baseRefOid": "b"*40, "mergedAt": None, "mergeCommit": None}))
    _fake_gh(bindir, state, log, "t"*40)
    rs.update_payload("pr-1", {"pr": 1, "head_sha": "h"*40, "base_sha": "b"*40, "attested_merge_tree": "t"*40})
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "ARC_METRICS_QUEUE_DIR": str(q), "PYTHONPATH": str(TOOLS), "HARNESS_LANE_ID": "A"}
    p1 = subprocess.run(_land_cmd(kill), env={**env, "MERGE_DOOR_TEST_KILL_AFTER": kill}, capture_output=True, text=True, timeout=120)
    assert p1.returncode == 137, p1.stderr
    if kill == "reservation-merged":
        assert md.read_lease() is not None and rs.current("pr-1")[1]["state"] == "merged"
    p2 = subprocess.run(_land_cmd(None), env=env, capture_output=True, text=True, timeout=120)
    assert p2.returncode == 0, p2.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert len(calls) == expect_merge_calls, calls
    if resume_state == "released":
        assert md.read_lease() is None and any(md.DOOR.glob("released.*"))
    else:
        assert "nothing to land" in (p2.stdout + p2.stderr)
```
(`TOOLS = Path(__file__).resolve().parent`; the CLI's `land` self-resumes when it finds this lane's lease with a dead pid — `reclaim(..., ground_state=reconcile(...))` — and prints `nothing to land` + exits 0 when no lease exists and the reservation is already `merged`.)
- [ ] **Step 2: AC#2(c) harness** — the `_fake_gh` shim + parametrized crash-resume test above are the complete body (no prose stand-in): four kill points, rc 137 asserted on the killed run, rc 0 on the resume, `merge-calls.log` line count == 1 in every case, lease state asserted per kill point.
- [ ] **Step 3: RED**; **Step 4: Implement (landing half of `merge_door.py`)**
```python
class BudgetExhausted(LeaseError): ...


@dataclass
class Ground:
    gh_view: Callable[[int], dict]
    gh_merge: Callable[[int, str, float], subprocess.CompletedProcess]
    gh_runs_for_sha: Callable[[str], list[dict]]
    gh_main_runs_in_progress: Callable[[], int]
    git_merge_tree: Callable[[str, str], str]
    git_first_parent: Callable[[str], str]
    codex_worktree_present: Callable[[], bool]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _gh(*args: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=REPO, capture_output=True, text=True, timeout=timeout)


def default_ground() -> Ground:
    def gh_view(pr):
        p = _gh("pr", "view", str(pr), "--json", "state,mergedAt,headRefOid,baseRefOid,mergeCommit", timeout=30)
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError(f"gh pr view failed: {p.stderr.strip()}")
        return json.loads(p.stdout)
    def gh_merge(pr, head, timeout):
        return _gh("pr", "merge", str(pr), "--squash", "--match-head-commit", head, timeout=timeout)   # the ONE fixed string (C-HE-07)
    def gh_runs_for_sha(sha):
        p = _gh("run", "list", "--commit", sha, "--workflow", "CI", "--json", "status,conclusion,event", "--limit", "20", timeout=30)
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else []
    def gh_main_runs_in_progress():
        p = _gh("run", "list", "--branch", "main", "--event", "push", "--status", "in_progress", "--json", "databaseId", timeout=30)
        return len(json.loads(p.stdout)) if p.returncode == 0 and p.stdout.strip() else 0
    def git_merge_tree(base, head):
        return subprocess.run(["git", "-C", str(REPO), "merge-tree", "--write-tree", base, head], capture_output=True, text=True, check=True).stdout.split()[0]
    def git_first_parent(sha):
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", f"{sha}^1"], capture_output=True, text=True, check=True).stdout.strip()
    def codex_worktree_present():
        out = subprocess.run(["git", "-C", str(REPO), "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
        return "/.codex-worktrees/" in out
    return Ground(gh_view, gh_merge, gh_runs_for_sha, gh_main_runs_in_progress, git_merge_tree, git_first_parent, codex_worktree_present)


def _emit_gate(lease: dict | None, *, gate: str, fail_class: str, cause: str, evidence: str, arc_id: str, lane_id: str, severity: str = "warn") -> None:
    """§9 gate rows as C-HE-24 findings (`code` = <gate>:<fail_class>:<cause>)."""
    import finding_record as fr
    core = fr.FindingCore(fr.make_finding_id(gate, (lease or {}).get("head_sha") or "nohead", gate, 0), "merge-door", evidence,
                          "C-HE-06 §9", severity, fail_class, "door", gate)
    fr.append_row(fr.make_row(core, fr.Envelope("finding", fr.now_iso(), arc_id, lane_id, (lease or {}).get("head_sha"),
                                                (lease or {}).get("base_sha"), None, None, cause_attribution=cause)))


def local_base_cas_check(head_sha: str, attested_tree: str | None, ground: Ground) -> None:
    tree = ground.git_merge_tree("origin/main", head_sha)
    if not attested_tree or tree != attested_tree:
        raise DoorFailed(f"local-base-cas-check: merge-tree {tree[:12]} != attested {str(attested_tree)[:12]} -- base moved; re-gate (R-23)")


def verify_head_base(lease: dict, ground: Ground) -> dict:
    v = ground.gh_view(int(lease["pr"]))
    if v.get("headRefOid") != lease["head_sha"] or v.get("baseRefOid") != lease["base_sha"]:
        raise DoorFailed(f"pr #{lease['pr']} head/base moved since the lease was recorded; re-gate")
    return v


def wait_post_merge_ci(sha: str, ground: Ground, *, bound_s: float, lane_id: str = "") -> str:
    """Poll the merge SHA's OWN main run until completed. success → 'success'; anything else → 'blocked:<why>'."""
    deadline = ground.clock() + bound_s
    notified = False
    while ground.clock() < deadline:
        runs = [r for r in ground.gh_runs_for_sha(sha) if r.get("event") in (None, "push")]
        done = [r for r in runs if r.get("status") == "completed"]
        if done:
            concl = done[0].get("conclusion")
            return "success" if ci_is_green(concl) else f"blocked:post_merge_ci_not_green:{concl}"
        if not notified and ground.gh_main_runs_in_progress() > 2:
            rs.emit_loop_row("NOTIFY", lane_id, "merge-door-post-merge-ci:transient-retry:main_ci_queue_depth", f"> 2 main-push CI runs in progress while waiting on {sha[:12]}")
            notified = True
        ground.sleep(30)
    return "blocked:post_merge_ci_not_green:timeout"


def reconcile(lease: dict, ground: Ground) -> str:
    """Timeout / crash reconciliation by ground truth. MERGED ⇒ never re-issue. OPEN ⇒ caller may re-issue ONCE per pass."""
    v = ground.gh_view(int(lease["pr"]))
    if v.get("state") == "MERGED":
        return "MERGED"
    return "OPEN"


def _merge_once(lease: dict, pr: int, head_sha: str, ground: Ground, *, suffix: str = "") -> bool:
    """(iii) attempted-marker BEFORE (iv) the bounded merge; on timeout reconcile, re-issue at most once.
    Returns True iff a reconcile pass was needed (the cycle is then not "clean" for §10 tiering)."""
    mark_attempted(lease, suffix=suffix)
    _kill_after("refresh-attempted" if suffix else "attempted")
    for attempt in (1, 2):
        try:
            proc = ground.gh_merge(pr, head_sha, MERGE_TIMEOUT_S)
            _kill_after("merge")
            if proc.returncode == 0:
                return attempt > 1
        except subprocess.TimeoutExpired:
            pass
        if reconcile({**lease, "pr": pr}, ground) == "MERGED":
            return True                                     # invariant: never re-invoke after MERGED
        if attempt == 2:
            raise DoorFailed("merge_reissue_exhausted (cause_attribution: merge_reissue_exhausted)")
    return True


def land(pr: int, *, lane_id: str, arc_id: str, ground: Ground, refresh: Callable[[], tuple[int, str]] | None,
         lease: dict | None = None) -> str:
    """Steps (i)–(ix). `lease` is passed on self-resume (reclaimed); otherwise acquired here (one attempt)."""
    res = rs.current(arc_id)
    if res is None:
        raise DoorFailed(f"{arc_id}: no reservation")
    head_sha, base_sha, attested = res[1]["head_sha"], res[1]["base_sha"], res[1]["attested_merge_tree"]
    if lease is None:
        lease = acquire(lane_id=lane_id, arc_id=arc_id, pr=pr, head_sha=head_sha, base_sha=base_sha)  # (i)
    if ground.codex_worktree_present():
        rs.emit_loop_row("NOTIFY", lane_id, "merge-door-lease-acquire:transient-retry:cross_carrier_codex_lane", "a .codex-worktrees/ lane is present: C-HE-01 §1 residual — a Codex-exec lane may reach gh pr merge unfenced")
    tier = _tiering_active(); tier and rs.emit_loop_row("NOTIFY", lane_id, "merge-door-lease-acquire:transient-retry:attestation_tier", f"lease acquired for pr #{pr} by {lane_id}")
    reconciled = False
    try:
        already = lease.get("merge_attempted_at") is not None and reconcile(lease, ground) == "MERGED"
        reconciled = reconciled or already
        if not already:
            verify_head_base(lease, ground)                                    # (ii)
            local_base_cas_check(head_sha, attested, ground)
            _kill_after("verify")
            reconciled = _merge_once(lease, pr, head_sha, ground) or reconciled  # (iii)+(iv)
        v = ground.gh_view(pr)                                                   # (v)
        if v.get("state") != "MERGED":
            raise DoorFailed("post-merge confirm: not MERGED")
        _kill_after("confirm")
        merge_sha = (v.get("mergeCommit") or {}).get("oid") or ""
        if rs.current(arc_id)[1]["state"] != "merged":
            if merge_sha:
                rs.update_payload(arc_id, {"merge_sha": merge_sha})              # the landing SHA (squash commit) joins detections
            rs.transition(arc_id, "merged", lane_id=lane_id)                     # (vi) -- ground truth: it IS merged
        _kill_after("reservation-merged")
        if merge_sha and ground.git_first_parent(merge_sha) != base_sha:        # BASE_TOCTOU detection (C-HE-12 §2)
            # Positive proof the race window was hit: NEVER silent acceptance. The merge already landed server-side
            # (reservation reflects that fact); the DOOR blocks -- no refresh, no release -- and routes to re-validation.
            _emit_gate(lease, gate="BASE_TOCTOU", fail_class="HITL-recoverable", cause="first_parent_mismatch", evidence=f"merge {merge_sha[:12]} first parent != verified base {base_sha[:12]}", arc_id=arc_id, lane_id=lane_id, severity="hard")
            mark_blocked(lease, sha=merge_sha, reason="base_toctou_first_parent_mismatch")
            rs.emit_loop_row("DEFERRED-HIL", lane_id, "merge-door-post-merge:HITL-recoverable:base_toctou", f"{arc_id} — merge {merge_sha[:12]} landed on a base other than the verified {base_sha[:12]}; re-validate main, then `just merge-door-unblock {pr} {merge_sha}`")
            raise DoorBlocked("base_toctou_first_parent_mismatch")
        status = wait_post_merge_ci(merge_sha, ground, bound_s=POST_MERGE_CI_BOUND_S, lane_id=lane_id)   # (vii)
        if status != "success":
            mark_blocked(lease, sha=merge_sha, reason="post_merge_ci_not_green")
            _emit_gate(lease, gate="merge-door-post-merge-ci", fail_class="HITL-recoverable", cause="post_merge_ci_not_green", evidence=status, arc_id=arc_id, lane_id=lane_id)
            rs.emit_loop_row("DEFERRED-HIL", lane_id, "merge-door-post-merge-ci:HITL-recoverable:post_merge_ci_not_green", f"{arc_id} — post-merge main run for {merge_sha[:12]} {status}; door blocked; run `just merge-door-unblock {pr} {merge_sha}` after fixing")
            raise DoorBlocked(status)
        _kill_after("post-ci")
        recorded = (read_lease() or {}).get("refresh")
        if refresh is not None or recorded is not None:                          # (viii) continuation, NO re-acquire
            if recorded is not None:                                             # self-resume: NEVER create a second refresh PR
                rpr, rhead = int(recorded["pr"]), recorded["head_sha"]
            else:
                rpr, rhead = refresh()
                publish_exclusive(_sidecar(lease["lease_token"], "refresh"), json.dumps({"pr": rpr, "head_sha": rhead}))
            reconciled = _merge_once(lease, rpr, rhead, ground, suffix="refresh") or reconciled
            rv = ground.gh_view(rpr)
            if rv.get("state") != "MERGED":
                raise DoorFailed("refresh PR did not merge")
            _kill_after("refresh-merged")
            rsha = (rv.get("mergeCommit") or {}).get("oid") or ""
            rstatus = wait_post_merge_ci(rsha, ground, bound_s=REFRESH_BOUND_S, lane_id=lane_id)
            if rstatus != "success":
                mark_blocked(lease, sha=rsha, reason="refresh_ci_not_green")
                _emit_gate(lease, gate="merge-door-post-merge-ci", fail_class="HITL-recoverable", cause="refresh_ci_not_green", evidence=rstatus, arc_id=arc_id, lane_id=lane_id)
                rs.emit_loop_row("DEFERRED-HIL", lane_id, "merge-door-post-merge-ci:HITL-recoverable:refresh_ci_not_green", f"{arc_id} — terminating refresh #{rpr} run for {rsha[:12]} {rstatus}; door blocked; fix, then `just merge-door-unblock {rpr} {rsha}`")
                raise DoorBlocked(rstatus)
        release(lease)                                                           # (ix)
        if not reconciled:                                                       # a CLEAN cycle (C-HE-06 §10): no reconcile pass, no HITL
            (DOOR / "tier-clean-cycles").mkdir(exist_ok=True)
            (DOOR / "tier-clean-cycles" / lease["lease_token"]).touch()
        tier and rs.emit_loop_row("NOTIFY", lane_id, "merge-door-lease-release:transient-retry:attestation_tier", f"lease released after pr #{pr}")
        return "released"
    except DoorFailed as exc:
        live = read_lease() or lease                                             # the sidecar is the authority, not the in-memory dict
        if live.get("merge_attempted_at") is None:
            release(live)                                                        # pre-attempt failure: release + re-gate
            raise
        # A failure AFTER the attempt (reissue exhausted, refresh did not merge, ...) is an ambiguous merge state:
        # NEVER blind-release (C-HE-06 §5). Block the door and route to HITL reconciliation.
        mark_blocked(live, sha=live.get("head_sha") or head_sha, reason=f"door_failed_after_attempt:{exc}")
        _emit_gate(live, gate="merge-door-reconcile", fail_class="HITL-recoverable", cause="merge_reissue_exhausted", evidence=str(exc), arc_id=arc_id, lane_id=lane_id)
        rs.emit_loop_row("DEFERRED-HIL", lane_id, "merge-door-reconcile:HITL-recoverable:merge_reissue_exhausted", f"{arc_id} — pr #{pr}: {exc}; reconcile by ground truth then `just merge-door-unblock {pr} <sha>`")
        raise DoorBlocked(str(exc)) from exc


def _tiering_active() -> bool:
    """C-HE-06 §10: NOTIFY per acquire/release during ≥3 pilot merges + first N=3 production multi-lane merges;
    silent after 3 clean cycles. State = count of clean cycles in DOOR/tier-clean-cycles (one file per cycle)."""
    d = DOOR / "tier-clean-cycles"
    return not d.is_dir() or len(list(d.iterdir())) < 3


def wait_for_door(try_acquire: Callable[[], dict], *, clock=time.monotonic, sleep=time.sleep, rng=None) -> dict:
    """§8 caller policy: bounded exponential backoff + full jitter (base 30 s, ×2, cap 10 min, 12 attempts ≈ 1 h),
    then HITL-recoverable. Rate-limit refusals wait but never count against the 12."""
    import random
    rng = rng or random.random
    attempts = 0
    delay = BACKOFF["base_s"]
    while True:
        try:
            return try_acquire()
        except RateLimited:
            sleep(BACKOFF["base_s"] * rng()); continue
        except LeaseHeld:
            attempts += 1
            if attempts >= BACKOFF["max_attempts"]:
                raise BudgetExhausted("HITL-recoverable: lease_acquire_budget_exhausted")
            sleep(min(BACKOFF["cap_s"], delay) * rng())
            delay = min(BACKOFF["cap_s"], delay * BACKOFF["factor"])
```
CLI: `land <pr> --lane-id --arc-id [--no-refresh] [--refresh-cmd "<cmd printing JSON {pr, head_sha}>"]` (self-resume: if `read_lease()` shows this lane's lease for `pr` with a dead pid → `reclaim(..., ground_state=reconcile(...))` then `land(..., lease=fresh)`); `unblock <pr> <blocked_at_sha> --lane-id`; `status`; `gc`. Exit codes: 0 released; 3 blocked (HITL); 4 door failed (re-gate); 5 budget exhausted.
- [ ] **Step 5: GREEN**; probes marked above → PINNED (`reconcile` MERGED guard with `-k timeout_reconcile_merged`; `ci_is_green` gate with `-k post_merge_ci_blocked`; re-acquire with `-k continuation`). Register phase0 rows: `::test_ac2_c_crash_resume`, `::test_timeout_reconcile_merged_calls_once`, `::test_continuation_no_reacquire`, `::test_post_merge_ci_blocked_and_unblock`, `::test_inflight_first_attempt_then_reissue` (all `mutation_probe=True` where marked).
- [ ] **Step 6: Commit** — `git add tools/merge_door.py tools/test_merge_door.py tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-23 merge-door landing steps ii–ix, reconcile, continuation, backoff policy, gate rows (C-HE-06 §4/§5/§8/§9/§10)"`.

**Rev 2026-08-21 (U-HE-23 execution corrections, as-built — landing half).** *(i) Per-PR FakeGround:* the sketch's single shared state dict made `gh_view(1)` report the REFRESH PR's state once the continuation began, breaking every resume assertion; the as-built fake keys state by pr (`states[1]`, `add_refresh_pr()` mints `states[2]`). *(ii) Sha value domains:* the sketch's `"h"/"t"/"m" * 40` literals fail the landed C-HE-03 §3 hex validation at `update_payload` (head/base/attested/merge_sha are `_SHA_FIELDS`); as-built uses `a/b/d/c * 40`. *(iii) `_notify` tolerant ledger wrapper:* the §3 order ships `loop_log_structured` at U-HE-29 while the roadmap executes U-HE-23 first (the registered §0-vs-§1 ordering contradiction, now biting a third unit) — `rs.emit_loop_row` raises `LoopStatusWriteError` today, and a missing ledger writer must never mask a `DoorBlocked` or crash the driver mid-landing; pre-U-HE-29 the signal degrades to a LOUD stderr line (in-band, never silent), durable rows arrive with U-HE-29. *(iv) Gate-row identity:* `make_finding_id` must hash the row's OWN `location` field (the sketch hashed the gate name and failed C-HE-24 §4 validation) and `n` is the count of the producer's prior rows for the head (the sketch's fixed 0 collided on a second same-head emission — main-CI row then refresh-CI row — and tripped the same-core invariant). *(v) Naming:* the driver's §5 reconciliation is `reconcile_ground()` (the module-neighbor `reservations.reconcile` is a different §5; one name per concept). *(vi) CLI self-resume guard:* reclaim fires only for a SAME-lane, SAME-pr lease whose holder is provably dead on this host and NOT blocked (a blocked door routes to unblock); merged-reservation-with-no-lease prints `nothing to land` rc 0. *(vii) gh/git shim:* one `pr view` case (the sketch's two JSON-field-keyed cases were the same string), `run list` non-`--commit` fallback `[]`. *(viii) Round-1 codex corrections (7 P2, each witnessed + probe-pinned):* `reconcile_ground` FAILS CLOSED on a non-OPEN/MERGED state (CLOSED/malformed was read as permission to re-issue); `_merge_once` takes a re-issue `budget` and every RESUME pass gets 1 (an attempted-marker restart could issue two more merges against the §5 single-re-issue contract — wired at both the main and refresh legs); a `refresh.intent` sidecar is published BEFORE `refresh()` runs, and intent-without-record BLOCKS to HITL (a crash between creating the refresh PR and persisting its identity would otherwise mint a second terminating-refresh PR on resume); a RESUMED recorded refresh reconciles ground truth before any merge call (a landed refresh was re-issued through `_merge_once`'s internal loop); `tier-clean-cycles` gets the same symlink containment as every door subdir; the CLI self-resume additionally requires `reservation_id == --arc-id` (a wrong arc argument could transition an unrelated reservation to merged); and the CLI's fresh-acquisition path routes through `wait_for_door` so the §8 backoff/budget policy IS the production path (BudgetExhausted exit 5 reachable), not an isolated unit.* *(ix) Round-2 codex corrections (4 P2 + 1 P3, each witnessed):* the `refresh.intent` fence SURVIVES a reclaim's token change (`read_lease` surfaces `refresh_intent`, `_publish_fresh` republishes the sidecar under the new token — the token-keyed fence was silently lost on self-resume, reopening the second-refresh-PR hole the r1 fix closed); the CLI routes NORMAL contention (a live foreign lease) through `wait_for_door` too (r1's wiring only covered the no-lease case, so real contention still exited 4 fail-fast); `BudgetExhausted` emits the §9 gate row + `DEFERRED-HIL` signal before exit 5 (a wedged door is human-actionable state, never only a stderr line); 38 fixture-generated synthetic rows (pr-1 / dummy-sha findings accumulated by suite runs BEFORE the hermetic gate-log seam landed) are PURGED from the tracked `.harness/merge-gate-log.jsonl` (consistency reducer clean after); `_tiering_active` treats a planted symlink as ACTIVE (a link to a ≥3-entry dir would have suppressed the §10 notifications). The CLI-contention witness runs `md.main` in-process against a patched `default_ground`, with the rate limiter disabled (real-clock RateLimited refusals never count against the budget and would loop forever at K=5/60s).* *(x) Round-3 codex corrections (2 P1 + 2 P2, each witnessed):* the operator-keyed `unblock` IS the re-validation attestation — the successor carries `unblocked_from=<blocked_at_sha>` and a resumed `land()` skips the BASE_TOCTOU re-check for that exact sha (the block otherwise re-fired on every resume: a permanently wedged door); `refresh_intent_unresolved` gains its two ground-truth recovery verbs (`record-refresh <pr> <head>` attaches the discovered PR as the recorded-refresh sidecar; `clear-refresh-intent` removes a false intent — both refuse any caller but the live lease's own lane); `land()` validates a CALLER-SUPPLIED lease against the door's current persisted lease + arc/lane/pr (a stale or foreign dict could drive `gh pr merge` while another lease is current); `_emit_gate` allocates finding ids through `finding_record.append_observation` (mint + validate + append in ONE lock — the unlocked count-then-append let concurrent emitters collide and the loser's RecordError skipped its DEFERRED-HIL + exit-5 path). *(xi) Round-4 codex corrections (3 P2 + 1 P3, each witnessed):* a fresh landing honors a verify-returned MERGED (an externally-landed PR would have received another `gh pr merge` — only attempted-marker resumes took the never-reissue path); `unblock` refuses a non-holder lane while the reservation is OPEN (the foreign lane's merge would land externally then fail the (vi) holder transition: an ambiguous attempted state — holder transfer stays reclaim's dead-holder job); the post-attempt block derives `cause_attribution` from the failure class (`unreconcilable_pr_state` / `refresh_failed` / `merge_reissue_exhausted` — the blanket reissue cause misrouted operators + §9 reducers); the intent-recovery flow is witnessed END-TO-END through the real blocked state (intent-block → `record-refresh` → `unblock` → resumed land releases) and the verb guidance names the full unblock-then-land recipe. *(xii) Round-5 codex corrections (3 P2 + 1 P3, each witnessed):* a BLOCKED resumed lease is refused by `land()` (a direct `land(lease=read_lease())` could drive past an operator block into the refresh merge before `release` noticed the sidecar — the sanctioned transition is `unblock`); the CLI makes the §4(viii) continuation posture EXPLICIT (`--refresh-cmd` xor `--no-refresh`, argparse-required — neither silently skipped the mandatory terminating refresh); the intent-unresolved wedge emits its §9 gate row + `DEFERRED-HIL` before raising (crash-recovery wedges carry the same operator-attention contract as every other block); the T6 in-flight witness now models the ADVERTISED window (ground truth OPEN at the first reconcile → the permitted single re-issue → the delayed FIRST landing surfaces; the prior synchronous flip duplicated the ordinary timeout/MERGED case). *(xiii) Round-6 codex corrections (2 P2 + 1 P3, each witnessed):* the recovery verbs refuse a LIVE unblocked holder (a second same-lane process could remove the crash fence while refresh creation was actively in flight — they now operate only on a blocked door or a provably-dead holder, which is exactly the sanctioned unblock-successor flow); the §10 tier counter RESETS on a reconciled/HITL cycle (nonconsecutive cleans were accumulating to silence — the contract says three CONSECUTIVE); the `refresh.intent` sidecar joins the store-audit's lease-sidecar family row (authoritative crash-recovery state, previously uninventoried).* *(xiv) Round-7 codex corrections (2 P1 + 1 P2, each witnessed):* `record-refresh` resolves only an EXISTING unresolved intent and validates the pair against gh ground truth (`headRefOid` match + OPEN/MERGED state) — lane ownership + holder deadness alone let a mistaken caller-supplied pr/head pair merge an UNRELATED PR under the resumed lease, and a second record could overwrite a resolved one; the CLI self-resume routes a `reconcile_ground` raise to `mark_blocked` (`unreconcilable_at_resume:` cause, exit 3) — the r1 fail-closed raise BEFORE `reclaim` otherwise retained the dead holder's unblocked lease, permanently wedging the door with `unblock` unavailable (not blocked); `--no-refresh` emits a NOTIFY row (`refresh_skipped_by_operator`) — the flag satisfies the r5 explicit-posture group but silently skipped the mandatory §4(viii) continuation; it remains the sanctioned test/manual bypass, now loud.* *(xv) Round-8 codex corrections (3 P1 + 2 P2, each witnessed):* `git_first_parent` fetch-then-retries — the squash SHA is minted SERVER-SIDE by `gh pr merge` and is normally absent from the local odb, so a successful remote merge raised `CalledProcessError` AFTER the reservation flipped merged (door wedged, post-merge CI + refresh skipped); the AC#2(c) git shim now models the unfetched odb (rev-parse of the squash SHA fails until a `fetch origin` lands it — the answer-everything shim had masked the path); the post-attempt ambiguity check counts a REFRESH attempt as an attempt (an externally-MERGED main PR carries no main attempted marker, so an ambiguous refresh merge blind-released the global door); `record-refresh` validates the §12.2.1 terminating-refresh SHAPE (title prefix + roadmap-status-only file set, via gh_view's title/files fields) — pair-consistency alone let any real OPEN PR merge under the lease; `--no-refresh` upgrades to the full operator-attention contract (durable §9 gate row + DEFERRED-HIL, emitted as posture before the drive so a crash cannot lose it) — it remains the sanctioned test/manual bypass, per the r7 adjudication; BASE_TOCTOU persists the blocked sidecar BEFORE the gate/notify emissions (a raising gate-log writer left a positively-detected race as an unblocked held lease; the intent-fence and post-CI sites already blocked first — swept).* *(xvi) Round-9 codex corrections (2 P1 + 4 P2 + 2 P3, each witnessed):* the PERSISTED lease view is authoritative for a resumed drive (`lease = live` after the r3 identity validation — a caller retaining valid identifiers could forge `unblocked_from` to bypass BASE_TOCTOU or omit the attempted sidecars to regain spent re-issue budget); the FRESH refresh callback's output passes the same identity+shape gate as record-refresh BEFORE the pair persists (a refresh-cmd mistakenly returning any real OPEN PR's pair would have been squash-merged under the lease); the recovery verbs fail closed on a CROSS-HOST holder (liveness is unverifiable off-host — treating it as dead reopened the duplicate-refresh window); `--no-refresh` is env-gated (`MERGE_DOOR_ALLOW_NO_REFRESH=1`, refused pre-lease) — no longer a production path, resolving the r5→r9 held-until-refresh tension: the bypass exists for the subprocess crash suites and operator recovery only; ANY recorded refresh already MERGED by ground truth is never re-issued (record-refresh accepts a MERGED PR whose sidecar carries no attempted marker — the attempted-only reconcile re-drove `gh pr merge`); a §10 clean cycle requires a CONFIRMED refresh (a refresh-skipped run neither counts nor resets the consecutive streak); the AC#2(c) suite gains the REFRESH-half subprocess case (killed at refresh-attempted, resumed with the same --refresh-cmd; per-PR gh shim state; each PR merged exactly once) plus an unfetched-odb git shim; the in-flight and AC#2(c) manifest rows flip to mutation_probe=True with pinned targets (the timeout-tolerate arm; the CLI self-resume reclaim).* *(xvii) Terminal round (codex r10 — register-and-hold at the 10-round cap; 4 absorbed + witnessed, 4 HELD):* ABSORBED — the drive's exception adjudication widens from `DoorFailed` to ANY non-`DoorBlocked` exception (production seams raise RuntimeError/JSON/CalledProcessError/TimeoutExpired; a post-acquire escape now releases pre-attempt / blocks post-attempt instead of exiting with a live unblocked lease); `land()` enforces the refresh-skip env opt-in at the API layer (the CLI-only gate was bypassable by a direct caller — skipped continuation now fails closed, blocked+recoverable); the never-reissue pre-check covers the FRESH refresh path (a refresh-cmd returning an already-landed pair was observed MERGED then merged again); `wait_for_door` bounds rate-limited waits by the §8 envelope deadline (sustained same-lane contention could keep >K attempts in every rolling window and spin past the HITL exhaustion route forever). HELD at terminal (adjudication to the 3-lens merge gate): (a) reissue-after-reconcile-OPEN can land on a base advanced during the first attempt — same detect-after-block BASE_TOCTOU family as the r4/r5-held window (the §5 single-reissue contract is spec'd; detection + loud block + HITL is the designed mitigation); (b) the (vi) merge-tuple recording spans two reservation CASes without an expect predicate — a concurrent same-arc rebind window, bounded by the C-HE-03 §6 holder gate + the arc-serial reconcile venue (the U-HE-22 ABA family); (c) r10's foreign-reclaim/holder-transition wedge RE-LITIGATES the U-HE-22 r2-pinned adjudication (`rs.holder` stays with the original lane by design; witness `test_reclaim_two_step_and_transfers_merge_authority_only`) — the refusal is loud (post-attempt block) and the C-HE-03 §6 `transfer_holder` venue recovers; reviewer conflict recorded, not re-fixed; (d) refresh lineage-binding (an OLD terminating-refresh PR from a prior landing passes the shape gate) needs a carrier contract (e.g. the refresh PR naming the landed PR) — a DESIGN candidate routed to the forward register, not an incremental guard. 11 probes PINNED, coverage 0.*

---

### U-HE-24: `ci.yml` concurrency keyed by SHA for `main` pushes

**Scope.** `.github/workflows/ci.yml:43-45` → group `ci-${{ github.workflow }}-${{ github.ref == 'refs/heads/main' && github.sha || github.ref }}`; PR-event semantics unchanged; the tradeoff comment stated inline.

**Spec linkage.** C-HE-06 §4 "CI concurrency" (must key by SHA for `main` pushes; tradeoff: cancel-in-progress disabled for `main` runs; NOTIFY when > 2 in progress — emitted by U-HE-23).

**Files.** Modify `.github/workflows/ci.yml:43-45`; add `tools/test_ci_yml_concurrency.py`.

**Depends on.** (none) — but MUST land before any N ≥ 2 landing (a sibling lane's push would cancel the run step (vii) waits on).

- [ ] **Step 1: Test**
```python
import yaml
from pathlib import Path
def test_main_push_concurrency_keyed_by_sha():
    ci = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
    group = ci["concurrency"]["group"]
    assert "github.ref == 'refs/heads/main' && github.sha || github.ref" in group
    assert ci["concurrency"]["cancel-in-progress"] is True
```
- [ ] **Step 2: RED**; **Step 3: Edit**
```yaml
# Cancel superseded runs on the same ref so a fast push series doesn't queue -- EXCEPT on
# `main`, where the group is keyed by SHA: the merge door (C-HE-06 §4(vii)) holds its lease
# until the merge commit's OWN post-merge run is `success`, and lane B's landing must not
# cancel lane A's run. Tradeoff (stated): cancel-in-progress is effectively off for main
# pushes; under N-lane cadence full runs can queue -- the door emits a NOTIFY when > 2 are
# in progress.
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref == 'refs/heads/main' && github.sha || github.ref }}
  cancel-in-progress: true
```
- [ ] **Step 4: GREEN**, add the test to `tools/codex-parity-check.sh`; commit `ci(he-lanes): U-HE-24 key main-push concurrency by SHA (C-HE-06 §4)`.

---

### U-HE-25: `tools/hooks/safe-merge.sh` + guard deny-raw / allow-wrapper + test inversion

**Scope.** Create the allowlisted wrapper (exact arity `bash tools/hooks/safe-merge.sh <pr>`; performs C-HE-06 (i)–(ix) by delegating to `merge_door.py land`); deny raw `gh pr merge` in loop mode as an explicit `emit_deny`; allow only the wrapper next to `_safe_worktree_remove_wrapper`; invert the two guard tests that assert `gh pr merge → allow`. **Registered from U-HE-21 (codex r1–r6 on that PR; this is the reviewed guard-modification unit):** the same guard edit also allowlists the U-HE-21 carrier commands, EXACT-SHAPE only (codex r6 P2 ×2 narrowed both matchers) — (a) `uv run python tools/reservations.py <verb>` for the carrier verbs `selectable|show|reserve|update|mint-lane-id` ONLY — never the bare module prefix, which would auto-approve `transition` (terminal state changes), `gc` (history pruning), and `reconcile`/`reconcile-all` (gh-backed; hook-invoked, not Bash-tool-invoked, so it needs no allowlisting); (b) a leading env-prefix strip restricted to exactly `HARNESS_ARC_ID=` and `HARNESS_LANE_ID=` with bareword values (no `$`/quotes/spaces) — never the generic `HARNESS_[A-Z0-9_]+` class, which would let `HARNESS_FAILOVER_CHILD=1 just gemini-review` silently skip reservation-outcome persistence; (c) `git merge-tree` in the read-arc git verb group — each with allow + hardening cases in `test_permission_guard.sh` (the U-HE-21 witness already pins the never-denied floor for these shapes and the allow floor for the bare `just review-with-failover` headless fallback).

**Spec linkage.** C-HE-07 §1 (matcher verbatim), §2 (deny in the deny block; allow beside `:288-290`), §3 (test inversion `:167-169`, `:328-329`), §4, Invariants (`--admin` stays denied `:397`).

**Files.** Create `tools/hooks/safe-merge.sh`. Modify `tools/hooks/permission-guard.sh` (`:184-191` add `_safe_merge_wrapper`; `:288-290` allow; `:314-340` deny), `tools/hooks/test_permission_guard.sh:167-169, 328-329` (+ new cases).

**Depends on.** U-HE-23.

- [ ] **Step 1: Failing tests** (`tools/hooks/test_permission_guard.sh`; replace the two `gh pr merge → allow` assertions):
```bash
# C-HE-07: raw merge verb DENIED in loop mode; only the safe-merge wrapper is auto-allowed.
OUT=$(run_on "$(pl Bash 'gh pr merge 268 --squash --delete-branch' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "raw gh pr merge → deny (loop mode)" || bad "raw merge not denied: $OUT"
OUT=$(run_on "$(pl Bash 'gh pr merge 1 --squash' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "raw gh pr merge --squash → deny" || bad "raw merge --squash not denied: $OUT"
OUT=$(run_on "$(pl Bash 'bash tools/hooks/safe-merge.sh 268' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "safe-merge wrapper → allow" || bad "wrapper not allowed: $OUT"
for c in 'bash tools/hooks/safe-merge.sh 268 --squash' 'bash tools/hooks/safe-merge.sh $PR' 'bash tools/hooks/safe-merge.sh 268; rm x' 'bash tools/hooks/safe-merge.sh abc' 'tools/hooks/safe-merge.sh'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "wrapper hardening: '$c' → not allow" || bad "wrapper over-matched: $c"
done
OUT=$(run_on "$(pl Bash 'gh pr merge 5 --admin' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "--admin merge stays denied" || bad "--admin merge not denied: $OUT"
grep -q 'raw gh pr merge' "$REPO/.harness/loop_status.md" 2>/dev/null || true   # DENY row audited via emit_deny (venue per U-HE-29)
```
- [ ] **Step 2: RED**; **Step 3: Implement.** In `permission-guard.sh` after `_safe_worktree_remove_wrapper` (`:184-191`) add the reference matcher verbatim from C-HE-07 §1:
```bash
_safe_merge_wrapper() {
  local cmd="$1"
  printf '%s' "$cmd" | grep -q '[;&|<>`\\()]' && return 1
  [[ "$cmd" == *$'\n'* ]] && return 1
  printf '%s' "$cmd" | grep -Eq '(~|\.\.|\$\{?[A-Za-z_])' && return 1
  set -f; set -- $cmd; set +f
  if [ "${1:-}" = "bash" ]; then shift; fi
  [ "$#" -eq 2 ] && [ "$1" = "tools/hooks/safe-merge.sh" ] || return 1
  case "$2" in ''|*[!0-9]*) return 1 ;; esac
  return 0
}
```
After the `_safe_worktree_remove_wrapper` allow (`:288-290`):
```bash
if [ "$TOOL" = "Bash" ] && [ -n "$CMD" ] && _safe_merge_wrapper "$CMD"; then
  emit_allow
fi
```
In the deny block (`:314-340`), after the branch-deletion deny:
```bash
  # C-HE-07: the merge verb goes through the lease-holding wrapper ONLY (structural fence, P1).
  printf '%s' "$CMD" | grep -Eq '(^|[[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:]]|$)' \
    && emit_deny "raw gh pr merge — must go through tools/hooks/safe-merge.sh"
```
and remove `merge` from the `gh pr (…)` allow alternation at `:427` (`view|list|checks|diff|status|create|ready|comment`). Create `tools/hooks/safe-merge.sh`:
```bash
#!/usr/bin/env bash
# C-HE-07 allowlisted merge wrapper. Exact arity: `bash tools/hooks/safe-merge.sh <pr-number>`.
# Performs C-HE-06 steps (i)-(ix) by delegating to tools/merge_door.py; the ONLY merge string it
# ever issues is `gh pr merge <pr> --squash --match-head-commit <head_sha>` (inside merge_door).
# No flags are accepted or forwarded.
set -euo pipefail
[ "$#" -eq 1 ] || { echo "usage: safe-merge.sh <pr-number>" >&2; exit 64; }
case "$1" in ''|*[!0-9]*) echo "safe-merge: pr must be all digits" >&2; exit 64 ;; esac
: "${HARNESS_LANE_ID:?HARNESS_LANE_ID must be set (lane-init)}"
: "${HARNESS_ARC_ID:?HARNESS_ARC_ID must be set (roadmap-continue arc open)}"
cd "$(git rev-parse --show-toplevel)"
exec uv run python tools/merge_door.py land "$1" --lane-id "$HARNESS_LANE_ID" --arc-id "$HARNESS_ARC_ID" \
  --refresh-cmd "uv run python tools/roadmap_status_refresh.py --emit-refresh-pr-json"
```
(`--emit-refresh-pr-json` is the existing refresh tool's new flag that creates — idempotently, by branch name — the terminating refresh PR and prints `{"pr": N, "head_sha": "..."}` — add it in U-HE-28.)
- [ ] **Step 4: GREEN** — `bash tools/hooks/test_permission_guard.sh` all `ok`. Register `Row("C-HE-07", "shell:tools/hooks/test_permission_guard.sh", "phase0", "local + CI", False)`. Commit:
```bash
git add tools/hooks/safe-merge.sh tools/hooks/permission-guard.sh tools/hooks/test_permission_guard.sh tools/lanes_verify.py
git commit -m "feat(he-lanes): U-HE-25 safe-merge wrapper + deny raw gh pr merge / allow wrapper (C-HE-07)"
```

**Rev 2026-08-21 (U-HE-25 execution corrections, as-built — PR #1416).** *(i) `transition --to open` allowance (codex r1 P1; ratified by the 3-lens merge gate's spec-conformance round):* the registered (a) verb list omitted the production pending→open flip that ship-pr issues at its final gate (`.claude/skills/ship-pr/SKILL.md` — `transition --arc-id <arc> --to open --lane-id <lane>`, the U-HE-22-landed spec v1.4 X4a carrier), so a headless lane would reserve, review, and then strand at ask→deny on the flip. The as-built guard adds `_transition_to_open_only()` — a TOKEN PARSER, not a regex pair: exactly one `--to` option, two-token literal form, target exactly `open`; any `=`-form (`--to=merged`), argparse prefix abbreviation (`--t merged`), or last-occurrence-wins smuggle (`--to open --to=merged`, codex r2 P1) rejects the whole command. The U-HE-21 r6 exclusion rationale is PRESERVED, not overridden: terminal transitions (`--to merged|abandoned`) and `gc` still surface to the operator; only the one non-terminal production flip is allowlisted, each shape witnessed (allow + 8 hardening rows). *(ii) Pre-lease availability guard on the wrapper (merge-gate r1 — all three lenses BLOCKed the plan-verbatim body):* `safe-merge.sh`'s `--refresh-cmd` names `--emit-refresh-pr-json`, which this unit's own body defers to U-HE-28; `merge_door.land()` runs the refresh AFTER the irreversible merge (`check=True`), so on any pre-U-HE-28 invocation the refresh subprocess would fail and `mark_blocked()` would wedge the SHARED global lease (operator-only unblock) — the concurrency lens traced this as deterministic-on-first-use. The as-built wrapper probes `roadmap_status_refresh.py --help` for the flag and aborts `exit 69` BEFORE any lease/merge when absent; the delegated invocation itself stays plan-verbatim, and the guard auto-passes the moment U-HE-28 lands. Witnessed three ways: stubbed flag-present → exact arg-boundary delegation byte-compare; stubbed flag-absent → exit 69 with NO delegation; REAL CLI at HEAD → exit 69 pre-lease (U-HE-28 flips this last pin — update it in its Step 4). *(iii) Carrier docs re-grounded (merge-gate spec lens P2/P3):* `roadmap-continue/SKILL.md`'s "Known gap … until U-HE-25 lands" paragraph now records the gap as CLOSED (the degradation clause survives as the generic fallback), and `two-lane/SKILL.md`'s guard line cites re-pinned (`:370-371`/`:372-373`). *(iv) Registered residual → B-188:* the C-HE-07 §2 deny predicate is a spec-verbatim FREE-TEXT scan, so loop-mode commands whose quoted data mentions `gh pr merge` are hard-denied; narrowing it is a spec change-note decision, filed with close-out steps at `.harness/forward-register.yaml` B-188. *(v) Witness residual (merge-gate witness lens P3, non-exploitable):* the "prefixed force-push still-deny" row cannot discriminate whether the deny scan reads raw `$CMD` or a stripped command (the sibling deny regexes are unanchored, so both readings fire); no current deny pattern is defeated by the strip, and U-HE-26's anchored `^git push` predicates are where a discriminating witness first becomes constructible — add it there.*

---

### U-HE-26: Push-to-`main` client-side `emit_deny` predicates

**Scope.** Explicit `emit_deny` entries in the deny block for any push whose refspec/upstream targets `main` (incl. bare `git push` while `main` is checked out); topic-branch pushes remain auto-allowed.

**Spec linkage.** C-HE-08 §1 (predicates verbatim; in the deny block, not the allow regex, so `loop_log DENY` audits it), Invariants.

**Files.** Modify `tools/hooks/permission-guard.sh:321-329` (append), `tools/hooks/test_permission_guard.sh`.

**Depends on.** (none).

- [ ] **Step 1: Failing tests**
```bash
# C-HE-08 §1: push-to-main denied; topic pushes still allowed
for c in 'git push origin HEAD:main' 'git push origin main' 'git push origin refs/heads/main' 'git push -u origin feature:main' 'git push --set-upstream origin main' 'git push origin feature main' 'git push --force-with-lease=x origin +feature:refs/heads/main' "git push origin 'HEAD:main'" 'git push origin "main"'; do
  OUT=$(run_on "$(pl Bash "$c" '')"); [ "$(dec "$OUT")" = "deny" ] && ok "'$c' → deny" || bad "push-to-main not denied: $c → $OUT"
done
OUT=$(run_on "$(pl Bash 'git push origin feature' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "topic push → allow" || bad "topic push blocked: $OUT"
# bare `git push` while main is checked out
( cd "$REPO" && git init -q . && git checkout -q -b main 2>/dev/null; git commit -q --allow-empty -m i )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "bare push on main checkout → deny" || bad "bare push on main not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push -u origin' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "option-bearing bare push on main → deny" || bad "-u origin on main not denied: $OUT"
( cd "$REPO" && git checkout -q -b topic ); OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare push on topic → allow" || bad "bare push on topic blocked: $OUT"
```
- [ ] **Step 2: RED**; **Step 3: Implement** (deny block, after the branch-deletion deny):
```bash
  # C-HE-08 §1 (D5): no auto-approved push lands content on main. Explicit denies (audited via loop_log DENY),
  # NOT a removal from the allow regex (that would be the silent, unaudited "ask" path). The spec's reference
  # regexes (C10) consumed at most one token between `push` and the refspec and therefore ALLOWED
  # `git push -u origin feature:main` (Codex round-2 P1); this parses the argument list instead: option tokens
  # (`-*`) anywhere are skipped, positionals are [remote] [refspec...], and any refspec whose destination is
  # main -- `main`, `HEAD:main`, `X:main`, `refs/heads/main` -- or a push with NO refspec while main is checked
  # out, is denied.
  if printf '%s' "$CMD" | grep -Eq '^[[:space:]]*git[[:space:]]+push([[:space:]]|$)' && _push_targets_main "$CMD"; then
    emit_deny "push targeting main — land through a PR + tools/hooks/safe-merge.sh"
  fi
```
with the parser placed beside `_safe_merge_wrapper`:
```bash
# 0 iff the push command targets main: any refspec destination == main, or no refspec while main is checked out.
_push_targets_main() {
  local cmd="$1" tok positional=() dest branch
  set -f; set -- $cmd; set +f
  shift 2                                   # git push
  for tok in "$@"; do
    tok=${tok//\"/}; tok=${tok//\'/}         # the real shell strips quotes: `'HEAD:main'` IS HEAD:main (Codex round-4 P1)
    case "$tok" in -*) continue ;; esac     # options anywhere (-u, --set-upstream, --force-with-lease=..., ...)
    positional+=("$tok")
  done
  if [ "${#positional[@]}" -le 1 ]; then    # bare push (optional remote only) -> pushes the current branch
    branch=$(git -C "$PROJECT_DIR" symbolic-ref --short -q HEAD 2>/dev/null)
    [ "$branch" = "main" ] && return 0
    return 1
  fi
  for tok in "${positional[@]:1}"; do       # every refspec after the remote
    dest="${tok##*:}"; dest="${dest#+}"; dest="${dest#refs/heads/}"
    [ "$dest" = "main" ] && return 0
  done
  return 1
}
```
- [ ] **Step 4: GREEN**, probe (`--lines` = the first predicate) → PINNED. Register `Row("C-HE-08", "shell:tools/hooks/test_permission_guard.sh", "phase0", "local + CI", True)`. Commit `feat(he-lanes): U-HE-26 deny push-to-main predicates in the audited deny block (C-HE-08 §1)`.

**Rev 2026-08-21 (U-HE-26 execution corrections, as-built).** *(i) Stripped-read predicate — the U-HE-25 rev (v) discriminating witness, discharged:* the sketch above reads the RAW `$CMD`; the as-built predicate reads the `HARNESS_ARC_ID=`/`HARNESS_LANE_ID=` prefix-STRIPPED form (the same sed strip the U-HE-25 (b) allowance applies at `TRIM`). Grounds: the allowlist strips that exact prefix class BEFORE its `git push` alternation, so a raw-command-anchored deny would let `HARNESS_ARC_ID=x git push origin main` past this deny and into the alternation's AUTO-ALLOW — re-opening X9 through the very allowance U-HE-25 registered. The forecast discriminating witness (prefixed push-to-main → deny; fails under the raw reading, passes under the stripped one) landed with the unit; sibling denies stay unanchored and unaffected. *(ii) Combined manifest row:* the Step-4 `Row("C-HE-08", …)` registration would duplicate the C-HE-07 row's artifact, which `test_manifest_rows_well_formed` rejects (the manifest keys rows by artifact); as-built registers ONE combined `Row("C-HE-07/08", "shell:tools/hooks/test_permission_guard.sh", "phase0", "local + CI", True)` per the `C-HE-09/10` / `C-HE-15/16/18` precedent — `True` strictly strengthens the prior C-HE-07 `False`. *(iii) Shell-row probe-target derivation fix:* the Step-4 probe registration was unsatisfiable as written — `default_probe_target("tools/hooks/test_permission_guard.sh")` derives `permission_guard.sh` (underscore), a file that does not exist, so the coverage gate could never match the pin. As-built, `required_probes`' shell branch honors a file-level red-first-form annotation (`# mutation-probe: <path>:<lines> …`) naming the probed file (grammar shared with the red-first skill; `_DESC_TARGET`), the shell suite carries `# mutation-probe: tools/hooks/permission-guard.sh:<predicate-range> …` (the range is re-stamped whenever the predicate moves; `:512-514` at landing), the scan is deletion-expressible (flat loop, no guarding `if` — the probe refused the first shape as syntactically non-removable), and a pytest witness (`test_shell_row_explicit_annotation_names_hyphenated_probe_target`) pins the scan; the two `lanes_verify.py` guard pins were re-pinned after this unit's own edits staled their digests. *(iv) Codex r1 parser hardening (BLOCK absorbed):* four bypass classes in the sketch's parser were closed — (a) shell dequoting extends to backslashes (`HEAD:ma\in` IS `HEAD:main`; removing every backslash only widens the deny); (b) `--all`/`--branches` deny outright (`--mirror` stays with its sibling predicate); (c) EVERY positional is scanned for a main/wildcard destination BEFORE the bare-push branch — with `--repo=<r>` the remote arrives as an option and positional[0] is already a refspec (a remote token scanned as a refspec can only false-positive toward deny); (d) wildcard destinations (`refs/heads/*`) deny. The bare-push branch adds three deterministic config reads (`push.default=matching`; `branch.<b>.merge` = main — over-denies under `simple`, where the real push would merely fail; `remote.<r>.push` refspec dest main/wildcard) — `@{push}` was probed and rejected as the truth source (errors without remote-tracking refs). Witnessed by 9 new rows including a config-cleanup allow row proving the checks are config-driven, not sticky. *(v) Codex r2 parser hardening (BLOCK absorbed):* four further classes — (a) expansion-capable characters (`{`/`}`/`$`) anywhere in the push command deny outright (the parser sees the UNEXPANDED string: `HEAD:ma{in,ster}` executes as `HEAD:main`, and `_bash_args_safe` rejects only `$UPPERCASE` expansions, not `$b`); glob-capable destinations (`?`/`[`) deny in the refspec scan; (b) the separate-value options of `git push` (`-o|--push-option|--repo|--receive-pack|--exec`, complete per git 2.x `-h`) skip their value token — miscounting a value as a positional had skipped the bare-push branch entirely (`git push -o ci.skip origin` on a `main` checkout auto-allowed); (c) `+:`/`:`-terminated refspecs (the matching-push form, empty parsed destination) deny; (d) bare-push remote resolution honors git's actual precedence `branch.<b>.pushRemote` > `remote.pushDefault` > `branch.<b>.remote` > `origin`. Witnessed by 9 further rows (main/topic `-o` pair, pushRemote + pushDefault chains, second config-cleanup allow row). *(vi) Codex r3 parser hardening (BLOCK absorbed; one over-deny corrected):* (a) `--recurse-submodules` joins the separate-value set (empirically confirmed: `git push --recurse-submodules no origin` consumes `no` as the mode and bare-pushes); (b) `--repo`'s value is CAPTURED as the selected remote (not merely skipped) and enters the remote-resolution chain at its git precedence slot (positional > `--repo` > `pushRemote` > `pushDefault` > `branch.remote` > `origin`); (c) colonless `HEAD`/`@` refspecs resolve to the current branch (deny on a `main` checkout), and explicit `:HEAD`/`:@` destinations (the remote's default branch) deny outright; (d) `remote.<r>.mirror=true` denies for both bare and refspec pushes to that remote (behaves as `--mirror`); (e) configured matching refspecs (`remote.<r>.push` = `:`/`+:`) and `HEAD`/`@` config destinations deny — same classes as the command-line scan; (f) **r3 P2 over-deny corrected:** `branch.<b>.merge=main` now denies ONLY under `push.default=upstream|tracking` — under `simple` the real push refuses and under `current` it goes topic→topic, so both stay allowed per the contract's topic-push clause (the prior row codifying the false denial was inverted into an allow witness). Witnessed by 13 further rows including main/topic pairs and a third config-cleanup allow row. *(vii) Codex r4 — fail-closed closure moves (BLOCK absorbed):* r2-r3's enumerations were themselves the new defect surface, so r4 replaces enumeration with closure: (a) any quoted span with internal whitespace denies outright (word-splitting cannot preserve quoted argument boundaries — `--repo 'remote bare repo'` smeared into phantom positionals; single-token quoting like `'HEAD:main'` is unaffected); (b) the option branch flips from default-continue to default-DENY — a recognized EXACT allowlist of flag-only/=-form options continues, and any other option token fails closed, killing the entire long-option-abbreviation class (`--al` == `--all`) and every future unknown option in one move (the U-HE-21/25 exact-shape philosophy applied to option parsing); (c) `remote.<r>.mirror` reads through `git config --bool` (git booleans: `yes`/`on`/`1`). Witnessed by 4 further rows + a fourth config-cleanup allow row. *(viii) Codex r5 (BLOCK absorbed):* (a) the separate `--repo` value is dequoted BEFORE storing (`--repo 'origin'` had queried config for the literal quoted string and missed the remote's main-targeting refspec); (b) destination normalization strips `refs/` then `heads/` so the DWIM partial form `topic:heads/main` (git resolves it to `refs/heads/main`) denies — applied at both the command-line and configured-refspec scan sites; (c) the two documentation-consistency findings (clearance-marker scope prose, this note's own stale probe-range cite) were corrected in place. *(ix) Codex r6 (BLOCK absorbed; converging — 2 findings):* (a) `#` joins the unparseable-command gate (word-splitting treats a shell comment's tokens as arguments, so `git push # comment` IS a bare push while the parser counted phantom positionals; `#` is illegal in refnames, nothing legitimate lost); (b) the r6 P2 over-deny corrected: the refspec scan skips the remote slot at ≥2 positionals (a remote named `main` no longer hard-denies a topic push) — sound because the r4 closure makes every unrecognized option fail closed, so a ≥2-positional slot 0 is reliably the remote; at exactly one positional the token may be a `--repo`-form refspec and stays scanned. Probe range at landing: `:522-524`. *(x) Codex r7 (1 absorbed, 1 REFUTED by measurement):* (a) backslash-escaped whitespace joins the boundary gate (`origin\ repo` is ONE git argument but two word-split tokens — same class as the r4 quoted-whitespace gate); (b) the `--repo` positional-precedence finding was refuted empirically (`git push --repo=/tmp/repoA /tmp/repoB topic` errors against repoB: a positional repository BEATS `--repo`, per both measurement and the git docs), so `--repo=origin main topic` stays modeled as remote=main + refspec=topic — an allow witness pins the refutation, and the count==1 narrative comment was corrected to the measured truth (a single positional is the repository; scanning it as a refspec is pure over-approximation toward deny). *(xi) Codex r8 (BLOCK absorbed; severities declining P1/P2/P3):* (a) `--recurse-submodules` modes discriminate — only `no|check` are inert; `on-demand`/`only` (either form) deny, because recursive modes spawn NESTED submodule pushes that never pass through this hook; (b) `--` end-of-options handling — subsequent tokens are positionals, so `git push -- origin topic` stays allowed (the r4 fail-closed branch had hard-denied the standard marker) while `-- origin main` still denies through the positional scan; (c) `required_probes`' shell branch collects EVERY annotation (returning on the first let a second annotated target go unprobed — coverage false-green), with a two-annotation pytest witness. Probe range at landing: `:538-540`. *(xii) Codex r9 (2 absorbed, 1 registered):* (a) glob-capable characters (`*`/`?`/`[`) join the whole-command gate — pathname expansion applies to EVERY word, so a glob REMOTE token (`[am]*`) can expand into a main refspec the per-destination checks never see; (b) the r6/r9 P2 over-deny fully resolved on the r7 measurement: positional[0] is git's REPOSITORY at every arity, so the refspec scan now always skips it — `git push main` / `git push --repo=origin main` are bare pushes to a remote named `main` (denied on a main checkout by the branch check, allowed on topic, config checks running against exactly that remote); (c) the shell-annotation range-collapse coverage residual (two same-file range annotations reduce to one requirement — not exploitable at HEAD, one annotation per target) is REGISTERED at `.harness/forward-register.yaml` B-189 with the three-carrier key migration as its close-out. *(xiii) Codex r10 — REGISTER-AND-HOLD TERMINAL at the round cap:* the r10 classes circle the ADVERSARIAL-LOCAL-CONFIG trust boundary — (P1) quote-bearing remote NAMES defeating dequote-then-lookup and (P2) config TOCTOU between the hook's sample and the approved execution both presume an actor already holding git-config write (config-write is code-write here: the same actor could edit the hook), which sits outside the client predicate's threat model; both are registered at B-190 with the C-HE-08 §2 SERVER fence (U-HE-27, the immediately-next unit; invariant R-20) as the bounding close-out. The r10 P2 over-deny (a literal `#` inside a quoted push-option hard-denied) is RETAINED by design — quote-aware parsing is exactly what r10 P1 measured as unreliable — and pinned by an explicit witness row; the r10 P3 re-finds the already-registered B-189 (disposition stands). Ten rounds total: r1-r6/r8 absorbed same-round, r7 one absorbed + one refuted by measurement, r9 two absorbed + B-189 registered, r10 terminal (B-190 registered, over-deny pinned).

---

### U-HE-27: `tools/main_protection.py` + `just main-protection-{show,apply,rollback,tiebreaker,verify}`

**Scope.** Server-side X9 fence as recipes: `show` (read-only GET), `apply` (embeds the exact JSON payload; re-derives the "— blocking" context list from `ci.yml` job `name:` values; prints one before/after diff; run by Claude OUTSIDE loop mode; the operator answers one AskUserQuestion), `rollback` (`gh api -X DELETE …/protection`, with the pre-change `show` recorded in the evidence log), `tiebreaker` (scratch PR merge under `strict:true` + a refresh PR branched from a since-superseded `main` fast-forwards or is caught pre-merge), `verify` (read-only exact-compare of every required setting + context; 404/mismatch → RED; auth-absent → skip reason `gh-auth-absent`, which `lanes-phase0-check` counts as RED).

**Spec linkage.** C-HE-08 §2 (exact settings; recipes; `enforce_admins:true` does not block the refresh merge), §3 (operator-gated), §4 (tiebreaker), §5 (`verify` phase0 row, runs local); §11 #2.

**Files.** Create `tools/main_protection.py`, `tools/test_main_protection.py`. Modify `justfile` (five recipes), `tools/lanes_verify.py`, `tools/codex-parity-check.sh`.

**Interfaces.**
```python
REQUIRED = {"required_pull_request_reviews": None, "enforce_admins": True, "allow_force_pushes": False, "allow_deletions": False, "required_linear_history": False}
def blocking_contexts(ci_yml: Path = REPO/".github/workflows/ci.yml") -> list[str]   # job `name:` values ending "— blocking", workflow "CI"
def desired_payload(contexts) -> dict
def diff_report(current: dict | None, desired: dict) -> str
def verify(current: dict | None, desired: dict) -> list[str]     # mismatches; [] = pass
def main(argv) -> int   # show | apply --confirm | rollback | verify | tiebreaker
```

**Depends on.** U-HE-05 (manifest skip semantics), U-HE-25.

- [ ] **Step 1: Failing tests**
```python
def test_blocking_contexts_derived_from_ci_yml():
    ctx = mp.blocking_contexts()
    assert "pytest (all axis packages) — blocking" in ctx and all(c.endswith("— blocking") for c in ctx) and len(ctx) >= 12

def test_desired_payload_shape():
    p = mp.desired_payload(["a — blocking"])
    assert p["required_pull_request_reviews"] is None and p["required_status_checks"] == {"strict": True, "contexts": ["a — blocking"]}
    assert "restrictions" in p and p["restrictions"] is None
    assert p["enforce_admins"] is True and p["allow_force_pushes"] is False and p["allow_deletions"] is False and p["required_linear_history"] is False

def test_to_put_payload_normalizes_get_shape():
    got = {"url": "x", "required_status_checks": {"url": "y", "strict": True, "contexts": ["a — blocking"], "checks": [{"context": "a — blocking", "app_id": 1}]},
           "enforce_admins": {"url": "z", "enabled": True}, "required_pull_request_reviews": None,
           "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}, "required_linear_history": {"enabled": False}}
    put = mp._to_put_payload(got)
    assert put == {"required_status_checks": {"strict": True, "contexts": ["a — blocking"]}, "enforce_admins": True, "required_pull_request_reviews": None,
                   "restrictions": None, "allow_force_pushes": False, "allow_deletions": False, "required_linear_history": False}
    assert mp.verify(got, put) == []
    got["restrictions"] = {"users": [{"login": "alice"}], "teams": [{"slug": "core"}], "apps": []}
    assert mp._to_put_payload(got)["restrictions"] == {"users": ["alice"], "teams": ["core"], "apps": []}


def test_verify_flags_404_and_mismatch():
    d = mp.desired_payload(["a — blocking"])
    assert mp.verify(None, d) == ["unprotected (404)"]
    cur = {"required_status_checks": {"strict": False, "contexts": ["a — blocking"]}, "enforce_admins": {"enabled": True},
           "required_pull_request_reviews": None, "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}, "required_linear_history": {"enabled": False}}
    assert any("strict" in m for m in mp.verify(cur, d))
    cur["required_status_checks"]["strict"] = True
    assert mp.verify(cur, d) == []
```
- [ ] **Step 2: RED**; **Step 3: Write `tools/main_protection.py`** (stdlib + yaml; `gh api` via subprocess with 30 s timeouts; `apply --confirm` refuses when `HARNESS_LOOP=1` or `.harness/.loop-active` exists — the guard denies `gh api -X` in loop mode anyway; `verify` prints `SKIPPED [1] main_protection.py:1: gh-auth-absent` on `gh auth status` failure so `lanes_verify` classifies it as a legal skip).

```python
#!/usr/bin/env python3
"""C-HE-08 §2-5: server-side X9 fence for `main` as recipes. show / apply / rollback / verify / tiebreaker.
The context list is RE-DERIVED from .github/workflows/ci.yml at run time (job `name:` values ending "— blocking").
`apply` is operator-gated (one AskUserQuestion with the diff) and refuses to run in loop mode."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from pathlib import Path
import yaml

REPO = Path(__file__).resolve().parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
REQUIRED = {"required_pull_request_reviews": None, "enforce_admins": True, "allow_force_pushes": False,
            "allow_deletions": False, "required_linear_history": False}


def blocking_contexts(ci_yml: Path = CI_YML) -> list[str]:
    ci = yaml.safe_load(ci_yml.read_text())
    assert ci.get("name") == "CI", "workflow name must be CI (status contexts are keyed by it)"
    return sorted(j["name"] for j in ci["jobs"].values() if isinstance(j.get("name"), str) and j["name"].endswith("— blocking"))


def desired_payload(contexts: list[str]) -> dict:
    return {**REQUIRED, "required_status_checks": {"strict": True, "contexts": list(contexts)}, "restrictions": None}   # `restrictions` is a REQUIRED nullable field on the PUT (Codex round-5 P1)


def _gh(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)


def _repo() -> str:
    return _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").stdout.strip()


def current_protection() -> dict | None:
    p = _gh("api", f"repos/{_repo()}/branches/main/protection")
    if p.returncode != 0:
        if "404" in p.stderr or "Branch not protected" in p.stderr:
            return None
        raise SystemExit(f"gh api failed: {p.stderr.strip()}")
    return json.loads(p.stdout)


def _flag(cur: dict | None, key: str):
    v = (cur or {}).get(key)
    return v.get("enabled") if isinstance(v, dict) and "enabled" in v else v


def verify(current: dict | None, desired: dict) -> list[str]:
    if current is None:
        return ["unprotected (404)"]
    out = []
    rsc = current.get("required_status_checks") or {}
    if rsc.get("strict") is not True:
        out.append(f"required_status_checks.strict: {rsc.get('strict')!r} != True")
    have = sorted(rsc.get("contexts") or [c["context"] for c in rsc.get("checks", [])])
    want = sorted(desired["required_status_checks"]["contexts"])
    if have != want:
        out.append(f"contexts differ: missing={sorted(set(want)-set(have))} extra={sorted(set(have)-set(want))}")
    if current.get("required_pull_request_reviews") not in (None, {}):
        out.append("required_pull_request_reviews must be null (review authority is the gate chain)")
    for k in ("enforce_admins", "allow_force_pushes", "allow_deletions", "required_linear_history"):
        if _flag(current, k) != desired[k]:
            out.append(f"{k}: {_flag(current, k)!r} != {desired[k]!r}")
    return out


def _restrictions_payload(r: dict | None) -> dict | None:
    if not r:
        return None
    return {"users": [u["login"] for u in r.get("users", [])], "teams": [t["slug"] for t in r.get("teams", [])], "apps": [a["slug"] for a in r.get("apps", [])]}


def _to_put_payload(got: dict) -> dict:
    """The GET response is not a valid PUT body (nested response objects, read-only urls); normalize to the fields the
    PUT accepts so a rollback actually restores (Codex round-4 P1)."""
    rsc = got.get("required_status_checks") or {}
    prr = got.get("required_pull_request_reviews")
    return {
        "required_status_checks": {"strict": bool(rsc.get("strict")), "contexts": sorted(rsc.get("contexts") or [c["context"] for c in rsc.get("checks", [])])} if rsc else None,
        "enforce_admins": bool(_flag(got, "enforce_admins")),
        "required_pull_request_reviews": None if not prr else {"dismiss_stale_reviews": bool(prr.get("dismiss_stale_reviews")), "require_code_owner_reviews": bool(prr.get("require_code_owner_reviews")), "required_approving_review_count": int(prr.get("required_approving_review_count", 0))},
        "restrictions": _restrictions_payload(got.get("restrictions")),      # preserve user/team/app restrictions on rollback (round-5 P1)
        "allow_force_pushes": bool(_flag(got, "allow_force_pushes")),
        "allow_deletions": bool(_flag(got, "allow_deletions")),
        "required_linear_history": bool(_flag(got, "required_linear_history")),
    }


def diff_report(current: dict | None, desired: dict) -> str:
    return "BEFORE:\n" + json.dumps(current, indent=2, sort_keys=True) + "\nAFTER:\n" + json.dumps(desired, indent=2, sort_keys=True)


def _loop_mode() -> bool:
    return os.environ.get("HARNESS_LOOP") == "1" or (REPO / ".harness" / ".loop-active").exists()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("cmd", choices=["show", "apply", "rollback", "verify", "tiebreaker"])
    p.add_argument("--confirm", action="store_true"); a = p.parse_args(argv)
    desired = desired_payload(blocking_contexts())
    if a.cmd == "verify":
        if _gh("auth", "status").returncode != 0:
            print("SKIPPED [1] main_protection.py:1: gh-auth-absent"); return 0     # a legal skip; lanes-phase0-check counts it RED
        problems = verify(current_protection(), desired)
        for m in problems: print(f"MISMATCH {m}")
        print("main-protection-verify:", "PASS" if not problems else "FAIL"); return 1 if problems else 0
    if a.cmd == "show":
        print(json.dumps(current_protection(), indent=2, sort_keys=True)); return 0
    if a.cmd == "apply":
        if _loop_mode(): raise SystemExit("apply refuses to run in loop mode (operator-gated; CLAUDE.md §12.4.1)")
        cur = current_protection(); print(diff_report(cur, desired))
        if not a.confirm:
            print("\nDRY RUN — nothing changed. After the operator approves THIS diff, run `just main-protection-apply-confirm`."); return 3
        (REPO / ".harness" / "plan" / "evidence-log-he-loop-lanes.md").open("a").write(f"\n## main-protection apply {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n```\n{diff_report(cur, desired)}\n```\n")
        r = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{_repo()}/branches/main/protection", "--input", "-"],
                           input=json.dumps(desired), capture_output=True, text=True, timeout=60)
        if r.returncode != 0: raise SystemExit(f"apply failed: {r.stderr.strip()}")
        # C-HE-08 §4: the settings are exercised BEFORE they are allowed to persist. The tiebreaker needs strict:true
        # live to be meaningful, so apply is provisional: a FAIL rolls back to the pre-change state (Codex round-3 P1).
        print("applied provisionally; running the tiebreaker (FAIL → automatic rollback)")
        rc = tiebreaker()
        if rc != 0:
            rb = _gh("api", "-X", "DELETE", f"repos/{_repo()}/branches/main/protection", timeout=60)
            if cur is not None:   # there was a prior protection: restore it from a NORMALIZED (PUT-shaped) payload
                restore = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{_repo()}/branches/main/protection", "--input", "-"], input=json.dumps(_to_put_payload(cur)), capture_output=True, text=True, timeout=60)
                if restore.returncode != 0 or verify(current_protection(), _to_put_payload(cur)):
                    raise SystemExit(f"tiebreaker FAILED and prior protection could NOT be restored — main is UNPROTECTED; re-run apply-confirm or restore by hand ({restore.stderr.strip()[:200]})")
            raise SystemExit(f"tiebreaker FAILED → protection rolled back (rc={rb.returncode}); settings NOT persisted")
        print("tiebreaker PASS; protection persists. Run `just main-protection-verify`."); return 0
    if a.cmd == "rollback":
        r = _gh("api", "-X", "DELETE", f"repos/{_repo()}/branches/main/protection", timeout=60)
        print("rolled back (pre-change show output is in the evidence log)"); return r.returncode
    # tiebreaker: scratch PR under strict:true + stale-refresh-branch check (HE-1 O4; C10-T8). Runs OUTSIDE loop mode.
    if _loop_mode(): raise SystemExit("tiebreaker is a live probe; run outside loop mode")
    return tiebreaker()


def tiebreaker() -> int:
    """C-HE-08 §4 (HE-1 O4; C10-T8): exercise strict:true on a scratch PR, then the load-bearing parameter -- a
    refresh-shaped PR branched from the since-superseded main is CAUGHT pre-merge or fast-forwards cleanly.
    Runs in an ISOLATED temporary worktree (never switches the operator's checkout; never picks up staged
    changes -- Codex round-3 P1) and compares the stale landing against the main SHA captured BEFORE that merge."""
    import tempfile
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime()); br = f"mp-tiebreaker-{ts}"; br2 = f"mp-tiebreaker-stale-{ts}"
    wt = Path(tempfile.mkdtemp(prefix="mp-tiebreaker-"))
    def sh(*c, cwd=None):
        q = subprocess.run(list(c), capture_output=True, text=True, timeout=180, cwd=cwd or wt)
        if q.returncode != 0: raise SystemExit(f"{' '.join(c)}: {q.stderr.strip()}")
        return q.stdout.strip()
    sh("git", "fetch", "-q", "origin", cwd=REPO)
    sh("git", "worktree", "add", "-q", "--detach", str(wt), "origin/main", cwd=REPO)
    try:
        base = sh("git", "rev-parse", "origin/main")
        sh("git", "checkout", "-q", "-b", br); sh("git", "commit", "-q", "--allow-empty", "-m", f"chore: main-protection tiebreaker {ts}"); sh("git", "push", "-q", "-u", "origin", br)
        url = sh("gh", "pr", "create", "--title", f"chore: main-protection tiebreaker {ts}", "--body", "scratch PR; C-HE-08 §4"); pr = url.rsplit("/", 1)[-1]
        print(f"tiebreaker: waiting for checks on #{pr} (strict:true requires up-to-date + green)")
        sh("gh", "pr", "checks", pr, "--watch"); head = sh("git", "rev-parse", "HEAD")
        m = subprocess.run(["gh", "pr", "merge", pr, "--squash", "--match-head-commit", head], capture_output=True, text=True, timeout=120, cwd=wt)
        if m.returncode != 0:
            print(f"precondition failed: scratch merge refused under strict:true ({m.stderr.strip()})"); return 1
        # the load-bearing parameter: a refresh-shaped PR branched from the since-superseded main
        sh("git", "checkout", "-q", "-b", br2, base); sh("git", "commit", "-q", "--allow-empty", "-m", "ops: stale refresh-shaped commit"); sh("git", "push", "-q", "-u", "origin", br2)
        url2 = sh("gh", "pr", "create", "--title", f"chore: stale-base tiebreaker {ts}", "--body", "C-HE-08 §4 stale-branch check"); pr2 = url2.rsplit("/", 1)[-1]
        state = sh("gh", "pr", "view", pr2, "--json", "mergeStateStatus", "--jq", ".mergeStateStatus")
        if state in ("BEHIND", "BLOCKED", "DIRTY"):
            verdict, why = "PASS", f"stale PR caught pre-merge (mergeStateStatus={state})"
        else:
            sh("git", "fetch", "-q", "origin"); pre = sh("git", "rev-parse", "origin/main")       # main BEFORE the stale merge
            head2 = sh("git", "rev-parse", "HEAD")
            m2 = subprocess.run(["gh", "pr", "merge", pr2, "--squash", "--match-head-commit", head2], capture_output=True, text=True, timeout=120, cwd=wt)
            if m2.returncode != 0:
                verdict, why = "PASS", f"stale merge REFUSED under strict:true ({m2.stderr.strip()[:120]})"
            else:
                sh("git", "fetch", "-q", "origin"); new_main = sh("git", "rev-parse", "origin/main")
                first_parent = sh("git", "rev-parse", f"{new_main}^1")
                verdict, why = ("PASS", "stale PR fast-forwarded cleanly onto the pre-merge main") if first_parent == pre else ("FAIL", f"stale PR landed off the pre-merge main (first parent {first_parent[:12]} != {pre[:12]})")
        print(f"tiebreaker: {verdict} — {why}")
        subprocess.run(["gh", "pr", "close", pr2, "--delete-branch"], capture_output=True, text=True, timeout=60, cwd=wt)
        return 0 if verdict == "PASS" else 1
    finally:
        subprocess.run(["bash", str(REPO / "tools" / "hooks" / "safe-worktree-remove.sh"), str(wt)], capture_output=True, text=True, timeout=120, cwd=REPO)


if __name__ == "__main__":
    raise SystemExit(main())
```
 `tiebreaker`: creates branch `mp-tiebreaker-<ts>` with one empty commit → PR → `gh pr merge --squash --match-head-commit` (through `merge_door` is NOT required here — this is the operator-gated live probe, run outside loop mode) → then creates a second branch from the pre-merge `main` SHA, opens a "refresh-shaped" PR, and asserts `gh pr view --json mergeStateStatus` reports `BEHIND`/`BLOCKED` (caught pre-merge) or the merge fast-forwards cleanly; prints PASS/FAIL and the evidence lines. Recipes:
```make
# ─── C-HE-08 branch protection for main (server-side X9 fence; operator-gated apply) ──────
main-protection-show:
    uv run python tools/main_protection.py show
# `apply` shows the diff and MUTATES NOTHING; the operator approves the actual payload (AskUserQuestion), then
# `apply-confirm` performs the provisional apply + tiebreaker (+ automatic rollback on FAIL). Never hard-code --confirm.
main-protection-apply:
    uv run python tools/main_protection.py apply
main-protection-apply-confirm:
    uv run python tools/main_protection.py apply --confirm
main-protection-rollback:
    uv run python tools/main_protection.py rollback
main-protection-verify:
    uv run python tools/main_protection.py verify
main-protection-tiebreaker:
    uv run python tools/main_protection.py tiebreaker
```
- [ ] **Step 4: GREEN** unit tests; register `Row("C-HE-08", "just:main-protection-verify", "phase0", "local", False, ("gh-auth-absent",))` and `Row("C-HE-08", "live:main-protection-tiebreaker + apply (operator-gated; evidence log)", "operator-gated", "loop, live", False)`.
- [ ] **Step 5: The operator gate (the ONE decision this unit surfaces).** Run `just main-protection-show` (expect 404 today) and `just main-protection-apply` **outside loop mode** — it prints the diff and mutates nothing (exit 3); Claude then asks one AskUserQuestion: *"Apply branch protection to `main` now? [the printed diff]"*; on approval `just main-protection-apply-confirm` applies **provisionally, runs the tiebreaker in a throwaway worktree, and rolls back automatically on FAIL** (C-HE-08 §4: tiebreaker before enforcing). Then `just main-protection-verify` (must be GREEN before `lanes-phase0-check` can pass). Record `show` (pre), `apply` (post), tiebreaker PASS lines in `.harness/plan/evidence-log-he-loop-lanes.md` (created in U-HE-44).
- [ ] **Step 6: Commit** — `git add tools/main_protection.py tools/test_main_protection.py justfile tools/lanes_verify.py tools/codex-parity-check.sh && git commit -m "feat(he-lanes): U-HE-27 main branch-protection recipes + verify phase0 row (C-HE-08 §2-5)"`.

**Rev 2026-08-21 (U-HE-27 execution corrections, as-built — 10 out-of-family rounds, 34 findings absorbed, 1 class register-and-held as B-191).** The Step-3 sketch above is the plan-time draft; the landed module diverges in the following load-bearing ways, each traceable to a codex round against the committed branch. *(i) Digest-bound approval (r2/r9 P1):* `apply` dry-run prints an approval digest over the (repository, BEFORE, AFTER) triple — slug derived from the repo's own `origin` remote, never gh ambient state — and `apply-confirm` takes it as a positional `just` arg (`main-protection-apply-confirm <digest>`), re-validating against fresh state INSIDE the lock; the approved slug is threaded verbatim through every mutation. *(ii) Lockfile-serialized transaction (r4/r5/r7/r9):* an exclusive-create lock in the git COMMON dir (one lock across linked worktrees) serializes apply-confirm, manual rollback, and the standalone tiebreaker. *(iii) Rollback correctness (r1/r2/r7/r8):* any tiebreaker escape reaches the rollback; prior policy restored by a single PUT (no unprotected DELETE window); rollback is CAS-guarded (re-read must still match OUR provisional payload) and the manual path refuses non-matching policies; ambiguous PUT/merge failures reconcile against authoritative state (live policy / PR `.state`) before deciding. *(iv) verify() is desired-relative* (serves the §2 target AND the restore-check), compares (context, app_id) pairs with the null↔-1 any-app normalization, always compares the optional controls (conversation-resolution/block_creations/lock_branch/fork_syncing), and flags app-bound checks against the any-app target. *(v) Tiebreaker witness shape (r1–r10):* fence-liveness precondition (a clean ffwd is also the no-protection outcome); both merges EXERCISED and reconciled, never inferred from `mergeStateStatus`; three-tier refusal attribution (strict / generic-needs-BEHIND / transport-indeterminate) with `base branch` in the generic tier; ffwd arm accepts any DESCENDANT of the pre-merge sample and re-verifies the fence before PASS; setup inside the GC scope; ownership-tracked timeout-safe cleanup in worktree→local→remote order, with PASS-but-cleanup-incomplete surfacing as rc 2 (apply keeps the validated fence, exits nonzero). *(vi) `_loop_mode()` scans all linked worktrees' `.loop-active` markers.* *(vii) Residual (B-191, register-and-held):* non-atomic remote CAS (GitHub exposes no If-Match for this endpoint) + evidence-log-only prior-policy restore — concurrent-trusted-administrator classes outside the X9 threat model. *(viii) Step 5 EXECUTED 2026-08-21/22 (post-#1418 follow-up arc):* operator approved via one AskUserQuestion; apply-confirm landed the fence (digest `09582edbd7a3f2b2`); two as-built live corrections surfaced and fixed same-session — GitHub stores a `contexts` submission as `checks` auto-bound to the Actions app (the r6 any-app comparison misflagged our own round-trip; now allows null/-1/15368 and still flags foreign apps), and `gh pr checks --watch` exits 1 with "no checks reported" before CI registers (retry + registration-complete validation in `_watch_checks`); `verify` PASS; §4 tiebreaker PASS (scratch #1420/#1421, clean ffwd onto the pre-merge lineage, fence re-verified in the PASS arm); full record in the evidence log — **the fence is LIVE and B-190's bound is IN FORCE**.

---

### U-HE-28: `just merge-door-unblock` + `ship-pr` merge-door steps + refresh continuation

**Scope.** Add the unblock recipe; rewrite `ship-pr`'s merge step to: acquire before constructing any merge command (`bash tools/hooks/safe-merge.sh <pr>` is the ONLY merge invocation), and remove its separate "open terminating refresh PR + merge" steps (the door does it as a continuation under the same lease); give `tools/roadmap_status_refresh.py` the `--emit-refresh-pr-json` flag the wrapper calls.

**Spec linkage.** C-HE-06 §1, §4(viii), §6 (unblock only via recipe; never a path-only unlink), §8 (yield to next gate pass; `wait_for_door` numbers), C-HE-01 Invariants (§12.2.1 one-file refresh preserved).

**Files.** Modify `justfile`, `.claude/skills/ship-pr/SKILL.md` (merge + refresh sections), `tools/roadmap_status_refresh.py` (flag), `tools/hooks/test_skill_reservation_wiring.sh` (extend greps).

**Depends on.** U-HE-23, U-HE-25.

- [ ] **Step 1: Failing test** (extend `test_skill_reservation_wiring.sh`):
```bash
grep -q 'bash tools/hooks/safe-merge.sh' "$SP" && ok "ship-pr merges through the wrapper" || bad "ship-pr lacks safe-merge"
grep -Eq '(^|[^`])gh pr merge' "$SP" && bad "ship-pr still carries a raw gh pr merge instruction" || ok "no raw merge verb in ship-pr"
grep -q 'merge-door-unblock' justfile && ok "unblock recipe present" || bad "no unblock recipe"
grep -q 'emit-refresh-pr-json' tools/roadmap_status_refresh.py && ok "refresh tool emits PR json" || bad "no --emit-refresh-pr-json"
```
- [ ] **Step 2: RED**; **Step 3: Implement.** Recipe:
```make
# C-HE-06 §6: clear a `blocked` merge-door lease -- operator-confirmed reclaim through the marker CAS,
# keyed to the blocked SHA. There is NO raw-unlink recipe by design.
merge-door-unblock pr sha:
    uv run python tools/merge_door.py unblock {{pr}} {{sha}} --lane-id "$HARNESS_LANE_ID"
merge-door-status:
    uv run python tools/merge_door.py status
```
`ship-pr/SKILL.md` merge section (replace the current `gh pr merge` + post-merge CI + refresh-PR steps):
> **Land through the merge door (C-HE-06/07).** After CI green + `merge-gate` all-APPROVE: `bash tools/hooks/safe-merge.sh <pr>`. This acquires the lease (fail-fast; on `held` it yields — do the next natural gate-pass, then retry; the wrapper's own `wait_for_door` applies base 30 s ×2 cap 10 min ×12 then routes `HITL-recoverable`), verifies head/base + `local-base-cas-check`, merges with the fixed string, confirms MERGED, flips the reservation, **holds the lease through the merge SHA's own `main` run and the terminating refresh PR as a continuation** (`.harness/roadmap_status.md`-only, §12.2.1 shape unchanged), then releases. Exit 0 = landed + refreshed; 3 = door blocked (a `DEFERRED-HIL` row names `just merge-door-unblock <pr> <sha>`); 4 = re-gate (base moved); 5 = budget exhausted (HITL). Never issue `gh pr merge` yourself; the guard denies it in loop mode.

`tools/roadmap_status_refresh.py --emit-refresh-pr-json`: performs the existing refresh (one-file shape enforced as today), commits on `roadmap-refresh-post-<pr>`, pushes, `gh pr create`, prints `{"pr": N, "head_sha": "<sha>"}` and exits 0; any failure exits non-zero with no JSON (the door then fails (viii) → blocked).

Add to `tools/roadmap_status_refresh.py` (after the existing refresh writes `.harness/roadmap_status.md`; the one-file shape check the tool already performs stays the gate):
```python
def emit_refresh_pr(post_pr: int, *, run=subprocess.run) -> dict:
    """C-HE-06 §4(viii): create the terminating refresh PR and print {pr, head_sha} for the merge door.
    Any failure raises -> non-zero exit, no JSON -> the door marks (viii) blocked. Never merges here."""
    branch = f"roadmap-refresh-post-{post_pr}"
    def sh(*args: str) -> str:
        p = run(list(args), capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise SystemExit(f"emit-refresh-pr: {' '.join(args)} failed: {p.stderr.strip()}")
        return p.stdout.strip()
    # IDEMPOTENT by branch name (Codex round-4 P1): a crash between `gh pr create` and the door's sidecar publish must
    # not orphan/duplicate the refresh. If the branch already has an open PR, return it; if the branch was pushed but
    # no PR exists, create the PR on it; only otherwise create the branch.
    existing = sh("gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,headRefOid", "--jq", ".[0] | select(.) | \"\\(.number) \\(.headRefOid)\"")
    if existing:
        n, head = existing.split(); return {"pr": int(n), "head_sha": head}
    if run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], capture_output=True, text=True, timeout=60).returncode == 0:
        sh("git", "fetch", "-q", "origin", branch); sh("git", "checkout", "-q", "-B", branch, f"origin/{branch}")
        url = sh("gh", "pr", "create", "--head", branch, "--title", f"ops: roadmap status refresh post-#{post_pr}", "--body", "terminating refresh (CLAUDE.md §12.2.1); landed by the merge door as a continuation (C-HE-06 §4(viii))")
        return {"pr": int(url.rstrip("/").rsplit("/", 1)[-1]), "head_sha": sh("git", "rev-parse", "HEAD")}
    sh("git", "checkout", "-b", branch)
    sh("git", "add", ".harness/roadmap_status.md")            # the ONLY file (§12.2.1)
    changed = sh("git", "diff", "--cached", "--name-only").splitlines()
    if changed != [".harness/roadmap_status.md"]:
        raise SystemExit(f"emit-refresh-pr: refresh must touch exactly .harness/roadmap_status.md, got {changed}")
    sh("git", "commit", "-m", f"ops: roadmap status refresh post-#{post_pr}")
    sh("git", "push", "-u", "origin", branch)
    url = sh("gh", "pr", "create", "--title", f"ops: roadmap status refresh post-#{post_pr}", "--body", "terminating refresh (CLAUDE.md §12.2.1); landed by the merge door as a continuation (C-HE-06 §4(viii))")
    pr = int(url.rstrip("/").rsplit("/", 1)[-1])
    return {"pr": pr, "head_sha": sh("git", "rev-parse", "HEAD")}
```
`main()`: `p.add_argument("--emit-refresh-pr-json", type=int, metavar="POST_PR")` → after the refresh, `print(json.dumps(emit_refresh_pr(args.emit_refresh_pr_json)))`. (The wrapper passes the landed PR number: `--refresh-cmd "uv run python tools/roadmap_status_refresh.py --emit-refresh-pr-json $1"` — update `safe-merge.sh` accordingly.)

- [ ] **Step 4: GREEN**, register the shell row (already registered in U-HE-21). Commit `feat(he-lanes): U-HE-28 ship-pr lands through the door; refresh as continuation; unblock recipe (C-HE-06 §1/§4/§6)`.

**Rev 2026-08-22 (U-HE-28 execution corrections, as-built).** The Step-3 sketch above is the plan-time draft; the landed code diverges in the following load-bearing ways. *(i) Fresh-path branch base:* the sketch created `roadmap-refresh-post-<pr>` from the invoking worktree's HEAD after a pre-run refresh — but the door invokes the wrapper from the TOPIC worktree, so both §12.2.1 halves would break: `codex_context_guard._is_terminating_refresh_commit` requires the refresh commit's recorded `git_head` to equal its OWN parent (a topic-HEAD parent fails it), and the PR's base...head diff would drag the whole arc diff into the shape gate. As-built, `emit_refresh_pr()` runs `git fetch -q origin main` + `git checkout -q -B <branch> origin/main` FIRST (the just-merged tip — race-free because the door holds the global lease through (viii)), and only then runs the refresh. *(ii) Refresh composition:* `--emit-refresh-pr-json N` performs the mechanical refresh itself via an in-process `--refresh` re-invocation with derived defaults (`--pr "PR #N"`, `--date` = UTC today, mechanical notes cell); `--notes`/`--next-action`/`--date` compose when given on the same command line. The door's fixed wrapper string passes none, so the door-driven refresh leaves the `## Next action` pointer UNTOUCHED — a named residual, self-healing for derivation (the pointer's standing "then <next unit>" tail + the C-HE-03 terminal-head dedup at re-selection), with the authored-pointer path available on any manual/recovery invocation. *(iii) Wrapper:* `$1` appended to the `--refresh-cmd` string per this unit's own body (the U-HE-25 as-built wrapper omitted it — pre-U-HE-28 the guard made the omission unreachable), and the U-HE-25 pre-lease availability guard is KEPT as a permanent probe (comment retitled), not removed: a stale checkout's refresh CLI lacking the flag would still wedge the shared door post-merge. *(iv) Witness flips (U-HE-25 rev (ii) discharge):* the hermetic `SM_EXPECT` byte-compare now carries the `268` PR argument on the refresh-cmd, and the REAL-CLI pin flips from `exit 69 pre-lease` to `probe passes → REAL merge_door refuses the nonexistent witness reservation fail-fast (DoorFailed → rc 4)` — witnessed with rc 69 (flag regressed) and rc 0/3/5 (door engaged real state) as named failures. *(v) ship-pr rewrite scope:* the door section landed between the final-gate back-fill and the §12.2 section; the §12.2 mechanical half is reframed as door-ridden (one terminating refresh per landing — the door's) with the manual recipe retained for the RECOVERY path only (blocked door / headless unreserved arc); the two raw `gh pr merge` mentions in the branch-hygiene comments were reworded because the Step-1 negative grep counts non-backticked occurrences file-wide. *(vi) Out-of-family r1 absorptions:* emit-mode STDOUT PURITY (the in-process `--refresh` printed progress lines into the stdout `merge_door` json-parses — the dispatch now routes every nested write to stderr and prints the one JSON line to real stdout); `ls-remote --exit-code` contract (only exit 2 is "no matching ref"; auth/transport codes abort instead of resetting a previously-pushed branch onto `origin/main`); `merge-door-unblock` expands `${HARNESS_LANE_ID:?...}` (an unset lane would mint a `lane_id=""` successor lease no wrapper invocation could resume — witnessed live). *(vii) Out-of-family r2 absorptions:* `merge_door.wait_pr_head_checks` gates the §4(viii) refresh merge on the refresh HEAD's own checks going green (under the LIVE C-HE-08 strict fence `gh pr merge` refuses a pending PR outright — not a timeout — so the unguarded drive burned the §5 re-issue budget deterministically on every normal landing; blocked-shape `refresh_pr_ci_not_green`, cross-unit edit into U-HE-23's carrier justified because this unit's flag is what makes the path reachable); the §12.2 pointer re-derivation rides a gitignored `.harness/.next-action-draft` (ship-pr authors `post-pr: <N>` + pointer body pre-invocation; emit consumes iff N matches its own POST_PR, ignores-and-leaves stale drafts; explicit `--next-action` wins) — superseding the pointer-untouched residual named at (ii); the branch-hygiene close-out now covers the accumulating `roadmap-refresh-post-<pr>` branches (`deleteBranchOnMerge: false` measured live; 7 pre-existing refresh branches already on the remote). *(viii) Out-of-family r3 absorptions:* recorded-resume head RE-ADOPTION (the durable refresh record pins the head at creation, but the sanctioned recovery for a red refresh head is a fix commit on the SAME PR — the resume now adopts the PR's current head iff it still passes the full terminating-refresh identity gate, republishing the record; a moved head that lost the identity gate adjudicates to a BLOCKED door via land()'s post-attempt escape handling, never a blind release); draft DURABILITY (the draft is read without unlinking and retired only once its content is durably represented — after `gh pr create` on the fresh path, at entry on the two resume paths whose pushed commit already carries it; a push failure leaves it for the retry; a matching draft is retired even when an explicit `--next-action` won, since the landing it names is complete). *(ix) Out-of-family r5 absorptions (defects the r2/r3 fixes themselves introduced):* the re-adoption record replacement is ATOMIC (`os.replace`, closing the unlink→create crash window that resumed as manual `refresh_intent_unresolved` instead of idempotent re-adoption); `wait_pr_head_checks` holds while ANY run on the SHA is still pending (a rerun reuses the SHA — an older completed run must not be judged as the verdict); the mismatched-draft warning ALSO rides the refresh PR body (the door discards the producer's stderr on success); the ship-pr refresh-branch close-out derives the refresh PR number by `gh pr view roadmap-refresh-post-<content-pr>` (the wrapper prints only `released` — the doc's earlier "the PR the door reported" was not followable). *(x) Out-of-family r6 absorptions (hardening of the r3/r5 fixes):* the adoption gate additionally binds to THIS landing (`post-#<content-pr>` title binding) and to the `main` base (`baseRefName` added to the Ground query — a refresh PR retargeted while receiving its fix commit must never squash into another branch under the global lease); the record replacement uses the door's own `publish_exclusive` on an unpredictable tmp name + symlink containment check before `os.replace`; draft retirement is REPRESENTATION-VERIFIED (`git show` of the pushed refresh commit's status file on both resume paths; fresh path retires only an ADOPTED draft — the r5 retire-on-override choice is superseded by the sounder never-delete-unrepresented-authoring rule, so an explicit-flag override now KEEPS the draft). *(xi) Out-of-family r7 absorptions:* the idempotent open-PR resume identity-gates the found PR (exact title + `main` base — a retargeted/reused PR on the deterministic branch aborts instead of persisting for the door); the adoption title binding is delimiter-safe (`post-#1` refuses `post-#10`); the pushed-branch recovery surfaces un-appliable supplied flags in the PR body (re-running the refresh there would break the §12.2.1 fixed point on squash — structurally correct to not apply, now loud); draft-retirement proof is scoped to the LIVE `Current next action` paragraph (body text in a notes cell is not installation); and the clearance marker was rewritten to describe the full (i)–(xi) as-built revision including the `merge_door.py` cross-unit hardening (it had gone stale at the round-0 draft). *(xii) Out-of-family r8 (4 absorbed, 1 refuted):* `--base main` pinned on both `gh pr create` sites (a stale `branch.<name>.gh-merge-base` must not retarget the refresh); the door re-validates the FULL refresh identity (state/head/base/title-binding/file-set) immediately before driving the merge — the §4(ii) re-confirm mirrored onto the §4(viii) continuation, closing the up-to-45-min wait's TOCTOU; draft-retirement proof upgraded from substring to EQUALITY with the live pointer body; the r5 residue witness's inert glob fixed (`*.adopt.*`). REFUTED with citation: "the idempotent paths are unreachable after a producer crash" — the `refresh_intent_unresolved` HITL gate is U-HE-23 codex r1 P2's deliberate contract (a blind re-call could mint a second PR), and emit's idempotency is exactly what makes both documented recoveries (`record-refresh`; `clear-refresh-intent` + re-land) converge without duplication; docstring sharpened to say so. *(xiii) Out-of-family r9 (1 absorbed — composition of two prior fixes):* the door's emitted recovery command (`just merge-door-unblock <pr> <sha>`, no env prefix) deterministically hit the r1 `${HARNESS_LANE_ID:?}` abort in a fresh shell; the recipe now derives the lane from the persisted `.harness/.lane-id` when the env var is unset and aborts loud (exit 64) only when NEITHER source exists — both branches live-witnessed. *(xiv) Out-of-family r10 (3 absorbed):* the idempotent lookup binds to the ORIGIN repository (`isCrossRepository == false` — a fork PR squatting the predictable branch name with the expected title/base/one-file shape is attacker-controlled head content the door's gates cannot distinguish); BOTH resume paths now REFUSE supplied `--notes`/`--date`/`--next-action` (supersedes the r7 warn-and-continue — silently handing the stale PR to the door would land the old pointer while the operator believes the correction applied); the r8 45-min TOCTOU fence gained its mutation witness (identity retargeted DURING the checks wait → blocked, no merge issued). *(xv) Out-of-family r11 (4 absorbed):* `--emit-refresh-pr-json --dry-run` refuses (the emitter has no side-effect-free preview); the live-pointer proof additionally requires the pointer's own `post-#` label to name THIS landing (a stale post-#54 pointer equal to the post-#55 draft body is not installation); a best-effort content recheck precedes the draft unlink (another agent's replacement between read and unlink survives; the read-vs-unlink instant remains an irreducible non-atomic window, noted inline); both `gh pr create --base main` pins gained exact argv witnesses. *(xvi) Out-of-family r12 (4 absorbed):* the ship-pr door invocation's PRIMARY form carries the inline `HARNESS_*` prefix in every venue (shell exports do not survive Bash tool calls — the bare form works only where the ids are already exported); the recovery-path wording no longer implies an unreserved arc can reach a post-merge state outside the U-HE-19 drain bootstrap; the pushed-branch recovery verifies PROVENANCE (the branch's first parent must BE the just-merged `origin/main` tip — a pre-pushed same-name branch of any other shape refuses fail-closed); draft retirement is an ATOMIC rename-claim (only the verified claimed copy is ever deleted; a replacement written after the claim survives at the path, one written before it is restored via a fail-closed link, and a third-writer collision preserves the displaced correction at the claim path). *(xvii) Out-of-family r13 (2 absorbed — one a P1-class defect in the r12 fix itself):* the provenance check resolves via the remote-tracking refs (`origin/<branch>^`), never `FETCH_HEAD` — the second fetch clobbered it, so every GENUINE crash recovery failed the r12 check (the witness stubbed both rev-parses independently and could not see the mutation); the draft is read with `O_NOFOLLOW` in both the fresh path and the retirement helper (a planted symlink at the gitignored path leaked an outside file's first line into the PR body — refused with a containment warning, symlink left in place). *(xviii) Out-of-family r14 (2 absorbed + 1 mitigated-with-named-residual):* an UNREPRESENTED same-PR draft now REFUSES both resume paths (mirroring the r10 flags refusal — handing the stale pair to the door silently lands the old pointer while one-refresh-per-landing forecloses the correction); the mismatched-draft warning is fully SANITIZED (no draft content reaches the public PR body — a pasted token in the gitignored file must never publish); the rename-claim crash micro-window (process death between rename and unlink/restore under a concurrent replacement) is gitignore-covered (`.next-action-draft*`) and named inline as a residual — discovery machinery deliberately not built (three-writer micro-window on a single-lane scratch file). *(xix) Out-of-family r15 (1 absorbed + 1 register-and-held):* the moved-head adoption path now calls the writer-side `_check_door()` containment guard before publishing the record (consistent with every other DOOR write path); the missing-draft pointer-freshness posture (self-healing fallback vs strict §12.2 fail-closed on the irreversible post-merge path) is a documented two-option policy fork raised at round 15 against the rev-(ii) choice — REGISTER-AND-HELD as `B-192` with the merge-gate spec-conformance lens named as the decorrelated arbiter. *(xx) Out-of-family r16 (1 absorbed):* every fetch whose result is read through a remote-tracking ref uses an EXPLICIT `+refs/heads/<name>:refs/remotes/origin/<name>` refspec — a bare `git fetch origin <name>` updates `FETCH_HEAD` but does not guarantee the tracking ref the next line reads advances (branching the refresh from a pre-merge `origin/main` would land a BEHIND refresh PR that strict protection rejects post-merge). *(xxi) Out-of-family r17 (3 absorbed — residues of r13/r14):* the representation reader goes through `origin/<branch>:...` (FETCH_HEAD is worktree-shared and raceable — the r13 pattern applied to its own sibling); the fresh path honors an `unrepresented` retirement result with the same refusal as the resume paths (a mid-landing correction must not silently hand the superseded pointer to the door); post-claim I/O errors return `unrepresented` with the claim path printed, never `absent` (a stale refresh must not proceed while claimed authoring sits stranded). *(xxii) Out-of-family r18 (5 absorbed — incl. one spec-conformance catch):* an EXISTING-but-unreadable draft fails loud (only ENOENT means absent; the symlink refusal stays warn-and-ignore per r13); the post-wait revalidation additionally pins `baseRefOid` to OUR content merge tip (base-NAME equality cannot see main ADVANCING during the 45-min wait — a UI-merged sibling PR would leave the refresh BEHIND and burn both merge attempts; now blocks with a re-mint cause); `--emit-refresh-pr-json` refuses EVERY other operation selector (not just `--dry-run`); the unblock recipe restores the C-HE-06 §6 canonical ONE-arg form (`sha` optional — read + confirmed from the blocked lease's own `blocked_at_sha`; the two-arg DEFERRED-HIL form retained); an argv-exact witness pins `default_ground().gh_view`'s `--json` field list (the fakes inject fields directly, so a reverted real query was previously invisible). *(xxiii) Out-of-family r19 (1 revert + 1 absorbed + 2 register-and-held → `B-193`):* the r18 baseRefOid pin is REVERTED — it wedged the door (after main advances, every valid re-minted refresh necessarily carries the NEW tip as base and is rejected against the old content-merge SHA forever); the BEHIND-refresh class (an out-of-band human merge while the door holds the lease — a coordination violation the door serializes against) blocks via the ordinary post-attempt adjudication and its CONVERGING recovery verb is registered as `B-193`, with the r17 mid-landing-correction intent chain (record-refresh can attach the stale PR; resumed land skips draft checks) folded in; a symlink-refused draft now surfaces in the refresh PR BODY (the door discards producer stderr on success — never silent); the B-192 register wording corrected (a malformed/mismatched draft WARNS in the PR body while the door proceeds — that posture is part of the B-192 fork, not "fails loud"). *(xxiv) Out-of-family r20 + convergence argument:* r20's four findings proposed ZERO production-code changes — all were witness-strengthening (concurrent-correction survival through the rename-claim; adoption-record no-gap under a replace fault; argv-exact tracking-ref fetch/show pins on both reader paths) and all were absorbed. Convergence is argued, not merely counted (per the workspace discipline): 20 rounds, 59 findings — 55 absorbed, 1 refuted by design-cite (r8), 3 register-and-held with named arbiters (`B-192` pointer-freshness posture; `B-193` BEHIND-refresh recovery design, absorbed-then-reverted with the wedge witness recorded) — and the terminal round's finding class shifted fully from code defects to test adequacy, the same terminal shape as U-HE-26/27. The 3-lens merge gate is the scheduled decorrelated arbiter for the held rows. *(xxv, superseded tally — see (xxvi)).* *(xxvi) 3-lens merge-gate round 1 (3× BLOCK, all absorbed) + CANONICAL coverage tally:* the concurrency lens found the one class 20 codex rounds missed — `emit_refresh_pr`'s `git checkout -B` mutated the INVOKING worktree's HEAD (no `cwd=` pin on the door's refresh closure) with no restoration, plus the cross-worktree already-checked-out collision on recovery; as-built, the fresh path builds in an EPHEMERAL DETACHED `git worktree` (materialized from `origin/main`, refresh written via `--status`/`--archive` into it, pushed with an explicit `HEAD:refs/heads/<branch>` refspec so NO local branch ever exists, best-effort removal in `finally`) and the pushed-branch resume leg performs NO checkout at all (head read from the tracking ref). The spec lens re-synced this clearance marker (it had drifted to (i)–(xi)) and this tally consolidation (the stacked (x)/(xviii)/(xxv) tallies are superseded in place); it independently GROUNDED and ENDORSED the `B-192` hold (terminal-head dedup keeps derivation correct; residual is the cosmetic session-start banner). The witness lens closed the composition gap: a REAL `merge_door land` subprocess now drives the REAL producer CLI through the production `--refresh-cmd` closure (JSON keys + stdout purity witnessed ACROSS the process boundary), plus adoption-sidecar symlink containment, rv2 head/files arms, unreadable-draft, and failed-verification-refuses rows. CANONICAL tally at this head (file totals, recounted programmatically per the count-drift discipline): `tools/test_roadmap_status_refresh.py` 92 test functions; `tools/test_merge_door.py` 112 test functions (incl. the composed producer e2e); `tools/hooks/test_skill_reservation_wiring.sh` 41 assertions; `tools/hooks/test_permission_guard.sh` 292 rows.* *(xxvii) 3-lens merge-gate round 2:* spec APPROVE (marker/tally re-sync verified; counts independently re-derived); concurrency APPROVE-in-intent (both r1 findings verified structurally resolved; its P3 advisory — a raising finally-cleanup could mask the in-flight error — absorbed as a guarded cleanup) with its verdict re-emitted schema-valid at round 3; witness BLOCK (round-1 gaps all verified closed; the NEW `--status`/`--archive` isolation threading was itself unwitnessed) — absorbed: a default-closure witness pins both flags to the ephemeral worktree path, and the two exception-path rows assert cleanup fires; the resume-leg stale comment re-worded (spec nit).* *(The two older tallies formerly stacked here were superseded in place by the canonical (xxvi) tally — merge-gate r1 spec lens.)*

---
# S4d — `loop_status` venue + coalescing + env isolation + emitting detections

### U-HE-29: `loop_status.md` shared venue, structured column, `NOTIFY` / `COALESCE-DELIVERED` kinds, ACTIVATE scoping, rendered `[lane_id]`, pointer sweep

**Scope.** `loop_status_path()` resolves to the shared venue `QUEUE_DIR/../loop_status.md` (default `~/.gstack/projects/arhugula-v2/loop_status.md`, override `HARNESS_LOOP_STATUS_PATH`) for every caller; control markers stay per-lane; every emitted row carries the structured column **before** detail (`lane=<lane_id>;cause=<cause|->`); reducers accept both shapes; the "since last ACTIVATE" reset is struck for HIL rows; `NOTIFY` is append-only, rendered beside (never merged into) the DEFERRED-HIL summary and excluded from the skip-set; `COALESCE-DELIVERED` kind declared; every literal `.harness/loop_status.md` pointer swept.

**Spec linkage.** C-HE-09 §1 (single file), §2 (venue determinism; markers per-lane; pointer sweep list), §3 (row shape; rejoin defect; reducers key detail's first token; `[<lane_id>]` rendered), §4 (ACTIVATE option (b)), §5 (kinds), §6, Invariants; C-HE-20 §1 (NOTIFY for informational).

**Files.** Modify `tools/hooks/loop_lib.sh` (`:6`, `:24-27`, `:73-85`, `:127-157`, `:165-186`, `:191-231`), `tools/hooks/test_loop_lib.sh`, `tools/hooks/session-start.sh` (render NOTIFY beside HIL), `.claude/skills/loop-start/SKILL.md:16,34`, `.claude/skills/loop-stop/SKILL.md:23`, `.claude/skills/resolve/SKILL.md:15`, `.claude/skills/ship-pr/SKILL.md:309`.

**Interfaces (bash).**
```bash
loop_status_path            # → ${HARNESS_LOOP_STATUS_PATH:-<dirname of QUEUE_DIR>/loop_status.md}
loop_log <kind> <detail...>            # now writes: | ts | kind | lane=${HARNESS_LANE_ID:--};cause=${LOOP_CAUSE:--} | detail |
loop_log_structured <kind> <lane_id> <cause_signature> <detail...>
loop_notify_summary                    # last 5 NOTIFY rows (24 h), one line, for SessionStart
_loop_row_detail_awk                   # shared awk prelude: detects structured vs legacy, sets d (detail), lane
```

**Depends on.** (none). (Precedes U-HE-18/23 emitters per the shape decision.)

- [ ] **Step 1: Failing tests** (`tools/hooks/test_loop_lib.sh` — the file's `ok/bad`, `loop_now` stub idioms; the harness now sets `export HARNESS_LOOP_STATUS_PATH="$REPO/shared/loop_status.md"` at the top instead of relying on `CLAUDE_PROJECT_DIR`):
```bash
# ── C-HE-09 §2 venue determinism: two worktrees, hook and raw-shell contexts → ONE path
P1=$(CLAUDE_PROJECT_DIR="$REPO/wt-a" bash -c 'source tools/hooks/lib.sh; source tools/hooks/loop_lib.sh; loop_status_path')
P2=$(cd "$REPO/wt-b" && unset CLAUDE_PROJECT_DIR; bash -c 'source '"$PWD_ROOT"'/tools/hooks/lib.sh; source '"$PWD_ROOT"'/tools/hooks/loop_lib.sh; loop_status_path')
[ "$P1" = "$P2" ] && [ "$P1" = "$HARNESS_LOOP_STATUS_PATH" ] && ok "venue resolves to one shared path" || bad "venue split: $P1 vs $P2"
M1=$(CLAUDE_PROJECT_DIR="$REPO/wt-a" bash -c 'source tools/hooks/lib.sh; source tools/hooks/loop_lib.sh; loop_marker_path')
[[ "$M1" == "$REPO/wt-a/"* ]] && ok "control marker stays per-lane" || bad "marker not per-lane: $M1"

# ── C-HE-09 §3 row shape: structured column BEFORE detail; cause with ':' renders w/o stray '|'
: > "$(loop_status_path)"; loop_status_ensure >/dev/null
HARNESS_LANE_ID=lane-1 loop_log_structured DEFERRED-HIL lane-1 'merge-door-lease-acquire:transient-retry:lease_contended' 'B-9 — waiting on the door | pipe in reason'
ROW=$(tail -1 "$(loop_status_path)")
[[ "$ROW" == '| '*' | DEFERRED-HIL | lane=lane-1;cause=merge-door-lease-acquire:transient-retry:lease_contended | B-9 — waiting on the door \| pipe in reason |' ]] && ok "structured column before detail" || bad "row shape: $ROW"
LIST=$(loop_pending_hil_list)
[[ "$LIST" == *'[lane-1] B-9 — waiting on the door | pipe in reason'* ]] && ok "rendered [lane_id] + unescaped pipe, no stray column" || bad "render: $LIST"
# legacy 3-column row still parses
printf '| 2026-08-18T00:00:00Z | DEFERRED-HIL | B-10 — legacy row |\n' >> "$(loop_status_path)"
[[ "$(loop_skip_set)" == *B-10* && "$(loop_skip_set)" == *B-9* ]] && ok "legacy + structured both reduce" || bad "skip-set: $(loop_skip_set)"

# ── C-HE-09 §4 ACTIVATE scoping (option b): a lane's ACTIVATE never hides another lane's deferral
: > "$(loop_status_path)"; loop_status_ensure >/dev/null
HARNESS_LANE_ID=L1 loop_defer B-1 "lane one deferred"
HARNESS_LANE_ID=L2 loop_activate "lane two starts" >/dev/null
[[ "$(loop_skip_set)" == "B-1" ]] && ok "ACTIVATE does not reset HIL rows" || bad "ACTIVATE reset skip-set: '$(loop_skip_set)'"
HARNESS_LANE_ID=L2 loop_resolve B-1 "resolved by lane two"
[ -z "$(loop_skip_set)" ] && ok "RESOLVED-HIL is the only exit" || bad "resolve failed"

# ── C-HE-09 §5 NOTIFY: rendered beside, excluded from skip-set
: > "$(loop_status_path)"; loop_status_ensure >/dev/null
loop_log_structured NOTIFY L1 'reservation-stale:HITL-recoverable:pending_aged' 'B-2 pending > 24h'
[ -z "$(loop_skip_set)" ] && ok "NOTIFY not in skip-set" || bad "NOTIFY leaked into skip-set"
[[ "$(loop_notify_summary)" == *'B-2 pending > 24h'* ]] && ok "NOTIFY rendered" || bad "NOTIFY not rendered"

# ── pointer sweep
[ "$(grep -rl '\.harness/loop_status\.md' tools/hooks/loop_lib.sh .claude/skills/loop-start/SKILL.md .claude/skills/loop-stop/SKILL.md .claude/skills/resolve/SKILL.md .claude/skills/ship-pr/SKILL.md 2>/dev/null | wc -l | tr -d ' ')" = "0" ] && ok "pointer sweep: 0 literal hits" || bad "stale .harness/loop_status.md pointers remain"
```
- [ ] **Step 2: RED**; **Step 3: Implement** in `loop_lib.sh`:
```bash
# Path to the append-only status ledger -- the SHARED venue (C-HE-09 §2): one file for every lane
# and every caller, hook or raw shell. QUEUE_DIR-adjacent (outside every worktree), never
# per-worktree: DEFERRED-HIL / RESOLVED-HIL / NOTIFY / COALESCE-DELIVERED rows must reduce to
# one answer for every lane (X6/E10). Control markers (ACTIVATE marker, .loop-iter, .loop-halt)
# stay per-lane under hook_project_dir().
loop_status_path() {
  if [ -n "${HARNESS_LOOP_STATUS_PATH:-}" ]; then printf '%s' "$HARNESS_LOOP_STATUS_PATH"; return 0; fi
  local q="${ARC_METRICS_QUEUE_DIR:-$HOME/.gstack/projects/arhugula-v2/arc-metrics-queue}"
  printf '%s' "$(dirname "$q")/loop_status.md"
}

# Append a ledger row. Usage: loop_log <kind> <detail...>
# Row shape (C-HE-09 §3): | ts | kind | lane=<lane_id>;cause=<cause_signature|-> | detail |
# The structured column goes BEFORE detail: _loop_pending_hil_rows rejoins $4..NF-1 to restore
# escaped pipes, so anything appended AFTER detail would be glued into the rendered reason.
loop_log() {
  local kind="$1"; shift
  loop_log_structured "$kind" "${HARNESS_LANE_ID:--}" "${LOOP_CAUSE:--}" "$@" || true   # hooks: never break the caller
  return 0
}
loop_log_structured() {
  local kind="$1" lane="$2" cause="$3"; shift 3
  local detail="$*"
  local p; p=$(loop_status_ensure)
  [ -z "$p" ] && return 0
  lane=$(printf '%s' "$lane" | tr -d ' \t|'); cause=$(printf '%s' "$cause" | tr -d ' \t|')
  detail=$(printf '%s' "$detail" | tr '\n' ' ' | sed 's/|/\\|/g')
  # Returns 1 when the shared ledger cannot be written (Codex round-3 P2): coordination callers (reservations.py,
  # merge_door.py via emit_loop_row) MUST propagate -- an unrecorded DEFERRED-HIL/NOTIFY is a lost recovery signal.
  # Legacy hook callers go through loop_log, which keeps its always-0 contract.
  if ! printf '| %s | %s | lane=%s;cause=%s | %s |\n' "$(loop_now)" "$kind" "${lane:--}" "${cause:--}" "$detail" >> "$p" 2>/dev/null; then
    echo "loop_log_structured: cannot write $p" >&2; return 1
  fi
  return 0
}
```
Shared awk prelude (a bash variable sourced into every reducer):
```bash
# Detects the structured column: sets `d` = detail (rejoined $start..NF-1, escaped pipes preserved)
# and `lane` (or "-" for legacy 3-column rows).
_LOOP_AWK_ROW='
  function rowparse(   i, start) {
    k = $3; gsub(/^[ \t]+|[ \t]+$/, "", k)
    if ($4 ~ /^[ \t]*lane=/) { lane = $4; sub(/^[ \t]*lane=/, "", lane); sub(/;.*$/, "", lane); start = 5 } else { lane = "-"; start = 4 }
    d = $start; for (i = start + 1; i < NF; i++) d = d "|" $i
    sub(/^[ \t]+/, "", d); sub(/[ \t]+$/, "", d)
    split(d, a, /[ \t]/); tok = a[1]
  }'
```
`loop_skip_set` awk → `"$_LOOP_AWK_ROW"' { rowparse() } k == "DEFERRED-HIL" || k == "RESOLVED-HIL" { state[tok] = (k == "DEFERRED-HIL") ? "PENDING" : "RESOLVED" } END {...}'` — **the `k == "ACTIVATE" { delete state }` line is removed** (option (b)). `_loop_pending_hil_rows` → same prelude; `detail[tok] = "[" lane "] " d`; ACTIVATE reset removed. `loop_resolve`'s own-write grep matches `"| RESOLVED-HIL | lane=" ... "| ${escaped} |"` (build the exact structured row string the same way `loop_log_structured` does). Add:
```bash
# NOTIFY rows: append-only informational; rendered BESIDE the DEFERRED-HIL summary at SessionStart,
# never merged into it, never in the skip-set (C-HE-09 §5). Last 5 rows within 24 h.
loop_notify_summary() {
  local p; p=$(loop_status_path); [ -f "$p" ] || return 0
  local now; now=$(_loop_epoch "$(loop_now)")
  local rows; rows=$(awk -F'|' "$_LOOP_AWK_ROW"' { rowparse() } k == "NOTIFY" { ts = $2; gsub(/^[ \t]+|[ \t]+$/, "", ts); print ts "\t[" lane "] " d }' "$p" 2>/dev/null \
    | while IFS=$'\t' read -r ts line; do [ $(( now - $(_loop_epoch "$ts") )) -le 86400 ] && printf '%s\n' "$line"; done | tail -5 | sed 's/\\|/|/g')   # 24 h horizon (round-5 P2)
  [ -n "$rows" ] && printf '[loop] ℹ notify: %s' "$(printf '%s\n' "$rows" | paste -sd';' - | sed 's/;/; /g')"
}
```
`loop_pending_hil_summary`'s trailing text `See .harness/loop_status.md` → `See $(loop_status_path)`; header comment `:6` reworded; `session-start.sh` prints `loop_notify_summary` on its own line after the HIL summary. Pointer sweep: the four skill files' literals → *"the shared `loop_status.md` (`$(loop_status_path)`; default `~/.gstack/projects/arhugula-v2/loop_status.md`)"*.
- [ ] **Step 3b: write-failure test** (`test_loop_lib.sh`): `HARNESS_LOOP_STATUS_PATH=/nonexistent-dir/x.md loop_log_structured NOTIFY L1 a:b:c "detail"; [ $? -eq 1 ] && ok "structured write failure returns 1" || bad "..."`; and `loop_log NOTIFY x` under the same path returns 0 (hook contract preserved).
- [ ] **Step 4: GREEN** (`bash tools/hooks/test_loop_lib.sh` + `test_permission_guard.sh` + `test_stop_gate.sh` etc. — every hook test that reads the ledger must set `HARNESS_LOOP_STATUS_PATH`; sweep with `rg 'loop_status_path|loop_status.md' tools/hooks/test_*.sh`). Probes: ACTIVATE reset (`--lines` cannot re-add a line — witness is the §4 test; record) and row shape (`--lines` = the `if ($4 ~ /^[ \t]*lane=/)` branch → structured rows mis-parse → RED) → PINNED. Register `Row("C-HE-09/10", "shell:tools/hooks/test_loop_lib.sh", "phase0", "local + CI", True)`.
- [ ] **Step 5: Commit** — `git add tools/hooks/loop_lib.sh tools/hooks/test_loop_lib.sh tools/hooks/session-start.sh .claude/skills/loop-start/SKILL.md .claude/skills/loop-stop/SKILL.md .claude/skills/resolve/SKILL.md .claude/skills/ship-pr/SKILL.md tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-29 shared loop_status venue, structured column, NOTIFY kind, ACTIVATE scoping, pointer sweep (C-HE-09)"`.

**As-built rev note (2026-08-22, U-HE-29 landing).** Every C-HE-09 / C-HE-20 contract this unit
cites is unchanged; the corrections below are execution-time findings against the unit's own
sketch, recorded per the as-built discipline.

(i) **`tools/roadmap-audit/session-start.sh`, not `tools/hooks/session-start.sh`.** The unit's Files
list named a hook path that does not exist at HEAD. The SessionStart carrier is
`tools/roadmap-audit/session-start.sh` (it already sources `loop_lib.sh` at `:22-24` and renders
`loop_pending_hil_summary` at `:37-43`); `loop_notify_summary` is rendered there, as its own
segment appended after the HIL segment, so an empty HIL summary still surfaces notices and a
populated one is never diluted (C-HE-09 §5 "beside, never merged into").

(ii) **`tools/arc_exit_report.py` is a SECOND live carrier of the venue — absorbed, mandatory.**
Not in the unit's Files list. It held its own `LEDGER_REL = ".harness/loop_status.md"`
derivation and used it at 8 sites, including `append_ledger_row`'s pre/post growth check. Moving
the venue without it is a LANDING-ORDER DEFECT, not a cosmetic miss: `loop_log` would append to
the shared venue while the growth check watched the old per-worktree path, so the check would
see no growth, `append_ledger_row` would return False, and `main()` would fail closed with
exit 3 on EVERY arc closed through ship-pr. Absorbed by replacing the constant with a
`ledger_path(code_root)` resolver that ASKS `loop_status_path` (one authority, honouring
`HARNESS_LOOP_STATUS_PATH` / `ARC_METRICS_QUEUE_DIR` identically to every hook) and returns
None -- never a guessed fallback -- when it cannot resolve.

(iii) **The R-7 worktree-vs-main ledger discrimination is DELETED, not ported.** `_resolve_roots`
carried a wt_ledger/main_ledger branch whose two notes claimed rows came from the invoking
worktree's private ledger and that the main checkout's "SEPARATE ledger" was not borrowed. Under
one shared venue both claims are FALSE, so re-stating either would be a fabricated provenance
note. Replaced with a single honest note naming the shared venue. Its tests were re-cut the same
way: `test_r7_worktree_rows_win_over_the_main_checkouts` /
`..._without_a_ledger_is_an_honest_empty_never_borrowed` became
`test_u_he_29_worktree_read_reaches_the_shared_venue_with_lane_attribution` /
`..._no_shared_ledger_is_an_honest_empty`. The old exclusion was a workaround for the venue
split, not a goal — two lanes gated on the same operator are both real obligations, and the
`[lane_id]` render (§3) is what makes the shared list actionable.

(iv) **`_loop_epoch` does not exist; the epoch helper became a shared AWK prelude.** The sketch's
`loop_notify_summary` called a `_loop_epoch` shell helper that is nowhere at HEAD — the epoch
logic lived inline in `_loop_hil_ttl_resurface_unlocked`'s awk. Rather than add a shell helper
that forks per row, the Hinnant days-from-civil pair was lifted into `_LOOP_AWK_EPOCH` beside
`_LOOP_AWK_ROW` and both reducers now share it. Same rule as the row parser: one parse, one
authority.

(v) **`_loop_structured_col` + `_loop_escape_detail` extracted.** `loop_resolve` verifies its own
write by grepping for the exact row it just appended, so it must reproduce the structured
column and the pipe-escaping byte-for-byte. Building that string twice (once in the writer, once
in the checker) is a drift surface that would silently break the resolve verification the
moment either spelling changed; both now call the same two helpers.

(vi) **The ACTIVATE-reset strike reaches `_loop_hil_ttl_resurface_unlocked` too.** §4 strikes the
reset "for HIL rows"; the TTL re-surfacer reduces HIL rows, so its own
`k == "ACTIVATE" { delete state; delete at }` was struck as well. Left in place, a sibling lane's
ACTIVATE would silence another lane's aged deferral — precisely the notification the C-HE-20 TTL
exists to deliver.

(vii) **Six shell suites + two pytest suites pin `HARNESS_LOOP_STATUS_PATH`.** Beyond
`test_loop_lib.sh`, the venue move stranded `test_stop_loop.sh`, `test_resolve_lib.sh`,
`test_permission_guard.sh`, `tools/04-loop/test_run.sh`, `tools/04-loop/test_loop_wrappers.sh`,
`tools/roadmap-audit/test_session_start.sh`, `tools/test_review_wrapper.py` and
`tools/test_arc_exit_report.py`. In `test_arc_exit_report.py` the pin is an autouse fixture that
sets the env var DIRECTLY rather than through `monkeypatch.setenv`: several tests there call
`monkeypatch.undo()` mid-test to drop their `run` stub, and `monkeypatch` is one function-scoped
instance shared with the fixture — an `undo()` would have silently unpinned the venue mid-test
and pointed the next real subprocess at the OPERATOR's live ledger.

(viii) **Two more skip-set scoping assertions inverted.** Besides `test_loop_lib.sh` tests 10 and
17d, `test_stop_loop.sh` test 7 asserted that a DEFERRED-HIL row preceding an ACTIVATE is
excluded from the injected skip-set. Inverted per §4 and strengthened: the still-open pre-ACTIVATE
row must now be INCLUDED, and a companion RESOLVED-HIL row proves resolution remains the only exit.

(ix) **`Row("C-HE-09/10", "shell:tools/hooks/test_loop_lib.sh", ...)` already existed** (landed at
U-HE-09); the manifest keys rows by artifact, so it was not duplicated. A second row registers the
newly-load-bearing carrier from (ii): `Row("C-HE-09", "pytest:tools/test_arc_exit_report.py",
"phase0", "local + CI", False)`.

(x) **The U-HE-18 SessionStart activation gate self-activated.** `tools/roadmap-audit/session-start.sh:124-125` gates
its detached `reconcile-all` spawn on `grep -q 'loop_log_structured()' tools/hooks/loop_lib.sh`.
That writer now exists, so the pass runs and its C-HE-20 escalation rows reach the shared ledger
instead of only the store-local `.reconcile.log` (the pre-U-HE-29 registered residual). The gate
is RETAINED rather than removed — it is a live precondition, not a countdown: a checkout whose
`loop_lib.sh` predates U-HE-29 (a bisect, an old worktree) would otherwise run an unattended pass
that can only fail closed into the log.

(xi) **Out-of-family review, rounds 1-3 (11 findings absorbed).** Recorded because three are
contract-shaped, not cosmetic.

*r1.* (a) A first-writer TOCTOU on the now-SHARED venue: `loop_status_ensure` tested `-f`
then truncated with `cat >`, so a losing lane could erase rows the winner had appended.
(b) A RELATIVE `HARNESS_LOOP_STATUS_PATH` / `ARC_METRICS_QUEUE_DIR` silently defeated §2 --
each lane resolved the same string against its own CWD while every string comparison still
reported the venue identical; now rejected, since absolutizing against CWD cannot help when
the CWD is what differs. (c) The SessionStart NOTIFY wiring had no witness. (d) Two ship-pr
instructions still said worktree disposition deletes the ledger.

*r2.* (e) `set -o noclobber` did NOT close (a): O_EXCL publishes the file at open() time, so
a second lane's `-f` test passes and it appends while the winner is still writing the header
through a non-append fd whose absolute-offset writes land on top. Replaced with a publication
protocol -- stage the header to a temp file, publish with `ln` (atomic, EEXIST) -- so the
venue never exists partially written. (f) **The cutover would have STRANDED every still-open
deferral in the pre-U-HE-29 per-worktree ledgers.** Grounded, not hypothetical: the root
checkout's legacy ledger alone appeared to carry 2 open obligations invisible to the new
reducer — a measurement later shown to be WRONG; see (xvii). (g) `_loop_structured_col` stripped spaces/tabs/pipes but not newlines,
so a newline-bearing lane id split one row across two lines and the reducer could drop the
gate.

*r3.* (h) The migration from (f) was **dead code** -- no production path called it, so
production would still have switched venues with those rows unread. Wired into SessionStart
behind a sentinel, witnessed by a test that drives it through the hook rather than calling
the helper directly. (i) The migration concatenated whole legacy ledgers in worktree-
enumeration order, but reducers use PHYSICAL last-write-wins and ignore timestamps -- an
older `RESOLVED-HIL` from a later-enumerated worktree could follow and clear a newer open
gate. Narrowed to import only each file's still-OPEN rows, each reduced in its own correct
order: the import is then purely `DEFERRED-HIL`, which can only ADD to the skip-set, the safe
direction by §4's own rule. (j) **`todo_for_human` had quietly changed meaning.** The shared
venue made `loop_pending_hil_list` return every lane's gates, so PR A's closure record would
carry a sibling lane's obligations -- and the re-cut test in (iii) *blessed* it. U-WT-03's
per-arc contract is restored by scoping rows to the invoking worktree's `.harness/.lane-id`
(falling back to `HARNESS_LANE_ID`), with the excluded count STATED in the notes; with no
resolvable lane id every row is kept and the report says it is unfiltered. (k) `loop_resolve`
verified its write by grepping the WHOLE file, which proves a matching row exists, not that
this call wrote one -- on a shared, never-truncated, cross-run venue an identical historical
row is plausible, and a false "resolved" is the one answer that function must never give. It
now searches only the bytes appended since its own write began.

(xii) **A probe non-kill was reported, not tuned away.** The 12-lane create race added for
r1(a) was mutation-probed with the fix removed and stayed GREEN -- forked subshells almost
never interleave inside the microseconds between the `-f` test and the redirect. Rather than
tune the race until it flaked red (pinning luck, not the mechanism), the fix is pinned
structurally, exactly as tests 15c/15d in the same file already pin `loop_resolve`'s own-write
check and for the same stated reason. The race test remains an end-to-end sanity check and is
labelled as one.

(xiii) **Test pollution found in the operator's live shared venue.** Because the venue is
resolved from the environment rather than the repo, suites that had not yet pinned
`HARNESS_LOOP_STATUS_PATH` wrote four rows (`B-777`, `B-779`, `R-300`, `R-410`) into the real
`~/.gstack/projects/arhugula-v2/loop_status.md` during this arc. Cleared through the ledger's
own append-only mechanism (`RESOLVED-HIL` rows naming them as test artifacts) rather than by
editing the file. This is why the pins in (vii) are load-bearing rather than tidiness.

(xiv) **Rounds 4-6: the absorption rounds became the defect source, and the arc converged by
narrowing, not by adding.** r4's six findings, r5's five and r6's five were almost entirely
about machinery r1-r3 had *introduced* -- the migration and the lane rule. Two shape
corrections, not more patches, did the work:

*The lane rule settled on its third shape.* r3 filtered `todo_for_human` to the invoking
lane; r4 showed that drops this arc's OWN rows (the canonical `defer.sh` sets no lane, and
migrated rows carry `-`), so it was reverted to keep everything; r5 showed keeping everything
puts a sibling's obligations in this PR's closure record. The settled rule keys on what is
KNOWN: exclude a row whose lane is demonstrably some OTHER lane's, keep mine and keep the
unattributed, count every exclusion. Neither extreme was right, and the middle is not a
compromise -- it is the only rule that never hides a row that might be this arc's.

*The migration hardened along one axis: claim, then read.* r5 moved the atomic rename BEFORE
the reduce (a concurrent pre-U-HE-29 writer's row was being carried into the archive
unimported and never seen again, since later passes look only at the original filename), and
r6 added recovery of orphaned `.migrating-*` claims so a crash between claim and retire
cannot hide a ledger permanently. r6 also narrowed the retry dedupe from the ITEM to the ROW:
keyed on the item it suppressed every future legacy deferral for that item forever, breaking
the last-write-wins rule that a later deferral reopens a gate; keyed on the row, a re-seen
identical row is a retry and a genuinely new deferral still lands.

Also absorbed across these rounds: `mktemp` for the staging file (`$$` is shared across a
bash's subshells and `$RANDOM` is 15 bits, so a guessable staging name reopened the
partial-publication race the `ln` protocol exists to close); `_loop_lane_id` resolving the
persisted `.harness/.lane-id` when `HARNESS_LANE_ID` is unset (the canonical `defer.sh` and
the ordinary hooks never export it, so rows lost their §3 attribution in a worktree that had
carried a lane id all along); a `mig=ERR(...)` SessionStart segment so a failed cutover
surfaces instead of rendering as success; and gitignore entries for the `.migrated-*` /
`.migrating-*` residue, which `hook_worktree_local_state` would otherwise treat as precious
untracked state and refuse to dispose the worktree forever.

(xv) **One residual REGISTERED, not fixed: B-194.** The lane encoding is a destructive strip,
not a reversible encoding, so two ids differing only in stripped characters render the same.
The *misclassification* half is fixed here (`arc_exit_report._sanitize_lane` applies the
identical strip before comparing, so a separator-bearing lane no longer reads its own rows as
foreign); the *collision* half is inherent to a lossy mapping and needs a decision between
rejecting unsafe ids at mint time and adopting one reversible encoding across all three
carriers. It is not exploitable at HEAD -- minted ids contain none of the stripped
characters. Registered at `.harness/forward-register.yaml` B-194 with both options and the
one-commit-across-three-carriers constraint recorded, per the register-and-hold discipline
at the round cap.

(xvi) **Rounds 7-8, and where the arc stopped.** r7 replaced the dedupe heuristic outright:
imported rows are necessarily appended LAST into a log the reducers read by PHYSICAL order,
so no text-similarity key can be right. The import now compares the legacy row's own
timestamp against the shared venue's last resolution of that item — skip only what the venue
already answered AT OR AFTER the row was written. That single rule subsumes both earlier keys
and is witnessed in all three directions (stale skipped, newer imported, never-answered
imported regardless of age). r7 also fixed orphan-fold ordering, matched the lane precedence
across writer and reader (they disagreed, so an arc could classify its own rows foreign), and
stripped `[`/`]` from lane ids because the rendered `[lane] detail` form is parsed back by
brackets. Its P3 caught a second suite (`test_loop_gc.sh`) mutating the operator's live
ledger — the same class as (xiii), found in a place the first sweep missed.

r8 returned four findings, and their SHAPE is what closed the arc rather than fatigue. Two
were cheap and clearly right and were taken: multi-orphan chronological folding (folding one
at a time reversed the order whenever more than one claim existed), and a masked `awk`
failure inside a pipeline that made an unreadable ledger read as empty in any caller without
`pipefail` — which the migration would then retire as fully imported. The other two are
**registered as B-195**, with reasons rather than deferral: (a) the staleness check and the
import append are not atomic, but the consequence is C-HE-09 §4's SAFE direction — the item
stays in the skip-set and resurfaces for the operator, no data loss and no wrongly-retried
gate; (b) a pre-U-HE-29 writer holding an fd on the legacy inode keeps appending after the
claim rename, which is **not fixable from this side** — that process takes none of our locks
and holds the inode regardless of its name. Both windows are self-closing: they exist only
while a pre-U-HE-29 checkout is still running.

Eight rounds, ~30 findings absorbed. The honest summary is that r1-r3 hardened the UNIT and
r4-r8 hardened the machinery r1-r3 had introduced — the cutover migration in particular. Each
round's fix was the previous round's defect surface, and the arc converged only when two
shapes were NARROWED (the lane rule to "exclude the demonstrably foreign"; the import rule to
"compare timestamps") rather than patched again.

(xvii) **CORRECTION — the migration's founding measurement was wrong, and codex r14 caught
it.** Rounds 2-13 justified the cutover migration with "the root checkout's legacy ledger
carries 2 genuinely open obligations (B-124, B-137) that the venue move would strand." That
number is an ARTIFACT. It came from reducing an OLD-semantics file with the NEW rule: C-HE-09
§4 strikes the `ACTIVATE` reset, but every legacy row was WRITTEN under the old rule, in which
`ACTIVATE` cleared prior-run state. Measured both ways on the real file (3204 lines, 18
`ACTIVATE` rows): the new rule reports `B-124 B-137` pending; the old rule — the one those
rows were actually written under — reports **nothing** pending.

So the migration was not rescuing two stranded gates. It was about to RESURRECT two rows that
their own run had already closed, import them into the shared venue, and put them in the
global skip-set until a human explicitly resolved each one — making the loop refuse items
nobody was ever blocked on. `_loop_pending_rows_with_ts` now reduces legacy files under
LEGACY semantics, and a witness pins both halves (a pre-ACTIVATE row is not resurrected; a
post-ACTIVATE row still migrates) alongside one confirming the SHARED venue keeps §4's
no-reset rule unchanged.

The migration is still correct to exist — a genuinely pending legacy row would otherwise be
stranded, and the C-HE-09 §2 venue move is what creates that possibility. But on THIS
workspace it imports nothing, and the earlier claims are corrected in place rather than left
standing: in this note above, and in `loop_status_migrate`'s own header comment, which had
frozen the wrong number into the code as its rationale. Two lessons worth keeping: a
semantics change makes every historical file a DIFFERENT format, and a measurement taken with
the new rule is not evidence about data written under the old one.

(xviii) **Rounds 15-17, and the terminal.** r15 caught two defects introduced by r14's own
fixes: the NUL-safe worktree list was printed BACK through newlines to feed a `read` loop
(re-creating the split `-z` had just removed), and `_loop_resolved_map` sat inside the
per-worktree loop, making the pass O(worktrees x ledger) inside SessionStart's 8 s bounded
slice. It also exposed that the test meant to pin the single-scan property counted textual
CALL SITES — green while the map was rebuilt per worktree. Replaced with a behavioural
witness that stubs the builder, migrates three worktrees, and asserts exactly one read.

r16 added claim owner-liveness (two concurrent migrations were each treating the other's
in-flight claim as an orphan), removed the guessable staging fallback that survived a failed
`mktemp`, and sorted orphans by basename. Its liveness check needed a second pass of its own:
`kill -0` fails with EPERM for another user's process, which means the process EXISTS —
reading that as dead would let a migration steal a live claim — so `_loop_pid_alive` falls
back to `ps -p`.

**r17 returned four findings and ZERO new ones.** Every one maps to a class already
registered with a reasoned disposition: the lane-scoping objection is B-196 (registered after
three flips precisely so it stops being re-litigated per round), the check-then-append
atomicity is B-195(a) (whose consequence is §4's SAFE direction), the open-fd-survives-rename
hole is B-195(b) (not fixable from this side), and the lossy lane encoding is B-194
(misclassification half fixed; collision half needs a mint-time or encoding decision). That
is the terminal condition — not reviewer silence, which proves nothing, but a round in which
the reviewer produced no information the register did not already hold.

Seventeen rounds. The shape of the arc is worth stating plainly: r1-r3 hardened the UNIT;
r4-r16 almost entirely hardened the cutover MIGRATION that r2 introduced to protect the venue
move; and r14 then showed that migration's founding measurement had been wrong all along
(xvii). The unit's own contracts — venue, structured column, ACTIVATE scoping, NOTIFY — were
settled by r3 and never regressed.

(xix) **THE CUTOVER MIGRATION WAS CUT BEFORE MERGE — read this before items (ii)–(xviii).**
Much of this note documents the hardening of a `loop_status_migrate` that **is not in the
landed unit**. It was removed wholesale at the operator''s decision, and the reasoning is the
most useful thing this arc produced.

*What happened.* r2 raised that the venue move would strand still-open deferrals. That was
grounded, measured (`B-124 B-137` apparently open), and a migration was built. At r14 the
measurement was shown to be an ARTIFACT (xvii): reducing an old-semantics file with the new
no-ACTIVATE-reset rule is not evidence about what it holds, and under the rule those rows were
written with, the legacy ledgers hold NOTHING. The live dry-run agreed: 0 files to import.

*What it cost.* ~318 lines of concurrency-sensitive machinery — atomic claim-by-rename, orphan
recovery, pid liveness, NUL enumeration, timestamp staleness, a five-value exit taxonomy — plus
44 tests, to migrate nothing on this workspace. It generated roughly 20 of the arc''s 23 review
rounds, because a large concurrency surface produces findings indefinitely and each fix seeded
the next round''s defect.

*The correction that should have happened at r14, and did not.* When a measurement that
justified building something is falsified, the next move is to re-scope, not to keep hardening.
Rounds 15–23 hardened machinery whose premise had already been withdrawn. The unit''s own
contracts (C-HE-09 §1–§6, C-HE-20 §1) were settled at r3 and never regressed through any of it.

*What landed instead.* The venue, the structured column, the ACTIVATE-scoping strike, `NOTIFY`,
the `[lane_id]` render and the pointer sweep — the actual unit. `B-195` and `B-197` are CLOSED
with the migration (every race they recorded was a property of it), and **B-198** records the
measurement plus the conditions under which a drain would have to be built for real, so a later
session neither re-derives it nor rebuilds the machinery on the same false premise.



---

### U-HE-30: Gate coalescing by `cause_signature`, 10 min window, pull-based delivery

**Scope.** `loop_pending_hil_summary()` / `loop_cap_list()` gain a second reduction key `cause_signature` (from the structured column): pending rows group by cause; a group is *delivered* (one batched prompt) only once `first_seen + window` (default 600 s, `HARNESS_HIL_COALESCE_WINDOW_S` in 300–900) has elapsed; delivery appends `| ts | COALESCE-DELIVERED | lane=<lane>;cause=<sig> | <generation-id> |`; rows already covered by a `COALESCE-DELIVERED` at/after their `first_seen` are treated as delivered so two SessionStart paths cannot both prompt.

**Spec linkage.** C-HE-10 §1–§4; C-HE-09 §3, §5 (kind).

**Files.** Modify `tools/hooks/loop_lib.sh` (`loop_pending_hil_summary`, new `loop_hil_groups`, `loop_hil_deliver`), `tools/hooks/test_loop_lib.sh`, `tools/hooks/session-start.sh`.

**Depends on.** U-HE-29.

- [ ] **Step 1: Failing test**
```bash
: > "$(loop_status_path)"; loop_status_ensure >/dev/null
loop_now() { echo "2026-08-18T00:00:00Z"; }
loop_log_structured DEFERRED-HIL L1 'merge-door-lease-acquire:transient-retry:lease_contended' 'B-1 — waiting'
loop_log_structured DEFERRED-HIL L2 'merge-door-lease-acquire:transient-retry:lease_contended' 'B-2 — waiting'
loop_log_structured DEFERRED-HIL L3 'reviewer:permanent-fail-exit:codex_login' 'B-3 — login'
export ARC_METRICS_QUEUE_DIR="$REPO/queue"; mkdir -p "$ARC_METRICS_QUEUE_DIR"
G=$(loop_hil_groups)   # "<sig>\t<n>\t<first_seen_epoch>\t<items>"
[ "$(printf '%s\n' "$G" | wc -l | tr -d ' ')" = "2" ] && ok "two cause groups" || bad "groups: $G"
[[ "$G" == *"lease_contended	2	"* ]] && ok "equal signatures within window → one group of 2" || bad "no 2-group: $G"
loop_now() { echo "2026-08-18T00:05:00Z"; }
[ -z "$(HARNESS_HIL_COALESCE_WINDOW_S=600 loop_hil_deliver)" ] && ok "inside window: nothing delivered yet" || bad "delivered early"
loop_now() { echo "2026-08-18T00:11:00Z"; }
OUT=$(HARNESS_HIL_COALESCE_WINDOW_S=600 loop_hil_deliver)
[[ "$OUT" == *"[L1] B-1"* && "$OUT" == *"[L2] B-2"* ]] && ok "one batched prompt per cause after window" || bad "deliver: $OUT"
[ "$(grep -c '| COALESCE-DELIVERED |' "$(loop_status_path)")" = "2" ] && ok "delivery rows appended" || bad "no delivery rows"
[ -z "$(HARNESS_HIL_COALESCE_WINDOW_S=600 loop_hil_deliver)" ] && ok "second SessionStart does not re-prompt" || bad "double delivery"
# atomic claim: two CONCURRENT deliverers → exactly one prompt (Codex round-1 P1)
: > "$(loop_status_path)"; loop_status_ensure >/dev/null; rm -rf "$ARC_METRICS_QUEUE_DIR/hil-deliveries"
loop_now() { echo "2026-08-18T01:00:00Z"; }; loop_log_structured DEFERRED-HIL L1 'x:y:z' 'B-7 — a'
loop_now() { echo "2026-08-18T01:11:00Z"; }
OUT_A=$(HARNESS_HIL_COALESCE_WINDOW_S=600 loop_hil_deliver & HARNESS_HIL_COALESCE_WINDOW_S=600 loop_hil_deliver & wait)
[ "$(printf '%s\n' "$OUT_A" | grep -c 'need you')" = "1" ] && ok "concurrent deliverers: exactly one prompt" || bad "double prompt under concurrency: $OUT_A"
# ordering by timestamp, not item id: a lexically-earlier item deferred LATER must not re-anchor the window
: > "$(loop_status_path)"; loop_status_ensure >/dev/null; rm -rf "$ARC_METRICS_QUEUE_DIR/hil-deliveries"
loop_now() { echo "2026-08-18T02:00:00Z"; }; loop_log_structured DEFERRED-HIL L1 'x:y:z' 'B-9 — first'
loop_now() { echo "2026-08-18T02:20:00Z"; }; loop_log_structured DEFERRED-HIL L1 'x:y:z' 'B-1 — later but lexically first'
[ "$(loop_hil_groups | wc -l | tr -d ' ')" = "2" ] && ok "groups keyed by arrival time (20 min apart → 2 groups)" || bad "timestamp ordering wrong: $(loop_hil_groups)"
loop_now() { echo "2026-08-18T00:30:00Z"; }; loop_log_structured DEFERRED-HIL L4 'merge-door-lease-acquire:transient-retry:lease_contended' 'B-4 — later'
[ "$(printf '%s\n' "$(loop_hil_groups)" | grep -c lease_contended)" = "2" ] && ok "same signature outside window → separate group" || bad "window merge wrong"
loop_now() { echo "2026-08-18T00:45:00Z"; }
[[ "$(HARNESS_HIL_COALESCE_WINDOW_S=600 loop_hil_deliver)" == *"B-4"* ]] && ok "later same-signature generation is delivered (not suppressed by the earlier delivery)" || bad "later generation suppressed"
```
- [ ] **Step 2: RED**; **Step 3: Implement**
```bash
# C-HE-10: pending HIL rows grouped by cause_signature; a group is (sig, first_seen) with members within
# HARNESS_HIL_COALESCE_WINDOW_S (default 600, band 300..900) of the first. Additive group-by, never
# arbitration. Output: sig \t n \t first_seen \t "[lane] detail; [lane] detail".
loop_hil_groups() {
  local p; p=$(loop_status_path); [ -f "$p" ] || return 0
  local w="${HARNESS_HIL_COALESCE_WINDOW_S:-600}"; [ "$w" -lt 300 ] && w=300; [ "$w" -gt 900 ] && w=900
  # Pass 1 (awk, POSIX): resolve last-write-wins per item; emit PENDING rows as ts \t tok \t sig \t lane \t detail.
  # Pass 2 (bash): ts -> epoch (portable, no gawk mktime). Pass 3: sort by EPOCH (not by item id -- Codex round-1
  # P2: `asorti` sorted the token, not the arrival time). Pass 4 (awk): sequential window grouping.
  awk -F'|' "$_LOOP_AWK_ROW"'
    { rowparse() }
    k == "DEFERRED-HIL" || k == "RESOLVED-HIL" {
      cause = $4; if (cause ~ /^[ \t]*lane=/) { sub(/^[^;]*;cause=/, "", cause); gsub(/[ \t]/, "", cause) } else cause = "-"
      if (cause == "-") cause = "-:" tok          # legacy rows reduce as their OWN singleton group (C-HE-10 §1; round-5 P2)
      ts = $2; gsub(/^[ \t]+|[ \t]+$/, "", ts)
      if (k == "DEFERRED-HIL") { state[tok] = "PENDING"; sig[tok] = cause; when[tok] = ts; lanes[tok] = lane; det[tok] = d } else state[tok] = "RESOLVED" }
    END { for (t in state) if (state[t] == "PENDING") printf "%s\t%s\t%s\t%s\t%s\n", when[t], t, sig[t], lanes[t], det[t] }
  ' "$p" 2>/dev/null \
  | while IFS=$'\t' read -r ts tok sig lane det; do printf '%s\t%s\t%s\t%s\t%s\n' "$(_loop_epoch "$ts")" "$tok" "$sig" "$lane" "$det"; done \
  | sort -n -k1,1 -t$'\t' \
  | awk -F'\t' -v w="$w" '
    { s = $3; e = $1 + 0
      if (!(s in gstart) || e - gstart[s] > w) { gid++; gstart[s] = e; gsig[gid] = s; gfirst[gid] = e; gn[gid] = 0; gitems[gid] = ""; cur[s] = gid }
      g = cur[s]; gn[g]++; gitems[g] = gitems[g] (gitems[g] == "" ? "" : "; ") "[" $4 "] " $5 }
    END { for (g = 1; g <= gid; g++) printf "%s\t%d\t%d\t%s\n", gsig[g], gn[g], gfirst[g], gitems[g] }
  ' | sed 's/\\|/|/g'
}
# Pull-based delivery (Codex C2-05) with an ATOMIC per-generation claim (Codex round-1 P1): a generation is
# (sig, first_seen_epoch); before prompting, the deliverer must win the exclusive create of
# QUEUE_DIR/hil-deliveries/<gen-id> (a coordination file: QUEUE_DIR-adjacent, C-HE-02 §2). Two concurrent
# SessionStart paths cannot both prompt, and a later same-signature generation is never suppressed by an
# earlier one's delivery (the claim is keyed by the EXACT generation, not by ">= first_seen").
loop_hil_deliver() {
  local p; p=$(loop_status_path); [ -f "$p" ] || return 0
  local w="${HARNESS_HIL_COALESCE_WINDOW_S:-600}" now; [ "$w" -lt 300 ] && w=300; [ "$w" -gt 900 ] && w=900   # same clamp as the grouper (round-5 P2)
  now=$(_loop_epoch "$(loop_now)")
  local claims="${ARC_METRICS_QUEUE_DIR:-$HOME/.gstack/projects/arhugula-v2/arc-metrics-queue}/hil-deliveries"; mkdir -p "$claims"
  loop_hil_groups | while IFS=$'\t' read -r sig n first items; do
    [ $(( now - first )) -ge "$w" ] || continue
    local gen; gen="gen-${first}-$(printf '%s' "$sig" | tr -c 'A-Za-z0-9_.-' '_')-${n}"
    ( set -o noclobber; printf '%s\n' "$(loop_now) ${HARNESS_LANE_ID:--}" > "$claims/$gen" ) 2>/dev/null || continue   # lost the claim: already delivered
    printf '[loop] ⏸ %s item(s) need you (%s): %s\n' "$n" "$sig" "$items"
    loop_log_structured COALESCE-DELIVERED "${HARNESS_LANE_ID:--}" "$sig" "$gen"
  done
}
_loop_epoch() { date -u -j -f %Y-%m-%dT%H:%M:%SZ "$1" +%s 2>/dev/null || date -u -d "$1" +%s; }
```
(`session-start.sh`: replace the direct `loop_pending_hil_summary` line with `loop_hil_deliver` output; keep the bounded summary as the fallback line when no group is due.) The pipeline is POSIX awk + bash `date` (both forms) — no gawk `mktime/asorti`; the tests run on macOS and ubuntu CI.
- [ ] **Step 4: GREEN**, register (covered by the `test_loop_lib.sh` row). Commit `feat(he-lanes): U-HE-30 HIL coalescing by cause_signature with pull-based delivery (C-HE-10)`.

---

*As built (PR #1430).* The unit landed with its scope intact — `loop_hil_groups` +
`loop_hil_deliver`, the `cause_signature` second reduction key, the window, the
`COALESCE-DELIVERED` row, the SessionStart wiring — but **five departures from the code
drafted above**, each mutation-probed and each a real defect in the draft:

(i) **The generation id is `(sig, first_seen)`, not `(sig, first_seen, n)`.** The draft's own
prose says the former while its code appends `-${n}`; with the member count in the key, one
more lane deferring into an already-delivered window at the boundary second mints a second
generation for the same window.

(ii) **Epoch conversion reuses `_LOOP_AWK_EPOCH`** (the pure-awk days-from-civil helper every
other reducer already shares) rather than the drafted `_loop_epoch` `date`-fork pair — one
authority, no per-row fork, identical on macOS and Linux.

(iii) **The `QUEUE_DIR/hil-deliveries/<gen-id>` exclusive-create claim is GONE.** Delivery
serialises on the same `.loop-status.lock` flock `loop_hil_ttl_resurface` takes. The claim had
two defects the lock does not: it was persisted *before* the prompt and the row (a crash
between them left a claim no later deliverer could pass — the very residual
`.harness/spec/store-audit-he-loop-lanes.md` had registered against this unit), and being a
FILENAME it forced a lossy sanitisation of `cause_signature` under which `gate_a:fail:b` and
`gate:a_fail:b` collapsed together.

(iv) **Delivery coverage is per-MEMBER.** Each `COALESCE-DELIVERED` row records
`<gen-id> <item>@<arrival> …`. This deviates from C-HE-10 §2's literal "at/after their
`first_seen`" rule, which permanently swallows an adjacent same-cause generation — **filed as a
Class 1 fork** at `.harness/class_1_fork_c_he_10_delivery_coverage_rule_swallows_adjacent_generation.md`;
the spec amendment is owed there, not here.

(v) **`first_seen` is the FIRST arrival** (re-anchored by a RESOLVED-HIL or a changed cause).
The session-start reconcile pass re-emits the same unresolved deferral every session, so a
latest-wins anchor postponed delivery forever for frequent sessions.

*Also owed and landed by this unit, outside its stated Files list:* `tools/roadmap-audit/test_*.sh`
is now globbed by `tools/codex-parity-check.sh`. `test_session_start.sh` existed but no gate ran
it, so this unit's SessionStart wiring broke one of its assertions with every gate still green.


### U-HE-31: `tools/hooks/lane-init.sh` — `HARNESS_LANE_ID`, `HARNESS_LANE_INDEX`, `gc.auto 0` once, RAM probe; compose port variables; `-p` recipes

**Scope.** Lane initialisation script sourced by `two-lane` / `roadmap-continue` at worktree start: mints `HARNESS_LANE_ID`, allocates `HARNESS_LANE_INDEX` by exclusive create of `QUEUE_DIR/lanes/<k>` (released at teardown by `safe-worktree-remove.sh`), sets `gc.auto 0` repo-wide once idempotently, probes RAM headroom before a lane-`k ≥ 2` stack on a machine below the floor (default 32 GB) → `NOTIFY` + `stack=absent`; `compose.yaml` ports become `${R420_PORT_GRAFANA:-3000}` etc.; the three `r420-self-hosted-stack-*` recipes take `-p arhugula-r420-self-hosted-local-lane<k>` and the port block `30000 + 100·k + {0,1,2,3}` for `k ≥ 1`, `k < 350`.

**Spec linkage.** C-HE-11 §1 (Docker `-p`, full collision set, port formula, `k` allocation), §2 (`gc.auto 0` once), §4 (no per-lane uv cache), §5 (RAM probe → NOTIFY, never a `merge-door-`/`reservation-` cause).

**Files.** Create `tools/hooks/lane-init.sh`, `tools/hooks/test_lane_init.sh`, `tools/test_compose_lanes.py`. Modify `justfile:469-480`, `deploy/self-hosted-local/compose.yaml:12,24-25,41-42`, `tools/hooks/safe-worktree-remove.sh` (release `QUEUE_DIR/lanes/<k>` on disposal), `.claude/skills/two-lane/SKILL.md` (source lane-init).

**Depends on.** U-HE-29 (NOTIFY).

- [ ] **Step 1: Failing tests** — `tools/hooks/test_lane_init.sh`:
```bash
export ARC_METRICS_QUEUE_DIR="$REPO/queue"; export HARNESS_LOOP_STATUS_PATH="$REPO/loop_status.md"
( cd "$REPO/wt" && source "$ROOT/tools/hooks/lane-init.sh" && env | grep -q '^HARNESS_LANE_ID=' ) && ok "lane id exported" || bad "no HARNESS_LANE_ID"
K1=$(cd "$REPO/wt" && source "$ROOT/tools/hooks/lane-init.sh" && echo "$HARNESS_LANE_INDEX")
K2=$(cd "$REPO/wt2" && source "$ROOT/tools/hooks/lane-init.sh" && echo "$HARNESS_LANE_INDEX")
[ "$K1" != "$K2" ] && [ -f "$REPO/queue/lanes/$K1" ] && ok "distinct lane index via exclusive create" || bad "index: $K1 $K2"
# gc.auto once, idempotent
( cd "$REPO/wt" && source "$ROOT/tools/hooks/lane-init.sh" >/dev/null; source "$ROOT/tools/hooks/lane-init.sh" >/dev/null )
[ "$(git -C "$REPO/wt" config --get gc.auto)" = "0" ] && [ "$(git -C "$REPO/wt" config --get-all gc.auto | wc -l | tr -d ' ')" = "1" ] && ok "gc.auto=0 written once" || bad "gc.auto writes: $(git -C "$REPO/wt" config --get-all gc.auto)"
# RAM probe: floor above machine → NOTIFY + stack=absent
OUT=$(cd "$REPO/wt" && HARNESS_LANE_INDEX_FORCE=2 HARNESS_RAM_FLOOR_GB=99999 bash -c "source $ROOT/tools/hooks/lane-init.sh; lane_stack_allowed && echo ALLOWED || echo ABSENT")
[ "$OUT" = "ABSENT" ] && grep -q '| NOTIFY | .*ram_floor' "$HARNESS_LOOP_STATUS_PATH" && ok "RAM shortfall → NOTIFY + stack absent" || bad "ram probe: $OUT"
grep -q 'lane_stack_allowed' "$ROOT/tools/hooks/lane-init.sh" && ! grep -q 'cause=merge-door\|cause=reservation' "$ROOT/tools/hooks/lane-init.sh" && ok "env cause never merge-door/reservation" || bad "wrong cause family"
```
`tools/test_compose_lanes.py`:
```python
import os, shutil, subprocess, pytest, yaml
from pathlib import Path
import lane_ports  # tiny helper module tools/lane_ports.py: def ports(k) -> dict; def project(k) -> str

def test_lane_port_formula():                # phase0, no daemon
    assert lane_ports.ports(0) == {"grafana": 3000, "tempo": 3200, "otel_grpc": 4317, "otel_http": 4318}
    assert lane_ports.ports(1) == {"grafana": 30100, "tempo": 30101, "otel_grpc": 30102, "otel_http": 30103}
    assert lane_ports.ports(2)["grafana"] == 30200 and lane_ports.ports(2)["tempo"] != 3200   # the Codex C1-05 collision
    with pytest.raises(ValueError):
        lane_ports.ports(350)
    assert lane_ports.project(3) == "arhugula-r420-self-hosted-local-lane3"

def test_compose_uses_port_variables():
    text = Path("deploy/self-hosted-local/compose.yaml").read_text()
    for var in ("R420_PORT_GRAFANA", "R420_PORT_TEMPO", "R420_PORT_OTEL_GRPC", "R420_PORT_OTEL_HTTP"):
        assert var in text

@pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0, reason="docker-daemon-absent")
def test_two_lanes_disjoint_names_and_ports():   # env-tagged
    import json as _json
    env = {**os.environ, "HARNESS_RAM_FLOOR_GB": "0"}      # the RAM guard is not under test here
    def up(k): subprocess.run(["just", "r420-self-hosted-stack-up"], env={**env, "HARNESS_LANE_INDEX": str(k)}, check=True, capture_output=True, text=True, timeout=600)
    def down(k): subprocess.run(["just", "r420-self-hosted-stack-down"], env={**env, "HARNESS_LANE_INDEX": str(k)}, capture_output=True, text=True, timeout=300)
    def ps(k):
        out = subprocess.run(["docker", "compose", "-p", lane_ports.project(k), "-f", "deploy/self-hosted-local/compose.yaml", "ps", "--format", "json"], capture_output=True, text=True, check=True).stdout
        return [_json.loads(line) for line in out.splitlines() if line.strip()]
    try:
        up(1); up(2)
        c1, c2 = ps(1), ps(2)
        assert len(c1) == 3 and len(c2) == 3, (c1, c2)                                        # three containers per stack
        assert {c["Name"] for c in c1}.isdisjoint({c["Name"] for c in c2})                     # disjoint container names
        assert all(c["State"] == "running" for c in c1 + c2), "no port bind conflict: both stacks running"
        p1, p2 = lane_ports.ports(1), lane_ports.ports(2)
        pub1 = " ".join(c.get("Publishers", []) and str(c["Publishers"]) or "" for c in c1); pub2 = " ".join(str(c.get("Publishers", "")) for c in c2)
        assert str(p1["grafana"]) in pub1 and str(p2["grafana"]) in pub2 and str(p2["grafana"]) not in pub1
        vols = subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"], capture_output=True, text=True).stdout
        assert f"{lane_ports.project(1)}_grafana-data" in vols and f"{lane_ports.project(2)}_grafana-data" in vols   # disjoint volume namespace
    finally:
        down(1); down(2)
```
- [ ] **Step 2: RED**; **Step 3: Implement.** `tools/lane_ports.py`:
```python
"""C-HE-11 §1 lane port/project formula. Lane 0 keeps today's ports; lane k>=1 uses 30000+100k+{0..3}."""
BASE = {"grafana": 3000, "tempo": 3200, "otel_grpc": 4317, "otel_http": 4318}
ORDER = ("grafana", "tempo", "otel_grpc", "otel_http")
def ports(k: int) -> dict[str, int]:
    if k < 0 or k >= 350:
        raise ValueError("HARNESS_LANE_INDEX must be 0..349")
    if k == 0:
        return dict(BASE)
    return {name: 30000 + 100 * k + i for i, name in enumerate(ORDER)}
def project(k: int) -> str:
    return "arhugula-r420-self-hosted-local" + (f"-lane{k}" if k else "")
```
`tools/hooks/lane-init.sh` (sourced):
```bash
#!/usr/bin/env bash
# Lane initialisation (C-HE-11). Source at worktree start: exports HARNESS_LANE_ID, HARNESS_LANE_INDEX,
# sets gc.auto 0 ONCE (repo-wide, idempotent), and defines lane_stack_allowed for the Docker stack.
_LI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$_LI_ROOT/tools/hooks/lib.sh"; source "$_LI_ROOT/tools/hooks/loop_lib.sh"
_LI_Q="${ARC_METRICS_QUEUE_DIR:-$HOME/.gstack/projects/arhugula-v2/arc-metrics-queue}"
[ -n "${HARNESS_LANE_ID:-}" ] || export HARNESS_LANE_ID="$(uv run --quiet python "$_LI_ROOT/tools/reservations.py" mint-lane-id --worktree "$PWD" 2>/dev/null || printf '%s-%s-%s' "$(hostname -s)" "$(basename "$PWD")" "$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')")"
# Lane index: exclusive create of QUEUE_DIR/lanes/<k>; released by safe-worktree-remove.sh.
if [ -z "${HARNESS_LANE_INDEX:-}" ]; then
  mkdir -p "$_LI_Q/lanes"; _k="${HARNESS_LANE_INDEX_FORCE:-0}"
  while :; do
    if ( set -o noclobber; printf '%s\n' "$HARNESS_LANE_ID $PWD" > "$_LI_Q/lanes/$_k" ) 2>/dev/null; then export HARNESS_LANE_INDEX="$_k"; break; fi
    _k=$((_k + 1)); [ "$_k" -lt 350 ] || { echo "lane-init: no free lane index < 350 — refusing to continue (an unset index would collide with lane 0)" >&2; return 1 2>/dev/null || exit 1; }
  done
fi
# git gc: repo-wide once (extensions.worktreeConfig is UNSET, so `git config` is repo-wide anyway).
[ "$(git config --get gc.auto 2>/dev/null)" = "0" ] || git config gc.auto 0
# RAM headroom probe (C-HE-11 §5): below the floor, lanes k>=2 skip the stack with a NOTIFY (environmental cause).
lane_stack_allowed() {
  local floor_gb="${HARNESS_RAM_FLOOR_GB:-32}" mem_gb k="${HARNESS_LANE_INDEX:-0}"
  [ "$k" -ge 2 ] || return 0
  if [ "$(uname)" = "Darwin" ]; then mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 )); else mem_gb=$(( $(grep MemTotal /proc/meminfo | awk '{print $2}') / 1048576 )); fi
  if [ "$mem_gb" -lt "$floor_gb" ]; then
    loop_log_structured NOTIFY "$HARNESS_LANE_ID" "lane-env:transient-retry:ram_floor" "lane $k: ${mem_gb}GB < floor ${floor_gb}GB; self-hosted stack skipped (stack=absent)"
    return 1
  fi
  return 0
}
```
`compose.yaml` ports → `"${R420_PORT_TEMPO:-3200}:3200"`, `"${R420_PORT_OTEL_GRPC:-4317}:4317"`, `"${R420_PORT_OTEL_HTTP:-4318}:4318"`, `"${R420_PORT_GRAFANA:-3000}:3000"`. Recipes:
```make
# Per-lane project + port block (C-HE-11 §1). Lane 0 = today's names/ports.
_lane-env:
    @uv run python -c "import os,lane_ports as l;k=int(os.environ.get('HARNESS_LANE_INDEX','0'));p=l.ports(k);print(f'-p {l.project(k)}');[print(f'export R420_PORT_{n.upper()}={v}') for n,v in p.items()]"
r420-self-hosted-stack-up:
    bash -c 'source tools/hooks/lane-init.sh; lane_stack_allowed' || { echo "self-hosted stack skipped: RAM floor (C-HE-11 §5; NOTIFY emitted)"; exit 0; }
    eval "$(just _lane-env | sed -n '2,$p')"; docker compose $(just _lane-env | head -1) -f deploy/self-hosted-local/compose.yaml up -d
r420-self-hosted-stack-down:
    docker compose $(just _lane-env | head -1) -f deploy/self-hosted-local/compose.yaml down
r420-self-hosted-stack-status:
    docker compose $(just _lane-env | head -1) -f deploy/self-hosted-local/compose.yaml ps
```
`safe-worktree-remove.sh`: after a successful removal, `rm -f "$QUEUE_DIR/lanes/<k>"` for the entry whose second field equals the removed path. `two-lane/SKILL.md`: *"At lane start: `source tools/hooks/lane-init.sh` (exports `HARNESS_LANE_ID`, `HARNESS_LANE_INDEX`; C-HE-11)."*
- [ ] **Step 4: GREEN**, register `Row("C-HE-11", "shell:tools/hooks/test_lane_init.sh", "phase0", "local + CI", False)`, `Row("C-HE-11", "pytest:tools/test_compose_lanes.py::test_lane_port_formula", "phase0", "local + CI", False)`, `Row("C-HE-11", "pytest:tools/test_compose_lanes.py::test_two_lanes_disjoint_names_and_ports", "env", "local", False, ("docker-daemon-absent",))`. Commit `feat(he-lanes): U-HE-31 lane-init (lane id/index, gc.auto once, RAM probe) + per-lane compose project/ports (C-HE-11)`.


*As built (PR #1434).* The unit landed with its scope intact — lane id, lane index, `gc.auto`,
the headroom probe, the per-lane Compose project and port block, and the teardown release —
after thirteen out-of-family rounds and a 3-lens merge gate. Where the built form differs from
the drafted bodies above, it is because a reviewer exhibited the interleaving that the drafted
form permits:

(i) **`HARNESS_LANE_INDEX_FORCE` is GONE.** The draft carried two index knobs with different
rules (`FORCE` skipped the reuse-by-path scan; a preset claimed nothing), and that asymmetry
WAS the defect: a preset issued after a normal init added a SECOND claim for one worktree, so
two shells in one lane could drive different Compose projects. The built script has ONE rule —
a worktree holds at most one claim, and an index is usable only when a claim positively names
this worktree — and one knob, `HARNESS_LANE_INDEX`, which asks for a specific index rather
than the next free one and is range-checked (canonical spelling, `< 350`) before it becomes a
filename. The Step 1/Step 3 code above still spells the removed knob; it is the drafted form,
superseded by this note.

(ii) **Every registry write is a PUBLICATION, never a bare redirect.** The drafted
`set -o noclobber; … > file` makes the NAME visible at `open()` and the payload visible after,
so a concurrent reader sees an owner-less record and acts on it — taking `k+1` for a lane that
already has `k`, or keeping its own identity when the marker's owner had already won. Both the
`.lane-id` marker and each `lanes/<k>` claim are written to an exclusively-created temp and
published with `ln` (or `mv -f` under a lock, for a repair). A zero-byte or blank-line record
is a corpse the protocol cannot produce, so it is reclaimed — under an exclusive `mkdir` lock,
with a re-check inside the lock and no unlocked fallback.

(iii) **The RAM probe reads BOTH clauses of §5.** Total physical memory (the machine class)
decides WHETHER to probe — that is what the 32 GB default is a floor on — and the probe itself
is of available memory, plus the Docker VM's own ceiling when a daemon answers, against
`HARNESS_LANE_STACK_NEED_GB` (an implementation estimate; §5 quantifies only the machine
floor). A small-but-idle machine therefore runs its lane instead of being refused.

(iv) **Teardown fences an index it cannot verify, rather than trusting or holding it.** The
plan says the release frees `QUEUE_DIR/lanes/<k>`; built, it first brings that lane's Compose
project down *with its volumes* — because `up` ADOPTS an existing project rather than failing,
so a recycled index would silently inherit a dead lane's containers and state. Only the total
absence of a `docker` binary counts as proof that nothing can exist; every other outcome is
unverified and writes `lanes/.orphaned-<k>`, which `lane-init` honours before the next lane
uses that index. The release lives inside `hook_safe_worktree_remove`, not the wrapper script,
because `loop_gc_worktrees` calls the function directly.

*Also owed and landed by this unit, outside its stated Files list:* the permission guard admits
the sourced `lane-init.sh` and the three stack recipes (without them the two-lane carrier
stalls at ask-then-deny in loop mode); `roadmap-continue/SKILL.md` no longer tells an agent to
mint and write `.lane-id` itself, since lane-init is now its one writer; the store-audit page
gains the orphan fence and the repair lock, and its extractor gains the `$var/<name>$interp`
idiom that made those literals invisible to it.

*Registered rather than absorbed:* **B-200** (per-lane telemetry endpoints — the stack is
isolated, its consumers still dial lane 0), **B-201** (a failed `gc.auto 0` write warns rather
than refusing the lane; its close-out is U-HE-32's bounded retry), and **B-202** (a claim↔
cleanup lock and available-memory-inside-the-VM, both needing a primitive outside this unit).

---

### U-HE-32: git ref-lock bounded retry helper

**Scope.** `hook_git_retry <git args…>` in `tools/hooks/lib.sh`: on `index.lock`/`.lock: File exists` failures retry with `{base 100 ms, factor 2, cap 5 s, max 8}` + full jitter; exhaustion fails the git op and emits a `NOTIFY` (never the merge-door budget). Used by `lane-init.sh`, `safe-worktree-remove.sh`, and documented for skills.

**Spec linkage.** C-HE-11 §3 (numbers; distinct from the lease's fail-fast).

**Files.** Modify `tools/hooks/lib.sh`, `tools/hooks/test_lib.sh`.

**Depends on.** U-HE-29.

- [ ] **Step 1: Test** (`test_lib.sh`): create `.git/index.lock` in a scratch repo, run `hook_git_retry add -A` in the background with the lock removed after 300 ms → succeeds after ≥ 2 attempts (count via `HOOK_GIT_RETRY_TRACE` file); with the lock held → exits non-zero after 8 attempts, a `NOTIFY` row with `cause=git-ref-lock:transient-retry:lock_contention` exists.
- [ ] **Step 2–3:** RED, then:
```bash
# C-HE-11 §3: local ref/index lock contention -> bounded backoff + full jitter (100 ms, x2, cap 5 s, 8 attempts).
# Exhaustion fails the op and emits a NOTIFY. This is LOCAL-git retry, unrelated to the merge-door lease.
hook_git_retry() {
  local attempt=0 delay_ms=100 out rc
  while :; do
    out=$(git "$@" 2>&1); rc=$?
    [ $rc -eq 0 ] && { [ -n "$out" ] && printf '%s\n' "$out"; return 0; }
    printf '%s' "$out" | grep -Eq '\.lock.*(File exists|exists)|Unable to create .*index\.lock|Another git process' || { printf '%s\n' "$out" >&2; return $rc; }
    attempt=$((attempt + 1)); [ -n "${HOOK_GIT_RETRY_TRACE:-}" ] && echo "$attempt" >> "$HOOK_GIT_RETRY_TRACE"
    if [ $attempt -ge 8 ]; then
      loop_log_structured NOTIFY "${HARNESS_LANE_ID:--}" "git-ref-lock:transient-retry:lock_contention" "git $* failed after 8 lock retries"
      printf '%s\n' "$out" >&2; return $rc
    fi
    sleep "$(awk -v d="$delay_ms" -v r="$RANDOM" 'BEGIN{printf "%.3f", (d * (r / 32767)) / 1000}')"
    delay_ms=$((delay_ms * 2)); [ $delay_ms -gt 5000 ] && delay_ms=5000
  done
}
```
- [ ] **Step 4:** GREEN; commit `feat(he-lanes): U-HE-32 bounded git ref-lock retry helper (C-HE-11 §3)`.

---

### U-HE-33: Emitting detections — `SPLIT_BRAIN_LEDGER`, `ORPHANED_RESERVATION`, `BASE_TOCTOU` + lane field + CI split-brain job

**Scope.** Three new codes in `codex_context_guard.py`, each emitted through the C-HE-24 record (with `lane_id`/`arc_id`) and projected to `Finding` for the CI surface: `SPLIT_BRAIN_LEDGER` (duplicate `arc_id` among `record_kind=arc` rows — CI backstop on every push to `main`), `ORPHANED_RESERVATION` (an `open` head whose PR is MERGED/CLOSED without a terminal transition, or a `blocked` lease older than its bound), `BASE_TOCTOU` (merge commit first parent ≠ verified base — the door emits it in U-HE-23; the guard re-checks the last N merges on `main`). Detection count ≥ 4/19.

**Spec linkage.** C-HE-12 §1–§3; §9 rows 1, 4, 6, 7; C-HE-24 §6 (lane attribution); C-HE-25 (arc rows only).

**Files.** Modify `tools/codex_context_guard.py` (new checks + `--lane-id` field in `_json_report`), `tools/test_codex_context_guard.py`, `.github/workflows/ci.yml` (job `split-brain`), `tools/lanes_verify.py`.

**Depends on.** U-HE-01, U-HE-11, U-HE-17, U-HE-22.

- [ ] **Step 1: Failing tests** (`tools/test_codex_context_guard.py`, using the file's fixture style):
```python
def test_split_brain_ledger_duplicate_arc_id(tmp_path):
    ledger = tmp_path / "arc-metrics.jsonl"
    ledger.write_text('{"arc_id":"pr-1","record_kind":"arc"}\n{"arc_id":"pr-1","record_kind":"arc"}\n')
    fs = ccg.check_split_brain(ledger, lane_id="lane-x")
    assert [f.code for f in fs] == ["SPLIT_BRAIN_LEDGER"] and fs[0].severity == "hard" and "lane-x" in fs[0].message

def test_split_brain_ignores_non_arc_rows_and_clean(tmp_path):
    ledger = tmp_path / "arc-metrics.jsonl"; ledger.write_text('{"arc_id":"pr-1","record_kind":"arc"}\n')
    assert ccg.check_split_brain(ledger, lane_id="l") == []

def test_base_toctou(tmp_path):
    fs = ccg.check_base_toctou([("m"*40, "b"*40, "c"*40)], lane_id="l")   # (merge_sha, first_parent, verified_base)
    assert [f.code for f in fs] == ["BASE_TOCTOU"] and fs[0].severity == "hard"
    assert ccg.check_base_toctou([("m"*40, "b"*40, "b"*40)], lane_id="l") == []

def test_orphaned_reservation(monkeypatch, tmp_path):
    monkeypatch.setattr(ccg, "_reservation_heads", lambda: [{"arc_id": "pr-9", "state": "open", "pr": 9}])
    monkeypatch.setattr(ccg, "_gh_pr_state", lambda pr: "MERGED")
    fs = ccg.check_orphaned_reservations(lane_id="l")
    assert [f.code for f in fs] == ["ORPHANED_RESERVATION"]

def test_json_report_carries_lane_id(...):   # existing _json_report fixture + assert "lane_id" in json.loads(report)
```
- [ ] **Step 2: RED**; **Step 3: Implement** in `codex_context_guard.py` (near the other checks; each builds a C-HE-24 row via `finding_record` and returns its projection so the emitted `Finding` is derived, never authored):
```python
def _detection(code: str, evidence: str, *, lane_id: str, arc_id: str, severity: str = "hard") -> Finding:
    """C-HE-12: a detection EMITS a C-HE-24 row (lane-attributed) and returns its projection for the CI surface."""
    import finding_record as fr
    core = fr.FindingCore(fr.make_finding_id(code, "nohead", arc_id, 0), arc_id, evidence, "C-HE-12", severity, f"terminal-{code.lower()}", "guard", code)
    row = fr.make_row(core, fr.Envelope("finding", fr.now_iso(), arc_id, lane_id, None, None, None, None, cause_attribution=code.lower()))
    try:
        fr.append_row(row)
    except Exception as exc:  # noqa: BLE001 -- the detection must still surface even if the record write fails
        print(f"guard: finding row not written ({exc})", file=sys.stderr)
    return Finding(severity, code, f"[{lane_id}] {evidence}")


def check_split_brain(ledger: Path, *, lane_id: str) -> list[Finding]:
    seen, dup = set(), []
    for line in ledger.read_text().splitlines() if ledger.exists() else []:
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("record_kind", "arc") != "arc":
            continue
        a = r.get("arc_id")
        if a in seen:
            dup.append(a)
        seen.add(a)
    return [_detection("SPLIT_BRAIN_LEDGER", f"duplicate arc_id in arc-metrics.jsonl: {a}", lane_id=lane_id, arc_id=a) for a in sorted(set(dup))]


def check_base_toctou(merges: list[tuple[str, str, str]], *, lane_id: str) -> list[Finding]:
    return [_detection("BASE_TOCTOU", f"merge {m[:12]} first parent {fp[:12]} != verified base {vb[:12]} -- race window hit; re-validate", lane_id=lane_id, arc_id=f"merge-{m[:12]}")
            for m, fp, vb in merges if fp != vb]


def check_orphaned_reservations(*, lane_id: str) -> list[Finding]:
    out = []
    for h in _reservation_heads():
        if h["state"] == "open" and h.get("pr") and _gh_pr_state(h["pr"]) in ("MERGED", "CLOSED"):
            out.append(_detection("ORPHANED_RESERVATION", f"{h['arc_id']}: open reservation but PR #{h['pr']} is {_gh_pr_state(h['pr'])}", lane_id=lane_id, arc_id=h["arc_id"], severity="warn"))
    lease = _blocked_lease_older_than_bound()
    if lease:
        out.append(_detection("ORPHANED_RESERVATION", f"blocked lease for pr #{lease['pr']} older than bound", lane_id=lane_id, arc_id=lease["reservation_id"], severity="warn"))
    return out
```
(`_reservation_heads()` iterates `reservations.current` over the store; `_gh_pr_state()` is a bounded `gh pr view`; `_blocked_lease_older_than_bound()` reads `merge_door.read_lease()`; each guarded so a missing store yields `[]`.)
```python
def _reservation_heads() -> list[dict]:
    try:
        import reservations as rs
        root = rs.reservations_root()
        if not root.is_dir():
            return []
        heads = []
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                cur = rs.current(d.name)
                if cur:
                    heads.append(cur[1])
        return heads
    except Exception:  # noqa: BLE001 -- a missing/unreadable store is "nothing to detect", never a crash of the guard
        return []


def _gh_pr_state(pr: int) -> str | None:
    p = subprocess.run(["gh", "pr", "view", str(pr), "--json", "state", "--jq", ".state"], capture_output=True, text=True, timeout=30)
    return p.stdout.strip() or None if p.returncode == 0 else None


def _blocked_lease_older_than_bound() -> dict | None:
    try:
        import merge_door as md
        lease = md.read_lease()
    except Exception:  # noqa: BLE001
        return None
    if not lease or lease.get("state") != "blocked":
        return None
    blocked_at = lease.get("blocked_at")   # ISO from the .blocked sidecar, merged into the view by md.read_lease()
    if blocked_at is None:
        return None
    age_s = (datetime.now(UTC) - datetime.strptime(blocked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)).total_seconds()
    return lease if age_s > md.POST_MERGE_CI_BOUND_S + md.REFRESH_BOUND_S else None
```
 Wire: `check` mode on `main` calls `check_split_brain(REPO/".harness/arc-metrics.jsonl", lane_id=os.environ.get("HARNESS_LANE_ID","ci"))` and `check_base_toctou` over the last 10 **first-parent** commits on `main` (`git log --first-parent -10 --format=%H %P` — the door squash-merges, so landings have ONE parent and `--merges` would inspect none of them; Codex round-3 P2): each landing SHA is joined to the reservation that recorded it as `merge_sha` (U-HE-23) and its parent compared with that reservation's verified `base_sha`; landings without a reservation are reported as `unattributed`, never silently skipped; `_json_report` adds `"lane_id"`. CI job (after `codex-context-guard`):
```yaml
  split-brain:
    name: split-brain ledger backstop — blocking
    runs-on: ubuntu-latest
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
      - name: one arc row per arc_id
        run: |
          dups=$(jq -r 'select(.record_kind=="arc" or (.record_kind|not)) | .arc_id' .harness/arc-metrics.jsonl | sort | uniq -d)
          [ -z "$dups" ] || { echo "SPLIT_BRAIN_LEDGER: $dups"; exit 1; }
```
- [ ] **Step 4: GREEN**; injected-duplicate fixture makes the CI job red locally (`act` not required — run the `jq` line against a fixture). Register `Row("C-HE-12", "pytest:tools/test_codex_context_guard.py::test_split_brain_ledger_duplicate_arc_id", "phase0", "local + CI", False)` and `::test_base_toctou`. Add the new job name to `main_protection.blocking_contexts` expectations (it ends with "— blocking", so `apply`/`verify` pick it up automatically — re-run `just main-protection-verify` after this lands and re-apply if the context list changed: that is the plan's own two-step; record it in the evidence log).
- [ ] **Step 5: Commit** — `git add tools/codex_context_guard.py tools/test_codex_context_guard.py .github/workflows/ci.yml tools/lanes_verify.py && git commit -m "feat(he-lanes): U-HE-33 emitting detections SPLIT_BRAIN_LEDGER / ORPHANED_RESERVATION / BASE_TOCTOU + CI backstop (C-HE-12)"`.

---
# S5 — Phase timing as explicit spans

### U-HE-34: Phase spans — durable accretion, `result_capture` split, N6 formula, no-delta static witness

**Scope.** Every phase `queue / execute / capture / absorb / edit / verify` recorded as an explicit `{start, end}` (accreting on the reservation during the open window via `record_phase` — U-HE-17 — and folded into the arc row at drain — U-HE-19); `result_capture` records process-exit and log-write-completion separately; the emitters (`roadmap-continue`, `ship-pr`, `tools/hooks/*`) call `reservations.py phase`; N6 = COUNT(DISTINCT `finding_id` with last disposition `accepted`) ÷ Σ(`phases.verify` + `phases.edit`) hours with `REVIEWER_UNAVAILABLE` rounds excluded from the denominator; a static test that no metrics reader derives a duration from the gap between two records.

**Spec linkage.** C-HE-27 §1 (phases; result_capture split), §2 (never inter-record delta), §3 (durable spans on the reservation; fold at drain), §4 (N6 definition + `verify_unavailable_s` exclusion); C-HE-25 (`phases` on the row); §11 #5 (plan S5 decides audit-worthiness of a `result_capture` divergence — **decision: recorded, not audit-worthy in v1**; both timestamps land on the row, no finding is emitted on divergence; a forward-register row carries the question).

**Files.** Modify `tools/arc_metrics.py` (`n6(rows, gate_rows)`, `phase_spans(row)`), `tools/test_arc_metrics.py`, `.claude/skills/roadmap-continue/SKILL.md`, `.claude/skills/ship-pr/SKILL.md`, `tools/hooks/stop-gate.sh` (or the hook that observes process exit vs log write — the executor greps `REVIEW-.*log` writers in `tools/hooks/` and instruments the two timestamps at the site the file already logs).

**Depends on.** U-HE-19, U-HE-01.

- [ ] **Step 1: Failing tests**
```python
def test_phase_spans_no_deltas():
    """Static witness: no reader computes end_of_row_n - start_of_row_{n-1} for a duration."""
    src = (REPO / "tools" / "arc_metrics.py").read_text()
    assert "phases[" not in src or "prev" not in src.split("def n6")[1].split("def ")[0]
    for reader in ("tools/arc_metrics.py", "tools/shadow_trial.py", "tools/lanes_pilot_report.py"):
        p = REPO / reader
        if p.exists():
            assert not re.search(r"rows\[\s*\w+\s*-\s*1\s*\]\[['\"](captured_at|merged_at|ts)['\"]\]", p.read_text()), reader


def test_out_of_order_rows_still_yield_correct_spans():
    row = {"phases": {"verify": {"end": "2026-08-18T01:00:00Z", "start": "2026-08-18T00:30:00Z"}, "edit": {"start": "2026-08-18T00:00:00Z", "end": "2026-08-18T00:20:00Z"}}}
    s = am.phase_spans(row)
    assert s["verify"] == 1800.0 and s["edit"] == 1200.0


def test_n6_formula():
    """accepted-count ÷ (verify+edit) hours; unavailable-terminated verify spans excluded from the denominator."""
    rows = [{"arc_id": "a", "phases": {"verify": {"start": "2026-08-18T00:00:00Z", "end": "2026-08-18T01:00:00Z"}, "edit": {"start": "2026-08-18T01:00:00Z", "end": "2026-08-18T02:00:00Z"}}, "round_outcomes": {"1": {"terminal": "APPROVE"}}},
            {"arc_id": "b", "phases": {"verify": {"start": "2026-08-18T00:00:00Z", "end": "2026-08-18T02:00:00Z"}}, "round_outcomes": {"1": {"terminal": "REVIEWER_UNAVAILABLE"}}}]
    gate = [{"finding_id": "f1", "arc_id": "a", "disposition": "accepted", "ts": "t2", "record_kind": "finding_adjudication"},
            {"finding_id": "f1", "arc_id": "a", "disposition": None, "ts": "t1", "record_kind": "finding"},
            {"finding_id": "f2", "arc_id": "a", "disposition": "rejected", "ts": "t1", "record_kind": "finding_adjudication"}]
    n6, hours, excluded_s = am.n6(rows, gate)
    assert n6 == pytest.approx(0.5) and hours == 2.0 and excluded_s == 7200.0
```
- [ ] **Step 2: RED**; **Step 3: Implement** in `arc_metrics.py`:
```python
def phase_spans(row: dict) -> dict[str, float]:
    """Per-phase seconds from the row's OWN {start,end} pairs -- never from a neighbouring row (C-HE-27 §2)."""
    out = {}
    for name, span in (row.get("phases") or {}).items():
        if span.get("start") and span.get("end"):
            out[name] = (parse_iso(span["end"]) - parse_iso(span["start"])).total_seconds()
    return out


def n6(rows: list[dict], gate_rows: list[dict]) -> tuple[float | None, float, float]:
    """C-HE-27 §4: problems prevented per hour = COUNT(DISTINCT finding_id last-disposed accepted)
    ÷ Σ(verify + edit) hours. verify spans of rounds that terminated REVIEWER_UNAVAILABLE are excluded
    (bucketed as verify_unavailable_s) so reviewer downtime cannot deflate N6."""
    import finding_record as fr
    last = fr.reduce_last_by_finding_id(gate_rows)
    accepted = {fid for fid, r in last.items() if r.get("disposition") == "accepted"}
    denom_s = 0.0
    excluded_s = 0.0
    for r in rows:
        spans = phase_spans(r)
        unavailable = any(o.get("terminal") == "REVIEWER_UNAVAILABLE" for o in (r.get("round_outcomes") or {}).values())
        if unavailable:
            excluded_s += spans.get("verify", 0.0)
        else:
            denom_s += spans.get("verify", 0.0)
        denom_s += spans.get("edit", 0.0)
    hours = denom_s / 3600.0
    return (len(accepted) / hours if hours else None), hours, excluded_s
```
Emitters: `roadmap-continue/SKILL.md` — after arc open: `uv run python tools/reservations.py phase --arc-id "$HARNESS_ARC_ID" --phase execute --edge start`; before ship-pr: `--phase execute --edge end`; `ship-pr/SKILL.md` — around the review gate: `--phase verify --edge start|end` (and on `REVIEWER_UNAVAILABLE`: `--phase verify_unavailable --edge start|end`), around fix rounds: `--phase edit --edge start|end`, at capture: `--phase capture --edge start|end`; the review-log writer hook records `result_capture_process_exit` at process exit and `result_capture_log_write` when the log file's size stops changing (bounded 130 s), both as phase edges. `summary` prints N6 with the excluded seconds.
- [ ] **Step 4: GREEN**, register `Row("C-HE-27/28", "pytest:tools/test_arc_metrics.py::test_phase_spans_no_deltas", "measurement", "local + CI", False)` and `Row("C-HE-27", "pytest:tools/test_arc_metrics.py::test_n6_formula", "measurement", "local + CI", False)`. Commit `feat(he-lanes): U-HE-34 durable phase spans + N6 (C-HE-27)`.

> **As-built rev (2026-08-25, U-HE-34 landing; marker
> `implementation-plan-he-loop-lanes-v1-u-he-34-as-built-rev-cleared-2026-08-25.md`).**
> Execution-time corrections (enumerated (i)-(x) below; later review rounds appended items), stated in place so the drafted bodies above are
> superseded rather than silently contradicted. **(i) Step-1 `test_n6_formula` fixture
> order.** The drafted gate fixture lists f1's adjudication BEFORE its finding row — a
> shape the emitter itself refuses (`test_retry_after_adjudication_is_rejected`) and one
> the C-HE-24 §5 file-order reducer resolves to `disposition=None`, so the drafted test
> fails against the drafted implementation. As built, the fixture uses the legal append
> order (finding, then adjudication); the formula and expected values are unchanged.
> **(ii) Step-1 static witness strengthened.** The drafted first assertion
> (`"phases[" not in src or "prev" not in <n6 body>`) is vacuously green while no reader
> indexes `phases` at all. As built, the witness inspects n6's body unconditionally
> (`\bprev\b`, word-bounded so the docstring's "prevented" cannot trip it) —
> mutation-probed both ways at landing. **(iii) `result_capture` observation site.** The
> Files-list primary (`tools/hooks/stop-gate.sh` / a `REVIEW-*.log` writer hook) does not
> exist — no hook in `tools/hooks/` writes round logs (they are session-tee'd files, per
> the ledger's `round_log_source` history). *(Superseded at item (ix): the interim
> `review-log-settle` watch was removed — a synchronous tee cannot observe the
> wrapper-internal divergence; the split's recorder is registered on `B-218`.)* **(iv) Guard carrier verb `phase` (outside the Files list,
> execution-correction-H_E-tooling).** The emitters this unit wires call
> `reservations.py phase`; the U-HE-25 allowlist did not carry that verb, so every
> emitter would strand headless at ask→deny — the wired-but-unreachable class. As built
> after codex r1 (P1: argparse is last-value-wins and `record_phase` has no holder
> check, so a duplicated `--arc-id` riding a prefix allow would write onto another
> lane's reservation), the verb gets a dedicated POSITIONAL exact-shape branch
> (`_phase_exact_shape`: positional exact shape, canonical flag order, dash-led values
> and non-`start|end` edges reject; 13 tokens as-built after the r3 `--lane-id`) rather
> than a spot in the generic alternation, with an allow witness + a hardening deny set
> in `test_permission_guard.sh` (the suite is the authority on its count -- later
> rounds extended it). **(v) Emitter carve
> (codex r1).** The drafted §6339 emitter sketch nests edit inside a loop-spanning
> verify pair, double-counting every fix interval in the N6 denominator, and omits
> queue/absorb. As built: verify = the round-1 window, absorb = the classification
> window, edit = the fix window (disjoint by construction; the named bound —
> interleaved re-review rounds land inside edit and the final confirming round is
> uncovered — is stated in ship-pr SKILL.md), and roadmap-continue records queue
> start/end around grounding. The sketch's capture pair is NOT wired: the queue step
> runs post-`merged` and accretion refuses terminal states (C-HE-03 §3) — the
> C-HE-27 §1 conflict is registered as `B-218` rather than wired to fail. **(vi) n6
> window + downtime buckets (codex r1).** The drafted body counts accepted findings
> from the ENTIRE gate log against only the measured arcs' hours; as built the
> numerator is filtered to the window's `arc_id`s (C-HE-27 §4 "across the window's
> arcs"), and the explicit `verify_unavailable` span is bucketed into excluded seconds
> alongside unavailable-terminated verify spans, so failover-masked primary downtime
> is not counted as review work. **(vii) Queue declarations (codex r1).** ship-pr's
> queue step now passes `--arc-id` explicitly (a `pr-<N>` default breaks the ledger ↔
> reservation ↔ gate-log join) and `--levers` as separate arguments (a comma-joined
> token forges a synthetic single lever in exact-set cohorts); the two rows drained on
> this arc were corrected and refolded via `_refold_local_row`. **(viii) Holder fence +
> reachability (codex r2/r3).** `record_phase` gains the holder fence (a named lane
> must BE the head's holder — authority at the store's write funnel, the guard
> validates only form; `--lane-id` is CLI-mandatory and trails the canonical shape);
> guard values are positively charset-bounded (a lowercase `${victim:=…}` expansion
> would ride a dash-check); the round-log tee moves inside the allowlisted
> `review-with-failover-logged` recipe (the guard rejects pipes pre-allowlist) with
> logs under gitignored `.harness/tmp/`; the settle watch requires four equal samples
> (15 s) and n6 no longer double-counts nested downtime under a round-1-unavailable
> exclusion; coverage rows are §-granular (no whole-C-HE-27 green claim while
> `capture` is unrecordable — B-218, whose scope now also carries the per-round-record
> and wrapper-internal-emitter reconciliation). **(ix) result_capture subtraction +
> containment (codex r4).** The §1 result_capture split is session-unrecordable — the
> logged recipe's synchronous tee flushes before it returns, so the r2 settle recipe
> measured nothing and was REMOVED (recorder folded into B-218); the logged recipe is
> now the CANONICAL review invocation (its tee is the only headless round-log
> producer) and refuses symlinked / outside-worktree log paths (containment — it
> writes through an auto-allowed path); the `verify_unavailable` span is defined as
> the observable outage window (detection → resume; the phase CLI stamps now()); the
> no-deltas row drops its draft C-HE-28 half (the witness exercises no cohort
> reporting). **(x) Publisher-owned containment (codex r7).** The recipe's bash
> containment checks are DELETED — pathname-based check-then-act cannot close the
> parent-swap window — and all filesystem safety moves to one enforcer,
> `tools/round_log_publish.py`: an `O_NOFOLLOW` dir-fd walk from the repo root
> (every component refuses a symlink at its own openat, atomically) with the
> destination policy pinned to relative paths under `.harness/tmp/`, so an
> auto-allowed invocation can never overwrite a tracked file or ledger; the guard's
> grammar mirrors the pin as the form half, with allow + hardening witnesses, and
> the publisher carries its own hermetic suite (`tools/test_round_log_publish.py`,
> CI-wired in codex-parity-check.sh); on exit 2 the workflow closes the verify pair
> before opening verify_unavailable, and merge-gate-emit invocations pass
> `--arc-id` so the gate rows join the reservation ids N6 keys on. Refined at r8:
> the publisher writes a fresh `O_EXCL` temp inode and installs it by rename — a
> pre-planted HARD link (which `O_NOFOLLOW` cannot stop) keeps its inode's bytes,
> the entry alone is replaced, and concurrent publishers cannot interleave one
> inode; a publish failure is its own recipe terminal (exit 4) for EVERY reviewer
> outcome, since exits 1/2/3 would let a caller treat a round with a missing
> canonical log as valid. Refined at r9: the install is link(2), not rename — EEXIST
> is an atomic refusal, so round logs are write-once evidence (a replay cannot
> silently discard a transcript the gate log still counts) and ANY pre-planted
> entry is left untouched; the stale settle-timeout prose was deleted and the
> review invocation's own bound named (1260 s shared deadline + bounded failover —
> background mode or a tool timeout above it).

---

# S6 — Reviewer-concurrency probe → coalescing live → pilot gate + pilots + O1 + O3 / `arc_disjoint_check` + cohort report

### U-HE-35: `tools/reviewer_concurrency_probe.py`

**Scope.** Live probe: ≥ 5 repetitions at each of {1, 2, 4} concurrent `codex-review` / `gemini-review` invocations against one fixed diff; records verdict validity (C-HE-15) and per-call wall-clock as C-HE-24 rows (`producer=reviewer_concurrency_probe`); GREEN iff median wall-clock at N ≤ 2 × the N=1 median AND zero validity failures — either violation ⇒ RED, throttling assumed present, pilots do not start.

**Spec linkage.** C-HE-22 §1–§2, Verification (≥ 5 reps; pass rule); C-HE-13 §2 (order: probe → coalescing → pilots); §11 #7.

**Files.** Create `tools/reviewer_concurrency_probe.py`, `tools/test_reviewer_concurrency_probe.py` (pass-rule unit on synthetic samples). Modify `justfile` (`reviewer-concurrency-probe`), `tools/lanes_verify.py`, `tools/codex-parity-check.sh`.

**Depends on.** U-HE-04, U-HE-06, U-HE-01.

- [ ] **Step 1: Test** (`decide(samples: dict[int, list[tuple[float, bool]]]) -> tuple[bool, str]`): `{1: 5×(60, ok), 2: 5×(100, ok), 4: 5×(110, ok)}` → GREEN; `{…, 4: 5×(130, ok)}` (130 > 2×60) → RED "wall-clock"; any `(x, False)` → RED "validity"; fewer than 5 reps at any N → RED "insufficient".
- [ ] **Step 2–3:** RED, then implement `decide()` (median via `statistics.median`) and `run(diff_ref, reps=5, ns=(1,2,4), channel="codex")` that spawns N concurrent `uv run python tools/codex_review.py --base <ref>` (or `agy_review.py`) subprocesses per rep, times each, parses exit code + stderr terminal, appends rows (`finding_type="probe-sample"`, `observed_evidence=json{wall_s, terminal, n}`), prints the table + verdict, exits 0/1. Recipe `reviewer-concurrency-probe channel='codex' reps='5'`.

```python
#!/usr/bin/env python3
"""C-HE-22 reviewer-concurrency probe: >=5 reps at N in {1,2,4} concurrent reviewer invocations on ONE fixed
diff. GREEN iff median wall-clock at N <= 2x the N=1 median AND zero validity failures; either violation => RED
(throttling assumed present; pilots do not start). Samples are C-HE-24 rows (producer=reviewer_concurrency_probe)."""
from __future__ import annotations
import argparse, json, statistics, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import finding_record as fr
import review_wrapper_common as rw

REPO = Path(__file__).resolve().parent.parent
PRODUCER = "reviewer_concurrency_probe"
CMD = {"codex": ["uv", "run", "python", "tools/codex_review.py", "--base"], "gemini": ["uv", "run", "python", "tools/agy_review.py", "--base"]}


def decide(samples: dict[int, list[tuple[float, bool]]], *, min_reps: int = 5) -> tuple[bool, str]:
    for n, s in samples.items():
        if len(s) < min_reps:
            return False, f"insufficient reps at N={n} ({len(s)} < {min_reps})"
    if any(not ok for s in samples.values() for _, ok in s):
        return False, "validity failure observed (REVIEWER_UNAVAILABLE / unparsed verdict)"
    base = statistics.median(w for w, _ in samples[1])
    for n, s in samples.items():
        med = statistics.median(w for w, _ in s)
        if med > 2 * base:
            return False, f"wall-clock: median at N={n} is {med:.0f}s > 2x N=1 median {base:.0f}s"
    return True, "GREEN: no throttling signal at N<=4"


def _one(channel: str, base: str) -> tuple[float, bool]:
    t0 = time.monotonic()
    p = subprocess.run([*CMD[channel], base], cwd=REPO, capture_output=True, text=True, timeout=rw.TOTAL_BUDGET_S + 60)
    return time.monotonic() - t0, p.returncode in (0, 1)          # 0/1 = a parsed verdict; 2 = REVIEWER_UNAVAILABLE


def run(base: str, *, channel: str = "codex", reps: int = 5, ns=(1, 2, 4)) -> int:
    arc_id, lane_id = rw.env_arc_and_lane()
    samples: dict[int, list[tuple[float, bool]]] = {}
    for n in ns:
        for rep in range(reps):
            with ThreadPoolExecutor(max_workers=n) as ex:
                results = list(ex.map(lambda _: _one(channel, base), range(n)))
            for wall, ok in results:
                samples.setdefault(n, []).append((wall, ok))
                core = fr.FindingCore(fr.make_finding_id(PRODUCER, "probe", f"{channel}-n{n}-rep{rep}", len(samples[n])), f"{channel}@N={n}",
                                      json.dumps({"wall_s": round(wall, 1), "valid": ok, "n": n, "rep": rep}), "C-HE-22", "info", "probe-sample", "measured", PRODUCER)
                fr.append_row(fr.make_row(core, fr.Envelope("finding", fr.now_iso(), arc_id, lane_id, None, None, None, rep)))
    for n in ns:
        med = statistics.median(w for w, _ in samples[n]); print(f"N={n}: median {med:.0f}s over {len(samples[n])} calls, valid={all(ok for _, ok in samples[n])}")
    ok, why = decide(samples); print(("GREEN " if ok else "RED ") + why)
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("--base", default="main"); p.add_argument("--channel", choices=("codex", "gemini"), default="codex"); p.add_argument("--reps", type=int, default=5)
    a = p.parse_args(argv); return run(a.base, channel=a.channel, reps=a.reps)


if __name__ == "__main__":
    raise SystemExit(main())
```
Recipe: `reviewer-concurrency-probe channel='codex' reps='5':\n    uv run python tools/reviewer_concurrency_probe.py --channel {{channel}} --reps {{reps}}`.

- [ ] **Step 4:** Register `Row("C-HE-22", "live:tools/reviewer_concurrency_probe.py (provider-login-gated; result row required before pilots)", "phase1", "operator/loop, live", False, ("provider-login-absent",))` + the unit test row. **Run it** (authorized per `[[feedback-run-credential-gated-live-e2e-authorized]]`; both channels are subscription-auth, $0 metered) and paste the verdict lines into the evidence log. Commit `feat(he-lanes): U-HE-35 reviewer-concurrency probe (C-HE-22)`.

---

### U-HE-36: `tools/arc_disjoint_check.py` + selection-time refusal

**Scope.** Prospective merge-tree check: before a lane opens an arc, compute `git merge-tree --write-tree` of the candidate head against every other lane's current head (from the `open` reservations' `branch`) and refuse selection on a non-empty conflict set; the O3 base-rate recipe (`--historical`) runs merge-tree over the 172 historical colliding pairs and reports the textual-conflict rate against the 38.7 % bound with the semantic rate reported as unmeasured.

**Spec linkage.** C-HE-13 §4 (O3 + prospective; U-WT-07 named at `two-lane/SKILL.md:19`), §5 (scope hint, not gate; actual-write enforcement); §9 rows 10, 18.

**Files.** Create `tools/arc_disjoint_check.py`, `tools/test_arc_disjoint_check.py`. Modify `.claude/skills/roadmap-continue/SKILL.md` (call before `reserve`), `.claude/skills/two-lane/SKILL.md:17-19` (scope = hint; check = gate), `tools/lanes_verify.py`, `tools/codex-parity-check.sh`.

**Depends on.** U-HE-17, U-HE-21.

- [ ] **Step 1: Test**
```python
def test_conflict_set_textual_vs_disjoint(tmp_path):
    repo = _init_repo(tmp_path)                    # helper: git init, base commit with a.txt/b.txt
    _branch_edit(repo, "lane-a", "a.txt", "A")     # branch from base, edit a.txt
    _branch_edit(repo, "lane-b", "a.txt", "B")     # conflicting
    _branch_edit(repo, "lane-c", "b.txt", "C")     # disjoint
    assert adc.conflicts(repo, "lane-a", ["lane-b"]) != []
    assert adc.conflicts(repo, "lane-a", ["lane-c"]) == []
```
- [ ] **Step 2–3:** RED; implement `conflicts(repo, candidate_ref, other_refs) -> list[str]` (`git merge-tree --write-tree --name-only <other> <candidate>`; non-zero exit = conflict; collect the conflicted paths from stdout), `open_lane_heads() -> list[str]` (branches of every `open` reservation except this lane's), CLI `check --candidate <ref>` (exit 1 + list on conflict) and `--historical` (reads the pair list from `.harness/plan/o3-colliding-pairs.txt`, prints rate vs 38.7 %, prints `semantic-conflict rate: unmeasured`).

```python
#!/usr/bin/env python3
"""U-WT-07 / C-HE-13 §4-5: prospective merge-tree check. Declared scope is a HINT; the gate is actual-write:
before a lane opens an arc, the candidate head is merge-tree'd against every other open lane's head."""
from __future__ import annotations
import argparse, os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
O3_PAIRS = REPO / ".harness" / "plan" / "o3-colliding-pairs.txt"    # "<shaA> <shaB>" per line (172 historical pairs)
UPPER_BOUND = 0.387


def conflicts(repo: Path, candidate_ref: str, other_refs: list[str]) -> list[str]:
    out = []
    for other in other_refs:
        p = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", "--name-only", other, candidate_ref], capture_output=True, text=True)
        if p.returncode == 1:                                    # 1 = conflicts (0 = clean, >1 = error)
            out += [f"{other}: {ln}" for ln in p.stdout.splitlines()[1:] if ln.strip()]
        elif p.returncode > 1:
            raise SystemExit(f"merge-tree failed for {other}: {p.stderr.strip()}")
    return out


def open_lane_heads() -> list[str]:
    import reservations as rs
    me = os.environ.get("HARNESS_LANE_ID")
    root = rs.reservations_root()
    if not root.is_dir():
        return []
    heads = []
    for d in root.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            cur = rs.current(d.name)
            if cur and cur[1]["state"] == "open" and cur[1]["lane_id"] != me and cur[1].get("branch"):
                heads.append(f"origin/{cur[1]['branch']}")
    return heads


def historical(repo: Path) -> int:
    pairs = [ln.split() for ln in O3_PAIRS.read_text().splitlines() if ln.strip()]
    hits = sum(1 for a, b in pairs if conflicts(repo, b, [a]))
    rate = hits / len(pairs) if pairs else 0.0
    print(f"O3: textual-conflict rate {hits}/{len(pairs)} = {rate:.3f} (upper bound {UPPER_BOUND}); semantic-conflict rate: unmeasured")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("cmd", choices=("check", "historical")); p.add_argument("--candidate", default="HEAD")
    a = p.parse_args(argv)
    if a.cmd == "historical":
        return historical(REPO)
    subprocess.run(["git", "-C", str(REPO), "fetch", "-q", "origin"], check=False, timeout=60)
    cs = conflicts(REPO, a.candidate, open_lane_heads())
    for c in cs:
        print(f"CONFLICT {c}")
    return 1 if cs else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4:** Carriers: `roadmap-continue` — before `reservations.py reserve`: `uv run python tools/arc_disjoint_check.py check --candidate HEAD || { echo "textual conflict with an open lane — pick the next unit"; ...; }`; `two-lane/SKILL.md:17-19` — *"Declared `scope.files` is a scheduling HINT (the forward register carries no such keys); the gate is `arc_disjoint_check.py` at selection + `BASE_TOCTOU` at landing (C-HE-13 §5)."* Register `Row("C-HE-13", "pytest:tools/test_arc_disjoint_check.py", "phase1", "local + CI", False)`. Commit `feat(he-lanes): U-HE-36 arc_disjoint_check (U-WT-07) + selection-time refusal; scope is a hint (C-HE-13 §4-5)`.

---

### U-HE-37: Pilot gate — `just lanes-pilot`, `lanes-pilot-report`, O1 recipe, pilot-runner unit

**Scope.** `just lanes-pilot <run-id>` refuses to start unless `just lanes-phase0-check` exits 0 (names the failing row), then prints the manual N-lane pilot recipe (spawn N lanes via `two-lane`, each `lane-init`, run to landing) and records `pilot_run_id` on each lane's reservation; `just lanes-pilot-report <run-id>` computes the §3 iff-clause across the three stores (every lane's arc `merged`, first-parent detection clean, union ledger satisfies C-HE-03/04 invariants, no HITL escalation with a `merge-door-`/`reservation-` cause) and prints PASS/FAIL + friction rows; "recurring" = a `cause_signature` in ≥ 2 of ≥ 3 pilots OR one operator-rated severe. O1 = the instrumented 4-worktree `hook_project_dir()` probe **after** U-HE-29 (a recipe line in the pilot script).

**Spec linkage.** C-HE-13 §1 (mechanical gate), §2 (order; O1 after X6 fix), §3 (≥ 3 pilots at 3–4 lanes gate follow-on orchestration only; success iff-clause; recurring definition); C-HE-06 §10 (attestation tiering during pilots — U-HE-23); §8 AC#1.

**Files.** Create `tools/lanes_pilot.py` (`gate()`, `report(run_id)`), `tools/test_lanes_pilot_gate.py`. Modify `justfile` (`lanes-pilot`, `lanes-pilot-report`), `.claude/skills/two-lane/SKILL.md:140-142` (pilot bar wording: gates follow-on orchestration only), `tools/lanes_verify.py`, `tools/codex-parity-check.sh`.

**Depends on.** U-HE-05, U-HE-33, U-HE-35, U-HE-36, U-HE-30.

**Unit status — read this before the ticks below.** Steps 1–4 ran and their code is merged, but
the unit's Scope-level guarantee is **NOT** fully met, and Step 1's shipped test **diverges from
the skeleton this plan records** (the note at that step gives the divergence). "Implemented" here
means the steps were carried out — never that the plan's sketch is what shipped.

The Scope says `lanes-pilot-report` "computes the §3 iff-clause". It does not, completely:
**three** paths can still produce a false PASS, and all three must close before a PASS is
trustworthy — bounds **(a)**, **(c)** and **(d)** of `B-234`. **(a)** A lane that dies before
recording `pilot_run_id` is invisible, so a four-lane run that lost a lane presents as a valid
clean three-lane one; closing it needs an expected-participant roster, for which terminal door
evidence cannot substitute. **(c)** An already-externally-MERGED PR receives a `merge_sha` from
`merge_door.land()` and satisfies the landing clause; closing it needs a door-published completion
marker the report can read, so "landed through the door and finished" stops being inferred from
reservation payload. **(d)** A malformed live `LEASE` reads as absent, so the report can PASS while
the door's post-merge checks are unfinished; closing it needs an absent-vs-unreadable lease read.
Bound **(b)**, arcless friction rows, is reporting-only and does **not** gate a PASS, so it is not
in the blocking set.

**U-HE-37 must not be treated as closed for any decision that relies on a PASS being trustworthy.**
A checkbox cannot express "implemented, with three named false-PASS paths open" — hence this
paragraph. That same three-bound set, in the same order, is stated in
`.harness/forward-register.yaml`'s `close_out` and in the prose carrier's `Current state`, while
the module docstring of `tools/lanes_pilot.py` carries only (a) and (b) — a gap recorded at
`B-234` as owed, because `tools/` is IMPL and cannot be edited from this DESIGN-surface PR. The
ticks below record that the STEPS ran, not that the guarantee holds.

- [x] **Step 1: Tests**

*The block below is the PRE-IMPLEMENTATION SKELETON, kept as written for the record. It is not
what shipped, and the tick does not claim it is.* It calls `lp.gate()` with no arguments and
monkeypatches `lp.phase0_results`; HEAD defines `gate(results: list[lv.Result], probe:
tuple[str, str] | None = None) -> tuple[int, str]` and `tools/test_lanes_pilot_gate.py` passes
both explicitly, so the sketch as displayed would raise `TypeError`. The divergence is deliberate and is the better design the
review rounds drove toward: taking the results as a parameter makes `gate` pure over its inputs
instead of pinning a module seam the production path does not use, which is what let the §1 and
§2 reductions come straight from `lanes_verify` rather than be restated here. **Read every
skeleton in this plan as unreviewed input, never as a contract** — where it and the shipped code
disagree, the shipped code and the spec win, and this note is the record of one such divergence.

```python
def test_pilot_runner_refuses_on_any_phase0_red(monkeypatch):
    monkeypatch.setattr(lp, "phase0_results", lambda: [lv.Result(lv.Row("C-HE-06", "pytest:x", "phase0", "l", True), "fail", "boom")])
    rc, msg = lp.gate()
    assert rc != 0 and "C-HE-06" in msg and "pytest:x" in msg

def test_pilot_report_iff_clause(tmp_path, monkeypatch):
    # three stores synthesised: reservations (2 merged), gate rows (no merge-door-/reservation- HIL), loop_status (one env NOTIFY)
    ...
    rep = lp.report("pilot-1")
    assert rep["pass"] is True and rep["friction"] == [] 
    # a DEFERRED-HIL with cause merge-door-lease-acquire:… from one of the pilot's lanes INSIDE the window flips it...
    ...
    assert lp.report("pilot-1")["pass"] is False
    # ...a later RESOLVED-HIL for the same item clears it, and a coordination HIL from an OLDER pilot / another lane never counts
    ...
    assert lp.report("pilot-1")["pass"] is True

def test_recurring_definition():
    assert lp.recurring({"pilot-1": {"a:x:y"}, "pilot-2": {"a:x:y"}, "pilot-3": set()}, severe=set()) == {"a:x:y"}
    assert lp.recurring({"pilot-1": {"b:x:y"}, "pilot-2": set(), "pilot-3": set()}, severe={"b:x:y"}) == {"b:x:y"}
```
- [x] **Step 2–3:** RED; implement (`gate()` runs `lanes_verify.phase0_rows()` via `run_row`, returns `(1, "phase0 RED: <contract> <artifact> — <reason>")` on the first non-pass incl. skips; `report()` reads reservations with `pilot_run_id`, `merge-gate-log.jsonl` `BASE_TOCTOU` rows for those arcs, and the shared `loop_status.md` DEFERRED-HIL rows whose cause starts with `merge-door-`/`reservation-`). Recipes:

```python
#!/usr/bin/env python3
"""C-HE-13 §1-3: mechanical pilot gate + pilot report. A pilot against unfixed state produces contaminated signal,
so `gate()` refuses unless every phase0 row PASSES at HEAD (a skip is NOT a pass)."""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path
import finding_record as fr
import lanes_verify as lv

REPO = Path(__file__).resolve().parent.parent
COORD_CAUSES = ("merge-door-", "reservation-")


def phase0_results() -> list[lv.Result]:
    return [lv.run_row(r) for r in lv.phase0_rows()]


def gate() -> tuple[int, str]:
    for res in phase0_results():
        if res.status != "pass":
            return 1, f"phase0 RED: {res.row.contract} {res.row.artifact} — {res.status}: {res.reason}"
    return 0, "phase0 GREEN"


def start(run_id: str) -> int:
    print(f"pilot {run_id}: Phase 0 GREEN. Manual N-lane pilot recipe (3–4 lanes):")
    print("  1. per lane: `git worktree add ../lane-<k> -b <arc-branch>`; `source tools/hooks/lane-init.sh`;")
    print(f"     `uv run python tools/reservations.py update --arc-id $HARNESS_ARC_ID --set pilot_run_id={run_id}` after arc open")
    print("  2. run /roadmap-continue → /ship-pr in each lane to landing (safe-merge wrapper).")
    print("  3. O1: in 4 worktrees run `bash -c 'source tools/hooks/lib.sh; source tools/hooks/loop_lib.sh; loop_status_path'` — one path.")
    print("  4. O3: `uv run python tools/arc_disjoint_check.py historical`.")
    print(f"  5. `just lanes-pilot-report {run_id}` when every lane has landed.")
    return 0


def _loop_status_rows() -> list[dict]:
    p = subprocess.run(["bash", "-c", "source tools/hooks/lib.sh; source tools/hooks/loop_lib.sh; loop_status_path"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    rows = []
    for ln in Path(p).read_text().splitlines() if p and Path(p).exists() else []:
        m = re.match(r"^\|\s*(?P<ts>[^|]+)\|\s*(?P<kind>[^|]+)\|\s*lane=(?P<lane>[^;|]*);cause=(?P<cause>[^|]*)\|(?P<detail>.*)\|\s*$", ln)
        if m:
            rows.append({k: v.strip() for k, v in m.groupdict().items()})
    return rows


def report(run_id: str) -> dict:
    import reservations as rs
    arcs = []
    root = rs.reservations_root()
    for d in root.iterdir() if root.is_dir() else []:
        cur = rs.current(d.name) if d.is_dir() and not d.name.startswith(".") else None
        if cur and cur[1].get("pilot_run_id") == run_id:
            arcs.append(cur[1])
    all_merged = bool(arcs) and all(a["state"] == "merged" for a in arcs)
    gate_rows = fr.read_rows()
    toctou = [r for r in gate_rows if r["producer"] == "BASE_TOCTOU" and r["arc_id"] in {f"merge-{(a.get('merge_sha') or '')[:12]}" for a in arcs if a.get("merge_sha")}]
    # HIL rows scoped to THIS pilot (Codex round-3 P1): the pilot's lanes, inside the pilot window, reduced last-write-wins
    lanes = {a["lane_id"] for a in arcs}
    t0 = min((a["reserved_at"] for a in arcs), default="9999"); t1 = max((a["transitioned_at"] for a in arcs), default="0000")
    pending: dict[str, dict] = {}
    for r in _loop_status_rows():
        if r["lane"] not in lanes or not (t0 <= r["ts"] <= t1):
            continue
        tok = (r["detail"].split() or [""])[0]
        if r["kind"] == "DEFERRED-HIL":
            pending[tok] = r
        elif r["kind"] == "RESOLVED-HIL":
            pending.pop(tok, None)
    hil = [r for r in pending.values() if r["cause"].startswith(COORD_CAUSES)]
    friction = sorted({r["cause"] for r in _loop_status_rows() if r["lane"] in lanes and t0 <= r["ts"] <= t1 and r["kind"] in ("DEFERRED-HIL", "NOTIFY") and r["cause"] != "-"})
    ok = all_merged and not toctou and not hil
    return {"run_id": run_id, "arcs": [a["arc_id"] for a in arcs], "all_merged": all_merged, "base_toctou": len(toctou), "coordination_hil": len(hil), "pass": ok, "friction": friction}


def recurring(per_pilot: dict[str, set[str]], *, severe: set[str]) -> set[str]:
    """C-HE-13 §3: a cause_signature in >= 2 of the >= 3 pilots, OR one the operator rates independently severe."""
    counts: dict[str, int] = {}
    for sigs in per_pilot.values():
        for s in sigs:
            counts[s] = counts.get(s, 0) + 1
    return {s for s, n in counts.items() if n >= 2} | set(severe)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("cmd", choices=("gate", "start", "report")); p.add_argument("run_id", nargs="?")
    a = p.parse_args(argv)
    if a.cmd == "gate":
        rc, msg = gate(); print(msg); return rc
    if a.cmd == "start":
        return start(a.run_id)
    rep = report(a.run_id); print(json.dumps(rep, indent=2)); print("PILOT", "PASS" if rep["pass"] else "FAIL")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

```make
lanes-pilot run_id:
    uv run python tools/lanes_pilot.py gate && uv run python tools/lanes_pilot.py start {{run_id}}
lanes-pilot-report run_id:
    uv run python tools/lanes_pilot.py report {{run_id}}
```
`two-lane/SKILL.md:140-142` → *"Phase 0 (`just lanes-phase0-check` GREEN) gates running N ≥ 2 lanes at all; the ≥ 3 pilots at 3–4 lanes gate only follow-on lane orchestration (automated spawning) — never the right to keep running N lanes manually (C-HE-13 §3)."*
- [x] **Step 4:** Register `Row("C-HE-13", "pytest:tools/test_lanes_pilot_gate.py", "phase1", "local + CI", False)`, `Row("C-HE-13", "just:lanes-pilot-report <run-id>", "phase1", "local", False)` (the runner marks a `just:` row carrying `<run-id>` as `live`). Commit `feat(he-lanes): U-HE-37 mechanical pilot gate + pilot report (C-HE-13 §1-3)`.
- [ ] **Step 5 (execution, after S1–S5 GREEN):** run ≥ 3 pilots at 3–4 lanes; each report line into the evidence log; run O1 and O3 recipes and record.

**Landed (Steps 1–4)** at PR #1517 → `569b8fbfa`, refresh #1518 → `15ece142a` (both `main` runs
green, 2026-09-04). `tools/lanes_pilot.py`: `gate(results, probe)` takes its verdict
from `lanes_verify.phase0_verdict` (§1) AND `lanes_verify.probe_result_verdict` (§2), the same
reductions `just lanes-phase0-check` and `just pilot-gate-check` exit on, so the runner and the
recipes cannot disagree about what "green" means. It is pure over BOTH arguments when both are
supplied — which is how every test drives it — but NOT on the production path: `main()` calls
`gate(phase0_results())` and lets `probe` default to `None`, so `gate` then performs the
`probe_result_verdict()` read itself, and that read touches the gate log. The §1 half is already
lifted to the edge (`phase0_results()` is evaluated by the caller); the §2 half is not.
**Named follow-up, owed on the next `lanes_pilot.py` touch:** make `probe` required and have
`main()` supply `lv.probe_result_verdict()`, so the effect sits at the boundary and `gate` is
pure by construction rather than by convention ([LAW:effects-at-boundaries]). It is deliberately
NOT done here — this file is a DESIGN surface, so a code change cannot ride this PR — a `skip` and a `live` row both count RED without
this module restating the rule. `evaluate(run_id, Stores)` computes the §3 iff-clause pure over the
three stores' contents, so every clause is witnessed with plain data and no filesystem mocking.
Two qualifications the prose must carry, because `B-234` holds them open: clause (a) is checked as
`merged` AND a door-recorded `merge_sha`, which is NOT full proof of a door landing — `merge_door.land()`
also records a `merge_sha` when it encounters an already externally-MERGED PR, so a hand-merged
lane can still satisfy it; and `report()` refuses (exit 2) on the store failures it can DETECT — an
unreadable merged ledger, an absent gate log or loop ledger, a malformed ledger row, a live door
lease naming a pilot arc — but not on a lease it cannot tell from absence, since
`merge_door.read_lease()` maps a present-but-MALFORMED `LEASE` to `None`, which `report()` reads as
"no lease held" and may therefore PASS while post-merge checks are still running. Exit 1 stays
reserved for a measured FAIL. Recipes `lanes-pilot` /
`lanes-pilot-report` ride their own END-ANCHORED arity-bounded guard matcher rather than the generic
`just` verb alternation, which is not end-anchored (`just` executes a trailing token as a second
recipe). Two C-HE-13 phase1 manifest rows registered; the `just:lanes-pilot-report <run-id>` row
carries a placeholder so `_command` marks it LIVE and nothing auto-runs with an invented run id.
`parse_loop_row` was extracted into `loop_cost_baseline` as the one reader of the shared ledger's
row grammar.

**Step 5 is NOT closed and is blocked on an operator action, not on Claude.** Run end to end
against all 57 phase0 rows in a quiet tree, the gate exits 1 naming
`C-HE-08 just:main-protection-verify — fail: MISMATCH contexts differ: missing=['merge-gate log
consistency (C-HE-23 §2 reducer) — blocking', 'split-brain ledger backstop — blocking']`. Live
branch protection lacks those two blocking contexts; `just main-protection-apply` closes the gap and
is an outward-facing write. Until it runs, no pilot may start — which is precisely what C-HE-13 §1
exists to enforce, since a pilot against unfixed state produces contaminated friction signal.

**Review trail: four out-of-family codex rounds — 7 + 6 + 5 + 6 = 24 findings, every one adjudicated
`accepted` on `.harness/merge-gate-log.jsonl` — plus six merge-gate passes.** A recording gap worth
naming: the gate log carries ZERO merge-gate finding rows for this arc. Only the final all-APPROVE
pass was emitted, because a lens verdict binds to the head it reviewed (C-HE-15 §3) and each
absorption moved the head before its BLOCK was recorded. The five intermediate BLOCK passes survive
only in this PR's commit trail and body. The lesson for the next unit: emit each gate pass at the
head it reviewed, BEFORE absorbing it, or its findings never reach the durable record.

Two of the findings across both channels were production defects rather than test gaps. (i) The loop-mode
permission-guard allowlist is not end-anchored and `just` runs a trailing token as a further recipe,
so `just lanes-pilot p1 main-protection-rollback` was auto-approved — verified empirically before
absorbing (a bogus trailing token errors with "justfile does not contain recipe", which is only
reachable if it was parsed as one). The two pilot verbs now carry their own anchored matcher —
2 allow cases and 9 negatives in `tools/hooks/test_permission_guard.sh`, and removing the
`_bash_args_safe` conjunct turns exactly two of them red; the same non-anchoring holds for every parameterless recipe already in that
alternation and is registered at `B-233`, since closing it needs a per-recipe arity audit. (ii)
`ledger_invariants()` treated an arc that is BOTH queued and on merged history as a C-HE-04 breach,
but `arc_metrics._drain_one` unlinks a queue entry only once the row is ALREADY committed, on a
LATER drain pass — so that is the normal self-healing sweep window and, with the workspace drain
held at 29 entries, the steady state; every pilot report would have failed on a condition that
resolves itself. The spec-conformance lens ruled the corrected reading textually right: C-HE-04's
"no third state" is a post-drain postcondition ("after any drain invocation, for every `arc_id` that
was pending at entry"), and a globally-always reading would be unsatisfiable by any implementation
because the sweep is by design a later independent pass. It is now surfaced as
`rows_awaiting_queue_sweep`; the genuine breach (neither queued nor committed) still fails.

**The dominant finding class, at cardinality six within this arc alone: a production seam every test
bypasses.** In order — `main()`'s gate CLI dispatch had no caller; the guard's `_bash_args_safe`
conjunct was unwitnessed because a pre-existing universal guard short-circuited every negative case
before reaching the new branch; `_pilot_arcs` and `_loop_rows` were always monkeypatched away;
`gate()`'s probe-verdict else-branch was bypassed because every direct test passed `probe=`
explicitly; `_pilot_arcs`'s selection loop was covered only at its guard clauses; and finally
`report()`'s SUCCESS path had never executed at all, on a tool whose whole purpose is producing that
report. The sixth instance was in the FIXTURES: `_wire_report` stubbed the gate-log and ledger
readers to `lambda: []`, which is exactly what a mutation hardcoding them away returns, so clause
(a)'s BASE_TOCTOU check and clause (c)'s coordination-HIL check could have gone permanently vacuous
with the suite green. **The rule this arc paid for: a stub whose return value equals the mutation's
return value witnesses nothing** — a stub must return a call counter or content that must appear in
the output. Each fix was mutation-probed with the tree restored byte-identical.

**Four bounds this report cannot close, all registered at `B-234` rather than patched around.**
(i) Pilot membership is INFERRED from the reservations carrying `pilot_run_id`, because nothing
persists an expected roster — a lane that died before recording it is invisible, so a four-lane
pilot that lost one lane can still show three and satisfy the 3–4 check. (ii) The door's own arcless
release attestation falls outside any defensible friction window, because three `merge_door._notify`
details omit the leading arc id every other escalation carries. (iii) A non-empty `merge_sha` does
not prove a DOOR landing: `merge_door.land()` accepts a PR already externally MERGED, reconciles the
cycle and still writes that SHA. (iv) `merge_door.read_lease()` maps a present-but-malformed `LEASE`
to `None`, indistinguishable from no lease, so a corrupted live lease lets the report proceed while
the door's post-merge checks are unfinished. (iii) and (iv) are the same shape as (i) and (ii): the
report sits OUTSIDE the door and cannot verify door completion from the stores alone. Four consecutive rounds converged on
that family, which is the register-and-hold point: the root cause is producer-side. The interim
floor is landed — the report prints `arcs`, `lanes` and a `membership: inferred …` field, so a PASS
cannot be read as "every intended lane landed".

**Cite units by ID, never by line, in anything outside this file.** Inserting this record above
U-HE-38 moved its heading from `:6770` to `:6872`, which stranded the roadmap pointer's line cite
inside this retrospective — and the first correction was itself stale by the time the sibling
finding was absorbed, because that shifted the file again. Two review rounds went to chasing the
coordinate. A line number into a living document is a derived value with no single owner, so it
drifts on every insertion above it; a unit id does not. The refreshed pointer therefore names
`U-HE-38` and this file, with no line number at all. (`roadmap_status.md` itself may not be edited
here — §12.2.1: a commit on `main` touching it without being a verified terminating refresh
hard-fails CI — so the corrected pointer rides `.harness/.next-action-draft`, which the merge
door's §4(viii) refresh installs, and that draft's `post-pr:` MUST carry this PR's real number or
the draft is ignored with a warning and the stale cite survives.)

**A carrier constraint worth recording for every future unit in this plan.**
`codex_context_guard.DESIGN_RE` matches `.*Implementation_Plan_[A-Za-z_]+_v\d`, so THIS file is a
DESIGN surface while `tools/` and `justfile` are IMPL; the two together are a HARD `DESIGN_IMPL_MIX`
without a clearance marker. A marker records a design-substrate VERSION accepted for consumption, so
minting one to carry step ticks would misuse the convention. Step ticks for any unit here therefore
ride their OWN doc-only PR, before or after the code PR, never with it. (B-230's plan escaped this
only because `loop-optimization-plan-2026-09-03.md` does not match that pattern.)

---

### U-HE-38: Cohort report joint on `(concurrent_lanes_at_open, arc_type)`, drift join, correlational header

**Scope.** `summary` gains the joint stratification `(concurrent_lanes_at_open, arc_type_open)` with the existing exact-lever-set discipline; joins `ROADMAP_STATUS_DRIFT` findings (from `merge-gate-log.jsonl`, lane-attributed) by `lane_id`/`arc_id` and correlates incidence against `concurrent_lanes_at_open`; the report header carries the "correlational — assignment to N is operator-chosen; descriptive counts only until N ≥ 2 and `applying` cells populate" statement.

**Spec linkage.** C-HE-28 §1–§3; C-HE-25; §8 AC#10; C-HE-26 §3 (EVALUATE gate: no routing-accuracy claim until ≥ 20 open-labeled arcs with both `arc_type` cells non-zero).

**Files.** Modify `tools/arc_metrics.py` (`summary`), `tools/test_arc_metrics.py`.

**Depends on.** U-HE-11, U-HE-12, U-HE-33.

**Unit status (as-built, ticked 2026-09-16).** Steps 1–4 ran and are merged on PR #1523 (2026-09-05; ten codex rounds to convergence — 30 findings, 27 accepted, 3 rejected, per the PR body). Divergence from the sketch below: Step 4's row is registered as `"C-HE-28 §1+§3"`, not the draft `"C-HE-27/28"` — narrowed at codex r4, and the comment beside the row in `tools/lanes_verify.py` records why. Residuals registered rather than absorbed: B-237 (no emitter appends the §2 drift numerator), B-238 (lever stratification of the joint key, deferred), B-239 (C-HE-28 names the cohort field for lanes while the sensor stores siblings — spec wording routed to back-flow, consumer converts).

- [x] **Step 1: Test**
```python
def test_cohort_by_concurrent_lanes_at_open_and_arc_type(tmp_path, monkeypatch, capsys):
    rows = []
    for n in (1, 2, 4):
        for t in ("inventing", "applying"):
            for i in range(3):
                rows.append({"arc_id": f"{n}-{t}-{i}", "levers_active": [], "arc_span_s": 60.0 * n, "review_rounds": 1, "round_completeness": "complete",
                             "concurrent_lanes_at_open": n, "arc_type_open": t, "lane_id": f"lane-{n}"})
    ledger = tmp_path / "l.jsonl"; ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n"); monkeypatch.setattr(am, "LEDGER", ledger)
    gate = tmp_path / "g.jsonl"; monkeypatch.setattr(am, "GATE_LOG", gate)
    gate.write_text(json.dumps({"finding_id": "x", "producer": "ROADMAP_STATUS_DRIFT", "lane_id": "lane-4", "arc_id": "4-inventing-0", "ts": "t", "record_kind": "finding", "disposition": None, "finding_type": "terminal-drift", "cause_attribution": None, "location": "", "observed_evidence": "", "expected_contract": "", "severity": "hard", "lineage_claim": "", "head_sha": None, "base_sha": None, "diff_digest": None, "round_n": None, "disposition_actor": None, "unique_catch": None}) + "\n")
    am.summary(argparse.Namespace())
    out = capsys.readouterr().out
    assert "correlational" in out and "(N=2, applying) n=3" in out and "median 120.0" in out.split("(N=2, applying)")[1][:80]
    assert "drift incidence by concurrent_lanes_at_open" in out and "N=4: 1/6" in out
```
- [x] **Step 2–3:** RED; implement the joint grouping (`(r.get("concurrent_lanes_at_open"), r.get("arc_type_open"))` → label `(N=<n>, <t>)`, `null` rendered), the drift join (`GATE_LOG = REPO/".harness"/"merge-gate-log.jsonl"`; rows with `producer == "ROADMAP_STATUS_DRIFT"` counted per `concurrent_lanes_at_open` of the joined arc), and the header line: `NOTE: cohort deltas are CORRELATIONAL — assignment to N is operator-chosen (C-HE-28 §3); descriptive counts only until N>=2 and 'applying' cells populate.`

In `arc_metrics.py` (`GATE_LOG = REPO / ".harness" / "merge-gate-log.jsonl"` next to `LEDGER`; block appended to `summary()` after the lever cohorts):
```python
    print("NOTE: cohort deltas are CORRELATIONAL — assignment to N is operator-chosen (C-HE-28 §3); "
          "descriptive counts only until N>=2 and 'applying' cells populate.")
    joint: dict[tuple, list[dict]] = {}
    for r in rows:
        joint.setdefault((r.get("concurrent_lanes_at_open"), r.get("arc_type_open")), []).append(r)
    for (n, t), cohort in sorted(joint.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        spans = [x["arc_span_s"] for x in cohort if x.get("arc_span_s") is not None and x.get("round_completeness") == "complete"]
        print(f"-- (N={json.dumps(n)}, {json.dumps(t)}) n={len(cohort)} " + (f"median {statistics.median(spans):.1f}s" if spans else "no complete spans"))
    # drift incidence by concurrent_lanes_at_open (lane-attributed ROADMAP_STATUS_DRIFT findings, C-HE-28 §2)
    try:
        import finding_record as fr
        drift = [g for g in fr.read_rows(GATE_LOG) if g.get("producer") == "ROADMAP_STATUS_DRIFT"]
    except Exception:  # noqa: BLE001 -- absent/unreadable gate log => no drift rows, stated below
        drift = []
    by_arc = {r.get("arc_id"): r for r in rows}
    per_n: dict[str, list[int]] = {}
    for r in rows:
        per_n.setdefault(json.dumps(r.get("concurrent_lanes_at_open")), []).append(0)
    for g in drift:
        arc = by_arc.get(g.get("arc_id"))
        if arc is not None:
            per_n[json.dumps(arc.get("concurrent_lanes_at_open"))][0] += 1
    print("drift incidence by concurrent_lanes_at_open: " + ", ".join(f"N={n}: {hits[0]}/{len([r for r in rows if json.dumps(r.get('concurrent_lanes_at_open')) == n])}" for n, hits in sorted(per_n.items())))
```

- [x] **Step 4:** Register `Row("C-HE-27/28", "pytest:tools/test_arc_metrics.py::test_cohort_by_concurrent_lanes_at_open_and_arc_type", "measurement", "local + CI", False)`. Commit `feat(he-lanes): U-HE-38 joint (N, arc_type) cohorts + drift join, correlational header (C-HE-28)`.

---

### U-HE-39: Skill-carrier doc sweep — N ≥ 2 wording, blocked list, live-carrier cites, no round cap, non-goals, K5–K8

**Scope.** Bring the in-scope carriers to the spec's normative wording: `two-lane` + `roadmap-continue` state the model at N ≥ 2 without an N=2 literal cap and with the throughput qualifier "well under N×; prior, not measurement"; `two-lane` carries the C-HE-14 blocked/rejected table; `merge-gate` + `ship-pr` cite live carriers for #5/#14 and state #16 void and "no round cap"; Part D non-goals and the K5–K8 dispositions are recorded in `merge-gate`/`ship-pr` as non-goals; a grep witness asserts no numeric round cap in loop skills.

**Spec linkage.** C-HE-01 §1–§4 + Verification; C-HE-14 (table); C-HE-21 §1–§4; C-HE-34; C-HE-35; §8 AC#7 (grep witness).

**Files.** Modify `.claude/skills/two-lane/SKILL.md`, `.claude/skills/roadmap-continue/SKILL.md`, `.claude/skills/merge-gate/SKILL.md`, `.claude/skills/ship-pr/SKILL.md`; create `tools/hooks/test_skill_lanes_docs.sh`.

**Depends on.** U-HE-07, U-HE-28, U-HE-37.

**Unit status (as-built, ticked 2026-09-16).** Steps 1–4 ran and are merged on PR #1525 (2026-09-14). Step 1's witness shipped as `tools/hooks/test_skill_lanes_docs.sh` at 54 checks — PASS=8 FAIL=56 against the pre-sweep skills, PASS=54 FAIL=0 at the landed head — wider than the six-line sketch below; Step 4's row is registered as `"C-HE-01/14/21/34/35"` as written. Touched beyond the sketch: `.agents/skills/merge-gate` (the Codex projection's cap line, bound by `test_codex_workflow_parity.py`) and two lane-id fixes the PR body names. Named residual in the witness: a cap stated with neither a qualifier nor a number is outside what its pattern can see.

- [x] **Step 1: Test** (`tools/hooks/test_skill_lanes_docs.sh`):
```bash
TL=.claude/skills/two-lane/SKILL.md; RC=.claude/skills/roadmap-continue/SKILL.md; MG=.claude/skills/merge-gate/SKILL.md; SP=.claude/skills/ship-pr/SKILL.md
grep -q 'N ≥ 2' "$TL" && grep -q 'N ≥ 2' "$RC" && ok "N >= 2 model stated" || bad "N>=2 wording missing"
grep -Eq 'well under N×|well under Nx' "$TL" && grep -q 'prior, not measurement' "$TL" && ok "throughput qualifier" || bad "throughput qualifier missing"
grep -q 'Rejected and blocked mechanisms' "$TL" && grep -q 'flock' "$TL" && grep -q 'merge=union' "$TL" && ok "C-HE-14 table carried" || bad "blocked list missing"
grep -q 'invariant #16' "$MG" && grep -q 'void' "$MG" && ok "#16 void stated" || bad "#16 not stated"
! grep -Eiq '(max|cap)[^.]{0,20}(round|rounds)[^.]{0,10}[0-9]+|round cap[^.]{0,10}[0-9]+' "$MG" "$SP" "$RC" "$TL" && ok "AC#7: no numeric round cap in loop skills" || bad "numeric round cap found"
grep -q 'C-HE-34' "$SP" && grep -q 'K6' "$MG" && ok "non-goals + K5–K8 recorded" || bad "non-goals missing"
```
- [x] **Step 2–3:** RED; edits: `two-lane/SKILL.md:8` → *"N ≥ 2 lanes build concurrently in isolated worktrees, each with its own gates and reviewers, and land through exactly one merge door, one arc at a time (C-HE-01 §1). N is a dial (§2). Throughput: well under N×; merges serialize; trailing lanes re-gate on head change — **prior, not measurement** until AC#10 (C-HE-28) produces a baseline."* + a `## Rejected and blocked mechanisms (C-HE-14)` section copying the eleven-row table verbatim; `roadmap-continue` gains the same N ≥ 2 sentence + the lane-init/reservation steps (U-HE-21); `merge-gate/SKILL.md` gains *"Invariants bind by live carriage (C-HE-21 §2): #5 is live at merge-gate/SKILL.md 'Parsing — fail closed' and ship-pr's post-merge acceptance; #14 is C-HE-19; **invariant #16 is void** (no concurrent-reviewer-cap carrier exists). No flat round cap anywhere (C-HE-21 §1). K6 self-classification is dropped: a reviewer never acquires authority to suppress its own finding (C-HE-35). No eval-harness / model-judge as a governance gate."*; `ship-pr/SKILL.md` gains a `## Non-goals (C-HE-34)` list (no round cap; no best-of-N as speed fix; no fast mode for throughput; no agent framework; no collapsing review layers).
- [x] **Step 4:** Register `Row("C-HE-01/14/21/34/35", "shell:tools/hooks/test_skill_lanes_docs.sh", "phase0", "local + CI", False)`. Commit `docs(he-lanes): U-HE-39 skill carriers — N>=2 model, blocked list, live-carrier cites, no round cap, non-goals (C-HE-01/14/21/34/35)`.

---
# S7 — Mechanize classes → dedupe executions → local/CI gap (Layer 2)

### U-HE-40: `tools/mechanized_checks/` — seven classes + `just mech-check` + state file + promotion/demotion machine

**Scope.** A small framework (`Check` protocol, runner, C-HE-24 emission with `producer=<check_id>`, `.harness/mechanized-checks-state.json` runtime state) plus one module per self-inflicted defect class, each tagged `kind ∈ {deterministic, hybrid}`; sited at the stable boundary only (`just mech-check` = pre-commit / pre-review / pre-PR; `postedit-lint.sh` stays advisory); promotion (advisory → blocking) after zero `rejected` findings across a fixed replay of the last 20 merged arcs' diffs; demotion with rolling two-strikes hysteresis emitting a `gate_demotion` row + `NOTIFY`.

**Spec linkage.** C-HE-31 §1 (table, kinds), §2 (hybrid classes never "low-risk"), §3 (siting rule P9(c)), §4 (promotion/demotion semantics; state outside the spec), §5 (measure before "removed"), Invariants; C-HE-35 K8(c); C-HE-24.

**Files.** Create `tools/mechanized_checks/__init__.py`, `tools/mechanized_checks/{stale_carry,mutation_probe_reverify,unswept_consumers,unrun_cli,cited_symbol_exists,delta_chain_drift,test_double_fidelity}.py`, `tools/mechanized_checks/runner.py`, `tools/test_mechanized_checks.py`, `.harness/mechanized-checks-state.json`, `tools/mutation-probe-map.json` (the machine-readable twin of the prose `# mutation-probe:` annotations: `{"<test nodeid>": {"file": ..., "lines": "A-B"}}`). Modify `justfile` (`mech-check`, `mech-replay`), `tools/hooks/postedit-lint.sh` (comment: advisory-only by contract; no exit-code change), `tools/lanes_verify.py`, `tools/codex-parity-check.sh`.

**Interfaces.**
```python
# tools/mechanized_checks/__init__.py
@dataclass(frozen=True) class MechFinding(location: str, evidence: str, expected: str, severity: str = "warn")
class Check(Protocol):
    check_id: str; kind: str                       # deterministic | hybrid
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]: ...
STATE_PATH = REPO / ".harness" / "mechanized-checks-state.json"   # {check_id: {"mode": "advisory"|"blocking", "windows": [...]}}
def load_state() -> dict; def save_state(state) -> None
def emit(check: Check, findings, *, arc_id, lane_id, head_sha) -> list[dict]     # C-HE-24 rows, producer=check_id
def evaluate_promotion(check_id, replay_results: list[bool]) -> bool               # 20 arcs, zero rejected → promote
def evaluate_demotion(check_id, windows: list[int]) -> bool                       # ≥2 rejected in each of two consecutive non-overlapping 20-arc windows
CHECKS: list[Check]
```

**Depends on.** U-HE-01, U-HE-05, U-HE-13 (adjudication rows feed the windows), U-HE-34 (measurement before efficiency claims).

- [ ] **Step 1: Failing tests** — `tools/test_mechanized_checks.py`:
```python
import mechanized_checks as mc
from mechanized_checks import runner, stale_carry, cited_symbol_exists, delta_chain_drift, unswept_consumers, unrun_cli, test_double_fidelity as tdf, mutation_probe_reverify as mpr

def test_every_class_declared_with_kind():
    ids = {c.check_id: c.kind for c in mc.CHECKS}
    assert ids == {"stale_carry": "deterministic", "mutation_probe_reverify": "hybrid", "unswept_consumers": "deterministic",
                   "unrun_cli": "deterministic", "cited_symbol_exists": "deterministic", "delta_chain_drift": "deterministic",
                   "test_double_fidelity": "hybrid"}

def test_stale_carry_fixture_and_clean(tmp_path):
    bad = tmp_path / "doc.md"; bad.write_text("Three contracts:\n| a |\n| b |\n\nTBD: fill in\n")
    assert stale_carry.Check().run([bad], tmp_path)
    good = tmp_path / "ok.md"; good.write_text("Two contracts:\n| a |\n| b |\n")
    assert stale_carry.Check().run([good], tmp_path) == []

def test_cited_symbol_exists_fixture_and_clean(tmp_path):
    (tmp_path / "tools").mkdir(); (tmp_path / "tools" / "x.py").write_text("def real():\n    pass\n")
    bad = tmp_path / "d.md"; bad.write_text("see `tools/x.py:9` and `phantom()` in `tools/x.py`\n")
    fs = cited_symbol_exists.Check().run([bad], tmp_path)
    assert {f.location for f in fs} == {"tools/x.py:9", "phantom()"}
    good = tmp_path / "g.md"; good.write_text("see `tools/x.py:1` and `real()` in `tools/x.py`\n")
    assert cited_symbol_exists.Check().run([good], tmp_path) == []

def test_delta_chain_drift(tmp_path):
    (tmp_path / "Doc_v1_8.md").write_text("## 7.4.2 Citation\nbody\n"); (tmp_path / "Doc_v1_9.md").write_text("## 7.4.2 Citation (revised)\n")
    bad = tmp_path / "c.md"; bad.write_text("per `Doc_v1_8.md` §7.4.2\n")
    assert delta_chain_drift.Check().run([bad], tmp_path)          # a later version re-tables §7.4.2
    (tmp_path / "Doc_v1_9.md").write_text("## 9.1 Other\n")
    assert delta_chain_drift.Check().run([bad], tmp_path) == []

def test_unswept_consumers(tmp_path):
    (tmp_path / "a.py").write_text("def gone(): pass\n"); (tmp_path / "b.py").write_text("from a import gone\ngone()\n")
    diff = "-def gone(): pass\n+def kept(): pass\n"
    assert unswept_consumers.Check(diff=diff).run([tmp_path / "a.py"], tmp_path)
    (tmp_path / "b.py").write_text("print(1)\n")
    assert unswept_consumers.Check(diff=diff).run([tmp_path / "a.py"], tmp_path) == []

def test_unrun_cli(monkeypatch):
    c = unrun_cli.Check(claims=["just nothing-here-xyz"], runner=lambda cmd: (1, ""))
    assert c.run([], REPO)
    c = unrun_cli.Check(claims=["just x"], runner=lambda cmd: (0, "ok"))
    assert c.run([], REPO) == []


def test_unrun_cli_never_shells_out(monkeypatch):
    called = []
    monkeypatch.setattr(unrun_cli.subprocess, "run", lambda *a, **k: called.append(k.get("shell")) or type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})())
    rc, out = unrun_cli._default_runner("just check; rm -rf /")
    assert rc == 126 and "unsafe" in out and called == []
    rc, out = unrun_cli._default_runner("uv run python tools/x.py --flag")
    assert rc == 0 and called == [False]

# hybrid classes: an intentionally-false annotation is DETECTED as false
def test_mutation_probe_reverify_detects_false_annotation(tmp_path, monkeypatch):
    monkeypatch.setattr(mpr, "probe", lambda file, lines, test: 1)     # PROBE FAILED: test stayed green
    c = mpr.Check(map_={"tools/test_x.py::t": {"file": "tools/x.py", "lines": "1-2"}})
    fs = c.run([Path("tools/test_x.py")], REPO)
    assert fs and "annotation is FALSE" in fs[0].evidence

def test_test_double_fidelity_flags_non_subclass():
    class Real: ...
    class Fake: ...
    class Good(Real): ...
    assert tdf.assert_fake_is_subclass(Good, Real) is None
    with pytest.raises(AssertionError):
        tdf.assert_fake_is_subclass(Fake, Real)

def test_promotion_demotion_state_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "STATE_PATH", tmp_path / "state.json"); rows = []
    monkeypatch.setattr(mc, "_notify", lambda check, why: rows.append(("NOTIFY", check, why)))
    monkeypatch.setattr(mc, "_demotion_row", lambda check, counts: rows.append(("gate_demotion", check, counts)))
    assert mc.evaluate_promotion("stale_carry", [False] * 20) is True          # 20 replayed arcs, 0 rejected → promote
    assert mc.evaluate_promotion("stale_carry", [False] * 19 + [True]) is False
    assert mc.evaluate_demotion("stale_carry", windows=[3]) is False            # one window: no demotion
    assert mc.evaluate_demotion("stale_carry", windows=[3, 1]) is False         # second window < 2
    assert mc.evaluate_demotion("stale_carry", windows=[3, 2]) is True          # two consecutive windows ≥2 → demote
    assert ("NOTIFY", "stale_carry", "demoted") in rows and any(r[0] == "gate_demotion" for r in rows)
    assert mc.load_state()["stale_carry"]["mode"] == "advisory"
```
- [ ] **Step 2: RED**; **Step 3: Implement.** Framework (`__init__.py`):
```python
"""C-HE-31: mechanized pre-checks for the self-inflicted defect classes. Sited ONLY at a stable
boundary (`just mech-check`: pre-commit / pre-review / pre-PR); never a blocking PostToolUse per edit.
Runtime kind/window state lives in .harness/mechanized-checks-state.json -- a runtime event never
edits the spec. No mechanized check is ever cited as grounds for a round cap."""
from __future__ import annotations
import json, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import finding_record as fr

REPO = Path(__file__).resolve().parents[2]
STATE_PATH = REPO / ".harness" / "mechanized-checks-state.json"
WINDOW = 20; PROMOTE_MAX_REJECTED = 0; DEMOTE_STRIKES = 2

@dataclass(frozen=True)
class MechFinding:
    location: str; evidence: str; expected: str; severity: str = "warn"

class Check(Protocol):
    check_id: str; kind: str
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]: ...

def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}

def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

def mode(check_id: str) -> str:
    return load_state().get(check_id, {}).get("mode", "advisory")

def emit(check, findings: list[MechFinding], *, arc_id: str, lane_id: str, head_sha: str | None) -> list[dict]:
    rows = []
    for n, f in enumerate(findings, 1):
        core = fr.FindingCore(fr.make_finding_id(check.check_id, head_sha or "nohead", f.location, n), f.location, f.evidence, f.expected,
                              f.severity, "mechanized-" + check.kind, "fresh", check.check_id)
        row = fr.make_row(core, fr.Envelope("finding", fr.now_iso(), arc_id, lane_id, head_sha, None, None, None, cause_attribution=check.check_id))
        fr.append_row(row); rows.append(row)
    return rows

def _notify(check_id: str, why: str) -> None:
    import reservations as rs
    rs.emit_loop_row("NOTIFY", "lanes_verify", f"mech-check:{why}:{check_id}", f"mechanized check {check_id} {why}")

def _demotion_row(check_id: str, counts: list[int]) -> None:
    core = fr.FindingCore(fr.make_finding_id("lanes_verify", "nohead", check_id, 0), check_id, f"rejected counts in two consecutive windows: {counts}",
                          "C-HE-31 §4(b)", "warn", "gate_demotion", "policy", "lanes_verify")
    fr.append_row(fr.make_row(core, fr.Envelope("gate_demotion", fr.now_iso(), "policy", "lanes_verify", None, None, None, None, cause_attribution=check_id)))

def evaluate_promotion(check_id: str, replay_rejected: list[bool]) -> bool:
    """(a) fixed replay of the last 20 merged arcs, evaluated once: zero rejected → blocking.
    OC stated honestly: P(pass | true per-arc FP rate 0.05) = 0.95^20 ≈ 0.36 → excludes p ≳ 0.15, not less."""
    if len(replay_rejected) < WINDOW or sum(replay_rejected) > PROMOTE_MAX_REJECTED:
        return False
    st = load_state(); st.setdefault(check_id, {})["mode"] = "blocking"; st[check_id]["promoted_on_replay"] = len(replay_rejected); save_state(st)
    return True

def evaluate_demotion(check_id: str, windows: list[int]) -> bool:
    """(b) rolling non-overlapping 20-arc windows, evaluated at `just lanes-verify` and CI; demote only when
    >= 2 rejected in EACH of two consecutive windows (a single window at p=0.03 flaps with P ≈ 0.12)."""
    st = load_state(); st.setdefault(check_id, {})["windows"] = windows[-2:]
    if len(windows) >= 2 and windows[-1] >= DEMOTE_STRIKES and windows[-2] >= DEMOTE_STRIKES and st[check_id].get("mode") != "advisory":
        st[check_id]["mode"] = "advisory"; save_state(st)
        _demotion_row(check_id, windows[-2:]); _notify(check_id, "demoted")
        return True
    save_state(st)
    return False

from . import stale_carry, mutation_probe_reverify, unswept_consumers, unrun_cli, cited_symbol_exists, delta_chain_drift, test_double_fidelity  # noqa: E402
CHECKS: list = [stale_carry.Check(), mutation_probe_reverify.Check(), unswept_consumers.Check(), unrun_cli.Check(), cited_symbol_exists.Check(), delta_chain_drift.Check(), test_double_fidelity.Check()]
```
Class modules (each ≤ 40 lines; the load-bearing logic shown):
- `stale_carry.py` (`kind="deterministic"`): regexes `PLACEHOLDER = r"\b(TBD|TODO\(spec\)|XXX|<NNN>|<N>|vX\.Y)\b"` → finding per hit; and count-vs-table: for each `(\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b) (contracts|rows|units|files|tests|stores)\s*:?\s*\n((?:\|.*\n)+)` in the changed markdown, compare the word/number to the table's data-row count (excluding header/separator) → finding on mismatch.
- `mutation_probe_reverify.py` (`kind="hybrid"`): reads `tools/mutation-probe-map.json`; for every `# mutation-probe:` annotation in a changed test file (`re.finditer(r"# mutation-probe: (.*)\n(?:@[^\n]*\n)*def (test_\w+)", text)`), require a map entry for `<file>::<test>` (missing → finding "annotation not mechanized"), then `probe(file, lines, test)` = `subprocess.run(["uv","run","python","tools/mutation_probe.py","--file",...,"--lines",...,"--test", f"uv run pytest {nodeid} -q"]).returncode`; `1` → finding *"annotation is FALSE: test stayed green with the named lines removed"*; `2/3` → finding "probe indeterminate". Never re-reads the annotation as evidence.
- `unswept_consumers.py` (`kind="deterministic"`): from the diff (`git diff --unified=0 origin/main...HEAD` unless injected), collect removed/renamed `def|class` names (`^-\s*(?:def|class)\s+(\w+)`), then for each name `rg -n --glob '!*.md' '\b<name>\b'` (or `graft callers <name>` when available) outside the diff's own hunks → finding per surviving consumer.
- `unrun_cli.py` (`kind="deterministic"`): `claims` = every `` `just <recipe>` `` / `` `uv run python tools/<x>.py …` `` in the PR body / commit message under a line matching `^(Verified|Checked|Ran):`; for each, `runner(cmd)` → `(rc, out)`; `rc != 0 or not out.strip()` → finding *"claimed clean but exit <rc> / empty output"*.
- `cited_symbol_exists.py` (`kind="deterministic"`): in changed `.md`, `` `<path>:<line>` `` → path must exist and have ≥ line lines; `` `<name>()` `` adjacent (same line) to a `` `<path>` `` → `re.search(rf"\b(def|class)\s+{name}\b", path_text)`; each miss → finding.
- `delta_chain_drift.py` (`kind="deterministic"`): for `` `<Stem>_v<major>_<minor>.md` §<sec> `` cites, glob later versions `<Stem>_v<major>_<minor+k>.md`; if a later version contains a heading for the same `§<sec>` → finding *"§ re-tabled in <later>; cite the last substantive definition"* (this is the workspace's delta-baseline convention, CLAUDE.md §2).
- `test_double_fidelity.py` (`kind="hybrid"`): exposes `assert_fake_is_subclass(fake, real)` for shared fixtures; the check scans changed test files for `class (Fake|Stub|Dummy)\w+\b(?!\()` (a bare class with no base) that is passed where a real type is expected (`= <FakeName>(`), reports each as *"test double without fidelity assertion"* unless a `assert_fake_is_subclass(<FakeName>` call exists in the file. (Its mutation-probe: remove the base class from a Good double → the assertion fires.)

The seven modules, in full (each exposes `Check` with `check_id`, `kind`, `run(changed_files, repo)`):
```python
# tools/mechanized_checks/stale_carry.py
from __future__ import annotations
import re
from pathlib import Path
from . import MechFinding
PLACEHOLDER = re.compile(r"\b(TBD|TODO\(spec\)|XXX|<NNN>|<N>|vX\.Y)\b")
COUNT_TABLE = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b\s+(contracts|rows|units|files|tests|stores)\s*:?\s*\n((?:\|.*\n)+)", re.I)
WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten".split())}
class Check:
    check_id = "stale_carry"; kind = "deterministic"
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]:
        out = []
        for f in changed_files:
            if f.suffix != ".md" or not f.exists():
                continue
            text = f.read_text(errors="replace")
            for m in PLACEHOLDER.finditer(text):
                out.append(MechFinding(f"{f}:{text.count(chr(10), 0, m.start()) + 1}", f"placeholder token {m.group(1)!r}", "no placeholder tokens in shipped text"))
            for m in COUNT_TABLE.finditer(text):
                claimed = WORDS.get(m.group(1).lower(), None) if not m.group(1).isdigit() else int(m.group(1))
                rows = [ln for ln in m.group(3).splitlines() if ln.startswith("|") and not re.match(r"^\|[\s:-]+\|", ln)]
                data = max(0, len(rows) - 1)   # minus header
                if claimed is not None and claimed != data:
                    out.append(MechFinding(f"{f}:{text.count(chr(10), 0, m.start()) + 1}", f"claims {claimed} {m.group(2)} but the table has {data} data rows", "count claims match the enumerated table"))
        return out
```
```python
# tools/mechanized_checks/mutation_probe_reverify.py
from __future__ import annotations
import json, re, subprocess
from pathlib import Path
from . import MechFinding, REPO
MAP = REPO / "tools" / "mutation-probe-map.json"
ANNOT = re.compile(r"# mutation-probe: (?P<what>.*)\n(?:@[^\n]*\n)*def (?P<test>test_\w+)")
def probe(file: str, lines: str, test: str) -> int:
    return subprocess.run(["uv", "run", "python", "tools/mutation_probe.py", "--file", file, "--lines", lines, "--test", f"uv run pytest {test} -q"], cwd=REPO, capture_output=True, text=True).returncode
class Check:
    check_id = "mutation_probe_reverify"; kind = "hybrid"   # mutation-probe-backed, NOT sub-second, never "low-risk"
    def __init__(self, map_: dict | None = None):
        self.map_ = map_ if map_ is not None else (json.loads(MAP.read_text()) if MAP.exists() else {})
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]:
        out = []
        for f in changed_files:
            if not (f.name.startswith("test_") and f.suffix == ".py") or not f.exists():
                continue
            for m in ANNOT.finditer(f.read_text()):
                node = f"{f.as_posix()}::{m['test']}"
                entry = self.map_.get(node)
                if entry is None:
                    out.append(MechFinding(node, "annotation not mechanized (no tools/mutation-probe-map.json entry)", "every # mutation-probe: annotation has a machine-readable file/lines twin")); continue
                rc = probe(entry["file"], entry["lines"], node)   # re-VERIFY the named mutation; never re-read the prose
                if rc == 1:
                    out.append(MechFinding(node, f"annotation is FALSE: test stayed green with {entry['file']}:{entry['lines']} removed", "the named mutation turns the test RED", "hard"))
                elif rc in (2, 3):
                    out.append(MechFinding(node, f"probe indeterminate (rc {rc})", "probe must return PINNED or FAILED"))
        return out
```
```python
# tools/mechanized_checks/unswept_consumers.py
from __future__ import annotations
import re, subprocess
from pathlib import Path
from . import MechFinding
REMOVED_DEF = re.compile(r"^-\s*(?:def|class)\s+(\w+)", re.M)
class Check:
    check_id = "unswept_consumers"; kind = "deterministic"
    def __init__(self, diff: str | None = None): self.diff = diff
    def _diff(self, repo: Path) -> str:
        # committed + staged + unstaged (Codex round-5 P2): `git diff origin/main` compares the WORKING TREE against
        # the base, so a pending removal is visible to a pre-commit mech-check
        return self.diff if self.diff is not None else subprocess.run(["git", "-C", str(repo), "diff", "--unified=0", "origin/main"], capture_output=True, text=True).stdout
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]:
        out = []
        for name in sorted(set(REMOVED_DEF.findall(self._diff(repo)))):
            hits = subprocess.run(["rg", "-n", "--glob", "!*.md", "-w", name, str(repo)], capture_output=True, text=True).stdout.splitlines()
            # every remaining reference IS a surviving consumer -- the removed definition itself no longer matches, so
            # NO whole-file exclusion (Codex round-5 P2: excluding changed files hid consumers outside the hunks)
            for h in hits:
                out.append(MechFinding(h.split(":", 1)[0] + ":" + h.split(":", 2)[1], f"consumer of removed/renamed symbol {name!r} still present", "run `graft callers <sym> --depth all` before 'complete'; sweep every consumer"))
        return out
```
```python
# tools/mechanized_checks/unrun_cli.py
from __future__ import annotations
import re, subprocess
from pathlib import Path
from . import MechFinding
CLAIM_LINE = re.compile(r"^(?:Verified|Checked|Ran):\s*(.*)$", re.M | re.I)
CMD = re.compile(r"`((?:just|uv run python tools/)[^`]+)`")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./=:@,+-]+$")
def _default_runner(cmd: str) -> tuple[int, str]:
    """Never a shell (Codex round-3 P1: the claim text comes from a commit message). Allowlisted argv only."""
    import shlex
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return 126, "unsafe claim: unparseable"
    if not argv or argv[0] not in ("just", "uv") or not all(_SAFE_TOKEN.match(t) for t in argv):
        return 126, "unsafe claim: only plain `just <recipe> [args]` / `uv run python tools/<x>.py [args]` tokens are executed"
    p = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=600)
    return p.returncode, (p.stdout or "") + (p.stderr or "")
class Check:
    check_id = "unrun_cli"; kind = "deterministic"
    def __init__(self, claims: list[str] | None = None, runner=_default_runner): self.claims, self.runner = claims, runner
    def _claims(self, repo: Path) -> list[str]:
        if self.claims is not None:
            return self.claims
        srcs = []
        pr = subprocess.run(["gh", "pr", "view", "--json", "body", "--jq", ".body"], cwd=repo, capture_output=True, text=True, timeout=30)
        if pr.returncode == 0:
            srcs.append(pr.stdout)
        else:
            self.source_warning = "PR body unavailable (gh: %s); only the HEAD commit message was scanned" % (pr.stderr.strip()[:80] or "no PR")
        srcs.append(subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%B"], capture_output=True, text=True).stdout)
        return [c for body in srcs for line in CLAIM_LINE.findall(body) for c in CMD.findall(line)]
    source_warning: str | None = None
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]:
        out = []
        claims = self._claims(repo)
        if self.source_warning:
            out.append(MechFinding("unrun_cli", self.source_warning, "verification claims are read from the PR body AND the commit message"))
        for cmd in claims:
            rc, text = self.runner(cmd)
            if rc != 0 or not text.strip():
                out.append(MechFinding(cmd, f"claimed clean but exit {rc} / {'empty' if not text.strip() else 'non-empty'} output", "assert exit code AND positive content before 'clean'"))
        return out
```
```python
# tools/mechanized_checks/cited_symbol_exists.py
from __future__ import annotations
import re
from pathlib import Path
from . import MechFinding
FILE_LINE = re.compile(r"`([\w./-]+\.(?:py|sh|md|yml|yaml|toml)):(\d+)(?:-\d+)?`")
SYMBOL_IN = re.compile(r"`(\w+)\(\)`[^`\n]*`([\w./-]+\.(?:py|sh))`")
class Check:
    check_id = "cited_symbol_exists"; kind = "deterministic"
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]:
        out = []
        for f in changed_files:
            if f.suffix != ".md" or not f.exists():
                continue
            text = f.read_text(errors="replace")
            for path, line in FILE_LINE.findall(text):
                p = repo / path
                if not p.exists() or len(p.read_text(errors="replace").splitlines()) < int(line):
                    out.append(MechFinding(f"{path}:{line}", "cited file:line does not resolve", "every file:line cite resolves at HEAD"))
            for name, path in SYMBOL_IN.findall(text):
                p = repo / path
                if not p.exists() or not re.search(rf"\b(?:def|class|function)\s+{re.escape(name)}\b|^{re.escape(name)}\(\)\s*\{{", p.read_text(errors="replace"), re.M):
                    out.append(MechFinding(f"{name}()", f"cited symbol not defined in {path}", "cited symbols exist"))
        return out
```
```python
# tools/mechanized_checks/delta_chain_drift.py
from __future__ import annotations
import re
from pathlib import Path
from . import MechFinding
CITE = re.compile(r"`([A-Za-z0-9_]+?)_v(\d+)_(\d+)\.md`\s*§\s*([\d.]+)")
class Check:
    check_id = "delta_chain_drift"; kind = "deterministic"
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]:
        out = []
        for f in changed_files:
            if f.suffix != ".md" or not f.exists():
                continue
            for stem, major, minor, sec in CITE.findall(f.read_text(errors="replace")):
                later = sorted(p for p in repo.rglob(f"{stem}_v{major}_*.md") if int(p.stem.rsplit("_", 1)[1]) > int(minor))
                for p in later:
                    if re.search(rf"^#+\s*{re.escape(sec)}\b", p.read_text(errors="replace"), re.M):
                        out.append(MechFinding(f"{f}:{stem}_v{major}_{minor} §{sec}", f"§{sec} is re-tabled in {p.name}; cite the version of last substantive definition", "delta-chain cites name the last substantive definition (CLAUDE.md §2)"))
        return out
```
```python
# tools/mechanized_checks/test_double_fidelity.py
from __future__ import annotations
import re
from pathlib import Path
from . import MechFinding
BARE_DOUBLE = re.compile(r"^class\s+((?:Fake|Stub|Dummy)\w*)\s*:", re.M)
def assert_fake_is_subclass(fake: type, real: type) -> None:
    """Shared fixture helper: a test double stands in for `real` only if it IS a `real` (wrong-fidelity doubles
    pass tests the real type would fail)."""
    assert issubclass(fake, real), f"{fake.__name__} is not a subclass of {real.__name__}: wrong-fidelity test double"
class Check:
    check_id = "test_double_fidelity"; kind = "hybrid"
    def run(self, changed_files: list[Path], repo: Path) -> list[MechFinding]:
        out = []
        for f in changed_files:
            if not (f.name.startswith("test_") and f.suffix == ".py") or not f.exists():
                continue
            text = f.read_text(errors="replace")
            for name in BARE_DOUBLE.findall(text):
                if f"assert_fake_is_subclass({name}" not in text and re.search(rf"=\s*{name}\(", text):
                    out.append(MechFinding(f"{f}:{name}", "test double without a fidelity assertion (bare class, no base)", "assert_fake_is_subclass(<double>, <real>) in a shared fixture"))
        return out
```

`runner.py`: `main(["--changed", ...] | default = git diff names vs origin/main)`; runs every check; emits rows; prints per-check counts; exit 1 iff any finding from a check whose `mode == "blocking"`. Recipes: `mech-check` (runner), `mech-replay check_id` (replays the last 20 merged arcs' diffs from `git log --merges -20`, prints the rejected count = adjudication rows with `disposition=rejected` for that producer over those heads, and calls `evaluate_promotion` when 0). Initial state file: all seven `advisory`.
- [ ] **Step 4: GREEN**; the two hybrid classes' rows in the manifest are `layer2` (minutes; not phase0). Register `Row("C-HE-31", "pytest:tools/test_mechanized_checks.py", "layer2", "local + CI", True)` and `Row("C-HE-31 §4", "pytest:tools/test_mechanized_checks.py::test_promotion_demotion_state_machine", "layer2", "local + CI", False)`. `postedit-lint.sh` header comment: *"Advisory by contract (C-HE-31 §3): findings only, never a blocking exit per edit."*
- [ ] **Step 5: Commit** — `git add tools/mechanized_checks tools/test_mechanized_checks.py .harness/mechanized-checks-state.json tools/mutation-probe-map.json justfile tools/hooks/postedit-lint.sh tools/lanes_verify.py tools/codex-parity-check.sh && git commit -m "feat(he-lanes): U-HE-40 mechanized defect-class checks + promotion/demotion machine at the stable boundary (C-HE-31)"`.

---

### U-HE-41: Equivalence-proof rows + removal of proven double-runs

**Scope.** A duplicated execution is removed only with a `record_kind=equivalence_proof` row naming the decorrelated party (Codex / fresh reviewer) or the deterministic execution-context diff; first candidates: the two within-CI double-runs and `codex-check` re-run after CI-green on the same SHA.

**Spec linkage.** C-HE-32 §1–§3 + Verification (log witness); C-HE-24.

**Files.** Modify `tools/finding_record.py` (helper `equivalence_proof_row`), `tools/test_finding_record.py` (`test_equivalence_proof_rows`), `.github/workflows/ci.yml` (remove the proven double-runs), `justfile` (`codex-check` post-CI-green re-run guarded), `.claude/skills/ship-pr/SKILL.md`.

**Depends on.** U-HE-01, U-HE-40 (measurement instrument present).

- [ ] **Step 1: Test**
```python
def test_equivalence_proof_rows(tmp_path):
    row = fr.equivalence_proof_row(removed="ci: pytest tools twice on same SHA", proof_by="codex-review", context_diff="identical cmd/env/inputs/sha",
                                   arc_id="pr-1", lane_id="h", head_sha="a"*40)
    fr.append_row(row, tmp_path / "g.jsonl")
    r = fr.read_rows(tmp_path / "g.jsonl")[0]
    assert r["record_kind"] == "equivalence_proof" and r["finding_type"] == "equivalence_proof" and r["disposition_actor"] == "codex-review" and r["producer"] != r["disposition_actor"]
    with pytest.raises(fr.RecordError):
        fr.equivalence_proof_row(removed="x", proof_by="ship-pr", context_diff="", arc_id="a", lane_id="l", head_sha=None, producer="ship-pr")   # beneficiary's own say-so
```
- [ ] **Step 2–3:** RED; implement `equivalence_proof_row(*, removed, proof_by, context_diff, arc_id, lane_id, head_sha, producer="ship-pr")` (raises `RecordError` when `proof_by == producer`; `disposition="accepted"`, `disposition_actor=proof_by`).

In `tools/finding_record.py`:
```python
def equivalence_proof_row(*, removed: str, proof_by: str, context_diff: str, arc_id: str, lane_id: str,
                          head_sha: str | None, producer: str = "ship-pr") -> dict:
    """C-HE-32: a duplicated execution may be removed only on a proof by a party DECORRELATED from the
    beneficiary (or a deterministic execution-context diff). The beneficiary's own say-so is refused."""
    if proof_by == producer:
        raise RecordError(f"equivalence proof by the beneficiary itself ({producer}) is prohibited (C-HE-32 §3)")
    core = FindingCore(make_finding_id(producer, head_sha or "nohead", removed, 0), removed,
                       f"removed duplicated execution; proof: {context_diff}", "C-HE-32 §1", "info",
                       "equivalence_proof", "decorrelated", producer)
    return make_row(core, Envelope("equivalence_proof", now_iso(), arc_id, lane_id, head_sha, None, None, None,
                                   cause_attribution="duplicate_execution_removed", disposition="accepted",
                                   disposition_actor=proof_by))
```
Recipe: `equivalence-proof removed proof_by context_diff:` → `uv run python -c "import finding_record as fr, os; fr.append_row(fr.equivalence_proof_row(removed='{{removed}}', proof_by='{{proof_by}}', context_diff='{{context_diff}}', arc_id=os.environ.get('HARNESS_ARC_ID','policy'), lane_id=os.environ.get('HARNESS_LANE_ID','ship-pr-lane'), head_sha=None))"`.
 Then: (1) identify the two within-CI double-runs by reading `ci.yml` job steps for identical `run:` lines on the same checkout (the executor lists them in the PR body); (2) obtain the proof — `just codex-review` on the removal diff with the prompt "confirm the two removed steps are byte-identical executions (command, env, inputs, SHA)"; record the row via a `just equivalence-proof` recipe wrapping the helper; (3) remove the steps; (4) `codex-check` after CI-green on the same SHA: `ship-pr` step reworded to *"skip `just codex-check` when the CI `codex-context-guard` job on this exact SHA is green (equivalence proof row `<finding_id>` recorded 2026-08-…)"*.
- [ ] **Step 4:** Register `Row("C-HE-32/33", "pytest:tools/test_finding_record.py::test_equivalence_proof_rows", "layer2", "CI", False)`. Commit `chore(he-lanes): U-HE-41 equivalence-proof rows; remove proven CI double-runs (C-HE-32)`.

---

### U-HE-42: Local/CI parity for `codex_context_guard` — `just codex-context-check-ci`

**Scope.** Close K3 as a flag/ref-parity gap: add a local recipe mirroring CI's explicit-ref invocation (`check --base-ref <merge-base> --head-ref HEAD --allow-roadmap-drift`), and a parity test asserting the same finding set for the same SHA between the CI-shaped and local-shaped invocations (or the documented, named exclusion).

**Spec linkage.** C-HE-33 §1–§4 (target ~58 s; environment-irreproducible checks named; corrected claim; outcome measure via C-HE-28 cohorts).

**Files.** Modify `justfile:84-87` (add `codex-context-check-ci`), `tools/test_codex_context_guard.py` (`test_local_ci_parity`), `.claude/skills/ship-pr/SKILL.md` (pre-push: run the CI-shaped recipe).

**Depends on.** U-HE-33.

**Unit status (as-built, ticked 2026-09-16).** Steps 1–4 ran and are merged on PR #1527 (2026-09-16; the C-HE-33 §4 Class 1 fork, its Reading-C ratification and this plan's Rev 2026-09-15 line landed first on doc-only PR #1530). Divergences from the sketch below: Step 1's `--local-shape` flag was never built — parity is witnessed by running the real recipe body parsed from the `justfile` against CI's argv parsed from `ci.yml`, and `PARITY_EXCLUSIONS` holds seven named entries, each driven by its own test; Step 2–3's recipe fetches `origin/main` first, resolves both refs once, refuses a HEAD that moved while the guard ran, and prints the checked sha for the carrier to push by name; Step 4's row is registered as `"C-HE-32/33 §3"` (the §4 measure moved to C-HE-28 §4 per the Rev line). Review: codex r1–r12 with one gemini failover, merge-gate lens rounds 1–6 — 28 findings, 28 adjudicated (25 accepted, 3 rejected). A tick here means the step was carried out — never that the sketch is what shipped or that the Scope's parity guarantee is fully met: B-246 records, measured, that parity is false in CI's two-parent merge checkout while the landed witness drives the one-parent shape. Residual rows: B-247 (C-HE-27 spans keep only round 1) is registered on PR #1532; B-245 (C-HE-28 §4 outcome-measure cohorts unowned) and B-246 are drafted and owed — held behind the unmerged U-HE-40 branch (B-244), not yet on the register.

- [x] **Step 1: Test**
```python
def test_local_ci_parity(tmp_path, monkeypatch):
    """Same SHA: CI-shaped and local-shaped invocations → identical finding codes (named exclusions only)."""
    ci = ccg.main(["check", "--base-ref", base, "--head-ref", head, "--allow-roadmap-drift", "--json"])  # capture stdout json
    local = ccg.main(["check", "--base-ref", base, "--head-ref", head, "--allow-roadmap-drift", "--json", "--local-shape"])
    assert codes(ci) - EXCLUDED == codes(local) - EXCLUDED
    assert EXCLUDED == {"OPEN_PRS_UNAVAILABLE"}   # gh-dependent: named, not silently dropped
```
- [x] **Step 2–3:** RED; recipe:
```make
# C-HE-33: CI-shaped invocation of the guard (explicit refs, roadmap-drift tolerated as on main's push run).
codex-context-check-ci:
    /usr/bin/python3 tools/codex_context_guard.py check --base-ref "$(git merge-base origin/main HEAD)" --head-ref HEAD --allow-roadmap-drift
```
`ship-pr`: *"Before the single push: `just codex-context-check-ci` (parity with CI's guard job — converge locally, push once)."* The outcome measure (≥ 6-CI-run branch share; CANCELLED share) is tracked under **C-HE-28 §4**, NOT built here — see `.harness/class_1_fork_c_he_33_outcome_measure_mandates_an_input_that_does_not_exist.md`.

**Rev 2026-09-15 (U-HE-42 execution correction, as-built — Class 1 fork ratified under Reading C).** This line previously read *"Track the outcome measure … as two lines in `summary` (`arc_metrics.py`) over `ci_runs` (already on the row) — no new store"*, which is unsatisfiable as written and contradicted this unit's own `**Spec linkage.**` line above (*"outcome measure via C-HE-28 cohorts"*). Both named inputs fail at HEAD: `ci_runs` is `len(hit)` from `ci_metrics(row.merge_sha)` (`tools/arc_metrics.py:698`, `:562-574`) — runs on the MERGE COMMIT, so there is no branch-level denominator for a "share of branches burning ≥ 6 CI runs"; and `conclusion` is fetched (`:558`) but used only to filter timings (`:569`) and never persisted, so a CANCELLED share is not derivable (`ci_runs − len(ci_wall_s)` yields NON-GREEN, conflating CANCELLED with `failure` — a distinction C-HE-19 §1 makes load-bearing). Repairing either requires a new captured field, which the same sentence forbade. Resolved by reconciling this line to `:7452` and to spec C-HE-33 §4, whose own words place the two shares as "the tracked cohorts (**C-HE-28**)": the measure belongs to C-HE-28's owner, and U-HE-42 owes only the parity half (C-HE-33's Verification names exactly one witness, the parity test — built and green). Tracking row: **B-245**, re-scoped to C-HE-28 §4.
- [x] **Step 4:** Register `Row("C-HE-32/33", "pytest:tools/test_codex_context_guard.py::test_local_ci_parity", "layer2", "CI", False)`. Commit `feat(he-lanes): U-HE-42 codex-context-check-ci parity recipe + parity test (C-HE-33)`.

---

# S8 — Shadow trial, wired live

### U-HE-43: `tools/shadow_trial.py` — scoring reducer, `no_finding` markers, kill rule n=30 / < 2, OC table, HITL delivery

**Scope.** The second reviewer's shadow lens runs live off the blocking path from the first Arc-7 deploy: `ship-pr` invokes it after the blocking chain (non-blocking); every scored round emits finding rows or exactly one `no_finding` marker with `producer=<second_reviewer_identity>`; `unique_catch=true` iff (a) location+finding_type absent from blocking reviewers' rows for the same `head_sha` AND (b) last disposition `accepted`; the reducer reproduces the kill/keep decision from `merge-gate-log.jsonl` alone; the pre-committed rule (n=30, kill if < 2) and its OC table are recomputed by the test from the binomial; at round n the decision fires as an escalation-kind HITL request; adjudicator identity is `disposition_actor` of neither model family.

**Spec linkage.** C-HE-29 §1–§5, Invariants, Verification (reducer over 30 synthetic rounds; OC unit); §11 #10 (SPRT permitted alternative — recorded, not adopted); C-HE-24 §2 (`no_finding`, `unique_catch`, `disposition_actor`).

**Files.** Create `tools/shadow_trial.py`, `tools/test_shadow_trial.py`. Modify `.claude/skills/ship-pr/SKILL.md` (off-path invocation), `justfile` (`shadow-trial-score`, `shadow-trial-decide`), `tools/lanes_verify.py`, `tools/codex-parity-check.sh`.

**Interfaces.**
```python
N_ROUNDS = 30; KILL_IF_FEWER_THAN = 2
def config_row(*, lens: str, n=N_ROUNDS, threshold=KILL_IF_FEWER_THAN) -> dict          # recorded so amendments are auditable
def scored_rounds(rows, lens) -> set[tuple[str, int]]                                    # DISTINCT (arc_id, round_n) where producer==lens (finding or no_finding)
def unique_catches(rows, lens) -> list[dict]                                             # (a) ∧ (b)
def decide(rows, lens, *, n=N_ROUNDS, threshold=KILL_IF_FEWER_THAN) -> dict              # {"scored": k, "unique": u, "decision": "kill"|"keep"|"pending"}
def p_kill(p: float, n=N_ROUNDS, threshold=KILL_IF_FEWER_THAN) -> float                  # binomial P(X < threshold)
def oc_table() -> list[tuple[float, float]]
def hitl_request(decision) -> str                                                        # DEFERRED-HIL row kind='shadow-trial-adjudicate'
```

**Depends on.** U-HE-01, U-HE-06 (the gemini wrapper is the shadow lens), U-HE-13.

- [ ] **Step 1: Failing tests**
```python
from math import comb
import shadow_trial as st

def _row(round_n, producer, kind="finding", *, location="l", ftype="t", disp=None, actor=None, uc=None, head="h"*40, fid=None):
    return {"finding_id": fid or f"{producer}:{head}:{location}:{round_n}", "location": location, "observed_evidence": "e", "expected_contract": "c",
            "severity": "P2", "finding_type": ftype, "lineage_claim": "fresh", "producer": producer, "record_kind": kind, "ts": f"2026-08-18T00:00:{round_n:02d}Z",
            "arc_id": "pr-1", "lane_id": "L", "head_sha": head, "base_sha": None, "diff_digest": None, "round_n": round_n,
            "cause_attribution": None, "disposition": disp, "disposition_actor": actor, "unique_catch": uc}

def test_oc_table_matches_spec_numbers():
    table = dict(st.oc_table())
    for p, expect in ((0.0, 1.000), (0.05, 0.554), (0.10, 0.184), (0.15, 0.048), (0.20, 0.011), (0.25, 0.002)):
        assert round(table[p], 3) == expect
    # recomputed independently by the test, not read from the module's constants
    assert round(sum(comb(30, k) * 0.10**k * 0.90**(30 - k) for k in range(2)), 3) == 0.184
    assert round(sum(comb(15, k) * 0.10**k * 0.90**(15 - k) for k in range(2)), 2) == 0.55   # the rejected n=15 rule
    assert round(sum(comb(30, k) * 0.10**k * 0.90**(30 - k) for k in range(3)), 2) == 0.41   # the rejected n=30/<3 rule

# mutation-probe: count a unique_catch=true row whose last disposition is rejected
def test_kill_rule_reproducible_from_rows_and_rejected_excluded():
    rows = []
    for r in range(1, 31):
        rows.append(_row(r, "gemini-shadow", kind="no_finding"))                                   # marker rows → scored
    rows.append(_row(5, "gemini-shadow", location="only-shadow", uc=True))
    rows.append(_row(5, "gemini-shadow", kind="finding_adjudication", location="only-shadow", disp="accepted", actor="operator", uc=True))
    rows.append(_row(9, "gemini-shadow", location="also-blocking", uc=True))
    rows.append(_row(9, "merge-gate-concurrency", location="also-blocking"))                       # (a) fails: blocking reviewer saw it
    rows.append(_row(12, "gemini-shadow", location="later-rejected", uc=True))
    rows.append(_row(12, "gemini-shadow", kind="finding_adjudication", location="later-rejected", disp="rejected", actor="operator", uc=True))
    d = st.decide(rows, "gemini-shadow")
    assert d["scored"] == 30 and d["unique"] == 1 and d["decision"] == "kill"
    rows.append(_row(20, "gemini-shadow", location="second", uc=True)); rows.append(_row(20, "gemini-shadow", kind="finding_adjudication", location="second", disp="accepted", actor="operator", uc=True))
    assert st.decide(rows, "gemini-shadow")["decision"] == "keep"


# mutation-probe: count unique catches from ALL rows instead of the first-n sample in decide()
def test_sample_frozen_at_first_n_rounds():
    """30 scored rounds with 1 unique catch → kill. A round-31 unique catch arriving before adjudication must NOT flip it."""
    rows = [_row(r, "gemini-shadow", kind="no_finding") for r in range(1, 31)]
    rows.append(_row(3, "gemini-shadow", location="in-sample", uc=True)); rows.append(_row(3, "gemini-shadow", kind="finding_adjudication", location="in-sample", disp="accepted", actor="operator", uc=True))
    assert st.decide(rows, "gemini-shadow")["decision"] == "kill"
    late = _row(31, "gemini-shadow", location="late", uc=True); late["ts"] = "2026-08-18T00:00:31Z"; rows.append(late)
    adj = _row(31, "gemini-shadow", kind="finding_adjudication", location="late", disp="accepted", actor="operator", uc=True); adj["ts"] = "2026-08-18T00:00:32Z"; rows.append(adj)
    d = st.decide(rows, "gemini-shadow"); assert d["decision"] == "kill" and d["scored"] == 31 and d["unique"] == 1

def test_pending_before_n_and_config_row_recorded():
    rows = [_row(r, "gemini-shadow", kind="no_finding") for r in range(1, 10)]
    assert st.decide(rows, "gemini-shadow")["decision"] == "pending"


# mutation-probe: reduce scored rounds to round_n alone (drop arc_id from the key)
def test_scored_rounds_are_per_arc():
    """Two arcs each with rounds 1..15 = 30 scored rounds; the same rows keyed on round_n alone would count 15."""
    rows = []
    for arc in ("pr-1", "pr-2"):
        for r in range(1, 16):
            row = _row(r, "gemini-shadow", kind="no_finding"); row["arc_id"] = arc; row["finding_id"] = f"gemini-shadow:{arc}:{r}"; rows.append(row)
    assert len(st.scored_rounds(rows, "gemini-shadow")) == 30
    assert st.decide(rows, "gemini-shadow")["decision"] == "kill"      # n reached, 0 unique catches
    c = st.config_row(lens="gemini-shadow")
    assert c["record_kind"] == "no_finding" and "n=30" in c["observed_evidence"] and "kill_if_fewer_than=2" in c["observed_evidence"]

# mutation-probe: hard-code unique_catch=True in adjudicate() (drop the blocking-row check)
def test_adjudicate_computes_unique_catch(tmp_path):
    p = tmp_path / "g.jsonl"
    only = _row(4, "gemini-shadow", location="only-shadow", uc=None); fr.append_row(only, p)
    both = _row(5, "gemini-shadow", location="seen-by-blocking", uc=None); fr.append_row(both, p)
    fr.append_row(_row(5, "merge-gate-concurrency", location="seen-by-blocking"), p)
    a1 = st.adjudicate(only["finding_id"], disposition="accepted", actor="operator", path=p)
    a2 = st.adjudicate(both["finding_id"], disposition="accepted", actor="operator", path=p)
    assert a1["unique_catch"] is True and a2["unique_catch"] is False
    rows = fr.read_rows(p)
    assert [c["finding_id"] for c in st.unique_catches(rows, "gemini-shadow")] == [only["finding_id"]]
    with pytest.raises(ValueError):
        st.adjudicate(only["finding_id"], disposition="accepted", actor="gemini-review", path=p)   # same family


def test_adjudicator_never_placeholder_or_same_family():
    with pytest.raises(ValueError):
        st.validate_adjudicator("TODO")
    with pytest.raises(ValueError):
        st.validate_adjudicator("gemini-review")
    st.validate_adjudicator("operator"); st.validate_adjudicator("codex-review")
```
- [ ] **Step 2: RED**; **Step 3: Write `tools/shadow_trial.py`**
```python
#!/usr/bin/env python3
"""C-HE-29 shadow trial: second-reviewer lens live, OFF the blocking path; value measured from
merge-gate-log.jsonl rows alone. Kill rule pre-committed (n=30 scored rounds; kill if < 2 unique
catches) with its operating characteristics stated. Wall-clock is NOT a kill criterion."""
from __future__ import annotations
import argparse, json, sys
from math import comb
from pathlib import Path
import finding_record as fr

N_ROUNDS = 30
KILL_IF_FEWER_THAN = 2
MODEL_FAMILIES = {"gemini": ("gemini", "agy", "antigravity", "google"), "openai": ("codex", "gpt", "openai"), "anthropic": ("claude", "anthropic")}


def config_row(*, lens: str, n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN) -> dict:
    core = fr.FindingCore(fr.make_finding_id("shadow_trial", "config", lens, 0), lens, f"shadow-trial config: n={n} kill_if_fewer_than={threshold}",
                          "C-HE-29 §3", "info", "config", "policy", "shadow_trial")
    return fr.make_row(core, fr.Envelope("no_finding", fr.now_iso(), "policy", "shadow_trial", None, None, None, None))


def scored_rounds(rows: list[dict], lens: str) -> set[tuple[str, int]]:
    """Round numbers restart per arc: a scored round is identified by (arc_id, round_n), never round_n alone."""
    return {(r["arc_id"], r["round_n"]) for r in rows if r["producer"] == lens and r["record_kind"] in ("finding", "no_finding") and r["round_n"] is not None}


def unique_catches(rows: list[dict], lens: str) -> list[dict]:
    last = fr.reduce_last_by_finding_id(rows)
    blocking = {(r["head_sha"], r["location"], r["finding_type"]) for r in rows if r["producer"] != lens and r["record_kind"] == "finding"}
    out = []
    for fid, r in last.items():
        if r["producer"] != lens or not r.get("unique_catch"):
            continue
        if (r["head_sha"], r["location"], r["finding_type"]) in blocking:
            continue                                   # (a) a blocking reviewer saw it for the same head_sha
        if r.get("disposition") != "accepted":
            continue                                   # (b) last row must be accepted; a later `rejected` MUST NOT count
        out.append(r)
    return out


def first_n_rounds(rows: list[dict], lens: str, n: int) -> set[tuple[str, int]]:
    """The PRE-COMMITTED sample: the first n scored rounds by earliest row ts (deterministic). Rounds scored after the
    n-th never enter the count, so a late catch cannot flip kill→keep (Codex round-2 P2); later ADJUDICATION rows for
    findings inside the sample still count (the reducer takes the last row per finding_id)."""
    first_ts: dict[tuple[str, int], str] = {}
    for r in rows:
        if r["producer"] == lens and r["record_kind"] in ("finding", "no_finding") and r["round_n"] is not None:
            key = (r["arc_id"], r["round_n"]); first_ts[key] = min(first_ts.get(key, r["ts"]), r["ts"])
    return set(sorted(first_ts, key=lambda k: (first_ts[k], k))[:n])


def decide(rows: list[dict], lens: str, *, n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN) -> dict:
    k = len(scored_rounds(rows, lens))
    if k < n:
        return {"scored": k, "unique": len(unique_catches(rows, lens)), "decision": "pending", "n": n, "threshold": threshold}
    sample = first_n_rounds(rows, lens, n)
    u = len([c for c in unique_catches(rows, lens) if (c["arc_id"], c["round_n"]) in sample])
    return {"scored": k, "unique": u, "decision": "kill" if u < threshold else "keep", "n": n, "threshold": threshold, "sample": sorted(sample)}


def p_kill(p: float, n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN) -> float:
    return sum(comb(n, i) * p**i * (1 - p)**(n - i) for i in range(threshold))


def oc_table() -> list[tuple[float, float]]:
    return [(p, p_kill(p)) for p in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25)]


def adjudicate(finding_id: str, *, disposition: str, actor: str, rows: list[dict] | None = None, path: Path | None = None) -> dict:
    """The production writer of `unique_catch` (Codex round-3 P1: nothing else sets it). Appends the adjudication
    row for a shadow-lens finding with unique_catch = (a): its location+finding_type appears in NO blocking
    reviewer's row for the same head_sha; (b) -- disposition accepted -- is what unique_catches() then requires."""
    validate_adjudicator(actor)
    supplied = rows is not None
    rows = rows if supplied else fr.read_rows(path) if path is not None else fr.read_rows()
    orig = next((r for r in rows if r["finding_id"] == finding_id and r["record_kind"] == "finding"), None)
    if orig is None:
        raise ValueError(f"no finding row for {finding_id}")
    blocking = {(r["head_sha"], r["location"], r["finding_type"]) for r in rows if r["producer"] != orig["producer"] and r["record_kind"] == "finding"}
    uc = (orig["head_sha"], orig["location"], orig["finding_type"]) not in blocking
    core = fr.FindingCore(**{k: orig[k] for k in ("finding_id", "location", "observed_evidence", "expected_contract", "severity", "finding_type", "lineage_claim", "producer")})
    env = fr.Envelope("finding_adjudication", fr.now_iso(), orig["arc_id"], orig["lane_id"], orig["head_sha"], orig["base_sha"], orig["diff_digest"], orig["round_n"],
                      cause_attribution=orig["cause_attribution"], disposition=disposition, disposition_actor=actor, unique_catch=uc)
    row = fr.make_row(core, env)
    if not supplied or path is not None:                 # production path (no injected rows) ALWAYS persists (round-5 P1)
        fr.append_row(row, path)
    return row


def validate_adjudicator(actor: str) -> None:
    a = actor.strip().lower()
    if not a or a in ("todo", "tbd", "placeholder", "n/a", "-"):
        raise ValueError("adjudicator must be a specific identity, never a placeholder")
    if any(tok in a for tok in MODEL_FAMILIES["gemini"]) or any(tok in a for tok in MODEL_FAMILIES["anthropic"]):
        raise ValueError("adjudicator must be the operator or a third-party identity of NEITHER model family under trial (gemini shadow vs the Claude-authored diff)")


def hitl_request(decision: dict, lens: str) -> None:
    import reservations as rs
    rs.emit_loop_row("DEFERRED-HIL", "shadow_trial", "shadow-trial-adjudicate:HITL-recoverable:kill_keep_decision",
                     f"SHADOW-{lens} — n={decision['scored']} unique={decision['unique']} threshold={decision['threshold']} → proposed {decision['decision'].upper()}; respond approve-kill | reject-keep | amend-threshold")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("decide"); s.add_argument("--lens", required=True); s.add_argument("--hitl", action="store_true")
    sub.add_parser("oc")
    c = sub.add_parser("config"); c.add_argument("--lens", required=True)
    ad = sub.add_parser("adjudicate"); ad.add_argument("finding_id"); ad.add_argument("--disposition", choices=("accepted", "rejected", "suppressed"), required=True); ad.add_argument("--actor", required=True)
    a = p.parse_args(argv)
    if a.cmd == "adjudicate":
        print(json.dumps(adjudicate(a.finding_id, disposition=a.disposition, actor=a.actor))); return 0
    if a.cmd == "oc":
        for pv, pk in oc_table():
            print(f"p={pv:.2f}  P(kill)={pk:.3f}")
        return 0
    if a.cmd == "config":
        fr.append_row(config_row(lens=a.lens)); return 0
    d = decide(fr.read_rows(), a.lens); print(json.dumps(d))
    if a.hitl and d["decision"] != "pending":
        hitl_request(d, a.lens)
    return 0
```
`ship-pr/SKILL.md` — after the blocking chain, off-path: `HARNESS_ROUND_N=<n> HARNESS_SHADOW_LENS=1 just gemini-review || true`; the operator (or a third-party identity of neither family) disposes each shadow finding with `just shadow-trial-adjudicate <finding_id> accepted|rejected --actor <id>` — the ONLY writer of `unique_catch`; (the wrapper, under `HARNESS_SHADOW_LENS=1`, emits rows with `producer=gemini-shadow` and one `no_finding` marker when clean — add this branch to U-HE-06's `_emit`) then `uv run python tools/shadow_trial.py decide --lens gemini-shadow --hitl`. Recipes `shadow-trial-score` / `shadow-trial-decide` wrap the two.
- [ ] **Step 4: GREEN**, probe (`--lines` = the `(b)` disposition guard) → PINNED. Register `Row("C-HE-29", "pytest:tools/test_shadow_trial.py::test_kill_rule_reproducible_from_rows_and_rejected_excluded", "measurement", "local + CI", True)` and `::test_oc_table_matches_spec_numbers`. Commit `feat(he-lanes): U-HE-43 shadow trial live off-path — reducer, kill rule n=30/<2 with OC, HITL delivery (C-HE-29)`.

---

# Close-out

### U-HE-44: Forward-register rows for spec §11 + plan evidence log

**Scope.** Register the six carry-forwards the spec sends to the forward register as `B-*` rows (`.harness/forward-register.yaml` via `tools/forward_register.py`): #3 P9(a) prewritten testable done-condition; #4 K7 stop rule (prereqs C-HE-26 §3); #9 cross-carrier merge-door fencing (joint Claude/Codex arc; incl. `AGENTS.md:56-57` #3 restatement + Docker isolation for Codex legs; **operator may reverse the v1 scoping**); #10 SPRT alternative for the shadow-trial rule; #11 cross-`head_sha` `finding_id` tracking; #12 randomized lane assignment; plus the plan-level rows: the `result_capture` divergence audit-worthiness question (§11 #5, decided "recorded, not audit-worthy in v1"), the reservation bootstrap-at-drain migration path (U-HE-19), the `severity` dual vocabulary note (U-HE-01), §11 #13 (queue-depth cap once N-lane cadence data exists), the C-HE-06 §7 invariant-wording vs G4-continuation tension (plan §6 item 13; v1.1 change-note candidate), and the six residual defect classes from the plan's codex rounds (§7 item 8) — one row each, tagged as U-HE-40 mechanized-check candidates. Create `.harness/plan/evidence-log-he-loop-lanes.md` with the sections the units above append to (branch-protection show/apply/tiebreaker; reviewer-concurrency probe verdicts; pilot reports; RED-first runs of AC#2; equivalence proofs).

**Spec linkage.** §11 #3, #4, #5, #9, #10, #11, #12, #13; C-HE-30 (evidence log is not a store — a plan artifact).

**Files.** Modify `.harness/forward-register.yaml` (via the tool); create `.harness/plan/evidence-log-he-loop-lanes.md`.

**Depends on.** (none for the rows; the log is appended by U-HE-20, 27, 35, 37, 41).

- [ ] **Step 1:** `uv run python tools/forward_register.py add …` one row each (title, source `Spec_HE_Loop_Lanes_v1 §11 #N`, disposition per the spec column, `operator_may_reverse: true` on #9). Parse-check the YAML (`uv run --with pyyaml python -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" .harness/forward-register.yaml`). `just forward-register-check` green.
- [ ] **Step 2:** Evidence log skeleton (headings only + the RED-first table with columns *unit / test / unfixed commit / RED output line / GREEN commit*).
- [ ] **Step 3:** Commit `ops(he-lanes): U-HE-44 forward-register rows for spec §11 + plan evidence log`.

### U-HE-45: Roadmap wiring — register the S1–S8 arcs and the pilot bar

**Scope.** Add the plan's arcs to the roadmap's forward tracking so `/roadmap-continue` derives them: one `B-*` row per S-step (S1, S2, S3, S4a, S4b, S4c, S4d, S5, S6, S7, S8) with `depends_on` per §6, unit ranges from this plan, and the two gate columns; the pilot bar row (`≥ 3 pilots at 3–4 lanes`) marked as gating follow-on orchestration only.

**Spec linkage.** §6 (order + gates), C-HE-13 §3; CLAUDE.md §12 (roadmap protocol; refresh via `tools/roadmap_status_refresh.py`).

**Files.** Modify `.harness/forward-register.yaml` (rows), `.harness/roadmap_status.md` (next-action paragraph pointing at S1 = U-HE-01), via the idempotent scripts.

**Depends on.** U-HE-44.

- [ ] **Step 1–2:** add rows; run `uv run python tools/roadmap_status_refresh.py --next-action "<one paragraph: 'Execute Implementation_Plan_HE_Loop_Lanes_v1 §2 in topological order starting at U-HE-01 (finding record) — S1/S2 roots; Phase 0 = U-HE-01..33 gates N ≥ 2.'>"` as part of the doc-only PR's terminating refresh (CLAUDE.md §12.2.1).
- [ ] **Step 3:** Commit `ops: roadmap status refresh post-#<PR>` (the terminating refresh, roadmap_status.md only) — after the doc-only PR that carries the spec + ADRs + council ledger + this plan merges.

---
## §3 Dependency graph

Direct dependencies only (transitive closure computed by the sort). All 45 v1.0 units; the graph is acyclic. (The §8 rev units add no cycle: U-HE-46…49 and U-SR-01…09 are roots; U-HE-50 and U-HE-51 depend only on U-HE-46…49 — see §8.6.)

| Unit | Depends on | Unit | Depends on |
|---|---|---|---|
| U-HE-01 | (none) | U-HE-24 | (none) |
| U-HE-02 | U-HE-01, U-HE-03 | U-HE-25 | U-HE-23 |
| U-HE-03 | (none) | U-HE-26 | (none) |
| U-HE-04 | U-HE-01, U-HE-02, U-HE-03 | U-HE-27 | U-HE-05, U-HE-25 |
| U-HE-05 | (none) | U-HE-28 | U-HE-23, U-HE-25 |
| U-HE-06 | U-HE-02, U-HE-03 | U-HE-29 | (none) |
| U-HE-07 | U-HE-04, U-HE-06 | U-HE-30 | U-HE-29 |
| U-HE-08 | (none) | U-HE-31 | U-HE-29 |
| U-HE-09 | (none) | U-HE-32 | U-HE-29 |
| U-HE-10 | (none) | U-HE-33 | U-HE-01, U-HE-11, U-HE-17, U-HE-22 |
| U-HE-11 | U-HE-10 | U-HE-34 | U-HE-01, U-HE-19 |
| U-HE-12 | U-HE-11 | U-HE-35 | U-HE-01, U-HE-04, U-HE-06 |
| U-HE-13 | U-HE-01 | U-HE-36 | U-HE-17, U-HE-21 |
| U-HE-14 | U-HE-01, U-HE-05 | U-HE-37 | U-HE-05, U-HE-30, U-HE-33, U-HE-35, U-HE-36 |
| U-HE-15 | U-HE-10 | U-HE-38 | U-HE-11, U-HE-12, U-HE-33 |
| U-HE-16 | U-HE-15 | U-HE-39 | U-HE-07, U-HE-28, U-HE-37 |
| U-HE-17 | U-HE-14, U-HE-15 | U-HE-40 | U-HE-01, U-HE-05, U-HE-13, U-HE-34 |
| U-HE-18 | U-HE-17, U-HE-29 | U-HE-41 | U-HE-01, U-HE-40 |
| U-HE-19 | U-HE-15, U-HE-17, U-HE-18 | U-HE-42 | U-HE-33 |
| U-HE-20 | U-HE-16, U-HE-19 | U-HE-43 | U-HE-01, U-HE-06, U-HE-13 |
| U-HE-21 | U-HE-17, U-HE-18 | U-HE-44 | (none) |
| U-HE-22 | U-HE-14, U-HE-17 | U-HE-45 | U-HE-44 |
| U-HE-23 | U-HE-01, U-HE-08, U-HE-18, U-HE-22, U-HE-29 | | |

**Topological order (the execution order).**
`01 → 03 → 02 → 05 → 04 → 06 → 07 → 08 → 09 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 29 → 17 → 18 → 19 → 20 → 21 → 22 → 23 → 24 → 25 → 26 → 27 → 28 → 30 → 31 → 32 → 33 → 34 → 35 → 36 → 37 → 38 → 39 → 40 → 41 → 42 → 43 → 44 → 45`

Mapped onto spec §6 (S-steps are the PR clusters; merges stay serial through the door once U-HE-28 lands, and through the existing `ship-pr` before that):

```
S2-root  U-01 ─┐                                       S1  U-02..04, U-06..09 (live at N=1)
S1       U-03 ─┼─► U-02 ─► U-04 ─► U-06 ─► U-07         S2  U-05, U-10..13
S2       U-05 ─┤                                        S3  U-14
S2       U-10 ─┼─► U-11 ─► U-12                          S4a U-15, U-16
S2       U-13 ─┤                                        S4d U-29 (before S4b — shape decision 2)
S3       U-14 ─┼─► U-17 ─► U-18 ─► U-19 ─► U-20          S4b U-17..21
S4a      U-15 ─┼─► U-16          └► U-21                S4c U-22..28 (U-24 any time before N≥2)
S4d      U-29 ─┼─► U-30, U-31, U-32                      S4d U-30..33
             └─► U-22 ─► U-23 ─► U-25 ─► U-27, U-28      S5  U-34
                          └► U-33                       S6  U-35..39 (gate: lanes-phase0-check GREEN)
S5       U-34 ◄─ U-19                                    S7  U-40..42
S6       U-35, U-36 ─► U-37 ─► U-39 ;  U-38              S8  U-43
S7       U-40 ─► U-41 ; U-42                            close U-44, U-45
S8       U-43
```

**Cross-axis note.** Every unit is H_E dev tooling (`tools/`, `tools/hooks/`, `.claude/skills/`, `justfile`, `.github/workflows/`, `.harness/`). No `harness-*/src` or `design-substrate/` surface is touched; the CP-AL-1 boundary is untouched (lanes are worktrees, not `TopologyPattern`).

## §4 Coverage matrix

Every contract row has ≥ 1 unit; every unit appears in ≥ 1 row (checked at U-HE-39 for the doc-only contracts and at U-HE-44/45 for the cross-cutting sections).

| Contract | Units realising it (§ cited in the unit) |
|---|---|
| C-HE-01 | U-HE-39 (§1–§4, Verification); U-HE-28 (Invariants: refresh shape) |
| C-HE-02 | U-HE-16 (§1, §6, Invariants, Verification); U-HE-17 (§1–§2 CAS family); U-HE-22 (§1–§3) |
| C-HE-03 | U-HE-17 (§1–§4, §6, §8); U-HE-18 (§5, §7); U-HE-19 (§4, §6); U-HE-20 (Verification AC#2 a/b); U-HE-21 (§3–§4) |
| C-HE-04 | U-HE-15 (§1, §3, §4, §6 teardown guard, §7); U-HE-16 (Verification vi seam); U-HE-19 (§2, §4, §5); U-HE-20 (Verification) |
| C-HE-05 | U-HE-10 (§1–§3); U-HE-20 (§2 consumer) |
| C-HE-06 | U-HE-22 (§2, §3, §6, §7); U-HE-23 (§1, §4, §5, §8, §9, §10); U-HE-24 (§4 CI concurrency); U-HE-28 (§1, §4(viii), §6 unblock) |
| C-HE-07 | U-HE-25 (§1–§4, Invariants) |
| C-HE-08 | U-HE-26 (§1); U-HE-27 (§2–§5) |
| C-HE-09 | U-HE-29 (§1–§6) |
| C-HE-10 | U-HE-30 (§1–§4) |
| C-HE-11 | U-HE-31 (§1, §2, §4, §5); U-HE-32 (§3) |
| C-HE-12 | U-HE-33 (§1–§3); U-HE-23 (`BASE_TOCTOU` emission at landing) |
| C-HE-13 | U-HE-37 (§1–§3); U-HE-36 (§4–§5); U-HE-05 (§1 gate mechanism); U-HE-39 (§5 doc) |
| C-HE-14 | U-HE-39 (table carried in `two-lane`) |
| C-HE-15 | U-HE-02 (§1–§4); U-HE-03 (§4 schemas); U-HE-04 (§4 fenced block); U-HE-06 (gemini) |
| C-HE-16 | U-HE-02 (§1–§4); U-HE-06 (§3 retry in agy) |
| C-HE-17 | U-HE-07 (§1–§3, §5); U-HE-06 (§4) |
| C-HE-18 | U-HE-04 (§1–§3) |
| C-HE-19 | U-HE-08 (§1–§3); U-HE-23 (§2 door consumer) |
| C-HE-20 | U-HE-09 (§2 TTL); U-HE-18 (§1 reservation → HITL) |
| C-HE-21 | U-HE-39 (§1–§4); U-HE-02 (§2 #5 live-carried) |
| C-HE-22 | U-HE-35 |
| C-HE-23 | U-HE-13 (§2); U-HE-01 (§1, §3); U-HE-14 (Invariants store count) |
| C-HE-24 | U-HE-01 (§1–§6) |
| C-HE-25 | U-HE-11 |
| C-HE-26 | U-HE-12 (§2); U-HE-17 + U-HE-21 (§1); U-HE-38 (§3 EVALUATE gate) |
| C-HE-27 | U-HE-34 (§1–§4); U-HE-17 (`record_phase`); U-HE-19 (fold at drain) |
| C-HE-28 | U-HE-38 (§1–§3); U-HE-51 (§4 lens trial); **§4 outcome-measure cohorts UNOWNED — B-245** (rev 2026-09-15: moved off U-HE-42 when the Class 1 fork on C-HE-33 §4 ratified under Reading C; needs a branch-level CI run count + per-run conclusion on the C-HE-25 row before any unit can own it) |
| C-HE-29 | U-HE-43 (§1–§5) |
| C-HE-30 | U-HE-14 |
| C-HE-31 | U-HE-40 (§1–§5) |
| C-HE-32 | U-HE-41 |
| C-HE-33 | U-HE-42 |
| C-HE-34 | U-HE-39 |
| C-HE-35 | U-HE-39 (table); U-HE-40 (K8(c) siting) |
| §8.1 manifest + §0.3 probes | U-HE-05 (+ every unit's registration step) |
| §11 open items | U-HE-44 |
| §6 order / §12.2.1 | U-HE-45; U-HE-28 |

## §5 Verification manifest — final row set (what `just lanes-verify` runs after all units land)

Rows are registered by the unit that lands each artifact (each unit's steps name the exact `Row(...)`). Summary by tag:

| Tag | Rows (artifact → contract) |
|---|---|
| **phase0** (`lanes-phase0-check`; skip = RED) | `test_finding_record.py` (C-HE-24) · `test_review_wrapper.py` (C-HE-15/16/18) + `::test_failover_*` (C-HE-17) · `test_lanes_verify.py` + `just mutation-probe-coverage-check` (§8.1/§0.3) · `test_arc_metrics.py::test_ci_state_cancelled_incomplete` (C-HE-19) · `test_loop_lib.sh` (C-HE-09/10/20) · `test_arc_metrics.py::test_env_overrides` (C-HE-05) · `::test_arc_row_schema_has_c_he_25_fields`, `::test_arc_type_at_open` (C-HE-25/26) · `just merge-gate-log-check` (C-HE-23) · `test_store_audit.py` (C-HE-30) · `::test_drain_fault_isolation`, `::test_e9_capture_republish`, `::test_append_refuses_unless_holder` (C-HE-04) · `::test_takeover_token_compare`, `::test_no_flock_fcntl_in_coordination_modules` (C-HE-02) · `test_reservations.py` (C-HE-03) + `::test_ttl_never_reclaims` (C-HE-20) · `test_arc_metrics_lanes.py::test_ac2_a_same_instant`, `::test_ac2_b_cross_latency` (C-HE-03/04; no skip) · `test_skill_reservation_wiring.sh` (C-HE-03/06) · `test_merge_door.py::test_lease_holder_invariant`, `::test_contention_fail_fast`, `::test_marker_race_exactly_one_wins`, `::test_rate_limit_sixth_refused`, `::test_ac2_c_crash_resume`, `::test_timeout_reconcile_merged_calls_once`, `::test_continuation_no_reacquire`, `::test_post_merge_ci_blocked_and_unblock`, `::test_inflight_first_attempt_then_reissue` (C-HE-06) · `test_permission_guard.sh` (C-HE-07/08) · `just main-protection-verify` (C-HE-08; `gh-auth-absent` → RED here) · `test_lane_init.sh`, `test_compose_lanes.py::test_lane_port_formula` (C-HE-11) · `test_codex_context_guard.py::test_split_brain_ledger_duplicate_arc_id`, `::test_base_toctou` (C-HE-12) · `test_skill_lanes_docs.sh` (C-HE-01/14/21/34/35) |
| **phase1** | `test_arc_disjoint_check.py`, `test_lanes_pilot_gate.py`, `just lanes-pilot-report <run-id>` (C-HE-13) · `reviewer_concurrency_probe.py` live (C-HE-22; `provider-login-absent`) |
| **measurement** | `::test_phase_spans_no_deltas`, `::test_n6_formula` (C-HE-27) · `::test_cohort_by_concurrent_lanes_at_open_and_arc_type` (C-HE-28) · `test_shadow_trial.py::test_kill_rule_*`, `::test_oc_table_*` (C-HE-29) |
| **layer2** | `test_mechanized_checks.py` (+ `::test_promotion_demotion_state_machine`) (C-HE-31) · `test_finding_record.py::test_equivalence_proof_rows` (C-HE-32) · `test_codex_context_guard.py::test_local_ci_parity` (C-HE-33) |
| **env** | `test_compose_lanes.py::test_two_lanes_disjoint_names_and_ports` (`docker-daemon-absent`) |
| **operator-gated live** | `main-protection-tiebreaker` + `apply` (C-HE-08; evidence log) |

Every row marked **mutation-probe** in §8.1 has a named `--lines` target in its unit; `just mutation-probe-coverage-check` fails until each is PINNED and logged.

## §6 Open items (plan-shape observations; none blocks execution)

| # | Item | Where decided / carried |
|---|---|---|
| 1 | `severity` on the finding record accepts both the reviewer triple `P1|P2|P3` (C-HE-15 §4) and the projection triple `hard|warn|info` (C-HE-31 §4(c) writes `warn`). If a single vocabulary is preferred, that is a v1.1 spec note. | U-HE-01; flag at plan review |
| 2 | The fail-class vocabulary (`transient-retry`, `permanent-fail-exit`, `HITL-recoverable`, `terminal-…`) is carried in `finding_type`; `code` = `<producer>:<finding_type>:<cause_attribution>`. Reading of C-HE-24 §3 + C-HE-16 §3 stated explicitly. | U-HE-01 |
| 3 | Legacy queue entries (queued before reservations exist) get a reservation **bootstrapped at drain** (`arc_type_declared_at="close"`, `NOTIFY` emitted) — a one-time migration path, not a standing mechanism; remove after the queue drains once post-S4b. | U-HE-19; forward-register row (U-HE-44) |
| 4 | `arc_metrics` ↔ `reservations` import cycle is broken by importing `reservations` inside the functions that need it. | U-HE-19 |
| 5 | `loop_hil_groups` uses `mktime/strftime/asorti` (gawk); macOS ships BSD awk — the executor MUST vendor epoch math in bash (both platforms run the hook tests). | U-HE-30 |
| 6 | HITL rows for reservation escalations key on `arc_id` (`pr-N`, `B-…`), so `pr-N` items render in the summary and coalescing but do not enter `loop_skip_set()` (which filters `R-/B-`). Acceptable for v1; if skip-set membership is wanted, key escalations on the roadmap unit id. | U-HE-18; observation |
| 7 | §11 #5 decided in S5: a `result_capture` process-exit vs log-write divergence is **recorded (both timestamps on the row), not audit-worthy in v1** — no finding on divergence. | U-HE-34; forward-register row |
| 8 | Council-dependent contracts (C-HE-07; C-HE-06 §7 + §4-timeout; C-HE-09 §1) are built as normative. If a v1.1 change-note strikes one, the affected units are U-HE-25 (whole), U-HE-22 `HolderInvariant` + U-HE-23 `MERGE_TIMEOUT_S`, U-HE-29 §1 (single file) — struck by change-note; nothing else depends on them except as §6 notes. | spec §13 |
| 9 | Branch protection is a **two-step**: `apply` after U-HE-27, then re-`verify`/re-`apply` after U-HE-33 adds the `split-brain … — blocking` job (the context list is re-derived from `ci.yml`). | U-HE-27, U-HE-33; evidence log |
| 10 | The single operator gate in this plan is `just main-protection-apply` (outward-facing GitHub settings change) — one AskUserQuestion with the diff shown. Everything else is Claude-driven to its genuine gate (CLAUDE.md §12.4.1). The reviewer-concurrency probe and pilots are live but subscription-auth ($0 metered) and pre-authorized. | U-HE-27 |
| 11 | §11 #13 (queue-depth cap once N-lane cadence data exists) and #10 (SPRT alternative) are registered, not built. | U-HE-44 |
| 12 | The reservation `pending`-aged and `open`-stuck HITL rows use `HITL-recoverable` in the cause triple; the spec names the escalation but not a fail-class — this follows C-HE-06 §9's convention for human-actionable waits. | U-HE-18 |
| 13 | **Spec-internal tension (surfaced by Codex round 2, not absorbed):** C-HE-06 §7's Invariants bullet "no lease exists whose reservation is not `open`" cannot hold literally across the G4 continuation — C-HE-03 §4 flips the reservation to `merged` on confirmed merge while C-HE-06 §4(vii)–(viii) holds the lease through post-merge CI + the refresh. The plan implements §7 as its *text* says (acquisition-time verification; `acquire()` refuses a non-open reservation) and treats the continuation window as the G4-authorized state. Owed: a v1.1 change-note re-wording the invariant to "no lease is *acquired* against a non-open reservation; a held lease outlives its reservation's `merged` transition only inside the §4(vii)–(ix) continuation". Registered as a forward-register row by U-HE-44. | U-HE-22/23; spec §14 |

## §7 Self-review (writing-plans checklist, run after assembly)

1. **Spec coverage.** Every `C-HE-01`…`C-HE-35` row in §4 has ≥ 1 unit with section-level cites; §5 (files table) is honored file-for-file — the only additions beyond spec §5 are helper modules the listed files import (`tools/lane_ports.py`, `tools/lanes_pilot.py`, `tools/main_protection.py`, `tools/merge_gate_log.py`, `tools/lanes_verify.py`, `tools/review_wrapper_common.py` is in §5), each named in its unit; §6 order is followed with the two stated refinements; §8.1 rows all appear in §5 above; §11 items are carried by U-HE-44. Gaps found and closed during self-review: (a) C-HE-04 §6 (teardown guard) had no unit of its own — it lands with U-HE-15 Step 4b (`hook_worktree_local_state` gains the `rev-list @{u}..HEAD` refusal + its mutation-probe) and the coverage row says so; (b) C-HE-06 §10 attestation tiering — folded into U-HE-23 (`_tiering_active` reads, `land()` writes one file per clean cycle under `DOOR/tier-clean-cycles`; listed in the U-HE-14 store audit's derived families).
2. **Placeholder scan.** No `TBD`, `TODO`, "implement later", "fill in", "add appropriate error handling", "similar to Task N" in any step. Where a step says "the executor reads the exact line numbers from the file they just wrote", it is because `--lines` for a mutation probe cannot be known until the code exists — the guard being reverted is named in every case.
3. **Type consistency (names used across units).** `fr.FindingCore / fr.Envelope / fr.make_row / fr.append_row / fr.read_rows / fr.reduce_last_by_finding_id / fr.make_finding_id / fr.now_iso` (U-01) are used unchanged by U-02, 04, 06, 07, 13, 23, 33, 34, 40, 41, 43. `rw.Attempt / rw.ReviewOutcome / rw.parse_verdict / rw.run_with_retry / rw.run_with_failover / rw.classify / rw.compute_binding / rw.exit_code / rw.outcome_rows / rw.env_arc_and_lane` (U-02) are used unchanged by U-04, 06, 07, 35. `rs.reserve / rs.transition / rs.update_payload / rs.transfer_holder / rs.record_phase / rs.holder / rs.selectable / rs.current / rs.open_with_sensor / rs.reconcile / rs.emit_loop_row / rs.mint_lane_id` (U-17/18) are used unchanged by U-19, 20, 21, 22, 23, 33, 36, 40, 43. `am.ci_is_green / am.CI_TERMINAL` (U-08) → U-23. `am.LANE_ID / am._kill_after / am._hold_after / am._restore_or_republish / am._drain_one / am._reconcile_local_rows` (U-15/19/20). `lv.Row / lv.Result / lv.MANIFEST / lv.run_row / lv.phase0_rows / lv.phase0_verdict / lv.coverage_gaps` (U-05) → U-37. `md.acquire / md.release / md.reclaim / md.unblock / md.win_marker / md.mark_attempted / md.mark_blocked / md.read_lease / md.land / md.wait_for_door / md.Ground` (U-22/23) → U-25, 28, 33. `loop_log_structured / loop_status_path / loop_hil_groups / loop_hil_deliver / loop_notify_summary / _LOOP_AWK_ROW` (U-29/30) → U-09 (upgraded shape), U-18, U-31, U-32. One fix applied during review: U-HE-09 (S1) emits `NOTIFY` before U-HE-29 defines the structured kind — its row is written via legacy `loop_log` and the U-HE-29 reducer treats it as a legacy row (stated in U-HE-09).

4. **Codex round 1 on PR #1393 (out-of-family, cold) — 7 P1 / 4 P2, all absorbed in this PR.** P1: (a) marker/plan exit-gate mismatch → status block + marker now name this review as the plan's exit gate; (b) `round_outcomes` never written → `reservations.record_round_outcome` + `rw.record_round_outcome_if_reserved` called by both wrappers + folded at drain (U-HE-17/02/04/06/19); (c) `transition(updates)`/`update_payload` could rewrite `lane_id`/`arc_type`/`superseded_by` → `PAYLOAD_MUTABLE`/`TRANSITION_MUTABLE` allowlists + test (U-HE-17; U-HE-18's aging test now uses `now=`); (d) `BASE_TOCTOU` mismatch emitted but did not block → door blocks + HITL + test (U-HE-23); (e) resume re-created the refresh PR → resume from the recorded sidecar + test (U-HE-23); (f) coalescing delivery race / later-generation suppression → exclusive-create claim per exact generation under `QUEUE_DIR/hil-deliveries/` + concurrency test (U-HE-30; store audit lists the family); (g) shadow scored rounds collapsed across arcs → `(arc_id, round_n)` + test (U-HE-43). P2: artifact polling capped by the shared 1260 s deadline + test (U-HE-04); adjudication append enforces the same-core invariant against the ORIGINAL row + test (U-HE-01); reservation GC retention runs from the terminal head's `transitioned_at` + test (U-HE-17); HIL groups sorted by epoch, gawk-free pipeline (U-HE-30). Round 2 result is recorded in the PR.

5. **Codex round 2 on PR #1393 — 8 P1 / 3 P2, all new surfaces (no round-1 item re-flagged), all absorbed.** P1: session-artifact JSONL is decoded before verdict parsing (`artifact_text`, U-HE-04); `review-with-failover` no longer pre-gates on `_require-codex-subscription` (a permanent Codex failure must reach the failover, U-HE-07); same-lane reclaim still requires the old pid dead (U-HE-22); a crashed reclaimer's marker carries the fresh lease and `complete_dead_marker` publishes it (U-HE-22); the C-HE-06 §7 invariant-vs-G4 continuation tension is surfaced as open item 13 (acquisition-time enforcement; v1.1 change-note candidate; U-HE-22/23); the `DoorFailed` handler decides release-vs-block from `read_lease()` and blocks after an attempt (U-HE-23); push-to-main is parsed (options anywhere, every refspec, bare push) instead of the reference regex (U-HE-26); the tiebreaker verdict comes from the stale PR alone (U-HE-27). P2: failover path emits no duplicate gemini rows (U-HE-07); TTL resurface reducer is shape-aware for HIL rows (U-HE-09); the shadow-trial sample is frozen at the first n scored rounds (U-HE-43). Round 3 result recorded in the PR.
6. **Codex round 3 on PR #1393 — 11 P1 / 4 P2, all absorbed (three were consequences of round-2 edits — the tiebreaker's tautological first-parent compare, the reclaim publish window, and the status block's "round 2 clean" wording, which was false the moment round 2 returned findings and is now replaced by this record).** P1: holder-only terminal transitions (U-HE-17); reclaim verifies the published lease is its own token, never adopts a foreign lease (U-HE-22); tiebreaker runs in a throwaway worktree, compares the stale landing with the pre-merge main SHA, and `apply` is provisional with automatic rollback on FAIL (U-HE-27); `unique_catch` gets a production writer — `shadow_trial.py adjudicate` (U-HE-43); probe coverage matches every annotation's exact node id (U-HE-05); `unrun_cli` never shells out (U-HE-40); pilot report joins `BASE_TOCTOU` by the landing `merge_sha` now persisted on the reservation, and scopes HIL rows to the pilot's lanes + window with last-write-wins (U-HE-37/23); status block no longer asserts a clean round in advance (§ status). P2: envelope fields immutable on adjudication (U-HE-01); `BASE_TOCTOU` backstop walks first-parent commits (U-HE-33); failover emits when the gemini recipe died at preflight (U-HE-07); `loop_log_structured` returns 1 on write failure and `emit_loop_row` raises `LoopStatusWriteError` (U-HE-29/17).
7. **Codex round 4 on PR #1393 — 7 P1 / 1 P2, all absorbed.** P1: the AC#2(c) crash-resume test body is real code (a `gh`/`git` shim on PATH, four parametrized kill points, rc 137 → resume rc 0, `merge-calls.log` == 1) instead of a `...` stand-in (U-HE-23); `emit_refresh_pr` is idempotent by branch name so a crash between PR creation and the sidecar publish resumes the existing PR (U-HE-28); refresh-CI failure emits the gate finding + `DEFERRED-HIL` before blocking (U-HE-23); `_push_targets_main` strips quotes so `'HEAD:main'` is fenced (U-HE-26); `main-protection-apply` is a dry run (exit 3) and `apply-confirm` is the mutation, so the operator approves the printed payload (U-HE-27); rollback restores a normalized PUT payload and verifies it, else fails loud "main UNPROTECTED" (U-HE-27); this record itself (the exit gate's terminal item was owed before merge). P2: the lease rate counter ignores `.tmp` remnants and sweeps them (U-HE-22). **Terminal round: item 8.**
8. **Codex round 5 on PR #1393 (terminal) — 7 P1 / 10 P2, all absorbed; stopping rule applied.** P1: rollback preserves user/team/app `restrictions` and the PUT payload carries the required nullable `restrictions` key (U-HE-27); `shadow_trial.adjudicate` persists on the production path (U-HE-43); commit-before-probe is now a plan-wide convention (the probe tool refuses untracked/dirty targets) and U-HE-01's steps are reordered accordingly; `append_row`/`read_rows` resolve the ledger path at call time so tests never write the real ledger (U-HE-01, every emitter); `lanes_verify` tokenizes `just` args and treats placeholder rows as live, and probe coverage recognises `bash <script>` probes (U-HE-05). P2: door GC skips `.tmp` attempt remnants (U-HE-22); the stack recipe calls `lane_stack_allowed` (U-HE-31); the two-lane compose test is a real body (U-HE-31); `unswept_consumers` diffs the working tree and no longer excludes whole changed files (U-HE-40); `unrun_cli` reads the PR body and warns when it cannot (U-HE-40); the deliverer clamps the window like the grouper, legacy rows are singleton groups, `loop_notify_summary` enforces its 24 h horizon (U-HE-29/30); lane-init fails on index exhaustion (U-HE-31). **Verdict + stopping rule.** P1 yield across rounds was 7 / 8 / 11 / 7 / 7 — flat, not converging: a cold out-of-family reviewer finds real defects in ~7 k lines of *invented, not-yet-executed* code at a steady rate, and each round's fixes open a few new surfaces (rounds 3–5 each contained 2–3 findings against round-(n−1) edits). Continuing to review the plan text is therefore not the cheapest path to correctness; **executing** it is — every unit lands RED-first with its own tests, then `just codex-review` on the PR diff, then the 3-lens `merge-gate` (code PRs). **Residual classes** (what the five rounds kept finding, and what unit-execution review must watch for): (a) crash-window idempotency in multi-step filesystem protocols (marker → move → publish; sidecar → PR create); (b) test bodies that are stand-ins (`...`) or that pass vacuously; (c) payload-shape mismatches against a live API (GET vs PUT shapes; required nullable fields); (d) string-parsing edge cases in the permission guard (quotes, options anywhere); (e) default-argument capture vs call-time resolution; (f) invariant statements that hold at one time (acquisition) but not across a window (continuation). These six are registered by U-HE-44 as forward-register rows *and* as candidate classes for U-HE-40's mechanized checks (each is deterministic or hybrid-checkable). No finding from rounds 1–5 remains unaddressed in the plan text.
## §8 Workflow-repair program (rev 2026-08-26; spec v1.6 X6a–X6f + `Workflow_Repair_Charter_v1`)

Decomposes the U-HE-35 workflow-repair program into atomic units. Two source authorities, one per bucket: bucket-1 units cite the spec v1.6 X6-tagged clauses (contract surfaces the HE spec owns); bucket-2 units cite `.harness/spec/Workflow_Repair_Charter_v1.md` WR-ids (skill bodies, hooks, and eval cases the spec deliberately does not own — the `U-SR-*` family is minted for them so a `U-HE-*` id never implies a `C-HE-*` cite that does not exist). Evidence ids `[A]`/`[B]` are the two audits named in the charter §"Sources"; every number below is theirs.

### §8.1 Sequencing — instruments before levers (normative for these units)

The self-improvement circuit is broken at both ends: nothing flows in (0 skill repairs across 10 absorption commits [A §4]) and the measurement out is corrupted ([B] F15 round miscount, F16 null attribution). A lever landed before the instruments would be unmeasurable — so the rounds are strictly ordered:

- **R0 — instruments:** U-HE-46, U-HE-47, U-HE-48, U-HE-49. Fix what measures before touching what is measured.
- **R1 — skill/governance levers:** U-HE-50, U-SR-01…U-SR-08.
- **R2 — mechanical sweep:** U-SR-09.
- **R3 — eval arc:** U-HE-51 riding the next code arc (decision #2 RATIFIED 2026-08-26: U-HE-36 is the R3 eval arc — §8.5), judged against the [B] baseline: 13 rounds (10 codex + 3 gate), ≈25.6M IET grand total (21.0M main / 4.64M subagents — the u-he-48 execution-time correction ratified at the U-HE-48 acceptance bullet: [B]'s printed 4.52M/25.5M were first-copy-read undercounts), ≈2h agent-active in 5h19m foreground, 29 PR commits (13 pins), 418 main API calls, spec lens 0 findings per 1.34M IET, 29 wasted rtk calls, 12 re-pins, 3 attest-by-trial failures.

Units within a round are mutually independent and may land in any order or together. The instruments-before-levers rule binds as dependency edges wherever a lever touches a measured surface: U-HE-50 (R1, a lever on the C-HE-27 surface R0 repairs) and U-HE-51 (R3) both hard-depend on all four R0 units. The U-SR-* levers touch no instrument surface and may land alongside R0.

### §8.2 Item-coverage table (audit id → unit or disposition)

Every recommendation id from [A]/[B], so nothing silently drops:

| Audit id | Unit / disposition |
|---|---|
| a1 (spans from wrapper) | U-HE-50 |
| a2 (labels before answers) | U-SR-04 |
| a3 (relaunch guard) | U-HE-49 |
| a4 (bindings by file) | U-SR-03 |
| a5 (precedent-search rule) | U-SR-04 |
| b1 (pin scope) | U-SR-09 |
| b2 (round derivation) | U-HE-46 |
| b3 (attribution) | U-HE-47 |
| b4 (rtk shapes) | U-SR-09 |
| b5 (edit-hook timing) | U-SR-09 |
| b6 (codex-check shape) | U-SR-05 |
| b7 (worktree hygiene) | NO UNIT — operator one-off (guard-blocked for the agent; [B] §4 item 6); hand the operator the `git worktree prune` + removal commands |
| b8 (advisor) | U-SR-06 (decision #1 ratified: REWRITE) |
| b9 (lens trial) | U-HE-51 |
| b10 (audit session shape) | U-SR-07 |
| c1 (re-pin commits) | subsumed by b1 → U-SR-09 ([B] c1: they disappear with the pin-scope fix) |
| c2 (context-save trim) | U-SR-08 |
| c3 (Bash-hook notices) | U-SR-08 |
| c4 (task nags) | NO ACTION — [B] c4: cost negligible, no workspace knob |
| c5 (r11 launch step) | subsumed by a3 → U-HE-49 ([B] c5) |
| d1 (arc-cost ledger) | U-HE-48 |
| d2 (cache-warmth note) | U-SR-07 |
| d3 (read-before-grep) | U-SR-07 |
| [A] repair 1 (classes (i)(ii)(iii) + grep shapes) + repair 5 (planted-defect evals) | U-SR-01 |
| [A] repairs 2 (fix-sweep), 3 (bound-derivation), 4 (hold-policy) | U-SR-02 |
| laws:prompt durable wiring (memory plan) | U-SR-03 |

The [B] §6 keep-as-is list (charter §4) binds every unit below: a diff that "improves" a keep-as-is surface is out of scope for its unit.

### §8.3 Bucket-1 units (contract surfaces)

#### U-HE-46: arc-metrics round derivation from round-log content

**Contract.** C-HE-25 (v1.6 X6c). **Depends on.** (none — `tools/arc_metrics.py` round snapshot exists at HEAD). **Round.** R0.

- [ ] Derive per-round records from round-log content (the wrapper's `codex-review:` terminal lines), never file position; exclude `GATE_REFUSED` transcripts (refused launches, not rounds); key rounds by round id parsed from the log name/content.
- [ ] **Acceptance witness ([B] F15, shape-asserted):** the landed U-HE-35 directory `.harness/tmp/u-he-35-rounds/` (12 files: `r1..r11.log` + `r9-verdict.log`) reduces to **10 rounds with P1s at r1 and r10** — not the corrupted `review_rounds: 12`, `p1_rounds: [1, 11]`. Fixture test pins both the count and the P1 round ids; **mutation-probe**: revert to file-position numbering → red.
- [ ] Re-derive the u-he-35 arc row in `.harness/arc-metrics.jsonl` (single-row correction via the existing queue/drain path, never a second arc row — C-HE-25 `SPLIT_BRAIN_LEDGER` rule) so the B-211 lever cohort is not seeded with the corrupted baseline. *(Plan-level operational follow-through: X6c's contract obligation is the derivation rule + fixture witness; this bullet applies the repaired rule to the one already-landed corrupted row.)*

#### U-HE-47: attribution writers

**Contract.** C-HE-24 §2/§5 (v1.6 X6d). **Depends on.** (none — the merge-gate emit path landed at U-HE-13). **Round.** R0.

- [ ] `merge-gate-emit` populates `cause_attribution` and `unique_catch` at emission on lens rows, with the X6d match predicate: `unique_catch = true` iff no same-arc finding row from a codex round matches on `(location, finding_type)` — an intra-arc heuristic join keyed on location+type, never `finding_id` (cross-`head_sha` same-defect *identity* stays out of scope per C-HE-24 §4, and C-HE-29 §2's shadow-vs-blocking definition is untouched).
- [ ] The absorption step appends the §5 `finding_adjudication` rows (`disposition`, `disposition_actor` ≠ producer — the write-time check already exists).
- [ ] **Acceptance:** a fixture arc emits gate rows after two codex rounds → non-null `cause_attribution`/`unique_catch` on every lens row; the [B] F16 state (44/44 null) is unrepresentable for new rows. **Mutation-probe:** drop the emit-time population → red.

#### U-HE-48: `tools/arc_cost.py` + `just arc-cost <transcript>`

**Contract.** C-HE-25 (v1.6 X6e). **Depends on.** (none). **Round.** R0.

- [ ] ~100-line extractor per [B] "Evidence and method": deduplicate usage by `requestId` (naive sums double-count 1.9×), IET = input + 1.25×cache-write + 0.1×cache-read + 5×output, stage windows cut at transcript event timestamps; subagent transcripts under `<transcript-dir>/subagents/` included.
- [ ] Additive-nullable cost fields onto the arc row; B-211/B-212 lever report (`arc-lever-report`) renders cost per arc when present.
- [ ] **Acceptance witness:** run against the archived U-HE-35 transcript reproduces [B]'s headline within rounding — 418 main calls / ≈20.99M IET main / ≈4.52M subagents. (If the transcript has been GC'd, the witness is a synthetic fixture with duplicated `requestId`s asserting the 1.9× dedupe delta.) *Execution-time correction (u-he-48 codex r1, measured on the witness): subagent transcripts stamp an early partial `output_tokens` copy before the final one (42 of 291 requestIds; main 0 of 428), so [B]'s first-copy read undercounted subagent output — the extractor merges copies by per-field max and the ratified subagent witness value is the corrected 4,636,541 IET (≈4.64M; main unchanged at 20,996,434). R3 comparisons read against the corrected value.*

#### U-HE-49: launch-admission guard + per-attempt round-log names

**Contract.** C-HE-21 §1 (v1.6 X6b). **Depends on.** (none — `tools/review_loop_gate.py` + `tools/round_log_publish.py` landed at B-215/U-HE-34). **Round.** R0.

- [ ] The launch step (loop recipe / `roadmap-continue` carrier) evaluates the admission state — spent-round budget and sweep attestation — BEFORE launching; a launch that would be refused is not made ([B] F13: r11 launched into `BUDGET_EXHAUSTED`; r9 launched into `SWEEP_MISSING`).
- [ ] Round-log destinations named per attempt, so a refused/failed attempt never claims the write-once round name ([B] F13: r9's relaunch reused `r9.log` → `PUBLISH FAILED (exit 4)`).
- [ ] **Acceptance:** fixture reproducing both F13 shapes — a refused launch consumes no round identity and the retry publishes cleanly; **mutation-probe**: remove the pre-launch admission check → red.

#### U-HE-50: C-HE-27 span emission from the wrapper

**Contract.** C-HE-27 §5 (v1.6 X6a). **Depends on.** U-HE-46, U-HE-47, U-HE-48, U-HE-49 (all of R0 — this unit is a lever on the very C-HE-27/C-HE-25 measurement surface R0 repairs, so the §8.1 rule binds as an edge, not only as narrative; the mechanical prerequisites `review-with-failover-logged` + the `reservations.py phase` CLI landed at U-HE-34). **Round.** R1.

- [ ] `review-with-failover-logged` emits verify start/end at its own process boundaries; absorb/edit edges keyed off the first fix edit and the absorption commit per the ship-pr block's semantics (`ship-pr/SKILL.md:95-108` stays the definition; the wrapper becomes the emitter).
- [ ] Interim carrier: the emission block copied into `roadmap-continue/SKILL.md` (the skill in context during rounds) until the wrapper path fully lands; the copy is deleted in the same PR that completes wrapper emission.
- [ ] **Acceptance:** a fixture round through the wrapper writes the verify span pair with zero skill-prose involvement; the next real code arc's reservation shows non-null verify/absorb/edit spans (U-HE-35 baseline: 0 of 60 expected edges [B] §3 item 4). SKILL.md edits owe the wiring suite per workspace discipline.

#### U-HE-51: mid-budget lens trial (rides the R3 eval arc)

**Contract.** C-HE-28 §4 (v1.6 X6f). **Depends on.** U-HE-46, U-HE-47, U-HE-48, U-HE-49 (all of R0 — the trial is only readable through repaired instruments). **Round.** R3.

- [ ] On the eval arc: concurrency + witness-adequacy lenses once after codex round 4; spec-conformance at ship only, findings-per-IET recorded (baseline 0 per 1.34M IET [B] F3).
- [ ] Record rounds-to-all-approve against the 13-round baseline; result labeled correlational (C-HE-28 §3). Lens prompts authored per U-SR-03's wiring (delegated laws:prompt agent; bindings by file).
- [ ] **Acceptance:** the trial's numbers land as C-HE-24/C-HE-25 rows via the R0 instruments; a one-paragraph comparison lands in the arc's close-out.

### §8.4 Bucket-2 units (charter surfaces)

Acceptance mechanism per charter §3: eval-backed units close on "case added to `.claude/skills/defect-class-preflight/evals/evals.json` and passing"; the rest name a mechanical witness. All R1 except U-SR-09 (R2).

#### U-SR-01: preflight — three new classes + grep shapes + P1 planted-defect evals

**Charter.** WR-01, WR-02, WR-03, WR-07. **Depends on.** (none.)

- [ ] Add the spec-phrase-to-code completeness class, the new-command-surface class (fires on any new justfile recipe whose `runs_in` includes "loop"), and the signal/lock safety class to `defect-class-preflight/SKILL.md`; add the four shapes (`returncode in (`, `except subprocess.TimeoutExpired` without `OSError`, argparse `type=int` counts, guard `elif` without witness) to `preflight-grep.sh`.
- [ ] Planted-defect eval cases for both U-HE-35 P1 shapes (exit-code-as-verdict; declared-but-unenforced gate). **Acceptance:** eval cases added and passing; grep shapes each catch a planted fixture.

#### U-SR-02: preflight meta-rules — fix-sweep, bounds, hold-policy

**Charter.** WR-04, WR-05, WR-06. **Depends on.** (none.)

- [ ] Fix-sweep sharpening verbatim: "a commit that adds a mechanism can never answer 'no new mechanism'" ([A] §2: 8 of 29 findings were on the arc's own absorption fixes). Contract-derived-bounds rule ([A] §3: the reps token took 4 paid touches). Hold-policy promotion: a held finding is accompanied in the SAME round by the minimal fail-closed probe ([A] §1: the un-promoted memory cost 4 rounds).
- [ ] **Acceptance:** eval cases for each rule added and passing.

#### U-SR-03: laws:prompt durable wiring + bindings by file

**Charter.** WR-08, WR-09. **Depends on.** (none — `just merge-gate-binding` exists.)

- [ ] The three wiring parts from the memory plan: hard skill-text step in `merge-gate`/`fan-out`/`council-workflow`; PreToolUse Agent-tool advisory hook (advisory-inject, never deny); freehand-prompt regression eval case.
- [ ] `just merge-gate-binding` writes the six binding values to a file; the lens prompt names the path and the lens agent reads it — no hand-copied values (both r3 corruptions were transcription errors, 0.38M IET + ≈5 min [B] F3).
- [ ] **Acceptance:** eval case passing; a lens round on a fixture arc runs with zero inline binding values; wiring suite green after the SKILL.md edits.

#### U-SR-04: attest labels-first + mechanism-precedent search

**Charter.** WR-10, WR-11. **Depends on.** (none.)

- [ ] Attest recipe runs `preflight-grep.sh` first and writes its labels into the answers template (3 attest-by-trial failures [B] F14 → 0).
- [ ] Precedent-search rule into the preflight grounding step, per the `mechanism-precedent-search-before-authoring` memory: reviewed sibling first, gate-log corpus query, plan skeleton is unreviewed input (≥7 of 29 findings re-derived sibling disciplines [A]).
- [ ] **Acceptance:** the attest recipe's template carries pre-filled labels on a fixture run; grounding-step text landed + eval case where applicable.

#### U-SR-05: codex-check shape

**Charter.** WR-12. **Depends on.** (none.)

- [ ] `just codex-check` invocations in the loop skills always `run_in_background` (run 3's 600 s foreground timeout cost a 10-min dead gap [B] F6).
- [ ] Stop-gate tree-dirty test ignores — or re-homes/gitignores — `.harness/.preflight-answers-*` / `.harness/.sweep-answers-*` (2 of 3 runs red on environment only, ≈20 min [B] F6/item 14).
- [ ] **Acceptance:** a tree carrying only attestation artifacts passes the stop-gate; witness test for the chosen mechanism (ignore vs re-home vs gitignore — implementer's call, recorded in the commit).

#### U-SR-06: `advisor()` reconciliation — decision #1 RATIFIED 2026-08-26: REWRITE arm (§8.5)

**Charter.** WR-13. **Depends on.** Decision #1 (§8.5 — ratified: REWRITE).

- [ ] If REWRITE: conform `CLAUDE.md` §13.1, `roadmap-continue/SKILL.md:106`, `merge-gate/SKILL.md:9,17`, `resolve/SKILL.md:35` to the instrument that exists (fresh-context Agent on the transcript summary, or `just codex-review` alone) — the [B] §3 item 1 carrier list. If PROVISION: expose the tool and leave the prose. **Acceptance:** zero governance carriers name an instrument the environment does not expose (grep witness), or the tool answers a live call.

#### U-SR-07: session-shape codification

**Charter.** WR-14. **Depends on.** (none.)

- [ ] Three habit lines into the loop skills (`roadmap-continue` / `ship-pr` close-out): facts-brief → fresh-session for heavy audits/documents ([B] F10: 0.93M vs ≈0.3M IET); cache-warmth handoff before >TTL background waits at >400k context ([B] F4: ≈0.7M IET per re-warm); read-before-grep ([B] d3: 33 `sed -n` + 101 grep-shaped calls, one API call each).
- [ ] **Acceptance:** the lines exist at their carriers (grep witness); wiring suite green.

#### U-SR-08: context-noise deletions

**Charter.** WR-15, WR-16. **Depends on.** (none.)

- [ ] `context-save` preamble trimmed to the workspace-relevant subset ([B] F11: 53.8 KB per invocation; the only sink used is the local checkpoint directory).
- [ ] PreToolUse:Bash hook emits only on an actual rewrite or guard decision ([B] F12: 143 attachments ≈ 108 KB, mostly "rewrote nothing").
- [ ] **Acceptance:** byte measurement before/after on one invocation of each; no-rewrite Bash call produces zero attachment bytes.

#### U-SR-09: mechanical sweep (R2)

**Charter.** §2 "deliberately absent" + [B] b1/b4/b5 (+ c1 subsumed). **Depends on.** (none.)

- [ ] **b1 pin scope:** bind the mutation pin to the probed block's digest (`--lines` range + test body), or pin once at ship — implementer picks the arm and records why. Witness per [B]: edit an unrelated line, assert the pin is still fresh (baseline: 12 re-pins, 13 of 29 commits, 1.07M IET [B] F7; re-pin commits disappear — c1).
- [ ] **b4 rtk shapes:** fix or pass-through the three deterministic failures — `\|` alternation → rg parse error, `--glob` landing on BSD grep, `(`/`)` in fixed-string greps ([B] F5: 29 wasted calls ≈ 1.2M IET). Witness: each shape round-trips.
- [ ] **b5 edit-hook timing:** time `postedit-lint.sh` and `graft-hooks.cjs post-edit` separately on one `.py` edit; if `uv run ruff` is the path taken, put `ruff` on hook-shell PATH ([B] F8: 945 s across 100 edits, 8.9 s median). Witness: the measured numbers land in the commit message; post-fix median asserted < the measured baseline.

### §8.5 Open operator decisions — BOTH RATIFIED 2026-08-26 (the authoring handoff's surfaced-not-decided constraint is discharged; ratification recorded on the workflow-repair-rev clearance marker, U-HE-46 arc)

1. **`advisor()` — provision or rewrite?** The governance prose names a tool no environment exposes ([B] §3 item 1: 0 calls, 65 mentions, not in any settings/MCP surface). Arms: (a) REWRITE the four carriers to the instrument that exists — [B] b8's own framing ("a discipline that cannot be followed trains the agent to skip disciplines") leans this way, and it is reversible if the tool is provisioned later; (b) PROVISION a transcript-aware advisor tool. U-SR-06 executes whichever arm is chosen. **RATIFIED: arm (a) REWRITE** (operator, 2026-08-26, via the `/roadmap-continue` invocation opening the U-HE-46 arc).
2. **U-HE-36 ordering.** The roadmap's next implementable unit is U-HE-36 (`arc_disjoint_check`). Arms: (a) **R0 → R1 → R2 → U-HE-36-as-R3** — U-HE-36 rides as the program's eval arc with U-HE-51's trial, so the first post-repair code arc is measured by repaired instruments (recommended: it costs nothing — U-HE-36 is unblocked either way — and makes the A/B read clean); (b) U-HE-36 lands first, and a later code arc serves as R3. Recommendation (a), presented rather than assumed. **RATIFIED: arm (a)** (operator, 2026-08-26, same invocation).

### §8.6 Coverage note

The §4 coverage matrix is unchanged for the v1.0 contract×unit set: the X6 clauses are v1.6 additions to already-covered contracts (C-HE-21/24/25/27/28), and §8.2 is the coverage table for the rev's own source set (every [A]/[B] recommendation id and charter WR-id maps to exactly one unit or an explicit no-unit disposition). The dependency graph gains no cycle: U-HE-46…49 and U-SR-01…09 are roots; U-HE-50 and U-HE-51 depend only on the four R0 units.

## Execution handoff

Plan complete and saved to `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md`. Two execution options:

1. **Subagent-driven (recommended for S1–S4)** — one fresh subagent per unit in topological order, each given only its unit text + the Global Constraints + Conventions sections; review between units; the S-step boundary is the PR boundary (`ship-pr`); the merge-gate 3-lens review runs on every code PR.
2. **Inline execution** — `/roadmap-continue` derives U-HE-01 as the next action once U-HE-45's rows land; execute unit-by-unit in this session with checkpoints at each S-step.

Either way the entry gate is the spec's clearance marker (spec §14) — the same doc-only PR that carries the spec, the ADRs, the council ledger, and this plan.

---

*End of `Implementation_Plan_HE_Loop_Lanes_v1.md`. Plan namespace `U-HE-NN` (H_E tooling; not an H_T axis plan). Cites resolve at `17011f89c`.*
