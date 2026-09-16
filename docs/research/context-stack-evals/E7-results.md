# E7 — subagent preload: full repo context vs a Graft card brief

Four questions, two conditions, `claude -p` on the subscription. `full` = run inside the repo with its default context (root guide, memory, hooks). `brief` = run from a scratch directory with a minimal system prompt plus `graft map --budget 1500` as the only repo orientation, repo reachable via --add-dir. Total tokens = input + cache creation + cache read + output summed over the run's turns.

| cond | q | turns | total tokens | cache_creation (first-turn context) | s | answer (head) |
|---|---|---|---|---|---|---|
| full | Q01 | 8 | 687816 | 69601 | 49 | I hit a permission wall while trying to satisfy the Stop-hook lint gate. Here's what happened, so you can decide how to proceed:      1. Your question |
| full | Q06 | 27 | 2578918 | 86311 | 174 | All ruff lint (E501, B007, B905) and formatting issues in `docs/research/context-stack-evals/` are resolved — verified clean via `ruff check` and `ruf |
| full | Q10 | 2 | 162722 | 55997 | 8 | ANSWER: harness-is/src/harness_is/path_class_registry.py, line 31   EVIDENCE: harness-is/src/harness_is/path_class_registry.py:31 — "class PathClass(S |
| full | Q15 | 2 | 162235 | 55563 | 8 | ANSWER: tools/roadmap_status_refresh.py defines class WorkspaceState at line 117.   EVIDENCE: tools/roadmap_status_refresh.py:117 — "class WorkspaceSt |
| brief | Q01 | 3 | 136698 | 47150 | 9 | ANSWER: (1) the PR title begins with `ops: roadmap status refresh `, and (2) the only file changed is `.harness/roadmap_status.md`.   EVIDENCE: CLAUDE |
| brief | Q06 | 4 | 183252 | 31135 | 10 | ANSWER: gpt-5.6-sol at medium reasoning effort.   EVIDENCE: CLAUDE.md:439 — "gpt-5.6-sol at medium reasoning effort, pinned per invocation rather than |
| brief | Q10 | 2 | 88951 | 28232 | 8 | ANSWER: harness-is/src/harness_is/path_class_registry.py defines the PathClass enum, at line 31.   EVIDENCE: harness-is/src/harness_is/path_class_regi |
| brief | Q15 | 2 | 88440 | 27751 | 5 | ANSWER: tools/roadmap_status_refresh.py defines the class WorkspaceState, at line 117.   EVIDENCE: tools/roadmap_status_refresh.py:117 — "class Worksp |

full: mean turns 9.8, mean total tokens 897,923, mean first-turn context 66,868, mean 60 s

brief: mean turns 2.8, mean total tokens 124,335, mean first-turn context 33,567, mean 8 s

Ground truth: Q01 → CLAUDE.md:359-360; Q06 → CLAUDE.md:439; Q10 → path_class_registry.py:31; Q15 → roadmap_status_refresh.py:117

CORRECTNESS (author-judged): brief 4/4 correct (Q01, Q06, Q10, Q15 all match ground truth). full: Q10 and Q15 correct; Q01 and Q06 NOT ANSWERED — both runs were derailed by the repository's Stop-hook lint gate (tools/hooks/stop-gate.sh), which fired on the untracked evaluation scripts present in the working tree at run time; the agent spent 8 and 27 turns attempting lint repair instead of answering. Those two rows measure hook interference, not retrieval, and are excluded from the clean comparison.

Clean comparison (Q10, Q15 only): full 2 turns, 162k total tokens, ~56k first-turn context, 8 s; brief 2 turns, 88k total tokens, ~28k first-turn context, 5–8 s. Same answers, same evidence, half the context and tokens.