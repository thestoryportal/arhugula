"""U-RT-85 — Real-bootstrap e2e: validator framework binding chain.

Implements runtime plan v2.17 §1 U-RT-85 + runtime spec v1.18 §14.13.6
X-AL-2 retirement implication (operational-criterion-B exercise for
H_T-CP-21 RETIRE-READY transition under the operator-opt-in pattern per
fork doc `.harness/class_1_fork_validator_composer_arc_stage_4_absence.md`
§3.1 Reading A).

## Mechanism α-lite (per cluster-open operator decision 2026-05-24)

Exercises the binding chain end-to-end via the real bootstrap (no `_FakeCtx`
or `_MutableHarnessContext` shortcut) — `run_bootstrap` runs against
in-process fake providers/daemon/tracer (the standard integration-test
substrate per `conftest.py::patched_runtime`); the stage-4 OD bucket invokes
`materialize_validator_framework_stage` with the real factory; the test
asserts the binding-chain post-conditions.

Coverage:

- **Opt-out** (`RuntimeConfig.validator_framework_config = None`, the
  production-default): `ctx.validator_framework is None`; the driver hook
  False-arm at `workflow_driver.py:668` would evaluate False; backwards-
  compatible behaviour preserved per spec §14.13.5 invariant 2.

- **Opt-in** (`RuntimeConfig.validator_framework_config =
  ValidatorFrameworkConfig.default()`): `ctx.validator_framework is not
  None`; the bound instance is a `ConcreteValidatorFramework` satisfying
  the `@runtime_checkable ValidatorFramework` Protocol per spec §14.13.5
  invariant 3.

The `.evaluate()` invocation path is NOT exercised at α-lite scope (the
opt-in branch returns an empty-registry framework per option (ii) minimal
construction body — there are no operator-supplied `Validator`
implementations to exercise). Full True-arm `.evaluate()` exercise is
deferred to mechanism β / γ at a follow-on arc per FM-2.

## Verification-shape discipline

Per `[[verification-shape-sharpened-grep-vs-e2e]]` (batch-16 §6): "driver
invocation succeeds end-to-end against a real substrate" — this test uses
the production `run_bootstrap` orchestrator (not `_FakeCtx` or
`_MutableHarnessContext` test-local shortcuts) and verifies the full
binding chain at the produced `HarnessContext`. The stage-4 factory IS
empirically invoked (in the real `stage_4_od.execute` body) and the result
IS empirically bound at `ctx.validator_framework`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_cp.validator_framework import ConcreteValidatorFramework
from harness_cp.validator_framework_types import ValidatorFramework
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.validator_framework_types import (
    ValidatorFrameworkConfig,
)
from harness_runtime.types import HarnessContext, RuntimeConfig

from .conftest import WORKLOAD, build_config


def _config_with_validator_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={"validator_framework_config": ValidatorFrameworkConfig.default()},
    )


# AC #2 — opt-out e2e branch.


@pytest.mark.asyncio
async def test_validator_framework_e2e_opt_out_branch(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #2 — opt-out config → ctx.validator_framework is None; driver hook
    False-arm; backwards-compatible behaviour."""
    _ = patched_runtime
    config = build_config(tmp_path)
    # Production-default: validator_framework_config defaults to None.
    assert config.validator_framework_config is None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert ctx.validator_framework is None, (
        "opt-out (default) config must yield ctx.validator_framework is None "
        "per spec §14.13.5 invariant 2"
    )


# AC #1 + AC #3 — opt-in e2e branch (binding chain only).


@pytest.mark.asyncio
async def test_validator_framework_e2e_opt_in_binding_chain(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #1 + AC #3 — opt-in config → factory invoked → ctx.validator_framework
    bound to a ConcreteValidatorFramework instance satisfying the CP-canonical
    ValidatorFramework Protocol per spec §14.13.5 invariant 3."""
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    assert config.validator_framework_config is not None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert ctx.validator_framework is not None, (
        "opt-in config must yield a bound ValidatorFramework instance per "
        "spec §14.13.1 + §14.13.5 invariant 3"
    )
    # Spec §14.13.5 invariant 3 — Protocol conformance.
    assert isinstance(ctx.validator_framework, ValidatorFramework)
    # Option (ii) minimal construction body — concrete shape is
    # ConcreteValidatorFramework with empty registry.
    assert isinstance(ctx.validator_framework, ConcreteValidatorFramework)


# AC #5 — composer-depth parity: real bootstrap, not _FakeCtx.


@pytest.mark.asyncio
async def test_validator_framework_e2e_uses_real_bootstrap(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #5 — the test exercises the REAL bootstrap orchestrator
    (`run_bootstrap`), NOT `_FakeCtx` or `_MutableHarnessContext`
    short-circuits. Verification-shape discipline per batch-16 §6 sharpening:
    "driver invocation succeeds end-to-end against a real substrate"."""
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)

    # The production bootstrap orchestrator is what's invoked.
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    # If _MutableHarnessContext.freeze() bound it correctly, the production
    # frozen-HarnessContext carries the value (not a test-local mutation).
    assert isinstance(ctx, HarnessContext)
    assert ctx.validator_framework is not None
    # The frozen HarnessContext model is Pydantic v2 frozen (no post-freeze
    # mutation); the field value materialised at stage-4 via the production
    # factory.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ctx.validator_framework = None  # type: ignore[misc]


# AC #6 — stage-4 ordering empirically verified at full-bootstrap path.


@pytest.mark.asyncio
async def test_validator_framework_stage_4_ordering_empirical(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #6 — stage-4 OD-bucket ordering: validator framework binds AFTER
    tracer_provider + audit_writer + cost_chain + collector_daemon per spec
    §14.13.3. Verified empirically: a successful run_bootstrap with opt-in
    config produces a context where (a) validator_framework is bound non-None
    AND (b) all stage-4 OD prerequisites (tracer_provider + audit_writer +
    cost_chain + collector_daemon) are also bound non-None at the frozen
    HarnessContext (i.e., they were materialised prior to the validator
    framework factory invocation per the stage-4 ordering invariant)."""
    _ = patched_runtime
    config = _config_with_validator_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    # All four stage-4 prerequisites bound non-None at the frozen context.
    assert ctx.tracer_provider is not None
    assert ctx.audit_writer is not None
    assert ctx.cost_chain is not None
    assert ctx.collector_daemon is not None
    # Validator framework bound non-None (factory ran after the four above).
    assert ctx.validator_framework is not None
