# E4b — session-history index, operator-shaped questions

Index: 249 sessions, 15586 chunks, built in 42s. Questions: 20, ground truth = (session, turn) verified by verbatim quote before the run.

Threshold (written before the run): POINTERS iff session hit@1 ≥ 10/20 and turn hit@5 ≥ 8/20; AUGMENT iff session hit@5 ≥ 10/20; else not adopted.

**session hit@1 = 9/20, session hit@5 = 12/20; turn hit@1 = 7/20, turn hit@5 = 8/20**

**Verdict: AUGMENT — memory stays the authority; the index answers 'where did that come from'.**

| id | question | s@1 | s@5 | t@1 | t@5 | top sim |
|---|---|---|---|---|---|---|
| Q01 | What did the witness-adequacy lens flag about how the section() extrac | ✓ | ✓ | ✓ | ✓ | 0.92 |
| Q02 | How long did the U-HE-42 arc actually take from reservation to merge? | ✓ | ✓ |  |  | 0.78 |
| Q03 | What was wrong with the original self-guard test for the CI gate-class | ✓ | ✓ | ✓ | ✓ | 0.78 |
| Q04 | What caused a test run to accidentally write into the real tracked log |  |  |  |  | 0.79 |
| Q05 | What concurrency defect did the merge-gate lens find that 20 rounds of |  |  |  |  | 0.86 |
| Q06 | When did the operator correct my read on what the laws:prompt skill is |  | ✓ |  | ✓ | 0.81 |
| Q07 | What's the rule for timing a superset-gate launch relative to codex's  | ✓ | ✓ |  |  | 0.88 |
| Q08 | How many of this arc's paid review rounds just re-derived disciplines  |  |  |  |  | 0.73 |
| Q09 | What deterministic failure trace did the concurrency lens sharpen the  | ✓ | ✓ | ✓ | ✓ | 0.85 |
| Q10 | Why did a red CI run show up on a PR that was already merged, right af | ✓ | ✓ | ✓ | ✓ | 0.83 |
| Q11 | What clearance-marker convention applies when a plan revision note is  |  |  |  |  | 0.83 |
| Q12 | What self-authored defect did the spec-conformance lens catch in my ow | ✓ | ✓ | ✓ | ✓ | 0.79 |
| Q13 | Why did main go red right after landing two merges back to back withou | ✓ | ✓ | ✓ | ✓ | 0.80 |
| Q14 | How did colored terminal output almost cause a false-green merge decis |  |  |  |  | 0.84 |
| Q15 | What did the four-regime retrieval test conclude about keeping Graft v | ✓ | ✓ | ✓ | ✓ | 0.77 |
| Q16 | What was the final accepted-versus-rejected tally of findings behind t |  |  |  |  | 0.73 |
| Q17 | Why did the loop-optimization plan get rescoped away from full code sn |  | ✓ |  |  | 0.82 |
| Q18 | By how much was the earlier session token-cost estimate inflated befor |  |  |  |  | 0.77 |
| Q19 | How did the round-3 fix for a false negative end up reopening the same |  |  |  |  | 0.79 |
| Q20 | When did the operator say the Agent tool no longer needs a human-in-th |  | ✓ |  |  | 0.75 |
