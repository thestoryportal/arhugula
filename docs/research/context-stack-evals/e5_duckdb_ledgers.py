"""E5 — Can DuckDB reproduce the ledger analytics the repo hand-parses in Python?

Two checks. (1) arc-metrics.jsonl: reproduce the lever report's per-arc-type cohort sizes
and median review_rounds with SQL, and diff against `tools/arc_lever_report.py --json`.
(2) forward-register.yaml: load into DuckDB and answer register questions in one query
each (rows by status; open rows older than 60 days). Timings recorded.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import duckdb
import yaml

# [LAW:one-source-of-truth] the checkout this file lives in, not a hard-coded home
REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).with_name("E5-results.md")
LEVERS = ("B-211", "B-212")


def main() -> None:
    con = duckdb.connect()
    lines = ["# E5 — DuckDB over the ledgers", ""]

    # (1) arc-metrics
    t = time.perf_counter()
    con.execute(
        f"create table m as select * from read_json_auto('{REPO}/.harness/arc-metrics.jsonl')"
    )
    cols = [c[0] for c in con.execute("describe m").fetchall()]
    t_load = time.perf_counter() - t
    t = time.perf_counter()
    sql = f"""
      with f as (select * from m where coalesce(record_kind,'arc')='arc' and arc_type is not null),
      cohort as (select *,
        case when list_has_any(levers_active, {list(LEVERS)}) then 'treated'
             else 'baseline' end as cohort from f)
      select arc_type, cohort, count(*) n,
             median(review_rounds) med_rounds, median(p1_rounds) med_p1
      from cohort group by 1,2 order by 1,2"""
    try:
        agg = con.execute(sql).fetchall()
    except Exception as e:
        agg = [("ERROR", str(e)[:200])]
    t_q = time.perf_counter() - t
    tool = subprocess.run(
        ["uv", "run", "python", "tools/arc_lever_report.py", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    lines += [
        "## (1) arc-metrics.jsonl → cohort medians",
        "",
        f"Columns seen: {', '.join(cols)}",
        f"Load {t_load * 1000:.0f} ms; cohort query {t_q * 1000:.0f} ms.",
        "",
        "DuckDB (arc_type, cohort, n, median review_rounds, median p1_rounds):",
        "",
        *[f"- {row}" for row in agg],
        "",
        "Tool `arc_lever_report.py --json` (head, for side-by-side comparison):",
        "",
        "```",
        (tool.stdout or tool.stderr)[:2500],
        "```",
        "",
    ]

    # (2) forward-register.yaml
    t = time.perf_counter()
    data = yaml.safe_load((REPO / ".harness/forward-register.yaml").read_text())
    rows = (
        data.get("rows")
        or data.get("entries")
        or next((v for v in data.values() if isinstance(v, list)), [])
    )
    t_yaml = time.perf_counter() - t
    tmp = Path(__file__).with_name("_register.json")
    tmp.write_text(json.dumps(rows))
    t = time.perf_counter()
    con.execute(
        f"create table r as select * from read_json_auto('{tmp}', maximum_object_size=50000000)"
    )
    rcols = [c[0] for c in con.execute("describe r").fetchall()]
    by_status = con.execute("select status, count(*) from r group by 1 order by 2 desc").fetchall()
    datecol = next(
        (c for c in rcols if "date" in c.lower() or "minted" in c.lower() or "opened" in c.lower()),
        None,
    )
    stale = (
        con.execute(
            "select count(*) from r where status in "
            "('registered_finding','open','operator_gated') "
            f"and try_cast({datecol} as date) < current_date - interval 60 day"
        ).fetchone()[0]
        if datecol
        else "n/a (no date column)"
    )
    t_q2 = time.perf_counter() - t
    tmp.unlink()
    lines += [
        "## (2) forward-register.yaml → register questions",
        "",
        f"Rows: {len(rows)}; YAML parse {t_yaml:.2f}s; "
        f"DuckDB load+queries {t_q2 * 1000:.0f} ms; columns: {', '.join(rcols[:14])}…",
        "",
        "Rows by status:",
        "",
        *[f"- {s}: {c}" for s, c in by_status],
        "",
        f"Open-class rows older than 60 days by `{datecol}`: {stale}",
    ]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
