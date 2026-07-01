"""Tests for U-OD-21 — cross-family rollup + tokenization-version anchor.

Test set per the U-OD-21 §3.5.4 (v2.8) `Tests:` field — covers acceptance
#1-#9 against C-OD-15 §15.1 / §15.2 / §15.3. Rollup tests are executable
against the grown U-OD-20 12-field `SpanCostRecord` carrier (v2.8 D-5).
"""

from __future__ import annotations

import pytest
from harness_cp.engine_namespace import ReplayDisposition
from harness_od.cost_formula import PriceRateKey
from harness_od.cross_family_rollup import (
    TOKENIZER_VERSION_ANCHOR_REQUIREMENT,
    CrossFamilyCostRollup,
    CrossFamilyRollupError,
    CrossFamilyTag,
    FallbackChainCostComposition,
    RollupAxis,
    TokenizerVersionAnchor,
    rollup_costs_by_axis,
)
from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord


def _cost_record(
    *,
    span_id: str = "span-1",
    total_cost: float = 1.0,
    attempt: int | None = None,
    provider_discriminator: str | None = "frontier_managed",
    dispatch_kind: DispatchKind = DispatchKind.LLM,
    gen_ai_provider_name: str = "anthropic",
    gen_ai_request_model: str = "claude-opus-4-7",
) -> SpanCostRecord:
    """A `SpanCostRecord` — the U-OD-20 carrier (13 fields at v1.30)."""
    return SpanCostRecord(
        span_id=span_id,
        idempotency_key="idem-1",
        total_cost=total_cost,
        total_latency_ms=100,
        derived_keys=(),
        engine_replay_disposition=ReplayDisposition.NO_REPLAY,
        retry_attempt_number=attempt,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator=provider_discriminator,
        dispatch_kind=dispatch_kind,
        gen_ai_provider_name=gen_ai_provider_name,
        gen_ai_request_model=gen_ai_request_model,
    )


# --- acc #1 ----------------------------------------------------------------
def test_cross_family_tag_bounded_enum() -> None:
    """`CrossFamilyTag` is a bounded enum per F2-10 closure (acc #1)."""
    assert len(CrossFamilyTag) >= 3
    assert {t.value for t in CrossFamilyTag} >= {
        "frontier_managed",
        "frontier_managed_alt",
        "local_ollama",
    }


# --- acc #2 ----------------------------------------------------------------
def test_rollup_axis_cardinality_four() -> None:
    """`RollupAxis` enumerates exactly 4 values per §15.1 (PER_DISPATCH_KIND v1.30)."""
    assert len(RollupAxis) == 4
    assert RollupAxis.PER_DISPATCH_KIND in RollupAxis


# --- acc #3 — rollup aggregation per axis ----------------------------------
def test_rollup_per_provider_discriminator() -> None:
    """`PER_PROVIDER_DISCRIMINATOR` keys on the family tag (§15.1, acc #3)."""
    records = [
        _cost_record(provider_discriminator="frontier_managed", total_cost=2.0),
        _cost_record(provider_discriminator="frontier_managed", total_cost=3.0),
        _cost_record(provider_discriminator="local_ollama", total_cost=0.5),
    ]
    rollups = rollup_costs_by_axis(records, RollupAxis.PER_PROVIDER_DISCRIMINATOR)
    by_key = {r.group_key: r for r in rollups}
    assert by_key["frontier_managed"].total_cost == 5.0
    assert by_key["frontier_managed"].span_count == 2
    assert by_key["local_ollama"].total_cost == 0.5
    assert by_key["local_ollama"].span_count == 1


def test_rollup_per_provider_and_model() -> None:
    """`PER_PROVIDER_AND_MODEL` keys on `(provider, model)` (§15.1, acc #3)."""
    records = [
        _cost_record(
            gen_ai_provider_name="anthropic",
            gen_ai_request_model="claude-opus-4-7",
            total_cost=1.0,
        ),
        _cost_record(
            gen_ai_provider_name="anthropic",
            gen_ai_request_model="claude-opus-4-7",
            total_cost=2.0,
        ),
        _cost_record(gen_ai_provider_name="openai", gen_ai_request_model="gpt-5", total_cost=4.0),
    ]
    rollups = rollup_costs_by_axis(records, RollupAxis.PER_PROVIDER_AND_MODEL)
    by_key = {r.group_key: r for r in rollups}
    assert by_key["anthropic::claude-opus-4-7"].total_cost == 3.0
    assert by_key["anthropic::claude-opus-4-7"].span_count == 2
    assert by_key["openai::gpt-5"].total_cost == 4.0


