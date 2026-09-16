"""Shared embedding helper for the context-stack evaluations.

One model, one encoder call shape, so every evaluation measures the same technique
(bge-small-en-v1.5 on Apple MPS, the embedder the Pixeltable pilot used).
"""

from __future__ import annotations

import numpy as np

MODEL_ID = "BAAI/bge-small-en-v1.5"


def embed(texts: list[str], batch_size: int = 64) -> np.ndarray:
    """Return L2-normalised float32 embeddings, one row per text."""
    from sentence_transformers import (
        SentenceTransformer,  # [LAW:effects-at-boundaries] model load at the edge
    )

    model = SentenceTransformer(MODEL_ID, device="mps")
    vecs = model.encode(
        texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False
    )
    return np.asarray(vecs, dtype=np.float32)


def top_k(query: np.ndarray, corpus: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Indices and cosine scores of the k nearest corpus rows to one query row."""
    scores = corpus @ query
    idx = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
    return sorted(((int(i), float(scores[i])) for i in idx), key=lambda t: -t[1])
