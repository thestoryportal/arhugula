"""U-HE-43 / C-HE-29 — the shadow-trial reducer, kill rule and adjudication writer.

Behavioural contract only ([LAW:behavior-not-structure]): decisions are reproduced from gate-log
ROWS alone (C-HE-29 Invariants), the OC table is recomputed here from the binomial rather than
read back from the module, and the one production writer of `unique_catch` is `adjudicate`."""

from __future__ import annotations

import sys
from math import comb
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import finding_record as fr
import shadow_trial as st

HEAD = "h" * 40
LENS = "gemini-shadow"


def _row(
    round_n: int,
    producer: str,
    kind: str = "finding",
    *,
    location: str = "l",
    ftype: str = "t",
    disp: str | None = None,
    actor: str | None = None,
    uc: bool | None = None,
    head: str = HEAD,
    arc: str = "pr-1",
    n: int = 1,
    ts: str | None = None,
) -> dict:
    """A schema-valid C-HE-24 row: the id is minted by the record module's own formula, never
    by hand, so `finding_record.validate` accepts what the tests feed the reducer."""
    return {
        "finding_id": fr.make_finding_id(producer, head, location, n),
        "location": location,
        "observed_evidence": "e",
        "expected_contract": "c",
        "severity": "P2",
        "finding_type": ftype,
        "lineage_claim": "fresh",
        "producer": producer,
        "record_kind": kind,
        "ts": ts or f"2026-08-18T00:00:{round_n % 60:02d}Z",
        "arc_id": arc,
        "lane_id": "L",
        "head_sha": head,
        "base_sha": None,
        "diff_digest": None,
        "round_n": round_n,
        "cause_attribution": None,
        "disposition": disp,
        "disposition_actor": actor,
        "unique_catch": uc,
    }


def _adj(round_n: int, location: str, disp: str, **kw) -> dict:
    return _row(
        round_n,
        LENS,
        kind="finding_adjudication",
        location=location,
        disp=disp,
        actor="operator",
        uc=True,
        **kw,
    )


def _markers(n: int, arc: str = "pr-1") -> list[dict]:
    return [
        _row(r, LENS, kind="no_finding", location="gemini", arc=arc, n=r) for r in range(1, n + 1)
    ]


# mutation-probe: the binomial sum's upper bound (`range(threshold)` in p_kill)
def test_oc_table_matches_spec_numbers():
    table = dict(st.oc_table())
    for p, expect in (
        (0.0, 1.000),
        (0.05, 0.554),
        (0.10, 0.184),
        (0.15, 0.048),
        (0.20, 0.011),
        (0.25, 0.002),
    ):
        assert round(table[p], 3) == expect
    # recomputed independently by the test, not read from the module's constants
    assert round(sum(comb(30, k) * 0.10**k * 0.90 ** (30 - k) for k in range(2)), 3) == 0.184
    assert (
        round(sum(comb(15, k) * 0.10**k * 0.90 ** (15 - k) for k in range(2)), 2) == 0.55
    )  # rejected n=15
    assert (
        round(sum(comb(30, k) * 0.10**k * 0.90 ** (30 - k) for k in range(3)), 2) == 0.41
    )  # rejected <3


# mutation-probe: count a unique_catch=true row whose last disposition is rejected
def test_kill_rule_reproducible_from_rows_and_rejected_excluded():
    rows = _markers(30)
    rows.append(_row(5, LENS, location="only-shadow", uc=True))
    rows.append(_adj(5, "only-shadow", "accepted"))
    rows.append(_row(9, LENS, location="also-blocking", uc=True))
    rows.append(_adj(9, "also-blocking", "accepted"))
    rows.append(_row(9, "merge-gate-concurrency", location="also-blocking"))  # (a) fails
    rows.append(_row(12, LENS, location="later-rejected", uc=True))
    rows.append(_adj(12, "later-rejected", "rejected"))
    d = st.decide(rows, LENS)
    assert d["scored"] == 30 and d["unique"] == 1 and d["decision"] == "kill"
    rows.append(_row(20, LENS, location="second", uc=True))
    rows.append(_adj(20, "second", "accepted"))
    assert st.decide(rows, LENS)["decision"] == "keep"


