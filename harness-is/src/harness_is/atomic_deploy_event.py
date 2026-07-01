"""Atomic deploy-event composition contract + verification primitive — U-IS-06.

Implements C-IS-04 §4 (the atomic prompt + code + eval + manifest deploy
contract). Declares the deploy-event composition types and
`verify_deploy_atomicity` — an offline, on-demand verification primitive that
inspects a git commit range for split-deploy violations.

**Implementation-discretion note (C-IS-04 §4).** C-IS-04 §4 is a *surface
contract*: it commits the atomicity property and the verification surface
("git log inspection ... bisection ... isolates regression boundaries") but
defers "commit-message-driven deploy-event annotation conventions" and the
verification algorithm to implementation discretion. The operational
split-deploy criterion adopted here, per that deferral:

  > A SPLIT_DEPLOY violation exists within the inspected commit range when
  > >=2 commits each touch a non-empty *proper subset* of the range's total
  > touched deploy-artifact-class set — i.e. the deploy was spread across
  > commits rather than applied atomically in one. A range whose deploy
  > artifact changes are contained in a single commit is well-formed.

Path-to-artifact-class classification is by top-level directory convention
(`prompts/`, `routing/`, `evals/`, else `CODE`) — also §4-deferred discretion.
`MISSING_COMMIT_STREAM_ENTRY` is a declared `ViolationType` reserved for the
C-IS-03 commit-stream composition; the range-only verification primitive here
emits `SPLIT_DEPLOY` only.

Git is accessed by shelling out to the `git` CLI (Meta-Architecture shell-out
substitution; no Python git library — `CLAUDE.md` §3.2 framework-pull
discipline). The git-domain types `GitRepository` / `CommitRange` / `CommitId`
are IS-internal harness abstractions declared in-unit (Q-R2-1 operator
decision).

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.2 U-IS-06
(REVISED — R2); Spec_Information_Substrate_v1.md C-IS-04 §4; ADR-F2 v1.2
§Consequences (a).
"""

from __future__ import annotations

import subprocess
from enum import StrEnum
from pathlib import Path

from harness_core import ContractID
from pydantic import BaseModel, ConfigDict


class DeployArtifactClass(StrEnum):
    """The 4 co-located deploy artifact classes (C-IS-04 §4, verbatim)."""

    PROMPTS = "prompts"
    CODE = "code"
    EVAL_SETS = "eval_sets"
    ROUTING_MANIFEST = "routing_manifest"


class AtomicityProperty(StrEnum):
    """The deploy atomicity property (C-IS-04 §4)."""

    ALL_OR_NOTHING_PER_COMMIT = "all_or_nothing_per_commit"


class ObservabilityProperty(StrEnum):
    """The deploy observability property (C-IS-04 §4)."""

    SINGLE_VERSION_OBSERVABILITY = "single_version_observability"


class ViolationType(StrEnum):
    """Deploy-atomicity violation classes (C-IS-04 §4).

    `MISSING_COMMIT_STREAM_ENTRY` is declared for the C-IS-03 commit-stream
    composition; it is not emitted by the range-only verification primitive.
    """

    SPLIT_DEPLOY = "split_deploy"
    MISSING_COMMIT_STREAM_ENTRY = "missing_commit_stream_entry"


class DeployEventComposition(BaseModel):
    """The composition contract of an atomic deploy event (C-IS-04 §4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_classes: frozenset[DeployArtifactClass]
    atomicity_property: AtomicityProperty
    observability_property: ObservabilityProperty
    composes_with: frozenset[ContractID]


#: The canonical deploy-event composition — all 4 artifact classes; composes
#: with the C-IS-03 commit-stream sub-role and C-IS-08 shadow-Git (acceptance #2).
ATOMIC_DEPLOY_EVENT_COMPOSITION: DeployEventComposition = DeployEventComposition(
    artifact_classes=frozenset(DeployArtifactClass),
    atomicity_property=AtomicityProperty.ALL_OR_NOTHING_PER_COMMIT,
    observability_property=ObservabilityProperty.SINGLE_VERSION_OBSERVABILITY,
    composes_with=frozenset({ContractID("C-IS-03"), ContractID("C-IS-08")}),
)


# --- IS-internal git-domain types (Q-R2-1 operator decision) ----------------


class CommitId(BaseModel):
    """A git object SHA — a thin IS-internal git-domain type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str


class GitRepository(BaseModel):
    """A git repository — a thin IS-internal git-domain type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_root: Path


class CommitRange(BaseModel):
    """An inclusive git commit range — a thin IS-internal git-domain type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_commit: CommitId
    to_commit: CommitId


