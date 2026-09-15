"""C-HE-15/16/17 fail-closed review wrapper battery. CLIs are mocked; no skip."""

from __future__ import annotations

import json
import sys
from itertools import count
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import finding_record as fr
import review_loop_gate as rlg
import review_wrapper_common as rw


@pytest.fixture(autouse=True)
def _no_live_reviewers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Nothing in this battery may reach a real reviewer, the tracked gate log, or the live HITL
    ledger: `run_bounded` fails loudly unless a test installs its own fake, the gate log is a
    tmp file (module attribute + subprocess env), and HITL routing is a recorder."""
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "gate-log.jsonl")
    monkeypatch.setenv("HARNESS_GATE_LOG", str(tmp_path / "gate-log.jsonl"))
    monkeypatch.setattr(
        cr, "run_bounded", lambda *a, **k: pytest.fail(f"real reviewer subprocess: {a[0][:3]}")
    )
    monkeypatch.setattr(cr, "_route_to_hitl", lambda arc_id, reason, **kw: None)
    # B-215 gate: hermetically INACTIVE for this battery (it targets C-HE-15/16/17
    # behavior, not admission) — never a function of the ambient loop marker or the
    # real reservation store
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: False)
    # ...and off the REAL checkout's state file: joining root with an absolute
    # STATE_REL yields the absolute path, so cwd never reaches the live state
    monkeypatch.setattr(rlg, "STATE_REL", tmp_path / "review-gate-state.json")
    # in-process runs are not git checkouts at EXPECTED's head: pin HEAD and the tree export
    monkeypatch.setattr(cr, "_head", lambda repo: EXPECTED["head_sha"])
    monkeypatch.setattr(
        cr,
        "export_bound_tree",
        lambda repo, head, base, dest: (dest / "tree").mkdir(parents=True, exist_ok=True),
    )


EXPECTED = {
    "head_sha": "a" * 40,
    "base_sha": "b" * 40,
    "diff_digest": "c" * 64,
    "reviewer_identity": "codex-review",
    "prompt_version": "pv1",
    "config_hash": "ch1",
}


def _block(verdict="APPROVE", findings=None, **over):
    body = {"verdict": verdict, "findings": findings or [], **EXPECTED, **over}
    return "chatter\n```json\n" + json.dumps(body) + "\n```\ntrailer\n"


# ── C-HE-15 §1/§2: only a positive schema parse counts ──────────────────────
# mutation-probe(tools/review_wrapper_common.py): make parse_verdict() approve empty text
def test_empty_stdout_exit0_is_unavailable():
    out = rw.parse_verdict("codex", "", EXPECTED)
    assert out.terminal == "REVIEWER_UNAVAILABLE"
    assert rw.parse_verdict("codex", "   \n", EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_truncated_json_is_unavailable():
    text = _block()[:-12]  # cut inside the fence
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_no_fenced_block_is_unavailable():
    assert (
        rw.parse_verdict("codex", "VERDICT: APPROVE", EXPECTED).terminal == "REVIEWER_UNAVAILABLE"
    )


def test_out_of_enum_severity_is_unavailable():
    text = _block("BLOCK", [{"severity": "P0", "location": "x", "message": "m"}])
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_extra_property_on_finding_is_unavailable():
    text = _block("BLOCK", [{"severity": "P1", "location": "x", "message": "m", "extra": 1}])
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_extra_top_level_property_is_unavailable():
    assert rw.parse_verdict("codex", _block(note="x"), EXPECTED).terminal == "REVIEWER_UNAVAILABLE"


def test_blank_location_or_message_is_unavailable():
    """A finding row must be grounded (C-HE-24 §1); an ungrounded reviewer finding fails the
    channel schema at parse time rather than the record at write time."""
    for f in (
        {"severity": "P1", "location": "", "message": "m"},
        {"severity": "P1", "location": "x", "message": ""},
    ):
        assert (
            rw.parse_verdict("codex", _block("BLOCK", [f]), EXPECTED).terminal
            == "REVIEWER_UNAVAILABLE"
        )


def test_verdict_findings_consistency_is_enforced_by_schema():
    """Codex round-1 P2 on this arc: BLOCK with no findings / APPROVE with findings are
    contradictory results and must not parse."""
    assert (
        rw.parse_verdict("codex", _block("BLOCK", []), EXPECTED).terminal == "REVIEWER_UNAVAILABLE"
    )
    text = _block("APPROVE", [{"severity": "P3", "location": "x", "message": "m"}])
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "REVIEWER_UNAVAILABLE"
    for ch in ("codex", "gemini", "merge-gate"):
        assert len(rw.load_schema(ch)["allOf"]) == 2


def test_well_formed_parses():
    out = rw.parse_verdict(
        "codex", _block("BLOCK", [{"severity": "P1", "location": "x", "message": "m"}]), EXPECTED
    )
    assert out.terminal == "BLOCK" and len(out.findings) == 1 and out.binding == EXPECTED
    assert out.source == "stdout" and out.failure_class is None


def test_two_distinct_fenced_blocks_are_ambiguous_but_identical_repeats_are_one():
    """C-HE-15 §2: ambiguous output -> REVIEWER_UNAVAILABLE (a later APPROVE never overrides an
    earlier BLOCK; codex round 4). The session artifact repeats the final message in several
    envelopes, so byte-identical blocks collapse to one."""
    text = _block("BLOCK", [{"severity": "P1", "location": "x", "message": "m"}]) + _block(
        "APPROVE"
    )
    out = rw.parse_verdict("codex", text, EXPECTED)
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "ambiguous" in out.reason
    assert (
        rw.parse_verdict("codex", _block() + "\nnoise\n" + _block(), EXPECTED).terminal == "APPROVE"
    )


# ── C-HE-15 §3/§4: binding byte-compare, all six fields ─────────────────────
_SHAPE_VALID_OTHER = {
    "head_sha": "d" * 40,
    "base_sha": "e" * 40,
    "diff_digest": "f" * 64,
    "reviewer_identity": "gemini-review",
    "prompt_version": "pv2",
    "config_hash": "ch2",
}


@pytest.mark.parametrize("field", rw.BINDING_FIELDS)
def test_binding_mismatch_is_unavailable(field):
    """Every binding field is byte-compared against the wrapper's OWN value: a shape-valid but
    foreign value is refused with the field named (reviewer_identity is a schema `const`, so
    the schema refuses it one step earlier -- still REVIEWER_UNAVAILABLE)."""
    text = _block(**{field: _SHAPE_VALID_OTHER[field]})
    out = rw.parse_verdict("codex", text, EXPECTED)
    assert out.terminal == "REVIEWER_UNAVAILABLE"
    if field == "reviewer_identity":
        assert out.reason.startswith("schema:")
    else:
        assert f"binding mismatch on {field}" in out.reason
    # a shape-INVALID value is refused too (by the schema), never accepted
    assert rw.parse_verdict("codex", _block(**{field: "different"}), EXPECTED).terminal == (
        "REVIEWER_UNAVAILABLE"
    )


def test_binding_mismatch_on_shape_valid_value_is_caught_by_byte_compare():
    """A value that satisfies the schema's pattern but is not THIS invocation's value."""
    out = rw.parse_verdict("codex", _block(head_sha="d" * 40), EXPECTED)
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "binding mismatch on head_sha" in out.reason


def test_schema_requires_all_six_binding_fields():
    schema = rw.load_schema("codex")
    assert set(rw.BINDING_FIELDS) <= set(schema["required"])


# ── U-HE-03: per-channel schema shape ────────────────────────────────────────
@pytest.mark.parametrize("channel", ["codex", "gemini", "merge-gate"])
def test_channel_schema_shape(channel):
    s = rw.load_schema(channel)
    assert s["additionalProperties"] is False
    assert s["properties"]["verdict"]["enum"] == ["APPROVE", "BLOCK"]
    item = s["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["severity"]["enum"] == ["P1", "P2", "P3"]
    assert set(rw.BINDING_FIELDS) <= set(s["required"])


def test_channel_schema_pins_reviewer_identity():
    assert rw.load_schema("codex")["properties"]["reviewer_identity"] == {"const": "codex-review"}
    assert rw.load_schema("gemini")["properties"]["reviewer_identity"] == {"const": "gemini-review"}
    mg = rw.load_schema("merge-gate")["properties"]["reviewer_identity"]
    assert mg["pattern"] == "^merge-gate-[a-z-]+$"


# ── C-HE-16 §4: classifier table, row by row; unknown → transient ───────────
@pytest.mark.parametrize(
    "channel,text,expected",
    [
        ("codex", "codex-cli requires a newer version of Codex", "permanent"),
        ("codex", "Error: not logged in", "permanent"),
        ("codex", "HTTP 401 Unauthorized", "permanent"),
        ("codex", "HTTP 403 Forbidden", "permanent"),
        ("codex", "bash: codex: command not found", "permanent"),
        ("codex", "rate limit exceeded (429)", "transient"),
        ("codex", "read ETIMEDOUT", "transient"),
        ("codex", "ECONNRESET", "transient"),
        ("codex", "command timed out after 550 seconds", "transient"),
        ("gemini", "antigravity CLI not logged in", "permanent"),
        ("gemini", "Antigravity is not installed", "permanent"),
        ("gemini", "ERROR: agy (Antigravity CLI) not found on PATH.", "permanent"),
        ("gemini", "RESOURCE_EXHAUSTED", "transient"),
        ("gemini", "deadline exceeded", "transient"),
        ("codex", "some brand new vendor error text", "transient"),
        ("codex", "", "transient"),
        ("gemini", "not logged in", "transient"),  # a codex-only row never fires for gemini
    ],
)
def test_classifier_table(channel, text, expected):
    assert rw.classify(channel, text) == expected


def test_classifier_table_shape_is_one_row_per_channel_regex_class():
    for ch, rx, cls in rw.CLASSIFIER:
        assert ch in ("codex", "gemini") and cls in ("permanent", "transient")
        assert hasattr(rx, "search")


# ── C-HE-16 §2/§3: retry parameters ─────────────────────────────────────────
def test_retry_constants():
    assert (
        rw.PER_ATTEMPT_TIMEOUT_S,
        rw.MAX_ATTEMPTS,
        rw.TOTAL_BUDGET_S,
        rw.SECOND_ATTEMPT_MARGIN_S,
    ) == (1200.0, 2, 1260.0, 30.0)  # spec v1.2 X2: the per-attempt figure is a 1200 s cap
    assert rw.TERMINAL_STATES == ("APPROVE", "BLOCK", "REVIEWER_UNAVAILABLE")


def test_permanent_failure_skips_retry():
    calls = count()

    def invoke(timeout):
        next(calls)
        return rw.Attempt(stdout="", stderr="Error: not logged in", returncode=0, timed_out=False)

    out = rw.run_with_retry(
        invoke, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "permanent"
    assert next(calls) == 1  # exactly one attempt made


def test_auth_error_text_with_exit0_is_permanent_unavailable():
    """C-HE-15 verification row: auth-error text + exit 0 → REVIEWER_UNAVAILABLE(permanent)."""

    def invoke(timeout):
        return rw.Attempt(
            stdout="Error: not logged in. Run codex login.",
            stderr="",
            returncode=0,
            timed_out=False,
        )

    out = rw.run_with_retry(
        invoke, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "permanent"


def test_transient_then_success_uses_two_attempts_and_dynamic_second_timeout():
    seen: list[float] = []
    now = {"t": 0.0}

    def invoke(timeout):
        seen.append(timeout)
        now["t"] += 800.0  # first attempt burned 800 s of the 1260 s budget
        if len(seen) == 1:
            return rw.Attempt(
                stdout="", stderr="", returncode=0, timed_out=False
            )  # empty first attempt
        return rw.Attempt(stdout=_block(), stderr="", returncode=0, timed_out=False)

    out = rw.run_with_retry(
        invoke,
        channel="codex",
        expected=EXPECTED,
        deadline=rw.TOTAL_BUDGET_S,
        clock=lambda: now["t"],
    )
    assert out.terminal == "APPROVE"
    assert seen[0] == rw.PER_ATTEMPT_TIMEOUT_S == 1200.0  # min(1200, 1260)
    # attempt 2 timeout = min(1200, remaining - 30) = min(1200, 1260-800-30) = 430
    assert seen[1] == pytest.approx(430.0)


def test_empty_on_second_attempt_is_unavailable_transient():
    n = count()

    def invoke(timeout):
        next(n)
        return rw.Attempt(stdout="", stderr="", returncode=0, timed_out=False)

    out = rw.run_with_retry(
        invoke, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "transient"
    assert next(n) == 2  # never a third attempt (max_attempts = 2)


def test_timeout_is_transient_but_never_re_invoked():
    """Spec v1.2 X2: the retry exists for FAST failures; after a cap-length attempt only a stub
    of budget remains (codex round 4)."""
    n = count()

    def invoke(timeout):
        next(n)
        return rw.Attempt(stdout="", stderr="", returncode=None, timed_out=True)

    out = rw.run_with_retry(
        invoke, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "transient"
    assert "timed out" in out.reason and out.reason.startswith("HITL-recoverable") and next(n) == 1


def test_budget_exhaustion_is_hitl_recoverable():
    def invoke(timeout):
        return rw.Attempt(stdout="", stderr="", returncode=None, timed_out=True)

    out = rw.run_with_retry(
        invoke, channel="codex", expected=EXPECTED, deadline=1.0, clock=lambda: 5.0
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.reason.startswith("HITL-recoverable")
    assert out.failure_class == "transient"
    assert out.binding == EXPECTED  # an unavailable outcome is still bound to its invocation


def test_exhausted_budget_never_invokes_the_channel():
    def invoke(timeout):
        pytest.fail("must not invoke past the deadline")

    out = rw.run_with_retry(
        invoke, channel="codex", expected=EXPECTED, deadline=1.0, clock=lambda: 5.0
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE"


# ── C-HE-17: failover chain ─────────────────────────────────────────────────
def _unavail(cls):
    return rw.ReviewOutcome("REVIEWER_UNAVAILABLE", "codex", cls, "x", [], None, None)


def test_failover_invoked_once_on_primary_unavailable_and_blocks():
    n = count()

    def failover():
        next(n)
        return rw.ReviewOutcome(
            "BLOCK",
            "gemini",
            None,
            "",
            [{"severity": "P1", "location": "l", "message": "m"}],
            EXPECTED,
            "stdout",
        )

    p, f = rw.run_with_failover(lambda: _unavail("permanent"), failover)
    assert p.terminal == "REVIEWER_UNAVAILABLE"
    assert f is not None and f.terminal == "BLOCK" and next(n) == 1
    assert rw.exit_code(f) == 1


def test_failover_unavailable_blocks_with_both_reasons():
    p, f = rw.run_with_failover(
        lambda: _unavail("permanent"),
        lambda: rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE", "gemini", "transient", "y", [], None, None
        ),
    )
    assert f is not None and rw.exit_code(p) == 2 and rw.exit_code(f) == 2
    assert p.reason == "x" and f.reason == "y"


def test_failover_not_invoked_when_primary_terminal():
    for terminal in ("APPROVE", "BLOCK"):
        primary = rw.ReviewOutcome(terminal, "codex", None, "", [], EXPECTED, "stdout")
        p, f = rw.run_with_failover(lambda p=primary: p, lambda: pytest.fail("must not run"))
        assert f is None and p.terminal == terminal


def test_exit_codes():
    assert rw.exit_code(rw.ReviewOutcome("APPROVE", "codex", None, "")) == 0
    assert rw.exit_code(rw.ReviewOutcome("BLOCK", "codex", None, "")) == 1
    assert rw.exit_code(_unavail("transient")) == 2


# ── outcome rows: reviewer_unavailable → C-HE-24 rows with fail_class ───────
def test_unavailable_outcome_row_carries_fail_class():
    rows = rw.outcome_rows(
        _unavail("permanent"),
        producer="codex_review_wrapper",
        arc_id="pr-1",
        lane_id="h-w-1",
        round_n=1,
    )
    assert (
        rows[0]["record_kind"] == "reviewer_unavailable"
        and rows[0]["finding_type"] == "permanent-fail-exit"
    )
    assert (
        rows[0]["severity"] == "hard"
        and rows[0]["cause_attribution"] == "reviewer_unavailable_permanent"
    )
    rows = rw.outcome_rows(
        _unavail("transient"),
        producer="codex_review_wrapper",
        arc_id="pr-1",
        lane_id="h-w-1",
        round_n=1,
    )
    assert rows[0]["finding_type"] == "transient-retry" and rows[0]["severity"] == "warn"
    assert "finding_id" not in rows[0]  # minted at append time, never a per-invocation ordinal


def test_outcome_rows_one_finding_row_per_finding_bound_to_head():
    out = rw.ReviewOutcome(
        "BLOCK",
        "codex",
        None,
        "",
        [
            {"severity": "P1", "location": "a.py:1", "message": "m1"},
            {"severity": "P2", "location": "b.py:2", "message": "m2"},
        ],
        EXPECTED,
        "stdout",
    )
    rows = rw.outcome_rows(
        out, producer="codex_review_wrapper", arc_id="pr-1", lane_id="h-w-1", round_n=3
    )
    assert [r["record_kind"] for r in rows] == ["finding", "finding"]
    assert [r["severity"] for r in rows] == ["P1", "P2"] and rows[0]["head_sha"] == "a" * 40
    assert rows[0]["finding_type"] == "terminal-block" and rows[0]["round_n"] == 3
    approve = rw.ReviewOutcome("APPROVE", "codex", None, "", [], EXPECTED, "stdout")
    marker = rw.outcome_rows(approve, producer="p", arc_id="a", lane_id="l", round_n=0)
    # a clean review is durable evidence too (C-HE-17 §5): ONE bound no_finding marker
    assert len(marker) == 1 and marker[0]["record_kind"] == "no_finding"
    assert marker[0]["finding_type"] == "clean-approve" and marker[0]["head_sha"] == "a" * 40
    assert marker[0]["severity"] == "info"


def test_emit_outcome_mints_distinct_ids_on_rerun_at_same_head(tmp_path: Path):
    """A rerun on the same head re-reporting the same location gets a NEW observation id
    (next_finding_id under the log lock), never a rejected core mutation."""
    log = tmp_path / "gate.jsonl"
    out = rw.ReviewOutcome(
        "BLOCK",
        "codex",
        None,
        "",
        [{"severity": "P1", "location": "a.py:1", "message": "m1"}],
        EXPECTED,
        "stdout",
    )
    first = rw.emit_outcome(
        out, producer="codex_review_wrapper", arc_id="pr-1", lane_id="h-w-1", round_n=1, path=log
    )
    out2 = rw.ReviewOutcome(
        "BLOCK",
        "codex",
        None,
        "",
        [{"severity": "P1", "location": "a.py:1", "message": "m1 again"}],
        EXPECTED,
        "stdout",
    )
    second = rw.emit_outcome(
        out2, producer="codex_review_wrapper", arc_id="pr-1", lane_id="h-w-1", round_n=2, path=log
    )
    assert first[0]["finding_id"].endswith(":1") and second[0]["finding_id"].endswith(":2")
    rows = fr.read_rows(log)
    assert len(rows) == 2 and all(r["producer"] == "codex_review_wrapper" for r in rows)
    for r in rows:
        fr.validate(r)


def test_emit_outcome_unavailable_row_uses_nohead_token(tmp_path: Path):
    log = tmp_path / "gate.jsonl"
    rows = rw.emit_outcome(
        _unavail("transient"),
        producer="codex_review_wrapper",
        arc_id="pr-1",
        lane_id="h-w-1",
        round_n=1,
        path=log,
    )
    assert (
        rows[0]["finding_id"].startswith("codex_review_wrapper:nohead:")
        and rows[0]["head_sha"] is None
    )
    assert fr.read_rows(log)[0]["record_kind"] == "reviewer_unavailable"


def test_env_arc_and_lane_never_empty_never_colon(monkeypatch):
    monkeypatch.setenv("HARNESS_ARC_ID", "pr:1")
    monkeypatch.setenv("HARNESS_LANE_ID", "host:w:1")
    assert rw.env_arc_and_lane() == ("pr_1", "host_w_1")
    monkeypatch.delenv("HARNESS_ARC_ID")
    monkeypatch.delenv("HARNESS_LANE_ID")
    arc, lane = rw.env_arc_and_lane()
    assert arc.startswith("branch-") and lane.endswith("-nolane") and ":" not in arc + lane


# ── U-HE-04: codex_review.py wrapper (C-HE-18) ───────────────────────────────
import os  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

import codex_review as cr  # noqa: E402

_REAL_ROUTE_TO_HITL = cr._route_to_hitl  # captured before the autouse fixture stubs it
_REAL_EXPORT_BOUND_TREE = cr.export_bound_tree


NONCE = "n0nce-test"


def _artifact_tree(tmp_path: Path, head: str, mtime: float, nonce: str = NONCE) -> Path:
    d = tmp_path / "2026" / "08" / "18"
    d.mkdir(parents=True)
    p = d / "rollout-2026-08-18T00-00-00-abc.jsonl"
    # real shape: the user message (the brief, carrying the invocation token) then the
    # assistant text (fenced block, newlines ESCAPED inside the string), each in a JSONL envelope
    user = {
        "type": "response_item",
        "payload": {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": f"brief (Invocation token {nonce}; not in the JSON.)",
                }
            ],
        },
    }
    p.write_text(
        json.dumps(user)
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": _block(head_sha=head)}],
                },
            }
        )
        + "\n"
    )
    os.utime(p, (mtime, mtime))
    return p


def test_artifact_text_decodes_jsonl_envelopes(tmp_path):
    p = _artifact_tree(tmp_path, "a" * 40, mtime=1.0)
    raw = p.read_text()
    decoded = cr.artifact_text(p)
    assert rw.extract_fenced_json(raw) is None  # the raw envelope hides the fence behind escaping
    assert rw.extract_fenced_json(decoded) is not None  # decoding exposes it


def test_session_artifact_discovery_newest_after_start_containing_head(tmp_path):
    old = _artifact_tree(tmp_path / "a", "a" * 40, mtime=100.0)
    hit = _artifact_tree(tmp_path / "b", "a" * 40, mtime=200.0)
    assert (
        cr.find_session_artifact("a" * 40, started_at=150.0, now=210.0, root=tmp_path / "b") == hit
    )
    assert (
        cr.find_session_artifact("a" * 40, started_at=150.0, now=210.0, root=tmp_path / "a") is None
    )
    assert old.exists()
    # a fresh artifact for a DIFFERENT head is never picked up (binding to this invocation)
    assert (
        cr.find_session_artifact("d" * 40, started_at=150.0, now=210.0, root=tmp_path / "b") is None
    )
    # a sibling invocation's artifact (same head, other token) is never consumed (codex round 9)
    assert (
        cr.find_session_artifact(
            "a" * 40, started_at=150.0, now=210.0, root=tmp_path / "b", nonce="other"
        )
        is None
    )
    assert (
        cr.find_session_artifact(
            "a" * 40, started_at=150.0, now=210.0, root=tmp_path / "b", nonce=NONCE
        )
        == hit
    )
    assert (
        cr.find_session_artifact("a" * 40, started_at=0.0, now=1.0, root=tmp_path / "missing")
        is None
    )


def test_log_frozen_but_artifact_has_verdict_parses_from_artifact(tmp_path, monkeypatch):
    """PR #1386 mode: stdout inconclusive, session artifact carries the verdict."""
    head = "a" * 40
    _artifact_tree(tmp_path, head, mtime=time.time() + 1)
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(cr, "_binding", lambda repo, base: {**EXPECTED, "head_sha": head})

    def invoke(timeout):
        return rw.Attempt(stdout="working...\n", stderr="", returncode=0, timed_out=False)

    out = cr.run_codex_review(Path("."), "main", invoke=invoke, nonce=NONCE)
    assert out.terminal == "APPROVE" and out.source == "session-artifact"


