# E5 — DuckDB over the ledgers

## (1) arc-metrics.jsonl → cohort medians

Columns seen: additions, arc_id, arc_span_s, arc_type, captured_at, ci_runs, ci_wall_s, commits, created_at, decision_count, deletions, files, first_round_at, last_round_at, levers_active, merge_sha, merged_at, notes, p1_rounds, pr, provenance, review_rounds, round_completeness, round_log_source, round_wall_s, total_arc_wall_s, arc_type_close, arc_type_declared_at, arc_type_open, base_sha, concurrent_lanes_at_open, concurrent_lanes_max, concurrent_lanes_min, config_hash, head_sha, lane_id, phases, prompt_version, record_kind, reviewer_identity, round_outcomes, cost_main_calls, cost_main_iet, cost_source, cost_subagent_calls, cost_subagent_iet
Load 22 ms; cohort query 4 ms.

DuckDB (arc_type, cohort, n, median review_rounds, median p1_rounds):

- ('applying', 'baseline', 25, 6.0, [])
- ('applying', 'treated', 2, 6.0, [])
- ('inventing', 'baseline', 19, 10.0, [1, 2, 4, 7, 8, 9])
- ('inventing', 'treated', 2, 10.0, [])

Tool `arc_lever_report.py --json` (head, for side-by-side comparison):

```
{
  "target_levers": [
    "B-211",
    "B-212"
  ],
  "arc_types": {
    "applying": {
      "evaluable_for_lever_decision": true,
      "non_evaluable_reason": null,
      "pattern_metrics": {
        "B-211+B-212": {
          "n": 1,
          "median_rounds": 10,
          "median_p1": 2,
          "p1_measured_n": 1,
          "median_cost_miet": null,
          "cost_measured_n": 0,
          "cost_arcs": []
        },
        "(none)": {
          "n": 13,
          "median_rounds": 3,
          "median_p1": 0,
          "p1_measured_n": 13,
          "median_cost_miet": 5.8,
          "cost_measured_n": 9,
          "cost_arcs": [
            {
              "arc_id": "b-230-register",
              "cost_miet": 5.84
            },
            {
              "arc_id": "b-230-task-0",
              "cost_miet": 7.41
            },
            {
              "arc_id": "b-230-task-3",
              "cost_miet": 3.16
            },
            {
              "arc_id": "b-230-task-4",
              "cost_miet": 3.62
            },
            {
              "arc_id": "b-230-task-6",
              "cost_miet": 2.91
            },
            {
              "arc_id": "u-sr-04",
              "cost_miet": 15.42
            },
            {
              "arc_id": "u-sr-05",
              "cost_miet": 6.79
            },
            {
              "arc_id": "u-sr-09-arc-cost-synthetic",
              "cost_miet": 3.18
            },
            {
              "arc_id": "u-sr-09",
              "cost_miet": 44.48
            }
          ]
        },
        "defect-class-preflight+register-pr-prose+review-loop-gate": {
          "n": 2,
          "median_rounds": 10.0,
          "median_p1": 4.0,
          "p1_measured_n": 2,
          "median_cost_miet": 8.7,
          "cost_measured_n": 1,
          "cost_arcs": [
            {
              "arc_id": "u-he-50",
              "cost_miet": 8.71
            }
          ]
        },
        "B-230": {
          "n": 1,
          "median_rounds": 1,
          "median_p1": 0,
          "p1_measured_n": 1,
          "median_cost_miet": null,
          "cost_measured_n": 0,
          "cost_arcs": []
        },
        "U-HE-51": {
          "n": 1,
          "median_rounds": 10,
          "median_p1": 2,
          "p1_measured_n": 1,
          "median_cost_miet": 34.3,
          "cost_measured_n": 1,
          "cost_arcs": [
            {
              "arc_id": "u-he-36",
              "cost_miet": 34.28
```

## (2) forward-register.yaml → register questions

Rows: 242; YAML parse 1.24s; DuckDB load+queries 13 ms; columns: id, title, status, summary, pr, heading, close_out, council, filing, notes, grounded_2026_08_11, build_state_2026_08_11, closed_2026_08_11…

Rows by status:

- closed: 151
- registered_finding: 84
- held: 3
- design_substrate_gated: 3
- open: 1

Open-class rows older than 60 days by `None`: n/a (no date column)