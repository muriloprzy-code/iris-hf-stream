import datetime as dt
import json

import pytest

from hfstream.errors import SchemaMismatchError, ValueTooLongError
from hfstream.schema import (
    create_table_sql,
    encode_row,
    encode_value,
    infer_schema,
    insert_sql,
    sanitize_identifier,
)


def types(rows):
    return {c.name: c.sql_type for c in infer_schema(rows)}


def col(rows, name):
    return next(c for c in infer_schema(rows) if c.name == name)


def test_basic_types():
    rows = [
        {
            "i": 1,
            "f": 1.5,
            "b": True,
            "s": "hello",
            "d": dt.date(2024, 1, 2),
            "t": dt.datetime(2024, 1, 2, 3, 4, 5),
        }
    ]
    assert types(rows) == {
        "i": "BIGINT",
        "f": "DOUBLE",
        "b": "BIT",
        "s": "VARCHAR(32)",
        "d": "DATE",
        "t": "TIMESTAMP",
    }


def test_int_and_float_mix_is_double():
    assert types([{"x": 1}, {"x": 2.5}]) == {"x": "DOUBLE"}


def test_bool_is_not_treated_as_int():
    assert types([{"x": True}, {"x": False}]) == {"x": "BIT"}
    assert types([{"x": True}, {"x": 1}]) == {"x": "LONGVARCHAR"}


def test_string_sizing_and_long_text():
    assert types([{"s": "a" * 10}]) == {"s": "VARCHAR(64)"}
    assert types([{"s": "a" * 256}]) == {"s": "VARCHAR(1024)"}
    assert types([{"s": "a" * 300}]) == {"s": "LONGVARCHAR"}


def test_lists_and_dicts_are_json_text():
    rows = [{"tags": ["a", "b"], "meta": {"k": 1}}]
    assert types(rows) == {"tags": "LONGVARCHAR", "meta": "LONGVARCHAR"}
    tags = col(rows, "tags")
    assert json.loads(encode_value(tags, ["a", "b"])) == ["a", "b"]


def test_only_nulls_column():
    rows = [{"x": None}, {"x": None}]
    assert types(rows) == {"x": "LONGVARCHAR"}
    assert encode_value(col(rows, "x"), None) is None
    assert encode_value(col(rows, "x"), "later value") == "later value"


def test_column_names_are_sanitized_and_unique():
    used = set()
    names = ["Review Text", "review_text", "1st", "select", "", "id"]
    assert [sanitize_identifier(n, used) for n in names] == [
        "Review_Text",
        "review_text_2",
        "c_1st",
        "select_",
        "col",
        "id",
    ]


def test_nan_and_inf_become_null():
    c = col([{"x": 1.0}], "x")
    assert encode_value(c, float("nan")) is None
    assert encode_value(c, float("inf")) is None
    assert encode_value(c, 2) == 2.0


def test_date_and_datetime_encoding():
    rows = [{"d": dt.date(2024, 1, 2), "t": dt.datetime(2024, 1, 2, 3, 4, 5)}]
    d, t = col(rows, "d"), col(rows, "t")
    assert encode_value(d, dt.date(2024, 1, 2)) == "2024-01-02"
    assert encode_value(t, dt.datetime(2024, 1, 2, 3, 4, 5)) == "2024-01-02 03:04:05.000000"
    aware = dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    assert encode_value(t, aware) == "2024-01-02 06:04:05.000000"


def test_bool_encodes_as_bit():
    b = col([{"b": True}], "b")
    assert (encode_value(b, True), encode_value(b, False)) == (1, 0)


def test_type_mismatch_after_the_sample_is_a_clear_error():
    i = col([{"n": 1}], "n")
    with pytest.raises(SchemaMismatchError, match="column n"):
        encode_value(i, "abc")
    b = col([{"b": True}], "b")
    with pytest.raises(SchemaMismatchError):
        encode_value(b, 1)


def test_value_longer_than_sampled_varchar_is_a_clear_error():
    s = col([{"s": "hello"}], "s")  # VARCHAR(32)
    assert encode_value(s, "x" * 32) == "x" * 32
    with pytest.raises(ValueTooLongError, match="VARCHAR\\(32\\)"):
        encode_value(s, "x" * 33)


def test_missing_keys_become_null():
    columns = infer_schema([{"a": 1, "b": "x"}])
    assert encode_row(columns, {"a": 2}) == (2, None)


def test_sql_generation():
    columns = infer_schema([{"id": 1, "text": "hi"}])
    ddl = create_table_sql("Demo.T", columns)
    assert ddl.startswith("CREATE TABLE Demo.T")
    assert "id BIGINT" in ddl and "text VARCHAR(32)" in ddl
    assert insert_sql("Demo.T", columns) == "INSERT INTO Demo.T (id, text) VALUES (?, ?)"


def test_iris_reserved_words_get_a_suffix():
    # Found loading lhoestq/demo1 into IRIS: a column named "date" broke CREATE TABLE.
    used = set()
    names = ["date", "count", "level", "language", "time", "value", "first"]
    assert [sanitize_identifier(n, used) for n in names] == [
        "date_",
        "count_",
        "level_",
        "language_",
        "time_",
        "value",
        "first_",
    ]