def test_artifact_with_foreign_binding_is_still_unavailable(tmp_path, monkeypatch):
    """The artifact path requires the SAME positive parse + byte-compare (C-HE-18 §2)."""
    _artifact_tree(tmp_path, "a" * 40, mtime=time.time() + 1)
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(cr, "ARTIFACT_LAG_S", 0.0)
    monkeypatch.setattr(cr, "_binding", lambda repo, base: {**EXPECTED, "head_sha": "d" * 40})
    out = cr.run_codex_review(Path("."), "main", invoke=lambda t: rw.Attempt("", "", 0, False))
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.source != "session-artifact"


def test_artifact_polling_capped_by_shared_deadline(tmp_path, monkeypatch):
    """Two 550 s attempts + artifact polling must not exceed the 1260 s budget."""
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(cr.time, "time", lambda: fake_now["t"])
    monkeypatch.setattr(cr.time, "sleep", lambda s: fake_now.__setitem__("t", fake_now["t"] + s))

    # one fake clock for both axes: the retry loop's monotonic and the wall clock the artifact
    # poll sleeps on advance together, as they do in production
    def invoke(timeout):
        fake_now["t"] += timeout
        return rw.Attempt("", "", 0, False)

    out = cr.run_codex_review(Path("."), "main", invoke=invoke, clock=lambda: fake_now["t"])
    assert out.terminal == "REVIEWER_UNAVAILABLE"
    assert fake_now["t"] - 1000.0 <= rw.TOTAL_BUDGET_S + 1e-6


