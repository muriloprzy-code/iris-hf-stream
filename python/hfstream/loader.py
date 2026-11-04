"""Load a Hugging Face dataset into an IRIS table.

Flow of ``run_load``:

1. validate the arguments and the options (mode, columns, embed...);
2. open the dataset with ``streaming=True`` (+ ``take(limit)``), one stream per
   split for ``split="all"``; media columns are dropped, class labels named;
3. sample the first rows and infer the column types;
4. create the table (``mode`` decides what happens when it exists) and insert the
   rows in batches, one transaction per batch, widening a column when a later row
   does not fit it;
5. if anything fails, roll back the current batch and drop the table this call
   created, so a failed load never leaves a half-filled table behind.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from itertools import chain, islice
from typing import Any, Iterable, Iterator

from . import embed as embedding
from .backend import Backend
from .errors import (
    DatabaseError,
    DownloadError,
    EmptyDatasetError,
    HFStreamError,
    SchemaMismatchError,
    ValidationError,
)
from .schema import (
    Column,
    create_table_sql,
    encode_value,
    infer_schema,
    insert_sql,
    is_reserved,
    sanitize_identifier,
    vector_column,
    widen,
)
from .source import explain, open_stream, prepare, search, select, split_info, split_names

log = logging.getLogger("hfstream")

DEFAULT_BATCH_SIZE = 1000
DEFAULT_SAMPLE_SIZE = 1000
MAX_BATCH_SIZE = 100_000
MODES = ("fail", "replace", "append")
ALL_SPLITS = "all"
SPLIT_KEY = "hf_split"
TMP_SUFFIX = "_hfstream_tmp"
_EMBED_PREFIX = "__embedding__"
_OPTION_KEYS = ("mode", "columns", "embed", "embed_model", "sample_size")

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
    mode: str = "fail"
    columns: tuple[str, ...] = ()
    embed: tuple[str, ...] = ()
    embed_model: str | None = None

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


def _names(value: Any, option: str) -> tuple[str, ...]:
    """A column list given as a JSON list or a comma-separated string."""
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        value = [v for v in (p.strip() for p in value.split(",")) if v]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
        raise ValidationError(f'option "{option}" must be a list of column names')
    return tuple(dict.fromkeys(value))  # keep order, drop duplicates


def parse_options(options: Any) -> dict:
    """``options`` is a JSON object (string or dict); unknown keys are an error."""
    if options is None or options == "" or options == "\x00":
        return {}
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except ValueError:
            raise ValidationError(f"options must be a JSON object, got {options!r:.80}") from None
    if not isinstance(options, dict):
        raise ValidationError("options must be a JSON object")
    unknown = sorted(set(options) - set(_OPTION_KEYS))
    if unknown:
        raise ValidationError(
            f"unknown option(s) {', '.join(unknown)}; valid options: {', '.join(_OPTION_KEYS)}"
        )
    mode = str(options.get("mode") or "fail").strip().lower()
    if mode not in MODES:
        raise ValidationError(f'option "mode" must be one of {", ".join(MODES)}, got {mode!r}')
    model = options.get("embed_model")
    if model is not None and not (isinstance(model, str) and _DATASET_RE.match(model)):
        raise ValidationError(f'option "embed_model" must be a model id, got {model!r}')
    return {
        "mode": mode,
        "columns": _names(options.get("columns"), "columns"),
        "embed": _names(options.get("embed"), "embed"),
        "embed_model": model,
        "sample_size": options.get("sample_size"),
    }


def _validate(
    dataset: Any,
    split: Any,
    target: Any,
    limit: Any,
    config: Any,
    batch_size: Any,
    sample_size: Any,
    need_target: bool = True,
    options: Any = None,
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
    opts = parse_options(options)

    schema = table = ""
    if need_target:
        parts = str(target or "").strip().split(".")
        if len(parts) != 2 or not all(_IDENT_RE.match(p) for p in parts):
            raise ValidationError(
                f"invalid target {target!r}; expected schema.table with letters, digits "
                "and underscores, starting with a letter"
            )
        schema, table = parts
        reserved = [p for p in parts if is_reserved(p)]
        if reserved:
            raise ValidationError(
                f"invalid target {target!r}: {', '.join(reserved)} is a reserved word in "
                "IRIS SQL; choose another name"
            )

    if opts.get("sample_size") not in (None, ""):
        sample_size = opts["sample_size"]
    if opts.get("columns") and opts.get("embed"):
        missing = [c for c in opts["embed"] if c not in opts["columns"]]
        if missing:
            raise ValidationError(
                f"embedded column(s) {', '.join(missing)} must also be in \"columns\""
            )
    return Request(
        dataset=dataset,
        split=split,
        schema=schema,
        table=table,
        limit=_to_int(limit, "limit", 0, 0, 10**12),
        config=config,
        batch_size=_to_int(batch_size, "batch_size", DEFAULT_BATCH_SIZE, 1, MAX_BATCH_SIZE),
        sample_size=_to_int(sample_size, "sample_size", DEFAULT_SAMPLE_SIZE, 1, MAX_BATCH_SIZE),
        mode=opts.get("mode", "fail"),
        columns=opts.get("columns", ()),
        embed=opts.get("embed", ()),
        embed_model=opts.get("embed_model"),
    )


@contextmanager
def _wrap(error_cls: type[HFStreamError], dataset: str | None = None):
    """Re-raise unexpected exceptions as ``error_cls``; keep our own errors as they are.

    With ``dataset``, errors from the Hugging Face libraries are rewritten into an
    actionable message (missing owner/, gated dataset, missing config...).
    """
    try:
        yield
    except HFStreamError:
        raise
    except Exception as exc:
        message = explain(exc, dataset) if dataset else f"{type(exc).__name__}: {exc}"
        raise error_cls(message) from exc


def _default_backend() -> Backend:
    from .backend import IrisBackend

    try:
        return IrisBackend()
    except ImportError as exc:
        raise DatabaseError(
            "the 'iris' module is not available; this must run inside InterSystems IRIS"
        ) from exc


@dataclass
class Opened:
    rows: Iterator[dict]
    skipped: list[str]
    keep: set[str] | None  # row keys to keep when "columns" is given, else None
    split_counts: Counter | None  # rows per split, for split="all"


def _open_split(req: Request, split: str, token, load_kwargs):
    stream = open_stream(req.dataset, split, req.config, token, req.limit, load_kwargs)
    prepared = prepare(select(stream, list(req.columns) or None))
    rows: Iterable[dict] = prepared.rows
    if req.limit:  # belt and braces: take() already limits a streaming dataset
        rows = islice(rows, req.limit)
    return rows, prepared


def _tagged(rows: Iterable[dict], key: str, split: str, counts: Counter) -> Iterator[dict]:
    for row in rows:
        row = dict(row)
        row[key] = split
        counts[split] += 1
        yield row


def _open(req: Request, token: str | None, load_kwargs: dict | None) -> Opened:
    """Open the stream(s): one split, or every split for ``split="all"``."""
    with _wrap(DownloadError, req.dataset):
        splits = [req.split]
        if req.split == ALL_SPLITS:
            names = split_names(req.dataset, req.config, token, **(load_kwargs or {}))
            splits = [ALL_SPLITS] if ALL_SPLITS in names else names
        opened = [(name, *_open_split(req, name, token, load_kwargs)) for name in splits]

    first = opened[0][2]
    keep = None
    if req.columns:
        keep = set(req.columns) | {first.labels[c] for c in req.columns if c in first.labels}
    if splits == [req.split]:
        return Opened(iter(opened[0][1]), list(first.skipped), keep, None)

    features = getattr(first.rows, "features", None) or {}
    key = SPLIT_KEY
    while key in features or key in first.labels.values():
        key += "_"
    counts: Counter = Counter()
    rows = chain.from_iterable(_tagged(r, key, name, counts) for name, r, _ in opened)
    if keep is not None:
        keep.add(key)
    return Opened(rows, list(first.skipped), keep, counts)


def _only(rows: Iterable[dict], keep: set[str]) -> Iterator[dict]:
    for row in rows:
        yield {k: v for k, v in row.items() if k in keep}


@dataclass
class Embedder:
    source: str  # dataset column whose text is embedded
    key: str  # row key holding the vector literal
    model: Any


def _sample(req: Request, opened: Opened):
    with _wrap(DownloadError, req.dataset):
        sample = list(islice(opened.rows, req.sample_size))
    if not sample:
        raise EmptyDatasetError(f"{req.dataset} / {req.split} returned no rows")

    rows: Iterable[dict] = opened.rows
    if opened.keep is not None:
        present = {k for row in sample for k in row}
        unknown = [c for c in req.columns if c not in present]
        if unknown:
            raise ValidationError(
                f"unknown column(s) {', '.join(unknown)}; the dataset has: "
                f"{', '.join(sorted(present))}"
            )
        sample = list(_only(sample, opened.keep))
        rows = _only(rows, opened.keep)

    columns = infer_schema(sample, opened.skipped)
    if not columns:
        raise EmptyDatasetError(
            f"{req.dataset} / {req.split} has no column that can be stored in a table "
            f"(left out: {', '.join(opened.skipped)})"
        )

    embedders = []
    if req.embed:
        by_source = {c.source: c for c in columns}
        unknown = [c for c in req.embed if c not in by_source]
        if unknown:
            raise ValidationError(
                f"cannot embed unknown column(s) {', '.join(unknown)}; "
                f"columns: {', '.join(map(str, by_source))}"
            )
        with _wrap(DownloadError):
            model = embedding.get_model(req.embed_model)
        used = {c.name.lower() for c in columns}
        for source in req.embed:
            name = sanitize_identifier(f"{by_source[source].name}_embedding", used)
            key = _EMBED_PREFIX + source
            columns.append(vector_column(name, key, embedding.dimension(model)))
            embedders.append(Embedder(source, key, model))
    return sample, rows, columns, embedders


def _encode(
    db: Backend,
    target: str,
    columns: list[Column],
    row: dict,
    allow_widen: bool,
    widened: list[dict],
) -> tuple:
    """Encode one row; widen a column (ALTER TABLE) when a value does not fit it."""
    values = []
    for i in range(len(columns)):
        value = row.get(columns[i].source)
        while True:
            try:
                values.append(encode_value(columns[i], value))
                break
            except SchemaMismatchError:
                wider = widen(columns[i], value) if allow_widen else None
                if wider is None:
                    raise
                with _wrap(DatabaseError):
                    db.alter_column(target, wider.name, wider.sql_type)
                log.info("hfstream: widened %s to %s", wider.name, wider.sql_type)
                widened.append({"name": wider.name, "from": columns[i].sql_type, "to": wider.sql_type})
                columns[i] = wider
    return tuple(values)


def _insert_all(
    db: Backend,
    target: str,
    columns: list[Column],
    rows: Iterable[dict],
    batch_size: int,
    dataset: str | None = None,
    embedders: list[Embedder] = (),
    allow_widen: bool = True,
    widened: list[dict] | None = None,
    progress: dict | None = None,
) -> int:
    rows = iter(rows)
    widened = [] if widened is None else widened
    progress = {} if progress is None else progress
    insert = insert_sql(target, columns, db.vector_placeholder)
    total = 0
    while True:
        with _wrap(DownloadError, dataset):
            chunk = list(islice(rows, batch_size))
        if not chunk:
            return total
        for e in embedders:
            with _wrap(DatabaseError):
                vectors = embedding.encode(e.model, [row.get(e.source) for row in chunk])
            for row, vector in zip(chunk, vectors):
                row[e.key] = vector
        encoded = [_encode(db, target, columns, row, allow_widen, widened) for row in chunk]
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
        progress["rows"] = total
        log.info("hfstream: %d rows loaded", total)


def _merge_widened(widened: list[dict]) -> list[dict]:
    """One entry per column: from its original type to its final one."""
    merged: dict[str, dict] = {}
    for w in widened:
        merged.setdefault(w["name"], {"name": w["name"], "from": w["from"]})["to"] = w["to"]
    return list(merged.values())


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
    options: Any = None,
) -> dict:
    """Load ``dataset``/``split`` into the table ``target`` (schema.table). Returns a dict.

    ``options`` (JSON object): ``mode`` (fail | replace | append), ``columns``,
    ``embed``, ``embed_model``, ``sample_size``.
    """
    started = time.perf_counter()
    req = _validate(
        dataset, split, target, limit, config, batch_size, sample_size, options=options
    )
    db = backend if backend is not None else _default_backend()

    with _wrap(DatabaseError):
        exists = db.table_exists(req.schema, req.table)
    if exists and req.mode == "fail":
        raise ValidationError(
            f"table {req.target} already exists; drop it first (DROP TABLE {req.target}) "
            'or pass {"mode": "replace"} or {"mode": "append"} in the options'
        )

    opened = _open(req, token, load_kwargs)
    sample, rows, columns, embedders = _sample(req, opened)

    load_target, created, allow_widen = req.target, True, True
    with _wrap(DatabaseError):
        if exists and req.mode == "append":
            existing = set(db.table_columns(req.schema, req.table))
            missing = [c.name for c in columns if c.name.lower() not in existing]
            if missing:
                raise ValidationError(
                    f"table {req.target} has no column(s) {', '.join(missing)} for this dataset; "
                    'choose the columns to load with the "columns" option'
                )
            # The table is not ours: IRIS validates the sizes, nothing is widened.
            columns = [replace(c, max_len=None) for c in columns]
            created, allow_widen = False, False
        elif exists and req.mode == "replace":
            load_target = req.target + TMP_SUFFIX
            if db.table_exists(req.schema, req.table + TMP_SUFFIX):
                db.drop_table(load_target)  # left over by an interrupted replace
            db.execute(create_table_sql(load_target, columns))
        else:
            db.execute(create_table_sql(load_target, columns))

    widened: list[dict] = []
    progress = {"rows": 0}
    try:
        total = _insert_all(
            db,
            load_target,
            columns,
            chain(sample, rows),
            req.batch_size,
            req.dataset,
            embedders,
            allow_widen,
            widened,
            progress,
        )
    except BaseException as exc:
        if created:
            with suppress(Exception):
                db.drop_table(load_target)
        if not created and progress["rows"] and isinstance(exc, HFStreamError):
            raise type(exc)(
                f"{exc} ({progress['rows']} rows had already been appended and were kept)"
            ) from exc
        raise

    if load_target != req.target:  # replace: swap the new table in
        with _wrap(DatabaseError):
            db.drop_table(req.target)
            db.rename_table(load_target, req.table)

    result: dict[str, Any] = {
        "status": "ok",
        "table": req.target,
        "mode": req.mode,
        "rows_loaded": total,
        "seconds": round(time.perf_counter() - started, 3),
        "columns": [{"name": c.name, "type": c.sql_type} for c in columns],
    }
    if opened.split_counts is not None:
        result["rows_per_split"] = dict(opened.split_counts)
    if opened.skipped:
        result["skipped_columns"] = opened.skipped
    if widened:
        result["widened_columns"] = _merge_widened(widened)
    return result


def run_info(
    dataset: str,
    split: str = "train",
    config: str | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    token: str | None = None,
    load_kwargs: dict | None = None,
    options: Any = None,
) -> dict:
    """Dry run: sample the dataset and report the columns that would be created,
    plus the split sizes published on the Hub."""
    req = _validate(
        dataset, split, None, sample_size, config, None, sample_size,
        need_target=False, options=options,
    )
    opened = _open(req, token, load_kwargs)
    sample, _, columns, _ = _sample(req, opened)
    result: dict[str, Any] = {
        "status": "ok",
        "dataset": req.dataset,
        "split": req.split,
        "sampled_rows": len(sample),
        "columns": [{"name": c.name, "source": _source(c), "type": c.sql_type} for c in columns],
    }
    if opened.skipped:
        result["skipped_columns"] = opened.skipped
    if load_kwargs is None:
        result.update(split_info(req.dataset, req.config, token))
    return result


def _source(col: Column) -> str:
    source = str(col.source)
    return source[len(_EMBED_PREFIX):] if source.startswith(_EMBED_PREFIX) else source


def run_search(term: str, limit: int = 10, token: str | None = None) -> dict:
    term = str(term or "").strip()
    if not term or len(term) > 200:
        raise ValidationError("search term must be between 1 and 200 characters")
    limit = _to_int(limit, "limit", 10, 1, 100)
    with _wrap(DownloadError):
        results = search(term, limit, token)
    return {"status": "ok", "term": term, "results": results}


def _error_json(code: str, message: str) -> str:
    return json.dumps({"status": "error", "code": code, "message": message}, ensure_ascii=False)


def _as_json(fn, *args, **kwargs) -> str:
    """Run an entry point and always return JSON (the ObjectScript caller expects it)."""
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except HFStreamError as exc:
        return _error_json(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 - last resort, the caller expects JSON
        log.exception("hfstream: unexpected error")
        return _error_json("INTERNAL", f"{type(exc).__name__}: {exc}")


def _token() -> str | None:
    return os.environ.get("HF_TOKEN") or None


def load_to_iris(
    dataset: str,
    split: str = "train",
    target: str = "",
    limit: int = 0,
    config: str = "",
    batch_size: int = DEFAULT_BATCH_SIZE,
    options: str = "",
) -> str:
    """Entry point called from ObjectScript. Always returns a JSON string, never raises."""
    return _as_json(
        run_load, dataset, split, target, limit, config, batch_size,
        token=_token(), options=options,
    )


def dataset_info(
    dataset: str,
    split: str = "train",
    config: str = "",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    options: str = "",
) -> str:
    """Entry point for ``HF.Loader.Info``. Always returns a JSON string, never raises."""
    return _as_json(run_info, dataset, split, config, sample_size, token=_token(), options=options)


def search_datasets(term: str, limit: int = 10) -> str:
    """Entry point for ``HF.Loader.Search``. Always returns a JSON string, never raises."""
    return _as_json(run_search, term, limit, token=_token())


def embed_text(text: str, model: str = "") -> str:
    """Vector literal '[x,y,...]' for ``text`` (for queries with TO_VECTOR).

    Raises on errors, so a SQL query using it fails loudly instead of matching nothing.
    """
    vector = embedding.encode(embedding.get_model(model or None), [text])[0]
    return vector or ""
