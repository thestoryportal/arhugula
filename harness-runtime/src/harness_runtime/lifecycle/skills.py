"""U-RT-13 — Skills filesystem load (extended at v1.32 / U-RT-99 plan v2.28).

Per ``Spec_Harness_Runtime_v1.md`` v1.32 §4 (C-RT-04 ``skills`` field:
``dict[SkillID, Skill]``) + §14.17.5 invariant 7 (``version_sha`` +
``body_tokens`` computed at load).

The original ``SkillManifest`` at v1.1 carried 4 fields (``skill_id`` / ``name``
/ ``description`` / ``version``). v1.32 extends with 2 new fields:

* ``version_sha`` — git content hash (byte-exact identical to
  ``git hash-object <path>`` output) per AS spec v1.7 §14.4 + §14.17.7
  deferred-discretion default mechanism.
* ``body_tokens`` — non-negative integer estimate via ``len(body) // 4``
  per §14.17.7 default heuristic.

Loaded at ``load_skills_from_dir`` invocation; computed per-manifest as the
file is read.

Stored on disk as ``<skill_id>.skill.json`` under ``PATH_CLASS_REGISTRY[SKILLS]``.
For v1.32: the manifest JSON MAY include the new fields (operator-supplied)
OR omit them (computed at load — recommended). When omitted, ``version_sha``
is computed from the manifest file content + ``body_tokens`` is computed from
``description`` length as a body-substitute proxy at MVP.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from harness_core import SkillID
from pydantic import BaseModel, ConfigDict

__all__ = [
    "DuplicateSkillError",
    "Skill",
    "SkillManifest",
    "compute_git_blob_sha",
    "load_skills_from_dir",
]


class SkillManifest(BaseModel):
    """Runtime-defined skill manifest schema.

    Extended at v1.32 with ``version_sha`` + ``body_tokens`` fields per
    runtime spec §14.17.5 invariant 7 — both computed at load when not
    supplied in the manifest JSON.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: SkillID
    name: str
    description: str
    version: str
    # v1.32 extensions (computed at load when absent in manifest JSON).
    version_sha: str
    body_tokens: int


@dataclass(frozen=True)
class Skill:
    """A loaded skill — manifest + the filesystem path it loaded from."""

    manifest: SkillManifest
    source_path: Path


class DuplicateSkillError(ValueError):
    """Raised when two manifest files declare the same `skill_id`."""

    def __init__(self, skill_id: SkillID, paths: tuple[Path, Path]) -> None:
        super().__init__(
            f"duplicate skill_id {skill_id!r}: {paths[0]} and {paths[1]}",
        )
        self.skill_id = skill_id
        self.paths = paths


def compute_git_blob_sha(content: bytes) -> str:
    """Compute the git blob SHA-1 hash for ``content``.

    Byte-exact identical to ``git hash-object <file>`` output. Per spec
    §14.17.7 deferred-discretion default mechanism (canonical git-blob
    byte sequence: ``b"blob " + len + b"\\0" + content`` SHA-1'd).
    """
    header = f"blob {len(content)}".encode("ascii") + b"\0"
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def load_skills_from_dir(skills_dir: Path) -> dict[SkillID, Skill]:
    """Load all ``*.skill.json`` manifests under ``skills_dir``.

    At v1.32: computes ``version_sha`` + ``body_tokens`` at load when absent
    in the manifest JSON, per §14.17.5 invariant 7 + §14.17.7 default
    mechanism heuristics.

    Parameters
    ----------
    skills_dir :
        Resolved PathClass.SKILLS directory from U-RT-10.

    Returns
    -------
    dict[SkillID, Skill]
        Indexed by skill_id; iteration order is filesystem-glob order.

    Raises
    ------
    DuplicateSkillError
        Two manifests declare the same ``skill_id``.
    pydantic.ValidationError
        A manifest file fails schema validation.
    """
    skills: dict[SkillID, Skill] = {}
    path_index: dict[SkillID, Path] = {}

    for manifest_path in sorted(_walk_manifests(skills_dir)):
        content_bytes = manifest_path.read_bytes()
        raw = content_bytes.decode("utf-8")
        # Parse the raw JSON to a dict so we can inject computed defaults
        # before Pydantic validation rejects missing required fields.
        import json

        data = json.loads(raw)
        if "version_sha" not in data:
            data["version_sha"] = compute_git_blob_sha(content_bytes)
        if "body_tokens" not in data:
            # MVP heuristic per §14.17.7: rough estimate via description
            # length // 4. The manifest body itself is the description at
            # current SkillManifest shape; full SKILL.md body is a future
            # spec extension at FM-2 follow-on per §14.17.7.
            description = data.get("description", "")
            data["body_tokens"] = max(0, len(description) // 4)
        manifest = SkillManifest.model_validate(data)
        prior = path_index.get(manifest.skill_id)
        if prior is not None:
            raise DuplicateSkillError(manifest.skill_id, (prior, manifest_path))
        path_index[manifest.skill_id] = manifest_path
        skills[manifest.skill_id] = Skill(manifest=manifest, source_path=manifest_path)

    return skills


def _walk_manifests(skills_dir: Path) -> Iterable[Path]:
    """Yield `*.skill.json` files under `skills_dir` (non-recursive)."""
    if not skills_dir.exists():
        return
    yield from skills_dir.glob("*.skill.json")
