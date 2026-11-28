"""Load D1 exports into local SQLite for exact comparison.

A D1 export is one server-side-consistent read of a database's primary. Loading
the source's and the target's into local SQLite and comparing there is what lets
a copy say "the target holds exactly what the source held" without trusting a
paged scan of a live database to be a snapshot.

WHAT IS COMPARED, and what deliberately is not:

  - every application table, row for row and value for value;
  - `sqlite_sequence`, because an AUTOINCREMENT high-water mark outlives the row
    that set it: a target that lost one would hand a deleted row's id to the next
    insert;
  - `sqlite_stat1`, which a dump carries and an import restores. Only its rows
    can be compared: whether a database has loaded them is nothing a dump shows;
  - the schema itself, as the statements `sqlite_schema` stores, though not the
    order it stores them in.

Rows are compared as multisets and never by rowid: a table keyed by a BLOB still
has an implicit rowid that a dump does not carry, so an import assigns fresh ones.

NOTHING HERE PRINTS A ROW. A difference reports a table and counts, never
values: the working databases are full copies of whatever the database holds.

WHAT AN EXPORT CANNOT CARRY, AND WHY THAT IS CHECKED ELSEWHERE. Some values
leave an export changed, and the source's export and the target's then carry the
same changed value and agree: the local SQLite, which is exact, faithfully
compares what it was given. So the check is made against the live source, before
it is exported (unexportable_sql below, run by the command line), and a copy of a
source that holds such a value is refused. UNEXPORTABLE says which values and why.
"""

from __future__ import annotations

import collections
import dataclasses
import pathlib
import sqlite3
import typing
import uuid
from typing import override

from ._sqlite_schema import has_table, quote_identifier

# D1 keeps internal state in tables under this prefix. An export omits them and a
# caller may not write them, so they are not part of what a copy owes.
_PLATFORM_PREFIX = "_cf_"

# SQLite's own bookkeeping tables that a dump does carry and a copy does owe.
_SQLITE_TABLES_COMPARED = ("sqlite_sequence", "sqlite_stat1")

_Row = tuple[object, ...]


class SnapshotError(RuntimeError):
    """A dump could not be loaded, or is not a database a copy can vouch for."""


@dataclasses.dataclass(frozen=True)
class Difference:
    """One way the target is not the source. Never carries a row value."""

    subject: str
    detail: str

    @override
    def __str__(self) -> str:
        return f"{self.subject}: {self.detail}"


def load_dump(dump: pathlib.Path, database: pathlib.Path) -> None:
    """Build a local SQLite database from a D1 SQL dump.

    Refuses a dump that defines no table. An export that failed part-way, or a
    file that is not a dump at all, would otherwise load as an empty database and
    compare equal to another empty one.

    AN EMBEDDED NUL. TEXT may hold one, and D1 keeps it and then exports it
    faithfully, as the raw byte inside the string literal. Python refuses to
    execute SQL text containing that byte, so a single such value would stop a
    copy at its first step. Each NUL byte is therefore swapped for a token
    checked to be absent from the dump, and put back by replace() inside SQLite
    once the rows exist. No SQL is parsed to do it: a NUL cannot appear in valid
    SQL outside a string literal, and one that somehow did would surface as a
    schema difference.

    ONLY THIS DATABASE IS WRITTEN. A dump is SQL, and SQL can ATTACH another
    database file and write it. A D1 export never does, so a dump that tries is
    refused (_refuse_other_files) rather than run.
    """
    if database.exists():
        raise SnapshotError(f"refusing to load over an existing file: {database}")
    raw = dump.read_bytes()
    token = _absent_token(raw) if b"\x00" in raw else None
    if token is not None:
        raw = raw.replace(b"\x00", token.encode())
    refused: list[str] = []
    connection = sqlite3.connect(database)
    connection.set_authorizer(_refuse_other_files(refused))
    try:
        # One transaction, and no durability. executescript otherwise commits each
        # statement by itself, and a dump of a database with a couple of hundred
        # thousand rows is that many statements: measured, that alone was over a
        # minute, against a second or two this way. The file is a working copy
        # that is deleted at the end of the run, so nothing here needs to survive
        # a crash. A dump never carries its own BEGIN - D1 forbids one - so the
        # wrapper cannot nest.
        connection.executescript(
            "PRAGMA journal_mode=OFF;\nPRAGMA synchronous=OFF;\nBEGIN;\n"
            + raw.decode("utf-8")
            + "\nCOMMIT;\n"
        )
        if not application_tables(connection):
            raise SnapshotError(f"{dump.name} defines no application table")
        if token is not None:
            _restore_nuls(connection, token)
    except (sqlite3.Error, UnicodeDecodeError) as exc:
        if refused:
            raise SnapshotError(
                f"{dump.name} tries to {refused[0]} another database file, which "
                "a D1 export never does; it was not run"
            ) from exc
        raise SnapshotError(f"{dump.name} did not load as SQL: {exc}") from exc
    finally:
        connection.close()


