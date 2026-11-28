"""Regenerate a D1 import file from a loaded SQLite snapshot.

D1 exports each table's rows directly after its definition in catalog order.
A child's rows can come before its parent's CREATE TABLE. SQLite must find the
parent table when it compiles a child insert because `defer_foreign_keys` delays
only the row check. Import then stops with "no such table" before applying the
file. The export loads locally because a plain SQLite connection leaves foreign
key enforcement off. The regenerated file defines every table before writing
rows.
"""

from __future__ import annotations

import dataclasses
import itertools
import pathlib
import sqlite3
import typing

from ._sqlite_schema import has_table, quote_identifier
from .snapshot import SnapshotError, application_tables


def write_import_sql(database: pathlib.Path, destination: pathlib.Path) -> None:
    """Write `database` out as SQL that D1's import accepts. See the module docstring.

    The order is the whole point: the deferred-key pragma, every table, every
    row in an order _insert_plan has made safe, the restores that plan owes, the
    sequence marks, then everything else the schema holds, and last the planner
    statistics followed by the reload that puts them into use. The pragma is kept
    because D1's own export opens with it and it costs nothing, not because
    anything here relies on it; _insert_plan says why nothing may. Sequence
    marks follow the rows because an insert into an AUTOINCREMENT table moves its
    mark, and the source's mark can be higher than its highest surviving row.
    Indexes and triggers follow the rows so the copy neither maintains an index
    row by row nor fires a trigger on data that already reflects it.

    Values are rendered by _literal, which is exact for the storage classes this
    tool accepts - integers, text, blobs and NULL - and refuses the rest.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        with destination.open("w", encoding="utf-8") as out:
            out.write("PRAGMA defer_foreign_keys=TRUE;\n")
            tables = application_tables(connection)
            for table in tables:
                (definition,) = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                out.write(f"{definition};\n")
            plan = _insert_plan(connection, tables)
            # A foreign key whose parent key is unique only through a separate
            # index needs that index before its first child row is written.
            early = _indexes_parent_keys_need(connection, tables)
            for _, definition in early:
                out.write(f"{definition};\n")
            for table in plan.tables:
                _write_rows(
                    connection,
                    table,
                    out,
                    held_back=plan.deferred.get(table, ()),
                )
            for table, columns in plan.deferred.items():
                _write_restores(connection, table, columns, out)
            if has_table(connection, "sqlite_sequence"):
                _write_sequence_marks(connection, tables, out)
            # In the order the export created them, which is the source's own: an
            # export lists them as the source's catalog does. SQLite settles an
            # exact cost tie between indexes of one table by which was created
            # later, so another order can plan a query differently from the
            # source with every row and statistic equal. It is also an order
            # known to work: each object was created in it once already, when
            # the export loaded.
            others = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type != 'table' AND sql IS NOT NULL "
                "AND name NOT GLOB 'sqlite_*' ORDER BY rowid"
            ).fetchall()
            # The early indexes are made again here, in the source's order with
            # the rest: SQLite settles an exact cost tie between two indexes of
            # a table by which was created later.
            for name, _ in early:
                out.write(f"DROP INDEX {quote_identifier(name)};\n")
            for (definition,) in others:
                out.write(f"{definition};\n")
            if has_table(connection, "sqlite_stat1"):
                # sqlite_stat1 is a reserved name that cannot be created, so an
                # ANALYZE brings it into being; a D1 export opens the same way.
                # Analyzing sqlite_schema alone writes no statistics, so this
                # DELETE clears only what the target held before. Left unscoped
                # on purpose: a foreign table's statistics are lost with it,
                # which ANALYZE restores, whereas a lost sequence mark above
                # would hand out an id again.
                out.write("ANALYZE sqlite_schema;\nDELETE FROM sqlite_stat1;\n")
                _write_rows(connection, "sqlite_stat1", out)
                # A connection plans from the statistics it last loaded, and D1
                # was observed to ignore raw writes to the table until this
                # reload. Without it the target compares as an exact copy and
                # plans as though it had never been analyzed.
                out.write("ANALYZE sqlite_master;\n")
    finally:
        connection.close()


def _write_sequence_marks(
    connection: sqlite3.Connection, tables: list[str], out: typing.TextIO
) -> None:
    """The source's AUTOINCREMENT marks, without touching anyone else's.

    Inserting the rows set a mark for every AUTOINCREMENT table among them, and
    the source's marks replace those. Nothing else on the target is cleared or
    overwritten: the empty-target check is a read made before the import, so a
    table someone else creates there meanwhile can hold a mark of its own, and
    a lost mark lets its next insert reuse an id. The comparison then fails on
    that table. The DELETE names only this file's tables; the file cannot run
    against a database already holding one, since its CREATE TABLE fails there
    and D1 applies none of a failed import.

    A mark naming no table in the file - SQLite lets an application write such
    a row - is refused. Whether the target holds a table of that name by the
    time the import runs is unknowable here, and a mark written onto one would
    move the ids it hands out.
    """
    owned = set(tables)
    marks = connection.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
    # Counted, never named: an application wrote these names, so they are
    # values like any other row's.
    orphans = {name for name, _ in marks if name not in owned}
    if orphans:
        raise SnapshotError(
            f"sqlite_sequence.name holds {len(orphans)} name(s) no table has, "
            "which a copy cannot place without risking another table's ids"
        )
    named = ",".join(_literal(table, "sqlite_sequence.name") for table in tables)
    out.write(f"DELETE FROM sqlite_sequence WHERE name IN ({named});\n")
    _write_rows(connection, "sqlite_sequence", out)


@dataclasses.dataclass(frozen=True)
class _ForeignKey:
    """One foreign key, its columns resolved to the names the tables declare."""

    parent: str
    columns: tuple[str, ...]
    # The parent's columns it points at, its primary key when none are named.
    parent_columns: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class _InsertPlan:
    """The order tables' rows are written in, and what is held back to allow it."""

    tables: list[str]
    # table -> the columns written as NULL at insert and restored afterwards.
    deferred: dict[str, list[str]]


