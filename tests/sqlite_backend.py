"""SQLite stand-in for the IRIS backend, so the loader logic runs without IRIS."""

import re
import sqlite3

from hfstream.backend import Backend


class SqliteBackend(Backend):
    def __init__(self, schemas=("Demo",)):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        for schema in schemas:
            self.conn.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0
        self.altered = []

    def table_exists(self, schema, table):
        row = self.conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND lower(name)=lower(?)",
            (table,),
        ).fetchone()
        return row is not None

    def execute(self, sql, *params):
        # SQLite has no VECTOR type; the vectors are kept as their '[x,y,...]' text.
        sql = re.sub(r"VECTOR\(FLOAT, \d+\)", "TEXT", sql)
        self.conn.execute(sql, params)

    def table_columns(self, schema, table):
        rows = self.conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
        return [r[1].lower() for r in rows]

    def alter_column(self, qualified_name, column, sql_type):
        # SQLite columns are dynamically typed: nothing to change, just record it.
        self.altered.append((qualified_name, column, sql_type))

    def rename_table(self, qualified_name, new_table):
        self.conn.execute(f"ALTER TABLE {qualified_name} RENAME TO {new_table}")

    def vector_placeholder(self, dimension):
        return "?"  # vectors are stored as their '[x,y,...]' text

    def insert_many(self, sql, rows):
        self.conn.executemany(sql, list(rows))

    def begin(self):
        self.begins += 1
        self.conn.execute("BEGIN")

    def commit(self):
        self.commits += 1
        self.conn.execute("COMMIT")

    def rollback(self):
        self.rollbacks += 1
        self.conn.execute("ROLLBACK")

    # helpers for assertions
    def query(self, sql):
        return self.conn.execute(sql).fetchall()

    def count(self, qualified):
        return self.query(f"SELECT COUNT(*) FROM {qualified}")[0][0]
