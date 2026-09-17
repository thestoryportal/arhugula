# E2b verification notes (working file; the results doc is derived from this)

Method: every reviewer finding is checked by reading the cited file at the reviewed commit
(scratch worktree at the PR's merge commit), never from the reviewer's description.

## PR 1330 — no pack (result_pr1330_nopack.md)

FINDINGS: 1, PACK_USED: none.

- [P2] cited `test_b137_ninety_two_floor_at_the_real_run_venue.py:966` — AST matcher counts
  only `protocol.capture_pause_snapshot(` with a bare-name receiver, so `cast(...)`-wrapped
  sites are uncounted and the `len(emit_sites) >= len(capture_sites)` assertion is weak.
  VERDICT: **verified, wrong file in the citation.** The test is
  `harness-runtime/tests/integration/test_b162_pause_resume_span_emission_is_wired.py:155-185`;
  its matcher requires `isinstance(node.func.value, ast.Name) and node.func.value.id == "protocol"`.
  `workflow_driver.py` at 2c9094c62 has 11 call sites: 3 bare (`5276, 5482, 5556`) and 8
  `cast(PauseResumeProtocol, …).capture_pause_snapshot(` (`10504, 11322, 11490, 12531, 13360,
  14860, 15714, 15780`); the matcher sees 3. The regression scenario (drop one
  `_emit_pause_captured` at a cast-wrapped site; assertion stays green) follows.
  In-scope: PR 1330 changed 5 files — `.harness/forward-register.yaml`,
  `.harness/post-phase-8-forward-register.md`, `harness-cp/src/harness_cp/workflow_driver.py`,
  `test_b137_ninety_two_floor_at_the_real_run_venue.py`,
  `test_b162_pause_resume_span_emission_is_wired.py` — and the `capture_sites` matcher is
  added by the diff itself (4 diff lines mention it).

## PR 1330 — pack (result_pr1330_pack.md)

FINDINGS: 1, PACK_USED: yes.

- [P1] `test_b162_pause_resume_span_emission_is_wired.py:165-172,182` — same defect as the
  no-pack finding, with the correct file and lines, and the reviewer ran the filter itself
  (`capture_sites = [5276, 5482, 5556]`, `emit_sites` 11 entries). VERDICT: **verified**
  (same read as above). Severity P1 vs the no-pack P2 for the same defect.
  Pack vs no-pack on 1330: same single defect found both ways; the pack run cited the right
  file, the no-pack run cited the neighbouring B-137 test file.

## PR 1341 — codex exec, gpt-5.6-sol medium, with pack (codex_pr1341_pack.md)

FINDINGS: 2, PACK_USED: yes. 294 s, 225,503 tokens.

- [P2] `harness-runtime/tests/test_b71_escalation_token_mint_and_fold_u_rt_155.py:550` —
  the closing test substitutes a local dictionary for the real resume-time audit / dedup.
  VERDICT: **verified.** The enclosing test is
  `test_two_peers_sharing_a_child_workflow_id_now_produce_distinct_keys` (added by this
  diff): keys come from the real `compose_hitl_action_id`, but "both entries survive dedup"
  is asserted as `deduped = {k: None for k in keys}; assert len(deduped) == 2` — a dict
  comprehension, not the C-IS-07 §7.5 audit path.
- [P2] `harness-cp/tests/test_b71_escalation_correlation_carriers_u_cp_102.py:137` — every
  carrier test hardcodes PARALLELIZATION while the diff separately changed the
  orchestrator-workers path. VERDICT: **verified.** The file's 28 tests use
  `TopologyPattern.PARALLELIZATION` (line 137, the shared `_manifest`) and one
  `SINGLE_THREADED_LINEAR` (line 1029); no orchestrator-workers or hierarchical basis.
  The diff's `workflow_driver.py` hunks include `_execute_orchestrator_workers`,
  `_cancel_worker` and `_execute_parallelization` (hunk headers), so the claim that the
  worker path changed without a carrier test holds.

## PR 1341 — no pack (result_pr1341_nopack.md)

FINDINGS: 1, PACK_USED: none.

- [P1] `hitl_gate_composer.py:1713` — the audit `hitl_action_id` folds
  `resolve_escalation_instance_id` unconditionally while the driver sets
  `pre_dispatch_escalation_basis` for EVERY fan-out branch, so a gate that never escalates
  still gets a `:<sha256>` suffix on its audit/idempotency key; untested population.
  VERDICT: **mechanism verified; severity contested; the test gap is real.** Read at
  0af632f4e: `resolve_escalation_instance_id` arm 2 mints whenever the basis is non-None
  (hitl_gate_composer.py, resolver docstring arms 1-3); `workflow_driver.py:8560-8572`
  derives the basis "UNCONDITIONALLY, HOISTED OUT of the resume-only guard" (comment cites
  CP §25.20); `_compose_and_persist_audit` folds the token with the comment "the fold
  reaches THIS invocation too, not only the webhook one" citing Runtime spec v1.121 site 2
  and CP §0.2's one-identity-family promise. So the key change for non-escalating
  fan-out-branch gates is what the code says the spec directs, not an accident — but no
  test in the diff exercises that population (the two new test files cover escalating
  peers only), which is the defensible residue of the finding. Spec-cite check: see grep
  result recorded in the results doc.