def test_rollup_per_fallback_event_preserves_provider() -> None:
    """`PER_FALLBACK_EVENT` preserves per-attempt provider identity (§15.1)."""
    records = [
        _cost_record(gen_ai_provider_name="anthropic", attempt=1, total_cost=1.0),
        _cost_record(gen_ai_provider_name="openai", attempt=2, total_cost=2.0),
    ]
    rollups = rollup_costs_by_axis(records, RollupAxis.PER_FALLBACK_EVENT)
    keys = {r.group_key for r in rollups}
    assert keys == {"anthropic::attempt-1", "openai::attempt-2"}


def test_rollup_key_projects_from_span_cost_record_fields() -> None:
    """Rollup keys project from `SpanCostRecord` carrier fields (acc #9, D-5)."""
    record = _cost_record(
        provider_discriminator="frontier_managed_alt",
        gen_ai_provider_name="gemini",
        gen_ai_request_model="gemini-3-pro",
    )
    discr = rollup_costs_by_axis([record], RollupAxis.PER_PROVIDER_DISCRIMINATOR)
    assert discr[0].group_key == "frontier_managed_alt"
    model = rollup_costs_by_axis([record], RollupAxis.PER_PROVIDER_AND_MODEL)
    assert model[0].group_key == "gemini::gemini-3-pro"


def test_provider_discriminator_validated_against_cross_family_tag() -> None:
    """`provider_discriminator` is validated against `CrossFamilyTag` (acc #9).

    A record carrying a `provider_discriminator` string outside the bounded
    `CrossFamilyTag` vocabulary raises `CrossFamilyRollupError` at the
    `PER_PROVIDER_DISCRIMINATOR` rollup.
    """
    bad = _cost_record(provider_discriminator="not_a_known_family")
    with pytest.raises(CrossFamilyRollupError):
        rollup_costs_by_axis([bad], RollupAxis.PER_PROVIDER_DISCRIMINATOR)


# --- v1.30 B-COST-DISCRIMINATOR-TAXONOMY -----------------------------------
def test_rollup_per_dispatch_kind() -> None:
    """`PER_DISPATCH_KIND` keys on the typed `dispatch_kind` enum (§15.1, v1.30).

    The operator-meaningful dispatch-type breakdown — and crucially it does NOT
    raise on the production tagging that `PER_PROVIDER_DISCRIMINATOR` rejects
    (production records carry `provider_discriminator=None`).
    """
    records = [
        _cost_record(dispatch_kind=DispatchKind.LLM, provider_discriminator=None, total_cost=2.0),
        _cost_record(dispatch_kind=DispatchKind.LLM, provider_discriminator=None, total_cost=3.0),
        _cost_record(dispatch_kind=DispatchKind.TOOL, provider_discriminator=None, total_cost=0.5),
        _cost_record(
            dispatch_kind=DispatchKind.VALIDATOR, provider_discriminator=None, total_cost=0.25
        ),
        _cost_record(
            dispatch_kind=DispatchKind.WEBHOOK, provider_discriminator=None, total_cost=0.1
        ),
    ]
    rollups = rollup_costs_by_axis(records, RollupAxis.PER_DISPATCH_KIND)
    by_key = {r.group_key: r for r in rollups}
    assert by_key["llm"].total_cost == 5.0
    assert by_key["llm"].span_count == 2
    assert by_key["tool"].total_cost == 0.5
    assert by_key["validator"].total_cost == 0.25
    assert by_key["webhook"].total_cost == 0.1
    # sum-invariant: the dispatch-kind partition recovers the full run total.
    assert sum(r.total_cost for r in rollups) == 5.85


def test_per_provider_discriminator_skips_none_records() -> None:
    """A `provider_discriminator=None` record is skipped, not raised (§15.1.2, v1.30).

    Production per-dispatch records carry `None` (no chain-level family tag);
    `PER_PROVIDER_DISCRIMINATOR` skips them rather than raising — so a mix of
    tagged (synthetic / §15.3) + untagged (production) records rolls up the
    tagged ones only.
    """
    records = [
        _cost_record(provider_discriminator=None, total_cost=9.0),
        _cost_record(provider_discriminator="frontier_managed", total_cost=2.0),
    ]
    rollups = rollup_costs_by_axis(records, RollupAxis.PER_PROVIDER_DISCRIMINATOR)
    by_key = {r.group_key: r for r in rollups}
    assert set(by_key) == {"frontier_managed"}
    assert by_key["frontier_managed"].total_cost == 2.0
    # all-None production records → empty rollup, no raise.
    assert (
        rollup_costs_by_axis(
            [_cost_record(provider_discriminator=None)], RollupAxis.PER_PROVIDER_DISCRIMINATOR
        )
        == []
    )


