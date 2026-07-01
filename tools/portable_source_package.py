#!/usr/bin/env python3
"""Build a clean portable Arhugula source repository from an allowlist."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "packaging" / "portable-source.toml"
IGNORED_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".pyright",
        ".mypy_cache",
        ".venv",
        ".git",
        "dist",
        "build",
        "htmlcov",
    }
)


@dataclass(frozen=True)
class PortableManifest:
    include_paths: tuple[Path, ...]
    overlay_files: tuple[tuple[Path, Path], ...]
    scaffold_dirs: tuple[Path, ...]
    required_target_paths: tuple[Path, ...]
    forbidden_target_paths: tuple[Path, ...]
    forbidden_names: tuple[str, ...]


@dataclass(frozen=True)
class PackageReport:
    ready: bool
    target: str
    copied_paths: tuple[str, ...]
    overlay_files: tuple[str, ...]
    scaffold_dirs: tuple[str, ...]
    missing_required: tuple[str, ...]
    forbidden_present: tuple[str, ...]


class PortablePackageError(RuntimeError):
    """Raised when the portable package cannot be built safely."""


def _as_path_tuple(values: object, *, field: str) -> tuple[Path, ...]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise PortablePackageError(f"{field} must be a list of strings")
    return tuple(Path(cast(str, value)) for value in values)


def _as_name_tuple(values: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise PortablePackageError(f"{field} must be a list of strings")
    names = tuple(cast(str, value) for value in values)
    invalid = tuple(name for name in names if not name or Path(name).parts != (name,))
    if invalid:
        raise PortablePackageError(f"{field} entries must be plain file or directory names")
    return names


def _load_manifest(path: Path) -> PortableManifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    section = cast(dict[str, Any], data.get("portable_source", {}))
    if not section:
        raise PortablePackageError("manifest missing [portable_source]")

    overlays_raw = section.get("overlay_files", [])
    if not isinstance(overlays_raw, list):
        raise PortablePackageError("overlay_files must be a list")
    overlays: list[tuple[Path, Path]] = []
    for item in overlays_raw:
        if not isinstance(item, dict):
            raise PortablePackageError("overlay_files entries must be tables")
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise PortablePackageError("overlay_files entries require source and target")
        overlays.append((Path(source), Path(target)))

    return PortableManifest(
        include_paths=_as_path_tuple(section.get("include_paths"), field="include_paths"),
        overlay_files=tuple(overlays),
        scaffold_dirs=_as_path_tuple(section.get("scaffold_dirs", []), field="scaffold_dirs"),
        required_target_paths=_as_path_tuple(
            section.get("required_target_paths", []),
            field="required_target_paths",
        ),
        forbidden_target_paths=_as_path_tuple(
            section.get("forbidden_target_paths", []),
            field="forbidden_target_paths",
        ),
        forbidden_names=_as_name_tuple(section.get("forbidden_names", []), field="forbidden_names"),
    )


def _assert_relative(path: Path, *, field: str) -> None:
    if path.is_absolute() or ".." in path.parts:
        raise PortablePackageError(f"{field} path must stay inside the package: {path}")


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_tree(source: Path, target: Path, *, forbidden_names: tuple[str, ...]) -> None:
    def ignore(_: str, names: list[str]) -> set[str]:
        skipped = IGNORED_NAMES.union(forbidden_names)
        return {name for name in names if name in skipped}

    shutil.copytree(source, target, ignore=ignore)


def _ensure_clean_target(target: Path) -> None:
    if target.exists() and any(target.iterdir()):
        raise PortablePackageError(
            f"target must not already contain files: {target}. "
            "Choose a new path or remove the old package directory."
        )
    target.mkdir(parents=True, exist_ok=True)


def _validate_report(
    target: Path, manifest: PortableManifest, *, copied: list[str], overlays: list[str]
) -> PackageReport:
    missing = tuple(
        path.as_posix() for path in manifest.required_target_paths if not (target / path).exists()
    )
    forbidden_paths = tuple(
        path.as_posix() for path in manifest.forbidden_target_paths if (target / path).exists()
    )
    forbidden_names = tuple(
        sorted(
            path.relative_to(target).as_posix()
            for name in manifest.forbidden_names
            for path in target.rglob(name)
        )
    )
    return PackageReport(
        ready=not missing and not forbidden_paths and not forbidden_names,
        target=str(target),
        copied_paths=tuple(copied),
        overlay_files=tuple(overlays),
        scaffold_dirs=tuple(path.as_posix() for path in manifest.scaffold_dirs),
        missing_required=missing,
        forbidden_present=forbidden_paths + forbidden_names,
    )


def build_portable_source(
    target: Path,
    *,
    root: Path = ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> PackageReport:
    manifest = _load_manifest(manifest_path)
    _ensure_clean_target(target)

    copied: list[str] = []
    for relative in manifest.include_paths:
        _assert_relative(relative, field="include_paths")
        source = root / relative
        destination = target / relative
        if not source.exists():
            raise PortablePackageError(f"include path does not exist: {relative}")
        if source.is_dir():
            _copy_tree(source, destination, forbidden_names=manifest.forbidden_names)
        else:
            if source.name in manifest.forbidden_names:
                raise PortablePackageError(f"include path is forbidden by name: {relative}")
            _copy_file(source, destination)
        copied.append(relative.as_posix())

    overlays: list[str] = []
    for source_relative, target_relative in manifest.overlay_files:
        _assert_relative(source_relative, field="overlay source")
        _assert_relative(target_relative, field="overlay target")
        source = root / source_relative
        if not source.is_file():
            raise PortablePackageError(f"overlay source does not exist: {source_relative}")
        if target_relative.name in manifest.forbidden_names:
            raise PortablePackageError(f"overlay target is forbidden by name: {target_relative}")
        _copy_file(source, target / target_relative)
        overlays.append(target_relative.as_posix())

    for relative in manifest.scaffold_dirs:
        _assert_relative(relative, field="scaffold_dirs")
        directory = target / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").write_text("", encoding="utf-8")

    return _validate_report(target, manifest, copied=copied, overlays=overlays)


def _render_text(report: PackageReport) -> str:
    lines = [
        f"ready: {'yes' if report.ready else 'no'}",
        f"target: {report.target}",
        "copied:",
    ]
    lines.extend(f"- {path}" for path in report.copied_paths)
    lines.append("overlays:")
    lines.extend(f"- {path}" for path in report.overlay_files)
    lines.append("scaffold:")
    lines.extend(f"- {path}" for path in report.scaffold_dirs)
    if report.missing_required:
        lines.append("missing required:")
        lines.extend(f"- {path}" for path in report.missing_required)
    if report.forbidden_present:
        lines.append("forbidden present:")
        lines.extend(f"- {path}" for path in report.forbidden_present)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="empty directory to receive the package")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="portable source manifest",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--check", action="store_true", help="exit non-zero if not ready")
    args = parser.parse_args(argv)

    try:
        report = build_portable_source(
            args.target.resolve(),
            root=ROOT,
            manifest_path=args.manifest.resolve(),
        )
    except PortablePackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(_render_text(report))

    return 0 if report.ready or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
