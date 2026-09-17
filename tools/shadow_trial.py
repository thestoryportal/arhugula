#!/usr/bin/env python3
"""C-HE-29 shadow trial: the second reviewer's lens runs live, OFF the blocking path; its value
is measured from `merge-gate-log.jsonl` rows alone. The kill rule is pre-committed (n=30 scored
rounds; kill if fewer than 2 unique catches) with its operating characteristics stated. Wall
clock is NOT a kill criterion.

Vocabulary (C-HE-24 §2, C-HE-29 §2):
- a SCORED round is a distinct (arc_id, round_n) carrying a `finding` or `no_finding` row from the
  lens (round numbers restart per arc, so round_n alone would under-count across arcs);
- a BLOCKING reviewer is a loop producer or a merge-gate lens — never an operational producer
  (merge-door rows, the concurrency probe), whose rows are not reviews;
- a UNIQUE catch is a lens finding whose LAST row is an accepted adjudication with
  `unique_catch=true`, and whose (head_sha, location, finding_type) no blocking reviewer also
  reported; a later `rejected` MUST NOT count (Invariants);
- the SAMPLE is the first n scored rounds in APPEND order (the log's ordering authority per
  C-HE-24 §5; emitter timestamps may regress), frozen: a round scored after the n-th never
  enters the count, so a late catch cannot flip kill → keep;
- the RULE (n, threshold) is read from the lens's LAST config row on the log when one exists,
  so an amendment is itself a row and the decision stays reproducible from rows alone.

The trial's authorship premise: the diff under review is Claude-authored, so the blocking
reviewer is codex and the gemini shadow lens is a SECOND family. Where Gemini is already the
blocking reviewer (a Codex-authored change; `just gemini-review` on the blocking path) there is
no second family to measure and the carriers do not run the shadow (codex r5 P2) — which is also
why MODEL_FAMILIES names gemini and anthropic, never openai, as the families under trial.

`adjudicate` is the ONE production writer of `unique_catch` for the lens ([LAW:single-enforcer]);
it derives the value and appends under the same log lock ([LAW:no-ambient-temporal-coupling]).
"""

from __future__ import annotations

import argparse
import json
from math import comb
from pathlib import Path

import finding_record as fr
from review_loop_gate import LOOP_PRODUCERS

N_ROUNDS = 30
KILL_IF_FEWER_THAN = 2
SCORED_KINDS = ("finding", "no_finding")
CONFIG_PRODUCER = "shadow_trial"
CONFIG_TYPE = "config"
DECISION_TYPE = "decision"  # the round-n decision, recorded once per frozen sample
REQUEST_TYPE = "adjudication-requested"  # one marker per shadow finding handed to HITL
MERGE_GATE_PREFIX = "merge-gate-"
#: The two families under trial: the shadow lens (gemini) and the diff's author (Claude). An
#: adjudicator must belong to NEITHER (C-HE-29 §4); the openai family is the third party.
MODEL_FAMILIES = {
    "gemini": ("gemini", "agy", "antigravity", "google"),
    "anthropic": ("claude", "anthropic"),
}
PLACEHOLDERS = ("", "todo", "tbd", "placeholder", "n/a", "-")


def is_blocking_producer(producer: str) -> bool:
    """A reviewer whose finding blocks: the loop producers (one source of truth in the review
    gate) and the merge-gate lenses. Operational rows never disqualify a shadow catch."""
    return producer in LOOP_PRODUCERS or producer.startswith(MERGE_GATE_PREFIX)


def config_row(
    *, lens: str, rows: list[dict], n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN
) -> dict:
    """The rule as a row, its id minted against `rows` so every amendment is a NEW observation
    (an immutable core cannot be rewritten under one id — C-HE-24 §5). The bound is the rule's
    own: a kill needs `threshold` catches to be reachable, so 1 <= threshold <= n (§3)."""
    if not 1 <= threshold <= n:  # [LAW:parse-dont-validate] the row is the stamped rule
        raise ValueError(
            f"shadow-trial rule needs 1 <= kill_if_fewer_than <= n, got {threshold}/{n}"
        )
    fid = fr.next_finding_id(CONFIG_PRODUCER, CONFIG_TYPE, lens, rows)
    core = fr.FindingCore(
        fid,
        lens,
        f"shadow-trial config: n={n} kill_if_fewer_than={threshold}",
        "C-HE-29 §3",
        "info",
        CONFIG_TYPE,
        "policy",
        CONFIG_PRODUCER,
    )
    env = fr.Envelope("no_finding", fr.now_iso(), "policy", CONFIG_PRODUCER, None, None, None, None)
    return fr.make_row(core, env)


