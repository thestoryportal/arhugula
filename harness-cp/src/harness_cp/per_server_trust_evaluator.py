"""C-CP-27 PerServerTrustEvaluator — runtime per-server trust evaluation when
H_T acts as MCP client (consuming external MCP servers).

U-CP-68 — third unit of cluster 10-CP-C. Implements
`PerServerTrustEvaluator.evaluate(server_name, primitive, tool_contract,
operator_policy) -> TrustEvaluation` per CP spec v1.10 §27.1 + §27.6
invariants 1-4 (Decision 3.D1 ALLOW-with-tier-floor RATIFIED).

**§27.6 invariants enforced:**
1. Trust policy immutable per workflow (loaded once at bootstrap; verified by
   `TrustPolicy` model frozen ConfigDict — not enforced here)
2. Every MCP-as-client call goes through evaluator (no bypass — verified at
   caller-side wiring, not here)
3. Deny-list wins over allow-list (verified at the 4-branch dispatch below)
4. Unknown-server policy = ALLOW-with-tier-floor (resolved tier derived per
   `policy.tier_derivation`; permitted iff resolved tier >=
   `policy.default_tier`); UNKNOWN_SERVER_* decisions ALWAYS set
   `audit_required=true` per Decision 3.D1

**§27.7 deferred-to-discretion absorbed via FACTOR-OUT pattern** (mirrors
U-CP-63 `PauseContextReader` precedent at cluster 10-CP-B):

- `TierResolver = Callable[[str, ToolContract | None, TierDerivationRule],
  MCPTrustTier]` — operator-supplied callable injected at `__init__`. Resolves
  the tier for an unknown server given the configured `tier_derivation` rule.
  When `tier_derivation == CONSERVATIVE`, the default resolver returns
  `LEVEL_0_REFUSE_REMOTE` (MIN(MCPTrustTier members) per enum-order
  convention).

The 4-class dispatch produces one of 6 `TrustDecisionReason` outcomes:

| Branch | Trigger | Decision | Permitted | Audit |
|---|---|---|---|---|
| 1 | deny_list match | EXPLICIT_DENY | False | True |
| 2 | allow_list match | EXPLICIT_ALLOW | True | per `require_audit_below_tier` |
| 3a | known server, tier >= floor | TIER_FLOOR_PASS | True | per `require_audit_below_tier` |
| 3b | known server, tier < floor | TIER_FLOOR_VIOLATION | False | True |
| 4a | unknown, resolved tier >= floor | UNKNOWN_SERVER_TIER_FLOOR_PASS | True | **True** (inv 4) |
| 4b | unknown, resolved tier < floor | UNKNOWN_SERVER_TIER_FLOOR_VIOLATION | False | True |

The 3 CP fail-classes from §27.5 surface as raisable typed errors at the
caller-site composer (NOT raised here — evaluator returns the decision in the
`TrustEvaluation` envelope; the caller raises the matching fail-class on
`permitted=False`). Pattern mirrors U-CP-60 `ValidatorFramework` decision +
caller-side raise discipline.

Span emission (`mcp.trust.evaluate`) is owned by U-CP-70 (caller-side helper
mirrors the U-CP-65 + U-CP-61 caller-side emission discipline).

Authority: CP spec v1.10 §27.1 + §27.6 (NEW C-CP-27); plan unit U-CP-68
(CP plan v2.15 §1 cluster 10-CP-C, preserved at v2.17).
"""

from __future__ import annotations

import random as _random_module
from collections.abc import Callable
from typing import Any, Final

from harness_as.tool_contract import ToolContract

from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.per_server_trust_types import (
    MCPPrimitive,
    TierDerivationRule,
    TrustDecisionReason,
    TrustEvaluation,
    TrustPolicy,
)

# ---------------------------------------------------------------------------
# Tier-rank helper (StrEnum ordering via name-prefix)
# ---------------------------------------------------------------------------

