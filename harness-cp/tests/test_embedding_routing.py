"""Tests for the Layer-2 EMBEDDING embedding-classifier (C-CP-02 §2.1/§2.2/§2.4).

The classifier is exercised with a DETERMINISTIC keyword-bag stub ``EmbeddingFn``
(no embedding library — the model-agnostic seam means the routing logic is
testable with any vector source). The real light-form (fastembed) realization is
exercised by the runtime-axis gated live e2e.
"""

from __future__ import annotations

import pytest
from harness_cp.cp_shared_types import ProviderAgnosticPayload
from harness_cp.embedding_routing import (
    EmbeddingExemplar,
    EmbeddingRoutingCorpus,
    embedding_query_text,
    make_embedding_classifier,
)
from harness_cp.layered_routing_strategy import route
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import RoutingManifest
from pydantic import ValidationError

# A deterministic keyword-bag embedding: one orthogonal dimension per vocab word,
# 1.0 if the word is present in the (lowercased) text else 0.0. Cosine similarity
# then reduces to normalized keyword overlap — fully controllable for the k-NN.
_VOCAB = (
    "code",
    "python",
    "function",
    "bug",
    "story",
    "poem",
    "creative",
    "data",
    "pipeline",
    "etl",
    "research",
    "paper",
    "study",
)

_SE = "anthropic:claude-opus-4-8"
_CONTENT = "anthropic:claude-haiku-4-5"
_PIPELINE = "ollama:llama3.2:3b"
_RESEARCH = "openai:gpt-5.5"


def _stub_embed(text: str) -> tuple[float, ...]:
    low = text.lower()
    return tuple(1.0 if word in low else 0.0 for word in _VOCAB)


def _corpus() -> EmbeddingRoutingCorpus:
    return EmbeddingRoutingCorpus.from_pairs(
        [
            ("write and debug python code function", _SE, "software-engineering"),
            ("refactor a buggy python function", _SE, "software-engineering"),
            ("compose a creative story poem", _CONTENT, "content-creation"),
            ("build an etl data pipeline", _PIPELINE, "pipeline-automation"),
            ("research study paper analysis", _RESEARCH, "research"),
        ]
    )


def _payload(text: str) -> ProviderAgnosticPayload:
    return ProviderAgnosticPayload(
        messages=({"role": "user", "content": text},), tools=None, params={}
    )


def _empty_manifest() -> RoutingManifest:
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )


def test_classifier_routes_to_nearest_candidate() -> None:
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=1)
    # A code-flavoured call site → the software-engineering candidate.
    assert clf(_payload("fix this python function bug"), _empty_manifest()) == _SE
    # A creative call site → the content candidate.
    assert clf(_payload("write a creative poem"), _empty_manifest()) == _CONTENT
    # A data-pipeline call site → the pipeline candidate.
    assert clf(_payload("set up an etl data pipeline"), _empty_manifest()) == _PIPELINE


def test_majority_vote_among_k_nearest() -> None:
    # k=3: a python-code query is nearest to BOTH software-engineering exemplars
    # (2 votes) plus one other — majority vote returns the SE candidate, proving
    # the decision is a k-NN vote, not a single nearest neighbour.
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=3)
    assert clf(_payload("debug python code function"), _empty_manifest()) == _SE


def test_below_threshold_falls_through() -> None:
    # No vocab keywords → zero query vector → cosine 0 with every exemplar →
    # below min_similarity → None (fall through to LLM_AS_ROUTER per §2.2).
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=3)
    assert clf(_payload("hello there my friend"), _empty_manifest()) is None


def test_threshold_is_honoured() -> None:
    # A high threshold rejects an imperfect (partial-overlap) match.
    strict = make_embedding_classifier(
        embed=_stub_embed, corpus=_corpus(), k=1, min_similarity=0.99
    )
    # "python" alone partially overlaps the multi-keyword SE exemplars (<1.0) →
    # rejected at threshold 0.99.
    assert strict(_payload("python"), _empty_manifest()) is None
    # The same query clears a permissive threshold.
    loose = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=1, min_similarity=0.1)
    assert loose(_payload("python"), _empty_manifest()) == _SE


def test_empty_text_falls_through() -> None:
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus())
    # No messages at all.
    empty = ProviderAgnosticPayload(messages=(), tools=None, params={})
    assert clf(empty, _empty_manifest()) is None
    # A message with no textual content.
    no_text = ProviderAgnosticPayload(
        messages=({"role": "user", "content": {"image": "x"}},), tools=None, params={}
    )
    assert clf(no_text, _empty_manifest()) is None


def test_classifier_is_deterministic() -> None:
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=3)
    p = _payload("debug a python function")
    first = clf(p, _empty_manifest())
    assert first is not None
    assert all(clf(p, _empty_manifest()) == first for _ in range(5))


def test_classifier_binds_as_layer_decision_in_route() -> None:
    # The load-bearing CP-side reachability proof: the classifier IS a valid
    # `LayerDecisionFn` that `route()` consumes at the EMBEDDING layer. Omit the
    # DECLARATIVE binding so EMBEDDING is reached (mirrors how production reaches
    # it only once R-300 makes DECLARATIVE conditional).
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=1)
    trace = route(
        _payload("fix this python function bug"),
        _empty_manifest(),
        {RoutingLayer.EMBEDDING: clf},
    )
    assert trace.layer == RoutingLayer.EMBEDDING.value  # "embedding"
    assert trace.candidate == _SE


def test_route_falls_through_to_sentinel_when_embedding_declines() -> None:
    # When EMBEDDING is the only bound layer and it declines (no match), `route()`
    # returns the LLM_AS_ROUTER empty-candidate sentinel — the §2.2 fall-through.
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=3)
    trace = route(
        _payload("hello there my friend"),
        _empty_manifest(),
        {RoutingLayer.EMBEDDING: clf},
    )
    assert trace.layer == RoutingLayer.LLM_AS_ROUTER.value
    assert trace.candidate == ""


def test_corpus_rejects_malformed_candidate() -> None:
    with pytest.raises(ValidationError):
        EmbeddingExemplar(text="x", candidate="no-colon-here", workload_class="research")


def test_corpus_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        EmbeddingExemplar(text="   ", candidate="anthropic:claude-opus-4-8", workload_class="x")


def test_corpus_rejects_empty_exemplar_set() -> None:
    with pytest.raises(ValidationError):
        EmbeddingRoutingCorpus(exemplars=())


def test_k_below_one_raises() -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=0)


def test_k_larger_than_corpus_is_clamped() -> None:
    # k greater than the corpus size must not error — it clamps to the corpus.
    clf = make_embedding_classifier(embed=_stub_embed, corpus=_corpus(), k=100)
    assert clf(_payload("debug python code function"), _empty_manifest()) == _SE


def test_embedding_query_text_concatenates_and_handles_multipart() -> None:
    p = ProviderAgnosticPayload(
        messages=(
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": ["first part", {"type": "text", "text": "second part"}]},
        ),
        tools=None,
        params={},
    )
    assert embedding_query_text(p) == "be concise\nfirst part\nsecond part"
