"""U-RT-13 — `load_skills_from_dir` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L3:
- All skills under PathClass.SKILLS loaded.
- Duplicate IDs rejected.
- Manifest schema enforced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness_core import SkillID
from harness_runtime.lifecycle.skills import (
    DuplicateSkillError,
    Skill,
    SkillManifest,
    load_skills_from_dir,
)
from pydantic import ValidationError


def _write_manifest(skills_dir: Path, skill_id: str, **overrides: object) -> Path:
    """Write a valid skill manifest to disk; return the file path."""
    skills_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "skill_id": skill_id,
        "name": f"Skill {skill_id}",
        "description": "test",
        "version": "1.0",
        **overrides,
    }
    path = skills_dir / f"{skill_id}.skill.json"
    path.write_text(json.dumps(manifest))
    return path


def test_empty_dir_returns_empty_dict(tmp_path: Path) -> None:
    """An empty PathClass.SKILLS dir → empty skills dict."""
    (tmp_path / "skills").mkdir()
    assert load_skills_from_dir(tmp_path / "skills") == {}


def test_absent_dir_returns_empty_dict(tmp_path: Path) -> None:
    """A missing PathClass.SKILLS dir → empty skills dict (no crash)."""
    assert load_skills_from_dir(tmp_path / "skills") == {}


def test_single_skill_loaded(tmp_path: Path) -> None:
    """One manifest file → one Skill entry indexed by skill_id."""
    _write_manifest(tmp_path / "skills", "my-skill")
    skills = load_skills_from_dir(tmp_path / "skills")
    assert len(skills) == 1
    skill = skills[SkillID("my-skill")]
    assert isinstance(skill, Skill)
    assert skill.manifest.skill_id == "my-skill"
    assert skill.manifest.name == "Skill my-skill"
    assert skill.manifest.version == "1.0"


def test_multiple_skills_loaded(tmp_path: Path) -> None:
    """Multiple manifests → multiple Skill entries (plan AC: all loaded)."""
    for skill_id in ["alpha", "beta", "gamma"]:
        _write_manifest(tmp_path / "skills", skill_id)
    skills = load_skills_from_dir(tmp_path / "skills")
    assert set(skills.keys()) == {SkillID("alpha"), SkillID("beta"), SkillID("gamma")}


def test_duplicate_skill_id_rejected(tmp_path: Path) -> None:
    """Two manifests with the same skill_id → `DuplicateSkillError`."""
    # Two files, both declaring skill_id='same' but with different filenames.
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "first.skill.json").write_text(
        json.dumps(
            {
                "skill_id": "same",
                "name": "First",
                "description": "x",
                "version": "1.0",
            },
        ),
    )
    (skills_dir / "second.skill.json").write_text(
        json.dumps(
            {
                "skill_id": "same",
                "name": "Second",
                "description": "y",
                "version": "1.0",
            },
        ),
    )
    with pytest.raises(DuplicateSkillError) as exc_info:
        load_skills_from_dir(skills_dir)
    assert exc_info.value.skill_id == SkillID("same")


def test_manifest_missing_field_rejected(tmp_path: Path) -> None:
    """Manifest missing a required field → `ValidationError`."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "broken.skill.json").write_text(
        json.dumps({"skill_id": "broken", "name": "x"}),  # no description / version
    )
    with pytest.raises(ValidationError):
        load_skills_from_dir(skills_dir)


def test_manifest_extra_field_rejected(tmp_path: Path) -> None:
    """`extra='forbid'` — unknown manifest fields raise `ValidationError`."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "extra.skill.json").write_text(
        json.dumps(
            {
                "skill_id": "extra",
                "name": "x",
                "description": "y",
                "version": "1.0",
                "unknown_field": "boom",
            },
        ),
    )
    with pytest.raises(ValidationError):
        load_skills_from_dir(skills_dir)


def test_non_skill_files_ignored(tmp_path: Path) -> None:
    """Files not matching `*.skill.json` are ignored."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_manifest(skills_dir, "real")
    (skills_dir / "README.md").write_text("not a skill")
    (skills_dir / "config.json").write_text('{"not": "a skill"}')
    skills = load_skills_from_dir(skills_dir)
    assert set(skills.keys()) == {SkillID("real")}


def test_skill_carries_source_path(tmp_path: Path) -> None:
    """`Skill.source_path` records the file the manifest came from."""
    manifest_path = _write_manifest(tmp_path / "skills", "traced")
    skills = load_skills_from_dir(tmp_path / "skills")
    assert skills[SkillID("traced")].source_path == manifest_path


def test_skill_is_frozen(tmp_path: Path) -> None:
    """`Skill` is a frozen dataclass."""
    _write_manifest(tmp_path / "skills", "frozen-test")
    skill = load_skills_from_dir(tmp_path / "skills")[SkillID("frozen-test")]
    with pytest.raises((AttributeError, Exception)):
        skill.source_path = Path("/")  # type: ignore[misc]


def test_skill_manifest_is_frozen() -> None:
    """`SkillManifest` is a frozen Pydantic model."""
    assert SkillManifest.model_config.get("frozen") is True
