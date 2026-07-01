"""U-RT-71 — RuntimeConfig schema extension: trust_policy + sandbox_decision_policy.

Tests per Implementation_Plan_Harness_Runtime_v2_13.md §1 U-RT-71 acceptance
criteria. Spec contract: Spec_Harness_Runtime_v1.md v1.16 §3 C-RT-02 field-table
extension. Both fields default `None`; the stage-5 factory (U-RT-75) supplies
type defaults when unset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_core import SandboxDecisionPolicy
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.per_server_trust_types import (
    MCPTrustTier,
    TierDerivationRule,
    TrustPolicy,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from pydantic import ValidationError


def _required_kwargs() -> dict[str, Any]:
    return {
        "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
        "repository_root": Path("/tmp"),
        "path_bindings": PathBindingConfig(),
        "provider_secrets": ProviderSecretsConfig(),
        "otel": OTelConfig(otlp_endpoint="http://localhost:4318"),
        "collector": CollectorConfig(),
        "default_topology": TopologyPattern.SINGLE_THREADED_LINEAR,
    }


def _trust_policy() -> TrustPolicy:
    return TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={},
        allow_list=frozenset(),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )


def test_explicit_none_for_both_new_fields_instantiates() -> None:
    # AC #1 — explicit None for both new fields succeeds.
    cfg = RuntimeConfig(
        trust_policy=None,
        sandbox_decision_policy=None,
        **_required_kwargs(),
    )
    assert cfg.trust_policy is None
    assert cfg.sandbox_decision_policy is None


def test_backwards_compat_without_new_fields() -> None:
    # AC #2 — RuntimeConfig without new fields preserves v1.14-shape
    # backwards-compatibility; both fields default to None.
    cfg = RuntimeConfig(**_required_kwargs())
    assert cfg.trust_policy is None
    assert cfg.sandbox_decision_policy is None


def test_operator_supplied_trust_policy_stored_on_frozen_model() -> None:
    # AC #3 — operator-supplied TrustPolicy stored on frozen model.
    policy = _trust_policy()
    cfg = RuntimeConfig(trust_policy=policy, **_required_kwargs())
    assert cfg.trust_policy is policy


def test_operator_supplied_sandbox_decision_policy_stored() -> None:
    # AC #3 (extended to second new field) — operator-supplied SandboxDecisionPolicy
    # stored on frozen model.
    policy = SandboxDecisionPolicy.default()
    cfg = RuntimeConfig(sandbox_decision_policy=policy, **_required_kwargs())
    assert cfg.sandbox_decision_policy is policy


def test_type_mismatch_on_trust_policy_raises_validation_error() -> None:
    # AC #4 — wrong type for trust_policy raises typed ValidationError.
    with pytest.raises(ValidationError):
        RuntimeConfig(
            trust_policy="not_a_policy",  # type: ignore[arg-type]
            **_required_kwargs(),
        )


def test_type_mismatch_on_sandbox_decision_policy_raises_validation_error() -> None:
    # AC #4 (extended) — wrong type for sandbox_decision_policy raises ValidationError.
    with pytest.raises(ValidationError):
        RuntimeConfig(
            sandbox_decision_policy="not_a_policy",  # type: ignore[arg-type]
            **_required_kwargs(),
        )


def test_new_fields_importable_from_runtime_types() -> None:
    # AC #5 — importable; field metadata accessible.
    assert "trust_policy" in RuntimeConfig.model_fields
    assert "sandbox_decision_policy" in RuntimeConfig.model_fields
    # Both fields are optional (default None).
    assert RuntimeConfig.model_fields["trust_policy"].default is None
    assert RuntimeConfig.model_fields["sandbox_decision_policy"].default is None
