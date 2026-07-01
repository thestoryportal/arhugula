"""Per-sandbox-tier OTLP reachability + F4 capability-floor composition — U-OD-29.

Implements C-OD-20 §20.3 (per-sandbox-tier OTLP reachability per ADR-F4 v1.1
§Consequences (b)(iv)). Each of the four 1-indexed sandbox-isolation tiers must
reach the OTLP collector; the §20.3 reachability shape differs per tier:

- ``TIER_1_PROCESS``  -> ``LOCALHOST_SOCKET``                       (in-process
  collector or operator-bound self-hosted backend collector on host loopback)
- ``TIER_2_CONTAINER`` -> ``EXPLICIT_NETWORK_CONFIG``               (localhost socket OR
  ``host.docker.internal`` / sidecar)
- ``TIER_3_MICROVM``  -> ``PER_MICROVM_AGENT_OR_EGRESS_ALLOWLIST``  (per-microVM agent
  OR egress allow-list)
- ``TIER_4_FULL_VM``  -> ``VENDOR_MANAGED_COLLECTOR_REACHABILITY``  (vendor-managed
  collector)

``OtlpReachabilityClass`` enumerates the four §20.3 reachability shapes (declared
in-unit). ``SandboxTierReachability`` carries, per tier, its reachability class,
the per-tier egress-required flag (false for Tier-1, true for Tier-2/3/4), and
the cell-placement composition flag (always true).
``PER_SANDBOX_TIER_REACHABILITY`` declares exactly one entry per AS
``SandboxTier`` value. ``assert_otlp_reachable_from_sandbox`` verifies, for a
given sandbox tier and the U-OD-28 ``CollectorPlacement`` of the resident cell,
that the §20.3-required reachability holds. ``F4_CAPABILITY_FLOOR_LIFECYCLE_EMISSION_ANCHOR``
carries the F4 v1.1 capability-floor (iv) lifecycle-event-emission discipline
anchor verbatim.

v2.10 (FF-3): the v2.1 in-unit ``enum SandboxTier {TIER_0..TIER_3}`` (0-indexed)
is STRUCK. ``SandboxTier`` is the AS-axis-owned enum (ADR-F4 v1.1 four-tier
sandbox-isolation tier-set; C-AS-01 §1.1) — landed at the AS axis as
``harness_as.sandbox_tier.SandboxTier`` (1-indexed: ``TIER_1_PROCESS /
TIER_2_CONTAINER / TIER_3_MICROVM / TIER_4_FULL_VM``). U-OD-29 consumes the
AS-landed enum cross-axis; it declares NO sandbox-tier enum in-unit.

Cross-axis dependency resolution. The C-AS-12 §12.4 sandbox-tier reachability
edge (cross-axis: AS, edge target ``U-AS-NN`` per OD-S4-3.A) resolves at
sub-phase 7c — declared here, not chased. The ``SandboxTier`` enum import from
``harness_as.sandbox_tier`` IS done now (v2.10; operator-authorized at 7b).

Disposition note (acc #6 — egress-policy arm). The acc #6 C-AS-12 §12.4
egress-policy constraint — Tier-3/4 MUST NOT egress to an *arbitrary public*
ingestion endpoint — is satisfied **by construction** over the U-OD-28
``CollectorPlacement`` enum: every non-``IN_PROCESS`` placement class is a
private / vendor-managed endpoint (``SIDECAR`` / ``SELF_HOSTED_BACKEND_COLLECTOR``
/ ``SIDECAR_WITH_PER_TENANT_ROUTING`` / ``PER_TENANT_COLLECTOR_INSTANCE`` /
``VENDOR_PIPELINE`` / ``VENDOR_MANAGED_COLLECTOR``). The ``CollectorPlacement``
enum admits no "arbitrary public endpoint" value. The egress-policy arm of
``assert_otlp_reachable_from_sandbox`` is therefore presently unreachable —
it is retained as a forward guard against a future ``CollectorPlacement``
widening that admits a public-ingestion class.

Authority: Implementation_Plan_Operational_Discipline_v2_10.md §3.7.3 U-OD-29
(v2.10 FF-3 revision — in-unit ``SandboxTier`` struck, conformed to the
AS-owned enum + OD spec §20.3); Spec_Operational_Discipline_v1_2.md §20.3
(C-OD-20 — per-sandbox-tier OTLP reachability per F4 v1.1 §Consequences
(b)(iv); §20.3 canonical in v1.2, unchanged by v1.3/v1.4);
ADR-F4 v1.1 §Consequences (b)(iv) (capability-floor lifecycle-event emission).

Depends on: [U-OD-28] — ``CollectorPlacement`` from
``per_cell_collector_placement_matrix``; [U-AS-01] (cross-axis: AS) —
``SandboxTier`` from ``harness_as.sandbox_tier`` (C-AS-01 §1.1 four-tier set).
"""

