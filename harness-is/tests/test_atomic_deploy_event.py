"""Tests for U-IS-06 — atomic deploy-event composition + verification (C-IS-04 §4).

Test set per the U-IS-06 `Tests:` field — covers acceptance #1-#6. Layered:
fast pure-core tests over synthetic data + git-shell-out integration tests over
real `tmp_path` repositories.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness_core import ContractID
from harness_is.atomic_deploy_event import (
    ATOMIC_DEPLOY_EVENT_COMPOSITION,
    CommitId,
    CommitRange,
    DeployArtifactClass,
    GitRepository,
    ViolationType,
    classify_path,
    find_split_deploy_violations,
    verify_deploy_atomicity,
)

# --- acceptance #1 — DeployArtifactClass ------------------------------------


def test_deploy_artifact_class_completeness() -> None:
    """Acceptance #1 — DeployArtifactClass declares exactly 4 values."""
    assert len(DeployArtifactClass) == 4
    assert {c.name for c in DeployArtifactClass} == {
        "PROMPTS",
        "CODE",
        "EVAL_SETS",
        "ROUTING_MANIFEST",
    }


# --- acceptance #2 — composition declaration --------------------------------


def test_verify_composes_with_commit_stream() -> None:
    """Acceptance #2 — the canonical composition composes with the C-IS-03
    commit-stream sub-role and C-IS-08."""
    assert ContractID("C-IS-03") in ATOMIC_DEPLOY_EVENT_COMPOSITION.composes_with
    assert ContractID("C-IS-08") in ATOMIC_DEPLOY_EVENT_COMPOSITION.composes_with
    assert ATOMIC_DEPLOY_EVENT_COMPOSITION.artifact_classes == frozenset(DeployArtifactClass)


# --- pure-core tests (fast; no git) -----------------------------------------


def test_classify_path_by_directory_convention() -> None:
    """`classify_path` maps top-level directories to artifact classes."""
    assert classify_path("prompts/system.txt") is DeployArtifactClass.PROMPTS
    assert classify_path("routing/manifest.json") is DeployArtifactClass.ROUTING_MANIFEST
    assert classify_path("evals/case_1.jsonl") is DeployArtifactClass.EVAL_SETS
    assert classify_path("src/workflow.py") is DeployArtifactClass.CODE


def test_find_split_deploy_violations_well_formed() -> None:
    """A range whose deploy is contained in one commit has no violations."""
    commits = [
        (CommitId(sha="aaa"), frozenset({DeployArtifactClass.PROMPTS, DeployArtifactClass.CODE})),
    ]
    assert find_split_deploy_violations(commits) == ()


def test_find_split_deploy_violations_split() -> None:
    """>=2 commits each touching a proper subset of the total ⇒ SPLIT_DEPLOY."""
    commits = [
        (CommitId(sha="aaa"), frozenset({DeployArtifactClass.PROMPTS})),
        (CommitId(sha="bbb"), frozenset({DeployArtifactClass.CODE})),
    ]
    violations = find_split_deploy_violations(commits)
    assert len(violations) == 1
    assert violations[0].violation_type is ViolationType.SPLIT_DEPLOY
    assert {c.sha for c in violations[0].commit_ids} == {"aaa", "bbb"}


# --- integration tests (real git repo via shell-out) ------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@harness.local")
    _git(repo, "config", "user.name", "harness-test")
    # Base commit so the first deploy commit always has a parent.
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")


def _commit(repo: Path, message: str, files: dict[str, str]) -> CommitId:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return CommitId(sha=_git(repo, "rev-parse", "HEAD"))


def test_verify_well_formed_commits_returns_no_violations(tmp_path: Path) -> None:
    """Acceptance #3 — a single-commit atomic deploy returns no violations."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    deploy = _commit(
        repo,
        "atomic deploy",
        {
            "prompts/system.txt": "p",
            "src/workflow.py": "c",
            "evals/case.jsonl": "e",
            "routing/manifest.json": "{}",
        },
    )
    report = verify_deploy_atomicity(
        GitRepository(repository_root=repo), CommitRange(from_commit=deploy, to_commit=deploy)
    )
    assert report.violations == ()
    assert report.bisection_isolated is False
    assert report.commits_inspected == 1


def test_verify_split_deploy_returns_violation(tmp_path: Path) -> None:
    """Acceptance #4 — a deploy split across commits returns a SPLIT_DEPLOY
    violation naming the relevant commit IDs."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    prompts_commit = _commit(repo, "deploy prompts", {"prompts/system.txt": "p"})
    code_commit = _commit(repo, "deploy code", {"src/workflow.py": "c"})
    report = verify_deploy_atomicity(
        GitRepository(repository_root=repo),
        CommitRange(from_commit=prompts_commit, to_commit=code_commit),
    )
    assert len(report.violations) == 1
    assert report.violations[0].violation_type is ViolationType.SPLIT_DEPLOY
    assert {c.sha for c in report.violations[0].commit_ids} == {
        prompts_commit.sha,
        code_commit.sha,
    }


def test_verify_bisection_isolates_violating_commit(tmp_path: Path) -> None:
    """Acceptance #6 — a violation in the range is isolated to specific commit
    IDs (enabling O(log N) git-bisect regression isolation downstream)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    prompts_commit = _commit(repo, "deploy prompts", {"prompts/system.txt": "p"})
    code_commit = _commit(repo, "deploy code", {"src/workflow.py": "c"})
    report = verify_deploy_atomicity(
        GitRepository(repository_root=repo),
        CommitRange(from_commit=prompts_commit, to_commit=code_commit),
    )
    assert report.bisection_isolated is True
    assert all(v.commit_ids for v in report.violations)
