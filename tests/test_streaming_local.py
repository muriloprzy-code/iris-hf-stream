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


def test_class_labels_get_a_name_column(tmp_path, db):
    from datasets import ClassLabel, Features, Value

    path = tmp_path / "labels.jsonl"
    path.write_text("".join(json.dumps({"text": f"t{i}", "label": i % 2}) + "\n" for i in range(10)))
    features = Features({"text": Value("string"), "label": ClassLabel(names=["neg", "pos"])})
    result = run_load(
        "json",
        "train",
        "Demo.Labels",
        load_kwargs={"data_files": str(path), "features": features},
        backend=db,
    )
    assert [c["name"] for c in result["columns"]] == ["text", "label", "label_name"]
    assert db.query("SELECT label, label_name FROM Demo.Labels ORDER BY rowid LIMIT 2") == [
        (0, "neg"),
        (1, "pos"),
    ]


def test_image_columns_are_skipped_without_decoding(tmp_path, db):
    # A real Image feature, written the way the Hub stores it (a parquet struct plus
    # the "huggingface" features metadata). Encoding or decoding images would need
    # Pillow, which is not installed in the IRIS image: the loader must drop the
    # column before any row is decoded.
    import pyarrow as pa
    import pyarrow.parquet as pq

    png = bytes([0x89]) + b"PNG" + bytes(16)  # PNG signature start, then padding
    image_type = pa.struct([("bytes", pa.binary()), ("path", pa.string())])
    table = pa.table(
        {
            "image": pa.array([{"bytes": png, "path": None}] * 3, type=image_type),
            "caption": ["a", "b", "c"],
        }
    )
    features = {"image": {"_type": "Image"}, "caption": {"dtype": "string", "_type": "Value"}}
    meta = {b"huggingface": json.dumps({"info": {"features": features}}).encode()}
    path = tmp_path / "pics.parquet"
    pq.write_table(table.replace_schema_metadata(meta), str(path))
    result = run_load(
        "parquet", "train", "Demo.Pics", load_kwargs={"data_files": str(path)}, backend=db
    )
    assert result["skipped_columns"] == ["image"]
    assert [c["name"] for c in result["columns"]] == ["caption"]
    assert db.count("Demo.Pics") == 3
