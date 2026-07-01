"""Tests for U-CP-09 — cross-family fallback chain composition (C-CP-04).

Acceptance-criterion coverage:
  #1 FallbackChain 4 fields       -> test_fallback_chain_four_fields
  #2 ProviderFamily cardinality 4 -> test_provider_family_cardinality_four
  #3 on_provider_failure ordering -> test_on_provider_failure_ordering
  #4 cross-family emits both flags-> test_cross_family_emits_both_events
  #5 cache state loss attribution -> test_cache_state_loss_attribution
  #6 composition delegates U-AS-30-> test_composition_delegates_to_u_as_30
"""

from __future__ import annotations

from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
    compose_fallback_chain,
    on_provider_failure,
)

_PRIMARY = ProviderCandidate(
    provider="anthropic", model="claude-opus", family=ProviderFamily.ANTHROPIC
)
_SAME = ProviderCandidate(
    provider="anthropic", model="claude-haiku", family=ProviderFamily.ANTHROPIC
)
_CROSS = ProviderCandidate(provider="openai", model="gpt-4o", family=ProviderFamily.OPENAI)
_TERMINAL = ProviderCandidate(
    provider="ollama", model="llama3", family=ProviderFamily.LOCAL_OPEN_WEIGHT
)


def _chain() -> FallbackChain:
    return compose_fallback_chain(
        primary=_PRIMARY,
        same_family=(_SAME,),
        cross_family=(_CROSS,),
        terminal=_TERMINAL,
    )


def test_fallback_chain_four_fields() -> None:
    assert set(FallbackChain.model_fields) == {
        "primary",
        "same_family",
        "cross_family",
        "terminal",
    }


def test_provider_family_cardinality_four() -> None:
    assert set(ProviderFamily) == {
        ProviderFamily.ANTHROPIC,
        ProviderFamily.OPENAI,
        ProviderFamily.GOOGLE,
        ProviderFamily.LOCAL_OPEN_WEIGHT,
    }
    assert len(ProviderFamily) == 4


def test_on_provider_failure_ordering() -> None:
    chain = _chain()
    # primary fails -> same-family next (no boundary cross).
    r1 = on_provider_failure(_PRIMARY, chain)
    assert r1.next_candidate == _SAME
    assert r1.cross_family_triggered is False
    # same-family fails -> cross-family next.
    r2 = on_provider_failure(_SAME, chain)
    assert r2.next_candidate == _CROSS
    # cross-family fails -> terminal next.
    r3 = on_provider_failure(_CROSS, chain)
    assert r3.next_candidate == _TERMINAL
    # terminal fails -> no next.
    r4 = on_provider_failure(_TERMINAL, chain)
    assert r4.next_candidate is None


def test_cross_family_emits_both_events() -> None:
    chain = _chain()
    # same-family -> cross-family crosses the boundary.
    r = on_provider_failure(_SAME, chain)
    assert r.cross_family_triggered is True
    assert r.cache_state_lost is True


def test_cache_state_loss_attribution() -> None:
    chain = _chain()
    # within-family transition: no cache state loss.
    r_same = on_provider_failure(_PRIMARY, chain)
    assert r_same.cache_state_lost is False
    # cross-family transition: cache state lost.
    r_cross = on_provider_failure(_CROSS, chain)
    assert r_cross.cross_family_triggered is True
    assert r_cross.cache_state_lost is True


def test_composition_delegates_to_u_as_30() -> None:
    # compose_fallback_chain structures AS-resolved candidate tiers into the
    # §4.1 four-field chain; the candidate tiers are caller-supplied (U-AS-30).
    chain = compose_fallback_chain(primary=_PRIMARY, same_family=(), cross_family=(_CROSS,))
    assert chain.primary == _PRIMARY
    assert chain.terminal is None
    assert chain.cross_family == (_CROSS,)
