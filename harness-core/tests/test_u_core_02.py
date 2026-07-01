"""U-CORE-02 — SandboxDecisionPolicy empty-marker carrier tests.

Tests per Implementation_Plan_Harness_Core_v1_2.md §2 U-CORE-02 acceptance
criteria. The carrier is committed at runtime spec v1.16 §3 C-RT-02 as a
field type + `.default()` factory; no internal fields are spec-committed
(per X-AL-3 no silent design extension at Phase 7).
"""

from __future__ import annotations

import pytest
from harness_core import SandboxDecisionPolicy
from harness_core.sandbox_decision_policy import (
    SandboxDecisionPolicy as DirectImport,
)
from pydantic import BaseModel, ValidationError


def test_sandbox_decision_policy_is_pydantic_base_model() -> None:
    # AC #1 — frozen Pydantic v2 BaseModel with extra='forbid'.
    assert issubclass(SandboxDecisionPolicy, BaseModel)
    config = SandboxDecisionPolicy.model_config
    assert config.get("frozen") is True
    assert config.get("extra") == "forbid"


def test_sandbox_decision_policy_has_no_fields_at_v1_2() -> None:
    # AC #1 — empty-marker carrier; no fields declared at v1.2.
    assert SandboxDecisionPolicy.model_fields == {}


def test_sandbox_decision_policy_default_returns_instance() -> None:
    # AC #2 — `.default()` returns a SandboxDecisionPolicy instance.
    instance = SandboxDecisionPolicy.default()
    assert isinstance(instance, SandboxDecisionPolicy)


def test_sandbox_decision_policy_bare_construction_succeeds() -> None:
    # AC #3 — bare construction without kwargs succeeds.
    instance = SandboxDecisionPolicy()
    assert isinstance(instance, SandboxDecisionPolicy)


def test_sandbox_decision_policy_extra_field_rejected() -> None:
    # AC #4 — extra='forbid' rejects unknown kwargs.
    with pytest.raises(ValidationError):
        SandboxDecisionPolicy(extra_field="anything")  # type: ignore[call-arg]


def test_sandbox_decision_policy_frozen_attribute_assignment_raises() -> None:
    # AC #5 — frozen invariant: attribute assignment raises.
    instance = SandboxDecisionPolicy()
    with pytest.raises(ValidationError):
        instance.some_attr = "value"  # type: ignore[attr-defined]


def test_sandbox_decision_policy_importable_from_harness_core() -> None:
    # AC #6 — importable from package public API; identity preserved.
    assert SandboxDecisionPolicy is DirectImport


def test_sandbox_decision_policy_no_spec_extension_audit() -> None:
    # AC #8 — public surface limited to Pydantic-baseline + `.default()`.
    # Compare the carrier's public attributes against an empty BaseModel
    # subclass with the same config; the only delta should be `default`.

    class _EmptyBaseline(BaseModel):
        model_config = SandboxDecisionPolicy.model_config

    carrier_public = {name for name in dir(SandboxDecisionPolicy) if not name.startswith("_")}
    baseline_public = {name for name in dir(_EmptyBaseline) if not name.startswith("_")}
    delta = carrier_public - baseline_public
    assert delta == {"default"}, (
        "U-CORE-02 must not introduce any public surface beyond `.default()` "
        f"per X-AL-3 / AC #8; delta against empty BaseModel: {delta}"
    )