def test_zero_byte_output_emits_finding_row(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "gate.jsonl")
    monkeypatch.setattr(cr, "ARTIFACT_LAG_S", 0.0)
    rc = cr.main(["--base", "main", "--invoke-test-empty"])
    assert rc == 2
    rows = fr.read_rows(tmp_path / "gate.jsonl")
    assert rows and rows[0]["producer"] == "codex_review_wrapper"
    assert rows[0]["record_kind"] == "reviewer_unavailable"
    assert rows[0]["finding_type"] == "transient-retry" and rows[0]["head_sha"] == "a" * 40
    fr.validate(rows[0])


def test_fallback_arc_id_is_reservable_and_injective(monkeypatch):
    """codex r19/r20 P2: the branch-derived fallback arc id must be a single reservable
    path component AND injective -- feat/x and feat-x must not collapse to one arc."""
    monkeypatch.delenv("HARNESS_ARC_ID", raising=False)
    monkeypatch.setattr(rw, "_git", lambda cwd, *a: "feat/he-lanes-s4b-u-he-17")
    a1, _ = rw.env_arc_and_lane()
    assert "/" not in a1 and ":" not in a1
    monkeypatch.setattr(rw, "_git", lambda cwd, *a: "feat-he-lanes-s4b-u-he-17")
    a2, _ = rw.env_arc_and_lane()
    assert a1 != a2  # injective: the digest suffix separates slug collisions


def test_gate_lens_rounds_stay_producer_scoped(tmp_path, monkeypatch):
    """codex r19: one merge-gate pass emits three lenses sequentially with round_n=None --
    each lens is its own producer, so all three must land at the SAME round number."""
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    monkeypatch.delenv("HARNESS_ROUND_N", raising=False)
    out = rw.ReviewOutcome("APPROVE", "merge-gate", None, "", [], EXPECTED, "stdout")
    rounds = [
        rw.emit_outcome(out, producer=lens, arc_id="a1", lane_id="l", round_n=None)[0]["round_n"]
        for lens in (
            "merge-gate-concurrency",
            "merge-gate-spec-conformance",
            "merge-gate-witness-adequacy",
        )
    ]
    assert rounds == [1, 1, 1]