# mutation-probe: drop the `awaiting` arm of the pending rule (verdict at n rounds regardless)
def test_no_verdict_while_a_sampled_finding_is_undisposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """C-HE-29 §4: adjudication precedes counting. With n rounds scored but a sampled lens
    finding still undisposed the decision is pending and nothing is delivered; once it is
    disposed the verdict follows (codex r6 P2)."""
    p = tmp_path / "g.jsonl"
    for r in _markers(30):
        fr.append_row(r, p)
    x = _row(7, LENS, location="x", uc=None)
    fr.append_row(x, p)
    seen: list[tuple] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *a: seen.append(a))
    d = st.decide(fr.read_rows(p), LENS)
    assert d["scored"] == 30 and d["decision"] == "pending"
    assert d["awaiting"] == [x["finding_id"]]
    st.hitl_request(d, LENS)  # the evidence names the open adjudication
    assert "PENDING" in seen[-1][3] and "undisposed (adjudication pending)" in seen[-1][3]
    seen.clear()
    assert st.deliver_decision(LENS, p)["delivered"] is False and seen == []
    fr.append_row(_adj(7, "x", "accepted", ts="2026-08-18T00:00:08Z"), p)
    d = st.deliver_decision(LENS, p)
    assert d["decision"] == "kill" and d["awaiting"] == [] and d["delivered"] is True
    assert len(seen) == 1


# mutation-probe: drop the is_blocking_producer arm of validate_lens
def test_a_blocking_reviewer_is_never_a_lens(monkeypatch: pytest.MonkeyPatch):
    """codex r6 P2: `decide codex_review_wrapper --hitl` would score the loop's own verdict
    rows as a trial and enqueue a HITL over them. The lens is parsed at the CLI edge, before
    any read or write."""
    assert st.validate_lens(LENS) == LENS
    monkeypatch.setattr(st.fr, "read_rows", lambda *a, **k: pytest.fail("read before parse"))
    monkeypatch.setattr(st.fr, "append_derived", lambda *a, **k: pytest.fail("write"))
    for lens in ("codex_review_wrapper", "merge-gate-concurrency", "", "TBD"):
        for argv in (
            ["decide", "--lens", lens, "--hitl"],
            ["config", "--lens", lens],
            ["request-adjudications", "--lens", lens],
        ):
            with pytest.raises(ValueError, match="never a blocking reviewer"):
                st.main(argv)


# mutation-probe: treat every non-lens producer as blocking (drop is_blocking_producer)
def test_operational_producers_never_disqualify_a_catch():
    """A merge-door or concurrency-probe row is not a review; only loop producers and
    merge-gate lenses block (codex r1 P2)."""
    rows = _markers(30)
    rows.append(_row(4, LENS, location="op-only", uc=True))
    rows.append(_adj(4, "op-only", "accepted"))
    rows.append(_row(4, "merge-door-post-merge-ci", location="op-only"))
    rows.append(_row(4, "reviewer_concurrency_probe", location="op-only"))
    assert [c["location"] for c in st.unique_catches(rows, LENS)] == ["op-only"]
    rows.append(_row(4, "codex_review_wrapper", location="op-only"))
    assert st.unique_catches(rows, LENS) == []
    assert st.is_blocking_producer("merge-gate-witness-adequacy")
    assert not st.is_blocking_producer("merge-door-lease-acquire")


