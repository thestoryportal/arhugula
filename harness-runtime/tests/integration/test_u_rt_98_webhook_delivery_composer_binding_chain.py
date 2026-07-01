"""U-RT-98 — Real-bootstrap e2e: webhook delivery composer binding chain.

Implements runtime plan v2.25 §6.1 U-RT-98 + runtime spec v1.26 §14.16.5
operator-opt-in RETIRE-READY pattern (binding-chain materialization
verification gate for Reading A path 1 fork-resolution arc).

## Mechanism α (default per FM-2 implementer-discretion)

Exercises the binding chain end-to-end via the real bootstrap (no `_FakeCtx`
or `_MutableHarnessContext` shortcut) — `run_bootstrap` runs against
in-process fake providers/daemon/tracer (the standard integration-test
substrate per `conftest.py::patched_runtime`); the stage-5 LOOP_INIT bucket
invokes `materialize_webhook_delivery_composer_stage` with the real factory;
the test asserts the binding-chain post-conditions.

Coverage:

- **Opt-out** (`RuntimeConfig.webhook_delivery_composer_config = None`, the
  production-default): `ctx.webhook_delivery_composer is None`; the
  §14.8.8.1 step 0 OR-form precondition AND-arm at
  `ctx.webhook_delivery_composer is None` evaluates False (durable-async
  branch falls through to sync-blocking); pre-v1.26 production-default
  behaviour preserved.

- **Opt-in** (`RuntimeConfig.webhook_delivery_composer_config =
  WebhookDeliveryComposerConfig.default()`):
  `ctx.webhook_delivery_composer is not None`; the bound instance is a
  C-RT-20 §14.10.1 `WebhookDeliveryComposer` carrier per spec §14.16.2.

- **Joint-binding substrate** (both pause_resume_protocol_config AND
  webhook_delivery_composer_config supplied): both fields bound non-None at
  the frozen HarnessContext — the substrate for §14.8.8.1 step 0 OR-form
  precondition to evaluate False (durable-async branch reachable). The
  composer-body durable-async branch exercise is at U-RT-95 e2e (Phase 3
  step 10); U-RT-98 verifies the binding substrate only.

The `.deliver_webhook(...)` invocation path is NOT exercised at α scope (the
empty-marker config has no operator-supplied endpoint substrate — only the
binding-chain materialisation is verified). Outbound HTTP exercise (with
operator-supplied endpoint config + retry policy) is deferred to mechanism
β / γ at a follow-on arc per FM-2 (§14.16.1 + change-note adjacent defect
(i)).

## Verification-shape discipline

Per `[[verification-shape-sharpened-grep-vs-e2e]]` (batch-16 §6 sharpening +
batch-17 U-RT-85 + batch-18 U-RT-89 application): "driver invocation
succeeds end-to-end against a real substrate" — this test uses the
production `run_bootstrap` orchestrator (not `_FakeCtx` or
`_MutableHarnessContext` test-local shortcuts) and verifies the full
binding chain at the produced `HarnessContext`. The stage-5 factory IS
empirically invoked (in the real `stage_5_loop_init.execute` body) and the
result IS empirically bound at `ctx.webhook_delivery_composer`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)
from harness_runtime.lifecycle.webhook_delivery_composer import WebhookDeliveryComposer
from harness_runtime.lifecycle.webhook_delivery_composer_types import (
    WebhookDeliveryComposerConfig,
)
from harness_runtime.types import HarnessContext, RuntimeConfig

from .conftest import WORKLOAD, build_config


def _config_with_webhook_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={"webhook_delivery_composer_config": WebhookDeliveryComposerConfig.default()},
    )


def _config_with_joint_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={
            "webhook_delivery_composer_config": WebhookDeliveryComposerConfig.default(),
            "pause_resume_protocol_config": PauseResumeProtocolConfig.default(),
        },
    )


# AC #2 — opt-out e2e branch.


@pytest.mark.asyncio
async def test_webhook_delivery_composer_e2e_opt_out_branch(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #2 — opt-out config → ctx.webhook_delivery_composer is None;
    §14.8.8.1 step 0 OR-form precondition AND-arm evaluates False (durable-
    async branch falls through to sync-blocking); pre-v1.26 production-
    default behaviour preserved."""
    _ = patched_runtime
    config = build_config(tmp_path)
    # Production-default: webhook_delivery_composer_config defaults to None.
    assert config.webhook_delivery_composer_config is None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert ctx.webhook_delivery_composer is None, (
        "opt-out (default) config must yield ctx.webhook_delivery_composer "
        "is None per spec §14.16.2 opt-out branch"
    )


