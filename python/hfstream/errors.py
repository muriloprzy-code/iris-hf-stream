"""Exceptions raised by hfstream.

Every error carries a short machine-readable ``code`` that ends up in the JSON
returned to the caller (``{"status": "error", "code": ..., "message": ...}``).
"""


class HFStreamError(Exception):
    code = "INTERNAL"


class ValidationError(HFStreamError):
    """Bad input: dataset id, split, target table, numeric limits..."""

    code = "VALIDATION"


class EmptyDatasetError(HFStreamError):
    """The split exists but returned no rows."""

    code = "EMPTY"


class DownloadError(HFStreamError):
    """Hugging Face could not be reached, the dataset/split does not exist, etc."""

    code = "DOWNLOAD"


class DatabaseError(HFStreamError):
    """IRIS rejected a statement (DDL, insert, transaction control)."""

    code = "DATABASE"


class SchemaMismatchError(HFStreamError):
    """A row after the sampled ones does not fit the inferred column type."""

    code = "SCHEMA"


class ValueTooLongError(SchemaMismatchError):
    """A string is longer than the VARCHAR(n) chosen from the sample."""

    code = "VALUE_TOO_LONG"
