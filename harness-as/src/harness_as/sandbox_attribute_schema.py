"""Seven `sandbox.*` attribute names + tech-provider join — U-AS-16.

Implements C-AS-15 §15.2 (the seven `sandbox.*` attribute names), §15.3
(`sandbox.tech` <-> `sandbox.provider` join contract), §15.7 (capability-floor
traceability). Declares `SandboxTechClass`, `SandboxProvider`,
`SandboxAttributeSchema`, the seven-entry `SANDBOX_ATTRIBUTE_SCHEMA`, and the
`provider_belongs_to` / `tech_admits_provider` join functions.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-16 (verbatim CONFORM
unit per Implementation_Plan_Action_Surface_v1_1.md §0.5);
Spec_Action_Surface_v1.md §15 C-AS-15; ADR-D2 v1.2 §1.7 + §1.7.1.

Depends on: U-AS-01 (`SandboxTier`), U-AS-03 (`SandboxFailClass`), U-AS-08
(`AssignedTierReason`) — the three enums the `sandbox.tier` / `sandbox.fail.class`
/ `sandbox.policy.assigned_tier_reason` attributes reference (acceptance #6);
`AttributeValueType` / `Cardinality` from `harness-core`.

The plan signature types `SandboxAttributeSchema.emitted_on` as `SpanEventKind`;
`SpanEventKind` is declared downstream at U-AS-17, so U-AS-16 (upstream) would
have a carrier-ordering defect. `emitted_on` is materialized as the span-name
string (`sandbox.enter` / `sandbox.violation` / `sandbox.exit`) — the §15.2
"Always-emitted on" column — respecting carrier ordering. Documented discretion.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict


class SandboxTechClass(StrEnum):
    """The 5 sandbox technology classes (C-AS-15 §15.3). `VM` is reserved."""

    MICROVM = "microvm"
    CONTAINER = "container"
    VM = "vm"
    LANGUAGE_LEVEL = "language-level"
    FS_OVERLAY = "fs-overlay"


class SandboxProvider(StrEnum):
    """The 17 sandbox providers at v1.1 (C-AS-15 §15.3 join table)."""

    E2B_FIRECRACKER = "e2b_firecracker"
    MODAL_GVISOR = "modal_gvisor"
    KATA = "kata"
    BEDROCK_AGENTCORE = "bedrock_agentcore"
    VERTEX_AGENT_ENGINE = "vertex_agent_engine"
    ANTHROPIC_COMPUTER_USE_VM = "anthropic_computer_use_vm"
    DOCKER_OCI = "docker_oci"
    OPENSANDBOX = "opensandbox"
    DIFY_SANDBOX = "dify_sandbox"
    DAYTONA = "daytona"
    DENO = "deno"
    LANGUAGE_LEVEL = "language_level"
    BUBBLEWRAP = "bubblewrap"
    SEATBELT = "seatbelt"
    FUSE_OVERLAY = "fuse_overlay"
    FUSE_PROJFS = "fuse_projfs"
    KILOCODE_WORKTREE = "kilocode_worktree"


class SandboxAttributeSchema(BaseModel):
    """One `sandbox.*` attribute schema row (C-AS-15 §15.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    emitted_on: str
    """The §15.2 "Always-emitted on" span name (see module docstring)."""

    discriminator_role: str


_ENUM = AttributeValueType.ENUM_REF
_INT = AttributeValueType.INT
_FLOAT = AttributeValueType.FLOAT
_ENTER = "sandbox.enter"
_VIOLATION = "sandbox.violation"
_EXIT = "sandbox.exit"

#: The seven `sandbox.*` attribute schema rows (C-AS-15 §15.2).
SANDBOX_ATTRIBUTE_SCHEMA: tuple[SandboxAttributeSchema, ...] = (
    SandboxAttributeSchema(
        attribute_name="sandbox.tier",
        value_type=_ENUM,
        cardinality=Cardinality.LOW,
        emitted_on=_ENTER,
        discriminator_role="Structural; stable across tech swap (F4 canonical)",
    ),
    SandboxAttributeSchema(
        attribute_name="sandbox.tech",
        value_type=_ENUM,
        cardinality=Cardinality.LOW,
        emitted_on=_ENTER,
        discriminator_role="Technology class; swap-friendly (F4 canonical)",
    ),
    SandboxAttributeSchema(
        attribute_name="sandbox.fail.class",
        value_type=_ENUM,
        cardinality=Cardinality.LOW,
        emitted_on=_VIOLATION,
        discriminator_role="Failure-class taxonomy (F4 canonical)",
    ),
    SandboxAttributeSchema(
        attribute_name="sandbox.policy.assigned_tier_reason",
        value_type=_ENUM,
        cardinality=Cardinality.LOW,
        emitted_on=_ENTER,
        discriminator_role="Audit surface for which max() floor won (F4 canonical)",
    ),
    SandboxAttributeSchema(
        attribute_name="sandbox.cost.tier_overhead_ms",
        value_type=_INT,
        cardinality=Cardinality.PER_REQUEST,
        emitted_on=_EXIT,
        discriminator_role="Per-call latency overhead (F4 canonical)",
    ),
    SandboxAttributeSchema(
        attribute_name="sandbox.cost.tier_overhead_usd",
        value_type=_FLOAT,
        cardinality=Cardinality.PER_REQUEST,
        emitted_on=_EXIT,
        discriminator_role="Per-call dollar overhead (F4 canonical)",
    ),
    SandboxAttributeSchema(
        attribute_name="sandbox.provider",
        value_type=_ENUM,
        cardinality=Cardinality.MEDIUM,
        emitted_on=_ENTER,
        discriminator_role="Vendor+tech instance (D2-introduced)",
    ),
)

#: The `mcp.fail.class` attribute co-emitted on `sandbox.violation` per
#: AS spec v1.6 §15.9 dual-attribute emission discipline. Carries the
#: §15.8 MCPInvocationFailClass 4-value taxonomy at the MCP-protocol
#: layer (sibling to §4.1 F4 SandboxFailClass at the process-execution
#: layer). NOT a member of the 7-row `SANDBOX_ATTRIBUTE_SCHEMA` above —
#: `mcp.fail.class` carries the `mcp.*` namespace prefix; the schema row
#: lives in a sibling tuple to preserve the §15.2 "seven" semantics.
MCP_INVOCATION_ATTRIBUTE_SCHEMA: tuple[SandboxAttributeSchema, ...] = (
    SandboxAttributeSchema(
        attribute_name="mcp.fail.class",
        value_type=_ENUM,
        cardinality=Cardinality.LOW,
        emitted_on=_VIOLATION,
        discriminator_role="MCP-protocol-layer fail-class taxonomy (§15.8)",
    ),
)

# §15.3 join table — each provider belongs to exactly one tech class.
_PROVIDER_TECH: dict[SandboxProvider, SandboxTechClass] = {
    SandboxProvider.E2B_FIRECRACKER: SandboxTechClass.MICROVM,
    SandboxProvider.MODAL_GVISOR: SandboxTechClass.MICROVM,
    SandboxProvider.KATA: SandboxTechClass.MICROVM,
    SandboxProvider.BEDROCK_AGENTCORE: SandboxTechClass.MICROVM,
    SandboxProvider.VERTEX_AGENT_ENGINE: SandboxTechClass.MICROVM,
    SandboxProvider.ANTHROPIC_COMPUTER_USE_VM: SandboxTechClass.MICROVM,
    SandboxProvider.DOCKER_OCI: SandboxTechClass.CONTAINER,
    SandboxProvider.OPENSANDBOX: SandboxTechClass.CONTAINER,
    SandboxProvider.DIFY_SANDBOX: SandboxTechClass.CONTAINER,
    SandboxProvider.DAYTONA: SandboxTechClass.CONTAINER,
    SandboxProvider.DENO: SandboxTechClass.LANGUAGE_LEVEL,
    SandboxProvider.LANGUAGE_LEVEL: SandboxTechClass.LANGUAGE_LEVEL,
    SandboxProvider.BUBBLEWRAP: SandboxTechClass.FS_OVERLAY,
    SandboxProvider.SEATBELT: SandboxTechClass.FS_OVERLAY,
    SandboxProvider.FUSE_OVERLAY: SandboxTechClass.FS_OVERLAY,
    SandboxProvider.FUSE_PROJFS: SandboxTechClass.FS_OVERLAY,
    SandboxProvider.KILOCODE_WORKTREE: SandboxTechClass.FS_OVERLAY,
}


def provider_belongs_to(provider: SandboxProvider) -> SandboxTechClass:
    """Return the tech class a sandbox provider belongs to (C-AS-15 §15.3).

    Total over `SandboxProvider`; the belongs-to relation is functional — each
    provider belongs to exactly one tech class.
    """
    return _PROVIDER_TECH[provider]


def tech_admits_provider(tech: SandboxTechClass, provider: SandboxProvider) -> bool:
    """True when `tech` is the tech class `provider` belongs to (C-AS-15 §15.3)."""
    return provider_belongs_to(provider) is tech
