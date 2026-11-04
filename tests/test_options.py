"""Load options (mode, columns, embed, sample_size), split="all", Info metadata, Search."""

import json

import pytest

import hfstream.embed as embedding
import hfstream.loader as loader
from hfstream.errors import DatabaseError, DownloadError, ValidationError
from hfstream.loader import load_to_iris, parse_options, run_info, run_load, run_search
from sqlite_backend import SqliteBackend


def rows_for(split, n, start=0):
    return [{"id": i, "text": f"{split} text {i}", "label": i % 2} for i in range(start, start + n)]


@pytest.fixture
def hub(monkeypatch):
    """Fake Hub: {split: rows}; records every open_stream call."""
    calls = []

    def install(data):
        def fake_open(dataset, split, config=None, token=None, limit=0, load_kwargs=None):
            calls.append({"split": split, "limit": limit})
            if split not in data:
                raise ValueError(f"Bad split: {split}. Available splits: {list(data)}")
            rows = data[split]
            return iter(rows[:limit] if limit else rows)

        monkeypatch.setattr(loader, "open_stream", fake_open)
        monkeypatch.setattr(loader, "split_names", lambda *a, **k: list(data))
        return calls

    return install


# ---------------------------------------------------------------- options parsing


@pytest.mark.parametrize(
    "options",
    [
        "not json",
        "[1, 2]",
        '{"mode": "overwrite"}',
        '{"colums": ["x"]}',
        '{"columns": {"a": 1}}',
        '{"columns": [1]}',
        '{"embed_model": "a b"}',
    ],
)
def test_bad_options_are_validation_errors(options):
    with pytest.raises(ValidationError):
        parse_options(options)


def test_options_accept_json_text_dict_and_comma_lists():
    assert parse_options("") == {}
    assert parse_options("\x00") == {}  # '' from SQL
    opts = parse_options('{"mode": "APPEND", "columns": "id, text", "embed": ["text"]}')
    assert opts["mode"] == "append"
    assert opts["columns"] == ("id", "text")
    assert opts["embed"] == ("text",)
    assert parse_options({"mode": "replace"})["mode"] == "replace"


def test_embedded_column_must_be_selected(db, hub):
    hub({"train": rows_for("train", 3)})
    with pytest.raises(ValidationError, match="must also be in"):
        run_load("o/d", "train", "Demo.T", backend=db, options={"columns": ["id"], "embed": ["text"]})


# ---------------------------------------------------------------------- modes


def test_replace_swaps_in_a_new_table(db, hub):
    hub({"train": rows_for("train", 5)})
    db.execute("CREATE TABLE Demo.T (old TEXT)")
    db.execute("INSERT INTO Demo.T VALUES ('previous')")
    result = run_load("o/d", "train", "Demo.T", backend=db, options='{"mode": "replace"}')
    assert result["mode"] == "replace" and result["rows_loaded"] == 5
    assert db.count("Demo.T") == 5
    assert db.table_columns("Demo", "T") == ["id", "text", "label"]
    assert not db.table_exists("Demo", "T_hfstream_tmp")


def test_failed_replace_keeps_the_old_table(db, hub):
    def broken():
        yield from rows_for("train", 1500)
        raise RuntimeError("connection reset")

    hub({"train": []})
    loader_open = loader.open_stream
    loader.open_stream = lambda *a, **k: broken()
    try:
        db.execute("CREATE TABLE Demo.T (old TEXT)")
        db.execute("INSERT INTO Demo.T VALUES ('previous')")
        with pytest.raises(DownloadError):
            run_load("o/d", "train", "Demo.T", backend=db, options={"mode": "replace"})
    finally:
        loader.open_stream = loader_open
    assert db.query("SELECT old FROM Demo.T") == [("previous",)]
    assert not db.table_exists("Demo", "T_hfstream_tmp")


def test_replace_on_a_missing_table_just_creates_it(db, hub):
    hub({"train": rows_for("train", 2)})
    run_load("o/d", "train", "Demo.New", backend=db, options={"mode": "replace"})
    assert db.count("Demo.New") == 2


def test_append_adds_rows_to_an_existing_table(db, hub):
    hub({"train": rows_for("train", 3), "test": rows_for("test", 2, start=100)})
    run_load("o/d", "train", "Demo.T", backend=db)
    result = run_load("o/d", "test", "Demo.T", backend=db, options={"mode": "append"})
    assert result["rows_loaded"] == 2
    assert db.count("Demo.T") == 5
    assert db.altered == []  # never widens a table it did not create


def test_append_refuses_columns_the_table_does_not_have(db, hub):
    hub({"train": rows_for("train", 3)})
    db.execute("CREATE TABLE Demo.T (id INTEGER)")
    with pytest.raises(ValidationError, match="has no column"):
        run_load("o/d", "train", "Demo.T", backend=db, options={"mode": "append"})
    # ...unless only the matching columns are loaded
    run_load("o/d", "train", "Demo.T", backend=db, options={"mode": "append", "columns": ["id"]})
    assert db.count("Demo.T") == 3


