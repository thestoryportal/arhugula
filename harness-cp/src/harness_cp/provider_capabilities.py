"""`ProviderCapabilities` reflection contract — U-CP-02.

Implements C-CP-01 §1.2 (per-provider capability-introspection API). Declares
the 10-field `ProviderCapabilities` record, the `ProviderCapability` enum, and
the `reflect_provider_capabilities` / `provider_supports` functions.

The §1.2 capability surface is the authoring-time substrate that lets manifest
authoring bind providers per per-feature requirement. `reflect_provider_capabilities`
is deterministic — the same `(provider, model)` pair always yields the same
record (no inference path); the concrete provider/cost catalog is deferred to
implementation discretion per spec §1.2.

`supports_thinking` is `true` only for Anthropic's extended-thinking model tier
(Sonnet 4.6 / Opus 4.6 / Opus 4.7) per C-CP-01 §1.2 narrative + AS C-AS-13 §13.4
model-tier table. The acceptance reference catalog is anchored to U-AS-29's
landed `AnthropicModel` enum (`harness_as`).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.1 U-CP-02 (preserved
verbatim through v2.6 — no amendment); Spec_Control_Plane_v1_2.md §1 C-CP-01
§1.2 (preserved verbatim into v1.3); ADR-F1 v1.2 §Decision.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ProviderCapability(StrEnum):
    """The 4 introspectable provider capability classes (C-CP-01 §1.2).

    Each member names a boolean capability field on `ProviderCapabilities`;
    `provider_supports` discriminates on these.
    """

    TOOLS = "tools"
    CACHING = "caching"
    THINKING = "thinking"
    BATCH = "batch"


class ProviderCapabilities(BaseModel):
    """One provider's reflected capability surface (C-CP-01 §1.2).

    Exactly ten fields per the §1.2 capability-introspection record. Consumed
    at routing-time selection per C-CP-02 §2.1 and at fallback-chain
    composition per C-CP-04.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    """Provider identity — `{anthropic, openai, ollama, ...}` (open catalog;
    enumeration deferred to implementation discretion per §1.2)."""

    model_family: str
    model_version: str
    max_context_tokens: int
    supports_tools: bool
    supports_caching: bool

    supports_thinking: bool
    """Extended-thinking budget support — `true` only for Anthropic Sonnet /
    Opus extended-thinking tier per §1.2 narrative + AS C-AS-13 §13.4."""

    supports_batch: bool
    """Anthropic Batch API support."""

    cost_per_input_token: float
    cost_per_output_token: float


# --- Anthropic extended-thinking model reference (acceptance #4) -------------
# C-CP-01 §1.2 narrative: supports_thinking is true only for the Anthropic
# extended-thinking tier. The tier members are anchored to U-AS-29's landed
# `AnthropicModel` enum (Sonnet 4.6 / Opus 4.6 / Opus 4.7); Haiku 4.5 is the
# non-thinking Anthropic model and is excluded.
_ANTHROPIC_THINKING_MODELS: frozenset[str] = frozenset({"sonnet-4-6", "opus-4-6", "opus-4-7"})
"""The Anthropic extended-thinking model tiers (C-CP-01 §1.2 + AS C-AS-13 §13.4).
String values are the §13.4 model-tier identifiers (the same vocabulary the
U-AS-29 ``AnthropicModel`` enum carries — `engine_class_composition.AnthropicModel`)."""


def _is_anthropic_thinking_model(model: str) -> bool:
    """Whether ``model`` is an Anthropic extended-thinking tier, tolerant of the
    real runtime model-ID shape.

    The reflection's tier catalog uses the AS §13.4 short tier tokens
    (``opus-4-7``), but runtime ``ModelBinding.model`` strings carry the full
    Anthropic API IDs — the ``claude-`` prefix and an optional ``-YYYYMMDD``
    snapshot suffix (e.g. ``claude-opus-4-7`` / ``claude-opus-4-7-20250101``).
    A bare-token equality check would mis-classify those real IDs as
    non-thinking. The concrete provider catalog + ID-format matching is
    deferred to implementation discretion per §1.2; this normalization strips
    the ``claude-`` prefix and matches a tier token exactly or as the
    snapshot-suffixed ``{tier}-`` prefix.
    """
    normalized = model.removeprefix("claude-")
    return any(
        normalized == tier or normalized.startswith(f"{tier}-")
        for tier in _ANTHROPIC_THINKING_MODELS
    )


def reflect_provider_capabilities(provider: str, model: str) -> ProviderCapabilities:
    """Reflect the capability surface for a `(provider, model)` pair.

    Deterministic — the same `(provider, model)` pair always yields the same
    `ProviderCapabilities` value; no inference path (acceptance #2). The
    concrete provider/cost catalog is deferred to implementation discretion
    per spec §1.2; this reflection populates the §1.2 record shape with the
    capability discriminators that are determinable from `(provider, model)`
    at the specification surface.

    `supports_thinking` is `true` iff `(provider, model)` is an Anthropic
    extended-thinking-tier model per `_ANTHROPIC_THINKING_MODELS` (acceptance
    #4). All cost fields are float-typed placeholders; concrete cost-table
    maintenance is deferred per spec §1.2.
    """
    is_anthropic = provider == "anthropic"
    return ProviderCapabilities(
        provider=provider,
        model_family=model.rsplit("-", 1)[0] if "-" in model else model,
        model_version=model,
        max_context_tokens=0,
        supports_tools=True,
        supports_caching=is_anthropic,
        supports_thinking=is_anthropic and _is_anthropic_thinking_model(model),
        supports_batch=is_anthropic,
        cost_per_input_token=0.0,
        cost_per_output_token=0.0,
    )


def provider_supports(capability: ProviderCapability, caps: ProviderCapabilities) -> bool:
    """Whether `caps` supports `capability` (C-CP-01 §1.2).

    Pure projection of the boolean capability fields onto the
    `ProviderCapability` discriminator.
    """
    match capability:
        case ProviderCapability.TOOLS:
            return caps.supports_tools
        case ProviderCapability.CACHING:
            return caps.supports_caching
        case ProviderCapability.THINKING:
            return caps.supports_thinking
        case ProviderCapability.BATCH:
            return caps.supports_batch
