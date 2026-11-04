"""Feature handling (media columns, class labels) and error messages, without the network."""

import pytest

import hfstream.loader as loader
from hfstream.loader import run_info, run_load
from hfstream.source import explain, prepare


class ClassLabel:  # same class names as in `datasets`; prepare() looks at the type name
    def __init__(self, names):
        self.names = names


class Image:
    pass


class Value:
    def __init__(self, dtype):
        self.dtype = dtype


class FakeStream:
    """Mimics the parts of datasets.IterableDataset that prepare() uses."""

    def __init__(self, rows, features):
        self.rows, self.features = rows, features
        self.decoded = True

    def decode(self, enable=True):
        self.decoded = enable
        return self

    def remove_columns(self, names):
        assert not self.decoded, "media must not be decoded before it is dropped"
        rows = [{k: v for k, v in r.items() if k not in names} for r in self.rows]
        features = {k: v for k, v in self.features.items() if k not in names}
        return FakeStream(rows, features)

    def __iter__(self):
        return iter(self.rows)


MNIST_LIKE = {"image": Image(), "raw": Value("binary"), "label": ClassLabel(["zero", "one"])}


def mnist_rows():
    return [
        {"image": {"bytes": b"\x89PNG"}, "raw": b"\x00", "label": 1},
        {"image": {"bytes": b"\x89PNG"}, "raw": b"\x01", "label": -1},  # -1 = no label
    ]


def test_prepare_drops_media_and_names_labels():
    prepared = prepare(FakeStream(mnist_rows(), MNIST_LIKE))
    assert prepared.skipped == ["image", "raw"]
    assert prepared.labels == {"label": "label_name"}
    assert list(prepared.rows) == [
        {"label": 1, "label_name": "one"},
        {"label": -1, "label_name": None},
    ]


def test_label_name_does_not_overwrite_an_existing_column():
    features = {"label": ClassLabel(["a", "b"]), "label_name": Value("string")}
    rows = [{"label": 0, "label_name": "original"}]
    prepared = prepare(FakeStream(rows, features))
    assert prepared.labels == {"label": "label_name_"}
    assert list(prepared.rows) == [{"label": 0, "label_name": "original", "label_name_": "a"}]


def test_prepare_is_a_no_op_without_features():
    rows = [{"x": 1}]
    assert prepare(iter(rows)).skipped == []


@pytest.fixture
def fake_hub(monkeypatch):
    def install(stream):
        monkeypatch.setattr(loader, "open_stream", lambda *a, **k: stream)

    return install


def test_load_reports_skipped_columns_and_adds_label_names(db, fake_hub):
    fake_hub(FakeStream(mnist_rows(), MNIST_LIKE))
    result = run_load("ylecun/mnist", "train", "Demo.Mnist", backend=db)
    assert result["skipped_columns"] == ["image", "raw"]
    assert [c["name"] for c in result["columns"]] == ["label", "label_name"]
    assert db.query("SELECT label, label_name FROM Demo.Mnist") == [(1, "one"), (-1, None)]


def test_info_reports_skipped_columns(fake_hub):
    fake_hub(FakeStream(mnist_rows(), MNIST_LIKE))
    info = run_info("ylecun/mnist", "train")
    assert info["skipped_columns"] == ["image", "raw"]


def test_dataset_with_only_media_is_a_clear_error(db, fake_hub):
    fake_hub(FakeStream([{"image": {"bytes": b"x"}}], {"image": Image()}))
    with pytest.raises(loader.EmptyDatasetError, match="no column that can be stored"):
        run_load("owner/pics", "train", "Demo.Pics", backend=db)
    assert not db.table_exists("Demo", "Pics")


# Real messages from datasets 5.1.0 / huggingface_hub, captured against the Hub.
@pytest.mark.parametrize(
    "message, expected",
    [
        (
            "Invalid HF URI 'hf://datasets/imdb@e62/.huggingface.yaml'. "
            "Repository id must be 'namespace/name', got 'imdb'.",
            "needs its full id in the form owner/name",
        ),
        (
            "Dataset 'bigcode/the-stack' is a gated dataset on the Hub. "
            "You must be authenticated to access it.",
            "is gated: accept its terms at https://huggingface.co/datasets/bigcode/the-stack",
        ),
        (
            "Config name is missing. Please pick one among the available configs: "
            "['ax', 'cola', 'sst2'] Example of usage: `load_dataset('nyu-mll/glue', 'ax')`",
            "pass one as the config argument. Available: ax, cola, sst2.",
        ),
        (
            "Dataset 'no-such/ds' doesn't exist on the Hub or cannot be accessed.",
            "was not found on the Hugging Face Hub",
        ),
        (
            "Bad split: nope. Available splits: ['train', 'test', 'unsupervised']",
            "Available splits: train, test, unsupervised.",
        ),
    ],
)
def test_hub_errors_are_explained(message, expected):
    assert expected in explain(ValueError(message), "bigcode/the-stack" if "gated" in message else "x")


def test_long_config_lists_are_shortened():
    configs = ", ".join(f"'c{i}'" for i in range(40))
    text = explain(ValueError(f"Config name is missing. available configs: [{configs}]"), "x")
    assert "c14 ... (40 in total)" in text and "c15" not in text


def test_unknown_errors_keep_their_type_and_text():
    assert explain(RuntimeError("connection reset"), "x") == "RuntimeError: connection reset"


def test_hint_reaches_the_json_result(db, monkeypatch):
    def gated(*args, **kwargs):
        raise ValueError("Dataset 'org/secret' is a gated dataset on the Hub.")

    monkeypatch.setattr(loader, "open_stream", gated)
    with pytest.raises(loader.DownloadError, match="HF_TOKEN"):
        run_load("org/secret", "train", "Demo.T", backend=db)
