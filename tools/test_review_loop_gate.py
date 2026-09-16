"""B-215 review-loop admission gate battery.

Covers the pure decision core (no mocks — [LAW:effects-at-boundaries]), the
attest CLI edges in throwaway git repos, and the codex_review.main() wiring
(gate fires BEFORE any reviewer subprocess; refusal is exit 3 and is NOT a
review terminal per C-HE-16 §3 — no C-HE-24 row, no round outcome).
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import finding_record as fr
import review_loop_gate as rlg
import review_wrapper_common as rw

ARC = "b-000-test"
HEAD = "a" * 40
BASE = "b" * 40
DIGEST = "c" * 64
LOOP_PRODUCER = "codex_review_wrapper"


def _row(round_n: int, kind: str, finding_id: str | None = None, **over) -> dict:
    base = dict(
        arc_id=ARC,
        producer=LOOP_PRODUCER,
        round_n=round_n,
        record_kind=kind,
        finding_id=finding_id,
    )
    base.update(over)
    return base


def _pf(head=HEAD, digest=DIGEST, arc=ARC) -> rlg.PreflightAttestation:
    return rlg.PreflightAttestation(
        arc_id=arc, head_sha=head, diff_digest=digest, hit_labels=(), answers_digest="d", ts="t"
    )


def _sw(ids: tuple[str, ...], head=HEAD, digest=DIGEST, arc=ARC) -> rlg.SweepAttestation:
    return rlg.SweepAttestation(
        arc_id=arc,
        head_sha=head,
        diff_digest=digest,
        finding_ids=ids,
        answers_digest="d",
        ts="t",
    )


def _state(preflights=(), sweeps=(), extensions=()) -> rlg.GateState:
    return rlg.GateState(preflights=preflights, sweeps=sweeps, extensions=extensions)


def _decide(state, rows, **over):
    kw = dict(arc_id=ARC, head_sha=HEAD, diff_digest=DIGEST)
    kw.update(over)
    return rlg.decide(state, rows, **kw)


# ── pure core: entry gate ────────────────────────────────────────────────────


def test_round0_without_preflight_refuses():
    d = _decide(_state(), [])
    assert isinstance(d, rlg.Refused)
    assert d.code == "PREFLIGHT_MISSING"
    # loop-reachable recipe: names the attest recipe and the attest-after-commit discipline
    assert "review-attest-preflight" in d.recipe
    assert "commit" in d.recipe.lower()


def test_round0_with_bound_preflight_allows():
    d = _decide(_state(preflights=(_pf(),)), [])
    assert d == rlg.Allowed(round_n=1)


def test_round0_preflight_wrong_head_refuses_stale():
    d = _decide(_state(preflights=(_pf(head="e" * 40),)), [])
    assert isinstance(d, rlg.Refused)
    assert d.code == "PREFLIGHT_STALE"


def test_round0_preflight_wrong_digest_refuses_stale():
    d = _decide(_state(preflights=(_pf(digest="f" * 64),)), [])
    assert isinstance(d, rlg.Refused)
    assert d.code == "PREFLIGHT_STALE"


# ── pure core: post-round sweep gate ─────────────────────────────────────────


def test_block_round_without_sweep_refuses_and_enumerates_ids():
    rows = [_row(1, "finding", "cw:aa:11:1"), _row(1, "finding", "cw:aa:11:2")]
    d = _decide(_state(preflights=(_pf(),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "SWEEP_MISSING"
    # the refusal must hand the agent the exact unanswered ids — no log scraping
    assert "cw:aa:11:1" in d.detail and "cw:aa:11:2" in d.detail
    assert "review-attest-sweep" in d.recipe


def test_block_round_with_covering_sweep_allows():
    rows = [_row(1, "finding", "cw:aa:11:1")]
    d = _decide(_state(sweeps=(_sw(("cw:aa:11:1",)),)), rows)
    assert d == rlg.Allowed(round_n=2)


def test_sweep_missing_one_finding_names_it():
    rows = [_row(1, "finding", "cw:aa:11:1"), _row(1, "finding", "cw:aa:11:2")]
    d = _decide(_state(sweeps=(_sw(("cw:aa:11:1",)),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "SWEEP_MISSING"
    assert "cw:aa:11:2" in d.detail
    assert "cw:aa:11:1" not in d.detail  # answered ids are not re-demanded


def test_covering_sweep_at_old_head_fails_currency():
    # obligations answered, but the CURRENT bytes carry no attestation
    rows = [_row(1, "finding", "cw:aa:11:1")]
    d = _decide(_state(sweeps=(_sw(("cw:aa:11:1",), head="e" * 40),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "PREFLIGHT_STALE"


def test_covering_sweep_different_base_fails_currency():
    # codex r1 P2: head alone under-binds — a different --base is a different diff
    rows = [_row(1, "finding", "cw:aa:11:1")]
    d = _decide(_state(sweeps=(_sw(("cw:aa:11:1",), digest="f" * 64),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "PREFLIGHT_STALE"


def test_standalone_gemini_interleave_still_blocks():
    # codex r2 P1: round numbers are per-producer scales — a gemini finding minted
    # at gemini-round-1 while codex sits at round 5 is still an unanswered
    # obligation; round arithmetic must not hide it
    rows = [
        *[_row(n, "no_finding") for n in range(1, 6)],
        _row(1, "finding", "gw:aa:11:9", producer="gemini_review_wrapper"),
    ]
    d = _decide(_state(preflights=(_pf(),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "SWEEP_MISSING"
    assert "gw:aa:11:9" in d.detail


def test_approve_round_plus_current_attestation_allows():
    rows = [_row(1, "no_finding")]
    d = _decide(_state(preflights=(_pf(),)), rows)
    assert d == rlg.Allowed(round_n=2)


def test_unavailable_round_does_not_satisfy_currency():
    # codex r2 P1: an UNAVAILABLE round reviewed nothing — with no attestation for
    # the current bytes the next invocation must not bypass the entry preflight
    rows = [_row(1, "reviewer_unavailable")]
    assert isinstance(_decide(_state(), rows), rlg.Refused)
    d = _decide(_state(preflights=(_pf(),)), rows)
    assert d == rlg.Allowed(round_n=2)


def test_failover_gemini_findings_same_round_must_be_swept():
    # a failover round's findings land under the GEMINI producer at the forced
    # shared round number — they are loop findings and must be answered
    rows = [
        _row(1, "reviewer_unavailable"),
        _row(1, "finding", "gw:aa:11:1", producer="gemini_review_wrapper"),
    ]
    d = _decide(_state(), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "SWEEP_MISSING"
    assert "gw:aa:11:1" in d.detail


# ── pure core: termination predicate ─────────────────────────────────────────


def test_budget_counts_distinct_producer_round_pairs():
    # codex r2 P1: rounds spent = review invocations across BOTH producer scales —
    # 6 codex + 4 standalone gemini = 10 spent, even though max(round_n) is only 6
    rows = [
        *[_row(n, "no_finding") for n in range(1, 7)],
        *[_row(n, "no_finding", producer="gemini_review_wrapper") for n in range(1, 5)],
    ]
    d = _decide(_state(preflights=(_pf(),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "BUDGET_EXHAUSTED"
    # the unblock verb attest-budget is ask-gated in loop mode: the recipe must
    # point at the register-and-hold path, never ping-pong against a denied verb
    assert "defer" in d.recipe or "register" in d.recipe
    assert "attest-budget" not in d.recipe.split("operator")[0]


def test_budget_extension_extends():
    rows = [_row(n, "no_finding") for n in range(1, 11)]
    ext = rlg.BudgetExtension(arc_id=ARC, extra_rounds=2, reason="operator ok", ts="t")
    d = _decide(_state(preflights=(_pf(),), extensions=(ext,)), rows)
    assert d == rlg.Allowed(round_n=11)


def test_foreign_rows_do_not_count():
    # other arcs, lens producers (their per-producer round numbers collide), and
    # round_n=None detection rows (codex_context_guard) are all out of scope
    rows = [
        _row(9, "finding", "x:1", arc_id="other-arc"),
        _row(9, "finding", "x:2", producer="merge-gate-concurrency"),
        _row(None, "finding", "x:3", producer="codex_context_guard"),
    ]
    d = _decide(_state(preflights=(_pf(),)), rows)
    assert d == rlg.Allowed(round_n=1)


def test_foreign_arc_attestations_never_satisfy(monkeypatch: pytest.MonkeyPatch):
    # merge-gate witness lens P2: the three arc_id-scoping guards each need a
    # discriminating witness — one arc's preflight, sweep, or budget extension
    # must never satisfy/extend a DIFFERENT arc against the shared state file
    foreign = "b-999-other"
    # (1) currency: a foreign-arc preflight bound to the same head/digest does not admit
    d = _decide(_state(preflights=(_pf(arc=foreign),)), [])
    assert isinstance(d, rlg.Refused)
    assert d.code == "PREFLIGHT_MISSING"
    # (2) obligations: a foreign-arc sweep naming the id does not answer it
    rows = [_row(1, "finding", "cw:aa:11:1")]
    d = _decide(_state(sweeps=(_sw(("cw:aa:11:1",), arc=foreign),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "SWEEP_MISSING"
    # (3) termination: a foreign-arc extension does not raise this arc's budget
    rows = [_row(n, "no_finding") for n in range(1, 11)]
    ext = rlg.BudgetExtension(arc_id=foreign, extra_rounds=5, reason="other arc", ts="t")
    d = _decide(_state(preflights=(_pf(),), extensions=(ext,)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "BUDGET_EXHAUSTED"


def test_state_loss_only_tightens():
    # the tightening-direction invariant: attestations/extensions live in state,
    # round counts live in the gate log — losing state can refuse what was
    # allowed, never allow what was refused
    rows = [_row(1, "finding", "cw:aa:11:1")]
    with_state = _decide(_state(sweeps=(_sw(("cw:aa:11:1",)),)), rows)
    without_state = _decide(_state(), rows)
    assert isinstance(with_state, rlg.Allowed)
    assert isinstance(without_state, rlg.Refused)


# ── edges: state parse + admit ───────────────────────────────────────────────


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "f.txt").write_text("one\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "c1",
        ],
        check=True,
    )
    (tmp_path / ".harness").mkdir()
    # every finding matches SOME class unless a test plants otherwise: the intake
    # obligation is exercised by its own tests, not by every sweep test
    _plant_classifier(tmp_path, unmatched=())
    monkeypatch.setenv("HARNESS_GATE_LOG", str(tmp_path / "gate-log.jsonl"))
    monkeypatch.setattr(fr, "GATE_LOG_JSONL", tmp_path / "gate-log.jsonl")
    monkeypatch.delenv("HARNESS_LOOP", raising=False)
    return tmp_path


def test_load_state_missing_file_is_empty(repo: Path):
    assert rlg.load_state(repo) == _state()


def test_admit_state_unreadable_refuses(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: True)
    rlg.state_path(repo).write_text("{not json")
    d = rlg.admit(repo, "main", ARC)
    assert isinstance(d, rlg.Refused)
    assert d.code == "STATE_UNREADABLE"


def test_admit_out_of_scope_arc_ignores_unreadable_state(repo: Path):
    # Inactive means NOT IN FORCE: an unreadable state file must not refuse an
    # unreserved interactive invocation (live schema migrations would otherwise
    # brick every wrapper call in the checkout)
    rlg.state_path(repo).write_text("{not json")
    d = rlg.admit(repo, "main", ARC)
    assert isinstance(d, rlg.Inactive)


def test_admit_unreserved_is_inactive_in_every_mode(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r3 P1: the headless-degradation path (reserve REFUSED by the permission
    # layer) is sanctioned and proceeds unreserved — a loop-mode hard refusal would
    # strand it with an unsatisfiable recipe. Fallback-id visibility on every row is
    # the accepted trade (named residual).
    d = rlg.admit(repo, "main", ARC)
    assert isinstance(d, rlg.Inactive)
    assert "unreserved" in d.reason
    monkeypatch.setenv("HARNESS_LOOP", "1")
    assert isinstance(rlg.admit(repo, "main", ARC), rlg.Inactive)
    (repo / ".harness" / ".loop-active").write_text("1")
    assert isinstance(rlg.admit(repo, "main", ARC), rlg.Inactive)


# ── edges: attest CLI ────────────────────────────────────────────────────────

SCRIPT_REL = Path(".claude/skills/defect-class-preflight/scripts/preflight-grep.sh")


def _plant_script(repo: Path, body: str) -> None:
    p = repo / SCRIPT_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


CLASSIFIER_REL = Path(".claude/skills/defect-class-preflight/scripts/refresh-classes.py")
REAL_CLASSIFIER = Path(__file__).resolve().parent.parent / CLASSIFIER_REL


def _commit(repo: Path, msg: str) -> None:
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", msg], check=True)


def _plant_classifier(repo: Path, unmatched: tuple[str, ...], body: str | None = None) -> None:
    """A stand-in for the skill's `refresh-classes.py classify` verb honouring its
    contract (JSON list in, {finding_id: [class names]} out): the named ids are
    unmatched, every other id matches one stub class. COMMITTED — the gate reads the
    classifier at HEAD, the same binding the attestation records."""
    p = repo / CLASSIFIER_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        body
        if body is not None
        else (
            "import json, sys\n"
            f"UNMATCHED = {set(unmatched)!r}\n"
            "rows = json.load(sys.stdin)\n"
            "json.dump({r['finding_id']: ([] if r['finding_id'] in UNMATCHED else ['stub class'])"
            " for r in rows}, sys.stdout)\n"
        )
    )
    _commit(repo, "plant classifier")


def _plant_real_classifier(repo: Path) -> Path:
    """The skill's actual script, copied so a test may extend its CLASSES table the
    way an absorption commit does — the one-finding-flows-in witness runs on the
    real classifier, not the stub."""
    p = repo / CLASSIFIER_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(REAL_CLASSIFIER.read_text())
    _commit(repo, "plant real classifier")
    return p


def _attest_preflight(repo: Path, answers: str | None = "answers: all clean\n") -> int:
    a = repo / "answers.md"
    if answers is not None:
        a.write_text(answers)
    return rlg.main(
        ["attest-preflight", "--answers", str(a), "--base", "main", "--repo", str(repo)]
    )


def test_attest_preflight_script_cannot_run_refuses(repo: Path):
    _plant_script(repo, "#!/bin/sh\necho 'SWEEP DID NOT RUN' >&2\nexit 2\n")
    rc = _attest_preflight(repo)
    assert rc != 0
    assert rlg.load_state(repo).preflights == ()


def test_attest_preflight_unanswered_label_refuses(repo: Path):
    _plant_script(repo, "#!/bin/sh\nprintf '\\n[check-then-act on paths]\\n3:+x\\n'\nexit 0\n")
    rc = _attest_preflight(repo, answers="something unrelated\n")
    assert rc != 0
    assert rlg.load_state(repo).preflights == ()


def test_attest_preflight_records_bound_attestation(repo: Path, monkeypatch: pytest.MonkeyPatch):
    _plant_script(repo, "#!/bin/sh\nprintf '\\n[check-then-act on paths]\\n3:+x\\n'\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    rc = _attest_preflight(repo, answers="check-then-act on paths: guarded by exclusive create\n")
    assert rc == 0
    (pf,) = rlg.load_state(repo).preflights
    binding = rw.code_binding(repo, "main")
    assert pf.head_sha == binding["head_sha"]
    assert pf.diff_digest == binding["diff_digest"]
    assert pf.arc_id == ARC
    assert pf.hit_labels == ("check-then-act on paths",)


def _commit_all(repo: Path, msg: str) -> None:
    for verb in (["add", "."], ["commit", "-qm", msg]):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *verb],
            check=True,
        )


def test_attest_preflight_real_script_integration(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # the REAL preflight-grep.sh in RANGE mode against a committed silent-failure
    # shape on an arc branch: proves label parsing matches the script's actual
    # output AND that the attested range (base..head) is what gets swept — the
    # working tree is clean here, exactly the post-commit state attest requires
    real = Path(__file__).resolve().parents[1] / SCRIPT_REL
    _plant_script(repo, real.read_text())
    _commit_all(repo, "c2")  # script itself on main, outside the arc range
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "arc"], check=True)
    (repo / "new.sh").write_text("run 2>/dev/null\n")
    _commit_all(repo, "arc work")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    rc = _attest_preflight(repo, answers="silent-failure shapes: named answer here\n")
    assert rc == 0
    (pf,) = rlg.load_state(repo).preflights
    assert "silent-failure shapes" in pf.hit_labels


# ── edges: template verbs (U-SR-04, charter WR-10 — labels before answers) ───


def _template(repo: Path, cmd: str, answers: str = ".harness/tmp/answers.md") -> int:
    return rlg.main([cmd, "--answers", str(repo / answers), "--base", "main", "--repo", str(repo)])


def _attest_at(repo: Path, answers: str = ".harness/tmp/answers.md") -> int:
    # attest the file where the template wrote it (the _attest_preflight helper
    # above reads a root-level fixture path the namespace no longer admits)
    return rlg.main(
        [
            "attest-preflight",
            "--answers",
            str(repo / answers),
            "--base",
            "main",
            "--repo",
            str(repo),
        ]
    )


def test_template_preflight_prefills_labels_then_attests_first_trial(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # the WR-10 acceptance witness ([B] F14: 3 attest-by-trial failures → 0): the
    # template carries every hit label BEFORE any answer is authored; the unedited
    # template refuses attestation (labels alone are not answers); the filled
    # template attests on the first trial
    _plant_script(repo, "#!/bin/sh\nprintf '\\n[check-then-act on paths]\\n3:+x\\n'\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    assert _template(repo, "template-preflight") == 0
    text = (repo / ".harness/tmp/answers.md").read_text()
    assert "[check-then-act on paths]" in text
    assert rlg.TEMPLATE_PLACEHOLDER in text
    rc = _attest_at(repo)  # attest the UNEDITED template as-is
    assert rc != 0
    assert rlg.load_state(repo).preflights == ()
    (repo / ".harness/tmp/answers.md").write_text(
        text.replace(rlg.TEMPLATE_PLACEHOLDER, "guarded by exclusive create at f.py:3")
    )
    assert _attest_at(repo) == 0
    (pf,) = rlg.load_state(repo).preflights
    assert pf.hit_labels == ("check-then-act on paths",)


def test_template_no_hits_still_requires_a_filled_answer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # symmetry pin: a hitless sweep must not yield a template that attests unedited —
    # the author still owes the diff-level answer set
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    assert _template(repo, "template-preflight") == 0
    assert rlg.TEMPLATE_PLACEHOLDER in (repo / ".harness/tmp/answers.md").read_text()
    assert _attest_at(repo) != 0
    assert rlg.load_state(repo).preflights == ()


def test_template_refuses_existing_destination(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # templates never overwrite: hand-authored answers at the destination survive
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    (repo / ".harness/tmp").mkdir(exist_ok=True)
    (repo / ".harness/tmp/answers.md").write_text("hand-authored\n")
    assert _template(repo, "template-preflight") != 0
    assert (repo / ".harness/tmp/answers.md").read_text() == "hand-authored\n"


def test_template_symlink_destination_refused(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # a pre-planted symlink at the destination must refuse loudly, never be written
    # through (exclusive create: any survivor at the name is EEXIST)
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    victim = repo / "victim.md"
    victim.write_text("precious\n")
    (repo / ".harness/tmp").mkdir(exist_ok=True)
    (repo / ".harness/tmp/answers.md").symlink_to(victim)
    assert _template(repo, "template-preflight") != 0
    assert victim.read_text() == "precious\n"


def test_template_destination_outside_worktree_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
):
    # the template verbs are guard-auto-allowed, so the one file OUTPUT gets the same
    # containment discipline as the attest verbs' input — no write escapes the repo
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    outside = tmp_path_factory.mktemp("outside") / "answers.md"
    rc = rlg.main(
        ["template-preflight", "--answers", str(outside), "--base", "main", "--repo", str(repo)]
    )
    assert rc != 0
    assert not outside.exists()


def test_template_sweep_prefills_outstanding_ids_and_labels(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    _plant_script(repo, "#!/bin/sh\nprintf '\\n[silent-failure shapes]\\n1:+x\\n'\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(
        json.dumps(_row(1, "finding", "cw:aa:11:1"))
        + "\n"
        + json.dumps(_row(1, "finding", "cw:aa:11:2"))
        + "\n"
    )
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 0
    text = (repo / ".harness/tmp/sweep.md").read_text()
    assert "cw:aa:11:1" in text and "cw:aa:11:2" in text
    assert "[silent-failure shapes]" in text
    filled = text.replace(rlg.TEMPLATE_PLACEHOLDER, "fixed at f.py:1")
    (repo / ".harness/tmp/sweep.md").write_text(filled)
    rc = rlg.main(
        [
            "attest-sweep",
            "--answers",
            str(repo / ".harness/tmp/sweep.md"),
            "--base",
            "main",
            "--repo",
            str(repo),
        ]
    )
    assert rc == 0
    (sw,) = rlg.load_state(repo).sweeps
    assert set(sw.finding_ids) == {"cw:aa:11:1", "cw:aa:11:2"}


def test_template_sweep_without_outstanding_findings_declines(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # mirror of attest-sweep's guard: no obligations → nothing to template (rc 1),
    # and no file appears
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 1
    assert not (repo / ".harness/tmp/sweep.md").exists()


def _attest_sweep_rc(repo: Path, answers_rel: str) -> int:
    return rlg.main(
        [
            "attest-sweep",
            "--answers",
            str(repo / answers_rel),
            "--base",
            "main",
            "--repo",
            str(repo),
        ]
    )


def _two_findings(repo: Path, monkeypatch: pytest.MonkeyPatch, **evidence: str) -> None:
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(
        json.dumps(_row(1, "finding", "cw:aa:11:1", observed_evidence=evidence.get("one", "")))
        + "\n"
        + json.dumps(_row(1, "finding", "cw:aa:11:2", observed_evidence=evidence.get("two", "")))
        + "\n"
    )


def test_template_sweep_stamps_classes_and_unmatched_finding_owes_intake(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # the skill's repair loop, mechanical: the template NAMES the finding no class
    # covers and pre-fills the intake slot; attest records the instance-only escape
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, unmatched=("cw:aa:11:2",))
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 0
    text = (repo / ".harness/tmp/sweep.md").read_text()
    assert "# classes: stub class\n- finding cw:aa:11:1" in text
    assert "# classes: UNMATCHED" in text
    assert text.count(f"{rlg.INTAKE_PREFIX} {rlg.TEMPLATE_PLACEHOLDER}") == 1
    filled = text.replace(
        f"{rlg.INTAKE_PREFIX} {rlg.TEMPLATE_PLACEHOLDER}",
        f"{rlg.INTAKE_PREFIX} instance-only a one-off wording nit in a fixture",
    ).replace(rlg.TEMPLATE_PLACEHOLDER, "fixed at f.py:1")
    (repo / ".harness/tmp/sweep.md").write_text(filled)
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 0
    (sw,) = rlg.load_state(repo).sweeps
    assert set(sw.finding_ids) == {"cw:aa:11:1", "cw:aa:11:2"}
    assert sw.intake_instance_only == ("cw:aa:11:2",)


def test_attest_sweep_unmatched_finding_without_intake_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, unmatched=("cw:aa:11:2",))
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 0
    lines = (repo / ".harness/tmp/sweep.md").read_text().splitlines()
    kept = [ln for ln in lines if not ln.lstrip().startswith(rlg.INTAKE_PREFIX)]
    (repo / ".harness/tmp/sweep.md").write_text(
        "\n".join(kept).replace(rlg.TEMPLATE_PLACEHOLDER, "fixed at f.py:1") + "\n"
    )
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    err = capsys.readouterr().err
    assert "cw:aa:11:2" in err and rlg.INTAKE_PREFIX in err and "0 `intake:` lines" in err
    assert rlg.load_state(repo).sweeps == ()


def test_attest_sweep_intake_line_is_not_the_findings_fix_answer(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # hand-authored shape: an intake disposition under a bare finding heading must
    # not read as the finding's answer — the fix and the class disposition are two
    # obligations
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, unmatched=("cw:aa:11:2",))
    (repo / ".harness/tmp").mkdir()
    (repo / ".harness/tmp/sweep.md").write_text(
        "- finding cw:aa:11:1 fixed at f.py:1\n"
        "- finding cw:aa:11:2\n"
        "  intake: instance-only a one-off\n"
    )
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    assert "unanswered: cw:aa:11:2" in capsys.readouterr().err


def test_attest_sweep_repair_claim_the_live_table_does_not_show_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # a claim is verified against the classifier, never trusted: 'class-extended'
    # while the finding still matches nothing is the un-landed repair
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, unmatched=("cw:aa:11:2",))
    (repo / ".harness/tmp").mkdir()
    (repo / ".harness/tmp/sweep.md").write_text(
        "- finding cw:aa:11:1 fixed at f.py:1\n"
        "- finding cw:aa:11:2 fixed at f.py:2\n"
        "  intake: class-extended 3 silent failure now names exit-code-as-verdict\n"
    )
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    err = capsys.readouterr().err
    assert "class repair the live table does not show" in err and "cw:aa:11:2" in err
    assert rlg.load_state(repo).sweeps == ()


@pytest.mark.parametrize(
    "line",
    [
        "  intake: sometimes-maybe a made-up kind",
        "  intake: instance-only",
        "  intake: new-class 15 zorb\n  intake: instance-only twice",
    ],
)
def test_attest_sweep_malformed_intake_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], line: str
):
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, unmatched=("cw:aa:11:2",))
    (repo / ".harness/tmp").mkdir()
    (repo / ".harness/tmp/sweep.md").write_text(
        f"- finding cw:aa:11:1 fixed at f.py:1\n- finding cw:aa:11:2 fixed at f.py:2\n{line}\n"
    )
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    assert "cw:aa:11:2" in capsys.readouterr().err


def test_intake_belongs_to_its_heading_not_to_a_cross_reference(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # codex r1 P1: BOTH findings unmatched; finding 1's answer cites finding 2's id
    # and carries an intake line. Finding 2 owes its OWN intake — it must not inherit
    # 1's through the cross-reference
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, unmatched=("cw:aa:11:1", "cw:aa:11:2"))
    (repo / ".harness/tmp").mkdir()
    (repo / ".harness/tmp/sweep.md").write_text(
        "- finding cw:aa:11:1 fixed at f.py:1, same root as cw:aa:11:2\n"
        "  intake: instance-only a one-off\n"
        "- finding cw:aa:11:2 fixed at f.py:2\n"
    )
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    assert "finding cw:aa:11:2 matches no preflight class" in capsys.readouterr().err
    (repo / ".harness/tmp/sweep.md").write_text(
        "- finding cw:aa:11:1 fixed at f.py:1, same root as cw:aa:11:2\n"
        "  intake: instance-only a one-off\n"
        "- finding cw:aa:11:2 fixed at f.py:2\n"
        "  intake: instance-only its twin\n"
    )
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 0
    (sw,) = rlg.load_state(repo).sweeps
    assert set(sw.intake_instance_only) == {"cw:aa:11:1", "cw:aa:11:2"}


def test_intake_line_above_any_heading_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, unmatched=("cw:aa:11:2",))
    (repo / ".harness/tmp").mkdir()
    (repo / ".harness/tmp/sweep.md").write_text(
        "intake: instance-only orphaned\n"
        "- finding cw:aa:11:1 fixed at f.py:1\n"
        "- finding cw:aa:11:2 fixed at f.py:2\n"
    )
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    assert "under no finding heading" in capsys.readouterr().err


def test_one_finding_flows_into_the_class_table_end_to_end(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # the circuit the charter called broken ('nothing flows in'), on the REAL
    # classifier: a reviewer finding with vocabulary no class carries is named
    # UNMATCHED at template time; the absorption commit extends the skill's table;
    # attest then sees the finding matched and owes no intake. A repair claim made
    # BEFORE the row lands is refused by the same check.
    _two_findings(
        repo,
        monkeypatch,
        one="a race between two writers on the lock",
        two="the widget frobnicates the zorb twice per tick",
    )
    script = _plant_real_classifier(repo)
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 0
    text = (repo / ".harness/tmp/sweep.md").read_text()
    assert "# classes: 1 race / TOCTOU / atomicity / lock\n- finding cw:aa:11:1" in text
    assert "# classes: UNMATCHED" in text and text.count(rlg.INTAKE_PREFIX) == 1
    (repo / ".harness/tmp").mkdir(exist_ok=True)
    answers = (
        "- finding cw:aa:11:1 fixed at f.py:1\n"
        "- finding cw:aa:11:2 fixed at f.py:2\n"
        "  intake: new-class 15 zorb frobnication\n"
    )
    (repo / ".harness/tmp/sweep.md").write_text(answers)
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    assert "class repair the live table does not show" in capsys.readouterr().err
    # the absorption commit: one data row in the skill's table. Written but NOT yet
    # committed it changes nothing — the gate classifies with HEAD's bytes, the
    # binding the attestation records (codex r1 P2)
    src = script.read_text()
    marker = "CLASSES: dict[str, str | tuple[str, ...]] = {\n"
    assert marker in src
    script.write_text(src.replace(marker, marker + '    "15 zorb frobnication": r"frobnicat",\n'))
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 1
    assert "class repair the live table does not show" in capsys.readouterr().err
    _commit(repo, "absorb: class 15")
    assert _attest_sweep_rc(repo, ".harness/tmp/sweep.md") == 0
    (sw,) = rlg.load_state(repo).sweeps
    assert sw.intake_instance_only == ()
    assert "0 unmatched" in capsys.readouterr().out


def test_classifier_that_cannot_run_is_loud_not_all_unmatched(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _two_findings(repo, monkeypatch)
    _plant_classifier(repo, (), body="import sys\nsys.exit(3)\n")
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 1
    assert "finding classifier did not run (exit 3)" in capsys.readouterr().err
    assert not (repo / ".harness/tmp/sweep.md").exists()
    for body in (
        "print('{}')",  # an answer missing the asked ids
        "print('[]')",  # not an object
        'print(\'{"cw:aa:11:1": "stub class", "cw:aa:11:2": []}\')',  # a string, not a list
        'print(\'{"cw:aa:11:1": [""], "cw:aa:11:2": [1]}\')',  # empty / non-string names
    ):
        _plant_classifier(repo, (), body=body + "\n")
        assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 1, body
        assert "unusable answer" in capsys.readouterr().err, body


def test_classifier_must_be_committed_at_head(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _two_findings(repo, monkeypatch)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", str(CLASSIFIER_REL)], check=True)
    _commit(repo, "remove classifier")
    (repo / CLASSIFIER_REL).write_text("print('{}')\n")  # present in the tree, not at HEAD
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 1
    assert "is not committed at HEAD" in capsys.readouterr().err


def test_state_written_before_intake_field_still_loads(repo: Path):
    rlg.state_path(repo).write_text(
        json.dumps(
            {
                "records": [
                    {
                        "kind": "sweep",
                        "arc_id": ARC,
                        "head_sha": HEAD,
                        "diff_digest": DIGEST,
                        "finding_ids": ["cw:aa:11:1"],
                        "answers_digest": "d",
                        "ts": "t",
                    }
                ]
            }
        )
    )
    (sw,) = rlg.load_state(repo).sweeps
    assert sw.intake_instance_only == ()


def test_template_destination_outside_answers_namespace_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # codex u-sr-04 r1 P2: the guard auto-allows any in-worktree token, so the TOOL
    # is the enforcer of the answers namespace — a template must never materialize
    # a file in a venue whose edits are ask-gated (design-substrate), nor at the
    # repo root, nor via a `..` escape from a legal prefix
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    for dest in (
        "design-substrate/new.md",
        "answers.md",
        ".harness/answers.md",
        ".harness/review_loop_gate_state.json",
        ".harness/tmp/../answers.md",
    ):
        assert _template(repo, "template-preflight", dest) != 0, dest
        assert not (repo / dest).resolve().exists(), dest


def test_template_symlinked_harness_component_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
):
    # codex u-sr-04 r1 P1: the walk is descriptor-relative with O_NOFOLLOW at every
    # component, so a symlinked directory under .harness cannot route the create
    # outside the worktree (the resolve-then-reopen draft was check-then-act)
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    outside = tmp_path_factory.mktemp("outside-dir")
    (repo / ".harness" / "tmp").mkdir(exist_ok=True)
    (repo / ".harness" / "tmp" / "sub").symlink_to(outside)
    assert _template(repo, "template-preflight", ".harness/tmp/sub/answers.md") != 0
    assert list(outside.iterdir()) == []


def test_template_failed_publish_leaves_no_partial_and_retry_succeeds(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # codex u-sr-04 r1 P2: publication is temp-then-link, so a refused publish
    # (EEXIST) leaves neither a partial final nor a temp that blocks the retry
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    (repo / ".harness/tmp").mkdir(exist_ok=True)
    (repo / ".harness/tmp/answers.md").write_text("hand-authored\n")
    assert _template(repo, "template-preflight") != 0
    assert (repo / ".harness/tmp/answers.md").read_text() == "hand-authored\n"
    assert [p.name for p in (repo / ".harness/tmp").iterdir() if p.name.endswith(".tmp")] == []
    (repo / ".harness/tmp/answers.md").unlink()
    assert _template(repo, "template-preflight") == 0
    assert rlg.TEMPLATE_PLACEHOLDER in (repo / ".harness/tmp/answers.md").read_text()


def test_template_with_placeholders_deleted_does_not_attest(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # codex u-sr-04 r2 P1 (escalating the r1-rejected P2): the template writes every
    # label itself, so token PRESENCE stopped evidencing authorship — deleting the
    # placeholder substring, or its whole bullet line, must still refuse: 'answered'
    # means a content line, not a heading the tool wrote
    _plant_script(repo, "#!/bin/sh\nprintf '\\n[check-then-act on paths]\\n3:+x\\n'\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    assert _template(repo, "template-preflight") == 0
    text = (repo / ".harness/tmp/answers.md").read_text()
    # deletion arm 1: strip the placeholder substring, keep the bare bullet
    (repo / ".harness/tmp/answers.md").write_text(text.replace(rlg.TEMPLATE_PLACEHOLDER, ""))
    assert _attest_at(repo) != 0
    # deletion arm 2: drop every bullet line, keep only headings + header comments
    headings_only = "\n".join(ln for ln in text.splitlines() if not ln.startswith("- "))
    (repo / ".harness/tmp/answers.md").write_text(headings_only)
    assert _attest_at(repo) != 0
    assert rlg.load_state(repo).preflights == ()


def test_sweep_template_with_placeholders_deleted_does_not_attest(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # same r2 P1, id half: the sweep template writes every finding id — the id's own
    # line must carry residue beyond the id and template chrome
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(json.dumps(_row(1, "finding", "cw:aa:11:1")) + "\n")
    assert _template(repo, "template-sweep", ".harness/tmp/sweep.md") == 0
    text = (repo / ".harness/tmp/sweep.md").read_text()
    (repo / ".harness/tmp/sweep.md").write_text(text.replace(rlg.TEMPLATE_PLACEHOLDER, ""))
    rc = rlg.main(
        [
            "attest-sweep",
            "--answers",
            str(repo / ".harness/tmp/sweep.md"),
            "--base",
            "main",
            "--repo",
            str(repo),
        ]
    )
    assert rc != 0
    assert rlg.load_state(repo).sweeps == ()


def test_hitless_template_with_deleted_todo_line_does_not_attest(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # codex u-sr-04 r5 P2: in the hitless arm the placeholder bullet is the ONLY
    # fillable line — deleting it must not leave attestable content (the template's
    # explanatory prose is comment chrome, and attest requires authored residue)
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    assert _template(repo, "template-preflight") == 0
    text = (repo / ".harness/tmp/answers.md").read_text()
    gutted = "\n".join(ln for ln in text.splitlines() if rlg.TEMPLATE_PLACEHOLDER not in ln)
    (repo / ".harness/tmp/answers.md").write_text(gutted)
    assert _attest_at(repo) != 0
    assert rlg.load_state(repo).preflights == ()
    (repo / ".harness/tmp/answers.md").write_text(
        text.replace(rlg.TEMPLATE_PLACEHOLDER, "no hits; classes walked, no new consumer")
    )
    assert _attest_at(repo) == 0


def test_stale_template_binding_stamp_refuses_after_tree_moves(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # codex u-sr-04 r5 P2: the template's binding stamp is CHECKED, not chrome — a
    # template filled against old bytes must not attest a moved tree even when the
    # new label set is unchanged (here: empty both times)
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    assert _template(repo, "template-preflight") == 0
    text = (repo / ".harness/tmp/answers.md").read_text()
    (repo / ".harness/tmp/answers.md").write_text(
        text.replace(rlg.TEMPLATE_PLACEHOLDER, "answered against the OLD bytes")
    )
    (repo / "f.txt").write_text("two\n")
    _commit_all(repo, "tree moves while the template is being filled")
    assert _attest_at(repo) != 0
    assert rlg.load_state(repo).preflights == ()
    # the recovery the refusal names: re-template on a fresh path, fill, attest
    assert _template(repo, "template-preflight", ".harness/tmp/answers-2.md") == 0
    fresh = (repo / ".harness/tmp/answers-2.md").read_text()
    (repo / ".harness/tmp/answers-2.md").write_text(
        fresh.replace(rlg.TEMPLATE_PLACEHOLDER, "answered against the moved bytes")
    )
    assert _attest_at(repo, ".harness/tmp/answers-2.md") == 0


def test_capitalized_finding_heading_is_not_an_answer(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex u-sr-04 r6 P2: 'Finding <id>' (capitalized, no disposition) must not
    # satisfy the id's residue check — the label word strips case-insensitively
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(json.dumps(_row(1, "finding", "cw:aa:11:1")) + "\n")
    (repo / ".harness/tmp").mkdir(exist_ok=True)
    (repo / ".harness/tmp/sweep.md").write_text("Finding cw:aa:11:1\n")
    rc = rlg.main(
        [
            "attest-sweep",
            "--answers",
            str(repo / ".harness/tmp/sweep.md"),
            "--base",
            "main",
            "--repo",
            str(repo),
        ]
    )
    assert rc != 0
    assert rlg.load_state(repo).sweeps == ()
    (repo / ".harness/tmp/sweep.md").write_text("Finding cw:aa:11:1 — fixed at f.py:3\n")
    rc = rlg.main(
        [
            "attest-sweep",
            "--answers",
            str(repo / ".harness/tmp/sweep.md"),
            "--base",
            "main",
            "--repo",
            str(repo),
        ]
    )
    assert rc == 0


def test_obligation_tokens_do_not_answer_each_other(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex u-sr-04 r7 P2: a line that merely lists two ids (or two labels) must
    # not let each token read as the other's answer — residue strips the FULL
    # obligation set, not just the token under check
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(
        json.dumps(_row(1, "finding", "cw:aa:11:1"))
        + "\n"
        + json.dumps(_row(1, "finding", "cw:aa:11:2"))
        + "\n"
    )
    (repo / ".harness").mkdir(exist_ok=True)
    (repo / ".harness/tmp").mkdir(exist_ok=True)
    (repo / ".harness/tmp/sweep.md").write_text("cw:aa:11:1 cw:aa:11:2\n")
    args = [
        "attest-sweep",
        "--answers",
        str(repo / ".harness/tmp/sweep.md"),
        "--base",
        "main",
        "--repo",
        str(repo),
    ]
    assert rlg.main(args) != 0
    assert rlg.load_state(repo).sweeps == ()
    (repo / ".harness/tmp/sweep.md").write_text(
        "cw:aa:11:1 fixed at f.py:3\ncw:aa:11:2 registered as forward row\n"
    )
    assert rlg.main(args) == 0
    assert len(rlg.load_state(repo).sweeps) == 1
    # label half of the same class: two bracketed labels on one line answer neither
    two = ("check-then-act on paths", "silent-failure shapes")
    assert rlg._unanswered_labels(
        two, "[check-then-act on paths] [silent-failure shapes]\n"
    ) == list(two)
    assert (
        rlg._unanswered_labels(
            two, "[check-then-act on paths] [silent-failure shapes] both guarded at f.py:3\n"
        )
        == []
    )


def test_overlapping_id_tokens_do_not_shadow(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex u-sr-04 r8 P1: with ids ...:1 and ...:10, stripping the shorter first
    # left a stray '0' that read as residue on the longer id's bare template line —
    # tokens strip longest-first
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(
        json.dumps(_row(1, "finding", "cw:aa:11:1"))
        + "\n"
        + json.dumps(_row(2, "finding", "cw:aa:11:10"))
        + "\n"
    )
    ids = ("cw:aa:11:1", "cw:aa:11:10")
    assert rlg._unanswered_ids(ids, "cw:aa:11:1 fixed at f.py:3\n- finding cw:aa:11:10 —\n") == [
        "cw:aa:11:10"
    ]
    (repo / ".harness/tmp").mkdir(exist_ok=True)
    (repo / ".harness/tmp/sweep.md").write_text(
        "cw:aa:11:1 fixed at f.py:3\n- finding cw:aa:11:10 —\n"
    )
    args = [
        "attest-sweep",
        "--answers",
        str(repo / ".harness/tmp/sweep.md"),
        "--base",
        "main",
        "--repo",
        str(repo),
    ]
    assert rlg.main(args) != 0
    assert rlg.load_state(repo).sweeps == ()
    (repo / ".harness/tmp/sweep.md").write_text(
        "cw:aa:11:1 fixed at f.py:3\ncw:aa:11:10 registered as forward row\n"
    )
    assert rlg.main(args) == 0


def test_template_swapped_temp_inode_refused(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex u-sr-04 r8 P2 (the round-log publisher's codex r10 precedent): link(2)
    # resolves the temp NAME — a swap between write and link must refuse loudly and
    # leave nothing at the final name
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    real_link = os.link

    def swapping_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if src.endswith(".tmp") and src_dir_fd is not None:
            # hold the original inode open across the swap: Linux recycles a freed
            # inode number immediately, and a recycled number would make the
            # foreign file compare EQUAL and hide the swap (the first CI run of
            # this witness caught exactly that on ext4; APFS never recycles)
            holder = os.open(src, os.O_RDONLY, dir_fd=src_dir_fd)
            try:
                os.unlink(src, dir_fd=src_dir_fd)
                fd = os.open(src, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=src_dir_fd)
                os.write(fd, b"foreign bytes\n")
                os.close(fd)
                return real_link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            finally:
                os.close(holder)
        return real_link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "link", swapping_link)
    assert _template(repo, "template-preflight") != 0
    monkeypatch.setattr(os, "link", real_link)
    assert not (repo / ".harness/tmp/answers.md").exists()
    assert _template(repo, "template-preflight") == 0


def test_multiline_hand_authored_id_answer_still_attests(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # codex u-sr-04 r9 P2: the pre-template hand shape — an id heading with the
    # answer on the NEXT line — stays valid; the section ends at another id's line,
    # so a bare heading before the next finding still refuses
    ids = ("cw:aa:11:1", "cw:aa:11:2")
    multiline = "Finding cw:aa:11:1\n  fixed at f.py:3\n\nFinding cw:aa:11:2\n  registered\n"
    assert rlg._unanswered_ids(ids, multiline) == []
    bare_then_next = "Finding cw:aa:11:1\nFinding cw:aa:11:2\n  registered\n"
    assert rlg._unanswered_ids(ids, bare_then_next) == ["cw:aa:11:1"]


def test_punctuation_only_fill_is_not_an_answer(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex u-sr-04 r9 P2: replacing the placeholder with an empty quoted/backtick
    # value must not read as residue
    _plant_script(repo, "#!/bin/sh\nprintf '\\n[check-then-act on paths]\\n3:+x\\n'\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    assert _template(repo, "template-preflight") == 0
    text = (repo / ".harness/tmp/answers.md").read_text()
    (repo / ".harness/tmp/answers.md").write_text(text.replace(rlg.TEMPLATE_PLACEHOLDER, '""'))
    assert _attest_at(repo, ".harness/tmp/answers.md") != 0
    assert rlg.load_state(repo).preflights == ()


def test_recipe_prefix_falls_back_on_non_grammar_ids():
    # codex u-sr-04 r9 P2: an id outside the guard's bareword-prefix grammar must
    # not render a malformed recovery command — the placeholder form renders instead
    d = _decide(_state(), [], arc_id="bad arc@id", lane_id="lane-7")
    assert isinstance(d, rlg.Refused) and d.code == "PREFLIGHT_MISSING"
    assert "HARNESS_ARC_ID=<arc-id> HARNESS_LANE_ID=<lane-id>" in d.recipe
    assert "bad arc@id" not in d.recipe
    # the lane conjunct witnessed from ITS OWN side (merge-gate witness lens r1:
    # a valid arc with a malformed lane must also fall back — dropping the
    # lane half of the AND must red here, not stay green behind the arc case)
    d = _decide(_state(), [], lane_id="bad lane; rm")
    assert isinstance(d, rlg.Refused) and d.code == "PREFLIGHT_MISSING"
    assert "HARNESS_ARC_ID=<arc-id> HARNESS_LANE_ID=<lane-id>" in d.recipe
    assert "bad lane; rm" not in d.recipe


def test_malformed_binding_stamp_refuses_loudly(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex u-sr-04 r7 P2: an unparseable '# binding:' line must fail loud, never
    # silently demote the file to hand-authored semantics (the deliberate-deletion
    # arm stays out of scope per the module trust boundary — r6 rejection)
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    (repo / ".harness/tmp").mkdir(parents=True, exist_ok=True)
    (repo / ".harness/tmp/answers.md").write_text("# binding: mangled\n- real answer at f.py:3\n")
    assert _attest_at(repo, ".harness/tmp/answers.md") != 0
    assert rlg.load_state(repo).preflights == ()


def test_gate_refusal_recipes_route_through_the_template_verbs():
    # codex u-sr-04 r1 P2 (integration): an agent following the gate's own refusal
    # text must land on the labels-before-answers flow, not on attest-by-trial —
    # each refusal recipe names its template verb BEFORE its attest verb, on EVERY
    # branch including PREFLIGHT_STALE (r4 P2: a moved tree can carry new labels),
    # and every command carries the arc/lane prefix (r4 P2: a bare command binds
    # the branch-* fallback arc, so the recovery text could never clear this one)
    def _prefixed(recipe: str, verb: str) -> bool:
        return f"HARNESS_ARC_ID={ARC} HARNESS_LANE_ID=lane-7 just {verb}" in recipe

    d = _decide(_state(), [], lane_id="lane-7")
    assert isinstance(d, rlg.Refused) and d.code == "PREFLIGHT_MISSING"
    assert d.recipe.index("review-template-preflight") < d.recipe.index("review-attest-preflight")
    assert _prefixed(d.recipe, "review-template-preflight") and _prefixed(
        d.recipe, "review-attest-preflight"
    )
    rows = [_row(1, "finding", "cw:aa:11:1")]
    d = _decide(_state(preflights=(_pf(),)), rows, lane_id="lane-7")
    assert isinstance(d, rlg.Refused) and d.code == "SWEEP_MISSING"
    assert d.recipe.index("review-template-sweep") < d.recipe.index("review-attest-sweep")
    assert _prefixed(d.recipe, "review-template-sweep") and _prefixed(
        d.recipe, "review-attest-sweep"
    )
    d = _decide(_state(preflights=(_pf(head="e" * 40),)), [], lane_id="lane-7")
    assert isinstance(d, rlg.Refused) and d.code == "PREFLIGHT_STALE"
    assert d.recipe.index("review-template-preflight") < d.recipe.index("review-attest-preflight")
    assert _prefixed(d.recipe, "review-template-preflight")
    d = _decide(
        _state(preflights=(_pf(),)),
        [_row(n, "finding", f"cw:aa:11:{n}") for n in range(1, 11)],  # 10 rounds spent
        lane_id="lane-7",
    )
    assert isinstance(d, rlg.Refused) and d.code == "BUDGET_EXHAUSTED"
    assert _prefixed(d.recipe, "review-attest-budget")


def test_preflight_grep_u_sr_01_shapes_each_catch_a_planted_fixture(repo: Path):
    """U-SR-01 (charter WR-03) acceptance: each of the four added shapes catches a
    planted fixture, run through the REAL script — a pattern that matches nothing is
    a class the sweep silently stops covering. The TimeoutExpired case asserts BOTH
    directions: the bare arm is reported and the `(TimeoutExpired, OSError)` sibling
    is NOT, so the exclusion is witnessed as discriminating rather than merely
    present (a label-only assertion passes even if report_unless never filters)."""
    real = Path(__file__).resolve().parents[1] / SCRIPT_REL
    _plant_script(repo, real.read_text(encoding="utf-8"))
    # Commit the script BEFORE planting, so the sweep never reads its own source as
    # an untracked new file (codex r1 P3). `type=int` is a metacharacter-free
    # literal, so an untracked copy of the script self-matches that pattern from its
    # own `report` declaration — the label then fires with the planted fixture
    # deleted, and every assertion below would pass while proving nothing.
    _commit_all(repo, "script on main, outside the swept set")
    (repo / "planted.py").write_text(
        "if proc.returncode in (0, 1):\n"
        "    verdict = 'approve'\n"
        "if proc.returncode != 0:  # ordinary status check, not a verdict\n"
        "    log_it()\n"
        "try:\n"
        "    run()\n"
        "except subprocess.TimeoutExpired:\n"
        "    give_up()\n"
        "try:\n"
        "    run_sibling()\n"
        "except (subprocess.TimeoutExpired, OSError):\n"
        "    handle()\n"
        "try:\n"
        "    run_third()\n"
        "except subprocess.TimeoutExpired:  # OSError still propagates\n"
        "    give_up()\n"
        "try:\n"
        "    run_fourth()\n"
        "except TimeoutExpired:\n"  # unqualified import spelling, same defect
        "    give_up()\n"
        "try:\n"
        "    run_fifth()\n"
        'except subprocess.TimeoutExpired: print("OSError was not caught")\n'
        "try:\n"
        "    run_sixth()\n"
        "except (subprocess.TimeoutExpired, NotOSError):\n"
        "    handle()\n"
        'parser.add_argument("--reps", type=int, default=3)\n'
        # the ordinary MULTILINE argparse call: `add_argument(` and `type=int` land on
        # different lines, so a same-line-scoped pattern misses it (codex r10 P2)
        "        ap.add_argument(\n"
        '            "--budget",\n'
        "            type=int,\n"
    )
    (repo / "planted_guard.sh").write_text(
        "    elif printf '%s' \"$TRIM\" | grep -Eq '^just[[:space:]]+new-recipe$' \\\n"
    )
    out = subprocess.run(
        ["bash", str(repo / SCRIPT_REL)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr

    def block(label: str) -> str:
        assert f"[{label}]" in out.stdout, f"shape did not fire: {label}\n{out.stdout}"
        return out.stdout.split(f"[{label}]")[1].split("\n\n")[0]

    # Each label must carry the PLANTED line, not merely be present: deleting a
    # planted shape has to red its own assertion.
    verdict_block = block(
        "exit code read as verdict (class 12 — name the schema parse that decides)"
    )
    assert "proc.returncode in (0, 1)" in verdict_block
    # an ordinary `!= 0` status check is CORRECT code and must not be flagged: with
    # report()'s eight-hit cap, such false positives can push a real misuse out of the
    # report while the attestation still shows the label answered (codex r6 P2)
    assert "returncode != 0" not in verdict_block
    argparse_block = block("argparse count without a contract-derived bound")
    assert "--reps" in argparse_block
    # the multiline form must be caught too: scoping the pattern to the same line as
    # `add_argument(` silently dropped every ordinary multiline call (codex r10 P2)
    assert "type=int," in argparse_block
    assert "new-recipe" in block("new permission-guard allow branch (name its witness)")
    # Five planted arms, one per way the exclusion can be right or wrong. Only OSError
    # actually inside the except CLAUSE means "handled"; everything else is reported.
    timeout_block = block("TimeoutExpired without OSError (crash aliases as timeout)")
    assert "give_up()" not in timeout_block  # bodies are not swept, only the arms
    for reported in (
        "except subprocess.TimeoutExpired:\n",  # bare arm
        "# OSError still propagates",  # named only in a trailing comment
        "except TimeoutExpired:",  # unqualified import spelling
        'print("OSError was not caught")',  # named only after the clause's colon
        "NotOSError",  # a different identifier that merely CONTAINS the token
    ):
        assert reported in timeout_block, f"arm not reported: {reported}\n{timeout_block}"
    # the one genuinely handled arm — OSError inside the caught tuple — is excluded
    assert "(subprocess.TimeoutExpired, OSError)" not in timeout_block


CLASSES_REL = Path(".claude/skills/defect-class-preflight/scripts/refresh-classes.py")


def _preflight_module():
    import importlib.util

    src = Path(__file__).resolve().parents[1] / CLASSES_REL
    spec = importlib.util.spec_from_file_location("_refresh_classes", src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Evidence quoted from the real gate rows these classes were added FOR, pinned here so
# the assertion cannot drift with the live log (codex r3 P3: nothing exercised
# refresh-classes.py, so deleting every new CLASSES row stayed green).
_CLS_12 = "12 quoted contract phrase not discharged"
_CLASS_WITNESSES = [
    # u-he-35 r1 P1 — reviewer_concurrency_probe.py:96 (exit-code-as-verdict)
    (
        _CLS_12,
        "The probe treats every exit 1 as a schema-parsed BLOCK, but both"
        " wrappers can also exit 1 from an uncaught exception",
    ),
    # u-he-35 r10 P1 — lanes_verify.py:309 (declared-but-unenforced gate)
    (_CLS_12, "The claimed pilot gate is unenforced. This entry is a `live:` row"),
    (
        "13 new command the loop must reach",
        "the new justfile recipe's runs_in includes loop but the guard wiring is absent",
    ),
    (
        "14 signal handler meets lock",
        "the SIGTERM signal handler takes the RLock the main thread already owns",
    ),
    (
        "11 authority-bearing command surface",
        "the permission guard auto-allow admits every argument shape for this verb",
    ),
]


@pytest.mark.parametrize(("cls", "evidence"), _CLASS_WITNESSES)
def test_refresh_classes_rows_match_the_findings_they_were_added_for(cls: str, evidence: str):
    """Each class added by U-SR-01 must bucket its OWN motivating shape.

    A classifier row that misses the finding it was written for silently mis-buckets
    and leaves those rows in the unmatched 'new-class candidate' pile forever — the
    exact defect codex r2 caught on class 12, here made deletion-sensitive.
    """
    mod = _preflight_module()
    classes = mod.CLASSES
    assert cls in classes, f"class row removed: {cls}"
    assert mod.matches(classes[cls], evidence), f"{cls} does not match its own finding"


# Classes 13 and 14 are CONJUNCTIONS. Each near miss below satisfies ONE half and must
# still be refused: a flat OR of the same terms passes every one of them, which is the
# state codex r3/r4 measured (class 13 mis-bucketed 33 of 64 rows, class 14 six of ten).
_NEAR_MISSES = [
    # class 13: command half only — names a NEW command but no loop reachability and no
    # guard, so dropping the reach conjunct would let it through. This evidence is
    # checked against the command conjunct by the invariant below: an earlier version
    # cited a `justfile:` recipe and satisfied NEITHER conjunct once r9 narrowed the
    # command terms to `runs_in|new recipe|new command`, which made this case dead and
    # left the reach-conjunct-drop mutation untested (merge-gate witness lens, round 1).
    (
        "13 new command the loop must reach",
        "the new recipe writes its output file before validating the argument,"
        " so a malformed call truncates it",
    ),
    # class 13: reach half only — no new command
    (
        "13 new command the loop must reach",
        "the recovery path strands a headless claim"
        " when $HOME resolves to the operator's real store",
    ),
    # class 14: signal half only — no lock anywhere
    (
        "14 signal handler meets lock",
        "a SIGTERM landing between fut.result() returning"
        " and the append call loses that observation entirely",
    ),
    # class 14: lock half only — no signal anywhere
    (
        "14 signal handler meets lock",
        "the RLock is acquired twice on the same path, so"
        " a second holder waits forever behind the first",
    ),
    # class 14: `block`/`blocking` are not locks — a bare `lock` alternative matched
    # both, so a Ctrl-C shutdown row landed here with no lock in sight (codex r6 P2)
    (
        "14 signal handler meets lock",
        "Ctrl-C can block in ThreadPoolExecutor shutdown and the blocking wait is"
        " unbounded, so SIGINT never reaches the caller",
    ),
    # class 12: a bare location noun is not the class — every alternative must mean
    # BOTH a quoted obligation and its absence (codex r6 P2)
    (
        "12 quoted contract phrase not discharged",
        "the manifest row lists the wrong tag for this artifact",
    ),
    # class 11: merely MENTIONING adjudication is not an authority-bearing command
    # surface — the bare `adjudicat` alternative pulled 48 such rows (codex r8 P2)
    (
        "11 authority-bearing command surface",
        "the finding is not among the four adjudicated HELD classes",
    ),
    # class 12: naming a contract phrase is only half — the class needs its ABSENCE too,
    # so `spec phrase`/`contract phrase` were dropped as alternatives (codex r9 P2)
    (
        "12 quoted contract phrase not discharged",
        "the contract phrase names the wrong component",
    ),
    # class 13: guards the r9 removal of the `just recipe` alternative. As written this
    # satisfies the reach conjunct only; re-adding `just recipe` to the command conjunct
    # would make it satisfy BOTH, match, and red this case — which is the point.
    (
        "13 new command the loop must reach",
        "the return status from loop_log_structured is discarded by the just recipe and"
        " replaced with return 1",
    ),
]


def test_near_miss_cases_each_satisfy_exactly_one_conjunct():
    """A near miss discriminates AND from OR only if it satisfies exactly ONE conjunct.

    Satisfying NEITHER is the silent failure: the case still passes, so it looks like
    coverage, while the mutation it was written to catch goes undetected. That is not
    hypothetical — when r9 narrowed class 13's command terms to
    `runs_in|new recipe|new command`, a case citing a `justfile:` recipe stopped
    satisfying either conjunct and went dead, and dropping the reach conjunct became
    untestable (merge-gate witness lens, round 1). Checking the balance mechanically is
    what keeps the near-miss table honest as the patterns keep narrowing.
    """
    mod = _preflight_module()
    for cls, evidence in _NEAR_MISSES:
        pattern = mod.CLASSES[cls]
        if isinstance(pattern, str):
            continue  # disjunction rows have no conjuncts to balance
        satisfied = sum(bool(re.search(p, evidence, re.I)) for p in pattern)
        assert satisfied == 1, (
            f"{cls}: near miss satisfies {satisfied} conjunct(s), must be exactly 1 — {evidence!r}"
        )


@pytest.mark.parametrize(("cls", "evidence"), _NEAR_MISSES)
def test_refresh_classes_conjunctions_refuse_half_matches(cls: str, evidence: str):
    """Over-broad rows corrupt the counts the SKILL cites AND remove those findings from
    unmatched new-class discovery — the more expensive half, since a swallowed row can
    never surface as a candidate again. Each near miss satisfies one conjunct only."""
    mod = _preflight_module()
    classes = mod.CLASSES
    assert not mod.matches(classes[cls], evidence), f"{cls} matched a half-match"


def test_refresh_classes_rows_are_not_generically_over_broad():
    """Wholly unrelated evidence must land in none of the U-SR-01 classes — the trap that
    took class 12 from 6 to 126 mid-absorption."""
    mod = _preflight_module()
    classes = mod.CLASSES
    unrelated = "the fixture teardown leaks a temp dir when an assertion fails mid-run"
    for cls in (
        "11 authority-bearing command surface",
        "12 quoted contract phrase not discharged",
        "13 new command the loop must reach",
        "14 signal handler meets lock",
    ):
        assert not mod.matches(classes[cls], unrelated), f"{cls} is over-broad"


def test_attest_sweep_records_and_covers(repo: Path, monkeypatch: pytest.MonkeyPatch):
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(
        json.dumps(_row(1, "finding", "cw:aa:11:1"))
        + "\n"
        + json.dumps(_row(1, "finding", "cw:aa:11:2"))
        + "\n"
    )
    a = repo / "sweep.md"
    a.write_text("cw:aa:11:1 fixed by X; cw:aa:11:2 registered\n")
    rc = rlg.main(["attest-sweep", "--answers", str(a), "--base", "main", "--repo", str(repo)])
    assert rc == 0
    (sw,) = rlg.load_state(repo).sweeps
    assert set(sw.finding_ids) == {"cw:aa:11:1", "cw:aa:11:2"}
    binding = rw.code_binding(repo, "main")
    assert sw.head_sha == binding["head_sha"]
    assert sw.diff_digest == binding["diff_digest"]


def test_attest_sweep_id_matching_is_token_exact(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r2 P2: an answer naming ...:10 must not satisfy the sibling ...:1
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(
        json.dumps(_row(1, "finding", "cw:aa:11:1"))
        + "\n"
        + json.dumps(_row(1, "finding", "cw:aa:11:10"))
        + "\n"
    )
    a = repo / "sweep.md"
    a.write_text("cw:aa:11:10 fixed\n")
    rc = rlg.main(["attest-sweep", "--answers", str(a), "--base", "main", "--repo", str(repo)])
    assert rc != 0
    assert rlg.load_state(repo).sweeps == ()


def test_attest_tmp_symlink_cannot_redirect_write(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r2 P1: a pre-planted symlink at the fixed .tmp name must not receive the
    # write — unlink removes the LINK itself, exclusive-create refuses any survivor
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    victim = repo / "victim.json"
    victim.write_text("precious\n")
    sp = rlg.state_path(repo)
    sp.parent.mkdir(parents=True, exist_ok=True)
    # plant at the ACTUAL per-pid temp name production opens (codex r6 P3: the old
    # fixed-name plant went stale when r5 made temp names per-pid, leaving the
    # witness green even with O_EXCL/O_NOFOLLOW removed)
    tmp_name = f"{sp.name}.{os.getpid()}.tmp"
    sp.with_name(tmp_name).symlink_to(victim)
    rc = _attest_preflight(repo)
    assert rc != 0  # O_EXCL refuses the planted entry loudly
    assert victim.read_text() == "precious\n"  # never written through
    assert rlg.load_state(repo).preflights == ()
    sp.with_name(tmp_name).unlink()  # planted link removed by hand -> attest succeeds
    rc = _attest_preflight(repo)
    assert rc == 0
    assert victim.read_text() == "precious\n"
    assert len(rlg.load_state(repo).preflights) == 1


def test_attest_sweep_missing_id_refuses(repo: Path, monkeypatch: pytest.MonkeyPatch):
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    fr.GATE_LOG_JSONL.write_text(json.dumps(_row(1, "finding", "cw:aa:11:1")) + "\n")
    a = repo / "sweep.md"
    a.write_text("nothing relevant\n")
    rc = rlg.main(["attest-sweep", "--answers", str(a), "--base", "main", "--repo", str(repo)])
    assert rc != 0
    assert rlg.load_state(repo).sweeps == ()


def test_attest_sweep_on_corrupt_state_treats_it_as_empty(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    # a corrupt (e.g. old-schema) state must not brick the repair verb: obligations
    # compute against EMPTY state (maximal — tightening) and the write re-inits loudly
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    rlg.state_path(repo).parent.mkdir(parents=True, exist_ok=True)
    rlg.state_path(repo).write_text('{"records": [{"kind": "sweep", "bogus_field": 1}]}')
    fr.GATE_LOG_JSONL.write_text(json.dumps(_row(1, "finding", "cw:aa:11:1")) + "\n")
    a = repo / "sweep.md"
    a.write_text("cw:aa:11:1 fixed\n")
    rc = rlg.main(["attest-sweep", "--answers", str(a), "--base", "main", "--repo", str(repo)])
    assert rc == 0
    assert "EMPTY state" in capsys.readouterr().err
    (sw,) = rlg.load_state(repo).sweeps
    assert sw.finding_ids == ("cw:aa:11:1",)


def test_attest_budget_requires_reason_and_records(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    rc = rlg.main(["attest-budget", "--extra", "2", "--reason", "", "--repo", str(repo)])
    assert rc != 0
    # codex r5 P2: a non-positive "extension" would invert the tightening invariant
    rc = rlg.main(["attest-budget", "--extra", "0", "--reason", "ok", "--repo", str(repo)])
    assert rc != 0
    rc = rlg.main(["attest-budget", "--extra", "-3", "--reason", "ok", "--repo", str(repo)])
    assert rc != 0
    assert rlg.load_state(repo).extensions == ()
    for bad in (-2, True):  # bool subclasses int (codex r6 P2): true must refuse too
        rlg.state_path(repo).write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "kind": "budget_extension",
                            "arc_id": ARC,
                            "extra_rounds": bad,
                            "reason": "r",
                            "ts": "t",
                        }
                    ]
                }
            )
        )
        with pytest.raises(rlg.GateError, match="extra_rounds"):
            rlg.load_state(repo)
    rlg.state_path(repo).unlink()
    rc = rlg.main(
        ["attest-budget", "--extra", "2", "--reason", "operator approved", "--repo", str(repo)]
    )
    assert rc == 0
    (ext,) = rlg.load_state(repo).extensions
    assert ext.extra_rounds == 2


def test_attest_on_corrupt_state_reinits_loudly(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    # safe direction only: state loss tightens, so a corrupt file may be re-inited
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    rlg.state_path(repo).write_text("{corrupt")
    rc = _attest_preflight(repo)
    assert rc == 0
    assert "re-init" in capsys.readouterr().err
    assert len(rlg.load_state(repo).preflights) == 1


def test_symlinked_state_file_is_containment_refusal(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r1 P1: a pre-planted symlink must refuse loudly — never followed on read,
    # never "re-initialized" through on attest (the re-init license covers corrupt
    # CONTENT only)
    target = repo / "victim.json"
    target.write_text("precious\n")
    rlg.state_path(repo).symlink_to(target)
    with pytest.raises(rlg.GateError, match="containment"):
        rlg.load_state(repo)
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: True)
    d = rlg.admit(repo, "main", ARC)
    assert isinstance(d, rlg.Refused)
    assert d.code == "STATE_UNREADABLE"
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    rc = _attest_preflight(repo)
    assert rc != 0
    assert target.read_text() == "precious\n"  # never written through


def test_admit_reservation_store_unreadable_refuses(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r4 P1: "cannot tell" must REFUSE, never disable the gate — a corrupt
    # store would otherwise turn an actually-reserved arc's gate off
    def boom(arc_id):
        raise OSError("corrupt generation")

    monkeypatch.setattr(rlg, "_reservation_exists", boom)
    d = rlg.admit(repo, "main", ARC)
    assert isinstance(d, rlg.Refused)
    assert d.code == "STATE_UNREADABLE"
    assert "reservation store" in d.detail


def test_state_field_types_validated_at_parse(repo: Path):
    # codex r4 P2: dataclasses do not enforce annotations — extra_rounds="2" must
    # resolve to STATE_UNREADABLE at the boundary, not TypeError inside decide()
    rlg.state_path(repo).parent.mkdir(parents=True, exist_ok=True)
    rlg.state_path(repo).write_text(
        json.dumps(
            {
                "records": [
                    {
                        "kind": "budget_extension",
                        "arc_id": ARC,
                        "extra_rounds": "2",
                        "reason": "r",
                        "ts": "t",
                    }
                ]
            }
        )
    )
    with pytest.raises(rlg.GateError, match="extra_rounds"):
        rlg.load_state(repo)


def test_symlinked_answers_file_refused(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r4 P2: the attest verbs' one file input gets the same containment
    # discipline as the state file — an in-worktree symlink must not smuggle an
    # outside file through a guard-approved invocation
    _plant_script(repo, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    outside = repo.parent / "outside-answers.md"
    outside.write_text("everything answered\n")
    a = repo / "answers.md"
    a.symlink_to(outside)
    rc = rlg.main(["attest-preflight", "--answers", str(a), "--base", "main", "--repo", str(repo)])
    assert rc != 0
    assert rlg.load_state(repo).preflights == ()


def test_finding_row_without_id_refuses_not_skips():
    # codex r8 P2: valid-JSON log corruption (a finding row with no finding_id) must
    # refuse — silently skipping it would erase an obligation
    rows = [_row(1, "finding", None)]
    d = _decide(_state(preflights=(_pf(),)), rows)
    assert isinstance(d, rlg.Refused)
    assert d.code == "STATE_UNREADABLE"


def test_nested_import_error_is_not_absent_substrate(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r8 P1: a broken import INSIDE reservations must refuse, not inactivate
    def broken(arc_id):
        raise ImportError("No module named 'arc_metrics'", name="arc_metrics")

    monkeypatch.setattr(rlg, "_reservation_exists", broken)
    d = rlg.admit(repo, "main", ARC)
    assert isinstance(d, rlg.Refused)
    assert d.code == "STATE_UNREADABLE"


def test_admit_gate_log_unreadable_refuses(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r7 P2: the gate log is the round/obligation authority — unreadable must
    # produce the typed refusal, never a traceback out of the wrapper
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: True)
    fr.GATE_LOG_JSONL.write_text("{not jsonl\n")
    d = rlg.admit(repo, "main", ARC)
    assert isinstance(d, rlg.Refused)
    assert d.code == "STATE_UNREADABLE"
    assert "gate log" in d.detail


def test_admit_binding_unavailable_defers_to_wrapper(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # codex r1 P2: an unresolvable base is wrapper infrastructure, not an admission
    # fact — defer to run_codex_review's classifier instead of crashing pre-terminal
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: True)
    d = rlg.admit(repo, "no-such-base", ARC)
    assert isinstance(d, rlg.Inactive)
    assert "binding unavailable" in d.reason


# ── wiring: codex_review.main() gates before any reviewer ────────────────────


@pytest.fixture()
def wired(repo: Path, monkeypatch: pytest.MonkeyPatch):
    import codex_review as cr

    monkeypatch.chdir(repo)
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    monkeypatch.setenv("HARNESS_LANE_ID", "lane-t")
    monkeypatch.setattr(
        cr, "run_bounded", lambda *a, **k: pytest.fail("reviewer ran despite gate refusal")
    )
    # a reservation exists for the arc → the gate is ACTIVE
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: True)
    return cr


def test_wrapper_refuses_before_any_reviewer(wired, capsys):
    rc = wired.main(["--base", "main"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "codex-review: GATE_REFUSED (PREFLIGHT_MISSING)" in err
    # the recovery recipe carries the ENV-sourced lane, proving admit()'s
    # env_arc_and_lane -> decide(lane_id=...) plumbing end-to-end (merge-gate
    # witness lens r1: deleting that line, or swapping the tuple unpack, must red)
    assert "HARNESS_LANE_ID=lane-t just" in err
    # NOT a review terminal: no C-HE-24 row, no round consumed
    assert fr.read_rows() == []


def test_wrapper_gate_fires_even_with_round_env(wired, monkeypatch: pytest.MonkeyPatch):
    # the advisor-killed bypass: HARNESS_ROUND_N forces row numbering for the
    # failover CHILD (gemini), which never re-enters codex_review.main —
    # nothing legitimate needs a gate skip here
    monkeypatch.setenv("HARNESS_ROUND_N", "7")
    assert wired.main(["--base", "main"]) == 3


def test_wrapper_inactive_arc_warns_and_proceeds(wired, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: False)
    monkeypatch.setattr(
        wired, "run_codex_review", lambda repo, base, invoke=None: pytest.fail("stop at review")
    )
    with pytest.raises(pytest.fail.Exception):
        wired.main(["--base", "main"])
    assert "review gate INACTIVE" in capsys.readouterr().err


# ── wiring: agy_review.main() gates standalone gemini (codex r1 P1) ──────────


@pytest.fixture()
def agy_wired(repo: Path, monkeypatch: pytest.MonkeyPatch):
    import agy_review as agy

    monkeypatch.chdir(repo)
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    monkeypatch.setenv("HARNESS_LANE_ID", "lane-t")
    monkeypatch.delenv("HARNESS_FAILOVER_CHILD", raising=False)
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: True)
    monkeypatch.setattr(sys, "argv", ["agy_review.py", "--base", "main"])
    return agy


def test_agy_standalone_refuses_unattested(agy_wired, monkeypatch: pytest.MonkeyPatch, capsys):
    # an ungated standalone gemini review would mint loop rounds that poison the
    # guarded wrapper's round count (last > 0 skips the entry preflight)
    monkeypatch.setattr(
        agy_wired, "run_review", lambda repo, base: pytest.fail("reviewer ran despite refusal")
    )
    assert agy_wired.main() == 3
    assert "gemini-review: GATE_REFUSED (PREFLIGHT_MISSING)" in capsys.readouterr().err


def test_agy_failover_child_skips_gate(agy_wired, monkeypatch: pytest.MonkeyPatch):
    # the parent admitted the round; the child must not re-gate (nor double-refuse)
    monkeypatch.setenv("HARNESS_FAILOVER_CHILD", "1")
    monkeypatch.setattr(agy_wired, "run_review", lambda repo, base: 0)
    assert agy_wired.main() == 0


# ── launch verb: per-attempt names (U-HE-49; C-HE-21 §1 X6b) ─────────────────


def test_attempt_destination_first_attempt():
    assert (
        rlg.attempt_destination(".harness/tmp/x-rounds/r9.log", [])
        == ".harness/tmp/x-rounds/r9-a1.log"
    )


def test_attempt_destination_retry_increments():
    assert (
        rlg.attempt_destination(".harness/tmp/x-rounds/r9.log", ["r9-a1.log"])
        == ".harness/tmp/x-rounds/r9-a2.log"
    )


def test_attempt_destination_is_max_plus_one_not_count():
    # a deleted intermediate attempt must never resurrect a taken write-once name
    assert rlg.attempt_destination("d/r9.log", ["r9-a1.log", "r9-a3.log"]) == "d/r9-a4.log"


def test_attempt_destination_sibling_rounds_do_not_interfere():
    # the r1 prefix trap: r10/r11 attempts are not r1 attempts
    names = ["r1-a1.log", "r10-a2.log", "r11-a5.log", "r9-verdict-a1.log"]
    assert rlg.attempt_destination("d/r1.log", names) == "d/r1-a2.log"


def test_attempt_destination_bare_legacy_name_never_reused():
    # a pre-U-HE-49 bare `r9.log` on disk: attempts mint beside it, never claim it
    assert rlg.attempt_destination("d/r9.log", ["r9.log"]) == "d/r9-a1.log"


def test_attempt_destination_idempotent_on_own_output():
    # requesting a prior attempt's name mints the round's NEXT attempt, not a nested one
    assert rlg.attempt_destination("d/r9-a1.log", ["r9-a1.log"]) == "d/r9-a2.log"


def test_attempt_destination_extensionless_basename():
    assert rlg.attempt_destination("r9", []) == "r9-a1"


def test_attempt_names_key_to_their_round_for_arc_metrics():
    # seam contract with U-HE-46 round derivation: the r<N> prefix survives the
    # attempt suffix, so every attempt keys to its round and a refused attempt
    # plus its retry collapse to one round
    import arc_metrics as am

    stem = Path(rlg.attempt_destination("d/r9.log", ["r9-a1.log"])).stem
    m = am.ROUND_ID_RE.match(stem)
    assert m is not None and m.group(1) == "9"


# ── launch verb: admission before launch (U-HE-49; C-HE-21 §1 X6b) ───────────


def _seed_current_preflight(repo: Path) -> None:
    binding = rw.code_binding(repo, "main")
    rec = {
        "kind": "preflight",
        "arc_id": ARC,
        "head_sha": binding["head_sha"],
        "diff_digest": binding["diff_digest"],
        "hit_labels": [],
        "answers_digest": "d",
        "ts": "t",
    }
    rlg.state_path(repo).write_text(json.dumps({"records": [rec]}))


def _launch(repo: Path, log: str = ".harness/tmp/x-rounds/r1.log") -> int:
    return rlg.main(["launch", "--log", log, "--repo", str(repo)])


@pytest.fixture()
def launch_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARNESS_ARC_ID", ARC)
    monkeypatch.delenv("HARNESS_ROUND_N", raising=False)
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: True)
    return repo


def test_launch_refused_consumes_no_round_identity(launch_env: Path, capsys):
    # [B] F13 shape 1 (r11 launched into BUDGET_EXHAUSTED / r9 into SWEEP_MISSING):
    # the refusal now lands BEFORE the launch — exit 3, no destination minted,
    # no file, not even the rounds directory
    assert _launch(launch_env) == 3
    out, err = capsys.readouterr()
    assert out == ""  # nothing on the dataflow seam — no round identity consumed
    assert "review-launch: GATE_REFUSED (PREFLIGHT_MISSING)" in err
    assert not (launch_env / ".harness/tmp/x-rounds").exists()


def test_launch_refused_on_spent_budget_before_launch(launch_env: Path, capsys):
    # the literal F13 r11 shape: budget spent → the launch is not made
    _seed_current_preflight(launch_env)
    rows = [_row(n, "no_finding") for n in range(1, rlg.DEFAULT_ROUND_BUDGET + 1)]
    (launch_env / "gate-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert _launch(launch_env, ".harness/tmp/x-rounds/r11.log") == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "review-launch: GATE_REFUSED (BUDGET_EXHAUSTED)" in err
    assert not (launch_env / ".harness/tmp/x-rounds").exists()


def test_launch_allowed_prints_first_attempt_destination(launch_env: Path, capsys):
    _seed_current_preflight(launch_env)
    assert _launch(launch_env) == 0
    out, err = capsys.readouterr()
    assert out.strip() == ".harness/tmp/x-rounds/r1-a1.log"
    assert "review-launch: ALLOWED (next round 1)" in err


def test_launch_inactive_arc_proceeds_with_attempt_name(launch_env, monkeypatch, capsys):
    # unreserved degradation path (sanctioned): the gate is not in force, but the
    # per-attempt naming still applies — naming is identity discipline, not admission
    monkeypatch.setattr(rlg, "_reservation_exists", lambda arc_id: False)
    assert _launch(launch_env) == 0
    out, err = capsys.readouterr()
    assert out.strip() == ".harness/tmp/x-rounds/r1-a1.log"
    assert "review-launch: gate INACTIVE" in err


def test_launch_retry_after_refused_attempt_publishes_cleanly(launch_env: Path, capsys):
    # [B] F13 shape 2 (r9's relaunch reused `r9.log` → PUBLISH FAILED exit 4):
    # a wrapper-level refused attempt occupies the round's -a1 name (a refused
    # attempt minted no gate-log row, so the round identity has NOT advanced);
    # the retry re-requests the SAME round, mints -a2, and the REAL publisher
    # accepts it — no write-once collision
    _seed_current_preflight(launch_env)
    rounds = launch_env / ".harness/tmp/x-rounds"
    rounds.mkdir(parents=True)
    (rounds / "r1-a1.log").write_text("codex-review: GATE_REFUSED (SWEEP_MISSING)\n")
    assert _launch(launch_env, ".harness/tmp/x-rounds/r1.log") == 0
    dest = capsys.readouterr().out.strip()
    assert dest == ".harness/tmp/x-rounds/r1-a2.log"
    publisher = Path(__file__).resolve().parent / "round_log_publish.py"
    done = subprocess.run(
        [sys.executable, str(publisher), dest],
        cwd=launch_env,
        input=b"codex-review: BLOCK\n",
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr
    assert (rounds / "r1-a2.log").read_text() == "codex-review: BLOCK\n"
    # the OLD shape stays refused: re-publishing an existing attempt name is still
    # the publisher's write-once refusal — per-attempt naming is what avoids it
    again = subprocess.run(
        [sys.executable, str(publisher), ".harness/tmp/x-rounds/r1-a1.log"],
        cwd=launch_env,
        input=b"x\n",
        capture_output=True,
    )
    assert again.returncode == 4


def test_launch_refuses_round_name_not_matching_recorded_rounds(launch_env: Path, capsys):
    # codex r1 P2: after a RECORDED round 1, re-requesting `r1.log` would mint
    # r1-a2 while the wrapper records round 2 — two review transcripts claiming
    # round 1, which arc_metrics refuses. The launch binds the requested name to
    # round_n_for (the same mint the wrapper records) BEFORE the reviewer call.
    _seed_current_preflight(launch_env)
    (launch_env / "gate-log.jsonl").write_text(json.dumps(_row(1, "no_finding")) + "\n")
    assert _launch(launch_env, ".harness/tmp/x-rounds/r1.log") == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "GATE_REFUSED (ROUND_NAME_MISMATCH)" in err
    assert "next primary round is 2" in err
    assert not (launch_env / ".harness/tmp/x-rounds").exists()
    # the round the mint names IS accepted
    assert _launch(launch_env, ".harness/tmp/x-rounds/r2.log") == 0
    assert capsys.readouterr().out.strip() == ".harness/tmp/x-rounds/r2-a1.log"


def test_crashed_publisher_temp_neither_blocks_attempt_reuse_nor_its_publish(
    launch_env: Path, capsys
):
    # codex r3/r4 rejection, promoted to a fail-closed witness: a hard-killed
    # publisher leaves only its private `.r<N>-a<K>.log.<pid>.<hex>.tmp` — the
    # FINAL name was never installed, so it is legitimately free. The next
    # launch mints that same attempt name (the anchored matcher ignores the
    # temp) and the REAL publisher installs it cleanly; the orphan temp's bytes
    # survive untouched. No evidence is lost and no collision exists.
    _seed_current_preflight(launch_env)
    rounds = launch_env / ".harness/tmp/x-rounds"
    rounds.mkdir(parents=True)
    temp = rounds / ".r1-a1.log.12345.deadbeef.tmp"
    temp.write_text("partial transcript, publisher killed\n")
    assert _launch(launch_env) == 0
    dest = capsys.readouterr().out.strip()
    assert dest == ".harness/tmp/x-rounds/r1-a1.log"
    publisher = Path(__file__).resolve().parent / "round_log_publish.py"
    done = subprocess.run(
        [sys.executable, str(publisher), dest],
        cwd=launch_env,
        input=b"codex-review: BLOCK\n",
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr
    assert (rounds / "r1-a1.log").read_text() == "codex-review: BLOCK\n"
    assert temp.read_text() == "partial transcript, publisher killed\n"


def test_launch_refuses_destination_outside_harness_tmp(launch_env: Path, capsys):
    # codex r3 P2 (form mirror of the publisher's policy, checked BEFORE the paid
    # call): a destination the publisher would refuse must refuse at launch
    _seed_current_preflight(launch_env)
    assert _launch(launch_env, "tools/r1.log") == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "GATE_REFUSED (DEST_REFUSED)" in err


def test_launch_refuses_symlinked_rounds_dir(launch_env: Path, tmp_path_factory, capsys):
    # codex r3 P2: a pre-planted symlink under .harness/tmp must not route even
    # the read-only attempt listing outside the worktree — O_NOFOLLOW dir-fd walk
    _seed_current_preflight(launch_env)
    outside = tmp_path_factory.mktemp("outside")
    (outside / "r1-a1.log").write_text("planted\n")
    (launch_env / ".harness/tmp").mkdir(parents=True)
    (launch_env / ".harness/tmp/x-rounds").symlink_to(outside)
    assert _launch(launch_env) == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "GATE_REFUSED (DEST_REFUSED)" in err
    assert "containment" in err


def test_launch_refuses_stale_forced_round_env(launch_env: Path, monkeypatch, capsys):
    # codex r5: a leaked HARNESS_ROUND_N would force the WRAPPER (which honors
    # it) onto an already-recorded round — refuse pre-launch unless the forced
    # value IS the next unused primary round
    _seed_current_preflight(launch_env)
    (launch_env / "gate-log.jsonl").write_text(json.dumps(_row(1, "no_finding")) + "\n")
    monkeypatch.setenv("HARNESS_ROUND_N", "1")
    assert _launch(launch_env, ".harness/tmp/x-rounds/r2.log") == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "GATE_REFUSED (FORCED_ROUND_STALE)" in err
    # a forced value equal to the next unused round is the failover-child shape
    # and passes
    monkeypatch.setenv("HARNESS_ROUND_N", "2")
    assert _launch(launch_env, ".harness/tmp/x-rounds/r2.log") == 0
    assert capsys.readouterr().out.strip() == ".harness/tmp/x-rounds/r2-a1.log"


def test_launch_refuses_alias_round_names(launch_env: Path, capsys):
    # codex r5/r6: r01/round-1/r1-notes parse to the right NUMBER but would mint
    # a second attempt family for one round, and r1/r1.txt would publish outside
    # the documented r*.log glob — only the canonical r<N>.log basename launches
    _seed_current_preflight(launch_env)
    for alias in ("r01.log", "round-1.log", "r1-notes.log", "r1", "r1.txt"):
        assert _launch(launch_env, f".harness/tmp/x-rounds/{alias}") == 3
        out, err = capsys.readouterr()
        assert out == "", alias
        assert "GATE_REFUSED (ROUND_NAME_MISMATCH)" in err, alias
    assert not (launch_env / ".harness/tmp/x-rounds").exists()


def test_launch_refuses_unparseable_round_name(launch_env: Path, capsys):
    # a name arc_metrics cannot key to a round would poison the evidence set at
    # queue time — refuse it BEFORE the reviewer call, not after
    _seed_current_preflight(launch_env)
    assert _launch(launch_env, ".harness/tmp/x-rounds/final.log") == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "GATE_REFUSED (ROUND_NAME_UNPARSEABLE)" in err
    assert not (launch_env / ".harness/tmp/x-rounds").exists()


_RESERVATIONS = Path(__file__).resolve().parent / "reservations.py"


def _rsv(qdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_RESERVATIONS), *args],
        env=dict(os.environ, ARC_METRICS_QUEUE_DIR=str(qdir)),
        capture_output=True,
        text=True,
    )


def _seed_reservation(qdir: Path, arc: str = "arc-x", lane: str = "lane-1") -> None:
    done = _rsv(
        qdir,
        "reserve",
        "--arc-id",
        arc,
        "--lane-id",
        lane,
        "--branch",
        "b",
        "--arc-type",
        "applying",
    )
    assert done.returncode == 0, done.stderr


def _phases(qdir: Path, arc: str = "arc-x") -> dict:
    done = _rsv(qdir, "show", "--arc-id", arc)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout).get("phases", {})


def _recipe_body(log: str, base: str = "main") -> str:
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    body = justfile.split("review-with-failover-logged log base='main':", 1)[1].split("\n\n", 1)[0]
    lines = [ln[4:] if ln.startswith("    ") else ln for ln in body.splitlines()]
    return "\n".join(lines).replace("{{log}}", log).replace("{{base}}", base) + "\n"


@pytest.fixture()
def recipe_env(tmp_path: Path):
    """Execute the REAL recipe body under a PATH-shimmed `uv` (merge-gate witness
    lens, PR #1471): the shim refuses/allows the launch verb per REC_MODE, marks
    any wrapper invocation, and execs the REAL publisher — so the bash
    control-flow binding (`|| exit`, the empty-dest arm, PIPESTATUS folding) is
    witnessed by execution, not by substring presence."""
    repo = tmp_path / "repo"
    (repo / ".harness").mkdir(parents=True)
    shim = tmp_path / "bin"
    shim.mkdir()
    real_publisher = Path(__file__).resolve().parent / "round_log_publish.py"
    (shim / "uv").write_text(
        "#!/usr/bin/env bash\n"
        "# args: run python tools/<script> ...\n"
        'script="$3"; shift 3\n'
        'case "$script" in\n'
        "  tools/review_loop_gate.py)\n"
        '    case "$REC_MODE" in\n'
        "      refuse) echo 'review-launch: GATE_REFUSED (TEST)' >&2; exit 3 ;;\n"
        "      empty) exit 0 ;;\n"
        # allow: echo a MINTED name distinguishable from the raw --log value, as the
        # real verb always does — witness-lens r2: an echo of the input cannot
        # discriminate `publish "$dest"` from `publish "{{log}}"`
        '      allow) shift; echo "${2%.log}-a1.log" ;;\n'
        "    esac ;;\n"
        "  tools/codex_review.py)\n"
        '    touch "$REC_MARKER"\n'
        # REC_WRAP_MODE selects the wrapper's terminal (codex u-he-50 r4): approve
        # exercises the final-round arm, refused the in-process GATE_REFUSED arm
        '    case "${REC_WRAP_MODE:-block}" in\n'
        '      approve) printf "codex-review: APPROVE\\n"; exit 0 ;;\n'
        '      refused) printf "review-launch: GATE_REFUSED (INNER)\\n"; exit 3 ;;\n'
        '      *) printf "codex-review: BLOCK\\n"; exit 1 ;;\n'
        "    esac ;;\n"
        "  tools/round_log_publish.py)\n"
        f'    exec "{sys.executable}" "{real_publisher}" "$@" ;;\n'
        # U-HE-50: span emission execs the REAL store writer against the fixture's
        # ARC_METRICS_QUEUE_DIR, so the verify pair below is witnessed on a real
        # reservation head, not a stub's echo; REC_PHASE_MODE=fail-start injects a
        # start-edge store failure (codex u-he-50 r1: the partial-write arm)
        "  tools/reservations.py)\n"
        '    if [ "${REC_PHASE_MODE:-}" = fail-start ]; then\n'
        '      case "$*" in *"--phase verify --edge start"*)\n'
        '        echo "ABORT: injected start failure" >&2; exit 2 ;; esac\n'
        "    fi\n"
        '    if [ "${REC_PHASE_MODE:-}" = fail-end ]; then\n'
        '      case "$*" in *"--phase verify --edge end"*)\n'
        '        echo "ABORT: injected end failure" >&2; exit 2 ;; esac\n'
        "    fi\n"
        f'    exec "{sys.executable}" "{_RESERVATIONS}" "$@" ;;\n'
        "esac\n"
    )
    (shim / "uv").chmod(0o755)

    def run(
        mode: str,
        log: str = ".harness/tmp/x-rounds/r1.log",
        arc_env: dict[str, str] | None = None,
        phase_mode: str = "",
        wrap_mode: str = "",
    ):
        script = tmp_path / "recipe.sh"
        script.write_text(_recipe_body(log))
        env = dict(
            os.environ,
            PATH=f"{shim}:{os.environ['PATH']}",
            REC_MODE=mode,
            REC_MARKER=str(tmp_path / "wrapper-ran"),
            ARC_METRICS_QUEUE_DIR=str(tmp_path / "queue"),
            REC_PHASE_MODE=phase_mode,
            REC_WRAP_MODE=wrap_mode,
        )
        # hermetic: a live session's exported arc ids must not leak emission into
        # tests that model the unreserved invocation
        for key in ("HARNESS_ARC_ID", "HARNESS_LANE_ID"):
            env.pop(key, None)
        env.update(arc_env or {})
        return subprocess.run(
            ["bash", str(script)], cwd=repo, env=env, capture_output=True, text=True
        )

    return repo, tmp_path, run


def test_recipe_execution_refused_launch_never_reaches_wrapper(recipe_env):
    repo, tmp, run = recipe_env
    done = run("refuse")
    assert done.returncode == 3
    assert not (tmp / "wrapper-ran").exists()  # the reviewer call was NOT made
    assert not (repo / ".harness/tmp").exists()  # and no log was written


def test_recipe_execution_empty_dest_aborts_before_wrapper(recipe_env):
    _repo, tmp, run = recipe_env
    done = run("empty")
    assert done.returncode == 4
    assert "printed no destination" in done.stderr
    assert not (tmp / "wrapper-ran").exists()


def test_recipe_execution_allowed_pipes_wrapper_through_real_publisher(recipe_env):
    repo, tmp, run = recipe_env
    done = run("allow")
    assert done.returncode == 1  # the stub wrapper's BLOCK exit survives PIPESTATUS folding
    assert (tmp / "wrapper-ran").exists()
    # the publish lands at the MINTED destination, never the caller's raw name —
    # execution-discriminates `publish "$dest"` from `publish "{{log}}"` (lens r2)
    assert (repo / ".harness/tmp/x-rounds/r1-a1.log").read_text() == "codex-review: BLOCK\n"
    assert not (repo / ".harness/tmp/x-rounds/r1.log").exists()


def test_logged_recipe_evaluates_admission_before_launch():
    # the launch step composition ([LAW:verifiable-goals] on the bash seam): the
    # launch verb runs BEFORE the wrapper, short-circuits on refusal, and the
    # publisher receives the minted per-attempt destination, never the caller's name.
    # Mutation-probe carrier: removing the pre-launch admission line reds this test.
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    recipe = justfile.split("review-with-failover-logged log base='main':", 1)[1].split("\n\n", 1)[
        0
    ]
    assert recipe.index("review_loop_gate.py launch") < recipe.index("codex_review.py")
    launch_line = next(ln for ln in recipe.splitlines() if "review_loop_gate.py launch" in ln)
    assert '|| exit "$?"' in launch_line  # a refused launch is not made
    assert 'round_log_publish.py "$dest"' in recipe
    assert 'round_log_publish.py "{{log}}"' not in recipe


# --- U-HE-50 (C-HE-27 §5 X6a): the wrapper's process boundaries are the verify edges ---

_ARC_ENV = {"HARNESS_ARC_ID": "arc-x", "HARNESS_LANE_ID": "lane-1"}


def test_recipe_reserved_arc_emits_verify_span_pair(recipe_env):
    # the acceptance witness: a fixture round through the REAL recipe body writes
    # the verify {start, end} pair on the reservation head with zero skill-prose
    # involvement — the wrapper is the emitter
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    done = run("allow", arc_env=dict(_ARC_ENV))
    assert done.returncode == 1  # the verdict exit is untouched by emission
    verify = _phases(tmp / "queue").get("verify", {})
    assert set(verify) == {"start", "end"}
    assert verify["start"] <= verify["end"]  # ISO-8601 UTC orders lexicographically
    assert "WARN" not in done.stderr


def test_recipe_round2_reemission_keeps_round1_window(recipe_env):
    # record_phase is first-write-wins + replay-idempotent, so the round-2 wrapper
    # re-emits as a no-op and the durable pair stays the round-1 window (the ship-pr
    # "verify = the round-1 window" semantics hold by construction, not by prose)
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    run("allow", arc_env=dict(_ARC_ENV))
    first = _phases(tmp / "queue")["verify"]
    done = run("allow", ".harness/tmp/x-rounds/r2.log", arc_env=dict(_ARC_ENV))
    assert done.returncode == 1
    assert _phases(tmp / "queue")["verify"] == first
    assert "WARN" not in done.stderr  # a no-op replay is not an emission failure


def test_recipe_refused_launch_opens_no_verify_span(recipe_env):
    # C-HE-21 §1 X6b composition: admission precedes emission — a refused launch
    # spends no reviewer call, claims no round name, and opens no span.
    # Mutation-probe carrier: moving `emit_verify start` above the launch line
    # reds this test.
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    done = run("refuse", arc_env=dict(_ARC_ENV))
    assert done.returncode == 3
    assert "verify" not in _phases(tmp / "queue")


def test_recipe_unreserved_invocation_skips_emission(recipe_env):
    # C-HE-27 §3 disposition: no arc ids in the environment → no store touch at
    # all; the absent span reads null downstream, never a measured zero
    _repo, tmp, run = recipe_env
    done = run("allow")
    assert done.returncode == 1
    assert not (tmp / "queue" / "reservations").exists()
    assert "WARN" not in done.stderr  # the skip is the spec'd disposition, not a failure


def test_recipe_emission_failure_warns_and_preserves_verdict(recipe_env):
    # ids present but no reservation head: record_phase ABORTs (exit 2) — the
    # wrapper warns loud on stderr and the round's verdict + publish are untouched
    # ([LAW:no-silent-failure]: the failure surfaces; it never rewrites the exit).
    # The failed start also mutes the end emission (codex u-he-50 r1: a lone end
    # would durably record a reversed pair).
    repo, _tmp, run = recipe_env
    done = run("allow", arc_env=dict(_ARC_ENV))
    assert done.returncode == 1
    assert "WARN verify.start span emission failed" in done.stderr
    assert "WARN verify.end skipped" in done.stderr
    assert (repo / ".harness/tmp/x-rounds/r1-a1.log").read_text() == "codex-review: BLOCK\n"


def test_recipe_start_failure_records_no_partial_pair(recipe_env):
    # codex u-he-50 r1 (partial-write arm 1): a start-edge store failure with a
    # healthy end path must record NOTHING — a lone end would be a reversed pair
    # once a retry stamps its later start (record_phase refuses no rewrite, so the
    # corrupt state would be durable). The retry then emits a fresh coherent pair.
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    done = run("allow", arc_env=dict(_ARC_ENV), phase_mode="fail-start")
    assert done.returncode == 1
    assert "WARN verify.start span emission failed" in done.stderr
    assert "WARN verify.end skipped" in done.stderr
    assert "verify" not in _phases(tmp / "queue")  # nothing recorded this attempt
    retry = run("allow", ".harness/tmp/x-rounds/r2.log", arc_env=dict(_ARC_ENV))
    assert retry.returncode == 1
    verify = _phases(tmp / "queue")["verify"]
    assert set(verify) == {"start", "end"}
    assert verify["start"] <= verify["end"]  # coherent, never reversed


def test_recipe_end_failure_leaves_open_window_next_round_closes(recipe_env):
    # codex u-he-50 r2 (partial-write arm 3), pinned as the SAME named bound as the
    # crash window: a failed end WRITE leaves the durable start intact and only the
    # next invocation can close it — the recorded span is an upper bound that can
    # overlap the edit window (no durable same-attempt signal exists that would not
    # mint a second authority; the recipe comment names the bound).
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    done = run("allow", arc_env=dict(_ARC_ENV), phase_mode="fail-end")
    assert done.returncode == 1
    assert "WARN verify.end span emission failed" in done.stderr
    open_window = _phases(tmp / "queue")["verify"]
    assert set(open_window) == {"start"}  # start intact, window open
    retry = run("allow", ".harness/tmp/x-rounds/r2.log", arc_env=dict(_ARC_ENV))
    assert retry.returncode == 1
    verify = _phases(tmp / "queue")["verify"]
    assert verify["start"] == open_window["start"]  # first-write-wins keeps round-1 start
    assert set(verify) == {"start", "end"}
    assert verify["start"] <= verify["end"]


def test_recipe_half_set_ids_warn_and_skip_emission(recipe_env):
    # codex u-he-50 r2 P3: one id without the other is a MISCONFIGURED invocation,
    # not the spec'd unreserved case — the skip must be loud, and no store write
    # happens (a half-identity can never bind to the holder fence anyway)
    _repo, tmp, run = recipe_env
    done = run("allow", arc_env={"HARNESS_ARC_ID": "arc-x"})
    assert done.returncode == 1
    assert "must both be set" in done.stderr
    assert not (tmp / "queue" / "reservations").exists()


def test_recipe_env_inherited_flag_cannot_skip_end(recipe_env):
    # codex u-he-50 r5 P3: the skip flag is process-local state, initialized by the
    # recipe — an environment-exported _verify_start_failed=1 must not mute the end
    # emission after a successful start.
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    done = run("allow", arc_env={"_verify_start_failed": "1", **_ARC_ENV})
    assert done.returncode == 1
    verify = _phases(tmp / "queue")["verify"]
    assert set(verify) == {"start", "end"}  # the inherited flag was reset, end emitted
    assert "WARN" not in done.stderr


def test_recipe_inner_gate_refusal_records_no_verify_end(recipe_env):
    # codex u-he-50 r4: the wrapper's own in-process admit is the enforcer of record
    # and can refuse AFTER the launch precheck admitted — GATE_REFUSED is not a round
    # (C-HE-16 §3), so a refused attempt must never land as a COMPLETE verify pair
    # (a lone start closes at the next real round, the named upper bound).
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    done = run("allow", arc_env=dict(_ARC_ENV), wrap_mode="refused")
    assert done.returncode == 3
    assert "GATE_REFUSED is not a round" in done.stderr
    assert set(_phases(tmp / "queue")["verify"]) == {"start"}  # no end recorded


def test_recipe_terminal_end_failure_repaired_by_documented_command(recipe_env):
    # codex u-he-50 r4: on a final APPROVE there is no next round to close a
    # failed-end window — the carriers document the session repair (re-run the
    # failed edge before ship; the head accretes until terminal). Witness the
    # repair lands: approve + fail-end → open pair; the documented CLI closes it.
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    done = run("allow", arc_env=dict(_ARC_ENV), phase_mode="fail-end", wrap_mode="approve")
    assert done.returncode == 0  # APPROVE verdict preserved
    assert "WARN verify.end span emission failed" in done.stderr
    assert set(_phases(tmp / "queue")["verify"]) == {"start"}
    repaired = _rsv(
        tmp / "queue",
        "phase",
        "--arc-id",
        "arc-x",
        "--phase",
        "verify",
        "--edge",
        "end",
        "--lane-id",
        "lane-1",
    )
    assert repaired.returncode == 0, repaired.stderr
    verify = _phases(tmp / "queue")["verify"]
    assert set(verify) == {"start", "end"}
    assert verify["start"] <= verify["end"]


def test_recipe_crash_window_closed_by_retry_end(recipe_env):
    # codex u-he-50 r1 (partial-write arm 2), pinned as a NAMED measurement bound:
    # an attempt that recorded start and died before end leaves an open window; the
    # retry keeps the round identity (U-HE-49) and its terminal closes the pair, so
    # the recorded round-1 span is an upper bound that includes the interruption —
    # first-write-wins keeps the original start (no rewrite).
    _repo, tmp, run = recipe_env
    _seed_reservation(tmp / "queue")
    seeded = _rsv(
        tmp / "queue",
        "phase",
        "--arc-id",
        "arc-x",
        "--phase",
        "verify",
        "--edge",
        "start",
        "--lane-id",
        "lane-1",
    )
    assert seeded.returncode == 0, seeded.stderr
    crashed_start = _phases(tmp / "queue")["verify"]["start"]
    done = run("allow", arc_env=dict(_ARC_ENV))
    assert done.returncode == 1
    verify = _phases(tmp / "queue")["verify"]
    assert verify["start"] == crashed_start  # the crashed attempt's start survives
    assert set(verify) == {"start", "end"}
    assert verify["start"] <= verify["end"]


def test_logged_recipe_emits_verify_at_process_boundaries():
    # U-HE-50 composition pin: start is emitted AFTER admission (a refused launch
    # opens no span) and BEFORE the reviewer call; end closes the window at the
    # pipeline terminal BEFORE the publish-failure exit arm can leave it dangling.
    # Mutation-probe carrier: removing either emission line reds this test.
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    recipe = justfile.split("review-with-failover-logged log base='main':", 1)[1].split("\n\n", 1)[
        0
    ]
    assert recipe.index("review_loop_gate.py launch") < recipe.index("emit_verify start")
    assert recipe.index("emit_verify start") < recipe.index("codex_review.py --base")
    assert recipe.index("codex_review.py --base") < recipe.index("emit_verify end")
    assert recipe.index("emit_verify end") < recipe.index("PUBLISH FAILED")
    assert '--phase verify --edge "$1" --lane-id "$HARNESS_LANE_ID"' in recipe


# --- U-SR-02: preflight meta-rules (charter WR-04/05/06) -------------------------

PREFLIGHT_DIR = Path(".claude/skills/defect-class-preflight")
EVALS_REL = PREFLIGHT_DIR / "evals" / "evals.json"
PREFLIGHT_SKILL_REL = PREFLIGHT_DIR / "SKILL.md"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _evals() -> list[dict]:
    doc = json.loads((_repo_root() / EVALS_REL).read_text(encoding="utf-8"))
    return doc["evals"]


def _preflight_skill_text() -> str:
    return (_repo_root() / PREFLIGHT_SKILL_REL).read_text(encoding="utf-8")


def test_eval_cases_are_structurally_sound_and_their_fixtures_exist():
    """Every eval case's declared fixtures must resolve on disk.

    Nothing exercised evals.json before this: a deleted or renamed fixture left the
    case pointing at nothing and the suite stayed green, so a planted-defect case
    could rot into a no-op without a single red. The whole set is swept (not only
    U-SR-02's) because the gap was never case-specific.
    """
    evals_dir = _repo_root() / EVALS_REL.parent
    cases = _evals()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), f"duplicate eval ids: {ids}"
    for case in cases:
        where = f"eval {case['id']} ({case['eval_name']})"
        assert case["prompt"].strip(), f"{where}: empty prompt"
        assert case["expected_output"].strip(), f"{where}: empty expected_output"
        assert case["assertions"], f"{where}: no assertions"
        for rel in case["files"]:
            assert (evals_dir / rel).is_file(), f"{where}: missing fixture {rel}"


def test_u_sr_02_eval_cases_are_registered():
    """The three charter WR-04/05/06 rules each close on an eval case (charter §3)."""
    by_name = {c["eval_name"]: c for c in _evals()}
    for name in (
        "absorption-sweep-answers-claim-no-new-mechanism",  # WR-04
        "invented-bounds-without-contract-derivation",  # WR-05
        "bare-hold-without-fail-closed-probe",  # WR-06
    ):
        assert name in by_name, f"U-SR-02 eval case removed: {name}"
        assert by_name[name]["files"], f"{name}: case carries no fixture"


def _fixture(rel: str) -> str:
    return (_repo_root() / EVALS_REL.parent / "fixtures-usr02" / rel).read_text(encoding="utf-8")


def _fixture_ast(rel: str) -> ast.Module:
    return ast.parse(_fixture(rel))


def _nodes(tree: ast.AST, *kinds: type[ast.AST]) -> list[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, kinds)]


# Exact-AST comparison. `ast.dump` omits line/col by default, so these compare STRUCTURE
# and nothing else — and `ast.parse` discards comments, which is the whole point: every
# text-substring pin this arc wrote was satisfiable by leaving the old code in a
# migration comment while the running code changed (codex r5 P2, four findings).
def _dump(node: ast.AST) -> str:
    return ast.dump(node)


def _expr(source: str) -> str:
    """Dump of `source` parsed as an expression."""
    return ast.dump(ast.parse(source, mode="eval").body)


def _stmt(source: str) -> str:
    """Dump of `source` parsed as a single statement."""
    return ast.dump(ast.parse(source).body[0])


def _body_shape(fn: ast.FunctionDef) -> list[str]:
    """Statement kinds of `fn`'s body, docstring excluded."""
    return [
        type(s).__name__
        for s in fn.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]


def _call_names(tree: ast.AST) -> set[str]:
    """Dotted names of everything called anywhere in `tree`."""
    names = set()
    for call in _nodes(tree, ast.Call):
        parts, node = [], call.func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        names.add(".".join(reversed(parts)))
    return names


def _one_function(rel: str, name: str) -> ast.FunctionDef:
    fns = [n for n in _nodes(_fixture_ast(rel), ast.FunctionDef) if n.name == name]
    # named, not `next(...)`: a renamed function would otherwise surface as a bare
    # StopIteration, which reds for the wrong reason and reads as a broken test
    assert len(fns) == 1, f"{rel} no longer defines exactly one {name}()"
    # Undecorated, checked HERE so it holds for every row the table pins rather than
    # for whichever one a reviewer happened to look at (codex r4 P2: a synchronising
    # decorator on claim() left the source strings and the If/Assign/Return body
    # untouched, serialising the check-then-set while the race pin stayed green). A
    # decorator can change any pinned property from outside the body it wraps.
    assert not fns[0].decorator_list, f"{rel}:{name}() is decorated; its body no longer decides"
    return fns[0]


# Every defect the U-SR-02 evals grade, declared ONCE as a triple: what the fixture
# must still carry, what the contract clause must still say, and the phrase the
# oracle uses to demand it.
#
# This table is a re-scope by SUBTRACTION, not another layer of hardening. Rounds 1-3
# each found a different unpinned SIDE of one of these defects — r1: the contract
# value was not bound to the clause stating it; r2: the contract could drift from the
# oracle, and banning shapes lost to the next spelling; r3: the `--reps` default, the
# §3.3 refusal code, the release-then-reclaim defect, and the answers' denial were
# each pinned on one side only. Patching a side per round is the arms race this
# workspace's notes say does not converge. One table checked by one loop converges
# instead: a planted defect cannot be declared here with a side missing, because the
# loop reads all three sides of every row.
#
# Adding a planted defect to any of these evals owes a row here.


class _Planted(NamedTuple):
    what: str
    eval_id: int
    demand: str  # phrase the oracle must still use to require this defect
    check: Callable[[], None]  # fixture-side property
    clause: tuple[str, str] | None = None  # (pattern capturing the value, expected)


def _squash(text: str) -> str:
    return " ".join(text.split())


# The answers file's denial, pinned as its exact sentence rather than as the token
# "no new mechanism" (codex r3 P2): rewriting the bullet to ADMIT the mechanism —
# `The old "no new mechanism" answer was wrong; _LiveGroups is new coordination` —
# keeps the token and inverts the meaning, leaving eval 16's required rejection
# inapplicable while the test stayed green.
_DENIAL = (
    "**Race / TOCTOU / atomicity:** no new mechanism — this round only adds a skip "
    "to an existing loop, so there is no new coordination surface to sweep."
)
_DENIAL_CONCLUSION = (
    "Nothing in the diff introduces a mechanism the previous sweep did not already cover."
)

# Every call the bare drain_budget fixture legitimately makes. Pinned as an exact SET
# rather than as a list of banned node types: `ast.Assert/Raise/Compare` missed
# `operator.gt(v, 0)` and `sys.exit(3)`, which are loud probes carrying none of those
# nodes (codex r2 P2). A probe has to RUN something, and anything it runs is a new
# name here.
_DRAIN_BUDGET_CALLS = frozenset(
    {"int", "os.environ.get", "range", "_budget", "step", "time.sleep", "done.append"}
)

# admit()'s returns in SOURCE order: both refusals wrongly use 1 (§3.3 names 3 and
# reserves 0/1 for a schema-parsed verdict), then the admission returns 0. Pinned as
# the exact sequence, not as a substring: `"return 1" in guard` stayed green when
# only ONE of the two refusals was repaired (self-caught by this arc's r2 probe).
_ADMIT_RETURNS = [1, 1, 0]


def _load_fixture(rel: str, mod_name: str):
    """Import a fixture module FRESH — module-level state (`_LIVE`) must not leak.

    Executing the fixtures is the terminal answer to a class that produced findings in
    five straight rounds: every static pin is a PROXY for "this fixture still
    misbehaves", and each proxy had a gap the reviewer found — a lock at the call site
    rather than in the body (r6), a contract-correct guard making the planted one
    unreachable (r6), `set_defaults` overriding the pinned default (r6), a comment
    holding the old text (r5), a wrapper preserving the call set (r5). Running the code
    asserts the property itself, so there is no proxy left to slip past. The eval
    prompts forbid the REVIEWER from executing these files; that constrains the graded
    agent, not this suite.
    """
    src = _repo_root() / EVALS_REL.parent / "fixtures-usr02" / rel
    spec = importlib.util.spec_from_file_location(mod_name, src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Bullet labels of the answers file, in order. The denial was pinned by substring until
# codex r6 P2: moving both exact sentences into a quoted "previous incorrect answer"
# section and adding a corrected operative answer left the substring pin green over a
# file that no longer denies anything. Parsing the bullets pins which answer is
# OPERATIVE, and the label list pins that no second Race answer was added beside it.
_ANSWER_LABELS = [
    "Race / TOCTOU / atomicity",
    "Silent failure",
    "Vacuous witness",
    "Timeout / retry / budget arithmetic",
    "Env-var mutation and restore",
    "Subprocess boundary",
    "Path / default resolution",
]
_RACE_ANSWER = (
    "no new mechanism — this round only adds a skip to an existing loop, so there is "
    "no new coordination surface to sweep."
)


def _answer_bullets() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in _fixture("absorption/sweep-answers-r5.md").splitlines():
        match = re.match(r"- \*\*(.+?):\*\*\s*(.*)$", line)
        if match:
            out.append((match.group(1), match.group(2)))
        elif out and line.startswith("  ") and line.strip():
            label, text = out[-1]
            out[-1] = (label, f"{text} {line.strip()}")
    return out


# The race fixture is FROZEN EVIDENCE, pinned by content digest.
#
# This replaces three rounds of structural pinning, and the replacement is the point.
# A concurrency defect is a property of the WHOLE MODULE under threading, so every
# structural pin has an unpinned neighbour: r1 pinned claim()'s body and lost to a
# wrapper outside it; r2 pinned the class and lost to the instance; r3 pinned the
# instance and lost to `drain_all`, the one function that actually spawns the threads.
# Each fix was correct about its own scope and bought exactly one more round — the
# non-converging arms race this skill's own step 5 says to answer by SUBTRACTION.
#
# A digest has no unpinned neighbour: any edit anywhere in the file, at any scope, reds.
# It is also the honest shape for what this file IS. The fixture is not code under
# development; it is a recorded test vector that eval 16 was graded against, so it must
# not drift silently at all — changing it invalidates the grade in
# `.harness/u-sr-02-eval-run-2026-08-28.md`, which is exactly what this assertion says.
#
# Deliberately changing the fixture is a three-step act, not a digest bump: edit, re-run
# eval 16 and re-grade it, then update this digest and the recorded run together.
_RACE_FIXTURE_SHA256 = "f5c84559715beec4a717295939a03d6b95534d37e7d7bce10521de2ac6e13657"


def _check_registry_race() -> None:
    """The planted check-then-act race, pinned by freezing the artifact that carries it.

    See the note above the digest for why this is a digest and not a structural pin.
    """
    raw = (
        _repo_root() / EVALS_REL.parent / "fixtures-usr02" / "absorption" / "round5_fix.py"
    ).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    # The digest is bound to the RECORD, not just to this constant. Without this, the
    # three-step protocol below was documented in a comment and nothing enforced it:
    # repairing the race and bumping only `_RACE_FIXTURE_SHA256` left the suite green
    # while the recorded grade silently went stale (merge-gate witness lens r4). That is
    # the same prose-over-mechanism gap this file already closes for the ledger claims by
    # reading `unanswered_findings` and the permission guard's own source — applied here
    # too, instead of only where it was convenient.
    record = (_repo_root() / ".harness" / "u-sr-02-eval-run-2026-08-28.md").read_text(
        encoding="utf-8"
    )
    # Anchored to the record's OWN table cell, keyed by the fixture path — not a
    # whole-file substring. `in record` was the r4 draft and r5 refuted it: fixing the
    # race, bumping the constant, and dropping the new digest ANYWHERE in the ~150-line
    # record satisfied it while the actual row stayed stale. A cell-anchored read is the
    # difference between "this string occurs somewhere" and "the record says this".
    row = re.search(
        r"\|\s*`evals/fixtures-usr02/absorption/round5_fix\.py`\s*\|\s*`([0-9a-f]{64})`\s*\|",
        record,
    )
    assert row, (
        "the eval-run record no longer carries a digest row for the race fixture; the "
        "graded evidence and the fixture have come apart — re-grade eval 16 rather than "
        "syncing the strings"
    )
    assert row.group(1) == _RACE_FIXTURE_SHA256, (
        f"the record's digest row says {row.group(1)[:12]} but this constant says "
        f"{_RACE_FIXTURE_SHA256[:12]} — one was updated without the other"
    )
    assert actual == _RACE_FIXTURE_SHA256, (
        "the race fixture changed (sha256 "
        f"{actual[:12]} != {_RACE_FIXTURE_SHA256[:12]}). Eval 16 was graded against the "
        "recorded bytes, so this is not a digest to bump: re-run eval 16, re-grade it, and "
        "update this constant and .harness/u-sr-02-eval-run-2026-08-28.md together."
    )


def _check_answers_deny() -> None:
    bullets = _answer_bullets()
    assert [label for label, _ in bullets] == _ANSWER_LABELS, (
        "the answers' bullet set changed; a second or replaced answer may now be operative"
    )
    race = dict(bullets)["Race / TOCTOU / atomicity"]
    assert _squash(race) == _squash(_RACE_ANSWER), (
        "the OPERATIVE race answer no longer denies a new mechanism"
    )
    assert _squash(_DENIAL_CONCLUSION) in _squash(_fixture("absorption/sweep-answers-r5.md"))


def _guard_tests() -> list[str]:
    """Dumps of every `if` test in validate() — the EXECUTABLE bounds.

    Raw substring checks were the r4 draft and fell to the same comment trick as the
    race pin: correcting the guard to a contract-derived maximum while leaving
    `1 <= args.reps <= 99` in a historical comment kept them green (codex r5 P2).
    """
    validate = _one_function("reps_guard.py", "validate")
    return [_dump(s.test) for s in _nodes(validate, ast.If)]


def _admits(argv: list[str]) -> bool:
    """Does the guard, RUN, admit `argv`? Collecting matching `ast.If` nodes proved
    only that a bound was written (codex r6 P2): contract-correct guards followed by an
    early `return None` leave the planted ones unreachable with every dump assertion
    green. Running validate() proves the prohibited input is still ADMITTED."""
    guard = _load_fixture("reps_guard.py", "_usr02_reps_guard")
    return guard.validate(guard.build_parser().parse_args(argv)) is None


def _check_reps_range() -> None:
    assert _expr("not 1 <= args.reps <= 99") in _guard_tests()
    assert _admits(["--reps", "50"]), "reps=50 is no longer admitted; §3.2 breach gone"


def _check_round_bound() -> None:
    assert _expr("args.round is not None and args.round > 25") in _guard_tests()
    assert _admits(["--round", "20"]), "round=20 is no longer admitted; §3.1 breach gone"


def _check_lane_bound() -> None:
    assert _expr("args.lane_id is not None and len(args.lane_id) > 128") in _guard_tests()
    assert _admits(["--lane-id", "x" * 100]), "a 100-char lane id is no longer admitted"


def _check_reps_default() -> None:
    parser = _one_function("reps_guard.py", "build_parser")
    calls = [_dump(c) for c in _nodes(parser, ast.Call)]
    assert _expr('ap.add_argument("--reps", type=int, default=3)') in calls
    # the EFFECTIVE default (codex r6 P2): `ap.set_defaults(reps=1)` after the call
    # fixes the graded defect while the pinned call remains in the AST
    guard = _load_fixture("reps_guard.py", "_usr02_reps_guard")
    assert guard.build_parser().parse_args([]).reps == 3, (
        "the parser's effective --reps default is no longer the planted 3"
    )


def _check_refusal_code() -> None:
    admit = _one_function("reps_guard.py", "admit")
    # sorted by lineno, never by walk order: `ast.walk` is breadth-first, so it read
    # these as [0, 1, 1] and the sequence claim would have been about nothing
    returns = [
        s.value.value
        for s in sorted(_nodes(admit, ast.Return), key=lambda n: n.lineno)
        if isinstance(s.value, ast.Constant)  # type: ignore[union-attr]
    ]
    assert returns == _ADMIT_RETURNS, (
        f"admit() returns {returns}; the planted §3.3 refusal-code breach is gone"
    )
    guard = _load_fixture("reps_guard.py", "_usr02_reps_guard")
    # `--reps 0` refuses via validate()'s BOUNDS, so the outcome does not depend on
    # REVIEWER_PROBE. `--reps 50` was the first draft and refused via the probe-mode
    # branch instead, which returns 0 when REVIEWER_PROBE=1 — an ambient-env dependency
    # that would red this suite in a legitimate probe environment (codex r7 P2). The
    # variable is cleared as well, so neither branch can be reached by accident.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("REVIEWER_PROBE", None)
        assert guard.admit(["--reps", "0"]) == 1, (
            "admit() no longer refuses with 1; §3.3 names 3, which is the graded breach"
        )


def _check_uncited_bounds() -> None:
    # no bound in the guard cites its contract section — that absence IS the defect
    assert "§3" not in _fixture("reps_guard.py")


def _check_bare_hold() -> None:
    note = _fixture("hold_note.md")
    assert "Disposition: HELD" in note
    assert "U-XX-77" in note  # the deferral target the hold leans on
    # The note may DESCRIBE the finding freely ("a non-numeric value raises ...") —
    # what it must not do is claim a probe was landed. Word-bounded on purpose: a
    # bare `in` check reads "raises" as "raise" and reds on the description itself.
    assert re.search(r"\bprobe\b", note, re.I) is None, "hold is no longer bare"
    tree = _fixture_ast("drain_budget.py")
    # Coarse pins first — they catch the loud shapes cheaply — then the EXACT return
    # expression, which is what actually decides. The call set and the banned-node
    # list were both satisfiable while the budget was validated: `_budget()` returning
    # `{3: 3}[int(os.environ.get("DRAIN_RETRY_BUDGET", "3"))]` fails closed on zero,
    # negative, non-numeric and oversized values via KeyError while preserving the
    # substring, the Return-only body, the call names and the absence of
    # Assert/Raise/Compare/IfExp (codex r5 P2). Pinning the returned expression itself
    # leaves no room for a wrapper: any validation has to change what is returned.
    assert _call_names(tree) == _DRAIN_BUDGET_CALLS, "fixture's call set changed; hold not bare"
    assert not _nodes(tree, ast.Assert, ast.Raise, ast.Compare, ast.IfExp)
    budget = _one_function("drain_budget.py", "_budget")
    assert _body_shape(budget) == ["Return"]
    assert _dump(budget.body[-1]) == _stmt(
        'return int(os.environ.get("DRAIN_RETRY_BUDGET", "3"))'
    ), "_budget() no longer reads the env value bare; the hold is no longer unvalidated"
    # the env value flows straight into range() with nothing bounding or rejecting it
    drain = _one_function("drain_budget.py", "drain_with_retries")
    assert any(_dump(f.iter) == _expr("range(_budget())") for f in _nodes(drain, ast.For)), (
        "the budget no longer flows unbounded into range()"
    )
    # BEHAVIOURAL fail-open proof, which no static pin could give: a budget of 0 drains
    # NOTHING and raises NOTHING, so the caller reads success over an empty result.
    # This is what eval 18 demands, and it is what kills the `{3: 3}[int(...)]` dodge —
    # that variant fails closed with KeyError and would red here (codex r5 P2).
    mod = _load_fixture("drain_budget.py", "_usr02_drain_budget")
    with mock.patch.dict(os.environ, {"DRAIN_RETRY_BUDGET": "0"}):
        assert mod.drain_with_retries(lambda _row: None, ["a"]) == [], (
            "a zero budget no longer drains silently; the hold is no longer unvalidated"
        )


_PLANTED = (
    _Planted(
        "wr04-registry-race",
        16,
        "the _LiveGroups registry",
        _check_registry_race,
    ),
    _Planted(
        "wr04-answers-deny-the-mechanism",
        16,
        "no new coordination surface",
        _check_answers_deny,
    ),
    _Planted(
        "wr05-reps-range",
        17,
        "§3.2 ceiling of 3",
        _check_reps_range,
        (r"§3\.2[^§]*?never above \*\*(\d+)\*\*", "3"),
    ),
    _Planted(
        "wr05-round-budget",
        17,
        "§3.1 budget of 10 rounds",
        _check_round_bound,
        (r"§3\.1[^§]*?\*\*at most (\d+) review rounds\*\*", "10"),
    ),
    _Planted(
        "wr05-lane-id-length",
        17,
        "§3.4's 64",
        _check_lane_bound,
        (r"§3\.4[^§]*?\*\*at most (\d+) characters\*\*", "64"),
    ),
    _Planted(
        "wr05-reps-default",
        17,
        "§3.2's 'exactly one'",
        _check_reps_default,
        (r"§3\.2[^§]*?requests \*\*(exactly one)\*\*", "exactly one"),
    ),
    _Planted(
        "wr05-refusal-exit-code",
        17,
        "§3.3, which names 3 as the refusal code",
        _check_refusal_code,
        (r"§3\.3[^§]*?is \*\*(\d+)\*\*", "3"),
    ),
    _Planted(
        "wr05-bounds-cite-nothing",
        17,
        "cite the contract value it derives from",
        _check_uncited_bounds,
    ),
    _Planted(
        "wr06-bare-hold",
        18,
        "fail-closed probe",
        _check_bare_hold,
    ),
)


@pytest.mark.parametrize("planted", _PLANTED, ids=lambda p: p.what)
def test_planted_defect_is_pinned_on_every_side(planted: _Planted):
    """A planted-defect eval is only a witness while all three sides hold: the
    fixture still carries the defect, the contract clause still states the value the
    defect diverges from, and the oracle still demands it.

    Any one side alone is satisfiable without the other two — which is precisely how
    rounds 1, 2 and 3 each found a different green-but-vacuous case.
    """
    planted.check()
    if planted.clause is not None:
        pattern, expected = planted.clause
        match = re.search(pattern, _fixture("contract_excerpt.md"), re.S)
        assert match, f"contract clause for {planted.what} no longer states its value"
        assert match.group(1) == expected, (
            f"contract says {match.group(1)!r} but eval {planted.eval_id} "
            f"grades against {expected!r}"
        )
    oracle = " ".join(next(c for c in _evals() if c["id"] == planted.eval_id)["assertions"])
    assert planted.demand in oracle, f"eval {planted.eval_id} no longer demands {planted.what}"


# The complete row set, pinned by NAME. The eval-id set alone was the first draft and
# could not see a deletion: dropping `wr05-refusal-exit-code` left {16, 17, 18} and the
# uniqueness check untouched while that graded defect went unpinned entirely (codex r4
# P2). Removing a planted defect must now be a deliberate edit in two places — the
# table and this set — which is the point: a row cannot leave quietly.
_EXPECTED_ROWS = frozenset(
    {
        "wr04-registry-race",
        "wr04-answers-deny-the-mechanism",
        "wr05-reps-range",
        "wr05-round-budget",
        "wr05-lane-id-length",
        "wr05-reps-default",
        "wr05-refusal-exit-code",
        "wr05-bounds-cite-nothing",
        "wr06-bare-hold",
    }
)


def test_planted_table_covers_every_u_sr_02_eval():
    """The table is the single place a planted defect is declared, so it must span
    all three cases AND carry every declared row — a defect declared nowhere is a
    defect pinned on no side."""
    assert {p.eval_id for p in _PLANTED} == {16, 17, 18}
    names = [p.what for p in _PLANTED]
    assert len(names) == len(set(names)), f"duplicate row names: {names}"
    assert set(names) == _EXPECTED_ROWS, (
        f"planted-defect table drifted: missing {_EXPECTED_ROWS - set(names)}, "
        f"undeclared {set(names) - _EXPECTED_ROWS}"
    )


def test_preflight_carries_the_u_sr_02_meta_rules():
    """Charter WR-04/05/06 land as SKILL prose, so the pin is on the load-bearing
    tell of each rule rather than on surrounding wording that may be re-edited.

    The bullet count is pinned against the heading in the same assertion set: a
    fourth meta-rule added under a heading still reading "Three" is the class-2
    prose drift this skill's own list warns about.
    """
    text = _preflight_skill_text()
    # WR-04 — the tell, verbatim from the charter
    assert 'can never answer "no new mechanism"' in text
    # WR-05 — the rule, not merely the example
    assert "Every numeric bound names the contract value it derives from" in text
    # WR-06 — the same-round obligation, wired where holds are adjudicated
    assert "owes a fail-closed probe in the same round" in text

    block = text.split("Three meta-rules that outrank the list:", 1)[1].split("\n\n## ", 1)[0]
    assert len([ln for ln in block.splitlines() if ln.startswith("- **")]) == 3

    # WR-06's ledger half, which took four rounds to get right and is pinned at its
    # SETTLED form, not its history: r4 added `suppressed` to the absorber's vocabulary,
    # r5 ordered it after its probe, r6 recorded that the guard keeps it operator-visible,
    # and r8 (P1) established that the absorber must not write it at all. The r4/r5 pins
    # are deliberately GONE rather than kept alongside — a pin on superseded prose is a
    # second authority that would have to be argued with at every future edit.
    #
    # Kept from r6: the authority list, which is what makes the absorber ineligible.
    squashed = _squash(text)
    # Kept from r6/r7: the authority list, which is what makes the absorber ineligible
    # to sign a suppression at all.
    assert "a decorrelated lens, a deterministic rule, or a logged operator override" in squashed

    # The absorber writes only two dispositions (codex r8 P1). `suppressed` names its
    # actor as the ADJUDICATING AUTHORITY, and C-HE-24 §5 says that authority is a
    # decorrelated lens, a deterministic rule, or a logged operator override — never
    # the absorber. A row signed by the absorber therefore records an authority that
    # never existed, and readers reducing by finding_id see a settled mute where a
    # visible `disposition=null` used to be. Held findings stay UNDISPOSED.
    assert "--disposition accepted|rejected --actor <runner>_absorber" in squashed
    assert "suppressed|" not in squashed and "|suppressed" not in squashed, (
        "the absorber's documented disposition set admits `suppressed` again"
    )
    # A positive claim the doc must carry. NOT an exclusivity guarantee: r2 called this
    # "complete by construction", and r3 correctly refuted that — this assertion and the
    # negative above are independent substring checks, so prose added elsewhere granting
    # the absorber `suppressed` authority (phrased without an adjacent pipe) would leave
    # both green over a self-contradicting document. Whether a document says a thing
    # NOWHERE is not a substring property, so no pin closes it; a third spelling-ban would
    # only add a fourth. Registered as a named limit in the PR body instead of pretended
    # away here.
    assert "an absorber writes no third state" in squashed
    # r9 P1: the r8 rule (leave it null, name it in the sweep) lost the finding.
    # `unanswered_findings` subtracts every id an attestation names and never reads
    # disposition, so an attested null-disposition finding is retired permanently with
    # nothing recording that it was decided. Neither `suppressed` nor null is available,
    # so "held" is not a ledger state at all — the probe IS the disposition.
    assert '"Held" is not a ledger state, and reaching for one is the error.' in squashed
    assert "EVERY finding gets one of these two, always" in squashed
    assert "no finding may be attested past with `disposition=null`" in squashed
    # Bound to the gate's actual behaviour, not to a restatement of it: if
    # unanswered_findings ever starts consulting disposition, this reds and the prose
    # must be revisited.
    gate_src = (_repo_root() / "tools" / "review_loop_gate.py").read_text(encoding="utf-8")
    subtract = gate_src.split("def unanswered_findings", 1)[1].split("\ndef ", 1)[0]
    assert "sorted(all_ids - answered)" in subtract
    assert "disposition" not in subtract, (
        "unanswered_findings now reads disposition; the skill's step 4 rationale is stale"
    )

    # The guard venue (codex r6 P2), pinned against the GUARD's own source rather than
    # restated, so the two cannot drift apart: if the allowlist ever admits
    # `suppressed`, this reds and the prose must be revisited — the direction that
    # matters, since widening it would let an agent grant itself mute authority.
    assert (
        "permission guard's `accepted|rejected` allowlist (`_adjudicate_exact_shape`, "
        "U-HE-47)" in squashed
    )
    assert "never widen it to get past this moment" in squashed
    guard_src = (_repo_root() / "tools" / "hooks" / "permission-guard.sh").read_text(
        encoding="utf-8"
    )
    assert 'case "$6" in accepted|rejected) ;; *) return 1 ;; esac' in guard_src, (
        "the guard's disposition allowlist changed; the SKILL's headless claim must follow"
    )


# --- U-SR-03: laws:prompt durable wiring + bindings by file (charter WR-08/09) ----


def test_u_sr_03_eval_case_is_registered_and_its_fixture_still_carries_the_defects():
    """WR-08c: the regression case exists AND the fixture still plants what it grades.

    The registration half alone rots silently: an edit that tidied the fixture's prompts
    into the canonical three-lens shape, or repaired the truncated sha, would leave the
    case green-by-vacuity — it would elicit nothing, and no assertion in `assertions`
    could fire. Each pin below is one graded defect, so repairing the fixture reds here.
    """
    by_name = {c["eval_name"]: c for c in _evals()}
    case = by_name.get("freehand-lens-prompts-and-hand-copied-binding")
    assert case is not None, "U-SR-03 eval case removed"
    assert case["skill"] == "merge-gate", (
        "the case must load the carrier that holds the rule under test, or the A/B "
        "measures something other than the wiring this unit landed"
    )
    assert case["files"] == ["fixtures-usr03/gate_launch_notes.md"]

    fixture = (
        _repo_root() / EVALS_REL.parent / "fixtures-usr03" / "gate_launch_notes.md"
    ).read_text(encoding="utf-8")

    # Defect 1 -- authored freehand, inside a laws:code session.
    assert "laws:code is loaded" in fixture

    # Defect 2 -- a fourth lens: the departure from the canonical three-lens template
    # that makes this authoring rather than instantiation.
    assert len(re.findall(r"^## Lens", fixture, re.M)) == 4

    # Defect 3 -- the six values are hand-copied into the prompt text.
    assert "head_sha=" in fixture and "config_hash=" in fixture

    # Defect 4 -- and the copy is truncated: a 39-hex prefix of the 40-hex CI sha, the
    # exact round-3 corruption. Compared against the fixture's OWN real sha rather than a
    # literal, so the pin cannot pass by matching a stale constant.
    pasted = re.search(r"head_sha=([0-9a-f]+)", fixture).group(1)
    real = re.search(r"CI is green at `([0-9a-f]+)`", fixture).group(1)
    assert len(real) == 40 and len(pasted) == 39 and real.startswith(pasted)


def test_binding_publishes_by_file_and_the_recipe_routes_through_it():
    """WR-09: the tool writes the values to a file and the recipe is what skills call.

    Pinned against `merge_gate_log.py`'s own source: a revert to printing the JSON would
    red here as well as in test_merge_gate_log.py, and the SKILL prose that names a
    printed PATH would otherwise be describing a mechanism that no longer exists.
    """
    src = (_repo_root() / "tools" / "merge_gate_log.py").read_text(encoding="utf-8")
    binding_block = src.split("def binding_path", 1)[1].split("\ndef ", 1)[0]
    assert "merge-gate-binding-" in binding_block and "LENS_SCRATCH" in binding_block

    dispatch = src.split('if args.cmd == "binding":', 1)[1].split("\n    if args.cmd", 1)[0]
    # The mechanism as it actually ships, not as an earlier draft wrote it: a temp opened
    # O_EXCL|O_NOFOLLOW, published by os.replace, with the PATH as the only thing printed.
    # (codex r1 P2 caught this pin naming `out.write_text`, which the class-1 fix had
    # already replaced -- a stale source pin is red in CI and green in `just codex-check`,
    # which does not run this file; only .github/workflows/ci.yml does.)
    # Every step is dir_fd-relative to the descriptor `open_scratch_dir()` captured, so no
    # component can be repointed by a rename after the check (codex r2 P2), and the name is
    # content-addressed so a republish under different values cannot land on it (r2 P1).
    for token in (
        "open_scratch_dir()",
        "os.O_EXCL",
        "os.O_NOFOLLOW",
        "dir_fd=dfd",
        "src_dir_fd=dfd, dst_dir_fd=dfd",
        "print(out)",
    ):
        assert token in dispatch, f"binding dispatch no longer contains {token}"
    opener = src.split("def open_scratch_dir", 1)[1].split("\ndef ", 1)[0]
    assert "os.O_DIRECTORY" in opener and "os.O_NOFOLLOW" in opener, (
        "the scratch capture is no longer an atomic symlink-refusing directory open"
    )
    # Every component below the anchor, not just the last one: `O_NOFOLLOW` on `tmp` alone
    # left `.harness` swappable for a symlink containing a real `tmp` (codex r3 P2).
    assert "SCRATCH_ANCHOR" in opener and "for part in parts" in opener, (
        "the scratch capture no longer validates every component below the anchor"
    )
    addressing = src.split("def binding_path", 1)[1].split("\ndef ", 1)[0]
    assert "hashlib.sha256" in addressing and "sort_keys=True" in addressing, (
        "the published name is no longer a digest of the whole binding"
    )
    assert "json.dumps" not in dispatch.split("print(out)", 1)[1], (
        "something is printed after the path; hand-copying is back on the table"
    )

    recipe = (_repo_root() / "justfile").read_text(encoding="utf-8")
    assert "merge_gate_log.py binding --lens {{lens}} --base {{base}}" in recipe


# ── U-SR-04 witnesses: the WR-10 flow + WR-11 rule live at their carriers ────


def test_carriers_document_the_template_first_attest_flow():
    """WR-10's lever is only real if both loop carriers route authors through the
    template verbs (labels before answers) — a reverted carrier silently restores
    the attest-by-trial flow while the code side stays green."""
    for rel in (PREFLIGHT_SKILL_REL, Path(".claude/skills/ship-pr/SKILL.md")):
        # squashed: the carriers hard-wrap prose, so verb and argument may split lines
        text = _squash((_repo_root() / rel).read_text(encoding="utf-8"))
        assert "review-template-preflight <answers-file>" in text, rel
        # sweep-template must be spelled WITH the arc/lane prefix (codex u-sr-04 r1
        # P2: a bare invocation queries the branch-* fallback arc's obligations,
        # finds none, and declines — the documented command must be the working one)
        assert (
            "HARNESS_ARC_ID=<arc-id> HARNESS_LANE_ID=<lane-id> "
            "just review-template-sweep <answers-file>"
        ) in text, rel
        # order pin: the template step is documented BEFORE its attest verb, per verb
        for verb in ("preflight", "sweep"):
            assert text.index(f"review-template-{verb}") < text.index(f"review-attest-{verb}"), (
                f"{rel}: template-{verb} documented after attest-{verb}"
            )
    recipe = (_repo_root() / "justfile").read_text(encoding="utf-8")
    for verb in ("preflight", "sweep"):
        assert f"review_loop_gate.py template-{verb} --answers" in recipe


def test_preflight_carries_the_u_sr_04_mechanism_precedent_rule():
    """Charter WR-11 lands as grounding-time SKILL prose; pin the load-bearing tells
    of each of the three steps (sibling-first, corpus grep, skeleton-is-unreviewed),
    not the surrounding wording."""
    text = _preflight_skill_text()
    assert "## The mechanism-precedent search" in text
    section = _squash(text.split("## The mechanism-precedent search", 1)[1].split("\n## ", 1)[0])
    assert "adopt its shape or import it outright" in section
    assert ".harness/merge-gate-log.jsonl" in section
    assert "plan skeleton as UNREVIEWED input" in section
