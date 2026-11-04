"""Integration with the real ``datasets`` library, using a local JSONL file (no network).

Skipped automatically when ``datasets`` is not installed.
"""

import json

import pytest

pytest.importorskip("datasets")

from hfstream.errors import DownloadError  # noqa: E402
from hfstream.loader import run_info, run_load  # noqa: E402


@pytest.fixture
def jsonl(tmp_path):
    path = tmp_path / "data.jsonl"
    with path.open("w") as fh:
        for i in range(5000):
            fh.write(
                json.dumps(
                    {"id": i, "text": "word " * (i % 50 + 1), "tags": ["a", "b"], "score": i / 7}
                )
                + "\n"
            )
    return str(path)


def test_streaming_with_take(jsonl, db):
    result = run_load(
        "json", "train", "Demo.Local", limit=100, load_kwargs={"data_files": jsonl}, backend=db
    )
    assert result["rows_loaded"] == 100
    assert db.count("Demo.Local") == 100
    types = {c["name"]: c["type"] for c in result["columns"]}
    assert types["id"] == "BIGINT" and types["tags"] == "LONGVARCHAR"


def test_unknown_split_is_a_download_error(jsonl, db):
    with pytest.raises(DownloadError):
        run_load(
            "json",
            "validation",
            "Demo.Local",
            limit=10,
            load_kwargs={"data_files": jsonl},
            backend=db,
        )
    assert not db.table_exists("Demo", "Local")


def test_info_on_local_file(jsonl):
    info = run_info("json", "train", sample_size=50, load_kwargs={"data_files": jsonl})
    assert info["sampled_rows"] == 50
    assert [c["name"] for c in info["columns"]] == ["id", "text", "tags", "score"]
