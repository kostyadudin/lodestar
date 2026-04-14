"""Optional embedding support for semantic retrieval.

Install the [embeddings] extra to enable:
    pip install lodestar[embeddings]

When sentence-transformers is not installed every public function is a no-op
or returns an empty result so callers need no conditional logic.
"""

from __future__ import annotations

DEFAULT_MODEL = "all-MiniLM-L6-v2"

_model = None
_model_name: str | None = None


def available() -> bool:
    """Return True if sentence-transformers is importable."""
    try:
        import sentence_transformers  # noqa: F401

        return True
    except ImportError:
        return False


def encode(texts: list[str], model_name: str = DEFAULT_MODEL) -> list[bytes]:
    """Encode *texts* and return each vector as packed float32 bytes.

    Vectors are L2-normalised so dot product == cosine similarity.
    Returns an empty list when the package is not installed.
    """
    if not texts:
        return []
    global _model, _model_name
    if _model is None or _model_name != model_name:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(model_name)
        _model_name = model_name
    vecs = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.astype("float32").tobytes() for v in vecs]


def cosine_scores(query_bytes: bytes, ref_vectors: list[tuple[str, bytes]]) -> dict[str, float]:
    """Return cosine similarity between *query_bytes* and every (ref, vector) pair.

    Only positive scores are included.  Returns an empty dict on error or when
    the input is empty.
    """
    if not ref_vectors or not query_bytes:
        return {}
    try:
        import numpy as np

        q = np.frombuffer(query_bytes, dtype="float32")
        matrix = np.stack([np.frombuffer(v, dtype="float32") for _, v in ref_vectors])
        raw = (matrix @ q).tolist()
        return {ref: float(s) for (ref, _), s in zip(ref_vectors, raw) if s > 0}
    except Exception:
        return {}