def _insert_plan(connection: sqlite3.Connection, tables: list[str]) -> _InsertPlan:
    """An order in which every row's parents already exist when it is written.

    WHY THE FILE CANNOT LEAN ON `defer_foreign_keys`. D1 runs a large import as
    several transactions, and SQLite switches that pragma off at every COMMIT.
    Measured against D1: a handful of child rows ahead of their parent import
    fine under the pragma, and a few thousand fail with a foreign-key error. So
    the file has to be valid one statement at a time.

    Ordering tables parents-first does that for everything but a cycle. A cycle
    is broken at foreign keys that can be written as NULL: for each such key to
    a table still waiting, its nullable columns are written as NULL so their
    table can go first, and restored by UPDATE once every row exists. One NULL
    column is enough to satisfy a composite key, which SQLite checks only when
    every column holds a value. Only a table that this frees completely is
    chosen, so nothing is deferred that did not have to be. A cycle that leaves
    no such table cannot be written in any order and is refused.

    A column is never held back if it cannot be written as NULL and restored:
    one in the primary key, one another foreign key points at (its children
    would be written before it holds their parent key), or one in a table whose
    primary key can itself hold NULL, since a restore is addressed by that key.

    A self-reference orders nothing.

    Read from foreign_key_list and table_info, SQLite's own account of the
    schema, never from the CREATE TABLE text. Table and column names in a
    foreign key are matched as SQLite matches them, without regard to the case
    of ASCII letters (_folded).
    """
    by_name = {_folded(table): table for table in tables}
    columns = {table: _table_columns(connection, table) for table in tables}
    keys = {table: _primary_key(connection, table) for table in tables}

    def resolved(table: str, name: str) -> str:
        return {_folded(column): column for column in columns[table]}.get(
            _folded(name), name
        )

    foreign: dict[str, list[_ForeignKey]] = {}
    for table in tables:
        grouped: dict[int, list[tuple[int, str, str, str | None]]] = {}
        for row in connection.execute(
            f"PRAGMA foreign_key_list({quote_identifier(table)})"
        ):
            grouped.setdefault(int(row[0]), []).append(
                (int(row[1]), str(row[2]), str(row[3]), row[4])
            )
        found: list[_ForeignKey] = []
        for parts in grouped.values():
            parts.sort()
            parent = by_name.get(_folded(parts[0][1]))
            if parent is None:
                continue
            named = [to for _, _, _, to in parts]
            found.append(
                _ForeignKey(
                    parent,
                    tuple(resolved(table, column) for _, _, column, _ in parts),
                    tuple(keys[parent])
                    if any(to is None for to in named)
                    else tuple(resolved(parent, str(to)) for to in named),
                )
            )
        foreign[table] = found

    referenced = {
        (key.parent, column)
        for keys_of in foreign.values()
        for key in keys_of
        for column in key.parent_columns
    }
    holdable: dict[str, set[str]] = {
        table: {
            name
            for name, (not_null, in_key) in columns[table].items()
            if not not_null and not in_key and (table, name) not in referenced
        }
        if _key_holds_no_null(connection, table, keys[table])
        else set()
        for table in tables
    }

    deferred: dict[str, list[str]] = {}
    ordered: list[str] = []
    waiting = set(tables)
    while waiting:

        def blocking(table: str) -> list[_ForeignKey]:
            return [
                key
                for key in foreign[table]
                if key.parent != table and key.parent in waiting
            ]

        ready = sorted(table for table in waiting if not blocking(table))
        if ready:
            ordered.extend(ready)
            waiting.difference_update(ready)
            continue
        freed = next(
            (
                table
                for table in sorted(waiting)
                if all(set(key.columns) & holdable[table] for key in blocking(table))
            ),
            None,
        )
        if freed is None:
            raise SnapshotError(
                "foreign keys form a cycle with no column that can be written as "
                "NULL and restored - a nullable column no foreign key points at, "
                "in a table whose primary key cannot hold NULL: "
                + ", ".join(sorted(waiting))
            )
        held = {
            column
            for key in blocking(freed)
            for column in set(key.columns) & holdable[freed]
        }
        deferred[freed] = sorted({*deferred.get(freed, []), *held})
        ordered.append(freed)
        waiting.discard(freed)

    return _InsertPlan(ordered, deferred)


