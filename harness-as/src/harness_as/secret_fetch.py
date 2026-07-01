"""Secret-fetch surface — `fetch_secret` + `SecretRef` + tier-resolution table — U-AS-20.

Implements C-AS-05 §5.1 (function signature), §5.2 (tier-aware resolution),
§5.4 (`SecretRef` opaque-type discipline). Declares the opaque `SecretRef`
handle, the `SecretScope` credential-dimension session key, the four-entry
tier-aware `TIER_RESOLUTION_TABLE`, and the `fetch_secret` /
`tier_resolution_mechanism` functions.

Authority: Implementation_Plan_Action_Surface_v1_2.md §5.2 U-AS-20 (FINALIZED
at R3.1 — Q-R3-2 resolved R1 direction: `fetch_secret(name, scope, tier)`
3-param, `tier` a plain explicit argument; `SecretScope` explicit field set);
Spec_Action_Surface_v1.md v1.3 §5 C-AS-05; ADR-F5 v1.1 §Decision +
§"Permanent tensions engaged" T-perm-2 F5-layer closure.

Spec alignment: AS spec is v1.3 — the C-AS-05 §5.1 signature was reconciled to
the 3-parameter `fetch_secret(name, scope, tier) -> SecretRef` form by the
spec-writer pass that discharged AS-plan v1.2 §0.6 action item A-5. The plan
body and the spec now agree; no divergence.

Deferred to implementation discretion per spec §5.4: the specific keyring-
library binding (`python-keyring`), the in-sandbox HTTP client at microVM /
full-VM tiers, the bootstrap-token issuance protocol, and the `SecretScope`
serialization format. `fetch_secret` here is the typed entry point — it
constructs the opaque `SecretRef` handle bound to the call-site sandbox tier;
the tier-mechanism-specific value resolution is downstream of this unit.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_tier import SandboxTier


class SecretScope(BaseModel):
    """Credential-dimension session key (C-AS-05 §5.1).

    Explicit field set (R3.1 fix — replaces the v1-body `{ ... }` ellipsis).
    Spec §5.1 commits exactly the session-key identity for `scope`: a single
    `name` field, the credential-dimension session-key namespace identifier
    per ADR-F5 v1.1 §Context. The serialization format remains deferred to
    implementation discretion per spec §5.4.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """The credential-dimension session-key namespace identifier (spec §5.1)."""


