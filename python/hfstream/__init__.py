"""iris-hf-stream: stream a Hugging Face dataset into an InterSystems IRIS table."""

from .cache import ensure_writable_cache

ensure_writable_cache()  # before anything imports datasets / huggingface_hub

from .loader import (  # noqa: E402
    dataset_info,
    embed_text,
    load_to_iris,
    run_info,
    run_load,
    run_search,
    search_datasets,
)

__version__ = "0.2.0"
__all__ = [
    "load_to_iris",
    "dataset_info",
    "search_datasets",
    "embed_text",
    "run_load",
    "run_info",
    "run_search",
    "__version__",
]
