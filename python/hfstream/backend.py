"""Database access.

``Backend`` is the tiny surface the loader needs. ``IrisBackend`` implements it
with InterSystems Embedded Python (``import iris``); the tests use a SQLite
implementation, so the loader logic can be exercised without an IRIS instance.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .errors import DatabaseError


class Backend:
    def table_exists(self, schema: str, table: str) -> bool:
        raise NotImplementedError

    def execute(self, sql: str, *params) -> None:
        raise NotImplementedError

    def insert_many(self, sql: str, rows: Iterable[Sequence]) -> None:
        raise NotImplementedError

    def begin(self) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    def drop_table(self, qualified_name: str) -> None:
        self.execute(f"DROP TABLE {qualified_name}")

    def table_columns(self, schema: str, table: str) -> list[str]:
        """Column names of an existing table, in lower case."""
        raise NotImplementedError

    def alter_column(self, qualified_name: str, column: str, sql_type: str) -> None:
        self.execute(f"ALTER TABLE {qualified_name} ALTER COLUMN {column} {sql_type}")

    def rename_table(self, qualified_name: str, new_table: str) -> None:
        """Rename ``schema.table`` to ``schema.new_table`` (same schema)."""
        self.execute(f"ALTER TABLE {qualified_name} RENAME {new_table}")

    def vector_placeholder(self, dimension: int) -> str:
        """Parameter marker for a VECTOR column; the value is bound as '[x,y,...]' text."""
        return f"TO_VECTOR(?, FLOAT, {dimension})"


class IrisBackend(Backend):
    """IRIS through Embedded Python (``iris.sql``)."""

    def __init__(self) -> None:
        import iris  # only available inside IRIS (embedded Python / irispython)

        self._iris = iris

    @staticmethod
    def _check(result) -> None:
        """Raise if the statement result reports a negative SQLCODE."""
        try:
            code = getattr(result, "_SQLCODE", 0)
        except Exception:
            return
        if isinstance(code, int) and code < 0:
            message = getattr(result, "_Message", "") or "SQL error"
            raise DatabaseError(f"SQLCODE {code}: {message}")

    @staticmethod
    def _param(value):
        """Map a Python value to what IRIS binds correctly.

        Embedded Python does not turn ``None`` into SQL NULL: it binds an object
        reference, which fails validation on numeric/BIT/stream columns and is
        silently stored as text like ``'10@%SYS.Python'`` in VARCHAR columns.
        IRIS follows the ObjectScript convention instead: ``""`` is NULL and
        ``$CHAR(0)`` is the empty string.
        """
        if value is None:
            return ""
        if value == "" and isinstance(value, str):
            return "\x00"
        return value

    def table_exists(self, schema: str, table: str) -> bool:
        rs = self._iris.sql.exec(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE UPPER(TABLE_SCHEMA) = UPPER(?) AND UPPER(TABLE_NAME) = UPPER(?)",
            schema,
            table,
        )
        for _ in rs:
            return True
        return False

    def table_columns(self, schema: str, table: str) -> list[str]:
        rs = self._iris.sql.exec(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE UPPER(TABLE_SCHEMA) = UPPER(?) AND UPPER(TABLE_NAME) = UPPER(?) "
            "ORDER BY ORDINAL_POSITION",
            schema,
            table,
        )
        return [str(row[0]).lower() for row in rs]

    def execute(self, sql: str, *params) -> None:
        self._check(self._iris.sql.exec(sql, *map(self._param, params)))

    def insert_many(self, sql: str, rows: Iterable[Sequence]) -> None:
        # Note: iris.sql raises irisbuiltins.SQLError on failure (verified on
        # IRIS 2026.1); _check is kept only as a fallback for older versions.
        prepare = getattr(self._iris.sql, "prepare", None)
        if prepare is not None:
            statement = prepare(sql)  # prepared once, executed per row
            for row in rows:
                self._check(statement.execute(*map(self._param, row)))
        else:
            for row in rows:
                self._check(self._iris.sql.exec(sql, *map(self._param, row)))

    def begin(self) -> None:
        self._iris.sql.exec("START TRANSACTION")

    def commit(self) -> None:
        self._iris.sql.exec("COMMIT")

    def rollback(self) -> None:
        self._iris.sql.exec("ROLLBACK")
