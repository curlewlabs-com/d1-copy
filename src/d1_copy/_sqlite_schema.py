"""Shared access to identifiers in the SQLite catalog."""

from __future__ import annotations

import sqlite3


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    found = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return found is not None


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
