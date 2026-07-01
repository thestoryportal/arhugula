"""U-RT-97 — WebhookDeliveryComposer stage-5 LOOP_INIT factory.

Implements runtime spec v1.26 §14.16.2 factory signature + §14.16.3 stage-5
LOOP_INIT placement + §14.16.4 failure-mode taxonomy + §14.16.5
operator-opt-in RETIRE-READY pattern.

Reading A path 1 absorption of fork
`.harness/class_1_fork_u_rt_94_webhook_delivery_composer_binding_chain_absence.md`
(operator-ratified 2026-05-24):

- Opt-out branch (`config.webhook_delivery_composer_config is None`) returns
  `None` unconditionally — preserves the pre-v1.26 production-default
  behavior; the §14.8.8.1 step 0 OR-form precondition AND-arm at
  `ctx.webhook_delivery_composer is None` falls through to sync-blocking.
- Opt-in branch (non-None config) constructs a `WebhookDeliveryComposer`
  instance per spec v1.26 §14.16.1 + the existing C-RT-20 §14.10.1 carrier
  class body at `lifecycle/webhook_delivery_composer.py:94`. The empty-marker
  `WebhookDeliveryComposerConfig` at v1.26 carries no operator-supplied
  endpoint configuration; richer construction (per-endpoint URL,
  per-retry-policy, idempotency-key-store substrate, outbound HTTP timeout,
  TLS/auth) lands at a follow-on arc per FM-2 no-extension discipline
  (§14.16.1 + change-note adjacent defect (i)).
- Construction failure raises `WebhookDeliveryComposerStageMaterializeError`
  (fail class `RT-FAIL-WEBHOOK-COMPOSER-STAGE-MATERIALIZE`, permanent severity
  → bootstrap rollback per C-RT-02).

Mirrors L9-decies validator_framework_factory + L9-undecies
pause_resume_protocol_factory module-shape precedent (per-factory module
under `bootstrap/factories/` with typed exception + async factory body +
opt-out short-circuit + opt-in construction body).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness_runtime.lifecycle.webhook_delivery_composer import WebhookDeliveryComposer
from harness_runtime.types import RuntimeConfig

if TYPE_CHECKING:
    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext


class WebhookDeliveryComposerStageMaterializeError(Exception):
    """Raised when `materialize_webhook_delivery_composer_stage` cannot produce
    a `WebhookDeliveryComposer` instance.

    Fail class: `RT-FAIL-WEBHOOK-COMPOSER-STAGE-MATERIALIZE` per spec v1.26
    §14.16.4. Permanent severity — triggers bootstrap rollback per C-RT-02
    (stages 0..4 + sibling stage-5 bindings already constructed). Surfaces
    on opt-in branch only; opt-out branch returns `None` unconditionally and
    cannot raise this class.
    """


async def materialize_webhook_delivery_composer_stage(
    config: RuntimeConfig,
    ctx: _MutableHarnessContext,
) -> WebhookDeliveryComposer | None:
    """Construct the stage-5 `WebhookDeliveryComposer` instance from
    operator-supplied config, or return `None` when the operator has not
    opted in.

    Per spec v1.26 §14.16.2 + §14.16.3.

    Parameters
    ----------
    config : RuntimeConfig
        Runtime config; `config.webhook_delivery_composer_config` is the
        operator opt-in signal.
    ctx : _MutableHarnessContext
        Mutable bootstrap context. Provides the `tracer_provider` carrier from
        stage 4 OD-bucket for span-attribute emission at the composer's
        `deliver_webhook(...)` body per C-RT-20 §14.10.1.

    Returns
    -------
    WebhookDeliveryComposer | None
        `None` when `config.webhook_delivery_composer_config is None` — the
        operator has not opted in; `ctx.webhook_delivery_composer` binds to
        `None`; the §14.8.8.1 step 0 OR-form precondition AND-arm at
        `ctx.webhook_delivery_composer is None` evaluates False (durable-async
        branch falls through to sync-blocking; pre-v1.26 production-default
        state preserved per spec §14.16.5 invariant analog).

        Non-`None` when the operator has supplied a
        `WebhookDeliveryComposerConfig` instance — returns the C-RT-20
        §14.10.1 `WebhookDeliveryComposer` instance bound to
        `ctx.tracer_provider` (when present at stage-5 invocation).

    Raises
    ------
    WebhookDeliveryComposerStageMaterializeError
        Fail class `RT-FAIL-WEBHOOK-COMPOSER-STAGE-MATERIALIZE` per spec
        §14.16.4. Empty-marker config at v1.26 has no operator-supplied
        substrate that can fail at construction; the exception class is
        registered defensively for the post-FM-2-extension landing arc when
        operator-supplied endpoint config can fail validation. Currently
        unreachable at v1.26 empty-marker scope.
    """
    if config.webhook_delivery_composer_config is None:
        # Empty-sentinel branch. Operator opted out;
        # ctx.webhook_delivery_composer binds to None; §14.8.8.1 step 0
        # OR-form precondition AND-arm at ctx.webhook_delivery_composer is
        # None evaluates False (durable-async branch falls through to
        # sync-blocking). Pre-v1.26 production-default state preserved.
        return None

    # Operator opt-in branch. Construct the C-RT-20 §14.10.1 carrier with
    # tracer_provider from ctx (stage 4 OD-bucket bound). Empty-marker config
    # carries no operator-supplied endpoint substrate at v1.26 per spec
    # §14.16.1 + change-note adjacent defect (i); richer construction lands at
    # a follow-on arc per FM-2.
    tracer_provider = ctx.tracer_provider
    # R-FS-1 arc CA — thread the run-scoped cost accumulator so webhook
    # SpanCostRecords feed `RunResult.cost_attribution` (runtime v1.53 §9). The
    # v1.26 empty-marker factory binds no cost substrates yet (pre-existing), so
    # the composer's cost wrapper early-returns in production — the sink is
    # forward-ready, dormant until the FM-2 webhook config arc binds substrates.
    return WebhookDeliveryComposer(
        tracer_provider=tracer_provider,
        # B-INTERSTEP-PERRUN-ISOLATION — the run-scoped accumulator PROXY (not its
        # `.records` list) so any appended SpanCostRecord routes to the current
        # run's accumulator at append-time.
        cost_record_sink=ctx.cost_record_accumulator,
    )
