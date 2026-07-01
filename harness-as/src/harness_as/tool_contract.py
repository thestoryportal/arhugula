"""Tool-contract schema + registration validator — U-AS-07.

Implements C-AS-03 §3.1 (tool-contract field signature), §3.2 (declaration
discipline), §3.3 (default-tier policy). Declares the `ToolContract` schema,
the `RawContractInput` pre-validation record, the `SecretAllowlistEntry`
carrier, the `ContractValidationResult`, the registration validator, and the
recommended fail-closed default tier.

Authority: Implementation_Plan_Action_Surface_v1_1.md §5.3 U-AS-07 (R3-revised
body — Pattern B carrier `RawContractInput`; `SecretAllowlistEntry`
carrier-ordering fix per Q-R3-3 option (a); v1 base body at
Implementation_Plan_Action_Surface_v1.md §2 U-AS-07); Spec_Action_Surface_v1.md
§3 C-AS-03; ADR-F4 v1.1 §Consequences (a) + §Rationale (a).

Depends on: U-AS-01 (`SandboxTier`, `BlastRadiusTier`); U-AS-20 (`SecretScope`,
the `SecretAllowlistEntry.scope` type — in-cone via the `[U-AS-20]`
carrier-ordering edge).

Carrier-ordering fix (Q-R3-3 option (a)): `SecretAllowlistEntry` is declared
here as the `ToolContract.required_secrets` element type. U-AS-22 consumes it
(populates the access-control semantics); it does not re-declare it.

Two referenced-but-under-specified types resolved minimally (no invented
structure, X-AL-3 — same pattern as U-IS-01 `ResidenceContract`):
  ① `JSONSchema` — the §3.1 `input_schema` / `output_schema` field type. A
     JSON Schema document is a JSON object; modelled as `dict[str, object]`.
     The specific tool-contract serialization format is deferred to
     implementation discretion per spec §3.3.
  ② `RawContractInput` — the §3 registration-input subject, the un-validated
     counterpart of `ToolContract`. Modelled with the `ToolContract` fields,
     `minimum_tier` / `blast_radius_tier` / `required_secrets` optional so the
     registration validator can detect the missing-required-field cases.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.secret_fetch import SecretScope

#: A JSON Schema document — a JSON object (resolution ①).
JSONSchema = dict[str, object]


class SecretAllowlistEntry(BaseModel):
    """One per-tool secret-allowlist entry (C-AS-06 §6.1 2-field shape).

    Carrier declared here per the Q-R3-3 option (a) carrier-ordering fix —
    `SecretAllowlistEntry` is the `ToolContract.required_secrets` element type.
    U-AS-22 consumes this shape; it does not re-declare it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    scope: SecretScope


