"""Cross-family fallback chain lifecycle — stage 3b CP_ROUTING (U-RT-23).

Per `Spec_Harness_Runtime_v1.md` v1.1 §5 (C-RT-02 stage 3b invariants) and the
Phase 2 Session 3 Track A atomic decomposition §L5 (U-RT-23). The runtime wires
cross-family fallback over CP's landed primitives:

- `harness_cp.cross_family_fallback_chain.FallbackChain` — the C-CP-04 §4.1
  four-field chain (primary + same-family + cross-family + terminal).
- `harness_cp.cross_family_fallback_chain.on_provider_failure` — the §4.2
  fall-through advancement with §4.3 cross-family attribution flags.
- `harness_cp.fall_through_procedure.fall_through` — the §3.2 layer-advancement
  procedure (distinct concept from cross-family fallback; carried at the
  composer for span-attribution association by downstream units).
- `harness_cp.default_downgrade_rule.DEFAULT_DOWNGRADE_RULE` — the C-CP-12
  §12.1 sub-agent blast-radius downgrade rule (auditable surface).

Per-component landing posture:
- `FallbackChainExhaustedError` — raised when no further candidate is available
  in the chain (AC #1: degenerate all-down case surfaces typed).
- `FallbackChainBindError` — bootstrap-time failure when the manifest carries
  no fallback chain (the runtime requires ≥1 entry to bind `HarnessContext.
  fallback_chain`).
- `FallbackChainStage` — frozen dataclass carrying the bound `FallbackChain` +
  the audit-surfaced `DEFAULT_DOWNGRADE_RULE` (AC #2: downgrade rule
  auditable). The bootstrap orchestrator (U-RT-43) reads `.chain` into
  `HarnessContext.fallback_chain` and the downgrade rule into the audit
  ledger composition at U-RT-32.
- `advance_or_raise(chain, failed)` — wraps `on_provider_failure`; raises
  `FallbackChainExhaustedError` when `next_candidate` is `None`.
- `materialize_fallback_chain_stage(config)` — composer.

Scope discipline (U-RT-23 boundary held): NO retry/breaker primitives
(U-RT-24), NO HITL/handoff registries (U-RT-25/26), NO topology dispatch
(U-RT-40), NO sub-agent dispatch (deferred past Track A). The downgrade rule
is *surfaced* for audit at this unit; consuming it for actual sub-agent
dispatch is a future unit.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    OnFailureResult,
    ProviderCandidate,
    on_provider_failure,
)
from harness_cp.default_downgrade_rule import (
    DEFAULT_DOWNGRADE_RULE,
    SubAgentDefaultDowngrade,
)

from harness_runtime.types import RuntimeConfig

__all__ = [
    "FallbackChainBindError",
    "FallbackChainExhaustedError",
    "FallbackChainStage",
    "advance_or_raise",
    "materialize_fallback_chain_stage",
]


class FallbackChainExhaustedError(Exception):
    """No further candidate is available in the fallback chain.

    Raised by `advance_or_raise` when `on_provider_failure` returns
    `next_candidate=None` — the §4.2 traversal order
    (primary → same-family → cross-family → terminal) has been exhausted.
    Per C-CP-03 §3.5 / C-CP-04 §4.2, the caller emits `fallback.exhausted`
    (always-sampled head=1.0); this exception is the typed-flow surface for
    AC #1 (degenerate all-down case surfaces typed)."""

    def __init__(self, failed: ProviderCandidate) -> None:
        self.failed = failed
        super().__init__(
            f"FallbackChainExhausted: no candidate after "
            f"{failed.provider}:{failed.model} (chain traversal complete)"
        )


class FallbackChainBindError(Exception):
    """Bootstrap-time fallback-chain bind failure (RT-FAIL-BOOTSTRAP).

    Raised when the runtime cannot produce a `FallbackChain` for
    `HarnessContext.fallback_chain` — at HEAD, this is the manifest carrying
    no `fallback_chains` entries (the runtime requires ≥1 at stage 3b)."""


@dataclass(frozen=True, slots=True)
class FallbackChainStage:
    """Frozen result of stage 3b CP_ROUTING fallback-chain materialization.

    Mirrors the L4 / U-RT-21 stage shape. The bootstrap orchestrator (U-RT-43)
    binds `chain` to `HarnessContext.fallback_chain`. `downgrade_rule` is the
    C-CP-12 §12.1 default sub-agent blast-radius downgrade — surfaced here for
    audit composition (AC #2) and consumed for actual sub-agent dispatch at a
    future unit (not landed in Track A).
    """

    chain: FallbackChain
    downgrade_rule: SubAgentDefaultDowngrade


def advance_or_raise(
    chain: FallbackChain, failed: ProviderCandidate
) -> tuple[ProviderCandidate, OnFailureResult]:
    """Advance past `failed` in `chain`; raise on exhaustion.

    Returns the `(next_candidate, OnFailureResult)` tuple — the result carries
    the §4.3 cross-family attribution flags (`cross_family_triggered` /
    `cache_state_lost`). On exhaustion, raises `FallbackChainExhaustedError`
    rather than returning a `None`-bearing result (AC #1 — typed surface).
    """
    result = on_provider_failure(failed, chain)
    if result.next_candidate is None:
        raise FallbackChainExhaustedError(failed)
    return result.next_candidate, result


def materialize_fallback_chain_stage(config: RuntimeConfig) -> FallbackChainStage:
    """Build the fallback-chain stage at stage 3b CP_ROUTING.

    Stage 3b composer. Reads the operator-supplied manifest at
    `config.routing_manifest.fallback_chains` and binds the first entry as the
    runtime's default `FallbackChain` (the `HarnessContext.fallback_chain`
    surface). Raises `FallbackChainBindError` when the manifest carries no
    chain — the bootstrap path requires ≥1 entry at stage 3b.

    `DEFAULT_DOWNGRADE_RULE` is carried directly (the C-CP-12 §12.1 default;
    operator override per U-CP-27 is a future-unit concern). Auditable: the
    rule's `parent_blast_radius`, `child_ceiling`, and `rationale` fields
    surface in `FallbackChainStage.downgrade_rule` for downstream audit-ledger
    composition (AC #2).
    """
    chains = config.routing_manifest.fallback_chains
    if not chains:
        raise FallbackChainBindError(
            "manifest carries no fallback_chains entries; the runtime requires "
            "≥1 chain at stage 3b CP_ROUTING (operator-supplied via "
            "RuntimeConfig.routing_manifest.fallback_chains)"
        )
    return FallbackChainStage(
        chain=chains[0],
        downgrade_rule=DEFAULT_DOWNGRADE_RULE,
    )
