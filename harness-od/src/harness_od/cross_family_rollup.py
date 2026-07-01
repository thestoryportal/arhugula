"""Cross-family `provider_discriminator` rollup + tokenizer anchor — U-OD-21.

Implements C-OD-15 §15.1 (cross-family `provider_discriminator` cost rollup),
§15.2 (tokenization-version anchor), §15.3 (cross-family fallback chain
composition reference).

`CrossFamilyTag` is the bounded family-tag vocabulary. `RollupAxis` enumerates
the four §15.1 rollup axes (the dispatch-type `PER_DISPATCH_KIND` added at v1.30
per B-COST-DISCRIMINATOR-TAXONOMY; `DispatchKind` is its bounded vocabulary,
homed in the U-OD-20 carrier). `rollup_costs_by_axis` aggregates a list of
`SpanCostRecord` (the U-OD-20 carrier) into per-axis `CrossFamilyCostRollup`
records. `TokenizerVersionAnchor` /
`TOKENIZER_VERSION_ANCHOR_REQUIREMENT` carry the §15.2 anchor.
`FallbackChainCostComposition` carries the §15.3 chain-advancement seam.

v2.8 (D-5): `rollup_costs_by_axis` is now materializable — the U-OD-20
`SpanCostRecord` carrier (grown to 12 fields at OD plan v2.8 §3.5.3) carries
the three rollup-key fields `provider_discriminator` / `gen_ai_provider_name` /
`gen_ai_request_model`. `PER_PROVIDER_DISCRIMINATOR` groups by
`provider_discriminator`; `PER_PROVIDER_AND_MODEL` groups by
`(gen_ai_provider_name, gen_ai_request_model)`; `PER_FALLBACK_EVENT` reads
per-attempt provider identity from `gen_ai_provider_name` discriminated by
`retry_attempt_number`. The `provider_discriminator` string is validated
against `CrossFamilyTag` at this unit (acc #9) — `CrossFamilyTag` is the
bounded vocabulary, homed here to avoid a U-OD-20 → U-OD-21 carrier cycle.

Source authority per F2-10 closure: the `provider_discriminator` substrate is
the `c7-observability` SKILL.md primary anchor; ADR-F1 v1.2 §Decision is
composition context, not the attribute-name declaration site (acc #8).

Authority: Implementation_Plan_Operational_Discipline_v2_8.md §3.5.4 U-OD-21
(v2.8 D-5 revision — `rollup_costs_by_axis` materializable against the grown
carrier; signature unchanged; acc #9 extended; all other surfaces preserved
verbatim from v2.1/v2.6); Spec_Operational_Discipline_v1_2.md §15 C-OD-15
§15.1 + §15.2 + §15.3 (preserved verbatim into v1.3 per v1.3 §0.1);
ADR-D6 v1.1 §1.5; ADR-F1 v1.2 §Decision (chain-advancement seam).

Depends on: [U-OD-04, U-OD-18, U-OD-20, U-CP-NN (cross-axis: CP — C-CP-04
cross-family fallback chain)]. The U-CP-NN edge is a cross-axis dependency
resolved at Phase 7 sub-phase 7c — NOT a 7b blocker; no typed surface is
imported from any CP package here.
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.cost_formula import PriceRateKey
from harness_od.idempotency_join_dedup import SpanCostRecord

__all__ = [
    "TOKENIZER_VERSION_ANCHOR_REQUIREMENT",
    "CrossFamilyCostRollup",
    "CrossFamilyRollupError",
    "CrossFamilyTag",
    "FallbackChainCostComposition",
    "RollupAxis",
    "TokenizerVersionAnchor",
    "rollup_costs_by_axis",
]


class CrossFamilyTag(StrEnum):
    """The bounded cross-family fallback-chain family-tag vocabulary (§15.1).

    Per F2-10 closure (`c7-observability` SKILL.md primary anchor; ADR-F1 v1.2
    §Decision composition context). Extensible per chain composition. This is
    the bounded vocabulary that `rollup_costs_by_axis` validates the
    `SpanCostRecord.provider_discriminator` string against (acc #9).
    """

    FRONTIER_MANAGED = "frontier_managed"
    FRONTIER_MANAGED_ALT = "frontier_managed_alt"
    LOCAL_OLLAMA = "local_ollama"


class RollupAxis(StrEnum):
    """The 4 cross-cost-rollup axes (C-OD-15 §15.1; PER_DISPATCH_KIND added v1.30).

    `PER_PROVIDER_DISCRIMINATOR` — per-cross-family-tag cost (skips records with
    no chain-level family tag, §15.1.2); `PER_PROVIDER_AND_MODEL` —
    per-(provider, model) cost; `PER_FALLBACK_EVENT` — per-retry-attempt cost
    with family-tag rollup; `PER_DISPATCH_KIND` (v1.30,
    B-COST-DISCRIMINATOR-TAXONOMY) — per-dispatch-type cost (the operator-meaningful
    llm-vs-tool-vs-validator-vs-webhook split), keyed on the typed
    `SpanCostRecord.dispatch_kind`.
    """

    PER_PROVIDER_DISCRIMINATOR = "PER_PROVIDER_DISCRIMINATOR"
    PER_PROVIDER_AND_MODEL = "PER_PROVIDER_AND_MODEL"
    PER_FALLBACK_EVENT = "PER_FALLBACK_EVENT"
    PER_DISPATCH_KIND = "PER_DISPATCH_KIND"


class CrossFamilyCostRollup(BaseModel):
    """An aggregated cost rollup along one `RollupAxis` (C-OD-15 §15.1).

    Frozen → `Eq`. `group_key` is the projected key string for the rollup axis;
    `total_cost` sums `SpanCostRecord.total_cost` over the group;
    `span_count` is the group cardinality.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rollup_axis: RollupAxis
    group_key: str
    total_cost: float
    span_count: int


class TokenizerVersionAnchor(StrEnum):
    """The 2 tokenization-version anchor options (C-OD-15 §15.2 verbatim).

    `OPTION_A_ATTRIBUTE_ON_EVERY_SPAN` — `tokenizer_version` attribute on every
    span; `OPTION_B_VERSIONED_PRICE_TABLE` — versioned price table keyed on
    `(provider, model, tokenizer_version)`.
    """

    OPTION_A_ATTRIBUTE_ON_EVERY_SPAN = "OPTION_A_ATTRIBUTE_ON_EVERY_SPAN"
    OPTION_B_VERSIONED_PRICE_TABLE = "OPTION_B_VERSIONED_PRICE_TABLE"


#: §15.2 tokenization-version anchor requirement text, verbatim (acc #5).
TOKENIZER_VERSION_ANCHOR_REQUIREMENT: str = (
    "Phase 6+ dashboard authors MUST select OPTION_A or OPTION_B; failing to "
    "anchor on tokenizer_version produces silent cost-dashboard breakage on "
    "model version transitions"
)


class FallbackChainCostComposition(BaseModel):
    """A §15.3 cross-family fallback-chain cost-attribution composition.

    Frozen → `Eq`. `parent_span_family_tag` is the parent's retained family
    tag; `per_attempt_provider` is the per-retry actual provider;
    `per_attempt_rate_key` is the per-attempt `PriceRateKey`;
    `cache_state_loss_on_cross_family` is `True` when a cross-family transition
    loses cache state (`anthropic.cache_read_input_tokens = 0`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_span_family_tag: CrossFamilyTag
    per_attempt_provider: str
    per_attempt_rate_key: PriceRateKey
    cache_state_loss_on_cross_family: bool


class CrossFamilyRollupError(Exception):
    """Raised when a span's `provider_discriminator` is not a `CrossFamilyTag`.

    The Python materialization of the acc #9 validation — `provider_discriminator`
    is carried as a `str` on `SpanCostRecord` (to avoid a U-OD-20 → U-OD-21
    carrier cycle) and validated against the bounded `CrossFamilyTag`
    vocabulary at this unit. Stack is Pydantic v2 + stdlib, no `Result`
    framework pull (CLAUDE.md §3.2 / I-6).
    """


def _validate_family_tag(provider_discriminator: str) -> CrossFamilyTag:
    """Validate `provider_discriminator` against `CrossFamilyTag` (acc #9).

    Raises `CrossFamilyRollupError` if the string is not a member of the
    bounded `CrossFamilyTag` vocabulary.
    """
    try:
        return CrossFamilyTag(provider_discriminator)
    except ValueError as exc:
        raise CrossFamilyRollupError(
            f"provider_discriminator '{provider_discriminator}' is not a "
            f"member of the bounded CrossFamilyTag vocabulary (C-OD-15 §15.1)"
        ) from exc


def rollup_costs_by_axis(
    span_records: list[SpanCostRecord],
    axis: RollupAxis,
) -> list[CrossFamilyCostRollup]:
    """Aggregate per-span costs into per-`axis` rollups (C-OD-15 §15.1, acc #3).

    `PER_PROVIDER_DISCRIMINATOR` keys on the family tag — each non-`None`
    `provider_discriminator` is validated against `CrossFamilyTag` (acc #9);
    records with `provider_discriminator is None` (a per-dispatch record with no
    chain-level family tag, §15.1.2) are skipped. `PER_PROVIDER_AND_MODEL` keys
    on the `(gen_ai_provider_name, gen_ai_request_model)` tuple.
    `PER_FALLBACK_EVENT` preserves per-attempt provider identity — keyed on
    `gen_ai_provider_name` discriminated by `retry_attempt_number`.
    `PER_DISPATCH_KIND` (v1.30) keys on the typed `dispatch_kind` enum — the
    operator-meaningful dispatch-type (llm/tool/validator/webhook) cost split.

    The three rollup keys project from the U-OD-20 `SpanCostRecord` 12-field
    carrier (`provider_discriminator` / `gen_ai_provider_name` /
    `gen_ai_request_model`) per v2.8 §3.5.3 (D-5).
    """
    cost_by_key: dict[str, float] = defaultdict(float)
    count_by_key: dict[str, int] = defaultdict(int)

    for record in span_records:
        if axis is RollupAxis.PER_PROVIDER_DISCRIMINATOR:
            if record.provider_discriminator is None:
                continue  # no chain-level family tag at the per-dispatch site (§15.1.2)
            group_key = _validate_family_tag(record.provider_discriminator).value
        elif axis is RollupAxis.PER_PROVIDER_AND_MODEL:
            group_key = f"{record.gen_ai_provider_name}::{record.gen_ai_request_model}"
        elif axis is RollupAxis.PER_DISPATCH_KIND:
            group_key = record.dispatch_kind.value
        else:  # RollupAxis.PER_FALLBACK_EVENT
            attempt = record.retry_attempt_number if record.retry_attempt_number else 1
            group_key = f"{record.gen_ai_provider_name}::attempt-{attempt}"
        cost_by_key[group_key] += record.total_cost
        count_by_key[group_key] += 1

    return [
        CrossFamilyCostRollup(
            rollup_axis=axis,
            group_key=group_key,
            total_cost=cost_by_key[group_key],
            span_count=count_by_key[group_key],
        )
        for group_key in sorted(cost_by_key)
    ]
