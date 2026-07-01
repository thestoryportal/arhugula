#!/usr/bin/env python3
"""Host readiness probes for R-411/R-412 sandbox provider work.

The probes are intentionally non-mutating: they do not install runtimes, pull
images, create devices, or start VMs. They only answer whether the current host
already satisfies the minimum preconditions for a provider implementation/e2e.
"""

from __future__ import annotations

import argparse
import ctypes.util
import importlib.util
import json
import os
import platform
import shutil
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path


def _env_present(name: str) -> bool:
    return bool(os.environ.get(name))


@dataclass(frozen=True)
class HostMode:
    system: str
    architectures: tuple[str, ...]
    requires_kvm: bool
    detail: str


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    roadmap_item: str
    sandbox_tier: str
    required_binaries: tuple[str, ...]
    required_env_vars: tuple[str, ...]
    required_python_modules: tuple[str, ...]
    required_libraries: tuple[str, ...]
    host_modes: tuple[HostMode, ...]
    source_url: str
    note: str


@dataclass(frozen=True)
class HostProbe:
    system: Callable[[], str] = platform.system
    machine: Callable[[], str] = platform.machine
    which: Callable[[str], str | None] = shutil.which
    exists: Callable[[Path], bool] = Path.exists
    access: Callable[[Path, int], bool] = os.access
    stat_path: Callable[[Path], os.stat_result] = Path.stat
    env_present: Callable[[str], bool] = _env_present
    find_module: Callable[[str], object | None] = importlib.util.find_spec
    find_library: Callable[[str], str | None] = ctypes.util.find_library


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    provider: str
    roadmap_item: str
    sandbox_tier: str
    ready: bool
    checks: tuple[CheckResult, ...]
    source_url: str
    note: str


