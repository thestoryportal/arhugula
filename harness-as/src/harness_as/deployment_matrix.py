"""12-cell deployment matrix + cell-selection lookup — U-AS-10.

Implements C-AS-09 §9.1 (12-cell deployment-surface x blast-radius matrix),
§9.3 (forcing-condition cell resolution), §9.5 (cell selection contract).
Declares `DeploymentMatrixCell`, the populated `DEPLOYMENT_MATRIX`, and the
`lookup_cell` / `lookup_cell_with_forcing` functions.

Authority: Implementation_Plan_Action_Surface_v1_1.md §5.3 U-AS-10 (R3-revised
body — Pattern A3 conformance: AC2 re-stated against the actual
`SandboxProviderClass` member set; v1 base body at
Implementation_Plan_Action_Surface_v1.md §2 U-AS-10); Spec_Action_Surface_v1.md
§9 C-AS-09; ADR-D2 v1.2 §1.1.

Depends on: U-AS-01 (`SandboxTier`, `BlastRadiusTier`); U-AS-02 (`ToolContext`,
`forced_tier`); U-AS-04 (`DeploymentSurface`); U-AS-11 (`SandboxProviderClass`).

Pattern A3 conformance: the §9.1 cell content is transcribed using only the six
`SandboxProviderClass` carrier members (U-AS-11). The §9.1 tier-2 cell labels
its provider class "process-fs-overlay" — a label not present in the closed
§9.2 six-class taxonomy. Materialization discretion (Class 3): the tier-2
local-mutation cell is mapped to `PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT` — the
§9.1 cell witnesses are dominated by Seatbelt / bubblewrap (the class-3
defining mechanisms per §9.2), and the self-hosted-server tier-2 cell witness
is purely bubblewrap. The full §9.1 witness list is preserved in
`candidate_witnesses`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from harness_as.discriminators import DeploymentSurface
from harness_as.forced_tier_resolution import ToolContext, forced_tier
from harness_as.sandbox_provider_class import SandboxProviderClass
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier


class DeploymentMatrixCell(BaseModel):
    """One cell of the §9.1 deployment matrix (C-AS-09 §9.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_tier: SandboxTier
    provider_class: SandboxProviderClass
    candidate_witnesses: tuple[str, ...]
    """Non-normative §9.5 deployment-surface-time candidate witnesses."""


_LANG = SandboxProviderClass.LANGUAGE_LEVEL
_PROC = SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT
_CONT = SandboxProviderClass.CONTAINER
_UVM = SandboxProviderClass.MICROVM_FIRECRACKER
_FVM = SandboxProviderClass.FULL_VM

_T1 = SandboxTier.TIER_1_PROCESS
_T2 = SandboxTier.TIER_2_CONTAINER
_T3 = SandboxTier.TIER_3_MICROVM
_T4 = SandboxTier.TIER_4_FULL_VM

_RO = BlastRadiusTier.READ_ONLY
_LM = BlastRadiusTier.LOCAL_MUTATION
_ER = BlastRadiusTier.EXTERNAL_REVERSIBLE
_EI = BlastRadiusTier.EXTERNAL_IRREVERSIBLE

# §9.1 cell spec: (tier, provider_class, candidate_witnesses) per cell.
_MATRIX_SPEC: dict[
    tuple[DeploymentSurface, BlastRadiusTier],
    tuple[SandboxTier, SandboxProviderClass, tuple[str, ...]],
] = {
    # local-development row (§9.1)
    (DeploymentSurface.LOCAL_DEVELOPMENT, _RO): (
        _T1,
        _LANG,
        ("in-process", "deer-flow LocalSandboxProvider"),
    ),
    (DeploymentSurface.LOCAL_DEVELOPMENT, _LM): (
        _T2,
        _PROC,
        ("Seatbelt (macOS)", "bubblewrap+socat (Linux/WSL)", "kilocode-style worktree"),
    ),
    (DeploymentSurface.LOCAL_DEVELOPMENT, _ER): (
        _T3,
        _CONT,
        ("Docker-on-OCI", "gVisor", "OpenHands Docker reference", "dify-sandbox"),
    ),
    (DeploymentSurface.LOCAL_DEVELOPMENT, _EI): (
        _T4,
        _UVM,
        ("Firecracker (E2B class)", "E2B self-host", "full VM for computer-use cells"),
    ),
    # self-hosted-server row (§9.1)
    (DeploymentSurface.SELF_HOSTED_SERVER, _RO): (
        _T1,
        _LANG,
        ("in-process", "deer-flow LocalSandboxProvider"),
    ),
    (DeploymentSurface.SELF_HOSTED_SERVER, _LM): (
        _T2,
        _PROC,
        ("bubblewrap+socat (Linux)", "container upgrade acceptable"),
    ),
    (DeploymentSurface.SELF_HOSTED_SERVER, _ER): (
        _T3,
        _CONT,
        ("Docker-on-OCI default", "Kata Containers", "gVisor", "K8s-resident"),
    ),
    (DeploymentSurface.SELF_HOSTED_SERVER, _EI): (
        _T4,
        _UVM,
        ("Firecracker (E2B self-host)", "Modal gVisor", "Kata as microVM-backed"),
    ),
    # managed-cloud row (§9.1)
    (DeploymentSurface.MANAGED_CLOUD, _RO): (
        _T1,
        _LANG,
        ("vendor-managed runtime", "Lambda / Cloud Run / Cloud Functions class"),
    ),
    (DeploymentSurface.MANAGED_CLOUD, _LM): (
        _T2,
        _PROC,
        (
            "Bedrock AgentCore Runtime sandbox primitive",
            "Vertex Agent Engine",
            "Cloudflare Workers Durable Objects",
        ),
    ),
    (DeploymentSurface.MANAGED_CLOUD, _ER): (
        _T3,
        _CONT,
        ("Bedrock AgentCore Runtime (vendor-managed sandbox)", "Vertex Agent Engine"),
    ),
    (DeploymentSurface.MANAGED_CLOUD, _EI): (
        _T4,
        _FVM,
        (
            "Bedrock AgentCore Runtime computer-use sandbox primitive",
            "Anthropic Computer Use VMs (vendor-managed full VM)",
        ),
    ),
}

#: The 12-cell deployment matrix (3 surfaces x 4 blast-radius tiers, §9.1).
DEPLOYMENT_MATRIX: Mapping[tuple[DeploymentSurface, BlastRadiusTier], DeploymentMatrixCell] = (
    MappingProxyType(
        {
            key: DeploymentMatrixCell(
                sandbox_tier=tier,
                provider_class=provider_class,
                candidate_witnesses=witnesses,
            )
            for key, (tier, provider_class, witnesses) in _MATRIX_SPEC.items()
        }
    )
)


def lookup_cell(surface: DeploymentSurface, blast_radius: BlastRadiusTier) -> DeploymentMatrixCell:
    """Return the deployment-matrix cell for a (surface, blast-radius) pair.

    Total over `(DeploymentSurface, BlastRadiusTier)` — all 12 cells populated.
    """
    return DEPLOYMENT_MATRIX[(surface, blast_radius)]


def lookup_cell_with_forcing(
    surface: DeploymentSurface,
    blast_radius: BlastRadiusTier,
    ctx: ToolContext,
) -> DeploymentMatrixCell:
    """Return the deployment-matrix cell, honoring forcing conditions (§9.3).

    Per §9.3: a computer-use binding or code-execution beta invocation resolves
    to the `external-irreversible` column of the deployment surface, regardless
    of the nominal `blast_radius` declaration. When no forcing condition holds,
    this matches `lookup_cell(surface, blast_radius)`.
    """
    if forced_tier(ctx) is not None:
        return DEPLOYMENT_MATRIX[(surface, BlastRadiusTier.EXTERNAL_IRREVERSIBLE)]
    return DEPLOYMENT_MATRIX[(surface, blast_radius)]