def _refuse_other_files(
    refused: list[str],
) -> typing.Callable[[int, str | None, str | None, str | None, str | None], int]:
    """A SQLite authorizer that denies reaching any database file but this one.

    VACUUM INTO, the other statement that writes another file, asks for
    SQLITE_ATTACH too, though inside load_dump's transaction it cannot run
    anyway. What was denied goes into `refused`, so the refusal can say why
    instead of SQLite's bare "not authorized".
    """

    def authorize(
        action: int,
        _first: str | None,
        _second: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_ATTACH:
            refused.append("ATTACH")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_DETACH:
            refused.append("DETACH")
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def _absent_token(raw: bytes) -> str:
    """A marker that does not occur in `raw`, so replacing it back is exact."""
    while True:
        token = "NUL" + uuid.uuid4().hex
        if token.encode() not in raw:
            return token


def _restore_nuls(connection: sqlite3.Connection, token: str) -> None:
    for table in application_tables(connection):
        columns = [
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({quote_identifier(table)})"
            )
        ]
        for column in columns:
            name = quote_identifier(column)
            connection.execute(
                f"UPDATE {quote_identifier(table)} SET {name} = replace({name}, ?, char(0)) "
                f"WHERE typeof({name}) = 'text' AND instr({name}, ?) > 0",
                (token, token),
            )
    connection.commit()


def application_tables(connection: sqlite3.Connection) -> list[str]:
    """Every table a copy owes, excluding SQLite's and D1's own."""
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [
        name
        for (name,) in rows
        if not name.startswith("sqlite_") and not name.startswith(_PLATFORM_PREFIX)
    ]


def _schema(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    # Auto-indexes are named sqlite_autoindex_* and store no SQL. They follow
    # from the table definitions compared here, so they are left out rather than
    # compared as a second copy of the same fact.
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
    ).fetchall()
    return [
        (kind, name, table, sql)
        for kind, name, table, sql in rows
        if not name.startswith(_PLATFORM_PREFIX)
    ]


def _rows(connection: sqlite3.Connection, table: str) -> collections.Counter[_Row]:
    quoted = table.replace('"', '""')
    cursor = connection.execute(f'SELECT * FROM "{quoted}"')
    return collections.Counter(tuple(row) for row in cursor)


def _real_valued_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    """Columns of `table` that hold at least one REAL value.

    A dump writes a REAL as decimal text, and nothing here checks that it reads
    back as the same double (UNEXPORTABLE says what was measured). A source's
    dump and its copy's would agree with each other, so the comparison below
    could not see such a loss; refusing the value is what keeps "exact" true.
    SQLite does not stop one arriving in a column of another declared type, so
    every column is asked.
    """
    quoted = table.replace('"', '""')
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{quoted}")')]
    found: list[str] = []
    for column in columns:
        name = column.replace('"', '""')
        hit = connection.execute(
            f'SELECT 1 FROM "{quoted}" WHERE typeof("{name}") = \'real\' LIMIT 1'
        ).fetchone()
        if hit is not None:
            found.append(column)
    return found


def _health(connection: sqlite3.Connection, side: str) -> list[Difference]:
    problems: list[Difference] = []
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        problems.append(
            Difference(side, f"integrity_check reported {len(integrity)} problem(s)")
        )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = sorted({str(row[0]) for row in violations})
        problems.append(
            Difference(
                side,
                f"{len(violations)} foreign-key violation(s) in {', '.join(tables)}",
            )
        )
    return problems


def compare(source: pathlib.Path, target: pathlib.Path) -> list[Difference]:
    """Every way `target` is not an exact copy of `source`. Empty means it is."""
    source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_db = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        return _compare(source_db, target_db)
    finally:
        source_db.close()
        target_db.close()


def _compare(
    source: sqlite3.Connection, target: sqlite3.Connection
) -> list[Difference]:
    differences = [*_health(source, "source"), *_health(target, "target")]

    source_schema, target_schema = _schema(source), _schema(target)
    if source_schema != target_schema:
        only_source = sorted(set(source_schema) - set(target_schema))
        only_target = sorted(set(target_schema) - set(source_schema))
        differences.append(
            Difference(
                "schema",
                f"{len(only_source)} object(s) differ or exist only in the source "
                f"[{', '.join(name for _, name, _, _ in only_source)}]; "
                f"{len(only_target)} only in the target "
                f"[{', '.join(name for _, name, _, _ in only_target)}]",
            )
        )

    tables = sorted({*application_tables(source), *application_tables(target)})
    for table in [*tables, *_SQLITE_TABLES_COMPARED]:
        in_source, in_target = has_table(source, table), has_table(target, table)
        if not in_source and not in_target:
            continue
        if in_source != in_target:
            where = "source" if in_source else "target"
            differences.append(Difference(table, f"exists only in the {where}"))
            continue
        for side, connection in (("source", source), ("target", target)):
            for column in _real_valued_columns(connection, table):
                differences.append(
                    Difference(
                        table,
                        f"{side} column {column} holds a REAL value, which a dump "
                        "cannot be shown to carry exactly",
                    )
                )
        source_rows, target_rows = _rows(source, table), _rows(target, table)
        missing = sum((source_rows - target_rows).values())
        extra = sum((target_rows - source_rows).values())
        if missing or extra:
            differences.append(
                Difference(
                    table,
                    f"{missing} row(s) of {sum(source_rows.values())} in the source "
                    f"are absent or differ in the target; {extra} row(s) of "
                    f"{sum(target_rows.values())} in the target match nothing in "
                    "the source",
                )
            )

    return differences


