# E2b — structural context packs on code with callers (handoff-s2 §2 D)

E2 tried packs on two tool-and-test PRs whose `graft blast` was empty, so there was nothing
for a pack to show. This run picks merged PRs touching `harness-*/src` whose blast radius at
depth 2 is non-empty, runs the same four-reviewer design, verifies every finding by reading
the cited code at the reviewed commit, and adds one Codex run with the pack.

## Setup

| | PR 1341 | PR 1330 |
|---|---|---|
| merge commit | `0af632f4e` | `2c9094c62` |
| title | B-71 impl leg — branch-distinct escalation correlation (cp, runtime) | B-162 — wire `pause.captured` + `resume.attempted` emission |
| diff | 208 KB, 9 `harness-*/src` files | 45 KB, 1 `harness-*/src` file |
| blast at depth 2 | 2 dependent symbols (`_runner`, `dispatch`), 92 test suites reference the code | 1 dependent symbol (`_runner`), 32 test suites |

Candidates measured and rejected for an empty blast: PR 1317 (runtime retry/fallback, 18
files) and PR 1390 (OD alignment floor, 26 files). Each candidate got a scratch worktree at
its merge commit with its own `graft build` (2–3 s each, extraction cache reused), and
`graft blast --base <parent> --depth 2 --format markdown`.

Reviewers: four fresh Sonnet agents, one per (PR, condition), each handed
`E2/reviewer_prompt.txt` with the pack or the "(no structural context provided)" line and
the diff (`e2b_assemble.py`). Unlike E2, the repository path in the brief is the worktree at
the reviewed commit, not the live checkout (README "Known defects" item 3). Plus one
`codex exec` run (gpt-5.6-sol, medium, read-only sandbox, subscription) on PR 1341 with the
pack — the production transcript-less reviewer.

## Findings, each verified by reading the cited file at the reviewed commit

| run | reported | verified | severity | PACK_USED | what it found |
|---|---|---|---|---|---|
| 1330 no pack | 1 | 1 | P2 | none | AST site counter in the new B-162 test matches only bare `protocol.` receivers: 3 of 11 real `capture_pause_snapshot` sites counted, so `len(emit_sites) >= len(capture_sites)` cannot see the 8 `cast(...)`-wrapped ones. Cited the neighbouring B-137 test file; the test is `test_b162_pause_resume_span_emission_is_wired.py:155-185`. |
| 1330 pack | 1 | 1 | P1 | yes | The same defect, cited at the right file and lines, and the reviewer ran the filter (`[5276, 5482, 5556]` vs 11 emit sites). |
| 1341 no pack | 1 | 1 (mechanism) | P1 → spec-directed, test gap remains | none | The audit `hitl_action_id` folds the B-71 token for every fan-out-branch gate, escalated or not. True, and directed: Runtime spec v1.121 site 2 names `_compose_and_persist_audit` as one of the three fold sites and CP §0.2 requires one identity family. What survives: no test in the diff exercises a non-escalating fan-out-branch gate's key. |
| 1341 pack | 1 | 1 (one step not re-traced) | P1 | yes | The basis is derived for every fan-out branch and carried to every later step of it (`workflow_driver_types.py:585-601`, "a branch child never re-derives it"), so a gate escalating after a child was dispatched still mints a token and `_escalation_operator_surface` stamps `resolvability: UNIFORM_FALLBACK_ONLY` on a pause that is directly addressable. The spec's population is "pre-dispatch gate-owning escalation" (CP v1.119 §25.20 table, §0.4 arm 3, §26.9 absence rule); the implementation's is "every fan-out-branch escalation", by the §0.4.1 correction that derives unconditionally. Carrier, resolver arm 2 and the unconditional stamp verified; the reviewer's route to a post-dispatch escalation through the same `step_context` (dispatch loop, durable-async branch) is its trace, not re-traced here. |
| 1341 codex + pack | 2 | 2 | P2, P2 | yes | (a) `test_two_peers_sharing_a_child_workflow_id_now_produce_distinct_keys` asserts dedup survival on a dict comprehension, not the C-IS-07 §7.5 audit path; (b) all 28 carrier tests use `PARALLELIZATION` (one `SINGLE_THREADED_LINEAR`) while the diff has hunks in `_execute_orchestrator_workers` and `_cancel_worker`. 294 s, 225,503 tokens. |

Tally: 6 findings reported; 4 verified in full; 2 verified with a qualification (the 1341
no-pack P1 names a change the spec directs, so only its test-gap residue stands; the 1341
pack P1 rests on one trace step the reviewer made and this run did not re-trace); 0 refuted.
The precision claim below counts the qualified two as partial, not as clean hits. Both Sonnet
pack runs said the pack changed what they looked at; both no-pack runs found a real defect
anyway.

## Pack vs no pack

- **Count:** identical on both PRs (1 vs 1). The pack did not find more.
- **Precision:** on 1330 the pack run cited the exact file and lines and executed the
  matcher; the no-pack run cited the wrong file for the right defect. On 1341 the pack run
  went to the operator-facing consequence (the wrong `resolvability` stamp) while the
  no-pack run stopped at the key-format change the spec directs.
- **Blast content:** the packs named `_runner` and `dispatch` as dependents; neither
  reviewer's finding is located in those symbols. The sharper 1341 finding came from
  following `pre_dispatch_escalation_basis` through the carriers, which the pack's
  "test signal" section pointed at (`not reached: … _cancel_branch, _cancel_worker`) but
  did not name.
- **Codex with the pack** produced two verified test-adequacy findings and marked the pack
  used; without a paired no-pack Codex run this is a data point, not a comparison.

## Verdict

**Supported for precision, not for recall, at n=2, with the two qualified rows counted as
partial.** A pack does not make a transcript-less reviewer find more defects on these PRs; it
makes the finding it does report land on the right file and reach the operator-visible
consequence. Adoption shape: attach the blast
markdown to the out-of-family review prompt (it is $0 and ~3 KB), and judge it by citation
accuracy in the finding rows, which the gate log already records.

## Residue for the register

Two findings are against merged code and are not this PR's to fix:
1. PR 1330's AST site counter undercounts 8 of 11 sites (verified; test-adequacy).
2. PR 1341's token population is wider than the spec's stated one, with an operator-facing
   stamp on the wider population (verified carriers and stamp; one trace step from the
   reviewer). A spec-versus-implementation question, so a fork/register item, not a patch.
Plus two Codex test-adequacy findings on 1341 (local-dict dedup; PARALLELIZATION-only
carriers).
