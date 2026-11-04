"""Reading a Hugging Face dataset as a stream."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

# Feature types that hold media (decoded into images, audio arrays, ...). They
# cannot be stored in a SQL column, so they are dropped before reading.
_MEDIA_FEATURES = ("Image", "Audio", "Video", "Pdf", "Nifti")
_BINARY_DTYPES = ("binary", "large_binary")
_LABEL_SUFFIX = "_name"


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


@dataclass
class Prepared:
    rows: Iterable[dict]
    skipped: list[str] = field(default_factory=list)  # media/binary columns left out
    labels: dict[str, str] = field(default_factory=dict)  # label column -> added name column


def _is_media(feature: Any) -> bool:
    if type(feature).__name__ in _MEDIA_FEATURES:
        return True
    return getattr(feature, "dtype", None) in _BINARY_DTYPES


def prepare(stream: Iterable[dict]) -> Prepared:
    """Use the dataset's ``features`` (when it has them) to clean up the rows.

    - Media and binary columns are removed before any row is read, so images
      and audio are neither decoded nor stored as garbage text.
    - Every ``ClassLabel`` column (an integer such as ``label``) gets a text
      companion (``label_name``) holding the class name, e.g. ``neg``/``pos``.
    """
    features = getattr(stream, "features", None)
    if not features:
        return Prepared(stream)

    skipped = [name for name, feature in features.items() if _is_media(feature)]
    if skipped:
        if hasattr(stream, "decode"):
            stream = stream.decode(False)  # never decode the media we drop
        stream = stream.remove_columns(skipped)

    names: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    taken = set(features)
    for name, feature in features.items():
        if name in skipped or type(feature).__name__ != "ClassLabel":
            continue
        class_names = list(getattr(feature, "names", None) or [])
        if not class_names:
            continue
        target = name + _LABEL_SUFFIX
        while target in taken:
            target += "_"
        taken.add(target)
        names[name] = class_names
        labels[name] = target

    rows: Iterable[dict] = _with_label_names(stream, names, labels) if labels else stream
    return Prepared(rows, skipped, labels)


def _with_label_names(
    rows: Iterable[dict], names: dict[str, list[str]], labels: dict[str, str]
) -> Iterator[dict]:
    for row in rows:
        row = dict(row)
        for column, target in labels.items():
            value = row.get(column)
            known = names[column]
            # -1 means "no label" in Hugging Face datasets (e.g. hidden test labels).
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(known)
            row[target] = known[value] if ok else None
        yield row


def _short_list(text: str, limit: int = 15) -> str:
    items = re.findall(r"'([^']*)'", text)
    if not items:
        return text
    shown = ", ".join(items[:limit])
    return shown + (f" ... ({len(items)} in total)" if len(items) > limit else "")


def explain(exc: BaseException, dataset: str) -> str:
    """Turn an error from the Hugging Face libraries into an actionable message."""
    text = str(exc)
    low = text.lower()
    if "namespace/name" in low:
        return (
            f"dataset '{dataset}' needs its full id in the form owner/name "
            f"(for example 'stanfordnlp/imdb'); look it up at "
            f"https://huggingface.co/datasets?search={dataset}"
        )
    if "gated dataset" in low:
        return (
            f"dataset '{dataset}' is gated: accept its terms at "
            f"https://huggingface.co/datasets/{dataset} and set the HF_TOKEN "
            "environment variable for the IRIS process"
        )
    if "config name is missing" in low:
        match = re.search(r"available configs:\s*(\[[^\]]*\])", text, re.IGNORECASE)
        available = f" Available: {_short_list(match.group(1))}." if match else ""
        return (
            f"dataset '{dataset}' has several configurations; pass one as the "
            f"config argument.{available}"
        )
    if "doesn't exist on the hub" in low:
        return (
            f"dataset '{dataset}' was not found on the Hugging Face Hub; check the id "
            "(owner/name). If it is private, set the HF_TOKEN environment variable"
        )
    if "bad split" in low:
        match = re.search(r"available splits:\s*(\[[^\]]*\])", text, re.IGNORECASE)
        available = f" Available splits: {_short_list(match.group(1))}." if match else ""
        return f"split not found in dataset '{dataset}'.{available}"
    return f"{type(exc).__name__}: {text}"