PROVIDERS: Mapping[str, ProviderSpec] = {
    "r411-gvisor": ProviderSpec(
        provider="r411-gvisor",
        roadmap_item="R-411-sandbox-tier-3-microvm-execution",
        sandbox_tier="tier-3-microvm",
        required_binaries=("runsc", "docker"),
        required_env_vars=(),
        required_python_modules=(),
        required_libraries=(),
        host_modes=(
            HostMode(
                system="Linux",
                architectures=("x86_64", "aarch64"),
                requires_kvm=False,
                detail="Linux x86_64/aarch64 with runsc + Docker",
            ),
        ),
        source_url="https://github.com/google/gvisor",
        note="R-411 candidate: gVisor/runsc shared-kernel sandbox.",
    ),
    "r411-kata": ProviderSpec(
        provider="r411-kata",
        roadmap_item="R-411-sandbox-tier-3-microvm-execution",
        sandbox_tier="tier-3-microvm",
        required_binaries=("kata-runtime",),
        required_env_vars=(),
        required_python_modules=(),
        required_libraries=(),
        host_modes=(
            HostMode(
                system="Linux",
                architectures=("x86_64", "aarch64"),
                requires_kvm=True,
                detail="Linux x86_64/aarch64 with KVM and kata-runtime",
            ),
        ),
        source_url="https://github.com/kata-containers/kata-containers",
        note="R-411 candidate: Kata Containers microVM-backed container runtime.",
    ),
    "r411-shuru": ProviderSpec(
        provider="r411-shuru",
        roadmap_item="R-411-sandbox-tier-3-microvm-execution",
        sandbox_tier="tier-3-microvm",
        required_binaries=("shuru",),
        required_env_vars=(),
        required_python_modules=(),
        required_libraries=(),
        host_modes=(
            HostMode(
                system="Darwin",
                architectures=("aarch64",),
                requires_kvm=False,
                detail="macOS 14+ on Apple Silicon",
            ),
            HostMode(
                system="Linux",
                architectures=("aarch64",),
                requires_kvm=True,
                detail="experimental Linux ARM64 with KVM",
            ),
        ),
        source_url="https://github.com/superhq-ai/shuru",
        note="R-411 candidate: local-first microVM sandbox for AI agents.",
    ),
    "r411-microsandbox": ProviderSpec(
        provider="r411-microsandbox",
        roadmap_item="R-411-sandbox-tier-3-microvm-execution",
        sandbox_tier="tier-3-microvm",
        required_binaries=("msb",),
        required_env_vars=(),
        required_python_modules=(),
        required_libraries=(),
        host_modes=(
            HostMode(
                system="Darwin",
                architectures=("aarch64",),
                requires_kvm=False,
                detail="macOS on Apple Silicon",
            ),
            HostMode(
                system="Linux",
                architectures=("x86_64", "aarch64"),
                requires_kvm=True,
                detail="Linux x86_64/aarch64 with KVM",
            ),
        ),
        source_url="https://github.com/superradcompany/microsandbox",
        note="R-411 candidate: local-first embeddable microVM sandbox.",
    ),
    "r411-libkrun": ProviderSpec(
        provider="r411-libkrun",
        roadmap_item="R-411-sandbox-tier-3-microvm-execution",
        sandbox_tier="tier-3-microvm",
        required_binaries=(),
        required_env_vars=(),
        required_python_modules=(),
        required_libraries=("krun",),
        host_modes=(
            HostMode(
                system="Darwin",
                architectures=("aarch64",),
                requires_kvm=False,
                detail="macOS 14+ on Apple Silicon with HVF and libkrun",
            ),
            HostMode(
                system="Linux",
                architectures=("x86_64", "aarch64"),
                requires_kvm=True,
                detail="Linux x86_64/aarch64 with KVM and libkrun",
            ),
        ),
        source_url="https://github.com/containers/libkrun",
        note=(
            "R-411 substrate candidate: embeddable virtualization-based process "
            "isolation library; host OS isolation is still required around the VMM."
        ),
    ),
    "r412-firecracker": ProviderSpec(
        provider="r412-firecracker",
        roadmap_item="R-412-sandbox-tier-4-full-vm-execution",
        sandbox_tier="tier-4-full-vm",
        required_binaries=("firecracker",),
        required_env_vars=(),
        required_python_modules=(),
        required_libraries=(),
        host_modes=(
            HostMode(
                system="Linux",
                architectures=("x86_64", "aarch64"),
                requires_kvm=True,
                detail="Linux x86_64/aarch64 with KVM and firecracker",
            ),
        ),
        source_url="https://github.com/firecracker-microvm/firecracker",
        note="R-412 candidate: Firecracker hardware-virtualized microVM/full-VM provider.",
    ),
    "r412-qemu-microvm": ProviderSpec(
        provider="r412-qemu-microvm",
        roadmap_item="R-412-sandbox-tier-4-full-vm-execution",
        sandbox_tier="tier-4-full-vm",
        required_binaries=("qemu-system-x86_64",),
        required_env_vars=(),
        required_python_modules=(),
        required_libraries=(),
        host_modes=(
            HostMode(
                system="Linux",
                architectures=("x86_64",),
                requires_kvm=True,
                detail="Linux x86_64 with KVM and qemu-system-x86_64 microvm",
            ),
        ),
        source_url="https://github.com/bonzini/qemu/blob/master/docs/system/i386/microvm.rst",
        note=(
            "R-412 candidate: QEMU's Firecracker-inspired microvm machine type; "
            "requires per-run kernel/rootfs artifacts in addition to this host probe."
        ),
    ),
    "r421-e2b": ProviderSpec(
        provider="r421-e2b",
        roadmap_item="R-421-managed-cloud-deployment-e2e",
        sandbox_tier="managed-cloud-sandbox",
        required_binaries=(),
        required_env_vars=("E2B_API_KEY",),
        required_python_modules=("e2b",),
        required_libraries=(),
        host_modes=(),
        source_url="https://github.com/e2b-dev/e2b",
        note=(
            "R-421/R-412 candidate: E2B managed cloud sandbox API; requires "
            "operator-approved credentials and remote execution."
        ),
    ),
}

ALIASES = {
    "gvisor": "r411-gvisor",
    "kata": "r411-kata",
    "shuru": "r411-shuru",
    "microsandbox": "r411-microsandbox",
    "msb": "r411-microsandbox",
    "libkrun": "r411-libkrun",
    "firecracker": "r412-firecracker",
    "qemu-microvm": "r412-qemu-microvm",
    "microvm": "r412-qemu-microvm",
    "e2b": "r421-e2b",
}

KVM_DEVICE = Path("/dev/kvm")
DEFAULT_HOST_PROBE = HostProbe()


def normalize_provider(provider: str) -> str:
    """Return the canonical provider key accepted by the readiness table."""
    key = provider.strip().lower()
    return ALIASES.get(key, key)


def _kvm_device_is_character(probe: HostProbe) -> bool:
    try:
        return stat.S_ISCHR(probe.stat_path(KVM_DEVICE).st_mode)
    except OSError:
        return False