def test_wrapper_persists_round_outcome_on_reservation(monkeypatch):
    calls = []
    stub = types.SimpleNamespace(
        current=lambda arc_id: (1, {"state": "open"}),
        record_round_outcome=lambda arc_id, n, **kw: calls.append((arc_id, n, kw)),
        RoundOutcomeConflict=type("RoundOutcomeConflict", (RuntimeError,), {}),
    )
    monkeypatch.setitem(sys.modules, "reservations", stub)
    rw.record_round_outcome_if_reserved(
        "pr-1", 2, channel="codex", terminal="REVIEWER_UNAVAILABLE", finding_count=0
    )
    # the caller's round_n is preserved when free, so the folded reservation joins to the
    # gate-log round (codex round-7 P2)
    assert calls == [
        ("pr-1", 2, {"channel": "codex", "terminal": "REVIEWER_UNAVAILABLE", "finding_count": 0})
    ]
    # no reservation for this arc → no-op
    stub.current = lambda arc_id: None
    rw.record_round_outcome_if_reserved(
        "pr-2", 1, channel="codex", terminal="APPROVE", finding_count=0
    )
    assert len(calls) == 1


def test_wrapper_failover_collision_persists_both_channels(tmp_path, monkeypatch, capsys):
    """codex round-5 P2 (real reservations module): the codex leg records round 1
    REVIEWER_UNAVAILABLE; the gemini failover leg also carries round 1 (per-producer
    gate-log rounds) -- the wrapper lands it at the next free arc-level key instead of
    dropping it. Both channels persist; nothing is erased."""
    import reservations as real_rs

    q = tmp_path / "queue"
    q.mkdir()
    monkeypatch.setattr(real_rs, "QUEUE_DIR", q)
    monkeypatch.setitem(sys.modules, "reservations", real_rs)
    real_rs.reserve("pr-fo", lane_id="A", branch="b", arc_type="inventing")
    rw.record_round_outcome_if_reserved(
        "pr-fo", 1, channel="codex", terminal="REVIEWER_UNAVAILABLE", finding_count=0
    )
    rw.record_round_outcome_if_reserved(
        "pr-fo", 1, channel="gemini", terminal="BLOCK", finding_count=3
    )
    assert capsys.readouterr().err == ""
    outcomes = real_rs.current("pr-fo")[1]["round_outcomes"]
    # composite (round/channel) keys: both legs of the failover persist under their own
    # key, joins to the gate log stay exact (codex rounds 5-9 P2)
    assert outcomes["1/codex"]["terminal"] == "REVIEWER_UNAVAILABLE"
    assert outcomes["1/gemini"] == {"channel": "gemini", "terminal": "BLOCK", "finding_count": 3}
    # SAME-key conflict (same round, same channel, different content) is an anomaly:
    # reported to stderr, never overwritten (codex round-6/7 P2)
    rw.record_round_outcome_if_reserved(
        "pr-fo", 1, channel="codex", terminal="APPROVE", finding_count=0
    )
    assert "not persisted" in capsys.readouterr().err
    after = real_rs.current("pr-fo")[1]["round_outcomes"]
    assert after["1/codex"]["terminal"] == "REVIEWER_UNAVAILABLE"


def test_wrapper_round_outcome_noop_without_reservation_substrate(monkeypatch, capsys):
    """Pre-S4b: tools/reservations.py is absent → silent no-op, no stderr noise."""
    monkeypatch.setitem(sys.modules, "reservations", None)  # forces ImportError
    rw.record_round_outcome_if_reserved(
        "pr-1", 1, channel="codex", terminal="APPROVE", finding_count=0
    )
    assert capsys.readouterr().err == ""


def test_build_command_is_codex_exec_read_only_with_output_file(tmp_path):
    """codex-cli 0.146.0 (probed live): `codex review --base` rejects a PROMPT, and
    `codex review "<prompt>"` answers in its native findings JSON on a real diff; `codex exec`
    follows the brief and writes the final message to `-o`."""
    cmd = cr.build_command(Path("/r"), "INSTR", output_file=tmp_path / "last.md")
    assert cmd[:2] == ["codex", "exec"] and cmd[-1] == "INSTR"
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("-C") + 1] == "/r" and cmd[cmd.index("-o") + 1] == str(
        tmp_path / "last.md"
    )
    assert "--base" not in cmd and 'preferred_auth_method="chatgpt"' in cmd


#: Operator direction 2026-09-15: out-of-family review runs gpt-5.6-sol at medium reasoning
#: effort, pinned by each review invocation rather than inherited from ~/.codex/config.toml.
REVIEW_MODEL_PINS = ('model="gpt-5.6-sol"', 'model_reasoning_effort="medium"')

#: The known review carriers besides the wrapper's own argv: (file, anchor), where the anchor
#: selects exactly one command in that file. A new review carrier is added here when it is
#: written. A keyword sweep that tried to recognise every possible command shape did not
#: converge (#1528 merge-gate rounds 1-2); the permission guard refuses an unpinned isolated
#: lens call at runtime, so this list pins the documented recipes rather than enforcing them.
REVIEW_CARRIERS = (
    ("tools/hooks/resolve_lib.sh", "env -u OPENAI_API_KEY codex exec"),
    ("justfile", "env -u OPENAI_API_KEY codex review"),
    (
        ".agents/skills/merge-gate/SKILL.md",
        "codex exec --ephemeral --sandbox read-only -C <arc-worktree>",
    ),
    (".codex/notes/merge-gate-lenses/README.md", '"$(cat .codex/notes/merge-gate-lenses/$lens.md)'),
)
#: The isolated-exec shape documented on the `.agents` bridge skills.
BRIDGE_SHAPE = (
    "HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec --ephemeral --sandbox read-only -C <project>"
)


def _joined_lines(text: str) -> list[str]:
    """Physical lines with backslash continuations folded, so a multi-line command is one."""
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    return out


def test_known_codex_review_carriers_pin_gpt_5_6_sol_at_medium_effort(tmp_path):
    """Scope: the wrapper's argv, each carrier in REVIEW_CARRIERS, and every `.agents` skill line
    carrying BRIDGE_SHAPE. It does not discover carriers that are not listed."""
    cmd = cr.build_command(Path("/r"), "INSTR", output_file=tmp_path / "last.md")
    for pin in REVIEW_MODEL_PINS:
        assert cmd[cmd.index(pin) - 1] == "-c", cmd

    root = Path(__file__).resolve().parent.parent
    for rel, anchor in REVIEW_CARRIERS:
        lines = _joined_lines((root / rel).read_text(encoding="utf-8"))
        commands = [line for line in lines if anchor in line]
        assert len(commands) == 1, (rel, anchor, commands)
        assert all(pin in commands[0] for pin in REVIEW_MODEL_PINS), (rel, commands[0].strip())

    bridges = [
        (path.parent.name, line)
        for path in sorted((root / ".agents" / "skills").glob("*/SKILL.md"))
        for line in _joined_lines(path.read_text(encoding="utf-8"))
        if BRIDGE_SHAPE in line
    ]
    assert "c1-orchestration-control" in {name for name, _ in bridges}
    unpinned = [name for name, line in bridges if not all(p in line for p in REVIEW_MODEL_PINS)]
    assert unpinned == []


def test_review_instructions_name_the_bound_diff_and_carry_all_six_binding_values():
    text = cr.review_instructions(EXPECTED)
    assert f"git diff --binary {EXPECTED['base_sha']} {EXPECTED['head_sha']}" in text
    for k, v in EXPECTED.items():
        assert f"{k}={v}" in text
    assert "```json" in text and "APPROVE|BLOCK" in text and "Do not edit files" in text


def test_default_invoke_prefers_the_output_file_over_stdout(monkeypatch):
    """The `-o` file is the final message even when stdout is frozen/truncated (PR #1386)."""
    import subprocess

    def fake_rb(cmd, *, cwd, timeout, env):
        Path(cmd[cmd.index("-o") + 1]).write_text(_block())
        return subprocess.CompletedProcess(cmd, 0, "working...", "transcript")

    monkeypatch.setattr(cr, "run_bounded", fake_rb)
    att = cr._default_invoke(Path("."), "I", EXPECTED)(10.0)
    assert att.stdout == _block() and att.stderr == "transcript" and not att.timed_out


def test_stderr_echo_is_a_second_source_under_the_same_bar(monkeypatch, tmp_path):
    """`codex review` echoes the transcript on stderr; the same parse + byte-compare applies."""
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    monkeypatch.setattr(cr, "ARTIFACT_LAG_S", 0.0)
    out = cr.run_codex_review(
        Path("."), "main", invoke=lambda t: rw.Attempt("", "transcript\n" + _block(), 0, False)
    )
    assert out.terminal == "APPROVE" and out.source == "stderr"
    foreign = _block(head_sha="d" * 40)
    out = cr.run_codex_review(Path("."), "main", invoke=lambda t: rw.Attempt("", foreign, 0, False))
    assert out.terminal == "REVIEWER_UNAVAILABLE"