# --- verification result types ----------------------------------------------


class DeployAtomicityViolation(BaseModel):
    """One deploy-atomicity violation found in a commit range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    violation_type: ViolationType
    commit_ids: tuple[CommitId, ...]
    description: str


class DeployAtomicityVerificationReport(BaseModel):
    """The outcome of a `verify_deploy_atomicity` inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commits_inspected: int
    violations: tuple[DeployAtomicityViolation, ...]
    bisection_isolated: bool


# --- pure core (fast-testable; no git, no I/O) ------------------------------


def classify_path(path: str) -> DeployArtifactClass:
    """Classify a changed-file path into a `DeployArtifactClass`.

    Top-level-directory convention (C-IS-04 §4-deferred discretion):
    `prompts/` → PROMPTS, `routing/` → ROUTING_MANIFEST, `evals/` → EVAL_SETS,
    everything else → CODE.
    """
    head = path.split("/", 1)[0]
    if head == "prompts":
        return DeployArtifactClass.PROMPTS
    if head == "routing":
        return DeployArtifactClass.ROUTING_MANIFEST
    if head == "evals":
        return DeployArtifactClass.EVAL_SETS
    return DeployArtifactClass.CODE


def find_split_deploy_violations(
    commits: list[tuple[CommitId, frozenset[DeployArtifactClass]]],
) -> tuple[DeployAtomicityViolation, ...]:
    """Pure split-deploy detection over an ordered `(commit, touched-classes)` list.

    A SPLIT_DEPLOY violation exists when >=2 commits each touch a non-empty
    proper subset of the range's total touched artifact-class set (the deploy
    was spread across commits). A range contained in one commit is well-formed.
    """
    total: frozenset[DeployArtifactClass] = frozenset(
        cls for _, classes in commits for cls in classes
    )
    if not total:
        return ()
    proper_subset_commits = [
        commit_id for commit_id, classes in commits if classes and classes < total
    ]
    if len(proper_subset_commits) >= 2:
        return (
            DeployAtomicityViolation(
                violation_type=ViolationType.SPLIT_DEPLOY,
                commit_ids=tuple(proper_subset_commits),
                description=(
                    f"deploy split across {len(proper_subset_commits)} commits — "
                    f"artifact classes {sorted(c.value for c in total)} were not "
                    "applied in a single atomic commit (C-IS-04 §4)"
                ),
            ),
        )
    return ()


# --- git shell-out + verification primitive ---------------------------------


def _git(repo: GitRepository, *args: str) -> str:
    """Run a `git` command in `repo`, returning stdout (shell-out substitution)."""
    result = subprocess.run(
        ["git", "-C", str(repo.repository_root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _commits_in_range(
    repo: GitRepository, commit_range: CommitRange
) -> list[tuple[CommitId, frozenset[DeployArtifactClass]]]:
    """Collect `(commit, touched-artifact-classes)` for each commit in the
    inclusive range, oldest-first (git shell-out).

    Inclusive [from..to] is built as `from_commit` prepended to the exclusive
    `from..to` rev-list — avoiding a `from^` reference so a root `from_commit`
    is handled.
    """
    rev_spec = f"{commit_range.from_commit.sha}..{commit_range.to_commit.sha}"
    after_from = [s for s in _git(repo, "rev-list", "--reverse", rev_spec).splitlines() if s]
    shas = [commit_range.from_commit.sha, *after_from]
    collected: list[tuple[CommitId, frozenset[DeployArtifactClass]]] = []
    for sha in shas:
        names = _git(repo, "show", "--name-only", "--format=", sha).splitlines()
        classes = frozenset(classify_path(n) for n in names if n.strip())
        collected.append((CommitId(sha=sha), classes))
    return collected


def verify_deploy_atomicity(
    git_repository: GitRepository,
    commit_range: CommitRange,
) -> DeployAtomicityVerificationReport:
    """Verify that a commit range applies its deploy atomically (C-IS-04 §4).

    Offline / on-demand — inspects git history; does not block deploy commits
    at write-time (acceptance #5). Returns a report listing any SPLIT_DEPLOY
    violations with the relevant commit IDs. `bisection_isolated` is `True`
    when violations were pinpointed to specific commits — enabling O(log N)
    `git bisect`-style regression isolation downstream (acceptance #6).
    """
    commits = _commits_in_range(git_repository, commit_range)
    violations = find_split_deploy_violations(commits)
    bisection_isolated = bool(violations) and all(v.commit_ids for v in violations)
    return DeployAtomicityVerificationReport(
        commits_inspected=len(commits),
        violations=violations,
        bisection_isolated=bisection_isolated,
    )
