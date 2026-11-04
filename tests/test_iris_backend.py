"""IrisBackend parameter mapping, checked without IRIS (a fake ``iris`` module)."""

import sys
import types

import pytest

from hfstream.backend import IrisBackend


@pytest.fixture
def fake_iris(monkeypatch):
    calls = []

    class Statement:
        def execute(self, *args):
            calls.append(args)

    sql = types.SimpleNamespace(prepare=lambda sql: Statement(), exec=lambda sql, *a: calls.append(a))
    monkeypatch.setitem(sys.modules, "iris", types.SimpleNamespace(sql=sql))
    return calls


def test_none_binds_as_null_and_empty_string_stays_empty(fake_iris):
    # Embedded Python binds None as an object reference ('10@%SYS.Python'), not NULL.
    # In IRIS "" is NULL and $CHAR(0) is the empty string.
    IrisBackend().insert_many("INSERT ...", [(None, "", "x", 0, 1.5)])
    assert fake_iris == [("", "\x00", "x", 0, 1.5)]


def test_execute_params_are_mapped_too(fake_iris):
    IrisBackend().execute("SELECT ?", None)
    assert fake_iris == [("",)]
