"""Reading a Hugging Face dataset as a stream."""

from __future__ import annotations

from typing import Any, Iterable


def open_stream(
    dataset: str,
    split: str,
    config: str | None = None,
    token: str | None = None,
    limit: int = 0,
    load_kwargs: dict[str, Any] | None = None,
) -> Iterable[dict]:
    """Open ``dataset``/``split`` with ``streaming=True``.

    With streaming only the shards needed to produce the rows we ask for are
    read, and ``.take(limit)`` stops the iteration after ``limit`` rows, so a
    small limit does not download the whole dataset.
    """
    from datasets import load_dataset  # imported lazily: heavy import

    stream = load_dataset(
        dataset,
        name=config or None,
        split=split,
        streaming=True,
        token=token,
        **(load_kwargs or {}),
    )
    if limit:
        stream = stream.take(limit)
    return stream
