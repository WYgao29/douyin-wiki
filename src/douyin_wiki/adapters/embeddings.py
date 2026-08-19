from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

import numpy as np

from ..config import EmbeddingSettings


class EmbeddingService:
    """Local embeddings with a deterministic offline fallback.

    The fallback is character n-gram hashing, so the application remains usable before
    the optional sentence-transformers model has downloaded.
    """

    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self._model = None
        self._load_attempted = False
        self.provider_name = "char-ngram-fallback"

    def _load_model(self):
        if self._model is not None:
            return self._model
        if self.settings.provider != "sentence-transformers" or self._load_attempted:
            return None
        self._load_attempted = True
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

            self._model = SentenceTransformer(self.settings.model)
            self.provider_name = f"sentence-transformers:{self.settings.model}"
        except (ImportError, OSError, RuntimeError):
            self._model = None
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load_model()
        if model is not None:
            values = model.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
            return np.asarray(values, dtype=np.float32).tolist()
        return [self._hashed_embedding(text) for text in texts]

    def signature(self) -> str:
        """Return the effective provider/model/dimension tuple used by stored vectors."""
        probe = self.embed(["douyin-wiki-index-signature"])[0]
        return f"v1:{self.provider_name}:dim={len(probe)}"

    def _hashed_embedding(self, text: str) -> list[float]:
        dimensions = self.settings.fallback_dimensions
        vector = np.zeros(dimensions, dtype=np.float32)
        normalized = "".join(text.lower().split())
        tokens = [normalized[index : index + 2] for index in range(max(1, len(normalized) - 1))]
        if not tokens and normalized:
            tokens = [normalized]
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            index = number % dimensions
            sign = 1 if (number >> 8) & 1 else -1
            vector[index] += sign
        norm = math.sqrt(float(np.dot(vector, vector)))
        if norm:
            vector /= norm
        return vector.tolist()


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator else 0.0
