#!/usr/bin/env python3
"""Spec §8.1 verification manifest as data + the umbrella runners (spec-he-loop-lanes).

`just lanes-verify` runs every row. `just lanes-phase0-check` runs rows tagged `phase0`
and treats a skip as a failure (C-HE-13 §1: an implicit precondition is not a gate).
`just mutation-probe-coverage-check` asserts every row marked mutation-probe has a PINNED
probe result in `.harness/mutation-probe-log.jsonl` (the run log `tools/mutation_probe.py`
appends on every exit). Only the three named environment skip reasons are legal; "slow" is
never one. Rows are appended by the unit that lands each artifact; keep them in §8.1 order.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pin_scope  # [LAW:one-source-of-truth] the pin digest format + theorem live there

REPO = Path(__file__).resolve().parent.parent
PROBE_LOG = REPO / ".harness" / "mutation-probe-log.jsonl"
TAGS = ("phase0", "phase1", "measurement", "layer2", "env", "operator-gated")
ALLOWED_SKIP_REASONS = ("docker-daemon-absent", "provider-login-absent", "gh-auth-absent")
KINDS = ("pytest", "shell", "just", "live")
_SKIP_RE = re.compile(r"^SKIPPED \[\d+\] [^:]+:\d+: (.+)$", re.M)


@dataclass(frozen=True)
class Row:
    contract: str
    artifact: str  # pytest:<nodeid> | shell:<path> | just:<recipe> | live:<desc>
    tag: str
    runs_in: str
    mutation_probe: bool
    skip_reasons: tuple[str, ...] = ()
    depends: str = ""


@dataclass
class Result:
    row: Row
    status: str  # pass | fail | skip | live
    reason: str = ""


#: Rows are appended by the unit that lands each artifact. Keep in §8.1 order.
MANIFEST: list[Row] = [
    # C-HE-02 (U-HE-16)
    Row(
        "C-HE-02",
        "pytest:tools/test_arc_metrics.py::test_takeover_token_compare",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-02",
        "pytest:tools/test_arc_metrics.py::test_no_flock_fcntl_in_coordination_modules",
        "phase0",
        "local + CI",
        False,
    ),
    # C-HE-03 (U-HE-17)
    Row("C-HE-03", "pytest:tools/test_reservations.py", "phase0", "local + CI", True),
    # C-HE-03 §3/§4 + C-HE-26 §1 (U-HE-21): skill/hook carrier wiring — grep witness
    Row(
        "C-HE-03",
        "shell:tools/hooks/test_skill_reservation_wiring.sh",
        "phase0",
        "local + CI",
        False,
    ),
    # C-HE-03/04 (U-HE-20): AC#2 subprocess lanes — real subprocesses, NO skip
    Row(
        "C-HE-03/04",
        "pytest:tools/test_arc_metrics_lanes.py::test_ac2_a_same_instant",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-03/04",
        "pytest:tools/test_arc_metrics_lanes.py::test_ac2_b_cross_latency",
        "phase0",
        "local + CI",
        True,
    ),
    # C-HE-04 (U-HE-15); the shell row carries the §6 teardown ahead-of-@{u} guard
    Row(
        "C-HE-04",
        "pytest:tools/test_arc_metrics.py::test_drain_fault_isolation",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-04",
        "pytest:tools/test_arc_metrics.py::test_e9_capture_republish",
        "phase0",
        "local + CI",
        True,
    ),
    # C-HE-11 §3 (U-HE-32) joins this row rather than adding a second one: the manifest
    # forbids a duplicate artifact, and `hook_git_retry`'s witnesses live in this suite.
    Row("C-HE-04/11", "shell:tools/hooks/test_lib.sh", "phase0", "local + CI", True),
    # C-HE-04 §2/§4/§5 (U-HE-19): holder-gated append + drain ⇄ reservation integration —
    # one row per contract section so reverting any half leaves a RED row (codex r2 P3)
    Row(
        "C-HE-04",
        "pytest:tools/test_arc_metrics.py::test_append_refuses_unless_holder",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-04",
        "pytest:tools/test_arc_metrics.py::test_drain_flips_before_append_and_folds_reservation_fields",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-04",
        "pytest:tools/test_arc_metrics.py::test_recover_transfers_holder_to_recoverer",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-04",
        "pytest:tools/test_arc_metrics.py::test_local_row_reconciliation_drops_superseded_rows",
        "phase0",
        "local + CI",
        True,
    ),
    # C-HE-05 (U-HE-10)
    Row(
        "C-HE-05",
        "pytest:tools/test_arc_metrics.py::test_env_overrides",
        "phase0",
        "local + CI",
        False,
    ),
    # C-HE-06 (U-HE-22): merge-door lease primitive — acquire/holder/rate/marker
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_lease_holder_invariant",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_contention_fail_fast",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_marker_race_exactly_one_wins",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_rate_limit_sixth_refused",
        "phase0",
        "local + CI",
        True,
    ),
    # C-HE-06 §4/§5/§8 (U-HE-23): landing driver — reconcile, continuation, crash-resume
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_timeout_reconcile_merged_calls_once",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_continuation_no_reacquire",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_post_merge_ci_blocked_and_unblock",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_inflight_first_attempt_then_reissue",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-06",
        "pytest:tools/test_merge_door.py::test_ac2_c_crash_resume",
        "phase0",
        "local + CI",
        True,
    ),
    # C-HE-07 (U-HE-25): raw gh pr merge denied / safe-merge wrapper allowed (+ the
    # U-HE-21-registered exact-shape allowlist additions)
    # + C-HE-08 §1 (U-HE-26): push-to-main denied in the audited deny block (argument-list
    # parser on the prefix-stripped command); topic pushes stay auto-allowed. One combined
    # row: the manifest keys rows by artifact (test_manifest_rows_well_formed), mirroring
    # C-HE-09/10 / C-HE-15/16/18.
    Row("C-HE-07/08", "shell:tools/hooks/test_permission_guard.sh", "phase0", "local + CI", True),
    # C-HE-08 §2–§5 (U-HE-27): server-side X9 fence. `verify` exact-compares the live
    # protection against the ci.yml-derived payload (404/mismatch → RED; auth-absent is the
    # legal skip, which phase0 counts as NOT passed per C-HE-13 §1). The apply + tiebreaker
    # halves are operator-gated live steps recorded in the plan evidence log.
    Row("C-HE-08", "just:main-protection-verify", "phase0", "local", False, ("gh-auth-absent",)),
    Row(
        "C-HE-08",
        "live:main-protection-tiebreaker + apply (operator-gated; evidence log)",
        "operator-gated",
        "loop, live",
        False,
    ),
    # C-HE-09/10 (U-HE-09; extended by U-HE-29 with the shared venue, the structured
    # column, the ACTIVATE-scoping strike and the NOTIFY kind)
    Row("C-HE-09/10", "shell:tools/hooks/test_loop_lib.sh", "phase0", "local + CI", True),
    # C-HE-09 §2 (U-HE-29): the venue's SECOND live carrier. arc_exit_report.py both reads
    # pending rows from the ledger and appends its own EXIT-REPORT index row, so it must
    # resolve the path through loop_lib.sh's loop_status_path rather than deriving one of
    # its own -- a second derivation would point the append's growth check at a file the
    # writer never touches and fail every arc closed (exit 3). Registered separately from
    # the shell row above because it is a different artifact (rows are keyed by artifact).
    Row("C-HE-09", "pytest:tools/test_arc_exit_report.py", "phase0", "local + CI", False),
    # C-HE-11 (U-HE-31): lane env isolation — index registry, gc.auto once, RAM probe,
    # per-lane compose project/ports. The two-lanes-up row is the only one that can prove
    # "no port bind conflict", so it stays env-tagged rather than being folded into the
    # daemon-free formula row.
    Row("C-HE-11", "shell:tools/hooks/test_lane_init.sh", "phase0", "local + CI", False),
    Row(
        "C-HE-11",
        "pytest:tools/test_compose_lanes.py::test_lane_port_formula",
        "phase0",
        "local + CI",
        False,
    ),
    Row(
        "C-HE-11",
        "pytest:tools/test_compose_lanes.py::test_compose_uses_port_variables",
        "phase0",
        "local + CI",
        False,
    ),
    Row(
        "C-HE-11",
        "pytest:tools/test_compose_lanes.py::test_stack_recipes_pass_a_per_lane_project",
        "phase0",
        "local + CI",
        False,
    ),
    Row(
        "C-HE-11",
        "pytest:tools/test_compose_lanes.py::test_two_lanes_disjoint_names_and_ports",
        "env",
        "local",
        False,
        ("docker-daemon-absent",),
    ),
    # C-HE-12 (U-HE-33): emitting detections — split-brain ledger + base TOCTOU
    Row(
        "C-HE-12",
        "pytest:tools/test_codex_context_guard.py::test_split_brain_ledger_duplicate_arc_id",
        "phase0",
        "local + CI",
        False,
    ),
    Row(
        "C-HE-12",
        "pytest:tools/test_codex_context_guard.py::test_base_toctou",
        "phase0",
        "local + CI",
        False,
    ),
    # C-HE-15/16/18 (U-HE-02/03/04); C-HE-17 (U-HE-06/07)
    Row("C-HE-15/16/18", "pytest:tools/test_review_wrapper.py", "phase0", "local + CI", True),
    Row(
        "C-HE-17",
        "pytest:tools/test_review_wrapper.py::test_failover_invoked_once_on_primary_unavailable_and_blocks",
        "phase0",
        "local + CI",
        False,
    ),
    Row("C-HE-17", "pytest:tools/test_agy_review.py", "phase0", "local + CI", False),
    # C-HE-22 (U-HE-35): the live probe gates pilots (C-HE-13 §2); the pytest row pins
    # the pass rule on synthetic samples
    Row(
        "C-HE-22",
        "pytest:tools/test_reviewer_concurrency_probe.py",
        "phase1",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-22",
        "live:tools/reviewer_concurrency_probe.py "
        "(provider-login-gated; result row required before pilots)",
        "phase1",
        "operator/loop, live",
        False,
        ("provider-login-absent",),
    ),
    # C-HE-22 / C-HE-13 §2 (codex r10 P1): the "result row required before pilots" bound,
    # made fail-closed and mechanical — absent (probe never run) and RED both refuse.
    Row("C-HE-22", "just:pilot-gate-check", "phase1", "local", False),
    # C-HE-13 §4-5 (U-HE-36): selection-time merge-tree refusal + the O3 base-rate replay
    Row("C-HE-13", "pytest:tools/test_arc_disjoint_check.py", "phase1", "local + CI", False),
    # C-HE-13 §1-3 (U-HE-37): the mechanical pilot gate + the pilot report. The report row
    # carries a `<run-id>` placeholder, so `_command` returns None and the runner marks it
    # LIVE — it is answered by a real pilot's report line in the plan evidence log, never
    # by an auto-run with an invented run id.
    Row("C-HE-13", "pytest:tools/test_lanes_pilot_gate.py", "phase1", "local + CI", False),
    Row("C-HE-13", "just:lanes-pilot-report <run-id>", "phase1", "local", False),
    # C-HE-19/20 (U-HE-08)
    Row(
        "C-HE-19/20",
        "pytest:tools/test_arc_metrics.py::test_ci_state_cancelled_incomplete",
        "phase0",
        "local + CI",
        True,
    ),
    # C-HE-19/20 + C-HE-03 §5 (U-HE-18): TTL never reclaims -- ground-truth reconcile only
    Row(
        "C-HE-19/20",
        "pytest:tools/test_reservations.py::test_ttl_never_reclaims",
        "phase0",
        "local + CI",
        True,
    ),
    # C-HE-23–26 (U-HE-01 / U-HE-11 / U-HE-12 / U-HE-13)
    Row("C-HE-23–26", "pytest:tools/test_finding_record.py", "phase0", "local + CI", True),
    Row(
        "C-HE-23–26",
        "pytest:tools/test_arc_metrics.py::test_arc_row_schema_has_c_he_25_fields",
        "phase0",
        "local + CI",
        False,
    ),
    Row(
        "C-HE-23–26",
        "pytest:tools/test_arc_metrics.py::test_arc_type_at_open",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-25/28",
        "pytest:tools/test_arc_metrics.py::test_cohort_split_null_safe",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-26 §2",
        "pytest:tools/test_arc_metrics.py::test_relabel_aborts_when_the_ledger_changed_underneath",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-26 §2",
        "pytest:tools/test_arc_metrics.py::test_append_and_relabel_are_mutually_exclusive_by_claim",
        "phase0",
        "local + CI",
        True,
    ),
    Row(
        "C-HE-26 §2",
        "pytest:tools/test_arc_metrics.py::test_dead_claim_reclaim_never_steals_a_peers_fresh_live_claim",
        "phase0",
        "local + CI",
        True,
    ),
    Row("C-HE-23", "pytest:tools/test_merge_gate_log.py", "phase0", "local + CI", True),
    Row("C-HE-23", "just:merge-gate-log-check", "phase0", "local + CI", False),
    # C-HE-27 (U-HE-34) — §-granular on purpose: §1's `capture` pair is structurally
    # unrecordable post-terminal (B-218), so no row claims the whole contract green.
    # (The plan's draft label said "C-HE-27/28"; the no-deltas witness inspects the
    # phase-duration readers only and exercises no C-HE-28 cohort reporting, so the
    # C-HE-28 half is dropped — codex r4.)
    Row(
        "C-HE-27 §2",
        "pytest:tools/test_arc_metrics.py::test_phase_spans_no_deltas",
        "measurement",
        "local + CI",
        False,
    ),
    Row(
        "C-HE-27 §4",
        "pytest:tools/test_arc_metrics.py::test_n6_formula",
        "measurement",
        "local + CI",
        False,
    ),
    # C-HE-28 (U-HE-38) — §-granular for the same reason the C-HE-27 rows above
    # are: this witness exercises the joint cohort split (§1), the drift-finding
    # join (§2) and the correlational header (§3), and no C-HE-27 phase span, so
    # the plan's draft "C-HE-27/28" label is narrowed here exactly as codex r4
    # narrowed it there.
    # mutation-probe `—`: the discriminating mutations are SUBSTITUTIONS, and no
    # line DELETION expresses them — every line in the drift block is load-bearing
    # for the next, so the probe tool's comment-out range would raise NameError and
    # be refused as indeterminate rather than run. Both were performed manually and
    # are recorded on the U-HE-38 commits, each naming the test it actually kills:
    # (a) counting raw gate rows instead of reducing by finding_id and dropping
    # `rejected` -> test_a_refuted_drift_finding_is_not_a_collision via the NUMERATOR
    # (the undisposed row survives and N=4 flips 0/6 -> 1/6), and
    # test_drift_incidence_counts_findings_not_log_rows via its distinct-FINDING count
    # assertion only -- NOT via its numerator, because `affected_arcs` is a set keyed
    # by arc_id, so duplicate rows for one arc cannot move that cell (corrected from
    # the merge-gate witness-adequacy lens on PR #1523); (b) widening the drift
    # predicate back to either carrier -> test_drift_detection_binds_to_producer_only;
    # (c) dropping the siblings+1 conversion -> test_cohort_split_null_safe,
    # test_lane_cohort_medians_exclude_lower_bound_rows and
    # test_a_row_predating_the_lane_field_is_not_a_row_that_recorded_null; (d) a
    # finding-count numerator -> test_two_collisions_on_one_arc_are_one_affected_arc;
    # (e) gating measurability on `observed` rather than `attributable` ->
    # test_only_unattributable_rows_leave_every_cohort_unavailable.
    # All go green again on restore. Two earlier revisions of this note named probes
    # that could not have run as described — one against a test whose fixture made it
    # unfailable, and one (a cohort-size denominator) against a test deleted when the
    # committed implementation ADOPTED that very denominator at r8. Both are recorded
    # rather than quietly dropped, because a probe list is only worth its accuracy:
    # every entry above was re-run against THIS head.
    # Same disposition, and the same reason, as the WR-14 row below
    # (probe outside the tool's expressible set; manual probe recorded on the
    # landing commit).
    # The claim is SPLIT at §1+§3 on purpose, and §2 is deliberately not claimed.
    # §2's correlation mechanism is built and tested, but no runtime emitter persists
    # a ROADMAP_STATUS_DRIFT row to the gate log, so its numerator can only ever
    # report UNWIRED in production while a synthetic-row test stays green. A manifest
    # row asserting §2 covered would be a green check over a measurement that cannot
    # happen — the shape C-HE-22's "result row required before pilots" P1 was. The
    # emitter is producer-side work outside this unit's files and is registered as
    # B-237; §2 is claimable when it lands, not before.
    Row(
        "C-HE-28 §1+§3",
        "pytest:tools/test_arc_metrics.py::test_cohort_by_concurrent_lanes_at_open_and_arc_type",
        "measurement",
        "local + CI",
        False,
    ),
    # C-HE-30 (U-HE-14)
    # spec §8.1: mutation-probe `—` (static doc witness; no deletion-expressible target)
    Row("C-HE-30", "pytest:tools/test_store_audit.py", "phase0", "local + CI", False),
    # C-HE-01/14/21/34/35 (U-HE-39) — skill-carrier doc sweep: N ≥ 2 lane model, the
    # C-HE-14 table byte-exact, live-carrier invariant cites, non-goals, K5–K8, and the
    # §8 AC#7 no-numeric-round-cap scan. mutation-probe `—`: markdown skill prose, the
    # C-HE-30 static-doc-witness precedent; the red run against origin/main's tree is
    # recorded in the PR body.
    Row(
        "C-HE-01/14/21/34/35",
        "shell:tools/hooks/test_skill_lanes_docs.sh",
        "phase0",
        "local + CI",
        False,
    ),
    # WR-14 (U-SR-07) — session-shape habit lines at their loop-skill carriers.
    # mutation-probe `—`: the probed artifact is markdown skill prose, outside the
    # probe tool's language set (C-HE-30 static-doc-witness precedent); the manual
    # appendix-relocation probes are recorded on the u-sr-07 absorption commit.
    Row(
        "WR-14",
        "shell:tools/hooks/test_skill_session_shape.sh",
        "phase0",
        "local + CI",
        False,
    ),
    # WR-15 (U-SR-08) — context-save preamble trim: the project skill
    # context-save-lean carries the gstack save flow only; callers point at it.
    # mutation-probe `—`: the probed artifact is a markdown skill body, outside the
    # probe tool's language set (.py/.sh/.yaml/.yml — the C-HE-30 / WR-14 rows share
    # this reason); unlike those static-doc rows, the witness EXECUTES the skill's
    # fenced blocks in a hermetic repo (merge-gate witness lens, PR #1489).
    Row(
        "WR-15",
        "shell:tools/hooks/test_skill_context_save_trim.sh",
        "phase0",
        "local + CI",
        False,
    ),
    # WR-16 (U-SR-08) — PreToolUse:Bash emit policy: zero bytes on a plain command,
    # JSON only on a rewrite or guard decision. mutation-probe `—`: zero-emission is
    # not deletion-expressible (removing lines cannot create output); the manual
    # INSERTION probe (an unconditional echo in precmd-clear-cache.sh -> red) is
    # recorded on the u-sr-08 commit.
    Row(
        "WR-16",
        "shell:tools/hooks/test_pretooluse_bash_emit_policy.sh",
        "phase0",
        "local + CI",
        False,
    ),
    # §8 R2 / U-SR-09 b4 -- rtk grep-rewrite shape guard: the two shapes rtk 0.40.0 mangles
    # deterministically deny with the `rtk proxy` re-issue; every other Bash call is silent.
    # mutation-probe True: the guard's deny path is deletion-expressible (drop a shape
    # branch -> the deny disappears -> red).
    Row("§8-R2", "shell:tools/hooks/test_rtk_shape_guard.sh", "phase0", "local + CI", True),
    # §8 R2 / U-SR-09 b5 -- the graft post-edit hook replaced by a dirty-flag-only shim
    # (graft's `check` ran 46 s inside an 8 s budget on every edit: 8.8 s for a value that
    # is always 0). mutation-probe `—`: the shim is JavaScript, outside the probe tool's
    # language set (.py/.sh/.yaml/.yml).
    Row("§8-R2", "shell:tools/hooks/test_graft_mark_dirty.sh", "phase0", "local + CI", False),
    # §8 R2 / U-SR-09 b1 -- the scoped pin theorem (pin_scope.py), pure and probed.
    Row("§8-R2", "pytest:tools/test_pin_scope.py", "phase0", "local + CI", True),
    # §8 R2 / U-SR-09 b4 -- the quote-aware shape judgement behind the guard, pure and probed.
    Row("§8-R2", "pytest:tools/test_rtk_shape_guard.py", "phase0", "local + CI", True),
    # §8.1 / §0.3 (U-HE-05)
    Row("§8.1", "pytest:tools/test_lanes_verify.py", "phase0", "local + CI", True),
    Row("§0.3", "just:mutation-probe-coverage-check", "phase0", "local + CI", False),
]


def _command(row: Row) -> list[str] | None:
    kind, _, target = row.artifact.partition(":")
    if "<" in target and ">" in target:
        # a placeholder argument (e.g. `just:lanes-pilot-report <run-id>`) is a LIVE row
        return None
    if kind == "pytest":
        return ["uv", "run", "pytest", "-q", "-rs", target]
    if kind == "shell":
        return ["bash", *target.split()]
    if kind == "just":
        return ["just", *target.split()]  # recipe + controlled args, tokenized
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
        bad = [s.strip() for s in skips if s.strip() not in row.skip_reasons]
        if bad:
            return Result(row, "fail", f"skip with unlisted reason: {bad}")
        return Result(row, "skip", ";".join(s.strip() for s in skips))
    return Result(row, "pass")


def phase0_rows() -> list[Row]:
    return [r for r in MANIFEST if r.tag == "phase0"]


def phase0_verdict(results: list[Result]) -> int:
    """0 iff every phase0 row passed. A skip is NOT a pass here (C-HE-13 §1)."""
    return 0 if all(r.status == "pass" for r in results) else 1


#: `# mutation-probe: <desc>` binds the annotated test to its default target -- the test's
#: sibling module (`tools/test_x.py` -> `tools/x.py`; `tools/hooks/test_x.sh` -> `.../x.sh`);
#: `# mutation-probe(<repo-relative path>): <desc>` names the target explicitly (a test that
#: probes another module, e.g. `test_review_wrapper.py` -> `review_wrapper_common.py`). A pin
#: counts only if its logged `file` IS that target (codex R3/R4 P2: a pin credited to the test
#: node alone could come from probing an unrelated file). The LINES the annotation names stay a
#: human contract -- the gate proves a live pin of the named file exists for the annotated test.
# An annotation binds the NEXT `def test_*`, across any decorator lines between -- including
# a multi-line `@pytest.mark.parametrize(...)` (codex u-sr-09 r5: the single-line-decorator
# form silently dropped a parametrized test from `required_probes`). A line starting with
# `def ` or `async def ` that is not a test stops the scan (the annotation is then bound
# to nothing); an `async def test_*` binds like a `def` (codex r7: the bridge had walked
# through it to the next test). The bridge accepts only DECORATOR-shaped lines -- starting
# with `@`, whitespace (a continuation), `)`, `#`, or empty -- so a class, an assignment or
# any other statement between an annotation and the next test ends the scan instead of
# carrying the annotation over to it (codex r8). The bridge and the `def` are a LOOKAHEAD:
# a match consumes only its own annotation line, so two stacked annotations above one
# test each bind it (two required targets), instead of the first swallowing the second
# (codex r9).
_ANNOT = re.compile(
    r"^# mutation-probe(?:\((?P<target>[^)]+)\))?: (?P<desc>.*)\n(?=(?:(?:[@#) \t][^\n]*)?\n)*?"
    r"(?:async )?def (?P<name>test_\w+))",
    re.M,
)
#: The `red-first` skill's form, `# mutation-probe: <path>:<lines> ...` -- a leading path:lines
#: token in the description names the target too (one grammar for both carriers).
_DESC_TARGET = re.compile(r"^(?P<path>[\w./-]+\.(?:py|sh|yaml|yml)):\d")


def _relative(token: str) -> str:
    """A logged path as REPO-relative text; a relative token is returned unchanged."""
    p = Path(token)
    if p.is_absolute():
        try:
            return str(p.relative_to(REPO))
        except ValueError:
            return token
    return token


def _sha16(path: Path) -> str | None:
    try:
        return pin_scope.digest16(path.read_bytes())
    except OSError:
        return None


def default_probe_target(test_artifact: str) -> str:
    """The module a test artifact probes by default: its sibling without the `test_` prefix."""
    p = Path(test_artifact.split("::", 1)[0])
    return str(p.with_name(p.name.removeprefix("test_")))


def _pin_is_live(e: dict, target: str, probe_file: str) -> bool:
    """A PINNED entry is evidence only while the bytes it measured are the bytes at HEAD.
    Which bytes is the row's `pin_scope` (U-SR-09 b1; `pin_scope.py` owns the theorem):
    `file` (absent -- every pre-U-SR-09 row) -- the mutated source file (`file` +
    `target_sha`) AND the test artifact (`test_sha`) must both still digest to the logged
    values (codex R2 P2); `block` -- the probed lines still occur verbatim, once, in the file
    and the test SLICE (or whole artifact, per `test_scope`) is unchanged, so an unrelated edit in
    either file no longer stales the pin ([B] F7). Entries without digests never count; an
    unknown scope never counts. The probed file must be THE annotated target (`probe_file`),
    an existing source file -- never the test artifact itself, never an unrelated module
    (codex R3/R4 P2)."""
    tsha, fsha = e.get("target_sha"), e.get("test_sha")
    if not tsha or not fsha or not e.get("file"):
        return False
    src = Path(_relative(str(e["file"])))
    test_file = Path(target.split("::", 1)[0])
    if src == test_file or str(src) != probe_file or not (REPO / src).is_file():
        return False
    scope = e.get("pin_scope", pin_scope.PIN_SCOPE_FILE)
    if scope == pin_scope.PIN_SCOPE_FILE:
        return _sha16(REPO / src) == tsha and _sha16(REPO / test_file) == fsha
    pin = pin_scope.BlockPin.from_row(e, target) if scope == pin_scope.PIN_SCOPE_BLOCK else None
    if pin is None:
        return False
    try:
        # bytes -> decode, NOT read_text(): universal newlines would turn a CRLF source's
        # block into LF and never match the producer's digest (codex u-sr-09 r4)
        return pin.live((REPO / src).read_bytes().decode("utf-8"), (REPO / test_file).read_bytes())
    except (OSError, UnicodeDecodeError):
        return False


def _pinned_nodeids(log_path: Path) -> set[tuple[str, str]]:
    """`(test target, probed file)` pairs of LIVE PINNED probes (rc 0 + digests still matching
    HEAD, `_pin_is_live`): the test target is, for a pytest command, the first non-flag token
    after `pytest` that names a `.py` path (a node id or a file), normalized REPO-relative and
    compared EXACTLY; for `bash <script>` the probed script itself. The command is split on
    whitespace as logged -- the log is written by the probe tool, not typed by hand."""
    if not log_path.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("rc") != 0:
            continue
        toks = str(e["test"]).split()
        target = None
        if "pytest" in toks:
            # the producer's parser (codex u-sr-09 r7: a private `.py` scan here picked
            # `--ignore`'s value); a row with several targets binds nothing on either side
            targets = pin_scope.pytest_targets(toks)
            target = _relative(targets[0]) if len(targets) == 1 else None
        elif toks[:1] == ["bash"] and len(toks) > 1:
            target = _relative(toks[1])
        if target is None:
            continue
        probe_file = str(Path(_relative(str(e.get("file") or ""))))
        if not _pin_is_live(e, target, probe_file):
            continue
        out.add((target, probe_file))
    return out


def _annotations(path: Path) -> list[tuple[str, str | None]]:
    """`(test name, explicit target or None)` for every annotation in a test file. Explicit =
    `# mutation-probe(<path>):` or a red-first style `# mutation-probe: <path>:<lines> ...`."""
    out: list[tuple[str, str | None]] = []
    for m in _ANNOT.finditer(path.read_text()):
        target = m.group("target")
        if target is None:
            d = _DESC_TARGET.match(m.group("desc").strip())
            target = d.group("path") if d else None
        out.append((m.group("name"), target))
    return out


def required_probes(row: Row) -> list[tuple[str, str]]:
    """Every `# mutation-probe:` annotation the row's artifact carries -> `(exact node id,
    probed file)` (substring matching would let one pinned test cover a whole file). A node-id
    artifact requires exactly itself (its annotation's target, or the default); a shell
    artifact requires the script itself against its sibling module."""
    if not row.mutation_probe:
        return []
    kind, _, target = row.artifact.partition(":")
    if kind == "shell":
        script = target.split()[0]
        path = REPO / script
        # A shell suite has no `def test_` for _ANNOT to bind, so a file-level
        # red-first-form line (`# mutation-probe: <path>:<lines> ...`) names the probed
        # file explicitly -- required when the sibling default is underivable
        # (test_permission_guard.sh probes permission-guard.sh, a hyphenated name the
        # `test_`-strip cannot reach). First annotation wins; sibling default otherwise.
        # (Flat loop, no guarding `if`: the scan must stay deletion-expressible for the
        # mutation probe -- commenting it out falls through to the sibling default.
        # EVERY annotation is collected, per this function's every-annotation contract --
        # returning on the first one let a second annotated target go unprobed, codex r8.)
        found: list[tuple[str, str]] = []
        for line in path.read_text().splitlines() if path.exists() else []:
            if line.startswith("# mutation-probe: "):
                d = _DESC_TARGET.match(line.removeprefix("# mutation-probe: ").strip())
                if d and (script, d.group("path")) not in found:
                    found.append((script, d.group("path")))
        return found or [(script, default_probe_target(script))]
    if kind != "pytest":
        return [(target, target)]
    file_part, _, node = target.partition("::")
    path = REPO / file_part
    if not path.exists():
        # not yet landed: a gap until the file exists and its probes are pinned
        return [(target, default_probe_target(file_part))]
    annots = _annotations(path)
    if node:
        explicit = next((t for n, t in annots if n == node), None)
        return [(target, explicit or default_probe_target(file_part))]
    if not annots:
        # a mutation-probe row whose file carries NO annotation is a gap, not a vacuous pass
        # (codex R3 P2: deleting every annotation must not turn the gate green)
        return [(f"{file_part}::<no mutation-probe annotations>", default_probe_target(file_part))]
    return [(f"{file_part}::{n}", t or default_probe_target(file_part)) for n, t in annots]