class ToolContract(BaseModel):
    """A validated tool contract (C-AS-03 §3.1).

    `minimum_tier` and `blast_radius_tier` are required per §3.2; a
    `ToolContract` instance only exists post-validation, so both are
    non-optional here. `required_secrets` is optional per C-AS-06 — an empty
    tuple is permitted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    input_schema: JSONSchema
    output_schema: JSONSchema
    minimum_tier: SandboxTier
    """REQUIRED — drives the F4 capability-introspection floor at C-AS-02."""

    blast_radius_tier: BlastRadiusTier
    """REQUIRED — drives the C-AS-02 default `sandbox_tier_floor`."""

    required_secrets: tuple[SecretAllowlistEntry, ...] = ()
    """OPTIONAL per C-AS-06; empty tuple permitted."""

    forces_computer_use: bool = False
    """OPTIONAL §2.2 `ToolMetadata` discriminator (C-AS-03 §3.1, v1.11 — B6 Slice 2).
    Keys the C-AS-02 §2.3 row-1 forcing condition (→ TIER_4_FULL_VM) at the runtime
    per-tool sandbox resolver (runtime spec v1.56 §14.9.11). Default `False` — an
    existing contract resolves byte-identically."""

    forces_code_execution: bool = False
    """OPTIONAL §2.2 `ToolMetadata` discriminator — keys the C-AS-02 §2.3 row-2
    forcing condition (→ TIER_4_FULL_VM). Default `False`."""

    is_deterministic_inhouse: bool = False
    """OPTIONAL §2.2 `ToolMetadata` discriminator — keys the C-AS-02 §2.3 row-7
    read-only-deterministic-in-house lookup (→ TIER_1_PROCESS, bounded below by the
    deployment-surface default + blast-radius floor). Default `False`."""

    idempotent: bool = False
    """OPTIONAL (C-AS-03 §3.1, v1.12 — `B-EFFECT-FENCE-PER-TOOL`). Read ONLY by the
    runtime effect fence (runtime spec §14.22 / §14.22.7): when the fence is active
    for a run, a tool declaring `idempotent=True` is NOT reserved at the per-(run,
    step, tool) fence gate — it fires + is safely retryable. STRICT, tool-intrinsic,
    all-invocations semantic: asserts every invocation re-executes with NO additional
    external effect for ALL args (a pure read trivially qualifies; PUT-style qualifies;
    append/send/counter-increment do NOT). Default `False` = treat as non-idempotent →
    fenced (byte-identical to pre-v1.12). NOT a sandbox discriminator — does NOT enter
    the C-AS-02 §2.3 `sandbox_tier_floor` composition."""


class RawContractInput(BaseModel):
    """Pre-validation tool-contract serialization shape (resolution ②).

    The un-validated counterpart of `ToolContract` — the §3 registration-input
    subject. `minimum_tier` / `blast_radius_tier` are optional here so the
    registration validator can detect the §3.2 missing-required-field cases;
    `required_secrets` is optional and, when absent, treated as empty (§3.2
    declaration discipline / acceptance #3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    input_schema: JSONSchema
    output_schema: JSONSchema
    minimum_tier: SandboxTier | None = None
    blast_radius_tier: BlastRadiusTier | None = None
    required_secrets: tuple[SecretAllowlistEntry, ...] | None = None
    forces_computer_use: bool = False
    forces_code_execution: bool = False
    is_deterministic_inhouse: bool = False
    idempotent: bool = False


class ContractValidationOutcome(StrEnum):
    """Tool-contract registration-validation verdict (C-AS-03 §3.2)."""

    VALID = "VALID"
    MISSING_MINIMUM_TIER = "MISSING_MINIMUM_TIER"
    MISSING_BLAST_RADIUS_TIER = "MISSING_BLAST_RADIUS_TIER"


class ContractValidationResult(BaseModel):
    """Outcome of tool-contract registration validation (C-AS-03 §3.2).

    Discriminated result: `outcome` is the validation verdict; `contract` is
    the validated `ToolContract` populated iff `outcome` is `VALID`, else
    `None`. Materializes the plan's `{ VALID(ToolContract),
    MISSING_MINIMUM_TIER, MISSING_BLAST_RADIUS_TIER }` tagged union.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ContractValidationOutcome
    contract: ToolContract | None = None


#: Recommended fail-closed contract-default tier (C-AS-03 §3.3 / ADR-F4 v1.1
#: §Consequences (c)). Tool registration MAY apply this default; permissive
#: defaults are operator-tunable at the registry layer with an audit entry.
RECOMMENDED_CONTRACT_DEFAULT_TIER: SandboxTier = SandboxTier.TIER_4_FULL_VM


def validate_tool_contract_at_registration(
    raw_contract: RawContractInput,
) -> ContractValidationResult:
    """Validate a raw tool-contract input at the registration boundary.

    Per C-AS-03 §3.2: `minimum_tier` and `blast_radius_tier` are required —
    a contract missing either is rejected. `minimum_tier` is checked first
    (acceptance #1 / #2 precedence). `required_secrets`, when absent, is
    treated as empty (acceptance #3).
    """
    if raw_contract.minimum_tier is None:
        return ContractValidationResult(outcome=ContractValidationOutcome.MISSING_MINIMUM_TIER)
    if raw_contract.blast_radius_tier is None:
        return ContractValidationResult(outcome=ContractValidationOutcome.MISSING_BLAST_RADIUS_TIER)
    contract = ToolContract(
        name=raw_contract.name,
        description=raw_contract.description,
        input_schema=raw_contract.input_schema,
        output_schema=raw_contract.output_schema,
        minimum_tier=raw_contract.minimum_tier,
        blast_radius_tier=raw_contract.blast_radius_tier,
        required_secrets=raw_contract.required_secrets or (),
        forces_computer_use=raw_contract.forces_computer_use,
        forces_code_execution=raw_contract.forces_code_execution,
        is_deterministic_inhouse=raw_contract.is_deterministic_inhouse,
        idempotent=raw_contract.idempotent,
    )
    return ContractValidationResult(outcome=ContractValidationOutcome.VALID, contract=contract)
