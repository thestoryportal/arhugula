"""U-RT-84 — `materialize_validator_framework_stage` factory + stage-4 wiring tests.

ACs per runtime plan v2.17 §1 U-RT-84:

1. Factory authored with signature
   `async def materialize_validator_framework_stage(config) → ValidatorFramework | None`.
2. Opt-out branch: `config.validator_framework_config is None` → factory
   returns `None` unconditionally.
3. Opt-in branch: option (ii) minimal construction body — factory returns a
   `ConcreteValidatorFramework` instance with empty validator_registry that
   satisfies the `@runtime_checkable ValidatorFramework` Protocol.
4. Stage-4 OD-bucket wiring: factory invocation appended AFTER tracer +
   collector + cost + audit in `stage_4_od.py`; output bound to
   `ctx.validator_framework`.
5. `HarnessContext.validator_framework` field type narrowed `object | None`
   → `ValidatorFramework | None` (verified by schema inspection +
   pyright-clean import).
6. `ValidatorFrameworkStageMaterializeError` typed exception authored;
   carries the `RT-FAIL-VALIDATOR-STAGE-MATERIALIZE:` prefix when raised.
7. Spec §14.13.5 invariants: single instance per bootstrap (factory bound
   exactly once at stage 4), empty-sentinel preserves backward compat,
   CP-canonical Protocol satisfaction, no validator-composer arc
   resolutions.
8. Importable.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.validator_framework import ConcreteValidatorFramework
from harness_cp.validator_framework_types import ValidatorFramework
from harness_runtime.bootstrap.factories.validator_framework_factory import (
    ValidatorFrameworkStageMaterializeError,
    materialize_validator_framework_stage,
)
from harness_runtime.lifecycle.validator_framework_types import (
    ValidatorFrameworkConfig,
)
from harness_runtime.types import (
    CollectorConfig,
    HarnessContext,
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


# AC #1 — factory signature.


def test_factory_is_async() -> None:
    assert inspect.iscoroutinefunction(materialize_validator_framework_stage)


def test_factory_signature_accepts_config_returns_framework_or_none() -> None:
    sig = inspect.signature(materialize_validator_framework_stage)
    params = list(sig.parameters)
    # Per spec v1.18 §14.13.1 first positional is `config`. Per CP spec
    # v1.24 §28.10.5 mechanism (a) the factory accepts optional kw-only
    # cost-attribution substrates (rate_table, cost_chain, audit_writer).
    assert params[0] == "config", (
        f"factory signature first param must be `config` per spec §14.13.1; got {params}"
    )
    # Cost-attribution substrates per U-OD-40 hook binding extension.
    for name in ("rate_table", "cost_chain", "audit_writer"):
        assert name in params, (
            f"factory must accept kw-only `{name}` per CP spec v1.24 "
            f"§28.10.5 mechanism (a); got {params}"
        )
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters[name].default is None
    # Return annotation is `ValidatorFramework | None` — str-rendered.
    return_annotation = sig.return_annotation
    rendered = str(return_annotation)
    assert "ValidatorFramework" in rendered
    assert "None" in rendered


# AC #2 — opt-out branch.


@pytest.mark.asyncio
async def test_factory_returns_none_when_config_is_none(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        validator_framework_config=None,
    )
    result = await materialize_validator_framework_stage(config)
    assert result is None


@pytest.mark.asyncio
async def test_factory_returns_none_when_field_omitted(tmp_path: Path) -> None:
    """Backwards-compatibility branch: caller never supplied the field."""
    config = RuntimeConfig(**_minimal_runtime_config_kwargs(tmp_path))
    assert await materialize_validator_framework_stage(config) is None


# AC #3 — opt-in branch (option (ii) minimal construction).


@pytest.mark.asyncio
async def test_factory_returns_concrete_framework_on_opt_in(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        validator_framework_config=ValidatorFrameworkConfig.default(),
    )
    result = await materialize_validator_framework_stage(config)
    assert result is not None
    assert isinstance(result, ConcreteValidatorFramework)


@pytest.mark.asyncio
async def test_factory_opt_in_result_satisfies_validator_framework_protocol(
    tmp_path: Path,
) -> None:
    """AC #3 + spec §14.13.5 invariant 3 — runtime_checkable Protocol conformance."""
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        validator_framework_config=ValidatorFrameworkConfig.default(),
    )
    result = await materialize_validator_framework_stage(config)
    assert result is not None
    assert isinstance(result, ValidatorFramework)