def rule_from_rows(rows: list[dict], lens: str) -> tuple[int, int]:
    """(n, threshold) from the lens's LAST config row in append order; the code defaults only
    when no config row exists."""
    n, threshold = N_ROUNDS, KILL_IF_FEWER_THAN
    for r in rows:
        if (
            r["producer"] == CONFIG_PRODUCER
            and r["finding_type"] == CONFIG_TYPE
            and r["location"] == lens
        ):
            for token in r["observed_evidence"].split():
                key, _, value = token.partition("=")
                if key == "n":
                    n = int(value)
                elif key == "kill_if_fewer_than":
                    threshold = int(value)
    return n, threshold


def _scored(rows: list[dict], lens: str) -> list[dict]:
    return [
        r
        for r in rows
        if r["producer"] == lens and r["record_kind"] in SCORED_KINDS and r["round_n"] is not None
    ]


def scored_rounds(rows: list[dict], lens: str) -> set[tuple[str, int]]:
    """Distinct (arc_id, round_n) the lens scored — the denominator of C-HE-29 §2."""
    return {(r["arc_id"], r["round_n"]) for r in _scored(rows, lens)}


def _blocking_keys(rows: list[dict]) -> set[tuple[str | None, str, str]]:
    return {
        (r["head_sha"], r["location"], r["finding_type"])
        for r in rows
        if r["record_kind"] == "finding" and is_blocking_producer(r["producer"])
    }


def unique_catches(rows: list[dict], lens: str) -> list[dict]:
    """Lens findings satisfying (a) no blocking reviewer reported the same
    (head_sha, location, finding_type) and (b) the LAST row is an accepted adjudication."""
    last = fr.reduce_last_by_finding_id(rows)
    blocking = _blocking_keys(rows)
    return [
        r
        for r in last.values()
        if r["producer"] == lens
        and r.get("unique_catch")
        and (r["head_sha"], r["location"], r["finding_type"]) not in blocking
        and r.get("disposition") == "accepted"  # (b): a later `rejected` never counts
    ]


def first_n_rounds(rows: list[dict], lens: str, n: int) -> list[tuple[str, int]]:
    """The pre-committed sample: the first n scored rounds by first APPEARANCE in append order.
    Adjudication rows for findings inside the sample still count later (the reducer takes the
    last row per finding_id); rounds first scored after the n-th never do."""
    order: list[tuple[str, int]] = []
    for r in _scored(rows, lens):
        key = (r["arc_id"], r["round_n"])
        if key not in order:
            order.append(key)
    return order[:n]


def decide(
    rows: list[dict],
    lens: str,
    *,
    n: int | None = None,
    threshold: int | None = None,
) -> dict:
    """Reproducible from rows alone (C-HE-29 Invariants): the rule comes from the log's last
    config row (explicit `n` / `threshold` override it for evaluation), pending until n scored
    rounds AND every sampled lens finding is disposed, then kill iff the sample holds fewer
    than `threshold` unique catches. `catches`
    lists every lens finding with `unique_catch` and its last disposition, the evidence §4
    delivers to the operator."""
    logged_n, logged_threshold = rule_from_rows(rows, lens)
    n = logged_n if n is None else n
    threshold = logged_threshold if threshold is None else threshold
    k = len(scored_rounds(rows, lens))
    sample = first_n_rounds(rows, lens, n) if k >= n else []
    in_sample = set(sample)
    counted_ids = {
        c["finding_id"]
        for c in unique_catches(rows, lens)
        if k < n or (c["arc_id"], c["round_n"]) in in_sample
    }
    # EVERY lens finding, each with its last unique_catch value and disposition and WHY it
    # did or did not count — the §4 evidence is the sampled rounds' dispositions, so a
    # finding adjudicated unique_catch=false or still undisposed is presented too (codex r2
    # P2, r3 P2).
    blocking = _blocking_keys(rows)
    catches = [
        {
            "finding_id": r["finding_id"],
            "arc_id": r["arc_id"],
            "round_n": r["round_n"],
            "unique_catch": r.get("unique_catch"),
            "disposition": r.get("disposition"),
            "in_sample": k < n or (r["arc_id"], r["round_n"]) in in_sample,
            "blocked": (r["head_sha"], r["location"], r["finding_type"]) in blocking,
            "counted": r["finding_id"] in counted_ids,
        }
        for r in fr.reduce_last_by_finding_id(rows).values()
        if r["producer"] == lens and r["record_kind"] in ("finding", "finding_adjudication")
    ]
    # C-HE-29 §4: adjudication precedes counting. A sampled finding still undisposed could
    # yet become a unique catch, so no verdict is proposed until every sampled lens finding
    # is disposed (codex r6 P2); the evidence names each one "adjudication pending".
    awaiting = [c["finding_id"] for c in catches if c["in_sample"] and c["disposition"] is None]
    base = {
        "n": n,
        "threshold": threshold,
        "scored": k,
        "catches": catches,
        "awaiting": awaiting,
        "unique": len(counted_ids),
    }
    if k < n or awaiting:
        return {**base, "decision": "pending"}
    return {
        **base,
        "decision": "kill" if len(counted_ids) < threshold else "keep",
        "sample": sample,
    }


