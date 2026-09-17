#!/usr/bin/env python3
"""C-HE-24 common finding record and its `Finding` projection.

Every finding-class row (reviewer verdicts, deterministic checks, adjudications,
shadow-trial markers, equivalence proofs, gate demotions) carries the ratified
8-field core plus the row envelope and is appended to
``.harness/merge-gate-log.jsonl`` (C-HE-23 §2). ``.harness/arc-metrics.jsonl``
carries only ``record_kind=arc`` rows and never uses this module's emitter.

Adjudication is append-only (C-HE-24 §5): a later disposition is a new
``finding_adjudication`` row with the same ``finding_id``; readers reduce to
the last row. The write-time checks below are the enforcement of the "reviewer
is never authoritative for disposition" rule -- not prose.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat as stat_module
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
from codex_context_guard import Finding

REPO = Path(__file__).resolve().parent.parent
#: The gate log (C-HE-23 §2). `HARNESS_GATE_LOG` redirects it for a whole process tree -- the
#: seam the reviewer-wrapper subprocess tests use so a fixture run never writes the tracked log.
GATE_LOG_JSONL = Path(
    os.environ.get("HARNESS_GATE_LOG") or REPO / ".harness" / "merge-gate-log.jsonl"
)
SCHEMA_PATH = REPO / "tools" / "review_schemas" / "finding_record.schema.json"
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())

RECORD_KINDS = tuple(SCHEMA["properties"]["record_kind"]["enum"])
DISPOSITIONS = tuple(SCHEMA["properties"]["disposition"]["enum"])
SEVERITIES = tuple(SCHEMA["properties"]["severity"]["enum"])

#: C-HE-24 §4 ``finding_id`` shape: ``<producer>:<head_sha>:<location-hash>:<n>``.
_FINDING_ID_RE = re.compile(
    r"^(?P<producer>[^:]+):(?P<head>[^:]+):(?P<loc>[0-9a-f]{12}):(?P<n>[0-9]+)$"
)
#: Bounded wait for the log's exclusive lock (local read + one write; never a network call).
LOCK_TIMEOUT_S = 30.0

#: C-HE-24 §3 projection: fail-class (carried in ``finding_type``) -> guard severity.
_HARD_PREFIXES = ("terminal-", "permanent-fail-exit")
_WARN_PREFIXES = ("transient-retry", "Reflexion-", "HITL-recoverable")


class RecordError(ValueError):
    """A row that must not be written. Never swallowed."""


@dataclass(frozen=True)
class FindingCore:
    finding_id: str
    location: str
    observed_evidence: str
    expected_contract: str
    severity: str
    finding_type: str
    lineage_claim: str
    producer: str


@dataclass(frozen=True)
class Envelope:
    record_kind: str
    ts: str
    arc_id: str
    lane_id: str
    head_sha: str | None
    base_sha: str | None
    diff_digest: str | None
    round_n: int | None
    cause_attribution: str | None = None
    disposition: str | None = None
    disposition_actor: str | None = None
    unique_catch: bool | None = None


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def location_hash(location: str) -> str:
    return hashlib.sha1(location.encode()).hexdigest()[:12]


def make_finding_id(producer: str, head_sha: str, location: str, n: int) -> str:
    """``<producer>:<head_sha>:<location-hash>:<n>`` (C-HE-24 §4). Not stable across head_sha."""
    return f"{producer}:{head_sha}:{location_hash(location)}:{n}"


def next_finding_id(producer: str, head: str, location: str, rows: list[dict]) -> str:
    """Mint the id of a NEW observation: the smallest `n` not yet used for this
    (producer, head, location-hash) prefix among `rows`. Emitters MUST use this (or
    `append_observation`, which calls it under the log lock) rather than a per-invocation list
    ordinal: a rerun on the same head that re-reports a location at the same ordinal would reuse
    an id and be rejected as a core mutation (Codex round-10 P1). `head` is the id's head
    component -- the row's `head_sha`, or the emitter's null-head token (e.g. `nohead`)."""
    prefix = f"{producer}:{head}:{location_hash(location)}:"
    used: set[int] = set()
    for r in rows:
        fid = r["finding_id"]
        if not fid.startswith(prefix):
            continue
        tail = fid[len(prefix) :]
        if not tail.isdigit():  # a row that never passed validate() (legacy / external append)
            raise RecordError(f"finding_id {fid!r} has a non-integer <n> component (C-HE-24 §4)")
        used.add(int(tail))
    return f"{prefix}{max(used, default=0) + 1}"


def _check_finding_id_components(row: dict) -> None:
    """The id is the ONLY join key reducers and adjudications use, so its components must agree
    with the row they key (Codex round-2 P2): shape per C-HE-24 §4, `producer` component ==
    row producer, location-hash component == sha1(location)[:12], head component == row
    head_sha when the row carries one (a null head_sha row is not held to that component)."""
    m = _FINDING_ID_RE.match(row["finding_id"])
    if m is None:
        raise RecordError(
            f"finding_id {row['finding_id']!r} is not <producer>:<head_sha>:<12-hex>:<n> "
            "(C-HE-24 §4)"
        )
    if m["producer"] != row["producer"]:
        raise RecordError(
            f"finding_id producer component {m['producer']!r} != row producer {row['producer']!r}"
        )
    if m["loc"] != location_hash(row["location"]):
        raise RecordError(
            f"finding_id location-hash {m['loc']!r} != sha1({row['location']!r})[:12]"
        )
    if row["head_sha"] is not None and m["head"] != row["head_sha"]:
        raise RecordError(
            f"finding_id head component {m['head']!r} != row head_sha {row['head_sha']!r}"
        )


def make_row(core: FindingCore, env: Envelope) -> dict:
    return {**asdict(core), **asdict(env)}


def validate(row: dict) -> None:
    try:
        jsonschema.validate(row, SCHEMA)
    except jsonschema.ValidationError as exc:
        raise RecordError(f"finding record schema: {exc.message}") from exc
    _check_finding_id_components(row)
    try:
        # A shape-matching but impossible ts (e.g. `9999-99-99T99:99:99Z`) would pass the schema
        # regex and then, compared lexicographically, out-rank every real adjudication ts
        # (Codex round-8 P2). Validated as a real UTC calendar value; the canonical fixed-width
        # form then orders identically as text and as datetime.
        datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise RecordError(f"ts {row['ts']!r} is not a real UTC timestamp: {exc}") from exc
    if row["record_kind"] == "finding":
        # K5 / C-HE-24 §1: a `finding` must be grounded -- location, observed evidence and
        # expected contract are REQUIRED content, not merely present keys (Codex round-8 P2). A
        # wrapper normalizing missing reviewer output to "" must not persist an unactionable
        # finding. Marker kinds (`no_finding`, `reviewer_unavailable`, ...) may carry blanks.
        for key in ("location", "observed_evidence", "expected_contract"):
            if not row[key].strip():
                raise RecordError(f"finding rows require a non-empty {key!r} (C-HE-24 §1)")
    # `:`-free identifiers (producer / lane_id / disposition_actor, C-HE-24 §2) are enforced by
    # the schema's `pattern` alone -- one enforcement point, no duplicate charset loop here.
    if row["record_kind"] == "finding_adjudication":
        if row["disposition"] is None or row["disposition_actor"] is None:
            raise RecordError("finding_adjudication rows require disposition and disposition_actor")
    elif row["record_kind"] == "equivalence_proof":
        # C-HE-32 §1: the proof is BY a decorrelated party and the row IS the proof, so it carries
        # `accepted` inline with the prover as disposition_actor (U-HE-41's helper); the
        # self-disposition ban below still applies (the beneficiary may not prove for itself).
        if row["disposition"] != "accepted" or row["disposition_actor"] is None:
            raise RecordError(
                "equivalence_proof rows carry disposition='accepted' and the decorrelated "
                "prover as disposition_actor (C-HE-32 §1)"
            )
    elif row["disposition"] is not None or row["disposition_actor"] is not None:
        # C-HE-24 §2/§5: disposition is set ONLY by an append-only adjudication row. A `finding`
        # row that arrives pre-disposed would let the reviewer dispose its own finding without
        # any adjudication event (Codex round-1 P1).
        raise RecordError(
            f"{row['record_kind']!r} rows must carry null disposition/disposition_actor "
            "(only finding_adjudication rows dispose, C-HE-24 §5)"
        )
    if row["disposition_actor"] is not None and row["disposition_actor"] == row["producer"]:
        raise RecordError(
            f"disposition_actor {row['disposition_actor']!r} equals producer -- "
            "a reviewer never disposes its own finding (C-HE-24 §5)"
        )


# Everything but ts / record_kind / disposition / disposition_actor / unique_catch (C-HE-24
# Invariants, spec-literal). `round_n` IS immutable: a `finding_id` names ONE observation, and a
# re-observation in a later round at the same head_sha is a NEW observation minted by
# `next_finding_id` (Codex rounds 9-10; the round-9 mutable-`round_n` reading contradicted the
# cleared invariant and was reverted in round 10).
_CORE_IMMUTABLE = (
    "location",
    "observed_evidence",
    "expected_contract",
    "severity",
    "finding_type",
    "lineage_claim",
    "producer",
    "arc_id",
    "lane_id",
    "head_sha",
    "base_sha",
    "diff_digest",
    "round_n",
    "cause_attribution",
)


def _check_against_prior_rows(row: dict, rows: list[dict]) -> None:
    """C-HE-24 §5 invariants at WRITE time, for EVERY row kind: two rows with one finding_id
    differ only by ts / record_kind / disposition / disposition_actor / unique_catch; the only
    kind transition is `finding` -> finding_adjudication (markers and proofs are never
    adjudicated); an adjudication's ts is strictly later; nothing but further adjudications
    follows an adjudication. A repeated
    `finding` row (an emitter retry minting the same id) is held to the same core as the first
    row, not only adjudications (Codex round-1 P1). An adjudication cannot smuggle a new
    producer in to evade the self-disposition ban: the id binds `producer`, so
    `_check_finding_id_components` rejects the swap upstream (and `producer` is also listed
    core-immutable here); the ban itself has exactly one write-time enforcement point --
    ``validate()``."""
    prior = [r for r in rows if r["finding_id"] == row["finding_id"]]
    if not prior:
        if row["record_kind"] == "finding_adjudication":
            raise RecordError(f"adjudication for unknown finding_id {row['finding_id']!r}")
        return
    if row["record_kind"] != "finding_adjudication" and any(
        r["record_kind"] == "finding_adjudication" for r in prior
    ):
        # Once adjudicated, only further adjudications may follow: a same-core retry appended
        # after the decision would become the reducer's last row with a null disposition and
        # silently erase it (Codex round-4 P2; C-HE-24 §5, C-HE-29 "last disposition").
        raise RecordError(
            f"finding_id {row['finding_id']!r} is already adjudicated; a {row['record_kind']!r} "
            "row may not follow a finding_adjudication row"
        )
    orig = prior[0]
    if row["record_kind"] == "finding_adjudication" and orig["record_kind"] != "finding":
        # C-HE-24 §5 adjudicates FINDING rows. An "accepted" marker (`no_finding`,
        # `reviewer_unavailable`, `gate_demotion`) or proof would otherwise be reduced as a
        # prevented problem by N6 (Codex round-10 P2).
        raise RecordError(
            f"only finding rows may be adjudicated; {row['finding_id']!r} is a "
            f"{orig['record_kind']!r} row (C-HE-24 §5)"
        )
    if row["record_kind"] not in ("finding_adjudication", orig["record_kind"]):
        # A retry may repeat the ORIGINAL kind; the only other kind allowed on an existing id is
        # an adjudication. `finding` -> `no_finding` on one id would let the producer erase its
        # own finding through the reducer with no disposition_actor (Codex round-5 P1).
        raise RecordError(
            f"finding_id {row['finding_id']!r} was recorded as {orig['record_kind']!r}; a "
            f"{row['record_kind']!r} row may not follow it (only a same-kind retry or a "
            "finding_adjudication may)"
        )
    for k in _CORE_IMMUTABLE:
        if row[k] != orig[k]:
            raise RecordError(
                f"adjudication may not change core field {k!r} ({orig[k]!r} -> {row[k]!r})"
            )
    if row["record_kind"] == "finding_adjudication":
        latest_ts = max(r["ts"] for r in prior)
        if row["ts"] <= latest_ts:
            # C-HE-24 §5: an adjudication carries "a later ts". `now_iso()` is second-granular,
            # so a same-second adjudication is reachable and is rejected here rather than
            # recorded as a non-monotonic lineage (Codex round-5 P2). Reduction stays file-order.
            raise RecordError(
                f"adjudication ts {row['ts']!r} must be later than the finding's latest row ts "
                f"{latest_ts!r} (C-HE-24 §5)"
            )


def _lock_exclusive(fd: int, path: Path, timeout_s: float = LOCK_TIMEOUT_S) -> None:
    """Bounded `flock(LOCK_EX)` on the log's own fd -- the house pattern (`mutation_probe.py`,
    `tools/hooks/lib.sh`). Admissible here because the log is a REPO-resident record, not
    QUEUE_DIR coordination state: C-HE-02 §1's CAS-only rule and its `flock|fcntl` invariant are
    scoped to `arc_metrics.py` / `merge_door.py` / `reservations.py`. Kernel-released on process
    death, so there is no stale-lock reclaim path; the critical section is a local read + one
    write (C-HE-02 §3 satisfied). Times out loudly rather than waiting forever."""
    deadline = time.monotonic() + timeout_s
    delay = 0.005
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RecordError(f"could not lock {path} within {timeout_s}s") from None
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _under_log_lock(path: Path, body) -> None:
    """Open the log O_APPEND, hold its exclusive lock across `body(fd)`, release, close.

    The open itself is the containment enforcement point (merge-gate concurrency lens on
    PR #1454): the log is a suppression authority for the U-HE-33 detections, so a
    symlink or FIFO swapped in between any caller's pre-check and this open must fail at
    THIS syscall -- O_NOFOLLOW refuses the symlink (ELOOP) and O_NONBLOCK prevents an
    open()-on-FIFO from hanging; the post-open fstat then refuses any non-regular file
    the flags let through. A stat-then-open pre-check alone leaves a swap window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags, 0o644)
    if not stat_module.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RecordError(f"gate log {path} is not a regular file -- refused (containment)")
    try:
        _lock_exclusive(fd, path)
        try:
            body(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _encode(row: dict) -> bytes:
    return (json.dumps(row, sort_keys=True) + "\n").encode()


def append_row(row: dict, path: Path | None = None) -> None:
    """Validate, then -- under the log's exclusive lock -- run the same-core invariant for
    repeated finding_ids and append one line with a single write. The check and the append are
    ONE critical section: two writers minting the same finding_id (C-HE-22's concurrent reviewer
    invocations) must not both pass the check and then both append conflicting cores (Codex
    round-2 P1). `path` defaults to GATE_LOG_JSONL resolved AT CALL TIME (not bound at def time)
    so tests may monkeypatch it and production writes never leak into a test's tree (Codex
    round-5 P1)."""
    path = path or GATE_LOG_JSONL
    validate(row)
    data = _encode(row)

    def body(fd: int) -> None:
        _check_against_prior_rows(row, _read_rows_fd(fd, path))
        _append_line(fd, data, path)

    _under_log_lock(path, body)


def append_observation(core_fields: dict, env: Envelope, path: Path | None = None) -> dict:
    """Append a NEW observation, minting its `finding_id` under the same lock the append holds
    (allocate + validate + append = one critical section, so two concurrent emitters cannot mint
    one id). `core_fields` = the FindingCore fields except `finding_id`; the id's head component
    is `env.head_sha`, or the null-head token `nohead` when the row carries no head. Returns the
    row as written."""
    path = path or GATE_LOG_JSONL
    head = env.head_sha if env.head_sha is not None else "nohead"
    written: dict = {}

    def body(fd: int) -> None:
        rows = _read_rows_fd(fd, path)
        fid = next_finding_id(core_fields["producer"], head, core_fields["location"], rows)
        row = make_row(FindingCore(finding_id=fid, **core_fields), env)
        validate(row)
        _check_against_prior_rows(row, rows)  # a fresh id has no prior; kept for the invariant
        _append_line(fd, _encode(row), path)
        written.update(row)

    _under_log_lock(path, body)
    return written


def append_observations(
    build: Callable[[list[dict]], list[tuple[dict, Envelope]]], path: Path | None = None
) -> list[dict]:
    """Append SEVERAL new observations under ONE critical section. `build(rows)` receives the
    log as it stands under the lock and returns `(core_fields, env)` pairs (each like
    `append_observation`'s arguments) -- so anything the caller derives from the current log
    (a next round number, a count) and the appends it conditions are one atomic step (two
    wrappers on one arc cannot both mint the same round; C-HE-22 / S1 codex round 7). Ids are
    minted per pair against the rows already present PLUS the pairs appended before it."""
    path = path or GATE_LOG_JSONL
    written: list[dict] = []

    def body(fd: int) -> None:
        rows = _read_rows_fd(fd, path)
        for core_fields, env in build(rows):
            head = env.head_sha if env.head_sha is not None else "nohead"
            fid = next_finding_id(core_fields["producer"], head, core_fields["location"], rows)
            row = make_row(FindingCore(finding_id=fid, **core_fields), env)
            validate(row)
            _check_against_prior_rows(row, rows)
            _append_line(fd, _encode(row), path)
            rows.append(row)
            written.append(row)

    _under_log_lock(path, body)
    return written


def append_derived(
    build: Callable[[list[dict]], dict | None], path: Path | None = None
) -> dict | None:
    """Append ONE row that a caller derives from the log as it stands under the lock, keeping its
    `finding_id` (an adjudication of an existing finding, not a new observation). `build(rows)`
    returns the complete row, or None to append nothing. The derivation and the append are one
    critical section, so a fact the row asserts about the log (e.g. "no blocking reviewer has
    reported this key") cannot be invalidated by a row landing between the read and the write
    (U-HE-43 codex round 1). Returns the row as written, or None."""
    path = path or GATE_LOG_JSONL
    written: dict | None = None

    def body(fd: int) -> None:
        nonlocal written
        rows = _read_rows_fd(fd, path)
        row = build(rows)
        if row is None:
            return
        validate(row)
        _check_against_prior_rows(row, rows)
        _append_line(fd, _encode(row), path)
        written = row

    _under_log_lock(path, body)
    return written


def _append_line(fd: int, data: bytes, path: Path) -> None:
    """ONE `os.write` syscall on the O_APPEND fd -- not a buffered TextIO write that may split a
    long line across syscalls. C-HE-23 §2's "single write under PIPE_BUF" is not literally
    reachable for a C-HE-24 row (SHAs + digest + evidence exceed macOS's 512-byte PIPE_BUF, and
    PIPE_BUF governs pipes, not regular files); the operative guarantees here are: writers are
    serialized by the lock, the line goes down in one syscall, and a SHORT write (disk full)
    is rolled back to the pre-write offset before raising, so a torn tail never poisons the log
    (Codex round-6 P2). Spec v1.1 change-note X1 (C-HE-23 §2) records this correction."""
    end = os.lseek(fd, 0, os.SEEK_END)
    n = os.write(fd, data)
    if n != len(data):
        os.ftruncate(fd, end)  # roll the partial line back; the log stays parseable
        raise RecordError(f"short write to {path}: {n} of {len(data)} bytes (rolled back)")


def _read_rows_fd(fd: int, path: Path) -> list[dict]:
    """Read the log through the LOCKED fd (os.pread; the append offset is untouched).
    Reading by pathname inside the critical section reopens the file, so a concurrent
    rename+symlink swap could feed the dedupe/recall forged rows while appends still
    target the original inode (codex r15 P1 on PR #1454) -- the fd is the one identity
    the lock actually protects."""
    size = os.fstat(fd).st_size
    chunks = []
    off = 0
    while off < size:
        b = os.pread(fd, min(1 << 20, size - off), off)
        if not b:
            break
        chunks.append(b)
        off += len(b)
    rows: list[dict] = []
    for n, line in enumerate(b"".join(chunks).decode().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecordError(f"{path}:{n} is not valid JSON: {exc}") from exc
    return rows


def read_rows(path: Path | None = None) -> list[dict]:
    path = path or GATE_LOG_JSONL  # call-time resolution (see append_row)
    if not path.exists():
        return []
    rows: list[dict] = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecordError(f"{path}:{n} is not valid JSON: {exc}") from exc
    return rows


def reduce_last_by_finding_id(rows: list[dict]) -> dict[str, dict]:
    """Last row per finding_id in FILE (append) order -- the append-only log is the ordering
    authority. `ts` is emitter-supplied and is never used to reorder: a clock regression or a
    back-filled row must not let an earlier physical row outrank the latest appended
    adjudication (Codex round-1 P2; C-HE-24 §5 "readers reduce by finding_id -> last row")."""
    out: dict[str, dict] = {}
    for row in rows:
        out[row["finding_id"]] = row
    return out


def to_guard_finding(row: dict) -> Finding:
    """C-HE-24 §3 projection for the CI surface. Existing guard codes are untouched."""
    ft = row["finding_type"]
    if ft.startswith(_HARD_PREFIXES):
        severity = "hard"
    elif ft.startswith(_WARN_PREFIXES):
        severity = "warn"
    else:
        severity = "info"
    code = f"{row['producer']}:{ft}:{row['cause_attribution'] or '-'}"
    return Finding(severity, code, row["observed_evidence"])


def main(argv: list[str] | None = None) -> int:
    """`python tools/finding_record.py validate <jsonl>` -- schema-check every row."""
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2 or args[0] != "validate":
        print("usage: finding_record.py validate <path.jsonl>", file=sys.stderr)
        return 2
    bad = 0
    for n, row in enumerate(read_rows(Path(args[1])), start=1):
        try:
            validate(row)
        except RecordError as exc:
            bad += 1
            print(f"row {n}: {exc}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
