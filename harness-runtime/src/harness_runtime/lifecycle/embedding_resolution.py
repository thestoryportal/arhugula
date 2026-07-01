"""R-FS-1 L2 — the light in-process embedding realization + the routing corpus.

Impl-discretion realization (C-CP-02 §2.4 vendor deferral) of the ``EmbeddingFn``
the CP-pure ``make_embedding_classifier`` (``harness_cp.embedding_routing``)
injects. Operator-decided **Option B** (2026-06-16): a **light in-process**
embedding library — ``fastembed`` (Qdrant's ONNX-backed embedder; **torch-free**,
x86-macOS-stable), NOT ``sentence-transformers``/``torch`` and NOT a remote API.
The light form is what delivers the self-sufficiency / portability the operator
chose Option B for (works standalone with no external model server; no heavy
platform wheels) — see ``.harness/r-l2-option-b-light-embedding`` memory.

The embedding is **sync** (the L2-sync determinant — an in-process model), so the
classifier it backs composes inside the sync ``route()`` (no fork, no spec
amendment; the L2-sync path of R-DESIGN §3 D2). ``fastembed`` is a lazy import
(an OPTIONAL ``[embedding]`` extra) so the base runtime install stays light and
the provider-free lanes never pull ``onnxruntime``; importing THIS module does
not require ``fastembed`` — only constructing a real embedding does.

**Reachability honesty (carries from L3):** even bound, Layer 2 routes no
production traffic until ``R-300-second-provider`` makes DECLARATIVE conditional
(DECLARATIVE always echoes today → ``route()`` short-circuits before EMBEDDING),
exactly as the L3 router is inert at HEAD. At that activation the ``[embedding]``
extra is promoted to a required dependency (forward note:
``.harness/beyond-mvp-capability-boundary-ledger.md`` B-L2-EMBEDDING-ACTIVATION).

Authority: ``Spec_Control_Plane_v1_36.md`` §2 C-CP-02 §2.1/§2.2/§2.4; ADR-F1
v1.2; ``.harness/r-fs-1-r-routing-intelligence-design-v1.md`` §3/§6 (D2 L2-sync +
the L2 vendor gate); operator decision 2026-06-16 (Option B).
"""

from __future__ import annotations

import importlib
from typing import Any

from harness_cp.embedding_routing import EmbeddingFn, EmbeddingRoutingCorpus

# The default light embedding model — BAAI/bge-small-en-v1.5: 384-dim, ~133 MB,
# ONNX/torch-free via fastembed, strong on short-text semantic similarity. The
# specific model is impl-discretion (§2.4); an operator may bind another by
# passing `model_name` (the classifier is model-agnostic via `EmbeddingFn`).
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def make_fastembed_embedding(*, model_name: str = DEFAULT_EMBEDDING_MODEL) -> EmbeddingFn:
    """Build a sync ``EmbeddingFn`` backed by ``fastembed`` (the light-form
    realization; mirror of ``make_ollama_router``).

    The model is constructed eagerly (downloading on first use is bounded to this
    wiring-time call, not the routing hot path); the returned closure projects a
    text into a dense vector. The numpy ndarray ``fastembed`` returns is
    converted to ``tuple[float, ...]`` HERE so numpy never crosses into the
    CP-pure classifier (the ``EmbeddingFn`` return type is ``Sequence[float]``).

    Raises ``ImportError`` (with the install hint) when the ``[embedding]`` extra
    is not installed — fail loud, never silently degrade routing.

    fastembed is imported **dynamically** (``importlib``) rather than via a static
    ``from fastembed import ...``: the static import would fail the strict pyright
    gate in the provider-free CI lane (where the optional ``[embedding]`` extra is
    NOT installed — ``reportMissingImports`` + unknown-type propagation; Codex
    [P1]). The dynamic import keeps fastembed invisible to the typechecker while
    preserving the fail-loud runtime behavior; ``Any`` confines the untyped
    surface to this realization (numpy never crosses into the CP-pure classifier).
    """
    try:
        fastembed: Any = importlib.import_module("fastembed")
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "fastembed is required for the Layer-2 in-process embedding "
            "realization; install the optional extra: `uv sync --extra embedding` "
            "(harness-runtime[embedding])"
        ) from exc

    model: Any = fastembed.TextEmbedding(model_name=model_name)

    def _embed(text: str) -> tuple[float, ...]:
        # `model.embed([text])` yields one ndarray per input text.
        vector: Any = next(iter(model.embed([text])))
        return tuple(float(x) for x in vector)

    return _embed


def default_routing_corpus() -> EmbeddingRoutingCorpus:
    """A representative trained per-workload-class corpus (C-CP-02 §2.1 — the L2
    authoring deliverable).

    Maps characteristic call-site utterances across the four ``WorkloadClass``
    families to a sensible ``"provider:model"`` candidate (capability-aware,
    cheapest-adequate per the C-CP-02 cost discipline). This is an **illustrative
    default**: the candidates and exemplars are operator-tunable — an operator
    retrains the corpus for their own provider bindings + workload mix; it
    demonstrates + exercises the capability rather than fixing a routing policy.
    """
    # Candidate bindings (illustrative; capability-aware-cheapest-adequate):
    #   hard reasoning / code  → a frontier model
    #   creative / short-form  → a cheaper fast model
    #   deterministic pipeline → a free local model
    #   research / analysis    → a long-context frontier model
    code = "anthropic:claude-opus-4-8"
    creative = "anthropic:claude-haiku-4-5"
    pipeline = "ollama:llama3.2:3b"
    research = "openai:gpt-5.5"
    return EmbeddingRoutingCorpus.from_pairs(
        [
            # software-engineering
            ("write a python function to parse a config file", code, "software-engineering"),
            ("debug this failing unit test and fix the bug", code, "software-engineering"),
            ("refactor this module and add type annotations", code, "software-engineering"),
            ("implement a REST API endpoint with validation", code, "software-engineering"),
            # content-creation
            ("write a short story about a lighthouse keeper", creative, "content-creation"),
            ("compose a poem in the style of haiku", creative, "content-creation"),
            ("draft a friendly marketing email for a launch", creative, "content-creation"),
            ("rewrite this paragraph to sound more engaging", creative, "content-creation"),
            # pipeline-automation
            ("extract the totals from each row of this csv", pipeline, "pipeline-automation"),
            ("convert this json payload into a flat table", pipeline, "pipeline-automation"),
            ("run the nightly etl batch and summarize counts", pipeline, "pipeline-automation"),
            ("validate these records against the schema", pipeline, "pipeline-automation"),
            # research
            ("summarize the key findings of this research paper", research, "research"),
            ("compare these two studies and their methodology", research, "research"),
            ("analyze the trends across these survey results", research, "research"),
            ("synthesize the literature on this topic", research, "research"),
        ]
    )
