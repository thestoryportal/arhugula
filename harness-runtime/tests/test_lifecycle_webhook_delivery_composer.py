"""U-RT-69 — `WebhookDeliveryComposer` + `WebhookDeliveryResult`.

Per `Implementation_Plan_Harness_Runtime_v2_11.md` §1 U-RT-69 (5 ACs).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from harness_cp.hitl_timeout_degradation import (
    WebhookConfig,
    WebhookPayload,
)
from harness_runtime.lifecycle.webhook_delivery_composer import (
    ATTR_RETRY_ATTEMPT_NUMBER,
    ATTR_WEBHOOK_DELIVERY_ATTEMPTS,
    ATTR_WEBHOOK_IDEMPOTENCY_KEY,
    ATTR_WEBHOOK_STATUS_CODE,
    ATTR_WEBHOOK_URL_HASH,
    WebhookDeliveryComposer,
    WebhookDeliveryExhaustedError,
    WebhookDeliveryResult,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------- helpers --------------------------------------------------------


def _make_webhook_config(url: str = "https://example.test/hook") -> WebhookConfig:
    return WebhookConfig(
        webhook_id="wh-1",
        endpoint_url=url,
        timeout=5,
        degradation_mode="fail-closed",
    )


def _make_payload() -> WebhookPayload:
    return WebhookPayload(
        approval_id="approve-123",
        idempotency_key="idem-payload-1",
        gate_evaluation_ref="entry-1",
        payload_body={"prompt": "Approve?"},
    )


class _RecordingClient:
    """Test double for httpx.AsyncClient that returns scripted responses."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: Any):
        return None

    async def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> Any:
        _ = timeout
        self.requests.append((url, json, headers))
        if not self._responses:
            raise RuntimeError("test exhausted scripted responses")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # already a Response-shaped object


class _MockResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _async_noop_sleep_factory() -> tuple[Any, list[float]]:
    calls: list[float] = []

    async def noop(delay: float) -> None:
        calls.append(delay)

    return noop, calls


# ---------- AC #1 — delivers + retries -------------------------------------


@pytest.mark.asyncio
async def test_deliver_succeeds_first_attempt() -> None:
    client = _RecordingClient([_MockResponse(200)])
    composer = WebhookDeliveryComposer(
        retry_max_attempts=3,
        http_client_factory=lambda: client,
    )
    result = await composer.deliver_webhook(
        _make_webhook_config(),
        _make_payload(),
        "idem-1",
    )
    assert isinstance(result, WebhookDeliveryResult)
    assert result.delivered is True
    assert result.status_code == 200
    assert result.delivery_attempts == 1
    assert result.response_idempotency_key == "idem-1"
    assert client.requests[0][2]["Idempotency-Key"] == "idem-1"


@pytest.mark.asyncio
async def test_deliver_succeeds_after_retry() -> None:
    """AC #5: 3 attempts, 2 failures + 1 success → delivered=True attempts=3."""
    sleep_fn, sleep_calls = _async_noop_sleep_factory()
    # First two fail with 500; third returns 200.
    responses = [_MockResponse(500), _MockResponse(503), _MockResponse(200)]
    client = _RecordingClient(responses)
    composer = WebhookDeliveryComposer(
        retry_max_attempts=3,
        http_client_factory=lambda: client,
        sleep_fn=sleep_fn,
    )
    result = await composer.deliver_webhook(
        _make_webhook_config(),
        _make_payload(),
        "idem-retry",
    )
    assert result.delivered is True
    assert result.status_code == 200
    assert result.delivery_attempts == 3
    assert len(sleep_calls) == 2  # sleep between attempts 1→2, 2→3


# ---------- AC #4 — all retries failed raises EXHAUSTED --------------------


@pytest.mark.asyncio
async def test_deliver_all_attempts_fail_raises_exhausted() -> None:
    sleep_fn, _ = _async_noop_sleep_factory()
    client = _RecordingClient([_MockResponse(500), _MockResponse(500), _MockResponse(503)])
    composer = WebhookDeliveryComposer(
        retry_max_attempts=3,
        http_client_factory=lambda: client,
        sleep_fn=sleep_fn,
    )
    with pytest.raises(
        WebhookDeliveryExhaustedError,
        match="RT-FAIL-HITL-WEBHOOK-DELIVERY-EXHAUSTED",
    ):
        await composer.deliver_webhook(
            _make_webhook_config(),
            _make_payload(),
            "idem-fail",
        )


@pytest.mark.asyncio
async def test_deliver_transport_exception_treated_as_failure() -> None:
    """Connection errors count as failed attempts."""
    sleep_fn, _ = _async_noop_sleep_factory()
    client = _RecordingClient(
        [
            httpx.ConnectError("simulated connection refused"),
            _MockResponse(204),  # success
        ]
    )
    composer = WebhookDeliveryComposer(
        retry_max_attempts=3,
        http_client_factory=lambda: client,
        sleep_fn=sleep_fn,
    )
    result = await composer.deliver_webhook(
        _make_webhook_config(),
        _make_payload(),
        "idem-transport",
    )
    assert result.delivered is True
    assert result.status_code == 204
    assert result.delivery_attempts == 2


# ---------- AC #2 + #3 — span emission -------------------------------------


