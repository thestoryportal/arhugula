"""Tests for the portable memory substrate closeout checker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    path = ROOT / "tools" / "memory_closeout_check.py"
    assert path.is_file(), "tools/memory_closeout_check.py must exist"
    spec = importlib.util.spec_from_file_location("memory_closeout_check", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_memory_requirement_and_live_gate_ids_are_declared() -> None:
    checker = _load_checker()

    assert checker.verification_requirement_ids() == (
        "schema_validation",
        "path_traversal_rejection",
        "append_only_ledger_hash_chain",
        "concurrent_writer_no_fork",
        "promotion_policy",
        "memory_poisoning",
        "compaction_safety",
        "retrieval_determinism",
        "cross_scope_cross_tenant_denial",
        "prompt_packet_fallback",
        "standard_memory_tools",
        "native_anthropic_adapter",
        "cli_profile_resolution",
        "engine_class_durability",
        "redaction_tombstone_exclusion",
    )
    assert checker.live_gate_ids() == (
        "live-anthropic-native-memory",
        "live-claude-code-cli-auth",
        "live-codex-cli-auth",
        "live-antigravity-cli-auth",
        "live-gemini-legacy-cli-auth",
        "live-generic-command-cli-auth",
    )


def test_memory_closeout_evidence_is_complete() -> None:
    checker = _load_checker()

    report = checker.validate(ROOT)

    assert report.ready, [check.detail for check in report.checks if not check.ok]


def test_memory_docs_are_operator_discoverable_and_source_grounded() -> None:
    checker = _load_checker()
    docs_index = ROOT / "docs" / "README.md"
    portable_docs_index = ROOT / "packaging" / "portable" / "docs-README.md"
    memory_doc = ROOT / "docs" / "memory-substrate.md"
    memory_readme = ROOT / "docs" / "memory-layer-readme.md"

    assert "memory-substrate.md" in docs_index.read_text(encoding="utf-8")
    assert "memory-layer-readme.md" in docs_index.read_text(encoding="utf-8")
    assert "memory-substrate.md" in portable_docs_index.read_text(encoding="utf-8")
    assert "memory-layer-readme.md" in portable_docs_index.read_text(encoding="utf-8")
    assert memory_doc.is_file()
    assert memory_readme.is_file()
    checker.validate_markdown_links(
        ROOT,
        (
            Path("docs/README.md"),
            Path("docs/memory-substrate.md"),
            Path("docs/memory-layer-readme.md"),
        ),
    )