# mutation-probe: count unique catches from ALL rows instead of the first-n sample in decide()
def test_sample_frozen_at_first_n_rounds_in_append_order():
    """30 scored rounds with 1 unique catch → kill. A round-31 catch appended later must NOT
    flip it — even with a REGRESSED timestamp that would sort it before the cutoff (append
    order is the record's authority, C-HE-24 §5; codex r1 P2); a later adjudication of an
    in-sample finding still counts."""
    rows = _markers(30)
    rows.append(_row(3, LENS, location="in-sample", uc=True))
    rows.append(_adj(3, "in-sample", "accepted"))
    assert st.decide(rows, LENS)["decision"] == "kill"
    early = "2026-08-17T00:00:00Z"  # regressed clock: earlier than every sampled row
    rows.append(_row(31, LENS, location="late", uc=True, ts=early))
    rows.append(_adj(31, "late", "accepted", ts=early))
    d = st.decide(rows, LENS)
    assert d["decision"] == "kill" and d["scored"] == 31 and d["unique"] == 1
    assert ("pr-1", 31) not in d["sample"] and d["sample"][0] == ("pr-1", 1)


# mutation-probe: ignore config rows in decide() (always use the code defaults)
def test_rule_read_from_the_last_config_row_and_amendable():
    """The rule is a row: an amended threshold changes the decision from rows alone, and each
    config row is a NEW observation (distinct id), never a rewrite (codex r1 P2 ×2)."""
    rows = _markers(30)
    rows.append(_row(7, LENS, location="one", uc=True))
    rows.append(_adj(7, "one", "accepted"))
    assert st.decide(rows, LENS)["decision"] == "kill"  # default threshold 2
    c1 = st.config_row(lens=LENS, rows=rows)
    fr.validate(c1)
    rows.append(c1)
    c2 = st.config_row(lens=LENS, rows=rows, threshold=1)
    fr.validate(c2)
    assert c2["finding_id"] != c1["finding_id"]
    rows.append(c2)
    assert st.rule_from_rows(rows, LENS) == (30, 1)
    d = st.decide(rows, LENS)
    assert d["decision"] == "keep" and d["threshold"] == 1
    assert st.decide(rows, LENS, threshold=2)["decision"] == "kill"  # explicit override


def test_config_row_bounds_the_rule():
    """1 <= threshold <= n: a threshold of 0 can never kill and one above n can never keep."""
    for n, threshold in ((30, 0), (30, 31), (0, 1)):
        with pytest.raises(ValueError):
            st.config_row(lens=LENS, rows=[], n=n, threshold=threshold)
    fr.validate(st.config_row(lens=LENS, rows=[], n=30, threshold=30))


def test_pending_before_n_and_config_row_recorded():
    d = st.decide(_markers(9), LENS)
    assert d["decision"] == "pending" and d["n"] == 30
    c = st.config_row(lens=LENS, rows=[])
    fr.validate(c)  # the config row is a real C-HE-24 row, not a private shape
    assert c["record_kind"] == "no_finding"
    assert "n=30" in c["observed_evidence"] and "kill_if_fewer_than=2" in c["observed_evidence"]


# mutation-probe: reduce scored rounds to round_n alone (drop arc_id from the key)
def test_scored_rounds_are_per_arc():
    """Two arcs each with rounds 1..15 = 30 scored rounds; keyed on round_n alone it would be 15."""
    rows = _markers(15, arc="pr-1") + _markers(15, arc="pr-2")
    assert len(st.scored_rounds(rows, LENS)) == 30
    assert st.decide(rows, LENS)["decision"] == "kill"  # n reached, 0 unique catches


