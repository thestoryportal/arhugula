#!/usr/bin/env python3
"""Initialize local operator config for a portable Arhugula checkout."""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_ROOT = "/absolute/path/to/your/workspace"
LOCAL_DIRS = (
    ".harness",
    "skills",
    "prompts",
    "routing_manifest",
)


@dataclass(frozen=True)
class InitResult:
    created: tuple[str, ...]
    preserved: tuple[str, ...]


def _touch_gitkeep(directory: Path) -> None:
    marker = directory / ".gitkeep"
    if not marker.exists():
        marker.write_text("", encoding="utf-8")


def _copy_text_template(
    source: Path,
    target: Path,
    *,
    replacements: dict[str, str] | None = None,
    force: bool = False,
) -> bool:
    if target.exists() and not force:
        return False

    content = source.read_text(encoding="utf-8")
    for old, new in (replacements or {}).items():
        content = content.replace(old, new)
    target.write_text(content, encoding="utf-8")
    return True


def initialize(root: Path = ROOT, *, force: bool = False) -> InitResult:
    created: list[str] = []
    preserved: list[str] = []

    for relative in LOCAL_DIRS:
        directory = root / relative
        if directory.exists():
            preserved.append(relative)
        else:
            directory.mkdir(parents=True)
            created.append(relative)
        _touch_gitkeep(directory)

    config_target = root / "harness.toml"
    if _copy_text_template(
        root / "harness.toml.example",
        config_target,
        replacements={PLACEHOLDER_ROOT: root.as_posix()},
        force=force,
    ):
        created.append("harness.toml")
    else:
        preserved.append("harness.toml")

    env_target = root / ".env"
    if not env_target.exists():
        shutil.copyfile(root / ".env.example", env_target)
        created.append(".env")
    else:
        preserved.append(".env")

    return InitResult(created=tuple(created), preserved=tuple(preserved))


def _render(result: InitResult) -> str:
    lines = ["Portable Arhugula local initialization complete."]
    if result.created:
        lines.append("created:")
        lines.extend(f"- {path}" for path in result.created)
    if result.preserved:
        lines.append("preserved:")
        lines.extend(f"- {path}" for path in result.preserved)
    lines.extend(
        [
            "next:",
            "- authenticate at least one local CLI: claude, codex, or agy",
            "- optionally set API keys for secondary SDK fallback",
            "- run: uv sync --all-packages",
            "- run: just run examples/minimal.toml",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="checkout root to initialize; defaults to this repository",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite harness.toml; .env is never overwritten",
    )
    args = parser.parse_args(argv)

    print(_render(initialize(args.root.resolve(), force=args.force)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
