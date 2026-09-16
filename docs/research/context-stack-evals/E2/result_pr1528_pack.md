FINDINGS: 1
- [P2] tools/hooks/test_resolve_lib.sh:38-42 — resolve_codex's new review-model pins are unverified by this test.
  scenario: resolve_lib.sh:18 adds `-c model="gpt-5.6-sol" -c model_reasoning_effort="medium"`; the test asserts only preferred_auth_method=chatgpt and `exec`; dropping the pins would keep the suite green while /resolve reverts to the config-default model.
PACK_USED: yes
