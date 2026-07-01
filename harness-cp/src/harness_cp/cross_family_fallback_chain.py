"""Cross-family fallback chain composition — U-CP-09.

Implements C-CP-04 §4.1 (the `FallbackChain` four-field record + the
`ProviderFamily` four-value taxonomy), §4.2 (the fall-through ordering rule:
same-family before cross-family before terminal), and §4.3 (cross-family
transition attribution: `fallback.cross_family_triggered` +
`fallback.cache_state_lost`).

Declares `ProviderFamily`, `ProviderCandidate`, `FallbackChain`,
`compose_fallback_chain`, and `on_provider_failure`. Cross-family fallback is
the recovery path when a whole provider family is unavailable; crossing the
family boundary resets the per-family prompt-cache state (the
`anthropic.cache_read_input_tokens` 0.10x cost benefit is provider-bound).

Per-workload-class candidate selection delegates to the AS C-AS-13 §13.4
model-tier escalation chain (U-AS-30 — `MODEL_TIER_ESCALATION_CHAIN`); this
unit composes the chain structure and the on-failure traversal.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.1 U-CP-09 (preserved
verbatim at v2.2-v2.9); Spec_Control_Plane_v1_3.md §4 C-CP-04 §4.1 + §4.2 +
§4.3; Spec_Action_Surface_v1.md C-AS-13 §13.4 (model-tier escalation chain).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ProviderFamily(StrEnum):
    """The four provider families (C-CP-04 §4.1 + AS C-AS-13 §13.4 verbatim).

    Closed at cardinality 4."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    LOCAL_OPEN_WEIGHT = "local_open_weight"


class ProviderCandidate(BaseModel):
    """A single `provider:model` candidate within a fallback chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    family: ProviderFamily


class FallbackChain(BaseModel):
    """An ordered cross-family fallback chain (C-CP-04 §4.1 — four fields)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primary: ProviderCandidate
    same_family: tuple[ProviderCandidate, ...]
    """Ordered fallback within the primary's family."""

    cross_family: tuple[ProviderCandidate, ...]
    """Ordered fallback across families."""

    terminal: ProviderCandidate | None = None
    """Local / open-weight tier per C-AS-13 §13.4."""


class OnFailureResult(BaseModel):
    """The result of advancing the chain past a failed candidate.

    Carries the next candidate (if any) and the C-CP-04 §4.3 cross-family
    attribution flags. When the boundary is crossed, both
    `fallback.cross_family_triggered` and `fallback.cache_state_lost` are
    set true (the per-family prompt-cache state is reset)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    next_candidate: ProviderCandidate | None
    cross_family_triggered: bool
    cache_state_lost: bool


def compose_fallback_chain(
    primary: ProviderCandidate,
    same_family: tuple[ProviderCandidate, ...],
    cross_family: tuple[ProviderCandidate, ...],
    terminal: ProviderCandidate | None = None,
) -> FallbackChain:
    """Compose an ordered fallback chain.

    Per-workload-class candidate selection delegates to U-AS-30 (the
    `MODEL_TIER_ESCALATION_CHAIN` from AS C-AS-13 §13.4); the caller supplies
    the AS-resolved candidate tiers, this function structures them into the
    §4.1 four-field chain. Total; deterministic."""
    return FallbackChain(
        primary=primary,
        same_family=same_family,
        cross_family=cross_family,
        terminal=terminal,
    )


def _ordered_candidates(chain: FallbackChain) -> tuple[ProviderCandidate, ...]:
    """The full traversal order: primary, same-family, cross-family, terminal
    (C-CP-04 §4.2 fall-through ordering)."""
    ordered: list[ProviderCandidate] = [chain.primary]
    ordered.extend(chain.same_family)
    ordered.extend(chain.cross_family)
    if chain.terminal is not None:
        ordered.append(chain.terminal)
    return tuple(ordered)


def on_provider_failure(failed: ProviderCandidate, chain: FallbackChain) -> OnFailureResult:
    """Return the next candidate after `failed`, per the §4.2 ordering rule.

    Same-family fallback precedes cross-family precedes terminal. When the
    successor crosses a family boundary relative to `failed`, the §4.3
    attribution flags are set: `cross_family_triggered = true` and
    `cache_state_lost = true` (per-family prompt-cache state is provider-bound;
    `anthropic.cache_read_input_tokens` resets to 0). Total; deterministic."""
    ordered = _ordered_candidates(chain)
    try:
        idx = ordered.index(failed)
    except ValueError:
        # `failed` is not in the chain — no recovery candidate.
        return OnFailureResult(
            next_candidate=None,
            cross_family_triggered=False,
            cache_state_lost=False,
        )
    if idx + 1 >= len(ordered):
        return OnFailureResult(
            next_candidate=None,
            cross_family_triggered=False,
            cache_state_lost=False,
        )
    nxt = ordered[idx + 1]
    crossed = nxt.family != failed.family
    return OnFailureResult(
        next_candidate=nxt,
        cross_family_triggered=crossed,
        cache_state_lost=crossed,
    )