from __future__ import annotations

from enum import StrEnum

from harness_as.sandbox_tier import SandboxTier
from pydantic import BaseModel, ConfigDict

from harness_od.per_cell_collector_placement_matrix import CollectorPlacement

__all__ = [
    "F4_CAPABILITY_FLOOR_LIFECYCLE_EMISSION_ANCHOR",
    "PER_SANDBOX_TIER_REACHABILITY",
    "OtlpReachabilityClass",
    "ReachabilityViolation",
    # v2.10 FF-3: SandboxTier is AS-axis-owned (C-AS-01 §1.1) and intentionally
    # re-exported here as the canonical cross-axis citation surface; the acc #8
    # test verifies the re-export linkage via ``mod.SandboxTier.__module__``.
    "SandboxTier",
    "SandboxTierReachability",
    "assert_otlp_reachable_from_sandbox",
]


# --- §20.3 OtlpReachabilityClass enum (declared in-unit) --------------------


class OtlpReachabilityClass(StrEnum):
    """The 4 per-sandbox-tier OTLP reachability shapes (C-OD-20 §20.3).

    Exactly 4 values per `Spec_Operational_Discipline_v1_2.md` §20.3 — one
    reachability shape per AS `SandboxTier` value. Declared in-unit (unlike
    `SandboxTier`, which is AS-axis-owned and consumed cross-axis).
    """

    LOCALHOST_SOCKET = "LOCALHOST_SOCKET"
    """§20.3 — Tier-1 process: collector reached via localhost socket;
    no per-tier sandbox egress required."""

    EXPLICIT_NETWORK_CONFIG = "EXPLICIT_NETWORK_CONFIG"
    """§20.3 — Tier-2 container: localhost socket OR explicit network-config
    (`host.docker.internal` mapping or sidecar collector)."""

    PER_MICROVM_AGENT_OR_EGRESS_ALLOWLIST = "PER_MICROVM_AGENT_OR_EGRESS_ALLOWLIST"
    """§20.3 — Tier-3 microVM: per-microVM agent OR egress allow-list
    required to reach the collector."""

    VENDOR_MANAGED_COLLECTOR_REACHABILITY = "VENDOR_MANAGED_COLLECTOR_REACHABILITY"
    """§20.3 — Tier-4 full-VM: vendor-managed collector is the typical
    placement; reachability via the vendor-managed collector endpoint."""


# --- §20.3 reachability-violation error arm --------------------------------


class ReachabilityViolation(Exception):  # noqa: N818 — name is the U-OD-29 plan signature verbatim
    """Raised when a sandbox tier lacks §20.3-required OTLP collector reachability.

    The Python materialization of the `Result<(), ReachabilityViolation>` error
    arm in `assert_otlp_reachable_from_sandbox` — C-OD-20 §20.3 commits that
    every sandbox tier must reach the OTLP collector via its tier-specific
    reachability shape. Stack is Pydantic v2 + stdlib, no `Result` framework
    pull (CLAUDE.md §3.2 / I-6).
    """


# --- §20.3 per-tier reachability record ------------------------------------


