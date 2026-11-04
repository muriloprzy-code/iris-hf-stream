"""Column type inference and value encoding.

The first rows of the stream are sampled to decide the SQL type of each column.
Strings get a VARCHAR(n) sized from the longest sampled value (with headroom) or
LONGVARCHAR when the text is long. Lists and dicts are stored as JSON text.
Columns that only hold raw bytes are left out (see ``infer_schema``).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import numbers
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Sequence

from .errors import SchemaMismatchError, ValueTooLongError

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# VARCHAR sizes we are willing to pick. Anything that needs more becomes LONGVARCHAR.
VARCHAR_BUCKETS = (32, 64, 128, 256, 512, 1024)
# Sizes used when a later row does not fit and the column is widened with ALTER TABLE
# (IRIS cannot turn a VARCHAR that holds data into a LONGVARCHAR stream). The last one
# is the longest string IRIS can hold.
WIDEN_BUCKETS = VARCHAR_BUCKETS + (4096, 16384, 65536, 262144, 1048576, 3641144)
# Later rows may be longer than the sampled ones, so leave room.
HEADROOM = 4

# Column names that cannot be used unquoted; they get a trailing "_".
# The second block is what IRIS 2026.1 reports via $SYSTEM.SQL.IsReservedWord().
_RESERVED = frozenset(
    """all and any as between by case check column constraint create cross current
    default delete desc distinct drop else end exists for foreign from full grant
    group having in inner insert into is join key left like not null of on or order
    outer percent primary public references right select set some table then to
    union unique update user using values when where with

    absolute add allocate alter are asc assertion at authorization avg begin bit
    bit_length both cascade cast char char_length character character_length close
    coalesce collate commit connect connection constraints continue convert
    corresponding count current_date current_time current_timestamp current_user
    cursor date deallocate dec decimal declare deferrable deferred describe
    descriptor diagnostics disconnect domain double escape except exception exec
    execute external extract false fetch first float found get global go goto hour
    identity immediate indicator initially input insensitive int integer intersect
    interval isolation language last leading level local lower match max min minute
    module names national natural nchar next no nullif numeric octet_length only
    open option output overlaps pad partial prepare preserve prior privileges
    procedure read real relative restrict revoke role rollback rows schema scroll
    second section session_user smallint space sqlerror sqlstate statistics
    substring sum system_user temporary time timezone_hour timezone_minute top
    trailing transaction trim true upper varchar varying whenever work write""".split()
)


@dataclass(frozen=True)
class Column:
    name: str  # SQL column name (sanitized)
    source: str  # key in the dataset row
    kind: str  # bool | int | float | datetime | date | str | json | mixed | vector
    sql_type: str
    max_len: int | None = None  # only for VARCHAR(n)
    dimension: int | None = None  # only for VECTOR columns


def vector_column(name: str, source: str, dimension: int) -> Column:
    return Column(name, source, "vector", f"VECTOR(FLOAT, {dimension})", dimension=dimension)


def widen(col: Column, value: Any) -> Column | None:
    """A wider version of ``col`` that can hold ``value``, or None if there is none.

    Two cases are handled, the ones that come up when the first rows were not
    representative: a string longer than the VARCHAR(n) picked from the sample, and a
    float arriving in a column of integers.
    """
    if col.kind == "int" and isinstance(value, numbers.Real) and not isinstance(value, bool):
        return replace(col, kind="float", sql_type="DOUBLE")
    if col.max_len is not None and value is not None:
        length = len(_text(value))
        for size in WIDEN_BUCKETS:
            if size >= min(length * HEADROOM, WIDEN_BUCKETS[-1]) and size >= length:
                return replace(col, sql_type=f"VARCHAR({size})", max_len=size)
    return None


def is_reserved(name: str) -> bool:
    return name.lower() in _RESERVED


def sanitize_identifier(name: Any, used: set[str]) -> str:
    """Turn a dataset column name into a safe, unique SQL identifier.

    Accents are removed ("preço" -> "preco"), any other character that is not a
    letter, digit or underscore becomes "_".
    """
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^0-9A-Za-z_]", "_", text).strip("_")[:100] or "col"
    if not base[0].isalpha():
        base = "c_" + base
    if base.lower() in _RESERVED:
        base += "_"
    candidate, n = base, 2
    while candidate.lower() in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def _category(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, numbers.Integral):
        return "int" if INT64_MIN <= int(value) <= INT64_MAX else "bigint"
    if isinstance(value, numbers.Real):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dt.datetime):
        return "datetime"
    if isinstance(value, dt.date):
        return "date"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "binary"
    return "json"  # list, dict, anything else


def _varchar_for(max_len: int) -> tuple[str, int | None]:
    wanted = max_len * HEADROOM
    for bucket in VARCHAR_BUCKETS:
        if wanted <= bucket:
            return f"VARCHAR({bucket})", bucket
    return "LONGVARCHAR", None


def _make_column(name: str, source: str, cats: set[str], max_len: int) -> Column:
    if not cats:
        return Column(name, source, "mixed", "LONGVARCHAR")  # only NULLs seen
    if cats == {"bool"}:
        return Column(name, source, "bool", "BIT")
    if cats == {"int"}:
        return Column(name, source, "int", "BIGINT")
    if cats <= {"int", "float"}:
        return Column(name, source, "float", "DOUBLE")
    if cats == {"datetime"}:
        return Column(name, source, "datetime", "TIMESTAMP")
    if cats == {"date"}:
        return Column(name, source, "date", "DATE")
    if cats == {"str"}:
        sql_type, size = _varchar_for(max_len)
        return Column(name, source, "str", sql_type, size)
    if cats == {"json"}:
        return Column(name, source, "json", "LONGVARCHAR")
    return Column(name, source, "mixed", "LONGVARCHAR")


def infer_schema(rows: Sequence[dict], skipped: list | None = None) -> list[Column]:
    """Infer the columns from a sample of rows (dicts).

    Columns whose sampled values are all raw bytes (images, audio, ...) cannot be
    stored meaningfully and are left out; their names are appended to ``skipped``.
    """
    order: list[Any] = []
    seen: set[Any] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                order.append(key)

    used: set[str] = set()
    columns = []
    for key in order:
        cats: set[str] = set()
        max_len = 0
        for row in rows:
            value = row.get(key)
            if value is None:
                continue
            cat = _category(value)
            cats.add(cat)
            if cat == "str":
                max_len = max(max_len, len(value))
        if cats == {"binary"}:
            if skipped is not None:
                skipped.append(str(key))
            continue
        columns.append(_make_column(sanitize_identifier(key, used), key, cats, max_len))
    return columns


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _mismatch(col: Column, value: Any) -> SchemaMismatchError:
    return SchemaMismatchError(
        f"column {col.name}: expected {col.kind} (inferred from the sample) "
        f"but got {type(value).__name__}: {value!r:.80}"
    )


def encode_value(col: Column, value: Any) -> Any:
    """Convert a Python value into something IRIS accepts for ``col``."""
    if value is None:
        return None
    kind = col.kind
    if kind == "vector":
        return value  # already '[x,y,...]' text, see embed.py
    if kind == "bool":
        if isinstance(value, bool):
            return 1 if value else 0
        raise _mismatch(col, value)
    if kind == "int":
        if (
            isinstance(value, numbers.Integral)
            and not isinstance(value, bool)
            and INT64_MIN <= int(value) <= INT64_MAX
        ):
            return int(value)
        raise _mismatch(col, value)
    if kind == "float":
        if isinstance(value, numbers.Real) and not isinstance(value, bool):
            number = float(value)
            # IRIS has no NaN/Infinity in SQL; store them as NULL.
            return None if math.isnan(number) or math.isinf(number) else number
        raise _mismatch(col, value)
    if kind == "datetime":
        if isinstance(value, dt.datetime):
            if value.tzinfo is not None:
                value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return value.isoformat(sep=" ", timespec="microseconds")
        raise _mismatch(col, value)
    if kind == "date":
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return value.isoformat()
        raise _mismatch(col, value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _mismatch(col, value)
    text = _text(value)  # str, json, mixed
    if col.max_len is not None and len(text) > col.max_len:
        raise ValueTooLongError(
            f"column {col.name}: value with {len(text)} characters does not fit "
            f"{col.sql_type} (sized from the sampled rows). "
            'Increase "sample_size" in the options so the column is typed as LONGVARCHAR.'
        )
    return text


def encode_row(columns: Sequence[Column], row: dict) -> tuple:
    return tuple(encode_value(c, row.get(c.source)) for c in columns)


def create_table_sql(target: str, columns: Sequence[Column]) -> str:
    body = ",\n  ".join(f"{c.name} {c.sql_type}" for c in columns)
    return f"CREATE TABLE {target} (\n  {body}\n)"


def insert_sql(target: str, columns: Sequence[Column], vector_placeholder=None) -> str:
    """INSERT with one marker per column; VECTOR columns use ``vector_placeholder(dim)``."""
    names = ", ".join(c.name for c in columns)
    marks = ", ".join(
        vector_placeholder(c.dimension) if c.kind == "vector" and vector_placeholder else "?"
        for c in columns
    )
    return f"INSERT INTO {target} ({names}) VALUES ({marks})"