def test_default_invoke_maps_run_bounded_timeout_to_timed_out(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        cr,
        "run_bounded",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 124, "", "command timed out after 1 seconds"
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    seen = {}

    def fake_rb(cmd, *, cwd, timeout, env):
        seen["env"] = env
        return subprocess.CompletedProcess(cmd, 124, "", "command timed out after 1 seconds")

    monkeypatch.setattr(cr, "run_bounded", fake_rb)
    att = cr._default_invoke(Path("."), "I", EXPECTED)(1.0)
    assert att.timed_out and att.returncode == 124
    assert "OPENAI_API_KEY" not in seen["env"]  # subscription auth only, never the metered key


def test_gate_log_env_override_redirects_the_process_tree(tmp_path: Path):
    """`HARNESS_GATE_LOG` (finding_record) is the seam subprocess fixtures use so a reviewer run
    never appends to the tracked log."""
    import subprocess

    env = {**os.environ, "HARNESS_GATE_LOG": str(tmp_path / "g.jsonl")}
    out = subprocess.run(
        [sys.executable, "-c", "import finding_record as fr; print(fr.GATE_LOG_JSONL)"],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == str(tmp_path / "g.jsonl")
    # without the env, the default is the repo-resident tracked log
    env.pop("HARNESS_GATE_LOG")
    out = subprocess.run(
        [sys.executable, "-c", "import finding_record as fr; print(fr.GATE_LOG_JSONL.name)"],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "merge-gate-log.jsonl"


# ── U-HE-07: D-C failover chain (C-HE-17) ────────────────────────────────────
def test_codex_review_failover_flag_runs_gemini_once_and_blocks(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        cr,
        "run_codex_review",
        lambda repo, base, **kw: rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE", "codex", "permanent", "not logged in", [], EXPECTED
        ),
    )
    monkeypatch.setattr(
        cr,
        "_run_gemini_failover",
        lambda repo, base, chain_round=None: (
            calls.append(chain_round)
            or rw.ReviewOutcome(
                "BLOCK",
                "gemini",
                None,
                "",
                [{"severity": "P1", "location": "l", "message": "m"}],
                EXPECTED,
                "stdout",
            )
        ),
    )
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    assert cr.main(["--base", "main", "--failover"]) == 1
    assert calls == [1]
    kinds = [r["record_kind"] for r in fr.read_rows(tmp_path / "g.jsonl")]
    # primary's row only; the gemini subprocess is the sole emitter of its own rows
    assert kinds == ["reviewer_unavailable"]


def test_codex_review_failover_not_invoked_when_primary_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cr,
        "run_codex_review",
        lambda repo, base, **kw: rw.ReviewOutcome(
            "APPROVE", "codex", None, "", [], EXPECTED, "stdout"
        ),
    )
    monkeypatch.setattr(cr, "_run_gemini_failover", lambda repo, base: pytest.fail("must not run"))
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    assert cr.main(["--base", "main", "--failover"]) == 0


def test_codex_review_failover_both_unavailable_exits_2_with_both_reasons(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        cr,
        "run_codex_review",
        lambda repo, base, **kw: rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE", "codex", "permanent", "codex-reason", [], EXPECTED
        ),
    )
    monkeypatch.setattr(
        cr,
        "_run_gemini_failover",
        lambda repo, base, chain_round=None: rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE", "gemini", "transient", "gemini-reason", [], EXPECTED
        ),
    )
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    routed = []
    monkeypatch.setattr(
        cr, "_route_to_hitl", lambda arc_id, reason, **kw: routed.append((arc_id, reason, kw))
    )
    assert cr.main(["--base", "main", "--failover"]) == 2
    err = capsys.readouterr().err
    assert "codex-reason" in err and "gemini-reason" in err and "BOTH channels unavailable" in err
    # C-HE-20 §1: the arc-blocking outage routes to the durable HITL queue, once, as DEFERRED-HIL
    assert len(routed) == 1 and "codex-reason" in routed[0][1] and "gemini-reason" in routed[0][1]
    assert routed[0][2] == {"kind": "both-unavailable"}


def test_primary_outage_with_a_successful_failover_routes_by_failure_class(monkeypatch, tmp_path):
    """transient primary outage + carried failover -> NOTIFY; a PERMANENT one (stale login) is a
    human-fixable gate -> DEFERRED-HIL even though this arc was carried (codex rounds 7/9)."""
    monkeypatch.setattr(
        cr,
        "run_codex_review",
        lambda repo, base, **kw: rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE", "codex", "permanent", "codex-reason", [], EXPECTED
        ),
    )
    monkeypatch.setattr(
        cr,
        "_run_gemini_failover",
        lambda repo, base, chain_round=None: rw.ReviewOutcome(
            "APPROVE", "gemini", None, "", [], EXPECTED, "stdout"
        ),
    )
    routed = []
    monkeypatch.setattr(
        cr, "_route_to_hitl", lambda arc_id, reason, **kw: routed.append((reason, kw))
    )
    assert cr.main(["--base", "main", "--failover"]) == 0
    assert routed == [("codex: codex-reason; gemini: APPROVE", {"kind": "primary-permanent"})]
    routed.clear()
    monkeypatch.setattr(
        cr,
        "run_codex_review",
        lambda repo, base, **kw: rw.ReviewOutcome(
            "REVIEWER_UNAVAILABLE", "codex", "transient", "codex-reason", [], EXPECTED
        ),
    )
    assert cr.main(["--base", "main", "--failover"]) == 0
    assert routed == [("codex: codex-reason; gemini: APPROVE", {"kind": "primary-transient"})]


def test_route_to_hitl_notify_kind_for_the_non_blocking_case(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".harness").mkdir()
    # C-HE-09 §2: the ledger is a SHARED venue, no longer selected by CLAUDE_PROJECT_DIR.
    monkeypatch.setenv("HARNESS_LOOP_STATUS_PATH", str(tmp_path / "shared-loop_status.md"))
    _REAL_ROUTE_TO_HITL("B-778", "codex: x; gemini: APPROVE", kind="primary-transient")
    ledger = (tmp_path / "shared-loop_status.md").read_text()
    assert (
        "| NOTIFY | lane=-;cause=- | review-with-failover: primary REVIEWER_UNAVAILABLE, "
        "failover carried the "
        "review — B-778: codex: x; gemini: APPROVE [ref "
    ) in ledger
    assert "| DEFERRED-HIL |" not in ledger  # the header prose mentions the kind; rows do not


def test_route_to_hitl_writes_a_deferred_hil_row_via_loop_defer(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))  # lib.sh's project-dir override
    (tmp_path / ".harness").mkdir()
    monkeypatch.setenv("HARNESS_LOOP_STATUS_PATH", str(tmp_path / "shared-loop_status.md"))
    _REAL_ROUTE_TO_HITL("B-777", "codex: x; gemini: y")
    ledger = (tmp_path / "shared-loop_status.md").read_text()
    assert (
        "| DEFERRED-HIL | lane=-;cause=- | B-777 — review-with-failover: REVIEWER_UNAVAILABLE "
        "on both channels — "
        "codex: x; gemini: y [ref "
    ) in ledger
    assert ledger.count("| DEFERRED-HIL |") == 1


def test_failover_preflight_failure_still_records_both_reasons(monkeypatch, tmp_path):
    """`just gemini-review` dies at `_require-antigravity` → the subprocess wrote nothing → the
    failover path emits (permanent: the binary is missing)."""

    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    monkeypatch.setattr(
        cr,
        "run_bounded",
        _fake_just(None, rc=1, stderr="ERROR: agy (Antigravity CLI) not found on PATH."),
    )
    monkeypatch.setattr(rw, "compute_binding", lambda *a, **k: dict(EXPECTED))
    monkeypatch.setattr(cr, "_gemini_config_hash", lambda: "x")
    out = cr._run_gemini_failover(Path("."), "main")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "permanent"
    rows = fr.read_rows(tmp_path / "g.jsonl")
    assert len(rows) == 1 and rows[0]["producer"] == "gemini_review_wrapper"
    assert rows[0]["record_kind"] == "reviewer_unavailable"
    assert rows[0]["finding_type"] == "permanent-fail-exit"


def _fake_just(envelope_body, *, rc=1, stderr="stderr"):
    """A `run_bounded`-shaped `just gemini-review <base> <envelope>` stand-in that writes the
    wrapper envelope (or not) and returns raw vendor stdout that must NEVER be re-parsed."""
    import subprocess

    def run(cmd, *, cwd, timeout, env):
        assert cmd[:2] == ["just", "gemini-review"] and timeout == cr.FAILOVER_SUBPROCESS_CAP_S
        if envelope_body is not None:
            Path(cmd[3]).write_text(json.dumps(envelope_body))
        return subprocess.CompletedProcess(cmd, rc, "raw vendor stdout " + _block(), stderr)

    return run


def test_failover_reads_the_gemini_wrapper_envelope_not_raw_stdout(monkeypatch, tmp_path):
    """The wrapper reached a terminal and wrote its own rows: consume its envelope (identical
    bar: binding byte-equal to THIS invocation's expected); raw stdout may carry a schema-valid
    APPROVE the wrapper itself refused (codex round 3) -- it is never re-parsed."""
    log = tmp_path / "g.jsonl"
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", log)
    gem = {**EXPECTED, "reviewer_identity": "gemini-review"}
    monkeypatch.setattr(
        rw, "compute_binding", lambda *a, **k: {**gem, "config_hash": k["config_hash"]}
    )
    monkeypatch.setattr(cr, "_gemini_config_hash", lambda: EXPECTED["config_hash"])
    body = {
        "terminal": "BLOCK",
        "channel": "gemini",
        "failure_class": None,
        "reason": "",
        "findings": [{"severity": "P1", "location": "l", "message": "m"}],
        "binding": gem,
        "source": "stdout",
    }
    monkeypatch.setattr(cr, "run_bounded", _fake_just(body))
    out = cr._run_gemini_failover(Path("."), "main")
    assert out.terminal == "BLOCK" and len(out.findings) == 1 and out.binding == gem
    assert fr.read_rows(log) == []  # the wrapper's own rows are its business; no duplicate here
    # the wrapper said unavailable although its raw stdout carries a valid APPROVE block
    body_u = {
        **body,
        "terminal": "REVIEWER_UNAVAILABLE",
        "failure_class": "transient",
        "reason": "model mismatch",
        "findings": [],
    }
    monkeypatch.setattr(cr, "run_bounded", _fake_just(body_u))
    out = cr._run_gemini_failover(Path("."), "main")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.reason == "model mismatch"
    # an envelope bound to a different invocation is refused (identical bar)
    body_f = {**body, "binding": {**gem, "head_sha": "d" * 40}}
    monkeypatch.setattr(cr, "run_bounded", _fake_just(body_f))
    out = cr._run_gemini_failover(Path("."), "main")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "different invocation" in out.reason


