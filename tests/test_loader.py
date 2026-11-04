import datetime as dt
import json

import pytest

import hfstream.loader as loader
from hfstream.errors import (
    DatabaseError,
    DownloadError,
    EmptyDatasetError,
    SchemaMismatchError,
    ValidationError,
)
from hfstream.loader import load_to_iris, run_info, run_load
from sqlite_backend import SqliteBackend


def make_rows(n, start=0):
    for i in range(start, start + n):
        yield {"id": i, "text": f"review number {i}", "label": i % 2, "score": i / 3}


@pytest.fixture
def stream(monkeypatch):
    """Replace the Hugging Face source with a generator factory."""
    calls = {}

    def install(factory):
        def fake_open(dataset, split, config=None, token=None, limit=0, load_kwargs=None):
            calls.update(dataset=dataset, split=split, config=config, token=token, limit=limit)
            return factory()

        monkeypatch.setattr(loader, "open_stream", fake_open)
        return calls

    return install


def test_loads_all_rows_in_batches(db, stream):
    stream(lambda: make_rows(2500))
    result = run_load("owner/ds", "train", "Demo.Imdb", backend=db)
    assert result["status"] == "ok"
    assert result["table"] == "Demo.Imdb"
    assert result["rows_loaded"] == 2500
    assert db.count("Demo.Imdb") == 2500
    assert (db.begins, db.commits, db.rollbacks) == (3, 3, 0)  # 1000 + 1000 + 500
    assert {c["name"]: c["type"] for c in result["columns"]} == {
        "id": "BIGINT",
        "text": "VARCHAR(128)",
        "label": "BIGINT",
        "score": "DOUBLE",
    }


def test_limit_is_enforced_even_if_the_source_ignores_it(db, stream):
    calls = stream(lambda: make_rows(5000))
    result = run_load("owner/ds", "train", "Demo.T", limit=1200, backend=db)
    assert result["rows_loaded"] == 1200
    assert db.count("Demo.T") == 1200
    assert db.commits == 2
    assert calls["limit"] == 1200  # passed down so the streaming source can take(limit)


def test_sample_smaller_than_batch(db, stream):
    stream(lambda: make_rows(250))
    result = run_load("owner/ds", "train", "Demo.T", batch_size=100, sample_size=10, backend=db)
    assert result["rows_loaded"] == 250
    assert db.count("Demo.T") == 250
    assert db.commits == 3
    assert [r[0] for r in db.query("SELECT id FROM Demo.T ORDER BY rowid")] == list(range(250))


def test_row_by_row_baseline_commits_every_row(db, stream):
    stream(lambda: make_rows(20))
    run_load("owner/ds", "train", "Demo.T", batch_size=1, backend=db)
    assert db.commits == 20


def test_existing_table_is_refused_and_untouched(db, stream):
    stream(lambda: make_rows(10))
    db.execute("CREATE TABLE Demo.T (id BIGINT)")
    db.execute("INSERT INTO Demo.T VALUES (99)")
    with pytest.raises(ValidationError, match="already exists"):
        run_load("owner/ds", "train", "Demo.T", backend=db)
    assert db.query("SELECT id FROM Demo.T") == [(99,)]


def test_download_failure_midway_drops_the_table(db, stream):
    def broken():
        yield from make_rows(1500)
        raise RuntimeError("connection reset")

    stream(broken)
    with pytest.raises(DownloadError, match="connection reset"):
        run_load("owner/ds", "train", "Demo.T", backend=db)
    assert not db.table_exists("Demo", "T")


def test_value_longer_than_sampled_column_widens_the_column(db, stream):
    def rows():
        yield {"text": "short", "n": 1}
        yield {"text": "short", "n": 2}
        yield {"text": "x" * 500, "n": 2.5}  # longer text, and a float among integers

    stream(rows)
    result = run_load("owner/ds", "train", "Demo.T", sample_size=2, backend=db)
    assert result["rows_loaded"] == 3
    assert result["widened_columns"] == [
        {"name": "text", "from": "VARCHAR(32)", "to": "VARCHAR(4096)"},
        {"name": "n", "from": "BIGINT", "to": "DOUBLE"},
    ]
    assert db.altered == [("Demo.T", "text", "VARCHAR(4096)"), ("Demo.T", "n", "DOUBLE")]
    assert {c["name"]: c["type"] for c in result["columns"]} == {"text": "VARCHAR(4096)", "n": "DOUBLE"}
    assert db.query("SELECT length(text), n FROM Demo.T ORDER BY rowid")[-1] == (500, 2.5)


