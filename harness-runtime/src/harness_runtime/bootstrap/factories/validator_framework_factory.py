"""U-RT-84 — ValidatorFramework stage-4 factory.

Implements runtime spec v1.18 §14.13.1 factory signature + §14.13.2 per-factory
invocation discipline + §14.13.3 stage-4 OD-bucket placement + §14.13.4
failure-mode taxonomy + §14.13.5 invariants.

Reading A scope (per fork doc
`.harness/class_1_fork_validator_composer_arc_stage_4_absence.md` §3.1):

- Opt-out branch (`config.validator_framework_config is None`) returns `None`
  unconditionally — preserves v1.17 production-default behavior; the
  workflow_driver.py:668 hook branch evaluates False.
- Opt-in branch (non-None config) constructs an empty-registry
  `ConcreteValidatorFramework` per spec §14.13.7 implementer-discretion
  option (ii) minimal construction body (validator catalog mechanism deferred
  to a follow-on arc per FM-2 no-extension; the empty-marker
  `ValidatorFrameworkConfig` at v1.18 carries no operator-supplied validators).
- `@runtime_checkable ValidatorFramework` Protocol-conformance enforced per
  spec §14.13.5 invariant 3; failure raises
  `ValidatorFrameworkStageMaterializeError` (fail class
  `RT-FAIL-VALIDATOR-STAGE-MATERIALIZE`, permanent severity → bootstrap
  rollback per C-RT-02).
- U-RT-85 real-bootstrap ValidatorFramework e2e binds through this factory:
  opt-in config materializes the operator-supplied framework slot on the
  runtime context before the workflow-driver validator hook can fire.
"""

from __future__ import annotations

from harness_cp.validator_framework import ConcreteValidatorFramework
from harness_cp.validator_framework_types import (
    ValidatorFramework,
    ValidatorPostEvaluateHook,
)
from harness_od.rate_table_types import RateTable

from harness_runtime.lifecycle.cost_attribution_validator_dispatch import (
    CostAttributingValidatorHook,
)
from harness_runtime.lifecycle.cost_record_sink import SupportsCostRecordAppend
from harness_runtime.types import AuditLedgerWriter, CostAttributionChain, RuntimeConfig


class ValidatorFrameworkStageMaterializeError(Exception):
    """Raised when `materialize_validator_framework_stage` cannot produce a
    Protocol-satisfying `ValidatorFramework` instance.

    Fail class: `RT-FAIL-VALIDATOR-STAGE-MATERIALIZE` per spec v1.18 §14.13.4.
    Permanent severity — triggers bootstrap rollback of stages 0..3b per
    C-RT-02. Surfaces on opt-in branch only; opt-out branch returns `None`
    unconditionally and cannot raise this class.
    """


async def materialize_validator_framework_stage(
    config: RuntimeConfig,
    *,
    rate_table: RateTable | None = None,
    cost_chain: CostAttributionChain | None = None,
    audit_writer: AuditLedgerWriter | None = None,
    cost_record_sink: SupportsCostRecordAppend | None = None,
) -> ValidatorFramework | None:
    """Construct the stage-4 `ValidatorFramework` instance from operator-supplied
    config, or return `None` when the operator has not opted in.

    Per spec v1.18 §14.13.1 + §14.13.2.

    Returns
    -------
    ValidatorFramework | None
        `None` when `config.validator_framework_config is None` — the
        operator has not opted in; `ctx.validator_framework` binds to `None`;
        the workflow_driver.py:668 hook branch evaluates False (the v1.17
        production-default state).

        Non-`None` when the operator has supplied a `ValidatorFrameworkConfig`
        instance — Reading A scope returns an empty-registry
        `ConcreteValidatorFramework` per spec §14.13.7 implementer-discretion
        option (ii) minimal construction body. The empty-marker
        `ValidatorFrameworkConfig` at v1.18 carries no operator-supplied
        validators; richer construction (validator catalog, per-validator
        config, discovery) lands at a follow-on arc per FM-2.

    Raises
    ------
    ValidatorFrameworkStageMaterializeError
        Fail class `RT-FAIL-VALIDATOR-STAGE-MATERIALIZE` per spec §14.13.4 —
        if the constructed instance fails `@runtime_checkable ValidatorFramework`
        Protocol-conformance per spec §14.13.5 invariant 3.
    """
    if config.validator_framework_config is None:
        # Empty-sentinel branch. Operator opted out; ctx.validator_framework
        # binds to None; driver hook dead-branch at workflow_driver.py:668
        # is unreachable per the False arm. Preserves v1.17 production-default
        # behavior per spec §14.13.5 invariant 2.
        return None

    # Operator opt-in branch (option (ii) minimal construction body per spec
    # §14.13.7). v1.18 Reading A scope: the empty-marker
    # ValidatorFrameworkConfig carries no operator-supplied validators, so
    # the constructed framework has an empty validator registry. The
    # workflow_driver.py:668 True-arm fires per C-CP-25 §25.3.3.4 but the
    # framework's evaluate(...) method is invoked against an empty registry;
    # richer construction (populating the registry from operator-supplied
    # config) lands at a follow-on arc per §14.13.7.
    #
    # U-OD-40 hook binding (CP spec v1.24 §28.10 + this factory's mechanism
    # (a) per §28.10.5): when all 3 cost-attribution substrates are bound
    # (rate_table + cost_chain + audit_writer), construct the
    # CostAttributingValidatorHook and inject via ConcreteValidatorFramework's
    # optional post_evaluate_hook ctor param. If any substrate is None,
    # hook=None preserves pre-v1.24 behavior (cost attribution disabled).
    post_evaluate_hook: ValidatorPostEvaluateHook | None = None
    if rate_table is not None and cost_chain is not None and audit_writer is not None:
        post_evaluate_hook = CostAttributingValidatorHook(
            rate_table=rate_table,
            cost_chain=cost_chain,
            audit_writer=audit_writer,
            cost_record_sink=cost_record_sink,
        )

    framework: ValidatorFramework = ConcreteValidatorFramework(
        validator_registry={},
        post_evaluate_hook=post_evaluate_hook,
    )

    # Spec §14.13.5 invariant 3 — Protocol-conformance enforcement. The
    # isinstance check is statically redundant (the local is annotated
    # `ValidatorFramework`) but is a spec-mandated runtime @runtime_checkable
    # verification, so the pyright warning is intentionally suppressed.
    if not isinstance(framework, ValidatorFramework):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValidatorFrameworkStageMaterializeError(
            "RT-FAIL-VALIDATOR-STAGE-MATERIALIZE: constructed validator framework "
            "instance fails @runtime_checkable ValidatorFramework Protocol-conformance "
            "per spec v1.18 §14.13.5 invariant 3."
        )

    return framework
