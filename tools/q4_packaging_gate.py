#!/usr/bin/env python3
"""Provider-free packaging/deployment gate for R-CL-Q4."""

from __future__ import annotations

import argparse
import configparser
import json
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_LOCK = "requirements.lock.txt"
WORKSPACE_PACKAGES: tuple[str, ...] = (
    "harness-core",
    "harness-is",
    "harness-as",
    "harness-cp",
    "harness-od",
    "harness-cxa",
    "harness-runtime",
)
RUNTIME_ENTRY_POINTS = {
    "harness": "harness_runtime.cli:main",
    "harness-inspect": "harness_runtime.admin.inspect:main",
    "harness-shutdown": "harness_runtime.admin.shutdown_cli:main",
}
IMAGE_TARGETS = ("self-hosted-daemon", "managed-cloud-daemon", "sandbox-runner")
READINESS_RECIPES = (
    "q4-packaging-check",
    "r420-self-hosted-stack-up",
    "r420-self-hosted-readiness",
    "r421-managed-cloud-readiness",
    "sandbox-host-check",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PackagingReport:
    ready: bool
    checks: tuple[CheckResult, ...]
    dist: str
    note: str


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, ok=True, detail=detail)


def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, ok=False, detail=detail)


def _load_toml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], tomllib.loads(path.read_text(encoding="utf-8")))


def _nested_mapping(data: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    current: object = data
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return cast(dict[str, Any], current if isinstance(current, dict) else {})


def _canonical_package_name(package_name: str) -> str:
    return package_name.replace("-", "_")


def _wheel_for(dist: Path, package_name: str) -> Path | None:
    prefix = f"{_canonical_package_name(package_name)}-"
    matches = sorted(path for path in dist.glob("*.whl") if path.name.startswith(prefix))
    return matches[0] if matches else None


def _workspace_member_check(root: Path) -> CheckResult:
    data = _load_toml(root / "pyproject.toml")
    workspace = _nested_mapping(data, ("tool", "uv", "workspace"))
    observed = tuple(cast(list[str], workspace.get("members", [])))
    missing = sorted(set(WORKSPACE_PACKAGES) - set(observed))
    extra = sorted(set(observed) - set(WORKSPACE_PACKAGES))
    if missing or extra:
        return _fail(
            "workspace-members",
            f"workspace member mismatch; missing={missing or 'none'} extra={extra or 'none'}",
        )
    return _ok("workspace-members", f"{len(observed)} workspace package members declared")


def _package_build_metadata_check(root: Path) -> CheckResult:
    failures: list[str] = []
    for package_name in WORKSPACE_PACKAGES:
        data = _load_toml(root / package_name / "pyproject.toml")
        project = cast(dict[str, Any], data.get("project", {}))
        if project.get("name") != package_name:
            failures.append(f"{package_name}: project.name={project.get('name')!r}")
        build_system = cast(dict[str, Any], data.get("build-system", {}))
        if build_system.get("build-backend") != "hatchling.build":
            failures.append(f"{package_name}: build-backend={build_system.get('build-backend')!r}")
        wheel_target = _nested_mapping(data, ("tool", "hatch", "build", "targets", "wheel"))
        packages = cast(list[str], wheel_target.get("packages", []))
        if not packages:
            failures.append(f"{package_name}: missing wheel packages")
    if failures:
        return _fail("package-build-metadata", "; ".join(failures))
    return _ok("package-build-metadata", "all workspace packages declare hatchling wheel targets")


def _wheel_artifact_check(dist: Path) -> CheckResult:
    missing = [
        package_name
        for package_name in WORKSPACE_PACKAGES
        if _wheel_for(dist, package_name) is None
    ]
    sdists = sorted(path.name for path in dist.glob("*.tar.gz"))
    if missing or sdists:
        return _fail(
            "wheel-artifacts",
            f"missing wheels={missing or 'none'} unexpected sdists={sdists or 'none'}",
        )
    return _ok("wheel-artifacts", f"{len(WORKSPACE_PACKAGES)} workspace wheels present")


def _runtime_entry_point_check(dist: Path) -> CheckResult:
    runtime_wheel = _wheel_for(dist, "harness-runtime")
    if runtime_wheel is None:
        return _fail("runtime-entry-points", "harness-runtime wheel is missing")

    with zipfile.ZipFile(runtime_wheel) as wheel:
        entry_point_paths = [
            name for name in wheel.namelist() if name.endswith(".dist-info/entry_points.txt")
        ]
        if not entry_point_paths:
            return _fail("runtime-entry-points", "entry_points.txt missing from runtime wheel")
        parser = configparser.ConfigParser()
        parser.read_string(wheel.read(entry_point_paths[0]).decode("utf-8"))

    console_scripts = parser["console_scripts"] if parser.has_section("console_scripts") else {}
    failures = [
        f"{script}={console_scripts.get(script)!r}"
        for script, expected in RUNTIME_ENTRY_POINTS.items()
        if console_scripts.get(script) != expected
    ]
    if failures:
        return _fail("runtime-entry-points", "; ".join(failures))
    return _ok("runtime-entry-points", "runtime wheel exposes harness CLI entry points")


def _requirements_lock_check(dist: Path) -> CheckResult:
    lock_path = dist / REQUIREMENTS_LOCK
    if not lock_path.exists():
        return _fail("requirements-lock", f"{REQUIREMENTS_LOCK} missing from dist")
    content = lock_path.read_text(encoding="utf-8")
    if "--hash=sha256:" not in content:
        return _fail("requirements-lock", "lock export must retain sha256 hashes")
    forbidden_markers = ("-e ", " file://", "../harness-", "./harness-")
    observed = [marker for marker in forbidden_markers if marker in content]
    if observed:
        return _fail(
            "requirements-lock",
            f"lock export contains local/editable markers: {observed}",
        )
    return _ok("requirements-lock", "third-party requirements export is hashed and workspace-free")


def _image_recipe_check(root: Path) -> CheckResult:
    dockerfile = root / "deploy" / "images" / "harness-runtime.Dockerfile"
    readme = root / "deploy" / "images" / "README.md"
    if not dockerfile.exists() or not readme.exists():
        return _fail("deploy-image-recipes", "deploy/images Dockerfile or README is missing")
    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    readme_text = readme.read_text(encoding="utf-8")
    missing_targets = [target for target in IMAGE_TARGETS if f" AS {target}" not in dockerfile_text]
    missing_packages = [
        package_name for package_name in WORKSPACE_PACKAGES if package_name not in dockerfile_text
    ]
    required_fragments = (
        f"COPY {REQUIREMENTS_LOCK}",
        "pip install --no-cache-dir --require-hashes",
        "pip install --no-cache-dir --no-deps --no-index --find-links=/wheelhouse",
        'ENTRYPOINT ["harness", "daemon"]',
        'ENTRYPOINT ["python"]',
    )
    missing_fragments = [
        fragment for fragment in required_fragments if fragment not in dockerfile_text
    ]
    missing_readme_targets = [
        target for target in IMAGE_TARGETS if f"--target {target}" not in readme_text
    ]
    failures = missing_targets + missing_packages + missing_fragments + missing_readme_targets
    if failures:
        return _fail("deploy-image-recipes", f"missing image recipe evidence: {failures}")
    return _ok(
        "deploy-image-recipes",
        "self-hosted, managed-cloud, and sandbox image targets declared",
    )


def _readiness_recipe_check(root: Path) -> CheckResult:
    justfile = (root / "justfile").read_text(encoding="utf-8")
    missing = [recipe for recipe in READINESS_RECIPES if f"{recipe}" not in justfile]
    if missing:
        return _fail("one-command-readiness", f"missing just recipes: {missing}")
    return _ok(
        "one-command-readiness",
        "packaging, self-hosted, managed-cloud, and sandbox checks are one-command recipes",
    )


def validate(root: Path, dist: Path) -> PackagingReport:
    checks = (
        _workspace_member_check(root),
        _package_build_metadata_check(root),
        _wheel_artifact_check(dist),
        _runtime_entry_point_check(dist),
        _requirements_lock_check(dist),
        _image_recipe_check(root),
        _readiness_recipe_check(root),
    )
    return PackagingReport(
        ready=all(check.ok for check in checks),
        checks=checks,
        dist=str(dist),
        note=(
            "Provider-free Q4 gate: builds workspace wheels, exports locked third-party "
            "requirements, and validates deploy/readiness artifacts without starting providers."
        ),
    )


def _run_command(args: list[str], *, cwd: Path) -> None:
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)


