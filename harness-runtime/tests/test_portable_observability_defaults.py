"""Regression: the self-hosted-local observability stack publishes loopback only.

The shipped compose file backs a single-operator localhost stack
(``deploy/self-hosted-local/README.md``). Every published host port must bind
``127.0.0.1`` so Tempo, the OTLP collector, and Grafana are not exposed on
external interfaces. Container-to-container traffic uses the compose network and
is unaffected by these host bindings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "self-hosted-local" / "compose.yaml"

EXPECTED_PUBLISHED_PORTS: dict[str, list[str]] = {
    "tempo": ["127.0.0.1:3200:3200"],
    "otel-collector": ["127.0.0.1:4317:4317", "127.0.0.1:4318:4318"],
    "grafana": ["127.0.0.1:3000:3000"],
}


def _services() -> dict[str, Any]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]
    assert isinstance(services, dict)
    return services


@pytest.mark.parametrize(
    ("service", "expected"),
    sorted(EXPECTED_PUBLISHED_PORTS.items()),
    ids=sorted(EXPECTED_PUBLISHED_PORTS),
)
def test_service_publishes_exact_loopback_ports(service: str, expected: list[str]) -> None:
    ports = _services()[service].get("ports")
    assert ports == expected


def test_only_expected_services_publish_ports_and_all_bind_loopback() -> None:
    publishing = {name: svc["ports"] for name, svc in _services().items() if svc.get("ports")}
    assert set(publishing) == set(EXPECTED_PUBLISHED_PORTS)
    for name, ports in publishing.items():
        for entry in ports:
            assert isinstance(entry, str), f"{name}: non-string port entry {entry!r}"
            assert entry.startswith("127.0.0.1:"), f"{name}: non-loopback publish {entry!r}"
