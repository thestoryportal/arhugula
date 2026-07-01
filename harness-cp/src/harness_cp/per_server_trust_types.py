"""C-CP-27 PerServerTrustEvaluator + MCPClientNamespaceEmitter type carriers.

U-CP-66 / U-CP-67 — first two units of cluster 10-CP-C. Declares the carrier
enums (U-CP-66) and the policy + evaluation envelopes (U-CP-67) that the
C-CP-27 `PerServerTrustEvaluator.evaluate` (U-CP-68) + the `mcp.trust.evaluate`
span emission (U-CP-70) consume at runtime.

**U-CP-66 enums (3):**

- `MCPPrimitive` — 4-class MCP-primitive taxonomy per C-AS-14 §14.3 enum
  (tool / resource / prompt / sampling).
- `TrustDecisionReason` — 6-class trust-decision-outcome taxonomy per CP spec
  v1.10 §27.2. Includes the two `UNKNOWN_SERVER_*` members per Decision 3.D1
  RATIFIED (ALLOW-with-tier-floor for unknown servers).
- `TierDerivationRule` — 3-class tier-derivation strategy for unknown servers
  (CONSERVATIVE default per §27.7 deferred-to-discretion notes).

**U-CP-67 envelopes (2):**

- `TrustPolicy` — operator-configured-at-bootstrap immutable policy carrier;
  6 fields including the `tier_derivation: TierDerivationRule` per Decision
  3.D1 + `allow_list` / `deny_list` as `frozenset[str]` (deny wins per §27.6
  invariant 3).
- `TrustEvaluation` — 4-field evaluation-result envelope returned by
  `PerServerTrustEvaluator.evaluate(...)`.

Member string values are cited verbatim from CP spec v1.10 §27.2. Envelope
models use frozen Pydantic v2 `BaseModel` (matching the U-CP-58 / U-CP-62
precedent at cluster 10-CP-A and 10-CP-B; the spec's `@dataclass(frozen=True)`
declaration maps to `BaseModel` + `ConfigDict(frozen=True, extra="forbid")`
per repo discipline established at `validator_framework_types.py` and
`pause_resume_protocol_types.py`).

**MCPTrustTier reuse note.** `MCPTrustTier` is re-used (NOT re-authored) from
`harness_cp.cp_shared_types` (U-CP-00c) per spec §27.2 reused-types note.
Pattern-D field-set inheritance per Phase A.1 §4.2.

Authority: CP spec v1.10 §27.2 (NEW C-CP-27); plan units U-CP-66 + U-CP-67
(CP plan v2.15 §1 cluster 10-CP-C, preserved at v2.17).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import MCPTrustTier

# ---------------------------------------------------------------------------
# U-CP-66 enum carriers (3 enums; §27.2 verbatim)
# ---------------------------------------------------------------------------


class MCPPrimitive(StrEnum):
    """4-class MCP primitive taxonomy per C-AS-14 §14.3 + CP spec v1.10 §27.2.

    Member values match the `modelcontextprotocol.io` primitive enumeration
    (tool / resource / prompt / sampling) consumed by the
    `mcp.primitive.kind` span attribute declared at AS §14.3.
    """

    TOOL = "tool"
    RESOURCE = "resource"
    PROMPT = "prompt"
    SAMPLING = "sampling"


class TrustDecisionReason(StrEnum):
    """6-class trust-decision outcome taxonomy per CP spec v1.10 §27.2.

    The two `UNKNOWN_SERVER_*` members reflect Decision 3.D1 RATIFIED:
    unknown-server default = ALLOW-with-tier-floor (was DENY at draft;
    operator-elected ALLOW). UNKNOWN decisions always carry
    `audit_required=true` per §27.6 invariant 4.
    """

    EXPLICIT_ALLOW = "explicit_allow"
    EXPLICIT_DENY = "explicit_deny"
    TIER_FLOOR_PASS = "tier_floor_pass"
    TIER_FLOOR_VIOLATION = "tier_floor_violation"
    UNKNOWN_SERVER_TIER_FLOOR_PASS = "unknown_server_tier_floor_pass"
    UNKNOWN_SERVER_TIER_FLOOR_VIOLATION = "unknown_server_tier_floor_violation"


class TierDerivationRule(StrEnum):
    """3-class tier-derivation strategy for unknown servers per CP spec v1.10
    §27.2.

    - `CONSERVATIVE` — resolved tier = `MIN(MCPTrustTier members)` per §27.7
      deferred-to-discretion notes; the explicit minimum member is
      `LEVEL_0_REFUSE_REMOTE` per `harness_cp.cp_shared_types.MCPTrustTier`
      enum-order convention.
    - `PROTOCOL_VERSION_TABLE` — operator-supplied mapping
      `protocol_version → MCPTrustTier`; lookup at evaluator-resolution time.
    - `OPERATOR_HOOK` — operator-supplied callable
      `Callable[[str, str | None], MCPTrustTier]`; concrete signature owed
      to U-CP-18 implementation arc per §27.7.
    """

    CONSERVATIVE = "conservative"
    PROTOCOL_VERSION_TABLE = "protocol_version_table"
    OPERATOR_HOOK = "operator_hook"


# ---------------------------------------------------------------------------
# U-CP-67 envelopes (2 dataclasses; §27.2 verbatim)
# ---------------------------------------------------------------------------


class TrustPolicy(BaseModel):
    """Operator-configured-at-bootstrap immutable trust-policy carrier per
    CP spec v1.10 §27.2.

    Loaded once at bootstrap; immutable per workflow per §27.6 invariant 1.
    The `tier_derivation: TierDerivationRule` field carries Decision 3.D1
    ALLOW-with-tier-floor configuration. `deny_list` wins over `allow_list`
    per §27.6 invariant 3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=False)

    default_tier: MCPTrustTier
    """ALLOW-with-tier-floor threshold for unknown servers (Decision 3.D1)."""

    per_server_overrides: Mapping[str, MCPTrustTier]
    """Per-server explicit tier overrides (server_name → tier)."""

    allow_list: frozenset[str]
    """Exact server names always permitted (bypass tier-floor)."""

    deny_list: frozenset[str]
    """Exact server names always denied (deny wins over allow per §27.6 inv 3)."""

    require_audit_below_tier: MCPTrustTier
    """Any call resolved below this tier ALWAYS sets `audit_required=true`."""

    tier_derivation: TierDerivationRule
    """How to compute tier for unknown servers (CONSERVATIVE default per §27.7)."""


class TrustEvaluation(BaseModel):
    """4-field trust-evaluation-result envelope returned by
    `PerServerTrustEvaluator.evaluate(...)` per CP spec v1.10 §27.2.

    `audit_required=true` triggers tail-keep on the `mcp.trust.evaluate` span
    per §27.4 sampling discipline + §27.6 invariant 5.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=False)

    permitted: bool
    """True iff the call is permitted to proceed."""

    trust_tier_evaluated: MCPTrustTier
    """The MCPTrustTier resolved for this evaluation (known or derived)."""

    decision_reason: TrustDecisionReason
    """Which of the 6 §27.2 decision branches fired."""

    audit_required: bool
    """Tail-keep marker (True forces head=1.0 on `mcp.trust.evaluate` per §27.4)."""