# AC #1 + AC #3 — opt-in e2e branch (binding chain only).


@pytest.mark.asyncio
async def test_webhook_delivery_composer_e2e_opt_in_binding_chain(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #1 + AC #3 — opt-in config → factory invoked →
    ctx.webhook_delivery_composer bound to a C-RT-20 §14.10.1
    WebhookDeliveryComposer instance per spec §14.16.2 opt-in branch."""
    _ = patched_runtime
    config = _config_with_webhook_opt_in(tmp_path)
    assert config.webhook_delivery_composer_config is not None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert ctx.webhook_delivery_composer is not None, (
        "opt-in config must yield a bound WebhookDeliveryComposer instance per spec §14.16.2"
    )
    assert isinstance(ctx.webhook_delivery_composer, WebhookDeliveryComposer)


# AC #3 — joint-binding substrate (durable-async branch reachable).


@pytest.mark.asyncio
async def test_webhook_delivery_composer_e2e_joint_binding_substrate(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #3 — joint pause_resume + webhook opt-in: both bindings non-None at
    the frozen HarnessContext. Substrate condition for §14.8.8.1 step 0 OR-
    form precondition to evaluate False (durable-async branch reachable).
    The composer-body durable-async branch exercise is at U-RT-95 (Phase 3
    step 10); this test verifies only the binding substrate."""
    _ = patched_runtime
    config = _config_with_joint_opt_in(tmp_path)

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    # Both stage-5 LOOP_INIT bindings populated → §14.8.8.1 step 0 OR-form
    # precondition AND-arm evaluates True (durable-async branch reachable).
    assert ctx.pause_resume_protocol is not None, (
        "joint opt-in requires ctx.pause_resume_protocol bound"
    )
    assert ctx.webhook_delivery_composer is not None, (
        "joint opt-in requires ctx.webhook_delivery_composer bound"
    )


# AC #4 — composer-depth parity: real bootstrap, not _FakeCtx.


@pytest.mark.asyncio
async def test_webhook_delivery_composer_e2e_uses_real_bootstrap(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #4 — the test exercises the REAL bootstrap orchestrator
    (`run_bootstrap`), NOT `_FakeCtx` or `_MutableHarnessContext`
    short-circuits. Verification-shape discipline per batch-16 §6 sharpening
    + applied at batch-17 (U-RT-85) + batch-18 (U-RT-89): "driver invocation
    succeeds end-to-end against a real substrate"."""
    _ = patched_runtime
    config = _config_with_webhook_opt_in(tmp_path)

    # The production bootstrap orchestrator is what's invoked.
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    # If _MutableHarnessContext.freeze() bound it correctly, the production
    # frozen-HarnessContext carries the value (not a test-local mutation).
    assert isinstance(ctx, HarnessContext)
    assert ctx.webhook_delivery_composer is not None
    # The frozen HarnessContext model is Pydantic v2 frozen (no post-freeze
    # mutation); the field value materialised at stage-5 via the production
    # factory.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ctx.webhook_delivery_composer = None  # type: ignore[misc]


# AC #7 — freeze() propagation verification.


@pytest.mark.asyncio
async def test_webhook_delivery_composer_freeze_propagation(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #7 — freeze() at mutable_context.py propagates the
    webhook_delivery_composer field from _MutableHarnessContext builder to
    HarnessContext frozen surface. Verified empirically: opt-in config →
    frozen ctx carries non-None instance; opt-out config → frozen ctx
    carries None."""
    _ = patched_runtime
    # Opt-in branch.
    config_in = _config_with_webhook_opt_in(tmp_path)
    ctx_in = await run_bootstrap(config_in, workload_class=WORKLOAD)
    assert ctx_in.webhook_delivery_composer is not None

    # Opt-out branch.
    config_out = build_config(tmp_path)
    ctx_out = await run_bootstrap(config_out, workload_class=WORKLOAD)
    assert ctx_out.webhook_delivery_composer is None
