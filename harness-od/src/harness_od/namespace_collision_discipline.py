"""Namespace collision precedence + cross-namespace cardinality discipline — U-OD-10.

Implements C-OD-08 §8.1 (collision precedence rule), §8.2 (canonical example),
§8.3 (cross-namespace cardinality discipline).

`NamespacePrecedenceRule` enumerates the 2 precedence rules per §8.1.
`NAMESPACE_COLLISIONS` declares the canonical `harness.breaker.*` precedence
example per the plan acc #2 (F-CP-01 Stage 3b alignment).
`CacheTierSubsetInvariant` records the §8.3 cache-tier cardinality invariant.
`enforce_otel_canonical_value` rejects span attribute sets that violate the
cache-tier subset invariant `cache_creation + cache_read + uncached ==
input_tokens` (acc #4-#6).

Plan-vs-spec note. Spec §8.2 names the OTel `gen_ai.usage.input_tokens`
vs `anthropic.cache_*` overlap as its canonical example. Plan acc #2 (U-OD-10)
fixes a different canonical example — the CP-side `breaker.` namespace
superseded by the OD-anchored `harness.breaker.` namespace per F-CP-01
Stage 3b. Both are instances of the §8.1 precedence rule; the plan is
execution authority on the signature + acc, so `NAMESPACE_COLLISIONS` carries
the plan's `harness.breaker.` example verbatim. The §8.2 token-attribution
overlap is the cardinality surface enforced at `enforce_otel_canonical_value`.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.3.2 U-OD-10
(v2.6 M-1 type re-point — `SpanAttributes` re-pointed to the U-OD-04 carrier;
all other surfaces preserved verbatim from v2.1 §3.3.2);
Spec_Operational_Discipline_v1_2.md §8 C-OD-08 §8.1 / §8.2 / §8.3 (preserved
verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.2 (namespace collision
discipline).

Depends on: [U-OD-05, U-OD-08, U-OD-09, U-OD-04] — `SpanAttributes` resolves
to the U-OD-04 (`otel_genai_base`) OTel-handle alias family (v2.6 `[U-OD-04]`
edge).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.otel_genai_base import SpanAttributes

__all__ = [
    "NAMESPACE_COLLISIONS",
    "CacheTierSubsetInvariant",
    "CanonicalValueViolation",
    "NamespaceCollisionResolution",
    "NamespacePrecedenceRule",
    "enforce_otel_canonical_value",
]

#: OTel GenAI semconv 1.41.0 — total input-token attribute (§8.2, cross-vendor
#: canonical carrier).
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
#: anthropic specialization — 1.25x cache-creation token breakdown (§8.2).
ATTR_CACHE_CREATION = "anthropic.cache_creation_input_tokens"
#: anthropic specialization — 0.10x cache-read token breakdown (§8.2).
ATTR_CACHE_READ = "anthropic.cache_read_input_tokens"


class NamespacePrecedenceRule(StrEnum):
    """The 2 namespace-collision precedence rules (C-OD-08 §8.1).

    Exactly 2 values per §8.1 (acc #1). `SUBSTRATE_ANCHORED_TAKES_PRECEDENCE`
    is the §8.1 primary rule; `AUTHORITATIVE_DECLARER_RESOLVES_COLLISION` is
    the §8.2 secondary rule.
    """

    SUBSTRATE_ANCHORED_TAKES_PRECEDENCE = "SUBSTRATE_ANCHORED_TAKES_PRECEDENCE"
    """§8.1 verbatim — a substrate-anchored namespace takes precedence over a
    CP-side namespace it replaces."""

    AUTHORITATIVE_DECLARER_RESOLVES_COLLISION = "AUTHORITATIVE_DECLARER_RESOLVES_COLLISION"
    """§8.2 secondary rule — the authoritative declarer of a namespace
    resolves the collision at all subsequent ingestion sites."""


class NamespaceCollisionResolution(BaseModel):
    """A resolved namespace collision (C-OD-08 §8.1 / §8.2).

    Frozen → `Eq` + `Hash`, stable under serialization. Records the colliding
    prefix, the authoritative prefix that wins, the precedence rule, and the
    rationale anchoring the resolution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the superseded namespace prefix (e.g. CP-side "breaker.").
    colliding_prefix: str
    #: the winning namespace prefix (e.g. OD-side "harness.breaker.").
    authoritative_prefix: str
    #: the precedence rule resolving the collision.
    precedence_rule: NamespacePrecedenceRule
    #: the anchoring rationale (e.g. "F-CP-01 Stage 3b alignment").
    rationale_ref: str


#: The namespace-collision resolutions (C-OD-08 §8.2 canonical example, plan
#: acc #2 verbatim). The CP-side `breaker.` namespace is superseded by the
#: OD-anchored `harness.breaker.` namespace per F-CP-01 Stage 3b.
NAMESPACE_COLLISIONS: tuple[NamespaceCollisionResolution, ...] = (
    NamespaceCollisionResolution(
        colliding_prefix="breaker.",
        authoritative_prefix="harness.breaker.",
        precedence_rule=NamespacePrecedenceRule.SUBSTRATE_ANCHORED_TAKES_PRECEDENCE,
        rationale_ref="F-CP-01 Stage 3b alignment",
    ),
)


class CacheTierSubsetInvariant(BaseModel):
    """The §8.3 cross-namespace cardinality invariant for cache-tier tokens.

    Frozen → `Eq`. Records the invariant form and the enforcement site. The
    cache-tier breakdown (`cache_creation + cache_read + uncached`) sums to the
    OTel-canonical `input_tokens` total (§8.2 / §8.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the invariant form (verbatim §8.3).
    invariant_form: str = "cache_creation + cache_read + uncached == input_tokens"
    #: the enforcement site.
    enforced_at: str = "U-OD-18 cost formula composition + OTel canonical value verification"


#: The single §8.3 cache-tier subset invariant instance.
CACHE_TIER_SUBSET_INVARIANT: CacheTierSubsetInvariant = CacheTierSubsetInvariant()


class CanonicalValueViolation(Exception):  # noqa: N818 — U-OD-10 plan signature verbatim (no spec extension)
    """The `Err` arm of `enforce_otel_canonical_value` (C-OD-08 §8.3).

    Raised when a span attribute set violates the cache-tier subset invariant
    `cache_creation + cache_read + uncached == input_tokens` — i.e. the
    cache-tier breakdown exceeds the OTel-canonical `input_tokens` total.
    """


def enforce_otel_canonical_value(span_attrs: SpanAttributes) -> None:
    """Reject a span attribute set that violates the cache-tier subset invariant.

    Returns `None` (the `Ok(())` arm) when the cache-tier breakdown is
    consistent with the OTel-canonical `input_tokens` total — concretely when
    `cache_creation + cache_read <= input_tokens` (the uncached remainder is
    non-negative, so `cache_creation + cache_read + uncached == input_tokens`
    holds with `uncached = input_tokens - cache_read - cache_creation`).

    Raises `CanonicalValueViolation` (the `Err` arm) when the cache-tier
    breakdown exceeds the OTel total — the invariant is violated (acc #4-#6).

    A span attribute set that carries none of the three token attributes is
    accepted (the invariant is vacuous — not every span is an LLM-call span).
    A partial token attribute set (some but not all three present) is rejected
    as non-conformant per §8.3 (acc #6).
    """
    if span_attrs is None:
        return None
    present = [k in span_attrs for k in (ATTR_INPUT_TOKENS, ATTR_CACHE_CREATION, ATTR_CACHE_READ)]
    if not any(present):
        # No token attribution on this span — invariant vacuous.
        return None
    if not all(present):
        raise CanonicalValueViolation(
            "incomplete cache-tier attribute set: a span carrying any of "
            f"{ATTR_INPUT_TOKENS!r} / {ATTR_CACHE_CREATION!r} / "
            f"{ATTR_CACHE_READ!r} MUST carry all three (C-OD-08 §8.3)"
        )
    input_tokens = _as_int(span_attrs[ATTR_INPUT_TOKENS])
    cache_creation = _as_int(span_attrs[ATTR_CACHE_CREATION])
    cache_read = _as_int(span_attrs[ATTR_CACHE_READ])
    uncached = input_tokens - cache_read - cache_creation
    if uncached < 0:
        raise CanonicalValueViolation(
            "cache-tier subset invariant violated: "
            f"cache_creation ({cache_creation}) + cache_read ({cache_read}) "
            f"exceeds input_tokens ({input_tokens}); the breakdown cannot "
            "sum to the OTel-canonical total (C-OD-08 §8.3)"
        )
    return None


def _as_int(value: object) -> int:
    """Coerce an OTel attribute value to `int`, rejecting non-integral values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalValueViolation(
            f"token attribute must be an integer; got {value!r} (C-OD-08 §8.3)"
        )
    return value
