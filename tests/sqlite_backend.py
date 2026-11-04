"""SQLite stand-in for the IRIS backend, so the loader logic runs without IRIS."""

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

    def table_exists(self, schema, table):
        row = self.conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND lower(name)=lower(?)",
            (table,),
        ).fetchone()
        return row is not None

    def execute(self, sql, *params):
        self.conn.execute(sql, params)

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