def coverage_gaps(log_path: Path | None = None) -> list[tuple[Row, str]]:
    """`log_path` resolves to PROBE_LOG AT CALL TIME (never bound at def time) so a
    monkeypatched log is honoured and `main` never silently reads the tracked log in a test.
    Each gap names the required node id and the file its pin must have probed."""
    pinned = _pinned_nodeids(log_path or PROBE_LOG)
    gaps: list[tuple[Row, str]] = []
    for r in MANIFEST:
        for node, probe_file in required_probes(r):
            if (node, probe_file) not in pinned:
                gaps.append((r, f"{node} [probe of {probe_file}]"))
    return gaps


def probe_result_verdict(log_path: Path | None = None) -> tuple[str, str]:
    """C-HE-22 §8.1 "result row required before pilots", made machine-checkable (U-HE-35
    codex r10 P1): the LATEST `probe-result` row's verdict on the gate log, or
    ("absent", ...) when the live probe has never completed a run. Consumed fail-closed
    by `just pilot-gate-check` — absent and RED both refuse; only GREEN admits pilots
    (C-HE-13 §2 order: probe -> coalescing -> pilots)."""
    import finding_record as fr

    rows = [r for r in fr.read_rows(log_path) if r.get("finding_type") == "probe-result"]
    if not rows:
        return "absent", "no probe-result row on the gate log (C-HE-22: probe not run)"
    evidence = json.loads(rows[-1]["observed_evidence"])
    return evidence.get("verdict", "absent"), evidence.get("why", "")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = args[0] if args else "verify"
    if mode not in ("verify", "phase0", "coverage", "pilot-gate"):
        print("usage: lanes_verify.py [verify|phase0|coverage|pilot-gate]", file=sys.stderr)
        return 2
    if mode == "pilot-gate":
        verdict, why = probe_result_verdict()
        print(f"pilot-gate: probe-result {verdict} — {why}")
        return 0 if verdict == "GREEN" else 1
    if mode == "coverage":
        gaps = coverage_gaps()
        for row, node in gaps:
            print(f"UNPROBED {row.contract} {node}")
        print(f"mutation-probe coverage: {len(gaps)} unprobed annotation(s)")
        return 1 if gaps else 0
    rows = phase0_rows() if mode == "phase0" else MANIFEST
    results = [run_row(r) for r in rows]
    for r in results:
        tail = f" — {r.reason}" if r.reason else ""
        print(f"{r.status.upper():5} {r.row.contract:14} {r.row.artifact}{tail}")
    if mode == "phase0":
        return phase0_verdict(results)
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
