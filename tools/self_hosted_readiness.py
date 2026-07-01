#!/usr/bin/env python3
"""Non-mutating readiness checks for R-420/R-440 self-hosted deployment work.

This probe validates static runtime configuration only. It does not start the
harness daemon, call an OTLP collector, fetch secrets, or contact a secrets
backend. Its purpose is to make the current infrastructure boundary explicit
before any live SELF_HOSTED_SERVER e2e is attempted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from harness_core import DeploymentSurface
from harness_od.observability_matrix import CellID
from harness_od.per_cell_collector_placement_matrix import (
    CollectorPlacement,
    collector_placement,
)
from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource
from harness_runtime.types import ProviderSecretBackend, ProviderSecretsConfig, RuntimeConfig


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    checks: tuple[CheckResult, ...]
    roadmap_items: tuple[str, ...]
    note: str


ROADMAP_ITEMS = (
    "R-420-self-hosted-server-deployment-e2e",
    "R-440-tier-level-secrets-backend",
)
_SELF_HOSTED_REAL_COLLECTOR_PLACEMENTS = frozenset(
    placement for placement in CollectorPlacement if placement is not CollectorPlacement.IN_PROCESS
)


def _format_allowed(values: Iterable[CollectorPlacement]) -> str:
    return ", ".join(sorted(value.value for value in values))


def _deployment_surface_check(config: RuntimeConfig) -> CheckResult:
    ok = config.deployment_surface is DeploymentSurface.SELF_HOSTED_SERVER
    return CheckResult(
        name="deployment-surface",
        ok=ok,
        detail=(
            "deployment_surface is self-hosted-server"
            if ok
            else f"expected self-hosted-server; observed {config.deployment_surface.value}"
        ),
    )


def _collector_placement_check(config: RuntimeConfig) -> CheckResult:
    cell = CellID(
        persona_tier=config.persona_tier,
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
    )
    allowed = collector_placement(cell) & _SELF_HOSTED_REAL_COLLECTOR_PLACEMENTS
    observed = config.collector.placement
    ok = config.deployment_surface is DeploymentSurface.SELF_HOSTED_SERVER and observed in allowed
    return CheckResult(
        name="collector-placement",
        ok=ok,
        detail=(
            f"collector placement {observed.value} is valid for {cell.persona_tier.value} "
            "SELF_HOSTED_SERVER"
            if ok
            else (
                f"expected one of {_format_allowed(allowed)} for "
                f"{cell.persona_tier.value} SELF_HOSTED_SERVER with a real collector; "
                f"observed {observed.value}"
            )
        ),
    )


def _otlp_endpoint_check(config: RuntimeConfig) -> CheckResult:
    parsed = urlparse(config.otel.otlp_endpoint)
    ok = bool(parsed.scheme and parsed.netloc)
    return CheckResult(
        name="otlp-endpoint",
        ok=ok,
        detail=(
            f"OTLP endpoint is configured at {config.otel.otlp_endpoint}"
            if ok
            else f"OTLP endpoint must include scheme and host; observed {config.otel.otlp_endpoint}"
        ),
    )


def _secret_allowlist_check(config: RuntimeConfig) -> CheckResult:
    count = len(config.provider_secrets.operator_allowlist)
    return CheckResult(
        name="provider-secret-allowlist",
        ok=count > 0,
        detail=(
            f"provider_secrets.operator_allowlist has {count} entr{'y' if count == 1 else 'ies'}"
            if count > 0
            else "provider_secrets.operator_allowlist is empty"
        ),
    )


def _tier_secret_backend_check(provider_secrets: ProviderSecretsConfig) -> CheckResult:
    backend = provider_secrets.backend
    ok = backend is ProviderSecretBackend.SELF_HOSTED_KEYRING
    return CheckResult(
        name="tier-secrets-backend",
        ok=ok,
        detail=(
            f"tier-level backend selector is {backend.value}"
            if ok
            else f"expected self-hosted-keyring; observed {backend.value}"
        ),
    )


def evaluate_config(config: RuntimeConfig) -> ReadinessReport:
    """Evaluate a loaded config against the static R-420/R-440 readiness gates."""
    checks = (
        _deployment_surface_check(config),
        _collector_placement_check(config),
        _otlp_endpoint_check(config),
        _secret_allowlist_check(config),
        _tier_secret_backend_check(config.provider_secrets),
    )
    return ReadinessReport(
        ready=all(check.ok for check in checks),
        checks=checks,
        roadmap_items=ROADMAP_ITEMS,
        note=(
            "Static readiness only: no daemon start, OTLP network probe, "
            "secret fetch, or backend call was performed."
        ),
    )


def load_report(config_file: Path | None) -> ReadinessReport:
    """Load runtime config through the production source layer, then evaluate it."""
    config = RuntimeConfigSource.load(config_file=config_file)
    return evaluate_config(config)


def _render_text(report: ReadinessReport) -> str:
    lines = [
        f"roadmap_items: {', '.join(report.roadmap_items)}",
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
        "--config",
        type=Path,
        default=None,
        help="Runtime config file. Defaults to RuntimeConfigSource harness.toml discovery.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        report = load_report(args.config)
    except RuntimeConfigLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
