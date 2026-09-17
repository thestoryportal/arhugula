#!/usr/bin/env python3
"""C-HE-29 shadow trial: the second reviewer's lens runs live, OFF the blocking path; its value
is measured from `merge-gate-log.jsonl` rows alone. The kill rule is pre-committed (n=30 scored
rounds; kill if fewer than 2 unique catches) with its operating characteristics stated. Wall
clock is NOT a kill criterion.

Vocabulary (C-HE-24 §2, C-HE-29 §2):
- a SCORED round is a distinct (arc_id, round_n) carrying a `finding` or `no_finding` row from the
  lens (round numbers restart per arc, so round_n alone would under-count across arcs);
- a UNIQUE catch is a lens finding whose LAST row is an accepted adjudication with
  `unique_catch=true`, and whose (head_sha, location, finding_type) no blocking reviewer also
  reported; a later `rejected` MUST NOT count (Invariants);
- the SAMPLE is the first n scored rounds by earliest row ts, frozen: a catch scored after the
  n-th round never enters the count, so a late catch cannot flip kill → keep.

`adjudicate` is the ONE production writer of `unique_catch` for the lens ([LAW:single-enforcer]).
"""

from __future__ import annotations

import argparse
import json
from math import comb
from pathlib import Path

import finding_record as fr

N_ROUNDS = 30
KILL_IF_FEWER_THAN = 2
SCORED_KINDS = ("finding", "no_finding")
#: The two families under trial: the shadow lens (gemini) and the diff's author (Claude). An
#: adjudicator must belong to NEITHER (C-HE-29 §4); the openai family is the third party.
MODEL_FAMILIES = {
    "gemini": ("gemini", "agy", "antigravity", "google"),
    "anthropic": ("claude", "anthropic"),
}
PLACEHOLDERS = ("", "todo", "tbd", "placeholder", "n/a", "-")


def config_row(*, lens: str, n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN) -> dict:
    """The rule as a row, so an amendment is auditable from the log (C-HE-29 §3)."""
    core = fr.FindingCore(
        fr.make_finding_id("shadow_trial", "config", lens, 0),
        lens,
        f"shadow-trial config: n={n} kill_if_fewer_than={threshold}",
        "C-HE-29 §3",
        "info",
        "config",
        "policy",
        "shadow_trial",
    )
    env = fr.Envelope("no_finding", fr.now_iso(), "policy", "shadow_trial", None, None, None, None)
    return fr.make_row(core, env)


def _scored(rows: list[dict], lens: str) -> list[dict]:
    return [
        r
        for r in rows
        if r["producer"] == lens and r["record_kind"] in SCORED_KINDS and r["round_n"] is not None
    ]


def scored_rounds(rows: list[dict], lens: str) -> set[tuple[str, int]]:
    """Distinct (arc_id, round_n) the lens scored — the denominator of C-HE-29 §2."""
    return {(r["arc_id"], r["round_n"]) for r in _scored(rows, lens)}


def _blocking_keys(rows: list[dict], lens: str) -> set[tuple[str | None, str, str]]:
    return {
        (r["head_sha"], r["location"], r["finding_type"])
        for r in rows
        if r["producer"] != lens and r["record_kind"] == "finding"
    }


def unique_catches(rows: list[dict], lens: str) -> list[dict]:
    """Lens findings satisfying (a) no blocking reviewer reported the same
    (head_sha, location, finding_type) and (b) the LAST row is an accepted adjudication."""
    last = fr.reduce_last_by_finding_id(rows)
    blocking = _blocking_keys(rows, lens)
    return [
        r
        for r in last.values()
        if r["producer"] == lens
        and r.get("unique_catch")
        and (r["head_sha"], r["location"], r["finding_type"]) not in blocking
        and r.get("disposition") == "accepted"  # (b): a later `rejected` never counts
    ]


def first_n_rounds(rows: list[dict], lens: str, n: int) -> set[tuple[str, int]]:
    """The pre-committed sample: the first n scored rounds by earliest row ts, ties broken by
    key so the set is deterministic. Adjudication rows for findings INSIDE the sample still
    count later (the reducer takes the last row per finding_id); rounds after the n-th never do."""
    first_ts: dict[tuple[str, int], str] = {}
    for r in _scored(rows, lens):
        key = (r["arc_id"], r["round_n"])
        first_ts[key] = min(first_ts.get(key, r["ts"]), r["ts"])
    return set(sorted(first_ts, key=lambda k: (first_ts[k], k))[:n])


def decide(
    rows: list[dict], lens: str, *, n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN
) -> dict:
    """Reproducible from rows alone (C-HE-29 Invariants): pending until n scored rounds, then
    kill iff the sample holds fewer than `threshold` unique catches."""
    k = len(scored_rounds(rows, lens))
    if k < n:
        return {
            "scored": k,
            "unique": len(unique_catches(rows, lens)),
            "decision": "pending",
            "n": n,
            "threshold": threshold,
        }
    sample = first_n_rounds(rows, lens, n)
    u = sum(1 for c in unique_catches(rows, lens) if (c["arc_id"], c["round_n"]) in sample)
    return {
        "scored": k,
        "unique": u,
        "decision": "kill" if u < threshold else "keep",
        "n": n,
        "threshold": threshold,
        "sample": sorted(sample),
    }