@pytest.mark.asyncio
async def test_deliver_emits_outer_and_attempt_spans() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    sleep_fn, _ = _async_noop_sleep_factory()
    client = _RecordingClient([_MockResponse(429), _MockResponse(200)])
    composer = WebhookDeliveryComposer(
        retry_max_attempts=3,
        http_client_factory=lambda: client,
        sleep_fn=sleep_fn,
        tracer_provider=provider,
    )
    await composer.deliver_webhook(
        _make_webhook_config(),
        _make_payload(),
        "idem-spans",
    )

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "hitl.webhook.deliver" in names
    # 2 attempt spans for 2 attempts.
    assert names.count("hitl.webhook.attempt") == 2

    outer = next(s for s in spans if s.name == "hitl.webhook.deliver")
    outer_attrs = dict(outer.attributes or {})
    assert ATTR_WEBHOOK_URL_HASH in outer_attrs
    assert outer_attrs[ATTR_WEBHOOK_IDEMPOTENCY_KEY] == "idem-spans"
    assert outer_attrs[ATTR_WEBHOOK_DELIVERY_ATTEMPTS] == 2

    attempt_spans = [s for s in spans if s.name == "hitl.webhook.attempt"]
    for span in attempt_spans:
        attrs = dict(span.attributes or {})
        assert ATTR_RETRY_ATTEMPT_NUMBER in attrs
        assert ATTR_WEBHOOK_STATUS_CODE in attrs


# ---------- factory --------------------------------------------------------


def test_factory_returns_composer_with_tracer_bound() -> None:
    # v1.26 §14.16.2 factory landed at bootstrap/factories/
    # webhook_delivery_composer_factory.py; this test verifies the carrier
    # construction shape against TracerProvider. The bootstrap factory unit
    # tests live at test_u_rt_97_webhook_delivery_composer_factory.py.
    provider = TracerProvider()
    composer = WebhookDeliveryComposer(tracer_provider=provider)
    assert isinstance(composer, WebhookDeliveryComposer)


# ---------- WebhookDeliveryResult carrier ----------------------------------


def test_webhook_delivery_result_frozen() -> None:
    result = WebhookDeliveryResult(
        delivered=True,
        status_code=200,
        response_idempotency_key="x",
        delivery_attempts=1,
        final_attempt_at=1234567890,
    )
    with pytest.raises(Exception):
        result.delivered = False  # type: ignore[misc]


# ---------- deliver_webhook_for_brief (Reading H) --------------------------
# Per runtime spec v1.34 §14.10.1 brief-surface absorption + fork doc
# `.harness/class_1_fork_webhook_composer_per_workflow_context_threading.md`
# Reading (H) operator-ratified 2026-05-28.


def _make_brief_for_composer_tests():
    from harness_cp.hitl_response_palette import HITLResponse
    from harness_cp.validator_framework_types import HITLEscalationBrief

    return HITLEscalationBrief(
        parent_step_id="step-1",
        parent_action_id="workflow:wf-test:step:0",
        fail_class=None,
        fail_detail_hash=None,
        escalation_reason="durable_async_cell_synchrony",
        proposed_response_palette=frozenset({HITLResponse.APPROVE}),
    )


@pytest.mark.asyncio
async def test_deliver_webhook_for_brief_raises_when_webhook_config_missing() -> None:
    """Reading H invariant: brief surface requires ctor-supplied webhook_config."""
    composer = WebhookDeliveryComposer(retry_max_attempts=1)  # no webhook_config
    brief = _make_brief_for_composer_tests()
    with pytest.raises(RuntimeError, match="webhook_config"):
        await composer.deliver_webhook_for_brief(brief, "idem-1")


@pytest.mark.asyncio
async def test_deliver_webhook_for_brief_dispatches_via_raw_surface() -> None:
    """Reading H projection: brief → payload → raw deliver_webhook."""
    client = _RecordingClient([_MockResponse(200)])
    composer = WebhookDeliveryComposer(
        retry_max_attempts=1,
        http_client_factory=lambda: client,
        webhook_config=_make_webhook_config("https://example.test/brief-surface"),
    )
    brief = _make_brief_for_composer_tests()
    result = await composer.deliver_webhook_for_brief(brief, "idem-brief-1")
    assert result.delivered is True
    assert result.status_code == 200
    # Verify the raw HTTP layer received the projected payload
    assert len(client.requests) == 1
    url, body, headers = client.requests[0]
    assert url == "https://example.test/brief-surface"
    assert headers["Idempotency-Key"] == "idem-brief-1"
    assert body["approval_id"] == "workflow:wf-test:step:0"
    assert body["gate_evaluation_ref"] == "workflow:wf-test:step:0"
    assert body["payload_body"]["escalation_reason"] == "durable_async_cell_synchrony"


@pytest.mark.asyncio
async def test_deliver_webhook_for_brief_propagates_exhausted_error() -> None:
    """Reading H exhaustion path: brief surface propagates raw surface exhaustion."""
    sleep_fn, _ = _async_noop_sleep_factory()
    client = _RecordingClient([_MockResponse(500), _MockResponse(500), _MockResponse(500)])
    composer = WebhookDeliveryComposer(
        retry_max_attempts=3,
        http_client_factory=lambda: client,
        sleep_fn=sleep_fn,
        webhook_config=_make_webhook_config(),
    )
    brief = _make_brief_for_composer_tests()
    with pytest.raises(WebhookDeliveryExhaustedError):
        await composer.deliver_webhook_for_brief(brief, "idem-exhaust-1")