def test_failed_append_says_how_many_rows_were_kept(hub):
    class Flaky(SqliteBackend):
        calls = 0

        def insert_many(self, sql, rows):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("disk full")
            super().insert_many(sql, rows)

    flaky = Flaky()
    hub({"train": rows_for("train", 25)})
    flaky.execute("CREATE TABLE Demo.T (id INTEGER, text TEXT, label INTEGER)")
    with pytest.raises(DatabaseError, match="10 rows had already been appended"):
        run_load("o/d", "train", "Demo.T", batch_size=10, backend=flaky, options={"mode": "append"})
    assert flaky.count("Demo.T") == 10


def test_fail_mode_message_mentions_the_other_modes(db, hub):
    hub({"train": rows_for("train", 2)})
    db.execute("CREATE TABLE Demo.T (id INTEGER)")
    with pytest.raises(ValidationError, match='"mode": "replace"'):
        run_load("o/d", "train", "Demo.T", backend=db)


# -------------------------------------------------------------------- columns


def test_columns_option_keeps_only_those_columns(db, hub):
    hub({"train": rows_for("train", 4)})
    result = run_load("o/d", "train", "Demo.T", backend=db, options={"columns": ["text"]})
    assert [c["name"] for c in result["columns"]] == ["text"]


def test_unknown_column_is_a_validation_error(db, hub):
    hub({"train": rows_for("train", 4)})
    with pytest.raises(ValidationError, match="unknown column"):
        run_load("o/d", "train", "Demo.T", backend=db, options={"columns": ["nope"]})
    assert not db.table_exists("Demo", "T")


# ----------------------------------------------------------------- split=all


def test_all_splits_are_loaded_with_their_name(db, hub):
    calls = hub({"train": rows_for("train", 3), "test": rows_for("test", 2)})
    result = run_load("o/d", "all", "Demo.T", limit=2, backend=db)
    assert result["rows_per_split"] == {"train": 2, "test": 2}  # limit applies per split
    assert [c["split"] for c in calls] == ["train", "test"]
    assert db.query("SELECT hf_split, COUNT(*) FROM Demo.T GROUP BY hf_split ORDER BY 1") == [
        ("test", 2),
        ("train", 2),
    ]


def test_a_split_really_called_all_is_loaded_as_is(db, hub):
    hub({"all": rows_for("all", 3)})
    result = run_load("o/d", "all", "Demo.T", backend=db)
    assert "rows_per_split" not in result
    assert "hf_split" not in [c["name"] for c in result["columns"]]


# ------------------------------------------------------------------ embeddings


class FakeModel:
    dim = 3

    def __init__(self):
        self.batches = []

    def encode(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t)), 0.5, -1.0] for t in texts]


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(embedding, "get_model", lambda name=None: fake)
    return fake


def test_embed_adds_a_vector_column(db, hub, model):
    data = rows_for("train", 3)
    data[1]["text"] = None  # no text -> no vector
    hub({"train": data})
    result = run_load("o/d", "train", "Demo.T", batch_size=2, backend=db, options={"embed": "text"})
    assert result["columns"][-1] == {"name": "text_embedding", "type": "VECTOR(FLOAT, 3)"}
    stored = db.query("SELECT text_embedding FROM Demo.T ORDER BY rowid")
    assert stored == [("[12,0.5,-1]",), (None,), ("[12,0.5,-1]",)]
    assert model.batches == [["train text 0"], ["train text 2"]]  # one call per batch


def test_embedding_insert_uses_the_backend_vector_marker(model):
    from hfstream.schema import insert_sql, vector_column

    cols = [vector_column("e", "src", 3)]
    assert insert_sql("Demo.T", cols, lambda d: f"TO_VECTOR(?, FLOAT, {d})") == (
        "INSERT INTO Demo.T (e) VALUES (TO_VECTOR(?, FLOAT, 3))"
    )


def test_cannot_embed_an_unknown_column(db, hub, model):
    hub({"train": rows_for("train", 2)})
    with pytest.raises(ValidationError, match="cannot embed"):
        run_load("o/d", "train", "Demo.T", backend=db, options={"embed": ["body"]})


def test_embed_text_returns_a_vector_literal(model):
    assert loader.embed_text("abc") == "[3,0.5,-1]"
    assert loader.embed_text("   ") == ""


# ------------------------------------------------------------- Info and Search


def test_info_includes_split_sizes(hub, monkeypatch):
    hub({"train": rows_for("train", 5)})
    monkeypatch.setattr(loader, "split_info", lambda *a: {"splits": {"train": 5}})
    info = run_info("o/d", "train", sample_size=3)
    assert info["splits"] == {"train": 5}
    assert info["sampled_rows"] == 3


def test_search(monkeypatch):
    found = [{"id": "stanfordnlp/imdb", "downloads": 10, "likes": 1}]
    monkeypatch.setattr(loader, "search", lambda term, limit, token: found)
    assert run_search("imdb", 5) == {"status": "ok", "term": "imdb", "results": found}
    out = json.loads(loader.search_datasets("", 5))
    assert out["status"] == "error" and out["code"] == "VALIDATION"


def test_load_to_iris_passes_options(db, hub, monkeypatch):
    hub({"train": rows_for("train", 3)})
    monkeypatch.setattr(loader, "_default_backend", lambda: db)
    out = json.loads(load_to_iris("o/d", "train", "Demo.T", 0, "", 1000, '{"columns": ["id"]}'))
    assert out["status"] == "ok" and [c["name"] for c in out["columns"]] == ["id"]