# SQLite folds case in names for ASCII letters only: `K` and the Kelvin sign
# are different tables to it, and must not be the same one here.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _folded(name: str) -> str:
    return name.translate(_ASCII_LOWER)


def _indexes_parent_keys_need(
    connection: sqlite3.Connection, tables: list[str]
) -> list[tuple[str, str]]:
    """The separate indexes some foreign key's parent key needs, as (name, SQL)
    in the source's order.

    SQLite requires a foreign key's parent key to be its primary key or to
    have a unique index of exactly its columns, under their declared
    collation, when a child row is written, and says "foreign key mismatch"
    when it compiles one that has neither. Which index serves is asked of
    SQLite itself rather than worked out from the schema, one foreign key at
    a time: the tables alone are created in an empty database, with a probe
    table holding that one key, and the probe's INSERT is compiled with each
    of the parent's separate unique indexes in turn. The rule reads only the
    parent's side, so the probe's untyped columns stand in for the child's.
    The first index that compiles is the key's. Every other index still
    follows the rows.
    """
    # No statement cache: the same probe INSERT is compiled again after every
    # change to the indexes, and a cached EXPLAIN would answer for the old ones.
    scratch = sqlite3.connect(":memory:", cached_statements=0)
    try:
        scratch.execute("PRAGMA foreign_keys=ON")
        for (definition,) in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT GLOB 'sqlite_*' AND name NOT GLOB '_cf_*' ORDER BY rowid"
        ):
            scratch.execute(definition)
        candidates = [
            (str(name), str(table), str(definition))
            for name, table, definition in connection.execute(
                "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type = 'index' "
                "AND sql IS NOT NULL AND name NOT GLOB 'sqlite_*' ORDER BY rowid"
            )
            if _is_unique_and_whole(connection, str(name))
        ]
        # Tables and indexes share one namespace in SQLite.
        taken = {
            _folded(str(name))
            for (name,) in connection.execute("SELECT name FROM sqlite_schema")
        }
        probe = next(
            name
            for name in (f"d1_copy_probe_{n}" for n in itertools.count())
            if _folded(name) not in taken
        )

        def compiles(statement: str) -> bool:
            try:
                scratch.execute(statement)
            except sqlite3.OperationalError as exc:
                # Anything else that stops the compile says nothing about a
                # parent key; the key is left as it was.
                return "foreign key mismatch" not in str(exc)
            return True

        needed: set[str] = set()
        for table in tables:
            grouped: dict[int, list[tuple[int, str, str | None]]] = {}
            for row in connection.execute(
                f"PRAGMA foreign_key_list({quote_identifier(table)})"
            ):
                grouped.setdefault(int(row[0]), []).append(
                    (int(row[1]), str(row[2]), row[4])
                )
            for parts in grouped.values():
                parts.sort()
                parent = parts[0][1]
                columns = ",".join(f"c{seq}" for seq, _, _ in parts)
                named = [to for _, _, to in parts]
                pointed = (
                    ""
                    if any(to is None for to in named)
                    else "(" + ",".join(quote_identifier(str(to)) for to in named) + ")"
                )
                scratch.execute(
                    f"CREATE TABLE {quote_identifier(probe)} ({columns}, FOREIGN KEY "
                    f"({columns}) REFERENCES {quote_identifier(parent)}{pointed})"
                )
                insert = f"EXPLAIN INSERT INTO {quote_identifier(probe)} DEFAULT VALUES"
                try:
                    if compiles(insert):
                        continue
                    for name, owner, definition in candidates:
                        if _folded(owner) != _folded(parent):
                            continue
                        try:
                            scratch.execute(definition)
                        except sqlite3.Error:
                            # An index the tables alone cannot carry serves no
                            # key here.
                            continue
                        served = compiles(insert)
                        scratch.execute(f"DROP INDEX {quote_identifier(name)}")
                        if served:
                            needed.add(name)
                            break
                finally:
                    scratch.execute(f"DROP TABLE {quote_identifier(probe)}")
        return [
            (name, definition) for name, _, definition in candidates if name in needed
        ]
    finally:
        scratch.close()