def test_failover_child_env_set_and_parent_records_reservation_outcome(monkeypatch, tmp_path):
    """codex r17 / merge-gate spec-conformance P2: the child skips reservation persistence
    (HARNESS_FAILOVER_CHILD=1); the PARENT records the C-HE-25 outcome from the envelope it
    ACCEPTED -- including the synthetic REVIEWER_UNAVAILABLE on a binding mismatch -- so the
    reservation always matches what the caller acts on."""
    import reservations as real_rs

    q = tmp_path / "queue"
    q.mkdir()
    monkeypatch.setattr(real_rs, "QUEUE_DIR", q)
    monkeypatch.setitem(sys.modules, "reservations", real_rs)
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "g.jsonl")
    monkeypatch.setenv("HARNESS_ARC_ID", "pr-chain")  # a reservable id (no '/')
    arc_id, _ = rw.env_arc_and_lane()
    real_rs.reserve(arc_id, lane_id="A", branch="b", arc_type="inventing")
    gem = {**EXPECTED, "reviewer_identity": "gemini-review"}
    monkeypatch.setattr(
        rw, "compute_binding", lambda *a, **k: {**gem, "config_hash": k["config_hash"]}
    )
    monkeypatch.setattr(cr, "_gemini_config_hash", lambda: EXPECTED["config_hash"])
    seen_env = {}
    body = {
        "terminal": "BLOCK",
        "channel": "gemini",
        "failure_class": None,
        "reason": "",
        "findings": [{"severity": "P1", "location": "l", "message": "m"}],
        "binding": gem,
        "source": "stdout",
    }

    def fake_run(cmd, *, cwd, timeout, env):
        import subprocess as sp

        seen_env.update(env)
        Path(cmd[3]).write_text(json.dumps(body))
        return sp.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(cr, "run_bounded", fake_run)
    out = cr._run_gemini_failover(Path("."), "main", chain_round=2)
    assert out.terminal == "BLOCK"
    assert seen_env.get("HARNESS_FAILOVER_CHILD") == "1"
    assert seen_env.get("HARNESS_ROUND_N") == "2"
    outcomes = real_rs.current(arc_id)[1]["round_outcomes"]
    assert outcomes == {"2/gemini": {"channel": "gemini", "terminal": "BLOCK", "finding_count": 1}}
    # rejected envelope (binding mismatch): the PARENT records the synthetic UNAVAILABLE the
    # caller acts on -- same-key conflict with the accepted row above would raise, so use a
    # fresh chain round as the production flow would
    body["binding"] = {**gem, "head_sha": "d" * 40}
    out = cr._run_gemini_failover(Path("."), "main", chain_round=3)
    assert out.terminal == "REVIEWER_UNAVAILABLE"
    outcomes = real_rs.current(arc_id)[1]["round_outcomes"]
    assert outcomes["3/gemini"]["terminal"] == "REVIEWER_UNAVAILABLE"
    # codex r18 P2: the PARENT also emitted its own gate row for the rejected-envelope
    # terminal the caller acts on -- the join to the gate log holds
    rows = fr.read_rows(tmp_path / "g.jsonl")
    parent_rows = [r for r in rows if r["producer"] == cr.GEMINI_PRODUCER and r["round_n"] == 3]
    assert parent_rows and parent_rows[0]["record_kind"] == "reviewer_unavailable"


def test_failover_without_envelope_is_recorded_here_once(monkeypatch, tmp_path):
    """No envelope = the wrapper never reached a terminal (preflight death / missing `just`):
    classify from stderr, emit exactly one row here."""
    import subprocess

    log = tmp_path / "g.jsonl"
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", log)
    monkeypatch.setattr(rw, "compute_binding", lambda *a, **k: dict(EXPECTED))
    monkeypatch.setattr(cr, "_gemini_config_hash", lambda: "x")
    monkeypatch.setattr(cr, "run_bounded", _fake_just(None))
    out = cr._run_gemini_failover(Path("."), "main")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "no outcome envelope" in out.reason
    assert len(fr.read_rows(log)) == 1

    # a missing `just` is run_bounded's rc-127 CompletedProcess (never an exception)
    monkeypatch.setattr(cr, "run_bounded", _fake_just(None, rc=127, stderr="No such file: 'just'"))
    out = cr._run_gemini_failover(Path("."), "main")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "no outcome envelope" in out.reason
    assert len(fr.read_rows(log)) == 2

    def bad_base(*a, **k):
        raise subprocess.CalledProcessError(
            128, ["git", "-C", ".", "merge-base"], stderr="fatal: bad"
        )

    monkeypatch.setattr(rw, "compute_binding", bad_base)
    out = cr._run_gemini_failover(Path("."), "nope")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "permanent"
    assert len(fr.read_rows(log)) == 3


def test_review_with_failover_recipe_and_carriers_present():
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    recipe = justfile.split("review-with-failover base='main':", 1)[1].split("\n\n", 1)[0]
    assert "tools/codex_review.py" in recipe and "--failover" in recipe
    assert (
        "_require-codex-subscription"
        not in "review-with-failover base='main':" + recipe.split("\n")[0]
    )
    for skill in ("ship-pr", "roadmap-continue"):
        body = (
            Path(__file__).resolve().parents[1] / ".claude" / "skills" / skill / "SKILL.md"
        ).read_text()
        assert "just review-with-failover" in body
        assert "Invariant #3 (restated, C-HE-17 §3)" in body


def test_first_attempt_is_capped_by_the_remaining_budget():
    seen: list[float] = []

    def invoke(timeout):
        seen.append(timeout)
        return rw.Attempt(_block(), "", 0, False)

    rw.run_with_retry(invoke, channel="codex", expected=EXPECTED, deadline=700.0, clock=lambda: 0.0)
    assert seen == [700.0]  # min(1200, 700)


def test_timed_out_attempt_still_reads_the_session_artifact_once(tmp_path, monkeypatch):
    """Killed at the cap but the CLI had persisted its final message: the artifact counts under
    the same bar; without it the timeout stands (no 130 s wait after a timeout)."""
    head = "a" * 40
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(cr, "_binding", lambda repo, base: {**EXPECTED, "head_sha": head})

    def invoke_persisting_then_dying(t):
        _artifact_tree(tmp_path, head, mtime=time.time())  # the CLI persisted its final message
        return rw.Attempt("", "", None, True)  # ...then hung until the cap killed it

    out = cr.run_codex_review(Path("."), "main", invoke=invoke_persisting_then_dying, nonce=NONCE)
    assert out.terminal == "APPROVE" and out.source == "session-artifact"
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    n = count()

    def invoke(t):
        next(n)
        return rw.Attempt("", "", None, True)

    out = cr.run_codex_review(Path("."), "main", invoke=invoke)
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "timed out" in out.reason and next(n) == 1


def test_unresolvable_base_is_permanent_unavailable_not_a_traceback(monkeypatch, tmp_path):
    import subprocess

    def boom(*a, **k):
        raise subprocess.CalledProcessError(
            128, ["git", "-C", ".", "merge-base"], stderr="fatal: Not a valid object name nope"
        )

    monkeypatch.setattr(rw, "compute_binding", boom)
    out = cr.run_codex_review(Path("."), "nope")
    assert out.terminal == "REVIEWER_UNAVAILABLE" and out.failure_class == "permanent"
    assert "Not a valid object name" in out.reason


# ── round-3 absorptions: classifier input, exit-code rule, artifact floor, import ────────
def test_classifier_reads_short_streams_whole_and_long_streams_by_tail():
    diff_echo = "x" * 5000 + "\n-    ('codex', 'not logged in') 401 unauthorized\n" + "y" * 5000
    assert rw.classify("codex", rw.classifier_text("", diff_echo)) == "transient"
    assert rw.classify("codex", rw.classifier_text("", "Error: not logged in")) == "permanent"
    assert (
        rw.classify("codex", rw.classifier_text("", "z" * 9000 + "\nError: unauthorized"))
        == "permanent"
    )
    assert rw.classify("codex", rw.classifier_text("Error: not logged in", "")) == "permanent"


def test_retry_loop_ignores_auth_words_inside_a_long_transcript():
    n = count()

    def invoke(timeout):
        next(n)
        return rw.Attempt(
            "", "transcript " + "diff --git\n" * 900 + "+ 'not logged in'\n" + "t" * 3000, 0, False
        )

    out = rw.run_with_retry(
        invoke, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0
    )
    assert out.failure_class == "transient" and next(n) == 2  # retried, not misread as permanent