class SandboxTierReachability(BaseModel):
    """The §20.3 OTLP reachability shape committed for one sandbox tier.

    `sandbox_tier` is the AS-axis-owned `SandboxTier` value (cross-axis,
    C-AS-01 §1.1). `reachability_class` is the §20.3 reachability shape;
    `per_tier_egress_required` is false for `TIER_1_PROCESS` (in-process
    collector, no network egress) and true for `TIER_2_CONTAINER` /
    `TIER_3_MICROVM` / `TIER_4_FULL_VM`. `composes_with_cell_placement` is
    always true — per-tier reachability composes additively with the U-OD-28
    per-cell collector placement (acc #7).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the AS-axis-owned sandbox-isolation tier (cross-axis; C-AS-01 §1.1).
    sandbox_tier: SandboxTier
    #: the §20.3 reachability shape for this tier.
    reachability_class: OtlpReachabilityClass
    #: false for Tier-1 process; true for Tier-2/3/4.
    per_tier_egress_required: bool
    #: composes additively with the U-OD-28 per-cell placement — always true.
    composes_with_cell_placement: bool


def _reachability(
    tier: SandboxTier,
    cls: OtlpReachabilityClass,
    *,
    egress_required: bool,
) -> SandboxTierReachability:
    """Build a `SandboxTierReachability` with the §20.3 cell-placement flag."""
    return SandboxTierReachability(
        sandbox_tier=tier,
        reachability_class=cls,
        per_tier_egress_required=egress_required,
        composes_with_cell_placement=True,
    )


#: Per-sandbox-tier OTLP reachability — exactly 4 entries, one per AS
#: `SandboxTier` value, byte-exact with the C-OD-20 §20.3 reachability table:
#: Tier-1 -> localhost socket; Tier-2 -> explicit network-config; Tier-3 ->
#: per-microVM agent or egress allow-list; Tier-4 -> vendor-managed collector.
PER_SANDBOX_TIER_REACHABILITY: dict[SandboxTier, SandboxTierReachability] = {
    SandboxTier.TIER_1_PROCESS: _reachability(
        SandboxTier.TIER_1_PROCESS,
        OtlpReachabilityClass.LOCALHOST_SOCKET,
        egress_required=False,
    ),
    SandboxTier.TIER_2_CONTAINER: _reachability(
        SandboxTier.TIER_2_CONTAINER,
        OtlpReachabilityClass.EXPLICIT_NETWORK_CONFIG,
        egress_required=True,
    ),
    SandboxTier.TIER_3_MICROVM: _reachability(
        SandboxTier.TIER_3_MICROVM,
        OtlpReachabilityClass.PER_MICROVM_AGENT_OR_EGRESS_ALLOWLIST,
        egress_required=True,
    ),
    SandboxTier.TIER_4_FULL_VM: _reachability(
        SandboxTier.TIER_4_FULL_VM,
        OtlpReachabilityClass.VENDOR_MANAGED_COLLECTOR_REACHABILITY,
        egress_required=True,
    ),
}

#: Closure invariant — the table covers exactly the 4 AS `SandboxTier` values
#: (acc #3); `SandboxTier` is consumed from the AS axis, not declared in-unit.
assert set(PER_SANDBOX_TIER_REACHABILITY) == set(SandboxTier), (
    "PER_SANDBOX_TIER_REACHABILITY must cover exactly the 4 AS SandboxTier values"
)


#: The §20.3 collector placements that satisfy localhost-socket reachability for
#: an in-process collector.
_LOCALHOST_SOCKET_PLACEMENTS: frozenset[CollectorPlacement] = frozenset(
    {CollectorPlacement.IN_PROCESS}
)

#: The §20.3 collector placements reachable by the Tier-1 bootstrap process
#: without per-tier sandbox egress. In addition to an in-process collector, a
#: solo self-hosted server deployment may bind the self-hosted backend collector
#: on the host loopback interface (U-OD-28 cell-2 alternant).
_TIER_1_PROCESS_REACHABLE_PLACEMENTS: frozenset[CollectorPlacement] = frozenset(
    {
        CollectorPlacement.IN_PROCESS,
        CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
    }
)

#: The §20.3 collector placements reachable by an isolated container/VM tier —
#: any non-in-process placement (sidecar, self-hosted backend, vendor pipeline,
#: vendor-managed, per-tenant). Tier-2/3/4 require network reachability to one
#: of these; `IN_PROCESS` alone does not give an isolated tier network
#: reachability to the collector (acc #4).
_NETWORK_REACHABLE_PLACEMENTS: frozenset[CollectorPlacement] = (
    frozenset(CollectorPlacement) - _LOCALHOST_SOCKET_PLACEMENTS
)

#: The §20.3 private / vendor-managed collector endpoints to which the
#: most-isolated tiers (Tier-3 microVM, Tier-4 full-VM) MAY egress — they MUST
#: NOT egress to arbitrary public ingestion endpoints (acc #6; AS plan
#: C-AS-12 §12.4 egress policy composition).
_PRIVATE_OR_VENDOR_MANAGED_PLACEMENTS: frozenset[CollectorPlacement] = frozenset(
    {
        CollectorPlacement.SIDECAR,
        CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
        CollectorPlacement.SIDECAR_WITH_PER_TENANT_ROUTING,
        CollectorPlacement.PER_TENANT_COLLECTOR_INSTANCE,
        CollectorPlacement.VENDOR_PIPELINE,
        CollectorPlacement.VENDOR_MANAGED_COLLECTOR,
    }
)


def assert_otlp_reachable_from_sandbox(
    sandbox_tier: SandboxTier,
    cell_placement: CollectorPlacement,
) -> None:
    """Assert a sandbox tier reaches the OTLP collector under `cell_placement`.

    Verifies the C-OD-20 §20.3 per-sandbox-tier OTLP reachability invariant for
    `sandbox_tier` against the U-OD-28 `CollectorPlacement` of the resident
    cell. Returns `None` (the `Ok(())` arm) when the §20.3-required reachability
    holds; raises `ReachabilityViolation` (the `Err` arm) otherwise (acc #4).

    Reachability semantics per §20.3:

    - `TIER_1_PROCESS` requires localhost-socket reachability to an in-process
      collector (`IN_PROCESS`) or to an operator-bound self-hosted backend
      collector endpoint on host loopback; any other placement is a Tier-1
      reachability violation.
    - `TIER_2_CONTAINER` / `TIER_3_MICROVM` / `TIER_4_FULL_VM` require network
      reachability to a non-in-process collector; `IN_PROCESS` placement alone
      does not give an isolated tier network reachability — that is a violation.
    - `TIER_3_MICROVM` and `TIER_4_FULL_VM` additionally compose with the AS
      plan C-AS-12 §12.4 egress policy: the most-isolated tiers MAY egress only
      to a private / vendor-managed collector endpoint (acc #6).
    """
    if sandbox_tier == SandboxTier.TIER_1_PROCESS:
        if cell_placement not in _TIER_1_PROCESS_REACHABLE_PLACEMENTS:
            raise ReachabilityViolation(
                f"§20.3 reachability violated: {sandbox_tier} requires "
                f"localhost-socket reachability to an in-process collector "
                f"(CollectorPlacement.IN_PROCESS) or operator-bound self-hosted "
                f"backend collector; cell placement is "
                f"{cell_placement!r} (C-OD-20 §20.3)"
            )
        return None

    # Tier-2/3/4 — isolated tiers requiring network reachability.
    if cell_placement not in _NETWORK_REACHABLE_PLACEMENTS:
        raise ReachabilityViolation(
            f"§20.3 reachability violated: {sandbox_tier} requires network "
            f"reachability to a non-in-process collector; cell placement is "
            f"{cell_placement!r} — an in-process collector is not "
            f"network-reachable from an isolated sandbox tier (C-OD-20 §20.3)"
        )

    # acc #6 — Tier-3/4 egress-policy composition with AS plan C-AS-12 §12.4:
    # the most-isolated tiers MUST NOT egress to arbitrary public endpoints.
    if (
        sandbox_tier in (SandboxTier.TIER_3_MICROVM, SandboxTier.TIER_4_FULL_VM)
        and cell_placement not in _PRIVATE_OR_VENDOR_MANAGED_PLACEMENTS
    ):
        raise ReachabilityViolation(
            f"§20.3 / C-AS-12 §12.4 egress-policy violated: {sandbox_tier} "
            f"(a most-isolated tier) MAY egress only to a private or "
            f"vendor-managed collector endpoint; cell placement is "
            f"{cell_placement!r} (C-OD-20 §20.3)"
        )
    return None


#: F4 v1.1 capability-floor (iv) lifecycle-event-emission discipline anchor —
#: verbatim per Implementation_Plan_Operational_Discipline_v2_10.md §3.7.3
#: (acc #5). Lifecycle events MUST emit from every sandbox tier regardless of
#: collector placement; failure to emit is an F4 capability-floor violation.
F4_CAPABILITY_FLOOR_LIFECYCLE_EMISSION_ANCHOR: str = (
    "Lifecycle events (per U-OD-08 F3 mapping) MUST emit from every sandbox "
    "tier; failure to emit constitutes F4 v1.1 capability-floor (iv) violation"
)
