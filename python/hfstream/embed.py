"""Text embeddings for VECTOR columns.

Uses model2vec static embeddings: they only need numpy (no PyTorch), load in a few
seconds and encode thousands of texts per second, so they keep up with a streaming
load. Any model2vec model id from the Hugging Face Hub can be used.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .errors import ValidationError

DEFAULT_MODEL = "minishlab/potion-base-8M"  # 256 dimensions, ~30 MB

_models: dict[str, Any] = {}


def get_model(name: str | None = None):
    """Load (once per process) and return a model2vec ``StaticModel``."""
    name = name or DEFAULT_MODEL
    model = _models.get(name)
    if model is None:
        try:
            from model2vec import StaticModel
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ValidationError(
                "embeddings need the 'model2vec' package; install requirements.txt"
            ) from exc
        model = StaticModel.from_pretrained(name)
        _models[name] = model
    return model


def dimension(model) -> int:
    return int(model.dim)


def as_text(value: Any) -> str | None:
    """The text to embed for a cell value; None/empty values get no vector."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if text.strip() else None


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(x):.6g}" for x in vector) + "]"


def encode(model, values: Sequence[Any]) -> list[str | None]:
    """Embed ``values``; returns '[x,y,...]' strings (None where there is no text)."""
    texts = [as_text(v) for v in values]
    present = [t for t in texts if t is not None]
    vectors = iter(model.encode(present)) if present else iter(())
    return [vector_literal(next(vectors)) if t is not None else None for t in texts]
