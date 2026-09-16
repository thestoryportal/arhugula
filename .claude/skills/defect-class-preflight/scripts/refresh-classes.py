#!/usr/bin/env python3
"""Re-cluster the live gate log against the skill's defect classes.

The checklist in SKILL.md is a distilled map of `.harness/merge-gate-log.jsonl`;
the log is the one authority and only grows. This prints (a) current per-class
counts — compare them to the counts baked into SKILL.md and refresh the text when
they have moved meaningfully — and (b) the most recent findings that match NO
known class: those are candidates for a new class, surfaced mechanically instead
of waiting for recall. The report is advisory by design: it always exits 0 and its
output is for the agent running the preflight, never a gate.

The `classify` verb is the one consumer-facing seam: `tools/review_loop_gate.py`
feeds it an arc's outstanding reviewer findings at sweep template/attest time, and a
finding it leaves unmatched owes the author an `intake:` disposition there (SKILL.md
"When a reviewer catches what this sweep missed"). That verb exits non-zero on
malformed input — the gate must never read 'could not classify' as 'unmatched'.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# One definition per class, mirroring SKILL.md's class sections. When a class is
# added or reworded there, extend this table in the same commit (the counts the
# skill cites are derived HERE, so a drifted table silently mis-buckets, and a
# class absent here can never leave the unmatched bucket — it reads as a
# new-class candidate forever). Classes 11–14 were added by U-SR-01; 11 had been
# carried in SKILL.md since U-HE-47 without a row here.
#
# A row is EITHER one pattern — any alternative IS the class — OR a TUPLE of patterns
# that must ALL match, for the two classes that name two conditions in their own title.
# The conjunction was first written as `(?=.*A)(?=.*B)` and that was a product type
# squeezed into a string: with `re.search` the unanchored lookaheads retry both
# whole-suffix scans at every character, costing 12.3s and 17.4s on the committed corpus
# against under 0.11s for every other row, and degrading as this append-only log grows
# (codex r5 P2, reproduced before absorbing). Two independent searches are linear and say
# what they mean.
#
# UNDER-matching is the safe direction and is chosen deliberately throughout: a missed
# row stays in the unmatched new-class discovery pile, while a false match silently
# removes it from that pile. Prefer to miss.
#
# Known precision bound, named rather than chased: a conjunction still matches on
# CO-OCCURRENCE, so a row pairing an unrelated command term with an unrelated loop term
# can land in class 13, and a finding whose text DISCUSSES a class necessarily contains
# that class's vocabulary. This tool is advisory by its own docstring and multi-match is
# by design; successive regex layers were measured trading one imprecision for another
# without converging, so the bound is documented here rather than chased further.
CLASSES: dict[str, str | tuple[str, ...]] = {
    "1 race / TOCTOU / atomicity / lock": (
        r"race|TOCTOU|atomic|lock|flock|concurrent|interleav|CAS|exclusive"
    ),
    "2 prose stale / counts / cites": (
        r"stale|close_out|mis-cite|cite|count|narrat|docstring claim|partition"
    ),
    "3 silent failure / fallback": (
        r"swallow|silent|fallback|2>/dev/null|\|\| true|exit code|ignored error"
    ),
    "4 vacuous witness": (
        r"witness|vacuous|stays green|cannot fail|only .*presence|never red"
        r"|does not red|remains green|unexercised"
    ),
    "5 timeout / retry / budget": r"timeout|retry|budget|backoff|deadline",
    "6 unreachable / dead branch": (
        r"unreachable|dead|never reach|no witness could|half-dead|cannot see|restore arm"
    ),
    "7 env-var mutation / restore": (r"monkeypatch|os\.environ|env var|setenv|restore|undo\(\)"),
    "8 subprocess boundary": (r"subprocess|child process|inherit|process boundary|spawns|nested"),
    "9 path / default resolution": (
        r"fallback ledger|venue|QUEUE_DIR|path default|resolves|home default|\$HOME"
    ),
    "10 fixture scope / lifecycle": (
        r"session-scoped|module-scoped|function-scoped|teardown|collection|autouse|fixture"
    ),
    # Bare `adjudicat` alone pulled 48 rows in that merely MENTION adjudication, and
    # `exemption` was equally context-free (codex r8 P2). Every alternative left names a
    # command/permission surface outright. 158 -> 120.
    "11 authority-bearing command surface": (
        r"permission.guard|auto-allow|allowlist|guard venue|exact.shape|carrier parity"
        r"|gate override"
    ),
    # Both u-he-35 P1s must land here — the skill claims both were this shape, and a
    # classifier that says otherwise makes the claim false. `as the verdict` and
    # `schema-parsed BLOCK` are where the exit-code-as-verdict row matches; `is
    # unenforced` is where the pilot-gate row does. Overlap with class 3 is by design.
    # Every alternative carries BOTH halves of the class — a quoted obligation AND its
    # absence — on its own, so each is a PHRASE, not a token. Dropped once measured:
    # `manifest row` / `copied verbatim` (bare nouns), `unenforced` and `schema-parsed`
    # (too loose; narrowed to the phrases the P1s actually use), `spec phrase` and
    # `contract phrase` (name the obligation but not its absence, so "the contract phrase
    # names the wrong component" landed here), and `undischarged`, which matched zero rows
    # in the entire corpus — a dead alternative is not caution, it is noise with no upside.
    "12 quoted contract phrase not discharged": (
        r"is unenforced|declared but|no code discharges|as the verdict"
        r"|schema-parsed BLOCK"
    ),
    # Conjunctions (measured before absorbing): a flat OR mis-bucketed 33 of 64 class-13
    # rows and 6 of 10 class-14 rows. The command conjunct has been narrowed to terms that
    # mean a NEW command: `justfile` went because the matcher sees `location` too and that
    # token pulled in every finding merely LOCATED there; `just recipe` went because it
    # names any recipe, which let a lane-init logging finding in on co-occurrence with the
    # word "loop"; `allow branch` went because it matched zero rows. Each removal was
    # checked to keep justfile:777, the canonical member of this class.
    "13 new command the loop must reach": (
        r"runs_in|new recipe|new command",
        r"loop|headless|guard|auto-allow|permission|ask prompt",
    ),
    "14 signal handler meets lock": (
        r"signal handler|SIGTERM|SIGINT|SIGHUP|async-signal|self-pipe",
        # word-bounded: a bare `lock` also matched `block`/`blocking`, so a Ctrl-C row
        # that "can block in ThreadPoolExecutor shutdown" landed here with no lock in
        # sight (codex r6 P2). `\block\b` excludes both for free — neither has a word
        # boundary before "lock" — so no extra guard is needed. `RLock` gets its own
        # alternative because there is no boundary inside it either.
        r"\block\b|\blocks\b|RLock|mutex|reentran|acquire",
    ),
    # Added by the preflight-finding-intake arc through its own intake line (the first
    # finding to flow in mechanically): the review-loop gate classified the working
    # tree while the attestation bound base..HEAD; the 2026-08-19 row is the same
    # defect on the reviewer wrapper. Conjunction: a tree-state term AND a binding term
    # — either alone is co-occurrence noise (`committed` is in half the corpus).
    "15 bound bytes are not the executed bytes": (
        r"working tree|live worktree|uncommitted|mutable tree|dirty",
        r"binding|base_sha|head_sha|\bHEAD\b|committed (diff|range|bytes|base)",
    ),
}


def matches(pattern: str | tuple[str, ...], text: str) -> bool:
    """True when `text` satisfies a class row: one pattern, or all of a tuple."""
    patterns = (pattern,) if isinstance(pattern, str) else pattern
    return all(re.search(p, text, re.I) for p in patterns)


def classify(text: str) -> list[str]:
    """Every class name whose row `text` satisfies, in table order; empty = unmatched.
    [LAW:effects-at-boundaries] pure over the text — the report and the review gate's
    intake both derive from this one function, so 'unmatched' means the same thing in
    the corpus report and at a sweep attestation ([LAW:one-source-of-truth])."""
    return [name for name, pat in CLASSES.items() if matches(pat, text)]


def finding_text(row: dict) -> str:
    """The text a finding row is classified on: evidence plus location (the location
    carries file-shaped vocabulary such as `justfile` or `test_` that some rows rely on)."""
    return (row.get("observed_evidence") or "") + " " + (row.get("location") or "")


def classify_stdin() -> int:
    """`classify` verb: JSON list of {"finding_id", "observed_evidence", "location"} on
    stdin → JSON object finding_id → [class names] on stdout. The review-loop gate
    calls this at template and attest time so an unmatched reviewer finding is named
    as one at the moment its author must disposition it (the skill's repair loop,
    made mechanical). Malformed input is a loud non-zero exit, never an empty map
    that would read as 'every finding unmatched' ([LAW:no-silent-failure])."""
    try:
        rows = json.load(sys.stdin)
        if not isinstance(rows, list):
            raise TypeError(f"expected a JSON list of findings, got {type(rows).__name__}")
        out = {str(r["finding_id"]): classify(finding_text(r)) for r in rows}
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        print(f"refresh-classes classify: malformed input — {exc}", file=sys.stderr)
        return 2
    json.dump(out, sys.stdout)
    return 0


def main() -> int:
    if sys.argv[1:] == ["classify"]:
        return classify_stdin()
    if sys.argv[1:]:
        print("usage: refresh-classes.py [classify]  (no verb: corpus report)", file=sys.stderr)
        return 2
    log = Path(__file__).resolve().parents[3].parent / ".harness" / "merge-gate-log.jsonl"
    if not log.exists():
        # Fallback: resolve from the repo root when the skill is invoked from cwd.
        log = Path(".harness/merge-gate-log.jsonl")
    if not log.exists():
        print(f"refresh-classes: gate log not found at {log} — run from the repo root")
        return 0

    rows = []
    malformed = 0
    for lineno, line in enumerate(log.read_text().splitlines(), start=1):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            # A damaged row silently dropped would understate counts while the
            # output still reads authoritative — report it and mark the result
            # incomplete below (no-silent-failure; codex round 2 on this PR).
            malformed += 1
            print(f"refresh-classes: WARNING malformed JSONL at line {lineno} — skipped")
            continue
        # Real findings only, by the log's own discriminator: record_kind. A severity
        # filter is NOT a finding filter — it drops warn-severity findings while
        # admitting hard-severity infrastructure rows, skewing the counts this file
        # claims to derive (codex round 1 on the skills PR measured the mismatch).
        if r.get("observed_evidence") and r.get("record_kind", "finding") == "finding":
            rows.append(r)

    counts = dict.fromkeys(CLASSES, 0)
    unmatched = []
    for r in rows:
        hits = classify(finding_text(r))
        for name in hits:
            counts[name] += 1
        if not hits:
            unmatched.append(r)

    status = f" (INCOMPLETE — {malformed} malformed row(s) skipped)" if malformed else ""
    print(f"refresh-classes: {len(rows)} findings in {log}{status}")
    print("\nPer-class counts (multi-match rows count in every class they touch):")
    for name, n in counts.items():
        print(f"  {n:5}  {name}")

    print(f"\nUnmatched findings (new-class candidates): {len(unmatched)}")
    for r in unmatched[-10:]:
        ev = (r.get("observed_evidence") or "").replace("\n", " ")[:150]
        print(f"  {r.get('ts', '')[:10]} {r.get('severity', '?'):3} {r.get('location', '')[:50]}")
        print(f"      {ev}")
    if unmatched:
        print(
            "\nEach unmatched finding is a candidate: either extend an existing class's"
            " pattern (here AND in SKILL.md) or add a new class in both places."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
