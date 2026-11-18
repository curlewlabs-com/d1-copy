"""Exact local comparison of a source D1 database and a target, from SQL dumps,
and the import file a copy is made from.

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
    can be compared: whether a database has loaded them is nothing a dump shows,
    so the import file ends with the reload that puts them into use
    (write_import_sql);
  - the schema itself, as the statements `sqlite_schema` stores, though not the
    order it stores them in: write_import_sql keeps that, and its test holds it;
  - the rows of LOCK_TABLE are NOT compared, though the table's presence is. See
    LOCK_TABLE.

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

WHY A DUMP IS NEVER IMPORTED AS IT ARRIVED. D1 refuses its own export. An export
writes each table's rows directly after that table's definition, in catalog
order, so a child's rows can come before its parent's CREATE TABLE. D1 enforces
foreign keys, and SQLite has to find the parent TABLE when it compiles an insert
into the child: `defer_foreign_keys` postpones the row check, not that lookup. The
import stops with "no such table", having applied nothing. It loads locally only
because a plain SQLite connection leaves enforcement off. So the import file is
regenerated from the loaded database (write_import_sql) with every table defined
before any row is written.
"""

from __future__ import annotations

import collections
import dataclasses
import itertools
import pathlib
import sqlite3
import typing
import uuid
from typing import override

# D1 keeps internal state in tables under this prefix. An export omits them and a
# caller may not write them, so they are not part of what a copy owes.
_PLATFORM_PREFIX = "_cf_"

# The single-row deploy lock of a database managed by cloudflare-deploy-toolkit
# (https://github.com/curlewlabs-com/cloudflare-deploy-toolkit). Its Worker
# refuses requests while the row reads locked, so a copy that held data and read
# unlocked would be serving before anyone chose to open it: the import writes the
# row locked (write_import_sql). Its rows are therefore not compared, since a
# locked copy of an open source is the intended result. Its presence is: a
# database that has lost the row answers every request as locked. A database
# without the table is copied and compared like any other.
LOCK_TABLE = "_deploy_lock"

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
            for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
        ]
        for column in columns:
            name = _quoted(column)
            connection.execute(
                f"UPDATE {_quoted(table)} SET {name} = replace({name}, ?, char(0)) "
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


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    found = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return found is not None


def has_lock_table(database: pathlib.Path) -> bool:
    """Whether a LOADED database carries LOCK_TABLE."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return _has_table(connection, LOCK_TABLE)
    finally:
        connection.close()


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
    found = []
    for column in columns:
        name = column.replace('"', '""')
        hit = connection.execute(
            f'SELECT 1 FROM "{quoted}" WHERE typeof("{name}") = \'real\' LIMIT 1'
        ).fetchone()
        if hit is not None:
            found.append(column)
    return found


def _health(connection: sqlite3.Connection, side: str) -> list[Difference]:
    problems = []
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

    tables = sorted(
        {*application_tables(source), *application_tables(target)} - {LOCK_TABLE}
    )
    for table in [*tables, *_SQLITE_TABLES_COMPARED]:
        in_source, in_target = _has_table(source, table), _has_table(target, table)
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

    sides = (("source", source), ("target", target))
    if any(_has_table(connection, LOCK_TABLE) for _, connection in sides):
        for side, connection in sides:
            if not _has_table(connection, LOCK_TABLE):
                differences.append(Difference(LOCK_TABLE, f"missing from the {side}"))
                continue
            held = connection.execute(f"SELECT count(*) FROM {LOCK_TABLE}").fetchone()[
                0
            ]
            if held != 1:
                differences.append(
                    Difference(LOCK_TABLE, f"{side} holds {held} row(s), expected one")
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
    probes = []
    for index, column in enumerate(columns):
        name = _quoted(column)
        beyond = f"{name} > {EXPORT_SAFE_INTEGER} OR {name} < -{EXPORT_SAFE_INTEGER}"
        probes += [
            f"max(typeof({name}) = 'integer' AND ({beyond})) AS \"i{index}\"",
            f"max(typeof({name}) = 'real') AS \"r{index}\"",
        ]
    return f"SELECT {', '.join(probes)} FROM {_quoted(table)}"


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
        found = []
        tables = [*application_tables(connection)]
        tables += [t for t in _SQLITE_TABLES_COMPARED if _has_table(connection, t)]
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
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


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def write_import_sql(database: pathlib.Path, destination: pathlib.Path) -> None:
    """Write `database` out as SQL that D1's import accepts. See the module docstring.

    A LOCK_TABLE row is written as locked whatever the source held. Setting it
    with a second call after the import leaves a window, and a failure in it
    leaves a complete copy open; written here, the lock arrives in the same
    all-or-nothing import as the rows it guards.

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
            locked = {LOCK_TABLE: {"locked": "1"}}
            for table in plan.tables:
                _write_rows(
                    connection,
                    table,
                    out,
                    held_back=plan.deferred.get(table, ()),
                    fixed=locked.get(table, {}),
                )
            for table, columns in plan.deferred.items():
                _write_restores(connection, table, columns, out)
            if _has_table(connection, "sqlite_sequence"):
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
                out.write(f"DROP INDEX {_quoted(name)};\n")
            for (definition,) in others:
                out.write(f"{definition};\n")
            if _has_table(connection, "sqlite_stat1"):
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
        for row in connection.execute(f"PRAGMA foreign_key_list({_quoted(table)})"):
            grouped.setdefault(int(row[0]), []).append(
                (int(row[1]), str(row[2]), str(row[3]), row[4])
            )
        found = []
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
    holdable = {
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
            for row in connection.execute(f"PRAGMA foreign_key_list({_quoted(table)})"):
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
                    else "(" + ",".join(_quoted(str(to)) for to in named) + ")"
                )
                scratch.execute(
                    f"CREATE TABLE {_quoted(probe)} ({columns}, FOREIGN KEY "
                    f"({columns}) REFERENCES {_quoted(parent)}{pointed})"
                )
                insert = f"EXPLAIN INSERT INTO {_quoted(probe)} DEFAULT VALUES"
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
                        scratch.execute(f"DROP INDEX {_quoted(name)}")
                        if served:
                            needed.add(name)
                            break
                finally:
                    scratch.execute(f"DROP TABLE {_quoted(probe)}")
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
        f"PRAGMA index_list({_quoted(str(found[0]))})"
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
        for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
    }