class SecretRef(BaseModel):
    """Opaque handle to a resolved secret (C-AS-05 §5.1 + §5.4).

    Opaque per §5.4 row 1: the secret **value is not embedded** in `SecretRef`
    and there is no value-accessor API; tool-internal code resolves the value
    via the tier-specific mechanism per §5.2. The handle carries only
    reference metadata — the secret identity (`name`, `scope`) and the
    sandbox-tier binding (`tier`).

    Lifetime-bounded per §5.4 row 2: the `tier` field binds the handle to the
    call-site sandbox; a `SecretRef` is not valid across a different sandbox
    (no cross-sandbox sharing). Fresh-on-restart per §5.4 row 3: `fetch_secret`
    holds no in-process cache, so each call yields a fresh handle.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """Secret identifier — metadata, not the value (spec §5.1)."""

    scope: SecretScope
    """Credential-dimension session key the secret was fetched under."""

    tier: SandboxTier
    """The resolved sandbox tier of the call site — the lifetime-bound anchor."""


class SecretResolutionMechanism(StrEnum):
    """Tier-aware secret-resolution mechanism (C-AS-05 §5.2).

    Closed at cardinality 4 — one mechanism per sandbox tier.
    """

    ENV_VAR_AT_SANDBOX_STARTUP = "ENV_VAR_AT_SANDBOX_STARTUP"
    CONTAINER_ENV_VAR_WITH_KEYRING_HANDLES = "CONTAINER_ENV_VAR_WITH_KEYRING_HANDLES"
    IN_SANDBOX_HTTP_BOOTSTRAP_TOKEN = "IN_SANDBOX_HTTP_BOOTSTRAP_TOKEN"
    IN_SANDBOX_HTTP_WITH_ROTATION_REFRESH = "IN_SANDBOX_HTTP_WITH_ROTATION_REFRESH"


class TPerm2Pole(StrEnum):
    """T-perm-2 pole expressed by a tier's resolution mechanism (ADR-F5 v1.1).

    Tier choice picks pole; both poles are expressed across the four tiers.
    """

    C2_WITHIN_TURN_SNAPSHOT = "C2_WITHIN_TURN_SNAPSHOT"
    C3_ACROSS_TURN_FRESH_FETCH = "C3_ACROSS_TURN_FRESH_FETCH"


class TierResolutionMechanism(BaseModel):
    """One row of the tier-aware resolution table (C-AS-05 §5.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: SandboxTier
    mechanism: SecretResolutionMechanism
    pole_expressed: TPerm2Pole


#: The tier-aware resolution table — exactly 4 entries, one per `SandboxTier`,
#: transcribing spec C-AS-05 §5.2 row-by-row.
TIER_RESOLUTION_TABLE: tuple[TierResolutionMechanism, ...] = (
    TierResolutionMechanism(
        tier=SandboxTier.TIER_1_PROCESS,
        mechanism=SecretResolutionMechanism.ENV_VAR_AT_SANDBOX_STARTUP,
        pole_expressed=TPerm2Pole.C2_WITHIN_TURN_SNAPSHOT,
    ),
    TierResolutionMechanism(
        tier=SandboxTier.TIER_2_CONTAINER,
        mechanism=SecretResolutionMechanism.CONTAINER_ENV_VAR_WITH_KEYRING_HANDLES,
        pole_expressed=TPerm2Pole.C2_WITHIN_TURN_SNAPSHOT,
    ),
    TierResolutionMechanism(
        tier=SandboxTier.TIER_3_MICROVM,
        mechanism=SecretResolutionMechanism.IN_SANDBOX_HTTP_BOOTSTRAP_TOKEN,
        pole_expressed=TPerm2Pole.C3_ACROSS_TURN_FRESH_FETCH,
    ),
    TierResolutionMechanism(
        tier=SandboxTier.TIER_4_FULL_VM,
        mechanism=SecretResolutionMechanism.IN_SANDBOX_HTTP_WITH_ROTATION_REFRESH,
        pole_expressed=TPerm2Pole.C3_ACROSS_TURN_FRESH_FETCH,
    ),
)

_TABLE_BY_TIER: dict[SandboxTier, TierResolutionMechanism] = {
    row.tier: row for row in TIER_RESOLUTION_TABLE
}


def tier_resolution_mechanism(tier: SandboxTier) -> TierResolutionMechanism:
    """Return the tier-aware resolution row for a sandbox tier (C-AS-05 §5.2).

    Total over `SandboxTier` — every tier has exactly one table row.
    """
    return _TABLE_BY_TIER[tier]


def fetch_secret(name: str, scope: SecretScope, tier: SandboxTier) -> SecretRef:
    """Resolve a secret to an opaque `SecretRef` handle (C-AS-05 §5.1).

    The 3-parameter R1 form: `tier` is the call-site's resolved `SandboxTier`,
    passed as a plain explicit argument — not bundled in a context object — so
    the tier-aware resolution input (§5.2) is visible at the call surface.

    Returns a fresh opaque `SecretRef` bound to `tier`; the secret value is
    **not** embedded in the handle. Tier-mechanism-specific value resolution
    (env-var read at process / container tiers; in-sandbox HTTP at microVM /
    full-VM tiers) is deferred to implementation discretion per spec §5.4 and
    is downstream of this unit. No in-process cache is held — each call yields
    a fresh handle (fresh-on-restart per §5.4 row 3).
    """
    return SecretRef(name=name, scope=scope, tier=tier)
