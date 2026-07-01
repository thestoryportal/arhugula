"""Tests for U-AS-22 — allowlist-intersection access control (C-AS-06 §6.1-§6.2)."""

from __future__ import annotations

import inspect

from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.secret_allowlist import AllowlistDecision, check_secret_allowlist
from harness_as.secret_fetch import SecretScope
from harness_as.tool_contract import SecretAllowlistEntry, ToolContract

_SCOPE = SecretScope(name="prod")
_ENTRY = SecretAllowlistEntry(name="ANTHROPIC_API_KEY", scope=_SCOPE)


def _tool(*required: SecretAllowlistEntry) -> ToolContract:
    return ToolContract(
        name="echo",
        description="echo tool",
        input_schema={},
        output_schema={},
        minimum_tier=SandboxTier.TIER_2_CONTAINER,
        blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
        required_secrets=required,
    )


def test_secret_allowlist_entry_two_fields_only() -> None:
    """Acceptance #1 — SecretAllowlistEntry carries exactly two fields (§6.1)."""
    assert set(SecretAllowlistEntry.model_fields) == {"name", "scope"}


def test_required_secrets_empty_list_permitted() -> None:
    """Acceptance #2/#5 — an empty required_secrets list is permitted."""
    tool = _tool()
    assert tool.required_secrets == ()


def test_required_secrets_missing_field_treated_as_empty() -> None:
    """Acceptance #2 — omitted required_secrets defaults to empty."""
    tool = ToolContract(
        name="echo",
        description="echo tool",
        input_schema={},
        output_schema={},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
    )
    assert tool.required_secrets == ()


def test_check_allowlist_permitted_when_in_both_sets() -> None:
    """Acceptance #3 — PERMITTED iff (name, scope) is in tool ∩ operator override."""
    decision = check_secret_allowlist(
        _tool(_ENTRY),
        "ANTHROPIC_API_KEY",
        _SCOPE,
        frozenset({_ENTRY}),  # pyright: ignore[reportUnhashable]  # Pydantic frozen=True → hashable
    )
    assert decision is AllowlistDecision.PERMITTED


def test_check_allowlist_denied_when_not_in_tool() -> None:
    """Acceptance #3 — denial identifies the tool allowlist when the request is absent there."""
    decision = check_secret_allowlist(
        _tool(),
        "ANTHROPIC_API_KEY",
        _SCOPE,
        frozenset({_ENTRY}),  # pyright: ignore[reportUnhashable]  # Pydantic frozen=True → hashable
    )
    assert decision is AllowlistDecision.DENIED_NOT_IN_TOOL_ALLOWLIST


def test_check_allowlist_denied_when_not_in_operator_policy() -> None:
    """Acceptance #3 — in the tool allowlist but not the operator override → denied."""
    decision = check_secret_allowlist(_tool(_ENTRY), "ANTHROPIC_API_KEY", _SCOPE, frozenset())
    assert decision is AllowlistDecision.DENIED_NOT_IN_OPERATOR_POLICY_OVERRIDE


def test_required_secrets_orthogonal_to_sandbox_tier() -> None:
    """Acceptance #4 — required_secrets is orthogonal to sandbox tier; not a max() floor."""
    params = list(inspect.signature(check_secret_allowlist).parameters)
    assert "tier" not in params
    decision = check_secret_allowlist(
        _tool(_ENTRY),
        "ANTHROPIC_API_KEY",
        _SCOPE,
        frozenset({_ENTRY}),  # pyright: ignore[reportUnhashable]  # Pydantic frozen=True → hashable
    )
    assert isinstance(decision, AllowlistDecision)
    assert not isinstance(decision, SandboxTier)