def test_nonzero_exit_with_parsed_approve_is_unavailable_but_block_stands():
    approve_crash = lambda t: rw.Attempt(_block("APPROVE"), "", 3, False)  # noqa: E731
    out = rw.run_with_retry(
        approve_crash, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "parsed APPROVE" in out.reason

    def block_crash(t):
        return rw.Attempt(
            _block("BLOCK", [{"severity": "P1", "location": "l", "message": "m"}]), "", 3, False
        )

    out = rw.run_with_retry(
        block_crash, channel="codex", expected=EXPECTED, deadline=10_000.0, clock=lambda: 0.0
    )
    assert out.terminal == "BLOCK" and len(out.findings) == 1


def test_artifact_discovery_floors_the_start_and_needs_no_upper_bound(tmp_path):
    p = _artifact_tree(tmp_path, "a" * 40, mtime=1000.0)  # whole-second mtime
    assert cr.find_session_artifact("a" * 40, started_at=1000.7, now=1001.0, root=tmp_path) == p
    assert cr.find_session_artifact("a" * 40, started_at=1001.0, now=1002.0, root=tmp_path) is None
    future = _artifact_tree(tmp_path / "f", "a" * 40, mtime=5000.0)
    assert (
        cr.find_session_artifact("a" * 40, started_at=1000.0, now=1001.0, root=tmp_path / "f")
        == future
    )


def test_bound_diff_is_bytes_and_the_digest_hashes_them(monkeypatch):
    import subprocess

    raw = b"diff --git a/x b/x\n+\xff\xfe not utf-8\n"

    def fake_run(cmd, **k):
        if cmd[3:5] == ["diff", "--binary"]:
            return subprocess.CompletedProcess(cmd, 0, raw, b"")
        return subprocess.CompletedProcess(
            cmd, 0, "e" * 40 + "\n" if "rev-parse" in cmd else "b" * 40 + "\n", ""
        )

    monkeypatch.setattr(rw.subprocess, "run", fake_run)
    b = rw.compute_binding(Path("."), "main", channel="codex", prompt_version="p", config_hash="c")
    import hashlib

    assert b["diff_digest"] == hashlib.sha256(raw).hexdigest()
    assert rw.bound_diff(Path("."), "b" * 40, "e" * 40) == raw


def test_agy_review_imports_cleanly_in_a_fresh_interpreter():
    """codex round 3: review_wrapper_common back-imported constants from agy_review, so a plain
    `import agy_review` hit the partially-initialised module."""
    import subprocess

    subprocess.run(
        [sys.executable, "-c", "import agy_review, review_wrapper_common, codex_review"],
        cwd=Path(__file__).resolve().parent,
        check=True,
        capture_output=True,
    )


# ── round-5 absorptions ───────────────────────────────────────────────────────
def test_missing_binary_wording_from_run_bounded_is_permanent():
    """run_bounded reports an exec failure as Popen's OSError text, not the shell's."""
    assert rw.classify("codex", "[Errno 2] No such file or directory: 'codex'") == "permanent"
    assert rw.classify("gemini", "[Errno 2] No such file or directory: 'agy'") == "permanent"
    assert rw.classify("gemini", "bash: agy: command not found") == "permanent"


def test_timed_out_attempt_artifact_verdict_is_not_a_failed_process_approval(tmp_path, monkeypatch):
    """The real timeout Attempt carries rc 124 (our kill): an artifact APPROVE must still count."""
    head = "a" * 40
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(cr, "_binding", lambda repo, base: {**EXPECTED, "head_sha": head})

    def invoke(t):
        _artifact_tree(tmp_path, head, mtime=time.time())
        return rw.Attempt("", "command timed out after 1200 seconds", 124, True)

    out = cr.run_codex_review(Path("."), "main", invoke=invoke, nonce=NONCE)
    assert out.terminal == "APPROVE" and out.source == "session-artifact"


def test_review_with_failover_is_auto_allowed_under_loop_mode():
    guard = (
        Path(__file__).resolve().parents[1] / "tools" / "hooks" / "permission-guard.sh"
    ).read_text()
    assert "review-with-failover" in guard
    test = (
        Path(__file__).resolve().parents[1] / "tools" / "hooks" / "test_permission_guard.sh"
    ).read_text()
    assert '"just review-with-failover"' in test


# ── round-6 absorptions ───────────────────────────────────────────────────────
def test_reviewer_env_drops_secrets_and_marks_isolation():
    base = {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "OPENAI_API_KEY": "x",
        "ANTHROPIC_API_KEY": "x",
        "E2B_API_KEY": "x",
        "GOOGLE_APPLICATION_CREDENTIALS": "/c.json",
        "GH_TOKEN": "x",
        "MY_SERVICE_PASSWORD": "x",
        "SOME_AUTH_HEADER": "x",
        "HARNESS_ROUND_N": "3",
        "HARNESS_SECRET_TOKEN": "x",  # allowed prefix, secret-shaped name: still dropped
        "DATABASE_URL": "postgres://u:p@h/db",  # not allowlisted: dropped
    }
    env = rw.reviewer_env(base)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "HARNESS_ROUND_N": "3",
        "HARNESS_CODEX_REVIEW_ISOLATED": "1",
    }


def test_default_invoke_runs_codex_with_the_reviewer_env(monkeypatch):
    import subprocess

    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    seen = {}

    def fake_rb(cmd, *, cwd, timeout, env):
        seen["env"] = env
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, _block(), "")

    monkeypatch.setattr(cr, "run_bounded", fake_rb)
    cr._default_invoke(Path("."), "I", EXPECTED)(10.0)
    assert (
        "ANTHROPIC_API_KEY" not in seen["env"]
        and seen["env"]["HARNESS_CODEX_REVIEW_ISOLATED"] == "1"
    )
    assert "--ephemeral" not in seen["cmd"]  # C-HE-18 §2 needs the persisted session artifact


def test_bound_block_already_on_stdout_when_our_cap_kills_still_blocks(tmp_path, monkeypatch):
    """codex round 6: a discarded BLOCK would let the failover approve over proven findings."""
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    block = _block("BLOCK", [{"severity": "P1", "location": "l", "message": "m"}])
    out = cr.run_codex_review(
        Path("."), "main", invoke=lambda t: rw.Attempt(block, "timed out", 124, True)
    )
    assert out.terminal == "BLOCK" and len(out.findings) == 1


def test_round_n_is_env_or_next_for_this_arc_and_producer(monkeypatch):
    """Per-(arc, producer) scoping is load-bearing for the merge-gate lenses (codex r19:
    an arc-global mint renumbered one gate pass's three lens rows N/N+1/N+2 and was
    reverted); the failover chain shares its round via HARNESS_ROUND_N instead."""
    monkeypatch.delenv("HARNESS_ROUND_N", raising=False)
    rows = [
        {"arc_id": "a", "producer": "p", "round_n": 2},
        {"arc_id": "a", "producer": "q", "round_n": 9},
        {"arc_id": "b", "producer": "p", "round_n": 7},
    ]
    assert rw.round_n_for("a", "p", rows) == 3
    assert rw.round_n_for("a", "zzz", rows) == 1
    assert rw.round_n_for("new", "p", []) == 1
    monkeypatch.setenv("HARNESS_ROUND_N", "5")
    assert rw.round_n_for("a", "p", rows) == 5


