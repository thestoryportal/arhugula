"""Layer-2 EMBEDDING routing — the embedding-classifier ``LayerDecisionFn``.

Realizes C-CP-02 §2.1 Layer 2 ("embedding-classifier dispatch") + the §2.4
impl-discretion deferral ("specific embedding model and dimensionality; specific
classifier training-corpus construction") for the **in-process / sync** embedding
path (R-FS-1 arc R / L2 — operator-decided Option B, 2026-06-16: a light
in-process embedding library, NOT a remote API).

Per §2.2 the embedding layer is *deterministic-modulo-corpus*: project the
call-site context into embedding space, run a **k-nearest** classifier against a
trained corpus, and return the nearest labelled ``"provider:model"`` candidate —
or ``None`` to fall through to the next layer (the §2.2 cheapest-first invariant:
a layer that cannot resolve yields to the next). Because the projection here is
an **in-process sync** model and the k-NN search is local, the whole layer is a
sync ``LayerDecisionFn`` bound at the ``EMBEDDING`` layer **inside** ``route()``
(C-CP-02 §2.1's in-``route()`` drawing; the L2-sync / no-fork path of R-DESIGN
§3 D2) — no widening of the sync ``route()`` contract, no spec amendment.

**Model-agnostic by construction.** The classifier takes an injected
``EmbeddingFn`` (text → vector); it does NOT know whether vectors come from ONNX,
a different local library, or a hosted API. This package stays CP-pure /
ADR-F1-clean — it imports no embedding library; the concrete light-form
realization (e.g. ``fastembed``) is a runtime-axis impl-discretion binding
(``harness_runtime.lifecycle.embedding_resolution``), mirroring how the Layer-3
``make_*_router`` realizations live in the runtime while CP owns only the type.

Authority: ``Spec_Control_Plane_v1_36.md`` §2 C-CP-02 §2.1 + §2.2 + §2.4;
``Architectural_Design_Document_v1_3.md`` §5.3.3 (the embedding layer is
deterministic-modulo-corpus → stays in the deterministic ``route()``);
``.harness/r-fs-1-r-routing-intelligence-design-v1.md`` §3 (D2, L2-sync); ADR-F1
v1.2.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from harness_cp.cp_shared_types import ProviderAgnosticPayload
from harness_cp.layered_routing_strategy import LayerDecisionFn
from harness_cp.routing_manifest_residence import RoutingManifest

# The injected embedding projection: a call-site text string in, a dense vector
# out. SYNC (the L2-sync determinant — an in-process model), so the classifier
# composes inside the sync `route()`. The concrete model is the §2.4 vendor /
# impl-discretion deferral; this type is the model-agnostic seam.
EmbeddingFn = Callable[[str], Sequence[float]]


class EmbeddingExemplar(BaseModel):
    """A single labelled corpus point — a ``(text, candidate)`` pair.

    ``text`` is a representative call-site utterance; ``candidate`` is the
    ``"provider:model"`` binding the classifier should select for call sites
    semantically near it. ``workload_class`` tags the exemplar's workload family
    (the "per-workload-class corpus" of §2.1) for authoring traceability — it is
    not consumed by the k-NN (the projection is over ``text``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    candidate: str
    """The labelled ``"provider:model"`` binding (well-formed, validated)."""
    workload_class: str
    """The workload family this exemplar represents (authoring traceability)."""

    @field_validator("text")
    @classmethod
    def _text_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("exemplar text must be non-empty")
        return v

    @field_validator("candidate")
    @classmethod
    def _candidate_well_formed(cls, v: str) -> str:
        # A corpus candidate MUST be a well-formed "provider:model" — else the
        # classifier could return a candidate `infer()` rejects with
        # RoutingCandidateUnresolvedError (fail at authoring, not at dispatch).
        provider, sep, model = v.partition(":")
        if not sep or not provider or not model:
            raise ValueError(f"candidate must be a well-formed 'provider:model' string, got {v!r}")
        return v


class EmbeddingRoutingCorpus(BaseModel):
    """The trained per-workload-class corpus — the labelled exemplar set the
    embedding classifier matches against (C-CP-02 §2.1 "trained corpus").

    Frozen + content-validated: non-empty, all exemplars well-formed. The
    corpus *version* is the §2.2 "classifier corpus version" the layer's
    determinism is modulo of (a different corpus is a different classifier)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exemplars: tuple[EmbeddingExemplar, ...]

    @model_validator(mode="after")
    def _non_empty(self) -> EmbeddingRoutingCorpus:
        if not self.exemplars:
            raise ValueError("an embedding routing corpus must have at least one exemplar")
        return self

    @classmethod
    def from_pairs(cls, pairs: Sequence[tuple[str, str, str]]) -> EmbeddingRoutingCorpus:
        """Build from ``(text, candidate, workload_class)`` triples."""
        return cls(
            exemplars=tuple(
                EmbeddingExemplar(text=t, candidate=c, workload_class=w) for t, c, w in pairs
            )
        )


def embedding_query_text(payload: ProviderAgnosticPayload) -> str:
    """Project a routing call's payload into the text to embed — the call-site
    context (§2.1). Concatenates the string ``content`` of each provider-neutral
    message; list-valued content (multi-part) contributes its string parts.
    Deterministic given inputs; empty when no textual content is present (→ the
    classifier falls through, since there is nothing to classify)."""
    parts: list[str] = []
    for message in payload.messages:
        content: Any = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, (list, tuple)):
            for piece in cast("Sequence[Any]", content):
                if isinstance(piece, str):
                    parts.append(piece)
                elif isinstance(piece, Mapping):
                    text_piece: Any = cast("Mapping[str, Any]", piece).get("text")
                    if isinstance(text_piece, str):
                        parts.append(text_piece)
    return "\n".join(parts).strip()


def _normalize(vector: Sequence[float]) -> tuple[float, ...]:
    """Unit-normalize a vector so cosine similarity reduces to a dot product.
    A zero vector normalizes to itself (cosine 0 against everything)."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return tuple(float(x) for x in vector)
    return tuple(float(x) / norm for x in vector)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def make_embedding_classifier(
    *,
    embed: EmbeddingFn,
    corpus: EmbeddingRoutingCorpus,
    k: int = 3,
    min_similarity: float = 0.5,
) -> LayerDecisionFn:
    """Build the Layer-2 ``EMBEDDING`` decision function from an injected sync
    ``embed`` and a trained ``corpus`` (C-CP-02 §2.1/§2.2/§2.4 — the L2-sync
    realization).

    The returned ``LayerDecisionFn`` embeds the call-site text, finds the
    ``k`` nearest corpus exemplars by cosine similarity, and returns the
    majority candidate among them — provided the nearest exemplar clears
    ``min_similarity``. Below threshold (or no textual content) it returns
    ``None``, falling through to LLM_AS_ROUTER per the §2.2 cheapest-first
    invariant. Deterministic given inputs (the corpus is pre-embedded once at
    build; ties broken by descending summed-similarity then candidate string).

    Raises ``ValueError`` on an illegal config (``k < 1``) — fail at build, not
    at dispatch.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    # Pre-embed + normalize the corpus once at build (the §2.2 "trained corpus"
    # projection; the per-dispatch hot path then only embeds the query).
    embedded: tuple[tuple[tuple[float, ...], str], ...] = tuple(
        (_normalize(embed(ex.text)), ex.candidate) for ex in corpus.exemplars
    )
    effective_k = min(k, len(embedded))

    def _embedding_decision(
        payload: ProviderAgnosticPayload, _manifest: RoutingManifest
    ) -> str | None:
        text = embedding_query_text(payload)
        if not text:
            return None
        query = _normalize(embed(text))
        # Cosine similarity (= dot of normalized) to every exemplar; take the k
        # nearest. Stable, deterministic ordering: descending similarity, then
        # candidate string as the tie-break.
        scored = sorted(
            ((_dot(query, ev), cand) for ev, cand in embedded),
            key=lambda sc: (-sc[0], sc[1]),
        )
        if scored[0][0] < min_similarity:
            return None  # nothing near enough — fall through (§2.2).
        top = scored[:effective_k]
        # Majority vote among the k nearest, ordered by (vote count, summed
        # similarity, candidate string) — all deterministic, so a tie at any
        # level resolves stably.
        agg: dict[str, tuple[int, float]] = {}
        for sim, cand in top:
            count, total = agg.get(cand, (0, 0.0))
            agg[cand] = (count + 1, total + sim)
        winner = max(agg.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))
        return winner[0]

    return _embedding_decision
