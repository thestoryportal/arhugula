#!/usr/bin/env python3
"""Non-mutating readiness checks for R-421 MANAGED_CLOUD deployment work.

This probe validates the static runtime configuration and currently-landed
runtime substrate only. It does not start the daemon, probe OTLP, fetch secrets,
install SDKs, or call a managed-cloud provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from harness_as.secret_fetch import SecretScope
from harness_as.tool_contract import SecretAllowlistEntry
from harness_core import DeploymentSurface
from harness_od.observability_matrix import CellID
from harness_od.per_cell_collector_placement_matrix import (
    CollectorPlacement,
    collector_placement,
)
from harness_od.per_sandbox_tier_otlp_reachability import (
    ReachabilityViolation,
    assert_otlp_reachable_from_sandbox,
)
from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource
from harness_runtime.types import ProviderSecretBackend, RuntimeConfig

E2B_SECRET_NAME = "e2b-secret"


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


ROADMAP_ITEMS = ("R-421-managed-cloud-deployment-e2e",)
LOOPBACK_OTLP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MANAGED_CLOUD_SECRET_BACKENDS: frozenset[ProviderSecretBackend] = frozenset(
    {ProviderSecretBackend.GCP_SECRET_MANAGER}
)
"""Provider-secret backend enum members that implement cloud secret resolution."""


def _format_allowed(values: Iterable[CollectorPlacement]) -> str:
    return ", ".join(sorted(value.value for value in values))


def _deployment_surface_check(config: RuntimeConfig) -> CheckResult:
    ok = config.deployment_surface is DeploymentSurface.MANAGED_CLOUD
    return CheckResult(
        name="deployment-surface",
        ok=ok,
        detail=(
            "deployment_surface is managed-cloud"
            if ok
            else f"expected managed-cloud; observed {config.deployment_surface.value}"
        ),
    )


def _collector_placement_check(config: RuntimeConfig) -> CheckResult:
    cell = CellID(
        persona_tier=config.persona_tier,
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
    )
    allowed = collector_placement(cell)
    observed = config.collector.placement
    ok = config.deployment_surface is DeploymentSurface.MANAGED_CLOUD and observed in allowed
    return CheckResult(
        name="collector-placement",
        ok=ok,
        detail=(
            f"collector placement {observed.value} is valid for "
            f"{cell.persona_tier.value} MANAGED_CLOUD"
            if ok
            else (
                f"expected one of {_format_allowed(allowed)} for "
                f"{cell.persona_tier.value} MANAGED_CLOUD; observed {observed.value}"
            )
        ),
    )


def _managed_otlp_endpoint_check(config: RuntimeConfig) -> CheckResult:
    parsed = urlparse(config.otel.otlp_endpoint)
    host = parsed.hostname or ""
    has_endpoint = bool(parsed.scheme and parsed.netloc)
    is_loopback = host.lower() in LOOPBACK_OTLP_HOSTS
    ok = has_endpoint and not is_loopback
    return CheckResult(
        name="managed-otlp-endpoint",
        ok=ok,
        detail=(
            f"managed OTLP endpoint is configured at {config.otel.otlp_endpoint}"
            if ok
            else (
                "managed-cloud OTLP endpoint must include scheme/host and must "
                f"not be loopback; observed {config.otel.otlp_endpoint}"
            )
        ),
    )


def _bootstrap_reachability_check(config: RuntimeConfig) -> CheckResult:
    tier = config.collector.bootstrap_sandbox_tier
    placement = config.collector.placement
    try:
        assert_otlp_reachable_from_sandbox(tier, placement)
    except ReachabilityViolation as exc:
        return CheckResult(
            name="bootstrap-reachability",
            ok=False,
            detail=(
                f"bootstrap_sandbox_tier {tier.value} cannot reach collector placement "
                f"{placement.value}: {exc}"
            ),
        )
    return CheckResult(
        name="bootstrap-reachability",
        ok=True,
        detail=(
            f"bootstrap_sandbox_tier {tier.value} can reach collector placement {placement.value}"
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


def _cloud_secret_backend_check(config: RuntimeConfig) -> CheckResult:
    backend = config.provider_secrets.backend
    ok = backend in MANAGED_CLOUD_SECRET_BACKENDS
    landed = ", ".join(sorted(value.value for value in ProviderSecretBackend))
    return CheckResult(
        name="cloud-secret-backend",
        ok=ok,
        detail=(
            f"provider-secret backend {backend.value} is managed-cloud capable"
            if ok
            else (
                f"provider-secret backend {backend.value} is not managed-cloud capable; "
                f"currently landed selectors: {landed}"
            )
        ),
    )


def _e2b_candidate_check(config: RuntimeConfig, *, provider: str | None) -> CheckResult:
    if provider is None:
        return CheckResult(
            name="hosted-sandbox-provider",
            ok=False,
            detail=(
                "no hosted sandbox provider selected; E2B can be checked with "
                "--hosted-sandbox-provider e2b"
            ),
        )
    if provider != "e2b":
        return CheckResult(
            name="hosted-sandbox-provider",
            ok=False,
            detail=f"unsupported hosted sandbox provider {provider!r}; expected 'e2b'",
        )

    has_key = any(
        entry.name == E2B_SECRET_NAME for entry in config.provider_secrets.operator_allowlist
    )
    return CheckResult(
        name="hosted-sandbox-provider",
        ok=has_key,
        detail=(
            f"E2B selected and provider_secrets.operator_allowlist includes {E2B_SECRET_NAME}"
            if has_key
            else f"E2B selected but provider_secrets.operator_allowlist lacks {E2B_SECRET_NAME}"
        ),
    )


def evaluate_config(
    config: RuntimeConfig,
    *,
    hosted_sandbox_provider: str | None = None,
) -> ReadinessReport:
    """Evaluate a loaded config against the static R-421 readiness gates."""
    checks = (
        _deployment_surface_check(config),
        _collector_placement_check(config),
        _managed_otlp_endpoint_check(config),
        _bootstrap_reachability_check(config),
        _secret_allowlist_check(config),
        _cloud_secret_backend_check(config),
        _e2b_candidate_check(config, provider=hosted_sandbox_provider),
    )
    return ReadinessReport(
        ready=all(check.ok for check in checks),
        checks=checks,
        roadmap_items=ROADMAP_ITEMS,
        note=(
            "Static readiness only: no daemon start, OTLP network probe, "
            "secret fetch, SDK install, or managed-cloud provider call was performed."
        ),
    )


def load_report(
    config_file: Path | None,
    *,
    hosted_sandbox_provider: str | None = None,
) -> ReadinessReport:
    """Load runtime config through the production source layer, then evaluate it."""
    config = RuntimeConfigSource.load(config_file=config_file)
    return evaluate_config(config, hosted_sandbox_provider=hosted_sandbox_provider)


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
    parser.add_argument(
        "--hosted-sandbox-provider",
        choices=("e2b",),
        default=None,
        help="Optional hosted sandbox candidate selected for R-421.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        report = load_report(
            args.config,
            hosted_sandbox_provider=args.hosted_sandbox_provider,
        )
    except RuntimeConfigLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0 if report.ready else 1


def e2b_secret_allowlist_entry() -> SecretAllowlistEntry:
    """Return the convention used by the R-421 E2B config template/tests."""
    return SecretAllowlistEntry(name=E2B_SECRET_NAME, scope=SecretScope(name="r421-managed-cloud"))


if __name__ == "__main__":
    raise SystemExit(main())
