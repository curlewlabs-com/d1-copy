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
    """
    if database.exists():
        raise SnapshotError(f"refusing to load over an existing file: {database}")
    raw = dump.read_bytes()
    token = _absent_token(raw) if b"\x00" in raw else None
    if token is not None:
        raw = raw.replace(b"\x00", token.encode())
    connection = sqlite3.connect(database)
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
        raise SnapshotError(f"{dump.name} did not load as SQL: {exc}") from exc
    finally:
        connection.close()


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

    A dump writes a REAL as decimal text, which need not read back as the same
    double. A source's dump and its copy's would still agree with each other, so the
    comparison below could not see that loss; refusing the value is what keeps
    "exact" true. SQLite does not stop one arriving in a column of another
    declared type, so every column is asked.
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
# column is named with in unexportable_sql.
#
#   i - D1 holds a 64-bit integer exactly and exports it through a double:
#       2^53 + 1 leaves as 2^53, still written as an integer.
#   r - an export writes a REAL as decimal text, which need not read back as the
#       same double, and Cloudflare's export source writes a whole-number REAL
#       without its decimal point, so 1.0 in a column of no declared type reads
#       back as the INTEGER 1.
#   n - Cloudflare's export source writes a real newline inside text as the two
#       characters backslash and n, and wraps the literal in a replace() that
#       turns every such pair back into a newline, so a literal backslash-n in
#       the same value becomes a newline too; a carriage return and backslash-r
#       go the same way.
UNEXPORTABLE = {
    "i": "an integer beyond 2^53",
    "r": "a REAL value",
    "n": "text holding both a newline and a literal \\n, or both a carriage "
    "return and a literal \\r",
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
        newline = f"instr({name}, char(10)) > 0 AND instr({name}, '\\n') > 0"
        carriage = f"instr({name}, char(13)) > 0 AND instr({name}, '\\r') > 0"
        rewritten = f"({newline}) OR ({carriage})"
        probes += [
            f"max(typeof({name}) = 'integer' AND ({beyond})) AS \"i{index}\"",
            f"max(typeof({name}) = 'real') AS \"r{index}\"",
            f"max(typeof({name}) = 'text' AND ({rewritten})) AS \"n{index}\"",
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
                # This DELETE, and the one of sqlite_stat1 below, clear only what
                # this file's own statements wrote. The file is for an empty
                # database and cannot run against another of this schema: its
                # first CREATE TABLE fails there, and D1 applies none of a failed
                # import. The command line refuses a target holding any table
                # before it sends one.
                out.write("DELETE FROM sqlite_sequence;\n")
                _write_rows(connection, "sqlite_sequence", out)
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
            for (definition,) in others:
                out.write(f"{definition};\n")
            if _has_table(connection, "sqlite_stat1"):
                # sqlite_stat1 is a reserved name that cannot be created, so an
                # ANALYZE brings it into being; a D1 export opens the same way.
                out.write("ANALYZE sqlite_schema;\nDELETE FROM sqlite_stat1;\n")
                _write_rows(connection, "sqlite_stat1", out)
                # A connection plans from the statistics it last loaded, and D1
                # was observed to ignore raw writes to the table until this
                # reload. Without it the target compares as an exact copy and
                # plans as though it had never been analyzed.
                out.write("ANALYZE sqlite_master;\n")
    finally:
        connection.close()


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
    is broken at a NULLABLE referencing column: those columns are written as NULL
    so their table can go first, and restored by UPDATE once every row exists.
    Only a table that this frees completely is chosen, so nothing is deferred
    that did not have to be. A cycle with no such column cannot be written in
    any order and is refused.

    Read from foreign_key_list and table_info, SQLite's own account of the
    schema, never from the CREATE TABLE text. A self-reference orders nothing.
    """
    parents: dict[str, dict[str, set[str]]] = {}
    nullable: dict[str, set[str]] = {}
    for table in tables:
        # Not NOT NULL, and not part of the primary key. table_info reports an
        # INTEGER PRIMARY KEY as nullable, but a NULL written there is not an
        # absent value: SQLite assigns the next rowid, which as a foreign key
        # points at a parent nobody chose. A key column is never held back.
        nullable[table] = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
            if not row[3] and not row[5]
        }
        by_parent: dict[str, set[str]] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({_quoted(table)})"):
            parent, column = str(row[2]), str(row[3])
            if parent != table and parent in tables:
                by_parent.setdefault(parent, set()).add(column)
        parents[table] = by_parent

    ordered: list[str] = []
    deferred: dict[str, list[str]] = {}
    waiting = set(tables)
    while waiting:
        ready = sorted(
            table for table in waiting if not (parents[table].keys() & waiting)
        )
        if ready:
            ordered.extend(ready)
            waiting.difference_update(ready)
            continue
        freed = next(
            (
                table
                for table in sorted(waiting)
                if all(
                    columns <= nullable[table]
                    for parent, columns in parents[table].items()
                    if parent in waiting
                )
            ),
            None,
        )
        if freed is None:
            raise SnapshotError(
                "foreign keys form a cycle with no nullable column to break it at: "
                + ", ".join(sorted(waiting))
            )
        deferred[freed] = sorted(
            column
            for parent, columns in parents[freed].items()
            if parent in waiting
            for column in columns
        )
        ordered.append(freed)
        waiting.discard(freed)
    return _InsertPlan(ordered, deferred)


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
