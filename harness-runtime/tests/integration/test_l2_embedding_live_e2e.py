"""R-FS-1 L2 — the EMBEDDING vendor gate, free-local fastembed live e2e.

The CP-axis unit tests prove the k-NN classifier with a DETERMINISTIC stub embed.
THIS test removes the stub: a **real** light in-process ``fastembed`` embedding
(Option B — torch-free, no API, no secret) backs the classifier against the
default per-workload-class corpus, proving the REAL projection produces
meaningful semantic routing (the stub-tested logic + real 384-dim vectors). It is
free + local — no paid call, zero secret (`[[feedback-run-credential-gated-
live-e2e-authorized]]`).

Marked ``e2e`` (the marker keeps it out of the ``-m "not e2e"`` provider-free
lanes per `[[feedback-run-credential-gated-live-e2e-authorized]]`) AND it
self-skips when the optional ``[embedding]`` extra is not installed or the model
cannot be fetched (first-use download needs network) — so a machine with the
extra exercises it, CI without it skips cleanly.

Authority: CP spec v1.36 §2.1/§2.2/§2.4 (the L2 EMBEDDING surface + the §2.4
vendor deferral); operator decision 2026-06-16 (Option B);
``harness_runtime.lifecycle.embedding_resolution`` (the light realization).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from harness_cp.cp_shared_types import ProviderAgnosticPayload
from harness_cp.embedding_routing import make_embedding_classifier
from harness_cp.layered_routing_strategy import LayerDecisionFn
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_runtime.lifecycle.embedding_resolution import (
    default_routing_corpus,
    make_fastembed_embedding,
)

pytestmark = pytest.mark.e2e

_CODE = "anthropic:claude-opus-4-8"
_CREATIVE = "anthropic:claude-haiku-4-5"
_PIPELINE = "ollama:llama3.2:3b"
_RESEARCH = "openai:gpt-5.5"


def _real_classifier() -> LayerDecisionFn:
    """Build the classifier over a REAL fastembed embedding, or skip when the
    optional extra / model is unavailable (no `[embedding]` install, no network
    for the first-use model download)."""
    try:
        embed = make_fastembed_embedding()
    except ImportError as exc:  # the [embedding] extra is not installed
        pytest.skip(f"fastembed not installed (optional [embedding] extra): {exc}")
    except Exception as exc:  # model fetch failure (offline) → skip, not fail
        pytest.skip(f"fastembed model unavailable (offline?): {exc}")
    return make_embedding_classifier(embed=embed, corpus=default_routing_corpus(), k=3)


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


def test_l2_real_embedding_discriminates_across_workload_classes() -> None:
    """A real fastembed projection routes each workload-flavoured call site to
    its labelled candidate — and the picks are DISTINCT (genuine semantic
    discrimination, not a degenerate single-candidate collapse). Queries are
    NOT verbatim corpus exemplars (paraphrases) so this exercises real
    similarity, not string identity."""
    clf = _real_classifier()
    manifest = _empty_manifest()

    code = clf(_payload("help me fix a bug in my python script"), manifest)
    creative = clf(_payload("write me a whimsical short tale"), manifest)
    pipeline = clf(_payload("transform this spreadsheet into rows of records"), manifest)
    research = clf(_payload("review and summarize this academic study"), manifest)

    assert code == _CODE
    assert creative == _CREATIVE
    assert pipeline == _PIPELINE
    assert research == _RESEARCH
    # The discrimination is real: at least three distinct candidates were chosen.
    assert len({code, creative, pipeline, research}) >= 3


def test_l2_real_embedding_is_deterministic() -> None:
    """The same query embeds + routes identically across calls (the §2.2
    deterministic-modulo-corpus property holds for the real model)."""
    clf = _real_classifier()
    manifest = _empty_manifest()
    query = _payload("debug a failing python test")
    first = clf(query, manifest)
    assert first == _CODE
    assert all(clf(query, manifest) == first for _ in range(3))


def test_l2_factory_builds_real_classifier_when_routing_activation_on() -> None:
    """B-L2-EMBEDDING-ACTIVATION — the PRODUCTION factory path the non-e2e unit
    witnesses don't cover: `materialize_llm_dispatcher_stage(routing_activation=True)`
    with NO injected classifier builds the REAL fastembed classifier (the 3-call
    `make_embedding_classifier(embed=make_fastembed_embedding(), corpus=
    default_routing_corpus())` composition) — proving it composes end-to-end (not
    just by-types, which pyright already shows) and the built `LayerDecisionFn`
    routes a real call site. The unit tests inject a stub; this is the fastembed
    factory build."""
    try:
        make_fastembed_embedding()  # skip cleanly if the extra / model is unavailable
    except ImportError as exc:
        pytest.skip(f"fastembed not installed (optional [embedding] extra): {exc}")
    except Exception as exc:  # model fetch failure (offline) → skip, not fail
        pytest.skip(f"fastembed model unavailable (offline?): {exc}")

    from harness_runtime.lifecycle.llm_dispatch import materialize_llm_dispatcher_stage

    # The factory uses providers/tracer only at dispatch, not construction — a
    # non-empty placeholder map passes the `len(providers) == 0` guard.
    dispatcher = materialize_llm_dispatcher_stage(
        cast("Any", {"anthropic": object()}),
        cast("Any", object()),
        routing_activation=True,  # no embedding_classifier → factory builds the real one
    )
    assert dispatcher.routing_activation is True
    assert dispatcher.embedding_classifier is not None
    # The factory-built (real, not stub) classifier routes a real call site.
    candidate = dispatcher.embedding_classifier(
        _payload("help me fix a bug in my python script"), _empty_manifest()
    )
    assert candidate == _CODE