_TIER_RANK: Final[dict[MCPTrustTier, int]] = {
    MCPTrustTier.LEVEL_0_REFUSE_REMOTE: 0,
    MCPTrustTier.LEVEL_1_SIGNED_PINNED: 1,
    MCPTrustTier.LEVEL_2_SANDBOX_ALL: 2,
    MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT: 3,
}
"""MCPTrustTier name-prefix ordering for tier-floor comparison.

The `MCPTrustTier(StrEnum)` value strings are kebab-case `"level-N-..."` and
not naturally `>=` comparable — this rank table provides the canonical ordering
for `tier >= floor` evaluation per §27.6 invariants 3 + 4. Rank 0 is the
minimum (most restrictive); rank 3 is the maximum (most permissive)."""

_CONSERVATIVE_MIN_TIER: Final[MCPTrustTier] = MCPTrustTier.LEVEL_0_REFUSE_REMOTE
"""CONSERVATIVE tier-derivation resolution per §27.7 (MIN of MCPTrustTier members)."""


TierResolver = Callable[[str, ToolContract | None, TierDerivationRule], MCPTrustTier]
"""Operator-supplied callable for unknown-server tier derivation per §27.7.

Receives the server_name + tool_contract (populated when primitive=tool) +
the configured tier_derivation rule, returns the resolved MCPTrustTier. The
caller is expected to dispatch on the rule (PROTOCOL_VERSION_TABLE lookup vs
OPERATOR_HOOK invocation vs CONSERVATIVE default). Concrete OPERATOR_HOOK
signature owed to U-CP-18 implementation arc per §27.7."""


def _default_tier_resolver(
    server_name: str,
    tool_contract: ToolContract | None,
    rule: TierDerivationRule,
) -> MCPTrustTier:
    """Default tier resolver — CONSERVATIVE returns MIN, other rules raise.

    The default resolver only handles `CONSERVATIVE` (the spec-prescribed
    default per §27.7). When the policy declares `PROTOCOL_VERSION_TABLE` or
    `OPERATOR_HOOK`, the caller MUST inject a custom `TierResolver` at
    `__init__`; otherwise this default raises `ValueError` (an operator
    misconfiguration, not a runtime trust-evaluation failure)."""
    if rule == TierDerivationRule.CONSERVATIVE:
        return _CONSERVATIVE_MIN_TIER
    raise ValueError(
        f"TierDerivationRule {rule} requires an operator-supplied TierResolver "
        f"injected at PerServerTrustEvaluator.__init__; default resolver only "
        f"handles CONSERVATIVE (per §27.7 deferred-to-discretion)."
    )


# ---------------------------------------------------------------------------
# PerServerTrustEvaluator
# ---------------------------------------------------------------------------