def test_value_that_cannot_be_widened_drops_the_table(db, stream):
    def rows():
        yield {"n": 1}
        yield {"n": "not a number"}

    stream(rows)
    with pytest.raises(SchemaMismatchError):
        run_load("owner/ds", "train", "Demo.T", sample_size=1, backend=db)
    assert not db.table_exists("Demo", "T")


def test_database_error_rolls_back_the_batch_and_drops_the_table(stream):
    class Flaky(SqliteBackend):
        calls = 0

        def insert_many(self, sql, rows):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("disk full")
            super().insert_many(sql, rows)

    flaky = Flaky()
    stream(lambda: make_rows(2500))
    with pytest.raises(DatabaseError, match="disk full"):
        run_load("owner/ds", "train", "Demo.T", backend=flaky)
    assert (flaky.commits, flaky.rollbacks) == (1, 1)
    assert not flaky.table_exists("Demo", "T")


def test_empty_dataset(db, stream):
    stream(lambda: iter(()))
    with pytest.raises(EmptyDatasetError):
        run_load("owner/ds", "train", "Demo.T", backend=db)
    assert not db.table_exists("Demo", "T")


def test_data_roundtrip(db, stream):
    def rows():
        yield {
            "id": 1,
            "tags": ["a", "b"],
            "meta": {"k": 1},
            "ok": True,
            "day": dt.date(2024, 1, 2),
            "note": None,
            "score": float("nan"),
        }
        yield {"id": 2, "tags": [], "meta": {}, "ok": False, "day": dt.date(2024, 1, 3),
               "note": "hi", "score": 1.5}

    stream(rows)
    run_load("owner/ds", "train", "Demo.T", backend=db)
    first, second = db.query("SELECT id, tags, meta, ok, day, note, score FROM Demo.T ORDER BY id")
    assert first == (1, '["a", "b"]', '{"k": 1}', 1, "2024-01-02", None, None)
    assert second == (2, "[]", "{}", 0, "2024-01-03", "hi", 1.5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target": "Demo"},
        {"target": "Demo.Im-db"},
        {"target": "1Demo.T"},
        {"target": "a.b.c"},
        {"target": ""},
        {"target": "Demo.All"},
        {"target": "User.T"},
        {"dataset": "../etc/passwd"},
        {"dataset": "a b"},
        {"dataset": ""},
        {"split": "train; DROP"},
        {"limit": -1},
        {"limit": "abc"},
        {"batch_size": 0},
    ],
)
def test_validation_errors(db, kwargs):
    args = {"dataset": "owner/ds", "split": "train", "target": "Demo.T", **kwargs}
    with pytest.raises(ValidationError):
        run_load(backend=db, **args)


def test_load_to_iris_returns_json(db, stream, monkeypatch):
    stream(lambda: make_rows(30))
    monkeypatch.setattr(loader, "_default_backend", lambda: db)
    out = json.loads(load_to_iris("owner/ds", "", "Demo.Imdb", "10", "", 1000))
    assert out["status"] == "ok"
    assert out["rows_loaded"] == 10
    assert out["table"] == "Demo.Imdb"
    assert isinstance(out["seconds"], float)


def test_load_to_iris_reports_errors_as_json(db, monkeypatch):
    monkeypatch.setattr(loader, "_default_backend", lambda: db)
    out = json.loads(load_to_iris("owner/ds", "train", "not-a-table"))
    assert out["status"] == "error" and out["code"] == "VALIDATION"


def test_load_to_iris_outside_iris_is_a_clear_error():
    out = json.loads(load_to_iris("owner/ds", "train", "Demo.T"))
    assert out["status"] == "error" and out["code"] == "DATABASE"
    assert "iris" in out["message"]


def test_hf_token_comes_from_the_environment(db, stream, monkeypatch):
    calls = stream(lambda: make_rows(3))
    monkeypatch.setattr(loader, "_default_backend", lambda: db)
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    load_to_iris("owner/ds", "train", "Demo.T")
    assert calls["token"] == "hf_secret"


def test_config_is_forwarded(db, stream):
    calls = stream(lambda: make_rows(3))
    run_load("nyu-mll/glue", "train", "Demo.T", config="sst2", backend=db)
    assert calls["config"] == "sst2"


def test_info_is_a_dry_run(stream):
    stream(lambda: make_rows(50))
    info = run_info("owner/ds", "train", sample_size=20)
    assert info["sampled_rows"] == 20
    assert [c["name"] for c in info["columns"]] == ["id", "text", "label", "score"]
