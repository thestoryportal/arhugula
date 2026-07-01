"""U-RT-96 — Webhook delivery composer config carrier (empty-marker sub-model).

Implements runtime spec v1.26 §14.16.1 (architectural surfaces introduced):

- `WebhookDeliveryComposerConfig`: operator-supplied webhook-delivery composer
  opt-in marker. Empty-marker `@dataclass(frozen=True)` per the
  `ValidatorFrameworkConfig` (runtime spec v1.18 §14.13.1) +
  `PauseResumeProtocolConfig` (v1.21 §14.14.1) precedents. Presence at
  `RuntimeConfig.webhook_delivery_composer_config` signals operator opt-in
  to the durable-async cell HITL webhook delivery surface; absence (`None`
  default at C-RT-02) signals operator opt-out and yields
  `ctx.webhook_delivery_composer is None`.

Internal operator-supply shape (per-endpoint URL, per-retry-policy,
per-idempotency-key-store substrate, outbound HTTP timeout, TLS/auth) is
deferred to implementation discretion at C-RT-26 landing arc per FM-2
no-extension discipline (spec v1.26 §14.16.1 + change-note adjacent
defect (i)).

Module sits parallel to `pause_resume_protocol_types.py` +
`validator_framework_types.py` + `memory_tool_types.py` under `lifecycle/`
per the §14.12 + §14.13 + §14.14 carrier-home precedent (RuntimeConfig
sub-models that pair with stage factories at runtime spec contracts live
in harness-runtime/lifecycle/).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WebhookDeliveryComposerConfig:
    """Operator-supplied webhook-delivery composer opt-in marker.

    Empty-marker at v1.26 authoring scope per spec §14.16.1. The carrier
    shape is intentionally empty; presence signals opt-in, absence (the
    `RuntimeConfig.webhook_delivery_composer_config = None` default) signals
    opt-out.
    """

    @classmethod
    def default(cls) -> WebhookDeliveryComposerConfig:
        """Return the empty-marker default instance.

        Equivalent to leaving `RuntimeConfig.webhook_delivery_composer_config = None`
        at the opt-out shape (which is the production-default state); the
        explicit `.default()` factory provides the empty marker for opt-in
        callers who want the no-endpoint-config baseline at v1.26.
        """
        return cls()
