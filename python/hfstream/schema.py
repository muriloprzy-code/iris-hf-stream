"""Column type inference and value encoding.

The first rows of the stream are sampled to decide the SQL type of each column.
Strings get a VARCHAR(n) sized from the longest sampled value (with headroom) or
LONGVARCHAR when the text is long. Lists and dicts are stored as JSON text.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import numbers
import re
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import SchemaMismatchError, ValueTooLongError

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# VARCHAR sizes we are willing to pick. Anything that needs more becomes LONGVARCHAR.
VARCHAR_BUCKETS = (32, 64, 128, 256, 512, 1024)
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
    kind: str  # bool | int | float | datetime | date | str | json | mixed
    sql_type: str
    max_len: int | None = None  # only for VARCHAR(n)


def sanitize_identifier(name: Any, used: set[str]) -> str:
    """Turn a dataset column name into a safe, unique SQL identifier."""
    base = re.sub(r"[^0-9A-Za-z_]", "_", str(name)).strip("_")[:100] or "col"
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
    return "json"  # list, dict, bytes, anything else


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


def infer_schema(rows: Sequence[dict]) -> list[Column]:
    """Infer the columns from a sample of rows (dicts)."""
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

    text = _text(value)  # str, json, mixed
    if col.max_len is not None and len(text) > col.max_len:
        raise ValueTooLongError(
            f"column {col.name}: value with {len(text)} characters does not fit "
            f"{col.sql_type} (sized from the sampled rows). "
            f"Increase the sample size so the column is typed as LONGVARCHAR."
        )
    return text


def encode_row(columns: Sequence[Column], row: dict) -> tuple:
    return tuple(encode_value(c, row.get(c.source)) for c in columns)


def create_table_sql(target: str, columns: Sequence[Column]) -> str:
    body = ",\n  ".join(f"{c.name} {c.sql_type}" for c in columns)
    return f"CREATE TABLE {target} (\n  {body}\n)"


def insert_sql(target: str, columns: Sequence[Column]) -> str:
    names = ", ".join(c.name for c in columns)
    marks = ", ".join("?" for _ in columns)
    return f"INSERT INTO {target} ({names}) VALUES ({marks})"