def _primary_key(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = [
        row
        for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
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
        for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
    }
    if all(info[column][1] for column in key):
        return True
    key_indexed = any(
        row[3] == "pk"
        for row in connection.execute(f"PRAGMA index_list({_quoted(table)})")
    )
    if len(key) == 1 and info[key[0]][0].upper() == "INTEGER" and not key_indexed:
        return True
    # The PRAGMA itself, not the pragma_table_list table-valued function, which
    # an application table of that name would shadow.
    listed = connection.execute(f"PRAGMA main.table_list({_quoted(table)})").fetchone()
    return listed is not None and bool(listed[4] or listed[5])


def _literal(value: object, where: str) -> str:
    """`value` as SQL that reads back as exactly that value.

    Rendered here rather than by SQLite's quote(), which stops at the first NUL
    in a string: 'ab<NUL>cd' comes back as 'ab', silently. Text holding any
    control character is written as a cast of its bytes instead, which is exact
    for any text and leaves nothing in the import file for a line-oriented step
    between here and D1's SQLite to reinterpret - a NUL, a carriage return, a
    newline. Everything else is a plain literal, where the only character with
    meaning is the quote. A REAL is refused - compare() says why - and refused
    here so that it stops a copy before the target is touched.
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
    fixed: typing.Mapping[str, str] | None = None,
) -> None:
    """One INSERT per row. `held_back` columns are written NULL and restored later;
    `fixed` columns are written as the given SQL whatever the row holds."""
    fixed = fixed or {}
    columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
    ]
    names = ",".join(_quoted(column) for column in columns)
    prefix = f"INSERT INTO {_quoted(table)} ({names}) VALUES("
    for row in connection.execute(f"SELECT {names} FROM {_quoted(table)}"):
        rendered = ",".join(
            fixed[column]
            if column in fixed
            else "NULL"
            if column in held_back
            else _literal(value, f"{table}.{column}")
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
                for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
                if row[5]
            ),
            key=lambda row: int(row[5]),
        )
    ]
    if not key:
        raise SnapshotError(
            f"{table} needs a column restored after insert and declares no primary key"
        )
    selected = ",".join(_quoted(column) for column in key)
    for column in columns:
        target = _quoted(column)
        for row in connection.execute(
            f"SELECT {target},{selected} FROM {_quoted(table)} WHERE {target} IS NOT NULL"
        ):
            where = " AND ".join(
                f"{_quoted(name)} = {_literal(value, f'{table}.{name}')}"
                for name, value in zip(key, row[1:], strict=True)
            )
            out.write(
                f"UPDATE {_quoted(table)} SET {target} = "
                f"{_literal(row[0], f'{table}.{column}')} WHERE {where};\n"
            )
