"""Pick a writable cache folder for Hugging Face downloads.

IRIS often runs as a Windows service, or as an account whose home folder cannot be
written, and the Hugging Face libraries cache downloads (dataset metadata, embedding
models) under ``~/.cache/huggingface`` by default. This module must run before
``datasets`` or ``huggingface_hub`` is imported: they read the location once.

Found on a real IRIS 2026.2 on Windows with the earlier hf-iris-loader prototype.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import MutableMapping

FALLBACK_NAME = "hfstream_cache"


def _writable(folder: str) -> bool:
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, f".hfstream_probe_{uuid.uuid4().hex}")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def ensure_writable_cache(environ: MutableMapping[str, str] | None = None) -> str | None:
    """Set ``HF_HOME`` to a writable folder when the default one is not writable.

    ``HF_HOME`` / ``HF_DATASETS_CACHE`` set by the user are always respected.
    Returns the folder chosen, or None when nothing had to change.
    """
    env = os.environ if environ is None else environ
    if env.get("HF_HOME") or env.get("HF_DATASETS_CACHE"):
        return None
    default = os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    if _writable(default):
        return None
    fallback = os.path.join(tempfile.gettempdir(), FALLBACK_NAME)
    os.makedirs(fallback, exist_ok=True)
    env["HF_HOME"] = fallback
    return fallback
