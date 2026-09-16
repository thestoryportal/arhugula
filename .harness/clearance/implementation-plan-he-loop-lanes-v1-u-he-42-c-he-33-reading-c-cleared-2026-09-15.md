---
artifact: .harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md
version: v1.0 + rev 2026-09-15 (U-HE-42 execution correction, as-built — the C-HE-33 §4 outcome-measure line at :7473 and the C-HE-28 coverage row at :7838)
cleared_at: 2026-09-15T20:55:00Z
clearance_type: execution-correction-H_E-tooling
back_reference:
  - ".harness/class_1_fork_c_he_33_outcome_measure_mandates_an_input_that_does_not_exist.md (the Class 1 fork this rev absorbs; ratified under Reading C — Reading A withdrawn and recorded, not overwritten)"
  - ".harness/spec/Spec_HE_Loop_Lanes_v1.md (C-HE-33 §3 and §4, C-HE-28 §4 — the contract text is UNCHANGED by this rev; the plan line moved which unit owns the §4 measure, not what the measure is)"
  - ".harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md (:7473 — U-HE-42 no longer builds the ≥6-CI-run / CANCELLED-share measure; :7838 — C-HE-28 §4 outcome-measure cohorts recorded UNOWNED against B-245)"
  - "tools/lanes_verify.py Row \"C-HE-32/33 §3\" (PR #1527 — the scoped witness this rev licenses; an unscoped row would read as a green witness over an absent contract part)"
  - ".harness/clearance/implementation-plan-he-loop-lanes-v1-u-he-48-witness-correction-cleared-2026-08-27.md (the marker shape this one follows — every as-built revision to this plan file carries one; merge-gate r2 spec-conformance P2 on #1527 named the gap)"
---

What changed and why it is a correction, not an extension:

The plan at :7473 assigned U-HE-42 the C-HE-33 §4 outcome measure (≥ 6-CI-run
branch share; CANCELLED-run share) as two `summary` lines over `ci_runs`, while
:7452 already said the same measure is tracked "via C-HE-28 cohorts". The
input the §4 measure needs does not exist: `ci_metrics(row.merge_sha)` counts
runs on the merge commit and stores no per-run conclusion, so neither share has
a source (`tools/arc_metrics.py` 190-197, 528-574). That contradiction is the
Class 1 fork filed alongside this marker, ratified under Reading C: the measure
belongs to C-HE-28 §4, U-HE-42 ships the §3 parity mechanism only, and the §4
cohorts are recorded UNOWNED against the owed B-245 register row rather than
silently absorbed as a green witness.

No contract number, field name, schema, or §6 ordering changes; the spec text
of C-HE-33 and C-HE-28 is untouched; the plan's unit decomposition and
sequencing are untouched. Operator may reverse by a dated plan note.