def p_kill(p: float, n: int = N_ROUNDS, threshold: int = KILL_IF_FEWER_THAN) -> float:
    """P(kill | true per-round unique-catch rate p) = binomial P(X < threshold)."""
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(threshold))


def oc_table() -> list[tuple[float, float]]:
    return [(p, p_kill(p)) for p in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25)]


def validate_lens(lens: str) -> str:
    """The lens under trial is a shadow producer: never a placeholder and never a blocking
    reviewer, whose rows are the loop's own verdicts (codex r6 P2: `decide <blocking> --hitl`
    would score ordinary review rows as a trial and enqueue a HITL over them). Parsed ONCE at
    the CLI edge ([LAW:parse-dont-validate]); the reducers take the stamped value."""
    if lens.strip().lower() in PLACEHOLDERS or is_blocking_producer(lens):
        raise ValueError(
            f"shadow-trial lens {lens!r} must be a shadow producer, never a blocking reviewer "
            "or a placeholder"
        )
    return lens


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


def _adjudication_row(
    rows: list[dict], finding_id: str, lens: str, disposition: str, actor: str
) -> dict:
    orig = next(
        (r for r in rows if r["finding_id"] == finding_id and r["record_kind"] == "finding"), None
    )
    if orig is None:  # [LAW:no-silent-failure] an unknown id is an error, never an empty row
        raise ValueError(f"no finding row for {finding_id}")
    if orig["producer"] != lens:
        raise ValueError(
            f"{finding_id} was produced by {orig['producer']!r}, not the shadow lens {lens!r}; "
            "shadow-trial adjudication writes unique_catch for the lens only"
        )
    uc = (orig["head_sha"], orig["location"], orig["finding_type"]) not in _blocking_keys(rows)
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
    return fr.make_row(core, env)


def adjudicate(
    finding_id: str,
    *,
    disposition: str,
    actor: str,
    lens: str,
    rows: list[dict] | None = None,
    path: Path | None = None,
) -> dict:
    """Append the adjudication row for a shadow-lens finding with `unique_catch` = (a) computed
    against the blocking reviewers' rows for the same head_sha AS THE LOG STANDS UNDER THE LOCK;
    (b), the accepted disposition, is what `unique_catches` then requires. Injected `rows` are
    for pure evaluation (no write); every production call persists — a decision that is not on
    the log does not exist (C-HE-29 Invariants)."""
    validate_adjudicator(actor)
    if rows is not None:
        return _adjudication_row(rows, finding_id, lens, disposition, actor)
    written = fr.append_derived(
        lambda log: _adjudication_row(log, finding_id, lens, disposition, actor), path
    )
    assert written is not None
    return written


def _emit_loop_row(kind: str, lane_id: str, cause: str, detail: str) -> None:
    import reservations as rs  # the loop ledger's one writer; imported at the effect boundary

    rs.emit_loop_row(kind, lane_id, cause, detail)