def _normalize_arch(arch: str) -> str:
    return "aarch64" if arch == "arm64" else arch


def check_provider(provider: str, *, probe: HostProbe | None = None) -> ReadinessReport:
    """Check whether the current host is ready for a sandbox provider e2e."""
    if probe is None:
        probe = DEFAULT_HOST_PROBE

    key = normalize_provider(provider)
    if key not in PROVIDERS:
        allowed_keys = set(PROVIDERS) | set(ALIASES)
        allowed = ", ".join(sorted(allowed_keys))
        raise ValueError(f"unknown provider {provider!r}; expected one of: {allowed}")

    spec = PROVIDERS[key]
    checks: list[CheckResult] = []

    host_os = probe.system()
    arch = _normalize_arch(probe.machine())
    matching_modes = [
        mode for mode in spec.host_modes if mode.system == host_os and arch in mode.architectures
    ]
    if spec.host_modes:
        expected = "; ".join(mode.detail for mode in spec.host_modes)
        checks.append(
            CheckResult(
                name="host-mode",
                ok=bool(matching_modes),
                detail=f"expected {expected}; observed {host_os} {arch}",
            )
        )

    for binary in spec.required_binaries:
        binary_path = probe.which(binary)
        checks.append(
            CheckResult(
                name=f"binary:{binary}",
                ok=binary_path is not None,
                detail=(
                    f"{binary} found at {binary_path}"
                    if binary_path
                    else f"{binary} not found on PATH"
                ),
            )
        )

    for env_var in spec.required_env_vars:
        checks.append(
            CheckResult(
                name=f"env:{env_var}",
                ok=probe.env_present(env_var),
                detail=f"{env_var} {'is set' if probe.env_present(env_var) else 'is not set'}",
            )
        )

    for module in spec.required_python_modules:
        module_found = probe.find_module(module) is not None
        checks.append(
            CheckResult(
                name=f"python-module:{module}",
                ok=module_found,
                detail=(
                    f"Python module {module!r} "
                    f"{'is importable' if module_found else 'is not importable'}"
                ),
            )
        )

    for library in spec.required_libraries:
        library_path = probe.find_library(library)
        checks.append(
            CheckResult(
                name=f"library:{library}",
                ok=library_path is not None,
                detail=(
                    f"Library {library!r} found as {library_path}"
                    if library_path
                    else f"Library {library!r} is not discoverable"
                ),
            )
        )

    requires_kvm = False
    if spec.host_modes:
        requires_kvm = (
            any(mode.requires_kvm for mode in matching_modes)
            if matching_modes
            else all(mode.requires_kvm for mode in spec.host_modes)
        )
    if requires_kvm:
        kvm_exists = probe.exists(KVM_DEVICE)
        checks.append(
            CheckResult(
                name="kvm-device-present",
                ok=kvm_exists,
                detail=f"{KVM_DEVICE} {'exists' if kvm_exists else 'is missing'}",
            )
        )
        if kvm_exists:
            checks.append(
                CheckResult(
                    name="kvm-device-type",
                    ok=_kvm_device_is_character(probe),
                    detail=f"{KVM_DEVICE} must be a character device",
                )
            )
            can_read_write = probe.access(KVM_DEVICE, os.R_OK | os.W_OK)
            checks.append(
                CheckResult(
                    name="kvm-device-access",
                    ok=can_read_write,
                    detail=f"{KVM_DEVICE} must be readable and writable by this user",
                )
            )

    return ReadinessReport(
        provider=spec.provider,
        roadmap_item=spec.roadmap_item,
        sandbox_tier=spec.sandbox_tier,
        ready=all(check.ok for check in checks),
        checks=tuple(checks),
        source_url=spec.source_url,
        note=spec.note,
    )


def _render_text(report: ReadinessReport) -> str:
    lines = [
        f"provider: {report.provider}",
        f"roadmap_item: {report.roadmap_item}",
        f"sandbox_tier: {report.sandbox_tier}",
        f"source_url: {report.source_url}",
        f"ready: {'yes' if report.ready else 'no'}",
        f"note: {report.note}",
        "checks:",
    ]
    for check in report.checks:
        marker = "PASS" if check.ok else "FAIL"
        lines.append(f"- {marker} {check.name}: {check.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        default="r411-gvisor",
        help=(
            "Provider key: r411-gvisor, r411-kata, r411-libkrun, "
            "r412-firecracker, r412-qemu-microvm, or alias."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        report = check_provider(args.provider)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