# mutation-probe: hard-code unique_catch=True in the adjudication row (drop the blocking check)
def test_adjudicate_computes_unique_catch_and_persists(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    only = _row(4, LENS, location="only-shadow", uc=None)
    fr.append_row(only, p)
    both = _row(5, LENS, location="seen-by-blocking", uc=None)
    fr.append_row(both, p)
    fr.append_row(_row(5, "merge-gate-concurrency", location="seen-by-blocking"), p)
    a1 = st.adjudicate(
        only["finding_id"], disposition="accepted", actor="operator", lens=LENS, path=p
    )
    a2 = st.adjudicate(
        both["finding_id"], disposition="accepted", actor="operator", lens=LENS, path=p
    )
    assert a1["unique_catch"] is True and a2["unique_catch"] is False
    rows = fr.read_rows(p)
    assert [c["finding_id"] for c in st.unique_catches(rows, LENS)] == [only["finding_id"]]
    assert sum(1 for r in rows if r["record_kind"] == "finding_adjudication") == 2  # persisted
    with pytest.raises(ValueError):
        st.adjudicate(
            only["finding_id"], disposition="accepted", actor="gemini-review", lens=LENS, path=p
        )


def test_adjudicate_refuses_a_non_lens_finding(tmp_path: Path):
    """Only the shadow lens's own findings get shadow-trial adjudications (codex r1 P2)."""
    p = tmp_path / "g.jsonl"
    other = _row(2, "codex_review_wrapper", location="x")
    fr.append_row(other, p)
    with pytest.raises(ValueError, match="not the shadow lens"):
        st.adjudicate(
            other["finding_id"], disposition="accepted", actor="operator", lens=LENS, path=p
        )
    assert len(fr.read_rows(p)) == 1


def test_adjudicate_reads_the_log_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The blocking-row check and the append are ONE critical section: a blocking row that is
    on the log by the time the lock is held is seen, even if it was absent at call time
    (codex r1 P2, TOCTOU)."""
    p = tmp_path / "g.jsonl"
    mine = _row(6, LENS, location="racy", uc=None)
    fr.append_row(mine, p)
    real_read = fr._read_rows_fd

    def read_with_late_blocking_row(fd: int, path: Path) -> list[dict]:
        rows = real_read(fd, path)
        rows.append(_row(6, "codex_review_wrapper", location="racy"))
        return rows

    monkeypatch.setattr(fr, "_read_rows_fd", read_with_late_blocking_row)
    a = st.adjudicate(
        mine["finding_id"], disposition="accepted", actor="operator", lens=LENS, path=p
    )
    assert a["unique_catch"] is False


def test_adjudicate_unknown_finding_is_loud(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    fr.append_row(_row(1, LENS, location="x"), p)
    with pytest.raises(ValueError):
        st.adjudicate(
            "gemini-shadow:" + HEAD + ":000000000000:9",
            disposition="accepted",
            actor="operator",
            lens=LENS,
            path=p,
        )


def test_adjudicator_never_placeholder_or_same_family():
    for bad in ("TODO", "tbd", " ", "gemini-review", "agy", "claude-absorber", "anthropic-ops"):
        with pytest.raises(ValueError):
            st.validate_adjudicator(bad)
    st.validate_adjudicator("operator")
    st.validate_adjudicator("codex-review")


def test_hitl_request_presents_the_sample_and_every_disposition(monkeypatch: pytest.MonkeyPatch):
    """C-HE-29 §4: the request carries the sampled rounds and the unique_catch dispositions,
    not only the aggregate counts (codex r1 P2)."""
    seen: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *a: seen.append(a))
    rows = _markers(30)
    rows.append(_row(5, LENS, location="a", uc=True))
    rows.append(_adj(5, "a", "accepted"))
    rows.append(_row(12, LENS, location="b", uc=True))
    rows.append(_adj(12, "b", "rejected"))
    rows.append(_row(20, LENS, location="c", uc=True))
    rows.append(_adj(20, "c", "accepted"))
    rows.append(_row(20, "codex_review_wrapper", location="c"))  # blocked
    rows.append(_row(31, LENS, location="d", uc=True))
    rows.append(_adj(31, "d", "accepted"))  # accepted but outside the frozen sample
    rows.append(_row(8, LENS, location="e", uc=None))
    rows.append(  # disposed, never a catch: presented
        _row(
            8,
            LENS,
            kind="finding_adjudication",
            location="e",
            disp="suppressed",
            actor="operator",
            uc=None,
            ts="2026-08-18T00:00:09Z",
        )
    )
    rows.append(_row(9, LENS, location="f", uc=None))
    rows.append(
        _row(
            9,
            LENS,
            kind="finding_adjudication",
            location="f",
            disp="accepted",
            actor="operator",
            uc=False,
        )
    )  # adjudicated NOT unique: still presented
    d = st.decide(rows, LENS)
    assert d["unique"] == 1 and d["decision"] == "kill"
    assert [c["counted"] for c in d["catches"]].count(True) == 1
    assert len(d["catches"]) == 6  # every lens finding is presented, each with its reason
    st.hitl_request(d, LENS)
    ((kind, _lane, cause, detail),) = seen
    assert kind == "DEFERRED-HIL" and cause.startswith("shadow-trial-adjudicate")
    assert "n=30 scored=31" in detail and "unique=1" in detail and "KILL" in detail
    assert "pr-1/r1" in detail and "pr-1/r30" in detail
    assert "/r5=accepted [COUNTED]" in detail
    assert "/r12=rejected [not counted: last disposition rejected]" in detail
    assert "/r20=accepted [not counted: a blocking reviewer reported the same key]" in detail
    assert "/r8=suppressed [not counted: adjudicated unique_catch=false]" in detail
    assert "/r31=accepted [not counted: outside the frozen sample]" in detail
    assert "/r9=accepted [not counted: adjudicated unique_catch=false]" in detail
    assert "approve-kill" in detail and "reject-keep" in detail and "amend-threshold" in detail


# mutation-probe: drop the decision_recorded() check in deliver_decision (always deliver)
def test_decision_is_delivered_once_per_frozen_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`decide --hitl` after round n enqueues the kill/keep request ONCE; the marker row on
    the log is the memory, so re-running after round 31 does not enqueue it again (codex r3)."""
    p = tmp_path / "g.jsonl"
    for r in _markers(30):
        fr.append_row(r, p)
    seen: list[tuple] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *a: seen.append(a))
    d1 = st.deliver_decision(LENS, p)
    assert d1["decision"] == "kill" and d1["delivered"] is True and len(seen) == 1
    d2 = st.deliver_decision(LENS, p)
    assert d2["delivered"] is False and len(seen) == 1
    fr.append_row(_row(31, LENS, kind="no_finding", location="gemini", n=31), p)
    assert st.deliver_decision(LENS, p)["delivered"] is False and len(seen) == 1
    markers = [r for r in fr.read_rows(p) if r["finding_type"] == st.DECISION_TYPE]
    assert len(markers) == 1 and "decision=kill" in markers[0]["observed_evidence"]