def hitl_request(decision: dict, lens: str) -> None:
    """C-HE-29 §4: the kill/keep evaluation is delivered as an escalation-kind HITL request
    presenting the sampled rounds and every unique-catch disposition alongside the threshold and
    the three permitted responses; nothing is adopted or killed here."""
    sample = decision.get("sample", [])
    rounds = ", ".join(f"{a}/r{r}" for a, r in sample) or "(none)"

    def why(c: dict) -> str:
        if c["counted"]:
            return "COUNTED"
        if not c["in_sample"]:
            return "not counted: outside the frozen sample"
        if c["disposition"] is None:
            return "not counted: undisposed (adjudication pending)"
        if c["blocked"]:
            return "not counted: a blocking reviewer reported the same key"
        if not c["unique_catch"]:
            return "not counted: adjudicated unique_catch=false"
        return f"not counted: last disposition {c['disposition']}"

    catches = (
        "; ".join(
            f"{c['finding_id']}@{c['arc_id']}/r{c['round_n']}={c['disposition'] or 'undisposed'} "
            f"[{why(c)}]"
            for c in decision.get("catches", [])
        )
        or "(no unique_catch rows)"
    )
    _emit_loop_row(
        "DEFERRED-HIL",
        "shadow_trial",
        "shadow-trial-adjudicate:HITL-recoverable:kill_keep_decision",
        f"SHADOW-{lens} — n={decision['n']} scored={decision['scored']} "
        f"unique={decision['unique']} "
        f"threshold={decision['threshold']} → proposed {decision['decision'].upper()}; "
        f"sample rounds: {rounds}; unique_catch dispositions: {catches}; "
        "respond approve-kill | reject-keep | amend-threshold",
    )


def sample_digest(sample: list[tuple[str, int]]) -> str:
    import hashlib

    return hashlib.sha1("|".join(f"{a}/{r}" for a, r in sample).encode()).hexdigest()[:12]


def delivery_identity(decision: dict) -> str:
    """What makes a decision the SAME proposal: the frozen sample, the rule in force and the
    outcome. An amended threshold (the amend-threshold response) or a changed outcome is a
    new proposal and is delivered again (codex r4 P2)."""
    digest = sample_digest([tuple(x) for x in decision["sample"]])
    parts = [
        f"sample={digest}",
        f"n={decision['n']}",
        f"threshold={decision['threshold']}",
        f"decision={decision['decision']}",
    ]
    return " ".join(parts)


def decision_recorded(rows: list[dict], lens: str, identity: str) -> bool:
    """A proposal is delivered ONCE: its marker row on the log is the memory (rows alone), so
    a later `decide --hitl` re-run does not enqueue the same proposal again (codex r3 P3)."""
    return any(
        r["producer"] == CONFIG_PRODUCER
        and r["finding_type"] == DECISION_TYPE
        and r["location"] == lens
        and identity in r["observed_evidence"]
        for r in rows
    )


def decision_marker(rows: list[dict], lens: str, decision: dict) -> dict:
    core = fr.FindingCore(
        fr.next_finding_id(CONFIG_PRODUCER, DECISION_TYPE, lens, rows),
        lens,
        f"shadow-trial decision delivered: {delivery_identity(decision)} "
        f"unique={decision['unique']}",
        "C-HE-29 §4",
        "info",
        DECISION_TYPE,
        "policy",
        CONFIG_PRODUCER,
    )
    env = fr.Envelope("no_finding", fr.now_iso(), "policy", CONFIG_PRODUCER, None, None, None, None)
    return fr.make_row(core, env)


def deliver_decision(lens: str, path: Path | None = None) -> dict:
    """`decide --hitl`. The decision is computed from the rows AS THEY STAND UNDER THE LOCK
    (a concurrent adjudication, blocking row or config amendment cannot stale the proposal),
    the HITL request is emitted FIRST, and only a successful emission writes the marker — so
    a failed emission leaves no "delivered" memory and the next run retries (at-least-once:
    a crash between emission and marker re-emits, never loses; codex r4 P2 ×3). Returns the
    decision with `delivered`."""
    decided: dict = {}

    def build(log: list[dict]) -> dict | None:
        d = decide(log, lens)
        decided.update(d)
        if d["decision"] == "pending" or decision_recorded(log, lens, delivery_identity(d)):
            return None
        hitl_request(d, lens)  # raises on failure -> nothing appended, retry re-emits
        return decision_marker(log, lens, d)

    written = fr.append_derived(build, path)
    return {**decided, "delivered": written is not None}


def undisposed_findings(rows: list[dict], lens: str) -> list[dict]:
    last = fr.reduce_last_by_finding_id(rows)
    return [
        r
        for r in last.values()
        if r["producer"] == lens and r["record_kind"] == "finding" and r["disposition"] is None
    ]


