FINDINGS: 2
- [P2] tools/codex_context_guard.py:464 — _owed_lag/_lag_expected read git's live HEAD parent chain rather than the resolved --head-ref, so the local codex-context-check-ci recipe and the CI guard job can disagree on the same commit.
  scenario: multi-commit PR against a main whose tip is a refresh commit: local checks the branch tip (no lag), CI's synthetic merge has the refresh as first parent (lag expected); PARITY_EXCLUSIONS never names this code.
- [P2] tools/test_codex_context_guard.py:2536 — the fixture for GitHub's synthetic merge-ref checkout never creates a two-parent merge commit, so the merge-parent branch at codex_context_guard.py:511 is unreached by any test.
  scenario: a regression in the merge-walk logic passes the suite while live on every real CI pull_request run.
PACK_USED: none