# The largest magnitude a double holds every integer up to. D1's export passes
# integers through one, so anything beyond this can leave a database changed.
EXPORT_SAFE_INTEGER = 2**53


# Each kind of value an export does not carry exactly, by the letter its probe
# column is named with in unexportable_sql. Both were measured against remote
# D1's export.
#
#   i - D1 holds a 64-bit integer exactly and exports it through a double:
#       2^53 + 1 leaves as 2^53, still written as an integer, and the largest
#       int64 leaves as 9223372036854776000, which no INTEGER can hold.
#   r - an export writes a REAL as the decimal text JavaScript prints for it. A
#       whole number loses its decimal point, so 1.0 reads back into an ANY or
#       untyped column as the INTEGER 1. Other values read back as the same
#       double in every case measured, but that rests on SQLite's decimal
#       parsing, which nothing here checks, so every REAL is refused.
#
# Text is not among them. Remote D1 writes a line break inside text as an escape
# the value does not already hold (observed: `\n`, else `\012`, else `(\n0)`,
# `(\n1)`, ...), so the replace() it wraps the literal in turns back only that
# escape. Miniflare's local export uses a fixed `\n` instead, so a local export
# does change a literal backslash-n that sits beside a newline.
UNEXPORTABLE = {
    "i": "an integer beyond 2^53",
    "r": "a REAL value",
}


def unexportable_sql(table: str, columns: list[str]) -> str:
    """SQL answering, per column of `table`, whether it holds a value an export
    would change. See UNEXPORTABLE.

    One scan of the table, whatever the number of columns, so the check reads
    each row once. The answer is one row whose column `<letter><index>` is 1
    where column `index` holds such a value. Every column is tested, whatever
    type it declares, because only a STRICT table keeps a value of one type out
    of a column of another. An integer is compared on each side rather than with
    abs(), which raises on the most negative int64 instead of answering.
    """
    probes: list[str] = []
    for index, column in enumerate(columns):
        name = quote_identifier(column)
        beyond = f"{name} > {EXPORT_SAFE_INTEGER} OR {name} < -{EXPORT_SAFE_INTEGER}"
        probes += [
            f"max(typeof({name}) = 'integer' AND ({beyond})) AS \"i{index}\"",
            f"max(typeof({name}) = 'real') AS \"r{index}\"",
        ]
    return f"SELECT {', '.join(probes)} FROM {quote_identifier(table)}"


def unexportable_values(
    table: str, columns: list[str], answer: typing.Mapping[str, object]
) -> list[str]:
    """What unexportable_sql's `answer` found, as `table.column holds ...`. Names
    the column and the kind of value, never the value. An answer missing one of
    the expected columns is refused, so a partial answer never reads as clean."""
    expected = {
        f"{letter}{index}" for letter in UNEXPORTABLE for index in range(len(columns))
    }
    if not expected <= answer.keys():
        raise SnapshotError(f"the check of {table} came back without every column")
    return [
        f"{table}.{column} holds {reason}"
        for index, column in enumerate(columns)
        for letter, reason in UNEXPORTABLE.items()
        if answer.get(f"{letter}{index}") == 1
    ]


def values_an_export_would_change(database: pathlib.Path) -> list[str]:
    """unexportable_values for every table of a LOADED database.

    The same question unexportable_sql asks of a live database, asked of a local
    one. It is how a dump file is judged, since a file has no live origin to
    ask: a dump holding such a value can be imported exactly, but the target is
    then read back through an export, which would change it, so the copy could
    never be confirmed.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        found: list[str] = []
        tables = [*application_tables(connection)]
        tables += [t for t in _SQLITE_TABLES_COMPARED if has_table(connection, t)]
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({quote_identifier(table)})"
                )
            ]
            answer = connection.execute(unexportable_sql(table, columns)).fetchone()
            found += unexportable_values(table, columns, dict(answer))
        return found
    finally:
        connection.close()


def row_total(database: pathlib.Path) -> int:
    """Rows across every compared table. A size figure for the operator's log."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        total = 0
        for table in application_tables(connection):
            quoted = table.replace('"', '""')
            total += connection.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[
                0
            ]
        return total
    finally:
        connection.close()