def test_successive_wrapper_runs_get_distinct_rounds(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_ROUND_N", raising=False)
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    monkeypatch.setattr(cr, "ARTIFACT_LAG_S", 0.0)
    log = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", log)
    cr.main(["--base", "main", "--invoke-test-empty"])
    cr.main(["--base", "main", "--invoke-test-empty"])
    assert [r["round_n"] for r in fr.read_rows(log)] == [1, 2]


def test_concurrent_emitters_never_share_a_round(tmp_path, monkeypatch):
    """codex round 7: the round is minted under the same lock as the append."""
    import concurrent.futures

    monkeypatch.delenv("HARNESS_ROUND_N", raising=False)
    log = tmp_path / "gate.jsonl"
    out = rw.ReviewOutcome("REVIEWER_UNAVAILABLE", "codex", "transient", "x", [], EXPECTED)

    def emit(_):
        return rw.emit_outcome(out, producer="p", arc_id="a", lane_id="l", round_n=None, path=log)[
            0
        ]["round_n"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        rounds = sorted(ex.map(emit, range(8)))
    assert rounds == list(range(1, 9))
    assert sorted(r["round_n"] for r in fr.read_rows(log)) == list(range(1, 9))


# ── round-8 absorptions ───────────────────────────────────────────────────────
def test_reviewer_prose_on_stdout_never_classifies_permanent_when_stderr_speaks():
    prose = "The wrapper handles 401 unauthorized and login failures; truncated..."
    assert rw.classify("codex", rw.classifier_text(prose, "tokens used 12,345")) == "transient"
    # stderr silent: a CLI that reports its auth error on stdout is still caught
    assert rw.classify("codex", rw.classifier_text("Error: not logged in", "")) == "permanent"
    # stderr speaks: it is the only classified stream
    assert rw.classify("codex", rw.classifier_text(prose, "Error: not logged in")) == "permanent"


def test_findings_cap_matches_each_channel_prompt():
    caps = {"codex": cr.MAX_FINDINGS, "gemini": 5, "merge-gate": 8}
    for ch, cap in caps.items():
        assert rw.load_schema(ch)["properties"]["findings"]["maxItems"] == cap
    too_many = [{"severity": "P3", "location": f"f{i}", "message": "m"} for i in range(9)]
    assert (
        rw.parse_verdict("codex", _block("BLOCK", too_many), EXPECTED).terminal
        == "REVIEWER_UNAVAILABLE"
    )
    assert "at most 8 findings" in cr.review_instructions(EXPECTED)


def test_codex_review_recipe_has_no_subscription_preflight():
    """codex round 8: the preflight would exit 1 before the wrapper's terminal contract applies."""
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    line = next(ln for ln in justfile.splitlines() if ln.startswith("codex-review base='main':"))
    assert "_require-codex-subscription" not in line
    assert (
        "codex-review-uncommitted: _require-codex-subscription" in justfile
    )  # the ad-hoc recipe keeps it


# ── round-9 absorptions ───────────────────────────────────────────────────────
def test_classifier_reads_only_error_shaped_lines():
    prose = "We considered the login flow, a 401 unauthorized path, and truncated output."
    assert rw.classify("codex", rw.classifier_text("", prose)) == "transient"
    assert (
        rw.classify("codex", rw.classifier_text("", prose + "\nError: not logged in"))
        == "permanent"
    )
    assert (
        rw.classify("codex", rw.classifier_text("", "bash: codex: command not found"))
        == "permanent"
    )
    assert (
        rw.classify(
            "gemini", rw.classifier_text("", "ERROR: agy (Antigravity CLI) not found on PATH.")
        )
        == "permanent"
    )
    assert (
        rw.classify("codex", rw.classifier_text("", "read ETIMEDOUT while streaming"))
        == "transient"
    )


def test_codex_home_env_drives_config_hash_and_sessions_dir(tmp_path):
    import subprocess

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import codex_review as c; print(c.CODEX_HOME); print(c.SESSIONS_DIR)",
        ],
        cwd=Path(__file__).resolve().parent,
        env={**os.environ, "CODEX_HOME": str(tmp_path / "alt")},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == [str(tmp_path / "alt"), str(tmp_path / "alt" / "sessions")]


def test_route_to_hitl_reports_a_swallowed_append(tmp_path, monkeypatch, capsys):
    """loop_log turns a failed append into success (`|| true`); the router verifies the row."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".harness").mkdir()
    monkeypatch.setenv("HARNESS_LOOP_STATUS_PATH", str(tmp_path / "shared-loop_status.md"))
    (tmp_path / "shared-loop_status.md").write_text("# ledger\n")
    (tmp_path / "shared-loop_status.md").chmod(0o444)
    _REAL_ROUTE_TO_HITL("B-779", "codex: x; gemini: y")
    assert "HITL row NOT written" in capsys.readouterr().err


# ── round-10 absorptions ──────────────────────────────────────────────────────
def test_head_moved_during_review_is_unavailable_not_a_stale_approve(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    monkeypatch.setattr(cr, "_head", lambda repo: "f" * 40)  # a commit landed mid-review
    out = cr.run_codex_review(
        Path("."), "main", invoke=lambda t: rw.Attempt(_block(), "", 0, False)
    )
    assert out.terminal == "REVIEWER_UNAVAILABLE" and "HEAD moved" in out.reason
    assert out.binding == EXPECTED  # the recorded verdict stays bound to the reviewed head


def test_artifact_text_reads_assistant_output_only(tmp_path):
    """A fenced JSON example inside tool output / the reviewed diff must not make the channel's
    own verdict ambiguous (codex round 10)."""
    d = tmp_path / "s"
    d.mkdir()
    p = d / "rollout.jsonl"
    foreign = '```json\n{"verdict": "APPROVE", "example": true}\n```'
    lines = [
        {
            "type": "response_item",
            "payload": {
                "role": "user",
                "content": [{"type": "input_text", "text": "brief " + foreign}],
            },
        },
        {"type": "event_msg", "payload": {"type": "exec_command_end", "stdout": "diff " + foreign}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": _block()}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": _block()}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    text = cr.artifact_text(p)
    assert "example" not in text and "brief" not in text
    assert rw.parse_verdict("codex", text, EXPECTED).terminal == "APPROVE"


def test_export_bound_tree_materialises_the_digested_bytes_and_the_head_tree(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    head = rw._git(repo, "rev-parse", "HEAD")
    base = rw._git(repo, "rev-parse", "HEAD~1")
    dest = tmp_path / "review"
    _REAL_EXPORT_BOUND_TREE(repo, head, base, dest)
    assert (dest / "bound.diff").read_bytes() == rw.bound_diff(repo, base, head)
    assert (dest / "tree" / "tools" / "codex_review.py").exists()
    assert not (dest / "tree" / ".git").exists()


def test_config_hash_covers_user_and_repo_config_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CODEX_HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.toml").write_text("model = 'a'\n")
    repo = tmp_path / "repo"
    (repo / ".codex").mkdir(parents=True)
    h1 = cr._config_hash(repo)
    (repo / ".codex" / "config.toml").write_text("[hooks]\n")
    h2 = cr._config_hash(repo)
    assert h1 != h2
    monkeypatch.setattr(cr, "CODEX_OVERRIDES", ("-c", "x=1"))
    assert cr._config_hash(repo) != h2


def test_build_command_roots_the_reviewer_in_the_scratch_export(tmp_path):
    cmd = cr.build_command(tmp_path / "review", "INSTR", output_file=tmp_path / "last.md")
    assert "--skip-git-repo-check" in cmd and cmd[cmd.index("-C") + 1] == str(tmp_path / "review")
    brief = cr.review_instructions(EXPECTED)
    assert "./bound.diff" in brief and "./tree/" in brief and "open nothing outside" in brief


# ── round-11 absorptions ──────────────────────────────────────────────────────
def test_bound_block_on_stderr_when_our_cap_kills_still_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(cr, "_binding", lambda repo, base: EXPECTED)
    block = _block("BLOCK", [{"severity": "P1", "location": "l", "message": "m"}])
    out = cr.run_codex_review(
        Path("."), "main", invoke=lambda t: rw.Attempt("", "transcript\n" + block, 124, True)
    )
    assert out.terminal == "BLOCK" and out.source == "stderr"


def test_route_to_hitl_permanent_primary_defers_the_channel_not_the_arc(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".harness").mkdir()
    monkeypatch.setenv("HARNESS_LOOP_STATUS_PATH", str(tmp_path / "shared-loop_status.md"))
    _REAL_ROUTE_TO_HITL("B-780", "codex: not logged in; gemini: BLOCK", kind="primary-permanent")
    ledger = (tmp_path / "shared-loop_status.md").read_text()
    assert (
        "| DEFERRED-HIL | lane=-;cause=- | codex-review-channel — primary channel "
        "permanently unavailable; the failover carried B-780"
    ) in ledger
    assert "| DEFERRED-HIL | B-780" not in ledger  # the arc itself is NOT marked pending
    assert "on both channels" not in ledger


def test_gemini_review_recipe_has_no_antigravity_preflight():
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    line = next(ln for ln in justfile.splitlines() if ln.startswith("gemini-review base='main'"))
    assert "_require-antigravity" not in line


# ── merge-gate observations (L1 / L3) ─────────────────────────────────────────
def test_one_outcome_repeating_a_location_mints_distinct_ids_in_one_call(tmp_path):
    """append_observations mints each id against the snapshot PLUS the pairs already appended
    in the same call (merge-gate L3)."""
    log = tmp_path / "gate.jsonl"
    out = rw.ReviewOutcome(
        "BLOCK",
        "codex",
        None,
        "",
        [
            {"severity": "P1", "location": "a.py:1", "message": "first"},
            {"severity": "P2", "location": "a.py:1", "message": "second, same location"},
        ],
        EXPECTED,
        "stdout",
    )
    rows = rw.emit_outcome(out, producer="p", arc_id="a", lane_id="l", round_n=None, path=log)
    assert [r["finding_id"].rsplit(":", 1)[1] for r in rows] == ["1", "2"]
    assert len({r["finding_id"] for r in fr.read_rows(log)}) == 2


def test_compute_binding_merge_base_uses_the_captured_head(monkeypatch):
    import subprocess

    seen = []

    def fake_run(cmd, **k):
        seen.append(cmd)
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "e" * 40 + "\n", "")
        if "merge-base" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "b" * 40 + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, b"diff", b"")

    monkeypatch.setattr(rw.subprocess, "run", fake_run)
    rw.compute_binding(Path("."), "main", channel="codex", prompt_version="p", config_hash="c")
    mb = next(c for c in seen if "merge-base" in c)
    assert mb[-2:] == ["main", "e" * 40]  # never a second "HEAD" read (merge-gate L1)


def test_main_sigterm_machinery_installs_translates_and_restores(monkeypatch):
    """U-HE-35 codex r8/r9: run_bounded spawns the vendor CLI in its OWN session, so only
    this process's TerminationRequested unwind can tear that group down — the handler must
    be live for the whole review body, translate to the POSIX signal exit, and never
    outlive main. Reverting the main() install (the r8 fix) fails this test."""
    import signal as _signal

    before = _signal.getsignal(_signal.SIGTERM)
    seen = {}

    def fake_reviewed(args, invoke):
        seen["handler"] = _signal.getsignal(_signal.SIGTERM)
        raise cr.TerminationRequested(15)

    monkeypatch.setattr(cr, "_reviewed_main", fake_reviewed)
    assert cr.main(["--base", "main"]) == 143  # 128 + SIGTERM
    assert seen["handler"] is cr.handle_termination_signal  # live during the review body
    assert _signal.getsignal(_signal.SIGTERM) is before  # restored after