def p_kill(p: float, n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN) -> float:
    """P(kill | true per-round unique-catch rate p) = binomial P(X < threshold)."""
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(threshold))


def oc_table() -> list[tuple[float, float]]:
    return [(p, p_kill(p)) for p in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25)]


def validate_adjudicator(actor: str) -> None:
    """Neither model family under trial, and never a placeholder (C-HE-29 §4)."""
    a = actor.strip().lower()
    if a in PLACEHOLDERS:
        raise ValueError("adjudicator must be a specific identity, never a placeholder")
    for family, tokens in MODEL_FAMILIES.items():
        if any(tok in a for tok in tokens):
            raise ValueError(
                f"adjudicator {actor!r} belongs to the {family} family under trial; the "
                "adjudicator must be the operator or a third-party identity of NEITHER family"
            )


def adjudicate(
    finding_id: str,
    *,
    disposition: str,
    actor: str,
    rows: list[dict] | None = None,
    path: Path | None = None,
) -> dict:
    """Append the adjudication row for a shadow-lens finding with `unique_catch` = (a) computed
    against the blocking reviewers' rows for the same head_sha; (b), the accepted disposition,
    is what `unique_catches` then requires. Injected `rows` are for pure evaluation; every
    production call (no rows, or a path) persists — a decision that is not on the log does not
    exist (C-HE-29 Invariants)."""
    validate_adjudicator(actor)
    supplied = rows is not None
    if not supplied:
        rows = fr.read_rows(path)
    assert rows is not None
    orig = next(
        (r for r in rows if r["finding_id"] == finding_id and r["record_kind"] == "finding"), None
    )
    if orig is None:  # [LAW:no-silent-failure] an unknown id is an error, never an empty row
        raise ValueError(f"no finding row for {finding_id}")
    uc = (orig["head_sha"], orig["location"], orig["finding_type"]) not in _blocking_keys(
        rows, orig["producer"]
    )
    core = fr.FindingCore(
        **{
            k: orig[k]
            for k in (
                "finding_id",
                "location",
                "observed_evidence",
                "expected_contract",
                "severity",
                "finding_type",
                "lineage_claim",
                "producer",
            )
        }
    )
    env = fr.Envelope(
        "finding_adjudication",
        fr.now_iso(),
        orig["arc_id"],
        orig["lane_id"],
        orig["head_sha"],
        orig["base_sha"],
        orig["diff_digest"],
        orig["round_n"],
        cause_attribution=orig["cause_attribution"],
        disposition=disposition,
        disposition_actor=actor,
        unique_catch=uc,
    )
    row = fr.make_row(core, env)
    if not supplied or path is not None:
        fr.append_row(row, path)
    return row


def _emit_loop_row(kind: str, lane_id: str, cause: str, detail: str) -> None:
    import reservations as rs  # the loop ledger's one writer; imported at the effect boundary

    rs.emit_loop_row(kind, lane_id, cause, detail)


def hitl_request(decision: dict, lens: str) -> None:
    """C-HE-29 §4: the kill/keep evaluation is delivered as an escalation-kind HITL request
    naming the counts and the three permitted responses; nothing is adopted or killed here."""
    _emit_loop_row(
        "DEFERRED-HIL",
        "shadow_trial",
        "shadow-trial-adjudicate:HITL-recoverable:kill_keep_decision",
        f"SHADOW-{lens} — n={decision['scored']} unique={decision['unique']} "
        f"threshold={decision['threshold']} → proposed {decision['decision'].upper()}; "
        "respond approve-kill | reject-keep | amend-threshold",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("decide", help="kill / keep / pending from the gate log (read-only)")
    s.add_argument("--lens", required=True)
    s.add_argument("--hitl", action="store_true", help="deliver a non-pending decision as HITL")
    sub.add_parser("oc", help="print the operating-characteristics table")
    c = sub.add_parser("config", help="append the rule as a config row")
    c.add_argument("--lens", required=True)
    ad = sub.add_parser("adjudicate", help="dispose one shadow finding (writes unique_catch)")
    ad.add_argument("finding_id")
    ad.add_argument("--disposition", choices=("accepted", "rejected", "suppressed"), required=True)
    ad.add_argument("--actor", required=True)
    a = p.parse_args(argv)
    if a.cmd == "adjudicate":
        print(json.dumps(adjudicate(a.finding_id, disposition=a.disposition, actor=a.actor)))
        return 0
    if a.cmd == "oc":
        for pv, pk in oc_table():
            print(f"p={pv:.2f}  P(kill)={pk:.3f}")
        return 0
    if a.cmd == "config":
        fr.append_row(config_row(lens=a.lens))
        return 0
    d = decide(fr.read_rows(), a.lens)
    print(json.dumps(d))
    if a.hitl and d["decision"] != "pending":
        hitl_request(d, a.lens)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