def build_dist(root: Path, dist: Path) -> None:
    _run_command(
        [
            "uv",
            "build",
            "--all-packages",
            "--wheel",
            "--clear",
            "--no-create-gitignore",
            "--out-dir",
            str(dist),
        ],
        cwd=root,
    )
    _run_command(
        [
            "uv",
            "export",
            "--all-packages",
            "--no-dev",
            "--locked",
            "--format",
            "requirements.txt",
            "--no-emit-project",
            "--no-emit-workspace",
            "--output-file",
            str(dist / REQUIREMENTS_LOCK),
        ],
        cwd=root,
    )


def _render_text(report: PackagingReport) -> str:
    lines = [
        f"ready: {'yes' if report.ready else 'no'}",
        f"dist: {report.dist}",
        f"note: {report.note}",
        "checks:",
    ]
    for check in report.checks:
        marker = "PASS" if check.ok else "FAIL"
        lines.append(f"- {marker} {check.name}: {check.detail}")
    return "\n".join(lines)


def _emit_report(report: PackagingReport, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(_render_text(report))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--build",
        action="store_true",
        help="Build workspace wheels before checking.",
    )
    source.add_argument("--dist", type=Path, help="Existing dist directory to inspect.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Return non-zero when the gate is not ready.",
    )
    args = parser.parse_args(argv)

    if args.build:
        with tempfile.TemporaryDirectory(prefix="arhugula-q4-dist-") as tmp:
            dist = Path(tmp)
            build_dist(ROOT, dist)
            report = validate(ROOT, dist)
            _emit_report(report, as_json=args.json)
            return 0 if report.ready or not args.check else 1

    dist = args.dist
    if dist is None:
        parser.error("--dist is required unless --build is supplied")
    report = validate(ROOT, dist)
    _emit_report(report, as_json=args.json)
    return 0 if report.ready or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