def _is_unique_and_whole(connection: sqlite3.Connection, index: str) -> bool:
    """Whether `index` is UNIQUE and not partial, as a parent key's must be."""
    found = connection.execute(
        "SELECT tbl_name FROM sqlite_schema WHERE type = 'index' AND name = ?", (index,)
    ).fetchone()
    if found is None:
        return False
    for _, name, unique, _, partial in connection.execute(
        f"PRAGMA index_list({quote_identifier(str(found[0]))})"
    ):
        if name == index:
            return bool(unique) and not partial
    return False


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[bool, bool]]:
    """Each column as (declared NOT NULL, part of the primary key)."""
    return {
        str(row[1]): (bool(row[3]), bool(row[5]))
        for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
    }


def _primary_key(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = [
        row
        for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
        if row[5]
    ]
    return [str(row[1]) for row in sorted(rows, key=lambda row: int(row[5]))]


def _key_holds_no_null(
    connection: sqlite3.Connection, table: str, key: list[str]
) -> bool:
    """Whether `table` has a primary key no row can hold NULL in.

    A restore is addressed by that key. SQLite lets a primary key column of an
    ordinary rowid table hold NULL unless it is declared NOT NULL; a STRICT or
    WITHOUT ROWID table does not, and neither does a rowid alias - a lone
    INTEGER PRIMARY KEY, which SQLite gives no index of its own (one declared
    DESC is not an alias, gets an index, and can hold NULL). table_info reports
    none of those as NOT NULL, so each is asked for.
    """
    if not key:
        return False
    info = {
        str(row[1]): (str(row[2]), bool(row[3]))
        for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
    }
    if all(info[column][1] for column in key):
        return True
    key_indexed = any(
        row[3] == "pk"
        for row in connection.execute(f"PRAGMA index_list({quote_identifier(table)})")
    )
    if len(key) == 1 and info[key[0]][0].upper() == "INTEGER" and not key_indexed:
        return True
    # The PRAGMA itself, not the pragma_table_list table-valued function, which
    # an application table of that name would shadow.
    listed = connection.execute(
        f"PRAGMA main.table_list({quote_identifier(table)})"
    ).fetchone()
    return listed is not None and bool(listed[4] or listed[5])


def _literal(value: object, where: str) -> str:
    """`value` as SQL that reads back as exactly that value.

    Rendered here rather than by SQLite's quote(), which stops at the first NUL
    in a string: 'ab<NUL>cd' comes back as 'ab', silently. Text holding any
    control character is written as a cast of its bytes instead, which is exact
    for any text and leaves nothing in the import file for a line-oriented step
    between here and D1's SQLite to reinterpret - a NUL, a carriage return, a
    newline. Everything else is a plain literal, where the only character with
    meaning is the quote. A D1 export can change a REAL's storage class, so
    refusing one here stops the copy before the target is touched.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool) or not isinstance(value, (int, str, bytes)):
        raise SnapshotError(
            f"{where} holds a {type(value).__name__} value, which a dump cannot be "
            "shown to carry exactly"
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return f"CAST(X'{value.encode('utf-8').hex()}' AS TEXT)"
    return "'" + value.replace("'", "''") + "'"


def _write_rows(
    connection: sqlite3.Connection,
    table: str,
    out: typing.TextIO,
    *,
    held_back: typing.Collection[str] = (),
) -> None:
    """One INSERT per row. `held_back` columns are written NULL and restored later."""
    columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
    ]
    names = ",".join(quote_identifier(column) for column in columns)
    prefix = f"INSERT INTO {quote_identifier(table)} ({names}) VALUES("
    for row in connection.execute(f"SELECT {names} FROM {quote_identifier(table)}"):
        rendered = ",".join(
            "NULL" if column in held_back else _literal(value, f"{table}.{column}")
            for column, value in zip(columns, row, strict=True)
        )
        out.write(f"{prefix}{rendered});\n")


def _write_restores(
    connection: sqlite3.Connection, table: str, columns: list[str], out: typing.TextIO
) -> None:
    """UPDATEs that put back what _write_rows held back, addressed by primary key."""
    key = [
        str(row[1])
        for row in sorted(
            (
                row
                for row in connection.execute(
                    f"PRAGMA table_info({quote_identifier(table)})"
                )
                if row[5]
            ),
            key=lambda row: int(row[5]),
        )
    ]
    if not key:
        raise SnapshotError(
            f"{table} needs a column restored after insert and declares no primary key"
        )
    selected = ",".join(quote_identifier(column) for column in key)
    for column in columns:
        target = quote_identifier(column)
        for row in connection.execute(
            f"SELECT {target},{selected} FROM {quote_identifier(table)} WHERE {target} IS NOT NULL"
        ):
            where = " AND ".join(
                f"{quote_identifier(name)} = {_literal(value, f'{table}.{name}')}"
                for name, value in zip(key, row[1:], strict=True)
            )
            out.write(
                f"UPDATE {quote_identifier(table)} SET {target} = "
                f"{_literal(row[0], f'{table}.{column}')} WHERE {where};\n"
            )
