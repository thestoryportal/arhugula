"""Tests for U-CP-02 — `ProviderCapabilities` reflection contract (C-CP-01 §1.2).

Acceptance-criterion coverage:
  #1 ten fields per §1.2          -> test_provider_capabilities_ten_fields
  #2 reflect deterministic        -> test_reflect_deterministic
  #3 cost fields float            -> test_cost_fields_are_float
  #4 supports_thinking Anthropic  -> test_supports_thinking_anthropic_only,
                                     test_supports_thinking_anthropic_model_catalog

The U-CP-02 unit declares `Depends on: (none)` — no cross-axis edge. The plan's
acceptance #4 "integration test against U-AS-29 catalog" is a future cross-axis
integration concern; the unit-grade test below pins against the C-AS-13 §13.4
model-tier identifiers directly (the same string vocabulary U-AS-29 carries).
"""

from __future__ import annotations

from harness_cp.provider_capabilities import (
    ProviderCapabilities,
    ProviderCapability,
    provider_supports,
    reflect_provider_capabilities,
)

_SPEC_FIELDS = {
    "provider",
    "model_family",
    "model_version",
    "max_context_tokens",
    "supports_tools",
    "supports_caching",
    "supports_thinking",
    "supports_batch",
    "cost_per_input_token",
    "cost_per_output_token",
}


def test_provider_capabilities_ten_fields() -> None:
    """Acceptance #1 — exactly ten fields per C-CP-01 §1.2 verbatim."""
    assert set(ProviderCapabilities.model_fields) == _SPEC_FIELDS
    assert len(ProviderCapabilities.model_fields) == 10


def test_reflect_deterministic() -> None:
    """Acceptance #2 — same (provider, model) pair yields the same record."""
    a = reflect_provider_capabilities("anthropic", "opus-4-7")
    b = reflect_provider_capabilities("anthropic", "opus-4-7")
    assert a == b


def test_cost_fields_are_float() -> None:
    """Acceptance #3 — cost fields are float-typed."""
    caps = reflect_provider_capabilities("anthropic", "opus-4-7")
    assert isinstance(caps.cost_per_input_token, float)
    assert isinstance(caps.cost_per_output_token, float)


def test_supports_thinking_anthropic_only() -> None:
    """Acceptance #4 — supports_thinking true only for Anthropic thinking tier."""
    assert reflect_provider_capabilities("anthropic", "opus-4-7").supports_thinking
    assert reflect_provider_capabilities("anthropic", "sonnet-4-6").supports_thinking
    assert reflect_provider_capabilities("anthropic", "opus-4-6").supports_thinking
    # Non-thinking Anthropic model + non-Anthropic providers are false.
    assert not reflect_provider_capabilities("anthropic", "haiku-4-5").supports_thinking
    assert not reflect_provider_capabilities("openai", "gpt-x").supports_thinking
    assert not reflect_provider_capabilities("ollama", "llama-x").supports_thinking


def test_supports_thinking_anthropic_model_catalog() -> None:
    """Acceptance #4 — thinking tier per C-AS-13 §13.4 model-tier identifiers.

    Sonnet 4.6 / Opus 4.6 / Opus 4.7 are extended-thinking; Haiku 4.5 is not.
    """
    catalog = {
        "sonnet-4-6": True,
        "opus-4-6": True,
        "opus-4-7": True,
        "haiku-4-5": False,
    }
    for model, expected in catalog.items():
        caps = reflect_provider_capabilities("anthropic", model)
        assert caps.supports_thinking == expected


def test_supports_thinking_real_runtime_model_ids() -> None:
    """Acceptance #4 — the reflection identifies extended-thinking tiers from the
    *real* runtime ``ModelBinding.model`` shape, not just the short §13.4 tokens.

    Runtime bindings carry the full Anthropic API IDs (``claude-`` prefix +
    optional ``-YYYYMMDD`` snapshot suffix) — e.g. ``claude-opus-4-7`` and
    ``claude-haiku-4-5`` appear across the runtime tests/e2e. Regression guard
    for the capability-shortfall consumer (R-CL-P1): a bare-token equality check
    would mis-classify these real thinking models as non-thinking and skip them.
    """
    # Prefixed real IDs — thinking tiers.
    assert reflect_provider_capabilities("anthropic", "claude-opus-4-7").supports_thinking
    assert reflect_provider_capabilities("anthropic", "claude-sonnet-4-6").supports_thinking
    assert reflect_provider_capabilities("anthropic", "claude-opus-4-6").supports_thinking
    # Snapshot-suffixed real ID — still a thinking tier.
    assert reflect_provider_capabilities("anthropic", "claude-opus-4-7-20250101").supports_thinking
    # Prefixed real non-thinking ID — false.
    assert not reflect_provider_capabilities("anthropic", "claude-haiku-4-5").supports_thinking


def test_provider_capability_cardinality_four() -> None:
    """`ProviderCapability` declares exactly four classes per §1.2."""
    assert len(ProviderCapability) == 4
    assert {c.value for c in ProviderCapability} == {
        "tools",
        "caching",
        "thinking",
        "batch",
    }


def test_provider_supports_projects_each_capability() -> None:
    """`provider_supports` projects each `ProviderCapability` onto its field."""
    caps = reflect_provider_capabilities("anthropic", "opus-4-7")
    assert provider_supports(ProviderCapability.TOOLS, caps) == caps.supports_tools
    assert provider_supports(ProviderCapability.CACHING, caps) == caps.supports_caching
    assert provider_supports(ProviderCapability.THINKING, caps) == caps.supports_thinking
    assert provider_supports(ProviderCapability.BATCH, caps) == caps.supports_batch
