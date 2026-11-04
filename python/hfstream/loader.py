"""Load a Hugging Face dataset into an IRIS table.

Flow of ``run_load``:

1. validate the arguments and refuse to touch an existing table;
2. open the dataset with ``streaming=True`` (+ ``take(limit)``);
3. sample the first rows and infer the column types;
4. ``CREATE TABLE`` and insert the rows in batches, one transaction per batch;
5. if anything fails, roll back the current batch and drop the table we created,
   so a failed load never leaves a half-filled table behind.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from itertools import chain, islice
from typing import Any, Iterable, Iterator

from .backend import Backend
from .errors import (
    DatabaseError,
    DownloadError,
    EmptyDatasetError,
    HFStreamError,
    ValidationError,
)
from .schema import Column, create_table_sql, encode_row, infer_schema, insert_sql
from .source import open_stream

log = logging.getLogger("hfstream")

DEFAULT_BATCH_SIZE = 1000
DEFAULT_SAMPLE_SIZE = 1000
MAX_BATCH_SIZE = 100_000

_DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(/[A-Za-z0-9][A-Za-z0-9_.\-]*)?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Request:
    dataset: str
    split: str
    schema: str
    table: str
    limit: int
    config: str | None
    batch_size: int
    sample_size: int

    @property
    def target(self) -> str:
        return f"{self.schema}.{self.table}"


def _to_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer, got {value!r}") from None
    if not minimum <= number <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}, got {number}")
    return number


def _validate(
    dataset: Any,
    split: Any,
    target: Any,
    limit: Any,
    config: Any,
    batch_size: Any,
    sample_size: Any,
    need_target: bool = True,
) -> Request:
    dataset = str(dataset or "").strip()
    if not _DATASET_RE.match(dataset):
        raise ValidationError(
            f"invalid dataset id {dataset!r}; expected 'name' or 'owner/name' as on huggingface.co"
        )
    split = str(split or "train").strip()
    if not _NAME_RE.match(split):
        raise ValidationError(f"invalid split name {split!r}")
    config = str(config or "").strip() or None
    if config is not None and not _NAME_RE.match(config):
        raise ValidationError(f"invalid config name {config!r}")

    schema = table = ""
    if need_target:
        parts = str(target or "").strip().split(".")
        if len(parts) != 2 or not all(_IDENT_RE.match(p) for p in parts):
            raise ValidationError(
                f"invalid target {target!r}; expected schema.table with letters, digits "
                "and underscores, starting with a letter"
            )
        schema, table = parts

    return Request(
        dataset=dataset,
        split=split,
        schema=schema,
        table=table,
        limit=_to_int(limit, "limit", 0, 0, 10**12),
        config=config,
        batch_size=_to_int(batch_size, "batch_size", DEFAULT_BATCH_SIZE, 1, MAX_BATCH_SIZE),
        sample_size=_to_int(sample_size, "sample_size", DEFAULT_SAMPLE_SIZE, 1, MAX_BATCH_SIZE),
    )


@contextmanager
def _wrap(error_cls: type[HFStreamError]):
    """Re-raise unexpected exceptions as ``error_cls``; keep our own errors as they are."""
    try:
        yield
    except HFStreamError:
        raise
    except Exception as exc:
        raise error_cls(f"{type(exc).__name__}: {exc}") from exc


def _default_backend() -> Backend:
    from .backend import IrisBackend

    try:
        return IrisBackend()
    except ImportError as exc:
        raise DatabaseError(
            "the 'iris' module is not available; this must run inside InterSystems IRIS"
        ) from exc


def _open(req: Request, token: str | None, load_kwargs: dict | None) -> Iterator[dict]:
    with _wrap(DownloadError):
        stream = open_stream(req.dataset, req.split, req.config, token, req.limit, load_kwargs)
        iterator = iter(stream)
        if req.limit:  # belt and braces: take() already limits a streaming dataset
            iterator = islice(iterator, req.limit)
    return iterator


def _insert_all(
    db: Backend,
    insert: str,
    columns: list[Column],
    rows: Iterable[dict],
    batch_size: int,
) -> int:
    rows = iter(rows)
    total = 0
    while True:
        with _wrap(DownloadError):
            chunk = list(islice(rows, batch_size))
        if not chunk:
            return total
        encoded = [encode_row(columns, row) for row in chunk]
        with _wrap(DatabaseError):
            db.begin()
            try:
                db.insert_many(insert, encoded)
                db.commit()
            except BaseException:
                with suppress(Exception):
                    db.rollback()
                raise
        total += len(encoded)
        log.info("hfstream: %d rows loaded", total)


def run_load(
    dataset: str,
    split: str = "train",
    target: str = "",
    limit: int = 0,
    config: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    token: str | None = None,
    backend: Backend | None = None,
    load_kwargs: dict | None = None,
) -> dict:
    """Load ``dataset``/``split`` into the table ``target`` (schema.table). Returns a dict."""
    started = time.perf_counter()
    req = _validate(dataset, split, target, limit, config, batch_size, sample_size)
    db = backend if backend is not None else _default_backend()

    with _wrap(DatabaseError):
        if db.table_exists(req.schema, req.table):
            raise ValidationError(
                f"table {req.target} already exists; drop it first (DROP TABLE {req.target})"
            )

    iterator = _open(req, token, load_kwargs)
    with _wrap(DownloadError):
        sample = list(islice(iterator, req.sample_size))
    if not sample:
        raise EmptyDatasetError(f"{req.dataset} / {req.split} returned no rows")

    columns = infer_schema(sample)
    with _wrap(DatabaseError):
        db.execute(create_table_sql(req.target, columns))
    try:
        total = _insert_all(
            db, insert_sql(req.target, columns), columns, chain(sample, iterator), req.batch_size
        )
    except BaseException:
        with suppress(Exception):
            db.drop_table(req.target)
        raise

    return {
        "status": "ok",
        "table": req.target,
        "rows_loaded": total,
        "seconds": round(time.perf_counter() - started, 3),
        "columns": [{"name": c.name, "type": c.sql_type} for c in columns],
    }


def run_info(
    dataset: str,
    split: str = "train",
    config: str | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    token: str | None = None,
    load_kwargs: dict | None = None,
) -> dict:
    """Dry run: sample the dataset and report the columns that would be created."""
    req = _validate(dataset, split, None, sample_size, config, None, sample_size, need_target=False)
    iterator = _open(req, token, load_kwargs)
    with _wrap(DownloadError):
        sample = list(islice(iterator, req.sample_size))
    if not sample:
        raise EmptyDatasetError(f"{req.dataset} / {req.split} returned no rows")
    return {
        "status": "ok",
        "dataset": req.dataset,
        "split": req.split,
        "sampled_rows": len(sample),
        "columns": [
            {"name": c.name, "source": str(c.source), "type": c.sql_type}
            for c in infer_schema(sample)
        ],
    }


def _error_json(code: str, message: str) -> str:
    return json.dumps({"status": "error", "code": code, "message": message}, ensure_ascii=False)


def load_to_iris(
    dataset: str,
    split: str = "train",
    target: str = "",
    limit: int = 0,
    config: str = "",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> str:
    """Entry point called from ObjectScript. Always returns a JSON string, never raises."""
    try:
        result = run_load(
            dataset,
            split,
            target,
            limit,
            config,
            batch_size,
            token=os.environ.get("HF_TOKEN") or None,
        )
        return json.dumps(result, ensure_ascii=False)
    except HFStreamError as exc:
        return _error_json(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 - last resort, the caller expects JSON
        log.exception("hfstream: unexpected error")
        return _error_json("INTERNAL", f"{type(exc).__name__}: {exc}")


def dataset_info(
    dataset: str,
    split: str = "train",
    config: str = "",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> str:
    """Entry point for ``HF.Loader.Info``. Always returns a JSON string, never raises."""
    try:
        result = run_info(
            dataset, split, config, sample_size, token=os.environ.get("HF_TOKEN") or None
        )
        return json.dumps(result, ensure_ascii=False)
    except HFStreamError as exc:
        return _error_json(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("hfstream: unexpected error")
        return _error_json("INTERNAL", f"{type(exc).__name__}: {exc}")
