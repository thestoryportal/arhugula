#!/usr/bin/env python3
"""Provider-free portable memory substrate closeout checker."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY_DOC = Path("docs/memory-substrate.md")
DOCS_INDEX = Path("docs/README.md")
MATRIX_SOURCE = Path("harness-runtime/src/harness_runtime/memory_verification_suite.py")
JUSTFILE = Path("justfile")
PORTABLE_SOURCE = Path("packaging/portable-source.toml")
PORTABLE_DOCS_INDEX = Path("packaging/portable/docs-README.md")
PORTABLE_JUSTFILE = Path("packaging/portable/justfile")

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
EXPECTED_REQUIREMENT_IDS = (
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
EXPECTED_LIVE_GATES = (
    "live-anthropic-native-memory",
    "live-claude-code-cli-auth",
    "live-codex-cli-auth",
    "live-antigravity-cli-auth",
    "live-gemini-legacy-cli-auth",
    "live-generic-command-cli-auth",
)
EXPECTED_MATRIX_TOKENS = (
    "LIVE_CREDENTIAL_GATES",
    "ACCESS_MODE_VERIFICATION_SCENARIOS",
    "CLI_PROFILE_VERIFICATION_SCENARIOS",
    "EXTERNAL_CLI_ROUTING_SCENARIOS",
    "memory_verification_matrix",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class MemoryCloseoutReport:
    ready: bool
    checks: tuple[CheckResult, ...]


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, ok=True, detail=detail)


def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, ok=False, detail=detail)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def verification_requirement_ids() -> tuple[str, ...]:
    """Return the portable package's expected verification requirement ids."""

    return EXPECTED_REQUIREMENT_IDS


def live_gate_ids() -> tuple[str, ...]:
    """Return the explicit live gates carried by the verification matrix."""

    return EXPECTED_LIVE_GATES


def _normalise_link_target(raw: str) -> str | None:
    target = raw.strip()
    if not target or target.startswith("#"):
        return None
    lowered = target.lower()
    if lowered.startswith(("http://", "https://", "mailto:")):
        return None
    return target.split("#", 1)[0]


def validate_markdown_links(root: Path, paths: tuple[Path, ...]) -> None:
    """Raise AssertionError when a checked local markdown link is broken."""

    failures: list[str] = []
    resolved_root = root.resolve()
    for relative_file in paths:
        path = root / relative_file
        if not path.is_file():
            failures.append(f"{relative_file.as_posix()}: file is missing")
            continue
        for raw_target in MARKDOWN_LINK_RE.findall(_read(path)):
            target = _normalise_link_target(raw_target)
            if target is None:
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(resolved_root)
            except ValueError:
                failures.append(f"{relative_file.as_posix()}: link escapes repo: {raw_target}")
                continue
            if not resolved.exists():
                failures.append(f"{relative_file.as_posix()}: broken link: {raw_target}")
    if failures:
        raise AssertionError("; ".join(failures))


def _required_files_check(root: Path) -> CheckResult:
    required = (
        MEMORY_DOC,
        DOCS_INDEX,
        MATRIX_SOURCE,
        JUSTFILE,
        PORTABLE_SOURCE,
        PORTABLE_DOCS_INDEX,
        PORTABLE_JUSTFILE,
    )
    missing = [path.as_posix() for path in required if not (root / path).is_file()]
    if missing:
        return _fail("required-files", f"missing required files: {missing}")
    return _ok("required-files", f"{len(required)} required files present")


def _docs_index_check(root: Path) -> CheckResult:
    missing: list[str] = []
    for path in (DOCS_INDEX, PORTABLE_DOCS_INDEX):
        content = _read(root / path)
        if "memory-substrate.md" not in content:
            missing.append(path.as_posix())
    if missing:
        return _fail("docs-index", f"missing memory-substrate.md links: {missing}")
    return _ok("docs-index", "docs indexes link to docs/memory-substrate.md")


def _memory_doc_sections_check(root: Path) -> CheckResult:
    content = _read(root / MEMORY_DOC)
    required_sections = (
        "## Operator Policy Guide",
        "## Maintainer Architecture Notes",
        "## Migration Notes",
        "## Source Grounding",
    )
    missing = [section for section in required_sections if section not in content]
    if missing:
        return _fail("memory-doc-sections", f"missing sections: {missing}")
    return _ok(
        "memory-doc-sections", "operator, maintainer, migration, and grounding sections present"
    )


def _matrix_tokens_check(root: Path) -> CheckResult:
    content = _read(root / MATRIX_SOURCE)
    expected = (*EXPECTED_MATRIX_TOKENS, *EXPECTED_REQUIREMENT_IDS, *EXPECTED_LIVE_GATES)
    missing = [token for token in expected if token not in content]
    if missing:
        return _fail("matrix-tokens", f"missing matrix tokens: {missing}")
    return _ok(
        "matrix-tokens", "C-MEM ids, live gates, and scenario matrices are declared"
    )


def _portable_recipe_check(root: Path) -> CheckResult:
    missing: list[str] = []
    for path in (JUSTFILE, PORTABLE_JUSTFILE):
        content = _read(root / path)
        if "memory-closeout-check" not in content:
            missing.append(path.as_posix())
    manifest = _read(root / PORTABLE_SOURCE)
    for token in (
        "docs/memory-substrate.md",
        "tools/memory_closeout_check.py",
        "tools/test_memory_closeout_check.py",
    ):
        if token not in manifest:
            missing.append(f"{PORTABLE_SOURCE.as_posix()}:{token}")
    if missing:
        return _fail("portable-recipes", f"missing portable references: {missing}")
    return _ok("portable-recipes", "just and portable-source surfaces include memory gate")


def _markdown_links_check(root: Path) -> CheckResult:
    try:
        validate_markdown_links(root, (DOCS_INDEX, MEMORY_DOC))
    except AssertionError as exc:
        return _fail("markdown-links", str(exc))
    return _ok("markdown-links", "local markdown links resolve in memory docs")


def validate(root: Path = ROOT) -> MemoryCloseoutReport:
    checks = (
        _required_files_check(root),
        _docs_index_check(root),
        _memory_doc_sections_check(root),
        _matrix_tokens_check(root),
        _portable_recipe_check(root),
        _markdown_links_check(root),
    )
    return MemoryCloseoutReport(ready=all(check.ok for check in checks), checks=checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero when closeout evidence is incomplete"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root to validate")
    args = parser.parse_args(argv)

    report = validate(args.root)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        for check in report.checks:
            status = "ok" if check.ok else "fail"
            print(f"{status}: {check.name}: {check.detail}")
        print(f"ready: {'yes' if report.ready else 'no'}")

    if args.check and not report.ready:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
