"""Tests for portable checkout initialization."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_portable_init():
    path = ROOT / "tools" / "portable_init.py"
    spec = importlib.util.spec_from_file_location("portable_init", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_portable_init_creates_local_memory_layout(tmp_path: Path) -> None:
    module = _load_portable_init()
    (tmp_path / "harness.toml.example").write_text(
        '[runtime]\nrepository_root = "/absolute/path/to/your/workspace"\n'
        "\n"
        "[runtime.memory]\n"
        "enabled = true\n"
        "native_provider_enabled = false\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("", encoding="utf-8")

    result = module.initialize(tmp_path)

    assert ".harness/memory" in result.created
    assert (tmp_path / ".harness" / "memory" / "semantic" / "facts").is_dir()
    assert (tmp_path / ".harness" / "memory" / "procedural" / "snapshots").is_dir()
    assert (tmp_path / ".harness" / "memory" / "durable").is_dir()
    rendered_config = (tmp_path / "harness.toml").read_text(encoding="utf-8")
    assert tmp_path.as_posix() in rendered_config
    assert "[runtime.memory]" in rendered_config
    assert "native_provider_enabled = false" in rendered_config