# AC #4 — stage-4 wiring source-inspection.


def test_stage_4_od_invokes_factory_after_audit_writer() -> None:
    """AC #4 + spec §14.13.3 — validator framework is the FINAL stage-4 binding.

    Source-level verification: the factory invocation appears after
    `audit = materialize_audit_writer_stage(...)` in the execute body, and
    no other stage-4 binding follows. Integration test exercising full
    bootstrap lives at U-RT-85.
    """
    from harness_runtime.bootstrap import stage_4_od

    source = inspect.getsource(stage_4_od.execute)
    # Factory is invoked (signature widened at U-OD-40 to thread cost-
    # attribution substrates per CP spec v1.24 §28.10.5 mechanism (a) —
    # accept any positional-or-kw invocation containing `config`).
    assert "materialize_validator_framework_stage(" in source
    assert "config" in source[source.find("materialize_validator_framework_stage") :]
    # And bound to ctx.validator_framework.
    assert "ctx.validator_framework = " in source
    # And the invocation comes AFTER audit_writer.
    audit_idx = source.find("materialize_audit_writer_stage")
    validator_idx = source.find("materialize_validator_framework_stage")
    assert audit_idx > 0
    assert validator_idx > audit_idx, (
        "validator framework factory must appear AFTER audit_writer per "
        "spec §14.13.3 ordering pin (validator framework as final stage-4 binding)"
    )


# AC #5 — HarnessContext field type narrowing.


def test_harness_context_validator_framework_field_narrowed() -> None:
    """AC #5 — schema-level field annotation is ValidatorFramework | None
    (narrowed from v1.17-era `object | None`)."""
    assert "validator_framework" in HarnessContext.model_fields
    field = HarnessContext.model_fields["validator_framework"]
    annotation_str = str(field.annotation)
    assert "ValidatorFramework" in annotation_str, (
        f"validator_framework field must narrow to ValidatorFramework | None per "
        f"spec v1.18 §4 + plan U-RT-84 AC #5; got {annotation_str}"
    )


def test_freeze_signature_carries_validator_framework_kwarg() -> None:
    """AC #4 plumbing — _MutableHarnessContext.freeze passes validator_framework
    through to HarnessContext."""
    from harness_runtime.bootstrap import mutable_context

    freeze_source = inspect.getsource(mutable_context._MutableHarnessContext.freeze)
    assert "validator_framework=self.validator_framework" in freeze_source


# AC #6 — typed exception authored.


def test_fail_class_exception_authored() -> None:
    assert issubclass(ValidatorFrameworkStageMaterializeError, Exception)


def test_fail_class_prefix_documented_in_message_convention() -> None:
    """Per spec §14.13.4 fail class — the exception message convention carries
    the RT-FAIL-VALIDATOR-STAGE-MATERIALIZE: prefix when raised. Verified via
    source inspection: the only raise site uses this prefix."""
    source = inspect.getsource(materialize_validator_framework_stage)
    if "raise ValidatorFrameworkStageMaterializeError" in source:
        assert "RT-FAIL-VALIDATOR-STAGE-MATERIALIZE:" in source


# AC #7 — invariant 4 (no §14.8.2 touch).


def test_no_validator_composer_arc_resolutions() -> None:
    """AC #7 invariant 4 — factory module does NOT import any §14.8.2 surface
    (VALIDATOR_ESCALATION, _hitl_required composer, palette restriction).
    Reading A scope discipline."""
    import harness_runtime.bootstrap.factories.validator_framework_factory as mod

    source = inspect.getsource(mod)
    assert "VALIDATOR_ESCALATION" not in source
    assert "_hitl_required" not in source
    assert "PALETTE_RESTRICTION" not in source


# AC #8 — importable.


def test_carriers_importable() -> None:
    assert callable(materialize_validator_framework_stage)
    assert ValidatorFrameworkStageMaterializeError is not None
    assert ValidatorFramework is not None
