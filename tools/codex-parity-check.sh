#!/usr/bin/env bash
# Blocking provider-free regression gate for the Codex compatibility layer.

set -euo pipefail

unset ANTHROPIC_API_KEY OPENAI_API_KEY E2B_API_KEY GEMINI_API_KEY GOOGLE_API_KEY
unset GOOGLE_APPLICATION_CREDENTIALS GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_LOCATION
unset GOOGLE_GENAI_USE_VERTEXAI HARNESS_CODEX_REVIEW_ISOLATED
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/arhugula-uv-cache}"

# The C-HE-11 two-stacks-up witness is classified `env` / local-only in the §8.1 manifest
# (tools/lanes_verify.py) and is DESELECTED here rather than left to its skipif: on any
# host with a reachable daemon this gate would otherwise start two real stacks, write
# claims into the production lane registry, and run destructive volume cleanup. It runs
# from `just lanes-verify`, where an env-tagged row belongs.
uv run pytest -q \
  tools/test_ac_claim_precision.py \
  tools/test_agy_review.py \
  tools/test_codex_stop_gate.py \
  tools/test_codex_workflow_parity.py \
  tools/test_codex_worktree_gc.py \
  tools/test_arc_exit_report.py \
tools/test_arc_close_recipe.py \
  tools/test_branch_hygiene_batch.py \
  tools/test_mutation_probe.py \
  tools/test_pin_scope.py \
  tools/test_rtk_shape_guard.py \
  tools/test_arc_metrics.py \
  tools/test_round_log_publish.py \
  tools/test_arc_metrics_lanes.py \
  tools/test_arc_lever_report.py \
  tools/test_arc_cost.py \
  tools/test_loop_cost_baseline.py \
  tools/test_lease_yield_trigger.py \
tools/test_concurrency_surface.py \
  tools/test_arc_disjoint_check.py \
  tools/test_ci_yml_concurrency.py \
  tools/test_ci_bookkeeping_filter.py \
  tools/test_finding_record.py \
  tools/test_refresh_classes.py \
  tools/test_lanes_verify.py \
  tools/test_lanes_pilot_gate.py \
  tools/test_main_protection.py \
  tools/test_merge_door.py \
  tools/test_merge_gate_log.py \
  tools/test_merge_gate_emit_all.py \
  tools/test_review_wrapper.py \
  tools/test_reviewer_concurrency_probe.py \
  tools/test_reservations.py \
  tools/test_store_audit.py \
  tools/test_closure_certification.py \
  tools/test_compose_lanes.py \
  --deselect tools/test_compose_lanes.py::test_two_lanes_disjoint_names_and_ports \
  tools/test_docs_completeness.py \
  tools/test_graft_reachability.py \
  tools/test_leg_selfcheck.py \
  tools/test_loop_status_isolation.py \
  tools/test_memory_closeout_check.py \
  tools/test_prime_report.py \
  tools/test_managed_cloud_readiness.py \
  tools/test_q4_packaging_gate.py \
  tools/test_r420_self_hosted_local_stack.py \
  tools/test_r421_e2b_live_probe.py \
  tools/test_r421_managed_cloud_live_e2e.py \
  tools/test_r430_tail_keep_collector.py \
  tools/test_r810_files_live_e2e.py \
  tools/test_r820_managed_agents_live_e2e.py \
  tools/test_sandbox_host_readiness.py \
  tools/test_self_hosted_readiness.py \
  tools/test_tools_test_coverage_guard.py

# tools/roadmap-audit/ is named explicitly alongside the other two dirs: pytest's
# testpaths does not reach tools/, so a shell suite that is not listed here simply never
# runs in CI. test_session_start.sh had been in that gap -- U-HE-30's SessionStart wiring
# broke one of its assertions and every gate stayed green.
for test_script in tools/hooks/test_*.sh tools/statusline/test_*.sh tools/roadmap-audit/test_*.sh; do
  bash "$test_script"
done
