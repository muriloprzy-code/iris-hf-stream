"""iris-hf-stream: stream a Hugging Face dataset into an InterSystems IRIS table."""

from .loader import dataset_info, load_to_iris, run_info, run_load

__version__ = "0.1.0"
__all__ = ["load_to_iris", "dataset_info", "run_load", "run_info", "__version__"]
