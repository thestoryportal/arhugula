#!/usr/bin/env python3
"""Live R-430 tail-keep preservation e2e against the R-420 local collector stack.

This command is intentionally not part of CI. It expects the operator-owned
R-420 Docker Compose telemetry backend to be running locally. It emits two
SELF_HOSTED_SERVER traces through the real OTLP exporter:

* a trace containing the C-OD-10 §10.2 `sandbox.violation` classification
  trigger, which must be visible in Tempo; and
* a non-triggering trace, which must not be exported by the SDK-side
  tail-keep drop path when the root closes.

No hosted-provider inference, secrets, or paid calls are used.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPO_URL = "http://127.0.0.1:3200"
TRIGGER_ROOT = "r430.trigger.root"
TRIGGER_SPAN = "sandbox.violation"
PLAIN_ROOT = "r430.plain.root"
PLAIN_SPAN = "r430.plain.child"


class R430LiveE2EError(RuntimeError):
    """Raised for a failed R-430 live gate."""


def _print_step(message: str) -> None:
    print(f"[r430-live] {message}", flush=True)


def _trace_id_hex(trace_id: int) -> str:
    return f"{trace_id:032x}"


def _probe_tcp(url: str, *, timeout_seconds: float) -> None:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise R430LiveE2EError(f"endpoint has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((parsed.hostname, port), timeout=timeout_seconds):
        return None


def _iter_span_dicts(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        spans = payload.get("spans")
        if isinstance(spans, list):
            for item in spans:
                if isinstance(item, dict):
                    yield item
        for key in ("batches", "resourceSpans", "scopeSpans", "instrumentationLibrarySpans"):
            children = payload.get(key)
            if isinstance(children, list):
                for child in children:
                    yield from _iter_span_dicts(child)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_span_dicts(item)


def _tempo_span_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for span in _iter_span_dicts(payload):
        name = span.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _tempo_trace_payload(
    *,
    tempo_url: str,
    trace_id: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    trace_url = urljoin(tempo_url.rstrip("/") + "/", f"api/traces/{trace_id}")
    try:
        with urlopen(trace_url, timeout=timeout_seconds) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise R430LiveE2EError(f"Tempo trace query failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise R430LiveE2EError(f"Tempo trace query failed for {trace_id}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise R430LiveE2EError(f"Tempo trace query returned non-object JSON: {type(loaded)}")
    return loaded


def _wait_for_trace_names(
    *,
    tempo_url: str,
    trace_id: str,
    expected_names: set[str],
    timeout_seconds: float,
    query_interval_seconds: float,
) -> set[str]:
    deadline = time.monotonic() + timeout_seconds
    last_seen: set[str] = set()
    while time.monotonic() < deadline:
        payload = _tempo_trace_payload(
            tempo_url=tempo_url,
            trace_id=trace_id,
            timeout_seconds=min(query_interval_seconds, 5.0),
        )
        if payload is not None:
            last_seen = _tempo_span_names(payload)
            if expected_names <= last_seen:
                return last_seen
        time.sleep(query_interval_seconds)

    raise R430LiveE2EError(
        f"Tempo never exposed trace {trace_id} with spans {sorted(expected_names)}; "
        f"last_seen={sorted(last_seen)}"
    )


def _assert_trace_absent_for_window(
    *,
    tempo_url: str,
    trace_id: str,
    forbidden_names: set[str],
    window_seconds: float,
    query_interval_seconds: float,
) -> None:
    deadline = time.monotonic() + window_seconds
    while time.monotonic() < deadline:
        payload = _tempo_trace_payload(
            tempo_url=tempo_url,
            trace_id=trace_id,
            timeout_seconds=min(query_interval_seconds, 5.0),
        )
        if payload is not None:
            names = _tempo_span_names(payload)
            if names & forbidden_names:
                raise R430LiveE2EError(
                    f"non-trigger trace {trace_id} was exported; names={sorted(names)}"
                )
        time.sleep(query_interval_seconds)


def _validate_config_for_deterministic_r430(config: Any) -> None:
    from harness_core import DeploymentSurface
    from harness_od.base_rate_set_and_envelope import PER_CELL_BASE_RATE_ENVELOPE
    from harness_od.observability_matrix import CellID

    if config.deployment_surface == DeploymentSurface.LOCAL_DEVELOPMENT:
        raise R430LiveE2EError("R-430 requires a non-LOCAL deployment surface")

    cell = CellID(
        persona_tier=config.persona_tier,
        deployment_surface=config.deployment_surface,
    )
    default_rate = PER_CELL_BASE_RATE_ENVELOPE[cell].default_rate
    if default_rate != 1.0:
        raise R430LiveE2EError(
            "R-430 live proof is deterministic only at base_rate=1.0; "
            f"{cell} default_rate={default_rate}"
        )


def _emit_trigger_trace(config: Any, *, flush_timeout_millis: int) -> str:
    from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
    from harness_runtime.lifecycle.tracer_provider import materialize_tracer_provider_stage

    provider_stage = materialize_tracer_provider_stage(config, register_globally=False)
    processor_stage = materialize_span_processor_stage(config, provider_stage.provider)
    if processor_stage.tail_keep_processor is None:
        raise R430LiveE2EError("tail-keep wrapper was not engaged for non-LOCAL config")

    tracer = provider_stage.provider.get_tracer("r430-tail-keep-live")
    try:
        with tracer.start_as_current_span(TRIGGER_ROOT) as root:
            trace_id = _trace_id_hex(root.get_span_context().trace_id)
            with tracer.start_as_current_span(TRIGGER_SPAN):
                pass
        if not processor_stage.flush(timeout_millis=flush_timeout_millis):
            raise R430LiveE2EError("trigger trace force_flush timed out")
        return trace_id
    finally:
        provider_stage.provider.shutdown()


def _emit_plain_trace(config: Any, *, flush_timeout_millis: int) -> str:
    from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
    from harness_runtime.lifecycle.tracer_provider import materialize_tracer_provider_stage

    provider_stage = materialize_tracer_provider_stage(config, register_globally=False)
    processor_stage = materialize_span_processor_stage(config, provider_stage.provider)
    if processor_stage.tail_keep_processor is None:
        raise R430LiveE2EError("tail-keep wrapper was not engaged for non-LOCAL config")

    tracer = provider_stage.provider.get_tracer("r430-tail-keep-live")
    try:
        with tracer.start_as_current_span(PLAIN_ROOT) as root:
            trace_id = _trace_id_hex(root.get_span_context().trace_id)
            with tracer.start_as_current_span(PLAIN_SPAN):
                pass
        if not processor_stage.flush(timeout_millis=flush_timeout_millis):
            raise R430LiveE2EError("plain trace force_flush timed out")
        return trace_id
    finally:
        provider_stage.provider.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="SELF_HOSTED_SERVER harness config")
    parser.add_argument("--tempo-url", default=DEFAULT_TEMPO_URL)
    parser.add_argument("--positive-timeout", type=float, default=30.0)
    parser.add_argument("--negative-window", type=float, default=10.0)
    parser.add_argument("--query-interval", type=float, default=1.0)
    parser.add_argument("--flush-timeout-millis", type=int, default=30_000)
    args = parser.parse_args(argv)

    config_path = args.config.resolve()
    try:
        from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource

        _print_step("loading deterministic non-LOCAL config")
        config = RuntimeConfigSource.load(config_file=config_path)
        _validate_config_for_deterministic_r430(config)

        _print_step("probing local OTLP exporter and Tempo TCP endpoints")
        _probe_tcp(config.otel.otlp_endpoint, timeout_seconds=5)
        _probe_tcp(args.tempo_url, timeout_seconds=5)

        _print_step("emitting classification-trigger trace through OTLP")
        trigger_trace_id = _emit_trigger_trace(
            config,
            flush_timeout_millis=args.flush_timeout_millis,
        )
        trigger_names = _wait_for_trace_names(
            tempo_url=args.tempo_url,
            trace_id=trigger_trace_id,
            expected_names={TRIGGER_ROOT, TRIGGER_SPAN},
            timeout_seconds=args.positive_timeout,
            query_interval_seconds=args.query_interval,
        )

        _print_step("emitting non-trigger trace through OTLP")
        plain_trace_id = _emit_plain_trace(
            config,
            flush_timeout_millis=args.flush_timeout_millis,
        )
        _assert_trace_absent_for_window(
            tempo_url=args.tempo_url,
            trace_id=plain_trace_id,
            forbidden_names={PLAIN_ROOT, PLAIN_SPAN},
            window_seconds=args.negative_window,
            query_interval_seconds=args.query_interval,
        )
    except (R430LiveE2EError, RuntimeConfigLoadError, ModuleNotFoundError, OSError) as exc:
        print(f"R-430 live e2e failed: {exc}", file=sys.stderr)
        return 1

    _print_step(
        "completed: "
        f"trigger_trace_id={trigger_trace_id} "
        f"trigger_spans={sorted(trigger_names)} "
        f"plain_trace_id={plain_trace_id} "
        "trigger-trace-preserved=true "
        "non-trigger-trace-exported=false "
        "cost=0 hosted-provider-calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