# mutation-probe: drop n/threshold/decision from delivery_identity (key on the sample only)
def test_an_amended_threshold_is_a_new_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """amend-threshold is a permitted response: after it, the same frozen sample with the new
    rule (and possibly a new outcome) is delivered again; the same rule is not (codex r4 P2)."""
    p = tmp_path / "g.jsonl"
    for r in _markers(30):
        fr.append_row(r, p)
    fr.append_row(_row(7, LENS, location="one", uc=True), p)
    fr.append_row(_adj(7, "one", "accepted", ts="2026-08-18T00:00:08Z"), p)
    seen: list[tuple] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *a: seen.append(a))
    assert st.deliver_decision(LENS, p)["decision"] == "kill" and len(seen) == 1
    assert st.deliver_decision(LENS, p)["delivered"] is False and len(seen) == 1
    # an amended rule with the SAME outcome is still a new proposal (the rule is part of
    # the identity, not only the outcome): threshold 2 -> 3 keeps kill and is re-delivered
    fr.append_row(st.config_row(lens=LENS, rows=fr.read_rows(p), threshold=3), p)  # amend
    d = st.deliver_decision(LENS, p)
    assert d["decision"] == "kill" and d["delivered"] is True and len(seen) == 2
    assert "threshold=3" in seen[1][3] and "KILL" in seen[1][3]
    assert st.deliver_decision(LENS, p)["delivered"] is False and len(seen) == 2
    fr.append_row(st.config_row(lens=LENS, rows=fr.read_rows(p), threshold=1), p)  # amend
    d = st.deliver_decision(LENS, p)
    assert d["decision"] == "keep" and d["delivered"] is True and len(seen) == 3
    assert "threshold=1" in seen[2][3] and "KEEP" in seen[2][3]
    assert st.deliver_decision(LENS, p)["delivered"] is False and len(seen) == 3


