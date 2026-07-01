"""Tests for U-AS-07 — ToolContract schema + registration validator (C-AS-03 §3)."""

from __future__ import annotations

from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.secret_fetch import SecretScope
from harness_as.tool_contract import (
    RECOMMENDED_CONTRACT_DEFAULT_TIER,
    ContractValidationOutcome,
    RawContractInput,
    SecretAllowlistEntry,
    validate_tool_contract_at_registration,
)


def _raw(
    *,
    minimum_tier: SandboxTier | None = SandboxTier.TIER_2_CONTAINER,
    blast_radius_tier: BlastRadiusTier | None = BlastRadiusTier.LOCAL_MUTATION,
    required_secrets: tuple[SecretAllowlistEntry, ...] | None = None,
) -> RawContractInput:
    """Build a RawContractInput with overridable required-field values."""
    return RawContractInput(
        name="echo",
        description="echo tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        minimum_tier=minimum_tier,
        blast_radius_tier=blast_radius_tier,
        required_secrets=required_secrets,
    )


def test_tool_contract_minimum_tier_required() -> None:
    """Acceptance #1 — missing minimum_tier → MISSING_MINIMUM_TIER."""
    result = validate_tool_contract_at_registration(_raw(minimum_tier=None))
    assert result.outcome is ContractValidationOutcome.MISSING_MINIMUM_TIER
    assert result.contract is None


def test_tool_contract_blast_radius_tier_required() -> None:
    """Acceptance #2 — missing blast_radius_tier → MISSING_BLAST_RADIUS_TIER."""
    result = validate_tool_contract_at_registration(_raw(blast_radius_tier=None))
    assert result.outcome is ContractValidationOutcome.MISSING_BLAST_RADIUS_TIER
    assert result.contract is None


def test_tool_contract_required_secrets_optional_empty_permitted() -> None:
    """Acceptance #3 — required_secrets may be an explicit empty tuple."""
    result = validate_tool_contract_at_registration(_raw(required_secrets=()))
    assert result.outcome is ContractValidationOutcome.VALID
    assert result.contract is not None
    assert result.contract.required_secrets == ()


def test_tool_contract_required_secrets_omitted_treated_as_empty() -> None:
    """Acceptance #3 — omitted required_secrets is treated as empty."""
    result = validate_tool_contract_at_registration(_raw(required_secrets=None))
    assert result.outcome is ContractValidationOutcome.VALID
    assert result.contract is not None
    assert result.contract.required_secrets == ()


def test_recommended_contract_default_tier_is_tier_4_full_vm() -> None:
    """Acceptance #4 — RECOMMENDED_CONTRACT_DEFAULT_TIER is TIER_4_FULL_VM (§3.3 fail-closed)."""
    assert RECOMMENDED_CONTRACT_DEFAULT_TIER is SandboxTier.TIER_4_FULL_VM


def test_minimum_tier_non_tier_promoting() -> None:
    """Acceptance #5 — minimum_tier is a declared floor stored verbatim, not promoted.

    The §3.2 `max()`-composition that treats minimum_tier as a floor (never a
    ceiling) is U-AS-08's; at the schema unit the declared tier round-trips
    unchanged — a low declared tier is not silently promoted.
    """
    result = validate_tool_contract_at_registration(_raw(minimum_tier=SandboxTier.TIER_1_PROCESS))
    assert result.outcome is ContractValidationOutcome.VALID
    assert result.contract is not None
    assert result.contract.minimum_tier is SandboxTier.TIER_1_PROCESS


def test_raw_contract_input_declared() -> None:
    """v1.1 AC — RawContractInput is declared as the pre-validation registration input."""
    expected = {
        "name",
        "description",
        "input_schema",
        "output_schema",
        "minimum_tier",
        "blast_radius_tier",
        "required_secrets",
        # B6 Slice 2 (AS spec v1.11 §3.1) — ToolMetadata forcing discriminators.
        "forces_computer_use",
        "forces_code_execution",
        "is_deterministic_inhouse",
        # B-EFFECT-FENCE-PER-TOOL (AS spec v1.12 §3.1) — effect-fence exemption.
        "idempotent",
    }
    assert set(RawContractInput.model_fields) == expected


def test_tool_contract_idempotent_threads_through_converter() -> None:
    """B-EFFECT-FENCE-PER-TOOL (AS spec v1.12 §3.1) — `idempotent` flows from
    RawContractInput through the registration converter onto the validated
    ToolContract; default False (omitted) preserves the conservative fence-by-default."""
    # Default: omitted → False (fenced).
    default_result = validate_tool_contract_at_registration(_raw())
    assert default_result.contract is not None
    assert default_result.contract.idempotent is False
    # Declared True → threads through (exempt from the runtime effect fence).
    raw = RawContractInput(
        name="read_file",
        description="pure read",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
        idempotent=True,
    )
    result = validate_tool_contract_at_registration(raw)
    assert result.outcome is ContractValidationOutcome.VALID
    assert result.contract is not None
    assert result.contract.idempotent is True


def test_secret_allowlist_entry_declared_at_u_as_07() -> None:
    """v1.1 AC — SecretAllowlistEntry is declared in this unit (Q-R3-3 carrier-ordering)."""
    assert set(SecretAllowlistEntry.model_fields) == {"name", "scope"}
    entry = SecretAllowlistEntry(name="ANTHROPIC_API_KEY", scope=SecretScope(name="prod"))
    assert isinstance(entry.scope, SecretScope)


def test_tool_contract_carries_required_secrets_of_allowlist_entries() -> None:
    """Acceptance #1/#3 — required_secrets elements are SecretAllowlistEntry."""
    secret = SecretAllowlistEntry(name="TOKEN", scope=SecretScope(name="default"))
    result = validate_tool_contract_at_registration(_raw(required_secrets=(secret,)))
    assert result.outcome is ContractValidationOutcome.VALID
    assert result.contract is not None
    assert result.contract.required_secrets == (secret,)
