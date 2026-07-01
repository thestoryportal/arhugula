"""U-RT-83 — Validator framework type carrier (empty-marker sub-model).

Implements runtime spec v1.18 §14.13.1 (architectural surfaces introduced):

- `ValidatorFrameworkConfig`: operator-supplied validator framework opt-in
  marker. Empty-marker `@dataclass(frozen=True)` per the
  `SandboxDecisionPolicy` precedent
  (`.harness/class_1_fork_sandbox_decision_policy_phantom_cite.md` Q1=C-i
  ratification 2026-05-22). Presence at
  `RuntimeConfig.validator_framework_config` signals operator opt-in to the
  validator framework; absence (`None` default at C-RT-02) signals operator
  opt-out and yields `ctx.validator_framework is None`.

Internal operator-supply shape (validator catalog mechanism, per-validator
config, validator-discovery) is deferred to implementation discretion at
C-RT-23 landing arc per FM-2 no-extension discipline (spec §14.13.7).

Module sits parallel to `memory_tool_types.py` under `lifecycle/` per the
§14.12 carrier-home precedent (RuntimeConfig sub-models that pair with stage
factories at runtime spec contracts live in harness-runtime/lifecycle/).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidatorFrameworkConfig:
    """Operator-supplied validator framework opt-in marker.

    Empty-marker at v1.18 Reading A scope per spec §14.13.1. The carrier
    shape is intentionally empty; presence signals opt-in, absence (the
    `RuntimeConfig.validator_framework_config = None` default) signals
    opt-out.
    """

    @classmethod
    def default(cls) -> ValidatorFrameworkConfig:
        """Return the empty-marker default instance.

        Equivalent to leaving `RuntimeConfig.validator_framework_config = None`
        at the opt-out shape (which is the production-default state); the
        explicit `.default()` factory provides the empty marker for opt-in
        callers who want the no-validator-registry baseline at v1.18.
        """
        return cls()
