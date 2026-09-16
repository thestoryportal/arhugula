"""The defect-class-preflight skill's `refresh-classes.py`: the corpus report and the
`classify` verb the review-loop gate consumes at sweep template/attest time.

Behavioural contract only ([LAW:behavior-not-structure]): JSON list in, finding_id →
class-name list out; malformed input and unknown verbs exit non-zero on stderr, never
an empty map that would read as 'every finding unmatched'."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude/skills/defect-class-preflight/scripts/refresh-classes.py"
)


def _run(*argv: str, stdin: str = "", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_classify_names_every_matching_class_and_empty_for_unmatched():
    rows = [
        {
            "finding_id": "a:1",
            "observed_evidence": "a race between two writers; the timeout is never retried",
            "location": "x.py:1",
        },
        {"finding_id": "a:2", "observed_evidence": "the widget frobnicates", "location": ""},
    ]
    proc = _run("classify", stdin=json.dumps(rows))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert set(out) == {"a:1", "a:2"}
    assert "1 race / TOCTOU / atomicity / lock" in out["a:1"]
    assert "5 timeout / retry / budget" in out["a:1"]
    assert out["a:2"] == []


def test_classify_reads_the_location_too():
    # the location carries file-shaped vocabulary the row text may lack
    rows = [{"finding_id": "b:1", "observed_evidence": "", "location": "tools/conftest.py fixture"}]
    out = json.loads(_run("classify", stdin=json.dumps(rows)).stdout)
    assert "10 fixture scope / lifecycle" in out["b:1"]


def test_classify_malformed_input_is_a_loud_nonzero_exit():
    for bad in ("not json", "[{}]", '[{"finding_id": 1, "observed_evidence": 2}]', "{}"):
        proc = _run("classify", stdin=bad)
        assert proc.returncode == 2, bad
        assert "malformed input" in proc.stderr
        assert proc.stdout == ""


def test_unknown_verb_refuses():
    proc = _run("bogus")
    assert proc.returncode == 2
    assert "usage" in proc.stderr


def test_report_counts_agree_with_classify(tmp_path: Path):
    # one authority: the report's per-class tally and the verb's answer derive from
    # the same function, so a row the verb calls unmatched is one the report lists
    log = tmp_path / ".harness" / "merge-gate-log.jsonl"
    log.parent.mkdir()
    rows = [
        {"record_kind": "finding", "observed_evidence": "swallowed via 2>/dev/null", "ts": "2026"},
        {"record_kind": "finding", "observed_evidence": "the widget frobnicates", "ts": "2026"},
        {"record_kind": "no_finding", "observed_evidence": "", "ts": "2026"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    # the report resolves its log from the script's own location: run a copy
    # planted at the same relative path under the temp repo
    copy = tmp_path / SCRIPT.relative_to(SCRIPT.parents[4])
    copy.parent.mkdir(parents=True)
    copy.write_text(SCRIPT.read_text())
    proc = subprocess.run([sys.executable, str(copy)], capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "2 findings in" in proc.stdout
    assert "    1  3 silent failure / fallback" in proc.stdout
    assert "Unmatched findings (new-class candidates): 1" in proc.stdout
    assert "the widget frobnicates" in proc.stdout


def test_every_class_row_has_its_skill_section_and_vice_versa():
    # the table and the author-facing checklist are two carriers of one class list
    # (codex r1 P2 on the intake arc): a row landed in one without the other is the
    # drift the skill's own docstring forbids, so it fails here rather than in review
    import re
    import runpy

    table = runpy.run_path(str(SCRIPT))["CLASSES"]
    table_numbers = {name.split(" ", 1)[0] for name in table}
    skill = (SCRIPT.parents[1] / "SKILL.md").read_text()
    section_numbers = set(re.findall(r"^### (\d+)\. ", skill, re.M))
    assert table_numbers == section_numbers