# mutation-probe: append the decision marker before hitl_request (mark-then-emit)
def test_a_failed_delivery_leaves_no_marker_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The marker means DELIVERED: it is written only after the HITL row is emitted, so a
    failed emission leaves nothing behind and the next run emits again (codex r4 P2)."""
    p = tmp_path / "g.jsonl"
    for r in _markers(30):
        fr.append_row(r, p)

    def boom(*a):
        raise RuntimeError("loop ledger unavailable")

    monkeypatch.setattr(st, "_emit_loop_row", boom)
    with pytest.raises(RuntimeError):
        st.deliver_decision(LENS, p)
    assert not [r for r in fr.read_rows(p) if r["finding_type"] == st.DECISION_TYPE]
    seen: list[tuple] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *a: seen.append(a))
    assert st.deliver_decision(LENS, p)["delivered"] is True and len(seen) == 1
    assert len([r for r in fr.read_rows(p) if r["finding_type"] == st.DECISION_TYPE]) == 1


# mutation-probe: append the request marker before _emit_loop_row (mark-then-emit)
def test_a_failed_request_emission_is_retried_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    p = tmp_path / "g.jsonl"
    a = _row(1, LENS, location="a")
    fr.append_row(a, p)

    def boom(*x):
        raise RuntimeError("loop ledger unavailable")

    monkeypatch.setattr(st, "_emit_loop_row", boom)
    with pytest.raises(RuntimeError):
        st.request_adjudications(LENS, p)
    assert not [r for r in fr.read_rows(p) if r["finding_type"] == st.REQUEST_TYPE]
    seen: list[tuple] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *x: seen.append(x))
    assert [f["finding_id"] for f in st.request_adjudications(LENS, p)] == [a["finding_id"]]
    assert len(seen) == 1
    assert len([r for r in fr.read_rows(p) if r["finding_type"] == st.REQUEST_TYPE]) == 1


# mutation-probe: collect every pending finding into one build (batch markers after all emissions)
def test_a_marker_persists_per_finding_before_the_next_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A delivered finding is remembered even when a LATER finding's emission fails: its marker
    is on disk before the next emission is attempted, so the retry emits only the finding that
    never reached the operator (codex r5 P3)."""
    p = tmp_path / "g.jsonl"
    a = _row(1, LENS, location="a")
    b = _row(1, LENS, location="b")
    fr.append_row(a, p)
    fr.append_row(b, p)
    seen: list[tuple] = []

    def second_fails(*x):
        seen.append(x)
        if len(seen) == 2:
            raise RuntimeError("loop ledger unavailable")

    monkeypatch.setattr(st, "_emit_loop_row", second_fails)
    with pytest.raises(RuntimeError):
        st.request_adjudications(LENS, p)
    markers = [r["location"] for r in fr.read_rows(p) if r["finding_type"] == st.REQUEST_TYPE]
    assert markers == [a["finding_id"]]
    monkeypatch.setattr(st, "_emit_loop_row", lambda *x: seen.append(x))
    assert [f["finding_id"] for f in st.request_adjudications(LENS, p)] == [b["finding_id"]]
    assert len(seen) == 3
    markers = [r["location"] for r in fr.read_rows(p) if r["finding_type"] == st.REQUEST_TYPE]
    assert markers == [a["finding_id"], b["finding_id"]]


