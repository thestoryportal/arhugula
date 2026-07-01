"""U-RT-97 — `materialize_webhook_delivery_composer_stage` factory + stage-5 wiring tests.

ACs per runtime plan v2.25 §6.1 U-RT-97:

1. Factory authored with signature
   `async def materialize_webhook_delivery_composer_stage(config, ctx) → WebhookDeliveryComposer | None`.
2. Opt-out branch: `config.webhook_delivery_composer_config is None` → factory
   returns `None` unconditionally; ctx.webhook_delivery_composer binds to None
   at stage-5 LOOP_INIT completion (operator opt-out preserved per spec
   v1.26 §14.16.2).
3. Opt-in branch: factory returns the C-RT-20 §14.10.1 `WebhookDeliveryComposer`
   instance bound to `ctx.tracer_provider`.
4. Stage-5 LOOP_INIT wiring: factory invocation appended in
   `stage_5_loop_init.py`; output bound to `ctx.webhook_delivery_composer`
   per spec v1.26 §14.16.3 sibling-bucketing alongside
   `materialize_pause_resume_protocol_stage`.
5. `WebhookDeliveryComposerStageMaterializeError` typed exception authored;
   carries the `RT-FAIL-WEBHOOK-COMPOSER-STAGE-MATERIALIZE:` prefix per spec
   §14.16.4.
6. Importable; pyright strict-mode passes.
7. Sibling ordering within stage-5 LOOP_INIT is implementer-discretion (spec
   §14.16.3 + change-note adjacent defect (ii)) — observationally equivalent
   across orderings because the §14.8.8.1 step 0 OR-form precondition consumes
   both bindings' joint-presence regardless of stage-5 sub-order.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.bootstrap.factories.webhook_delivery_composer_factory import (
    WebhookDeliveryComposerStageMaterializeError,
    materialize_webhook_delivery_composer_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.webhook_delivery_composer import WebhookDeliveryComposer
from harness_runtime.lifecycle.webhook_delivery_composer_types import (
    WebhookDeliveryComposerConfig,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


def _minimal_runtime_config_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
        "repository_root": tmp_path,
        "path_bindings": PathBindingConfig(),
        "provider_secrets": ProviderSecretsConfig(),
        "otel": OTelConfig(otlp_endpoint="http://localhost:4318"),
        "collector": CollectorConfig(),
        "default_topology": TopologyPattern.SINGLE_THREADED_LINEAR,
    }


def _make_mutable_ctx_with_tracer_provider() -> _MutableHarnessContext:
    """Construct a _MutableHarnessContext with a non-None tracer_provider
    sentinel suitable for opt-in-branch factory invocation tests.

    The factory only forwards ctx.tracer_provider to the
    WebhookDeliveryComposer constructor; it does not invoke any method on it.
    """
    ctx = _MutableHarnessContext()
    ctx.tracer_provider = object()  # type: ignore[assignment]
    return ctx


# AC #1 — factory signature.


def test_factory_is_async() -> None:
    assert inspect.iscoroutinefunction(materialize_webhook_delivery_composer_stage)


def test_factory_signature_accepts_config_ctx() -> None:
    sig = inspect.signature(materialize_webhook_delivery_composer_stage)
    params = list(sig.parameters)
    assert params == ["config", "ctx"], (
        f"factory signature must be (config, ctx) per spec §14.16.2; got {params}"
    )


# AC #2 — opt-out branch.


@pytest.mark.asyncio
async def test_factory_returns_none_when_config_is_none(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        webhook_delivery_composer_config=None,
    )
    ctx = _make_mutable_ctx_with_tracer_provider()
    result = await materialize_webhook_delivery_composer_stage(config, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_factory_opt_out_does_not_validate_ctx_prerequisites(
    tmp_path: Path,
) -> None:
    """Opt-out branch returns None unconditionally; does NOT inspect ctx for
    tracer_provider prerequisites (mirrors pause_resume_protocol spec §14.14.5
    invariant 2 — empty-sentinel preserves backward compat)."""
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        webhook_delivery_composer_config=None,
    )
    ctx = _MutableHarnessContext()  # no tracer_provider populated
    result = await materialize_webhook_delivery_composer_stage(config, ctx)
    assert result is None


# AC #3 — opt-in branch.


@pytest.mark.asyncio
async def test_factory_returns_composer_when_config_present(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        webhook_delivery_composer_config=WebhookDeliveryComposerConfig.default(),
    )
    ctx = _make_mutable_ctx_with_tracer_provider()
    result = await materialize_webhook_delivery_composer_stage(config, ctx)
    assert result is not None
    assert isinstance(result, WebhookDeliveryComposer)


@pytest.mark.asyncio
async def test_factory_binds_tracer_provider_from_ctx(tmp_path: Path) -> None:
    """Opt-in factory forwards `ctx.tracer_provider` to the WebhookDeliveryComposer
    constructor per spec §14.16.2 ctx-consumption discipline."""
    sentinel_tracer = object()
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        webhook_delivery_composer_config=WebhookDeliveryComposerConfig.default(),
    )
    ctx = _MutableHarnessContext()
    ctx.tracer_provider = sentinel_tracer  # type: ignore[assignment]
    result = await materialize_webhook_delivery_composer_stage(config, ctx)
    assert result is not None
    # Composer carrier stores tracer_provider on the instance for span emission
    # (private attribute name per the v1.10.1 carrier class body).
    assert result._tracer_provider is sentinel_tracer  # type: ignore[attr-defined]


# AC #4 — stage-5 LOOP_INIT wiring.


def test_stage_5_loop_init_invokes_webhook_delivery_composer_factory() -> None:
    """Verify the stage-5 LOOP_INIT bootstrap module invokes the webhook factory
    + binds the result to ctx.webhook_delivery_composer per spec §14.16.3."""
    from harness_runtime.bootstrap import stage_5_loop_init

    assert stage_5_loop_init.__file__ is not None
    source = Path(stage_5_loop_init.__file__).read_text()
    assert "materialize_webhook_delivery_composer_stage" in source, (
        "stage-5 LOOP_INIT must import + invoke the webhook factory per "
        "spec §14.16.3 sibling-bucketing with pause_resume_protocol"
    )
    assert "ctx.webhook_delivery_composer" in source, (
        "stage-5 LOOP_INIT must bind factory output to "
        "ctx.webhook_delivery_composer per spec §14.16.3"
    )


# AC #5 — RT-FAIL-WEBHOOK-COMPOSER-STAGE-MATERIALIZE fail-class.


def test_webhook_delivery_composer_stage_materialize_error_is_exception_subclass() -> None:
    assert issubclass(WebhookDeliveryComposerStageMaterializeError, Exception)


def test_fail_class_prefix_documented_at_factory_source() -> None:
    """Verify the RT-FAIL-WEBHOOK-COMPOSER-STAGE-MATERIALIZE prefix is named at
    the factory module per spec §14.16.4 failure-mode taxonomy."""
    from harness_runtime.bootstrap.factories import webhook_delivery_composer_factory

    assert webhook_delivery_composer_factory.__file__ is not None
    source = Path(webhook_delivery_composer_factory.__file__).read_text()
    assert "RT-FAIL-WEBHOOK-COMPOSER-STAGE-MATERIALIZE" in source


# AC #7 — spec invariants.


@pytest.mark.asyncio
async def test_invariant_empty_sentinel_preserves_backward_compat(
    tmp_path: Path,
) -> None:
    """Spec §14.16.5 invariant analog: when operator does not supply config,
    ctx.webhook_delivery_composer binds to None and §14.8.8.1 step 0 OR-form
    precondition AND-arm evaluates False (pre-v1.26 production-default
    behavior preserved)."""
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        webhook_delivery_composer_config=None,
        pause_resume_protocol_config=None,
    )
    ctx = _MutableHarnessContext()
    webhook = await materialize_webhook_delivery_composer_stage(config, ctx)
    assert webhook is None, (
        "Pre-v1.26 production-default state requires ctx.webhook_delivery_composer "
        "is None when operator did not supply WebhookDeliveryComposerConfig"
    )
