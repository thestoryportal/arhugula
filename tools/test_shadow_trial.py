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
    d = st.decide(rows, LENS)
    st.hitl_request(d, LENS)
    ((kind, _lane, cause, detail),) = seen
    assert kind == "DEFERRED-HIL" and cause.startswith("shadow-trial-adjudicate")
    assert "n=30" in detail and "unique=1" in detail and "KILL" in detail
    assert "pr-1/r1" in detail and "pr-1/r30" in detail
    assert "/r5=accepted" in detail and "/r12=rejected" in detail
    assert "approve-kill" in detail and "reject-keep" in detail and "amend-threshold" in detail


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
    assert st.main(["config", "--lens", LENS, "--if-absent"]) == 0
    assert st.main(["config", "--lens", LENS, "--if-absent"]) == 0
    assert "already present" in capsys.readouterr().out
    assert sum(1 for r in fr.read_rows(p) if r["producer"] == st.CONFIG_PRODUCER) == 1
    assert st.main(["config", "--lens", LENS, "--threshold", "1"]) == 0  # an amendment appends
    assert st.rule_from_rows(fr.read_rows(p), LENS) == (30, 1)