def test_decision_is_computed_from_the_rows_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A row that lands by lock time (here: injected at the locked read) is part of the
    delivered decision; the proposal never describes a stale snapshot (codex r4 P2)."""
    p = tmp_path / "g.jsonl"
    for r in _markers(30):
        fr.append_row(r, p)
    fr.append_row(_row(7, LENS, location="one", uc=True), p)
    fr.append_row(_adj(7, "one", "accepted", ts="2026-08-18T00:00:08Z"), p)
    real_read = fr._read_rows_fd

    def read_with_late_config(fd: int, path: Path) -> list[dict]:
        rows = real_read(fd, path)
        rows.append(st.config_row(lens=LENS, rows=rows, threshold=1))  # amendment lands late
        return rows

    monkeypatch.setattr(fr, "_read_rows_fd", read_with_late_config)
    seen: list[tuple] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *x: seen.append(x))
    d = st.deliver_decision(LENS, p)
    assert d["decision"] == "keep" and d["threshold"] == 1 and "KEEP" in seen[0][3]


# mutation-probe: drop the `already` filter in request_adjudications (re-request every run)
def test_every_undisposed_shadow_finding_is_requested_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """C-HE-29 §4: each shadow finding becomes a `shadow-trial-adjudicate` HITL row, once;
    an adjudicated finding is never requested (codex r3 P2)."""
    p = tmp_path / "g.jsonl"
    a = _row(1, LENS, location="a")
    b = _row(2, LENS, location="b")
    fr.append_row(a, p)
    fr.append_row(b, p)
    fr.append_row(
        _row(
            2,
            LENS,
            kind="finding_adjudication",
            location="b",
            disp="rejected",
            actor="operator",
            uc=False,
            ts="2026-08-18T00:00:03Z",  # an adjudication is later than its finding
        ),
        p,
    )
    seen: list[tuple] = []
    monkeypatch.setattr(st, "_emit_loop_row", lambda *x: seen.append(x))
    first = st.request_adjudications(LENS, p)
    assert [f["finding_id"] for f in first] == [a["finding_id"]]
    assert (
        len(seen) == 1 and a["finding_id"] in seen[0][3] and "shadow-trial-adjudicate" in seen[0][3]
    )
    assert st.request_adjudications(LENS, p) == [] and len(seen) == 1  # idempotent
    c = _row(3, LENS, location="c")
    fr.append_row(c, p)
    assert [f["finding_id"] for f in st.request_adjudications(LENS, p)] == [c["finding_id"]]
    markers = [r for r in fr.read_rows(p) if r["finding_type"] == st.REQUEST_TYPE]
    assert sorted(r["location"] for r in markers) == sorted([a["finding_id"], c["finding_id"]])


def test_cli_oc_decide_and_config_if_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    p = tmp_path / "g.jsonl"
    for r in _markers(3):
        fr.append_row(r, p)
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", p)
    assert st.main(["oc"]) == 0
    assert "p=0.10  P(kill)=0.184" in capsys.readouterr().out
    assert st.main(["decide", "--lens", LENS]) == 0
    assert '"decision": "pending"' in capsys.readouterr().out
    assert len(fr.read_rows(p)) == 3  # decide never writes
    assert st.main(["decide", "--lens", LENS, "--hitl"]) == 0
    assert '"delivered": false' in capsys.readouterr().out
    assert len(fr.read_rows(p)) == 3  # a pending decision writes no marker either
    assert st.main(["config", "--lens", LENS, "--if-absent"]) == 0
    assert st.main(["config", "--lens", LENS, "--if-absent"]) == 0
    assert "already present" in capsys.readouterr().out
    assert sum(1 for r in fr.read_rows(p) if r["producer"] == st.CONFIG_PRODUCER) == 1
    assert st.main(["config", "--lens", LENS, "--threshold", "1"]) == 0  # an amendment appends
    assert st.rule_from_rows(fr.read_rows(p), LENS) == (30, 1)
