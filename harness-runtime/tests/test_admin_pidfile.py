"""U-RT-48 pidfile primitive tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from harness_runtime.admin.pidfile import (
    DEFAULT_PIDFILE_BASENAME,
    PidfileError,
    default_pidfile_path,
    read_pidfile,
    remove_pidfile,
    write_pidfile,
)


def test_default_pidfile_basename() -> None:
    assert DEFAULT_PIDFILE_BASENAME == Path(".harness/runtime.pid")


def test_default_pidfile_path(tmp_path: Path) -> None:
    assert default_pidfile_path(tmp_path) == tmp_path / ".harness/runtime.pid"


def test_write_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".harness/runtime.pid"
    write_pidfile(path, 12345)
    assert read_pidfile(path) == 12345


def test_write_pidfile_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "newly/nested/dir/runtime.pid"
    assert not path.parent.exists()
    write_pidfile(path, 42)
    assert path.parent.is_dir()
    assert path.is_file()


def test_write_pidfile_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tmp file must be renamed atomically via os.replace."""
    path = tmp_path / "runtime.pid"
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy_replace(src: object, dst: object, *args: object, **kwargs: object) -> object:
        calls.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", _spy_replace)
    write_pidfile(path, 9999)

    assert len(calls) == 1
    src, dst = calls[0]
    assert src.endswith(".pid.tmp")
    assert dst == str(path)
    assert read_pidfile(path) == 9999


def test_write_pidfile_overwrites_prior_value(tmp_path: Path) -> None:
    path = tmp_path / "runtime.pid"
    write_pidfile(path, 100)
    write_pidfile(path, 200)
    assert read_pidfile(path) == 200


def test_read_pidfile_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(PidfileError, match="not found"):
        read_pidfile(tmp_path / "missing.pid")


def test_read_pidfile_empty_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.pid"
    path.write_text("")
    with pytest.raises(PidfileError, match="empty"):
        read_pidfile(path)


def test_read_pidfile_non_integer_raises(tmp_path: Path) -> None:
    path = tmp_path / "junk.pid"
    path.write_text("not-a-number\n")
    with pytest.raises(PidfileError, match="not an integer"):
        read_pidfile(path)


def test_read_pidfile_trims_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "padded.pid"
    path.write_text("  4242\n  \n")
    assert read_pidfile(path) == 4242


def test_remove_pidfile_deletes(tmp_path: Path) -> None:
    path = tmp_path / "runtime.pid"
    write_pidfile(path, 1)
    remove_pidfile(path)
    assert not path.exists()


def test_remove_pidfile_idempotent(tmp_path: Path) -> None:
    """Second remove is a no-op (FileNotFoundError swallowed)."""
    path = tmp_path / "runtime.pid"
    write_pidfile(path, 1)
    remove_pidfile(path)
    remove_pidfile(path)  # must not raise
    assert not path.exists()