class PerServerTrustEvaluator:
    """Runtime per-server trust evaluation when H_T acts as MCP client.

    Distinct from H_T-as-MCP-server role landed at U-RT-62 (Q5 disjointness
    pin closed at CP spec v1.10 §27). Instantiated at Stage 3a per §27.3
    alongside `MCPClientHost` (runtime spec v1.13 §14.9.3); bound to
    `ctx.per_server_trust_evaluator`.

    The trust policy is loaded from bootstrap config and immutable per
    workflow per §27.6 invariant 1 (enforced by `TrustPolicy` model frozen
    ConfigDict at U-CP-67).
    """

    def __init__(
        self,
        *,
        tier_resolver: TierResolver | None = None,
    ) -> None:
        """Construct evaluator with optional operator-supplied tier resolver.

        :param tier_resolver: callable invoked for unknown servers; defaults
            to `_default_tier_resolver` which handles CONSERVATIVE only.
            Operator MUST inject a custom resolver when the policy declares
            PROTOCOL_VERSION_TABLE or OPERATOR_HOOK (else default raises at
            unknown-server-with-non-CONSERVATIVE-rule evaluation)."""
        self._tier_resolver: TierResolver = tier_resolver or _default_tier_resolver

    async def evaluate(
        self,
        server_name: str,
        primitive: MCPPrimitive,
        tool_contract: ToolContract | None,
        operator_policy: TrustPolicy,
    ) -> TrustEvaluation:
        """Evaluate per-server trust per §27.1 signature + §27.6 invariants.

        :param server_name: MCP server registry identifier
        :param primitive: MCPPrimitive (tool / resource / prompt / sampling);
            currently unused at evaluation branch but kept on the signature
            per §27.1 + carried through to the span attribute by U-CP-70
            caller-side emission
        :param tool_contract: tool contract iff primitive=tool; passed to
            tier resolver for OPERATOR_HOOK-aware policies
        :param operator_policy: immutable TrustPolicy loaded at bootstrap

        :returns: TrustEvaluation envelope with one of 6 §27.2 decision
            reasons; `audit_required=True` triggers tail-keep on the
            mcp.trust.evaluate span per §27.4
        """
        # Branch 1 — Deny wins over allow per §27.6 invariant 3.
        if server_name in operator_policy.deny_list:
            return TrustEvaluation(
                permitted=False,
                trust_tier_evaluated=_CONSERVATIVE_MIN_TIER,
                decision_reason=TrustDecisionReason.EXPLICIT_DENY,
                audit_required=True,
            )

        # Branch 2 — Allow-list bypass per §27.6 invariant 3 ordering.
        if server_name in operator_policy.allow_list:
            # Allow-list bypass uses the per-server override tier if present,
            # else the default tier (the allow-list grant does NOT itself
            # establish a tier — it only bypasses the floor check).
            tier = operator_policy.per_server_overrides.get(
                server_name, operator_policy.default_tier
            )
            return TrustEvaluation(
                permitted=True,
                trust_tier_evaluated=tier,
                decision_reason=TrustDecisionReason.EXPLICIT_ALLOW,
                audit_required=_require_audit_for_tier(
                    tier, operator_policy.require_audit_below_tier
                ),
            )

        # Branch 3 — Known server (per_server_overrides match).
        if server_name in operator_policy.per_server_overrides:
            tier = operator_policy.per_server_overrides[server_name]
            if _tier_at_or_above_floor(tier, operator_policy.default_tier):
                return TrustEvaluation(
                    permitted=True,
                    trust_tier_evaluated=tier,
                    decision_reason=TrustDecisionReason.TIER_FLOOR_PASS,
                    audit_required=_require_audit_for_tier(
                        tier, operator_policy.require_audit_below_tier
                    ),
                )
            return TrustEvaluation(
                permitted=False,
                trust_tier_evaluated=tier,
                decision_reason=TrustDecisionReason.TIER_FLOOR_VIOLATION,
                audit_required=True,
            )

        # Branch 4 — Unknown server. Resolve tier per tier_derivation rule
        # and apply ALLOW-with-tier-floor (Decision 3.D1).
        resolved_tier = self._tier_resolver(
            server_name, tool_contract, operator_policy.tier_derivation
        )
        if _tier_at_or_above_floor(resolved_tier, operator_policy.default_tier):
            return TrustEvaluation(
                permitted=True,
                trust_tier_evaluated=resolved_tier,
                decision_reason=TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS,
                # §27.6 invariant 4 — UNKNOWN decisions ALWAYS audit-required.
                audit_required=True,
            )
        return TrustEvaluation(
            permitted=False,
            trust_tier_evaluated=resolved_tier,
            decision_reason=TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_VIOLATION,
            audit_required=True,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tier_at_or_above_floor(tier: MCPTrustTier, floor: MCPTrustTier) -> bool:
    """Tier-floor `>=` comparison per name-prefix ordering."""
    return _TIER_RANK[tier] >= _TIER_RANK[floor]


def _require_audit_for_tier(tier: MCPTrustTier, audit_floor: MCPTrustTier) -> bool:
    """Audit required iff `tier < audit_floor` per `TrustPolicy.require_audit_below_tier`."""
    return _TIER_RANK[tier] < _TIER_RANK[audit_floor]


# ---------------------------------------------------------------------------
# U-CP-70 — mcp.trust.evaluate span emission + head sampling
# ---------------------------------------------------------------------------

MCP_TRUST_EVALUATE_SPAN_NAME: Final[str] = "mcp.trust.evaluate"
"""Span site emitted per evaluation per CP spec v1.10 §27.4."""

# Pattern-P1 alignment: attribute name literals match OD spec v1.9 §C-OD-31.1
# byte-exact. Runtime emits string literals; OD schema module is NOT imported
# at runtime per Phase D iteration-1 F1-03 absorption (mirrors the U-CP-65
# caller-side emission pattern).
ATTR_MCP_TRUST_SERVER_NAME: Final[str] = "mcp.trust.server_name"
ATTR_MCP_TRUST_PRIMITIVE_KIND: Final[str] = "mcp.trust.primitive_kind"
ATTR_MCP_TRUST_DECISION_REASON: Final[str] = "mcp.trust.decision_reason"
ATTR_MCP_TRUST_AUDIT_REQUIRED: Final[str] = "mcp.trust.audit_required"
ATTR_MCP_TRUST_TIER_EVALUATED: Final[str] = "mcp.trust.tier_evaluated"

_NON_AUDIT_SAMPLE_RATE: Final[float] = 0.1
"""Head sample rate for non-audit-required evaluations per §27.4 (head=0.1)."""


def _default_rng() -> _random_module.Random:
    return _random_module.Random()


def emit_mcp_trust_evaluate_span(
    tracer: Any,
    evaluation: TrustEvaluation,
    server_name: str,
    primitive: MCPPrimitive,
    *,
    rng: _random_module.Random | None = None,
) -> Any:  # returns Span | None — typed Any to avoid OTel SDK coupling
    """Emit the `mcp.trust.evaluate` span for a single PerServerTrustEvaluator
    result per CP spec v1.10 §27.4 + sampling discipline.

    **Caller-side emission pattern** (mirrors U-CP-65 `emit_pause_captured_span`
    + U-CP-61 `emit_validator_evaluate_span`): the framework class
    (PerServerTrustEvaluator) is decoupled from tracer dependencies — the
    caller (Stage 5 RuntimeToolDispatcher per §27.3) wraps the evaluator
    invocation with this helper.

    **Sampling discipline** per §27.4 + §27.6 invariant 5:
    - `audit_required=True` → **always** emit (head=1.0)
    - `audit_required=False` → head=0.1 (per `_NON_AUDIT_SAMPLE_RATE`)

    UNKNOWN_SERVER_* decisions ALWAYS carry `audit_required=True` per
    Decision 3.D1 + §27.6 invariant 4, so they ALWAYS emit (covered by the
    audit_required branch above).

    :param tracer: opentelemetry Tracer-like (typed Any to avoid SDK coupling)
    :param evaluation: TrustEvaluation result from
        `PerServerTrustEvaluator.evaluate(...)`
    :param server_name: MCP server registry identifier (carried to span attr)
    :param primitive: MCPPrimitive (carried to span attr)
    :param rng: optional Random instance for deterministic testing; defaults
        to a fresh Random() per call when omitted

    :returns: the emitted Span context manager value (after exit) OR None when
        sampled out at the non-audit branch
    """
    if not evaluation.audit_required:
        sampler = rng if rng is not None else _default_rng()
        if sampler.random() >= _NON_AUDIT_SAMPLE_RATE:
            return None
    with tracer.start_as_current_span(MCP_TRUST_EVALUATE_SPAN_NAME) as span:
        span.set_attribute(ATTR_MCP_TRUST_SERVER_NAME, server_name)
        span.set_attribute(ATTR_MCP_TRUST_PRIMITIVE_KIND, primitive.value)
        span.set_attribute(ATTR_MCP_TRUST_DECISION_REASON, evaluation.decision_reason.value)
        span.set_attribute(ATTR_MCP_TRUST_AUDIT_REQUIRED, evaluation.audit_required)
        span.set_attribute(ATTR_MCP_TRUST_TIER_EVALUATED, evaluation.trust_tier_evaluated.value)
        return span
