#!/usr/bin/env python3
"""Materialize a temp harness config from a local config plus example overlay."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OVERLAY_ROOT_PLACEHOLDERS = (
    "/absolute/path/to/arhugula",
    "/absolute/path/to/your/workspace",
)


def _load_toml(path: Path, *, replacements: Mapping[str, str] | None = None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    for old, new in (replacements or {}).items():
        text = text.replace(old, new)
    return tomllib.loads(text)


def _merge_config(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config(current, value)
        else:
            merged[key] = value
    return merged


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, Mapping):
        items = ", ".join(f"{key} = {_format_value(item)}" for key, item in value.items())
        return f"{{ {items} }}"
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    if value is None:
        raise TypeError("TOML has no null value")
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


def _render_table(path: tuple[str, ...], table: Mapping[str, Any]) -> list[str]:
    scalars: list[tuple[str, Any]] = []
    subtables: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in table.items():
        if isinstance(value, Mapping):
            subtables.append((key, value))
        else:
            scalars.append((key, value))

    lines: list[str] = []
    if path:
        lines.append(f"[{'.'.join(path)}]")
    lines.extend(f"{key} = {_format_value(value)}" for key, value in scalars)
    if path and scalars:
        lines.append("")

    for key, value in subtables:
        lines.extend(_render_table((*path, key), value))
    return lines


def _render_toml(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    root_scalars = {key: value for key, value in data.items() if not isinstance(value, Mapping)}
    if root_scalars:
        lines.extend(f"{key} = {_format_value(value)}" for key, value in root_scalars.items())
        lines.append("")

    for key, value in data.items():
        if isinstance(value, Mapping):
            lines.extend(_render_table((key,), value))
    return "\n".join(lines).rstrip() + "\n"


def _default_output_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="arhugula-harness-", suffix=".toml")
    os.close(fd)
    return Path(name)


def apply_overlay(
    *,
    base_config: Path,
    overlay: Path,
    repo_root: Path,
    output: Path | None = None,
) -> Path:
    """Apply an example RuntimeConfig overlay to a copy of ``base_config``."""
    resolved_root = repo_root.resolve()
    replacements = {
        placeholder: resolved_root.as_posix() for placeholder in OVERLAY_ROOT_PLACEHOLDERS
    }
    base_data = _load_toml(base_config)
    overlay_data = _load_toml(overlay, replacements=replacements)
    output_path = output or _default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_toml(_merge_config(base_data, overlay_data)), encoding="utf-8")
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("overlay", type=Path, help="example runtime overlay TOML file")
    parser.add_argument("--base", type=Path, default=ROOT / "harness.toml")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    output = apply_overlay(
        base_config=args.base,
        overlay=args.overlay,
        repo_root=args.repo_root,
        output=args.output,
    )
    print(output.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
