FINDINGS: 2
- [P2] tools/hooks/test_resolve_lib.sh:38-42 — the hermetic test for resolve_codex() does not assert the new model/effort pins are passed to codex.
  scenario: argv log checked only for preferred_auth_method=chatgpt and `exec`; dropping the -c pins keeps the test green.
- [P2] .harness/council/council-workflow.harness-aware.yaml:123 and .claude/skills/council/workflows/council-workflow.harness-aware.yaml:123 — no test keeps the two near-duplicate council YAMLs (and CLAUDE.md:439 prose) in sync on the review-model text, though this drift occurred during the PR's own review.
  scenario: round 2 fixed one copy, round 3 caught the other still saying gpt-5.5; nothing in CI pins the pair.
PACK_USED: none