# --- acc #4 ----------------------------------------------------------------
def test_tokenizer_anchor_two_options() -> None:
    """`TokenizerVersionAnchor` enumerates exactly 2 options per §15.2."""
    assert len(TokenizerVersionAnchor) == 2
    assert set(TokenizerVersionAnchor) == {
        TokenizerVersionAnchor.OPTION_A_ATTRIBUTE_ON_EVERY_SPAN,
        TokenizerVersionAnchor.OPTION_B_VERSIONED_PRICE_TABLE,
    }


# --- acc #5 ----------------------------------------------------------------
def test_tokenizer_anchor_requirement_byte_exact() -> None:
    """`TOKENIZER_VERSION_ANCHOR_REQUIREMENT` carries §15.2 text verbatim."""
    assert TOKENIZER_VERSION_ANCHOR_REQUIREMENT == (
        "Phase 6+ dashboard authors MUST select OPTION_A or OPTION_B; failing "
        "to anchor on tokenizer_version produces silent cost-dashboard "
        "breakage on model version transitions"
    )


# --- acc #6 ----------------------------------------------------------------
def test_fallback_chain_parent_family_tag_retained() -> None:
    """`FallbackChainCostComposition` retains the parent family tag (§15.3)."""
    composition = FallbackChainCostComposition(
        parent_span_family_tag=CrossFamilyTag.FRONTIER_MANAGED,
        per_attempt_provider="anthropic",
        per_attempt_rate_key=PriceRateKey(
            provider_name="anthropic",
            model="claude-opus-4-7",
            tokenizer_version="v1",
        ),
        cache_state_loss_on_cross_family=False,
    )
    assert composition.parent_span_family_tag is CrossFamilyTag.FRONTIER_MANAGED


def test_fallback_chain_per_attempt_provider_updates() -> None:
    """Per-attempt provider updates per retry (§15.3)."""
    first = FallbackChainCostComposition(
        parent_span_family_tag=CrossFamilyTag.FRONTIER_MANAGED,
        per_attempt_provider="anthropic",
        per_attempt_rate_key=PriceRateKey(
            provider_name="anthropic", model="claude-opus-4-7", tokenizer_version="v1"
        ),
        cache_state_loss_on_cross_family=False,
    )
    second = FallbackChainCostComposition(
        parent_span_family_tag=CrossFamilyTag.FRONTIER_MANAGED,
        per_attempt_provider="openai",
        per_attempt_rate_key=PriceRateKey(
            provider_name="openai", model="gpt-5", tokenizer_version="v2"
        ),
        cache_state_loss_on_cross_family=True,
    )
    assert first.per_attempt_provider != second.per_attempt_provider
    assert first.parent_span_family_tag is second.parent_span_family_tag


def test_cache_state_loss_on_cross_family() -> None:
    """Cache state loss on cross-family transition is carried (§15.3)."""
    composition = FallbackChainCostComposition(
        parent_span_family_tag=CrossFamilyTag.FRONTIER_MANAGED,
        per_attempt_provider="openai",
        per_attempt_rate_key=PriceRateKey(
            provider_name="openai", model="gpt-5", tokenizer_version="v2"
        ),
        cache_state_loss_on_cross_family=True,
    )
    assert composition.cache_state_loss_on_cross_family is True


# --- acc #8 ----------------------------------------------------------------
def test_provider_discriminator_source_authority_c7() -> None:
    """`provider_discriminator` substrate authority is `c7-observability` (acc #8)."""
    from harness_od import cross_family_rollup as mod

    assert mod.__doc__ is not None
    assert "c7-observability" in mod.__doc__


# --- acc #7 — cross-axis edge ----------------------------------------------
def test_cross_axis_edge_to_u_cp_nn_c_cp_04() -> None:
    """Cross-axis edge to U-CP-NN / C-CP-04 is declared in the module (acc #7)."""
    from harness_od import cross_family_rollup as mod

    assert mod.__doc__ is not None
    assert "U-CP-NN" in mod.__doc__
    assert "C-CP-04" in mod.__doc__


# --- acc #9 — SpanCostRecord carrier in-cone -------------------------------
def test_span_cost_record_param_carrier_u_od_20_in_cone() -> None:
    """`rollup_costs_by_axis` consumes the U-OD-20 `SpanCostRecord` (acc #9)."""
    rollup = rollup_costs_by_axis([_cost_record()], RollupAxis.PER_PROVIDER_DISCRIMINATOR)
    assert isinstance(rollup[0], CrossFamilyCostRollup)


def test_depends_on_u_od_20_edge_declared() -> None:
    """The `[U-OD-20]` `Depends on` edge is declared in the module (acc #9)."""
    from harness_od import cross_family_rollup as mod

    assert mod.__doc__ is not None
    assert "U-OD-20" in mod.__doc__
