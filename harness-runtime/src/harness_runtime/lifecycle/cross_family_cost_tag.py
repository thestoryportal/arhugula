"""Provider → cross-family cost-attribution family-tag map — R-FS-1 arc
`B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION`.

Maps a dispatched LLM provider to its OD `CrossFamilyTag` family tag (C-OD-15
§15.1 / §15.3) so the §15.3 fallback-chain cost composition can **populate**
`SpanCostRecord.provider_discriminator` — which makes
`RollupAxis.PER_PROVIDER_DISCRIMINATOR` (per-family cost visibility under
fallback) **non-vacuous in production** (today every production LLM cost record
writes `provider_discriminator = None`, so that axis is empty even though it is
defined + admissible).

**Mapping (provider-fixed, per the OD spec §15.1 example).** OD spec
`Spec_Operational_Discipline_v1_2.md` §15.3 gives the canonical example chain
"Anthropic → cross-family OpenAI/Gemini → local Ollama" and §15.1 names the
tag vocabulary `{frontier_managed, frontier_managed_alt, local_ollama}`. The
example groups **OpenAI/Gemini together** as the cross-family middle tier, so
the `ProviderFamily(4) → CrossFamilyTag(3)` projection is:

    ANTHROPIC          → FRONTIER_MANAGED
    OPENAI             → FRONTIER_MANAGED_ALT
    GOOGLE             → FRONTIER_MANAGED_ALT   # grouped with OpenAI per §15.1
    LOCAL_OPEN_WEIGHT  → LOCAL_OLLAMA

This is impl-to-cleared-**example** (the §15.1 example's own grouping), not an
invented taxonomy. `CrossFamilyTag` is deliberately NOT extended (no benefit —
the spec groups the two frontier-managed-alt families, and a 4th member would
add an OD surface for no rollup gain).

**Carrier home (this lives in `harness-runtime`).** `CrossFamilyTag` is
OD-homed (`harness_od.cross_family_rollup`) and `ProviderFamily` is CP-homed
(`harness_cp.cross_family_fallback_chain`), so a map between them cannot live in
either axis package without introducing a cross-axis import cycle
(`[[carrier-home-defect-pattern]]`). The runtime composes both axes, so the
map is homed here. This module also owns the canonical provider→`ProviderFamily`
map (one source of truth — `retry_breaker_fallback._provider_family` imports
`provider_family_for_provider` from here rather than duplicating it).

Authority: `Spec_Operational_Discipline_v1_2.md` §15.1 + §15.3 (preserved
verbatim into v1.30 §15.1.2 — `provider_discriminator` per-dispatch-optional,
populated by *this* §15.3 composition arc); `Spec_Harness_Runtime_v1.md` §9
C-RT-09 (v1.58); ADR-F1 v1.2 §Decision (cross-family fallback chain).
"""

from __future__ import annotations

from harness_cp.cross_family_fallback_chain import ProviderFamily
from harness_od.cross_family_rollup import CrossFamilyTag

__all__ = [
    "cross_family_tag_for_provider",
    "provider_family_for_provider",
]


#: Provider key → `ProviderFamily`. The three constructed providers
#: (`providers.py` — anthropic / openai / ollama) plus the google family for
#: the §15.1 "Gemini" cross-family example. `ProviderFamily` is value-equal to
#: the anthropic / openai / google provider keys; `ollama` maps explicitly to
#: `LOCAL_OPEN_WEIGHT`.
_PROVIDER_FAMILY_BY_PROVIDER: dict[str, ProviderFamily] = {
    "anthropic": ProviderFamily.ANTHROPIC,
    "claude_code": ProviderFamily.ANTHROPIC,
    "openai": ProviderFamily.OPENAI,
    "codex": ProviderFamily.OPENAI,
    "google": ProviderFamily.GOOGLE,
    "antigravity": ProviderFamily.GOOGLE,
    "gemini": ProviderFamily.GOOGLE,
    "ollama": ProviderFamily.LOCAL_OPEN_WEIGHT,
}


#: `ProviderFamily` → `CrossFamilyTag` (the §15.1 example grouping; total over
#: the closed 4-value `ProviderFamily` enum).
_CROSS_FAMILY_TAG_BY_FAMILY: dict[ProviderFamily, CrossFamilyTag] = {
    ProviderFamily.ANTHROPIC: CrossFamilyTag.FRONTIER_MANAGED,
    ProviderFamily.OPENAI: CrossFamilyTag.FRONTIER_MANAGED_ALT,
    ProviderFamily.GOOGLE: CrossFamilyTag.FRONTIER_MANAGED_ALT,
    ProviderFamily.LOCAL_OPEN_WEIGHT: CrossFamilyTag.LOCAL_OLLAMA,
}


def provider_family_for_provider(provider: str) -> ProviderFamily:
    """Map a provider key to its `ProviderFamily`.

    Known providers map directly; any other family-named provider resolves via
    `ProviderFamily(provider)`; an unknown provider falls back to
    `LOCAL_OPEN_WEIGHT` (conservative — affects only cross-family attribution,
    never WHICH model is dispatched). Same semantics as the prior
    `retry_breaker_fallback._provider_family` (which now imports this).
    """
    family = _PROVIDER_FAMILY_BY_PROVIDER.get(provider)
    if family is not None:
        return family
    try:
        return ProviderFamily(provider)
    except ValueError:
        return ProviderFamily.LOCAL_OPEN_WEIGHT


def cross_family_tag_for_provider(provider: str) -> str:
    """Map a dispatched provider to its `CrossFamilyTag` family-tag string.

    Returns the `CrossFamilyTag.value` (e.g. `"frontier_managed_alt"`) for the
    provider's family, suitable for `SpanCostRecord.provider_discriminator`
    (which is `str`-typed to avoid the U-OD-20→U-OD-21 carrier cycle but
    validated against `CrossFamilyTag` at the `PER_PROVIDER_DISCRIMINATOR`
    rollup). Total over the closed `ProviderFamily` enum — never `None` for an
    LLM dispatch (an LLM call always has a provider, hence a family).
    """
    return _CROSS_FAMILY_TAG_BY_FAMILY[provider_family_for_provider(provider)].value
