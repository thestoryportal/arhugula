"""U-RT-01 — package import smoke test.

Verifies the workspace member is importable. Concrete public-surface tests
land at later units (U-RT-02 types, U-RT-03 enum, etc.).
"""

from __future__ import annotations


def test_package_imports() -> None:
    """`harness_runtime` is importable as a uv workspace member."""
    import harness_runtime

    assert harness_runtime is not None