def _request_row(f: dict, lens: str) -> tuple[dict, fr.Envelope]:
    core = dict(
        location=f["finding_id"],
        observed_evidence=f"shadow-trial adjudication requested for {lens}",
        expected_contract="C-HE-29 §4",
        severity="info",
        finding_type=REQUEST_TYPE,
        lineage_claim="policy",
        producer=CONFIG_PRODUCER,
    )
    env = fr.Envelope("no_finding", fr.now_iso(), "policy", CONFIG_PRODUCER, None, None, None, None)
    return core, env


def request_adjudications(lens: str, path: Path | None = None) -> list[dict]:
    """C-HE-29 §4: every shadow finding is handed to the operator as a `shadow-trial-adjudicate`
    HITL row, once — the request marker on the log is the memory (rows alone), so repeated
    score runs never re-enqueue a finding (codex r3 P2). Each finding is its own critical
    section: its HITL row is emitted FIRST and its marker appended before the NEXT finding is
    attempted, so a failed emission is retried next run and a finding already delivered is
    never re-emitted because a later one failed (codex r4 P2, r5 P3). Returns the findings
    requested."""
    requested: list[dict] = []

    def build_next(rows: list[dict]) -> list[tuple[dict, fr.Envelope]]:
        already = {
            r["location"]
            for r in rows
            if r["producer"] == CONFIG_PRODUCER and r["finding_type"] == REQUEST_TYPE
        }
        pending = [f for f in undisposed_findings(rows, lens) if f["finding_id"] not in already]
        if not pending:
            return []
        f = pending[0]
        _emit_loop_row(  # raises -> THIS marker unwritten; earlier ones already persisted
            "DEFERRED-HIL",
            "shadow_trial",
            "shadow-trial-adjudicate:HITL-recoverable:per_finding_disposition",
            f"SHADOW-{lens} finding {f['finding_id']} at {f['location']} ({f['severity']}) "
            f"awaits an adjudicator of neither family: just shadow-trial-adjudicate "
            f"{f['finding_id']} accepted|rejected|suppressed <actor>",
        )
        requested.append(f)
        return [_request_row(f, lens)]

    # [LAW:no-ambient-temporal-coupling] the marker's persistence is owned by the same lock
    # that emitted it; the next finding is only attempted once the previous marker is on disk.
    while fr.append_observations(build_next, path):
        pass
    return requested


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("decide", help="kill / keep / pending from the gate log (read-only)")
    s.add_argument("--lens", required=True)
    s.add_argument("--hitl", action="store_true", help="deliver a non-pending decision as HITL")
    sub.add_parser("oc", help="print the operating-characteristics table")
    c = sub.add_parser("config", help="append the rule as a config row")
    c.add_argument("--lens", required=True)
    c.add_argument("--n", type=int, default=N_ROUNDS)
    c.add_argument("--threshold", type=int, default=KILL_IF_FEWER_THAN)
    c.add_argument(
        "--if-absent", action="store_true", help="append only when the lens has no config row yet"
    )
    ad = sub.add_parser("adjudicate", help="dispose one shadow finding (writes unique_catch)")
    ad.add_argument("finding_id")
    ad.add_argument("--disposition", choices=("accepted", "rejected", "suppressed"), required=True)
    ad.add_argument("--actor", required=True)
    ad.add_argument("--lens", default="gemini-shadow")
    rq = sub.add_parser(
        "request-adjudications", help="hand every undisposed shadow finding to HITL, once"
    )
    rq.add_argument("--lens", required=True)
    a = p.parse_args(argv)
    if a.cmd == "oc":
        for pv, pk in oc_table():
            print(f"p={pv:.2f}  P(kill)={pk:.3f}")
        return 0
    lens = validate_lens(a.lens)  # before any read or write: a refused lens touches nothing
    if a.cmd == "request-adjudications":
        for f in request_adjudications(lens):
            print(f["finding_id"])
        return 0
    if a.cmd == "adjudicate":
        row = adjudicate(a.finding_id, disposition=a.disposition, actor=a.actor, lens=lens)
        print(json.dumps(row))
        return 0
    if a.cmd == "config":

        def build(rows: list[dict]) -> dict | None:
            if a.if_absent and any(
                r["producer"] == CONFIG_PRODUCER
                and r["finding_type"] == CONFIG_TYPE
                and r["location"] == lens
                for r in rows
            ):
                return None
            return config_row(lens=lens, rows=rows, n=a.n, threshold=a.threshold)

        written = fr.append_derived(build)
        print("config row appended" if written else "config row already present")
        return 0
    d = deliver_decision(lens) if a.hitl else decide(fr.read_rows(), lens)
    print(json.dumps(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
