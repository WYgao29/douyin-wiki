from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

import numpy as np

from ..config import EmbeddingSettings
from ..errors import ExternalToolError


class EmbeddingService:
    """Local embeddings with a deterministic offline fallback.

    The fallback is character n-gram hashing, so the application remains usable before
    the optional sentence-transformers model has downloaded.
    """

    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self._model = None
        self._load_attempted = False
        self._dimensions: int | None = None
        self.provider_name = "char-ngram-fallback"
        self.last_error: str | None = None

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
            dimension = getattr(self._model, "get_sentence_embedding_dimension", lambda: None)()
            if isinstance(dimension, int) and dimension > 0:
                self._dimensions = dimension
        except Exception as exc:  # Optional acceleration must never disable local search.
            self._model = None
            self.provider_name = "char-ngram-fallback"
            self.last_error = f"{type(exc).__name__}: {exc}"
        return self._model

    def embed(self, texts: Sequence[str], *, persistent: bool = False) -> list[list[float]]:
        model = self._load_model()
        if model is not None:
            try:
                values = model.encode(
                    list(texts), normalize_embeddings=True, show_progress_bar=False
                )
                array = np.asarray(values, dtype=np.float32)
                if array.ndim == 2 and array.shape[1] > 0:
                    self._dimensions = int(array.shape[1])
                return array.tolist()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if persistent:
                    # Once this provider has produced persisted vectors, silently
                    # switching only this write to a different vector space would
                    # permanently corrupt similarity results. Let the job retry.
                    raise ExternalToolError(
                        "嵌入模型暂时不可用，未写入不兼容向量",
                        details={"cause": self.last_error},
                    ) from exc
                # Queries remain available through lexical search. A zero vector in
                # the provider's dimension disables only the semantic contribution.
                dimensions = self._dimensions or self.settings.fallback_dimensions
                return np.zeros((len(texts), dimensions), dtype=np.float32).tolist()
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
