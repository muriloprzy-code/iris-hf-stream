import pytest

from sqlite_backend import SqliteBackend


@pytest.fixture
def db():
    return SqliteBackend()
