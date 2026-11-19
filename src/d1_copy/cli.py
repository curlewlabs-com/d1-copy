"""The command line: copy one D1 database into another and prove the copy exact.

    d1-copy copy    --source NAME --target NAME
    d1-copy restore --dump FILE   --target NAME
    d1-copy verify  --source NAME --target NAME
    d1-copy check   --source NAME

`copy` exports the source, imports it into an EMPTY target, exports the target
back, and compares that export with the source's locally (snapshot.py, which
also says why the import file is regenerated rather than replayed). It is a
whole-database copy on purpose: with no delta to compute there is no
UPDATE-or-DELETE ordering, no UNIQUE reuse and no cascade for a partial apply to
get wrong, and the target is what the source's own dump says and nothing else.
The price is that the copy's duration grows with the database rather than with
what changed. It reads the source ONCE, through an export, which blocks the
source's other queries while it runs.

`restore` is `copy` with a dump file standing in for the source: an export kept
as a backup, already decompressed. It reads no live database. D1 refuses an
export's own statement order, and this does not replay it.

`verify` exports two databases and compares them, writing nothing.

`check` reads a live database for what an export cannot carry and reports it. It
writes nothing. `copy` runs the same check on its source before exporting,
`verify` runs it on both sides, and `restore`, whose source is a file with no
live origin to ask, runs it on the loaded dump.

WHAT "EXACT" IS A CLAIM ABOUT. A source nobody is writing. The check of the live
source and the export of it are separate reads, so against a database still
taking writes a value could arrive between them, and the copy is exact only as
of the instant it was exported. Nothing here pauses a source's writers; whatever
calls this does.

WHAT IT REFUSES, each before anything is written:
  - a source and target that resolve to the same database;
  - a target that already holds application tables. A copy is only ever made
    into an empty database, and nothing here drops or deletes what a database
    already held;
  - a source holding a value an export would change, or one it cannot be
    shown to carry: an integer beyond 2^53, or a REAL. The copy would change
    it and the comparison, made between exports, could not tell (snapshot.py's
    UNEXPORTABLE);
  - foreign keys that form a cycle with no nullable column to break it at
    (snapshot.py).

Credentials: CLOUDFLARE_ACCOUNT_ID, and CLOUDFLARE_API_TOKEN with D1 Edit on the
account (an export is write-classed). Neither is ever printed.

Exit status: 0 when the target is an exact copy (or `check` finds nothing), 1
when it differs or a call fails, 2 when a precondition refused the run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys
import time

from . import rest, snapshot
from ._decoded import json_object
from .workdir import WorkdirInUse, WorkdirUnsafe, private_workdir

_TABLES_SQL = (
    "SELECT name FROM sqlite_schema WHERE type = 'table' "
    "AND name NOT GLOB 'sqlite_*' AND name NOT GLOB '_cf_*' ORDER BY name"
)


# SQLite's tables that a copy carries along with the application's, and so whose
# values an export has to carry too.
_BOOKKEEPING_TABLES = ("sqlite_sequence", "sqlite_stat1")


class Refusal(RuntimeError):
    """A precondition failed. Nothing was written."""


def _say(message: str) -> None:
    print(message, flush=True)


# --- interlocks -------------------------------------------------------------


def _resolve_pair(
    client: rest.D1Client, source_name: str, target_name: str
) -> tuple[rest.Database, rest.Database]:
    source, target = client.resolve(source_name), client.resolve(target_name)
    _say(f"source: {source}")
    _say(f"target: {target}")
    if source.uuid == target.uuid:
        raise Refusal("source and target are the same database")
    return source, target


def _application_tables(client: rest.D1Client, database: rest.Database) -> list[str]:
    result = client.query(database.uuid, _TABLES_SQL, mutates=False)
    return [str(row["name"]) for row in result.rows]


def _values_an_export_would_change(
    client: rest.D1Client, source: rest.Database
) -> list[str]:
    """Columns of the LIVE source holding a value an export cannot carry.

    Asked of the live database because nothing downstream of an export can
    answer it. One bounded scan per table, on the primary, so it reads about as
    many rows as the database holds. That is metered, and it is said out loud on
    every run rather than left to be inferred.
    """
    tables = [*_application_tables(client, source), *_BOOKKEEPING_TABLES]
    # Batched, because a round trip per table adds up to seconds on a database
    # with many tables, and a caller may be holding writes paused meanwhile.
    described = client.query_many(
        source.uuid,
        [
            'PRAGMA table_info("{}")'.format(table.replace('"', '""'))
            for table in tables
        ],
        mutates=False,
    )
    # A bookkeeping table a database has not created describes as no columns;
    # an application table never does, so one that did is an incomplete answer.
    probes = [
        (table, [str(row["name"]) for row in info.rows])
        for table, info in zip(tables, described, strict=True)
        if info.rows or table not in _BOOKKEEPING_TABLES
    ]
    if any(not columns for _, columns in probes):
        raise rest.D1Error(f"{source}: a table described as having no columns")
    answers = client.query_many(
        source.uuid,
        [snapshot.unexportable_sql(table, columns) for table, columns in probes],
        mutates=False,
    )
    rows_read = sum(
        int(result.meta.get("rows_read") or 0) for result in (*described, *answers)
    )
    _say(f"{source}: live check read {rows_read} row(s)")
    return [
        found
        for (table, columns), answer in zip(probes, answers, strict=True)
        for found in snapshot.unexportable_values(table, columns, _one(answer))
    ]


def _one(answer: rest.QueryResult) -> dict[str, object]:
    if len(answer.rows) != 1:
        raise rest.D1Error(f"an aggregate query answered {len(answer.rows)} row(s)")
    return answer.rows[0]


def _refuse_what_an_export_cannot_carry(
    client: rest.D1Client, source: rest.Database
) -> None:
    found = _values_an_export_would_change(client, source)
    if found:
        raise Refusal(
            f"{source} holds what an export would change, so no comparison of "
            f"exports could tell: {'; '.join(found)}"
        )


def _refuse_a_source_a_copy_cannot_vouch_for(source_db: pathlib.Path) -> None:
    found = snapshot.values_an_export_would_change(source_db)
    if found:
        # For a dump file this is the only place the question can be asked. For
        # a live source it backs up the check made before the export: a value an
        # export rounded is usually still beyond 2^53, so it shows here too.
        raise Refusal(
            "the target is read back through an export, which would change what "
            f"the source holds, so the copy could not be confirmed: {'; '.join(found)}"
        )


def _checked_target(client: rest.D1Client, target: rest.Database) -> None:
    existing = _application_tables(client, target)
    if existing:
        raise Refusal(
            f"{target} already holds {len(existing)} application table(s), and a "
            "copy is only made into an empty database. This tool drops nothing; "
            "empty the target yourself, or copy into a new database"
        )


# --- the lock row -----------------------------------------------------------


def _require_locked(client: rest.D1Client, database: rest.Database) -> None:
    """Read the target's deploy lock back and refuse to go on unless it is set.

    The import wrote the row as locked, in the same all-or-nothing step as the
    rows it guards (snapshot.write_import_sql), so there was never a moment when
    the target held data and was open. This only reads it back.
    """
    observed = client.query(
        database.uuid,
        f"SELECT locked FROM {snapshot.LOCK_TABLE} WHERE id = 1",
        mutates=False,
    ).rows
    if len(observed) != 1 or int(observed[0]["locked"]) != 1:
        raise rest.D1Error(
            f"{database}: deploy lock did not read back as set; observed {observed!r}"
        )
    _say(f"{database}: deploy lock reads back as set")


# --- the copy ---------------------------------------------------------------


def _export_and_load(
    client: rest.D1Client,
    database: rest.Database,
    workdir: pathlib.Path,
    label: str,
) -> tuple[pathlib.Path, float]:
    started = time.monotonic()
    dump, loaded = workdir / f"{label}.sql", workdir / f"{label}.db"
    client.export_to(database.uuid, dump)
    snapshot.load_dump(dump, loaded)
    return loaded, time.monotonic() - started


def _report(differences: list[snapshot.Difference]) -> int:
    if not differences:
        _say("[OK] the target is an exact copy of the source")
        return 0
    _say(f"[FAIL] the target differs from the source in {len(differences)} way(s):")
    for difference in differences:
        _say(f"  - {difference}")
    return 1


def _fill_and_prove(
    client: rest.D1Client,
    target: rest.Database,
    source_db: pathlib.Path,
    workdir: pathlib.Path,
    timings: dict[str, float],
) -> list[snapshot.Difference]:
    """Import `source_db` into the empty target and compare it back."""
    _refuse_a_source_a_copy_cannot_vouch_for(source_db)
    _say(f"source holds {snapshot.row_total(source_db)} application row(s)")

    # Rendering is where a value that cannot be carried exactly, or a
    # foreign-key cycle that cannot be written, is refused - before the target
    # is touched. The export is never imported as it arrived; snapshot.py says
    # why.
    started = time.monotonic()
    import_sql = workdir / "import.sql"
    snapshot.write_import_sql(source_db, import_sql)
    timings["render_import"] = time.monotonic() - started

    # The target was read as empty a moment ago (_checked_target). Should
    # anything have filled it since, the import's first CREATE TABLE fails and
    # D1 applies none of the file, so an import can add to a database and
    # cannot replace what one holds.
    started = time.monotonic()
    summary = client.import_file(target.uuid, import_sql)
    timings["import_target"] = time.monotonic() - started
    _say(f"import summary: {_import_figures(summary)}")

    if snapshot.has_lock_table(source_db):
        _require_locked(client, target)

    target_db, timings["export_target"] = _export_and_load(
        client, target, workdir, "target"
    )
    started = time.monotonic()
    differences = snapshot.compare(source_db, target_db)
    timings["compare"] = time.monotonic() - started
    return differences


def _say_timings(timings: dict[str, float]) -> None:
    _say(
        "timings (s): "
        + " ".join(f"{name}={seconds:.1f}" for name, seconds in timings.items())
        + f" total={sum(timings.values()):.1f}"
    )


def _import_figures(summary: dict[str, object]) -> str:
    meta = summary.get("meta")
    figures = json_object(meta) or {}
    return (
        " ".join(
            f"{key}={figures[key]}"
            for key in ("rows_written", "rows_read", "size_after", "duration")
            if key in figures
        )
        or f"num_queries={summary.get('num_queries')}"
    )


# --- commands ---------------------------------------------------------------


def copy(client: rest.D1Client, args: argparse.Namespace) -> int:
    source, target = _resolve_pair(client, args.source, args.target)
    _checked_target(client, target)
    timings: dict[str, float] = {}
    started = time.monotonic()
    _refuse_what_an_export_cannot_carry(client, source)
    timings["check_source"] = time.monotonic() - started
    with private_workdir() as workdir:
        source_db, timings["export_source"] = _export_and_load(
            client, source, workdir, "source"
        )
        differences = _fill_and_prove(client, target, source_db, workdir, timings)
    _say_timings(timings)
    return _report(differences)


def restore(client: rest.D1Client, args: argparse.Namespace) -> int:
    dump = pathlib.Path(args.dump)
    if not dump.is_file():
        raise Refusal(f"no dump file at {dump}")
    target = client.resolve(args.target)
    _say(f"source: {dump.name} (a dump file; no live database is read)")
    _say(f"source sha256: {hashlib.sha256(dump.read_bytes()).hexdigest()}")
    _say(f"target: {target}")
    _checked_target(client, target)
    timings: dict[str, float] = {}
    with private_workdir() as workdir:
        started = time.monotonic()
        source_db = workdir / "source.db"
        snapshot.load_dump(dump, source_db)
        timings["load_dump"] = time.monotonic() - started
        differences = _fill_and_prove(client, target, source_db, workdir, timings)
    _say_timings(timings)
    return _report(differences)


def verify(client: rest.D1Client, args: argparse.Namespace) -> int:
    source, target = _resolve_pair(client, args.source, args.target)
    # The source AND the target, live, before an export of either is trusted.
    # An export rounds an integer beyond 2^53, so a source holding 2^53 + 1 and
    # a target holding 2^53 export identically and would compare as an exact
    # copy. `copy` needs this of its source alone, because it fills the target
    # from that export; here the target's contents are whatever they already
    # were.
    for database in (source, target):
        _refuse_what_an_export_cannot_carry(client, database)
    with private_workdir() as workdir:
        source_db, _ = _export_and_load(client, source, workdir, "source")
        target_db, _ = _export_and_load(client, target, workdir, "target")
        differences = snapshot.compare(source_db, target_db)
    return _report(differences)


def check(client: rest.D1Client, args: argparse.Namespace) -> int:
    source = client.resolve(args.source)
    _say(f"source: {source}")
    found = _values_an_export_would_change(client, source)
    if found:
        _say(f"[FAIL] an export would change {len(found)} column(s):")
        for each in found:
            _say(f"  - {each}")
        return 1
    _say("[OK] every value is one an export carries exactly")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="d1-copy",
        description="Copy one D1 database into another and prove the copy exact.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("copy", copy), ("verify", verify)):
        sub = commands.add_parser(name)
        sub.add_argument("--source", required=True, help="exact database name")
        sub.add_argument("--target", required=True, help="exact database name")
        sub.set_defaults(handler=handler)

    from_dump = commands.add_parser("restore")
    from_dump.add_argument("--dump", required=True, help="a decompressed SQL dump")
    from_dump.add_argument("--target", required=True, help="exact database name")
    from_dump.set_defaults(handler=restore)

    live = commands.add_parser("check")
    live.add_argument("--source", required=True, help="exact database name")
    live.set_defaults(handler=check)
    return parser


def _client() -> rest.D1Client:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not account or not token:
        raise Refusal("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must both be set")
    return rest.D1Client(account, token)


def run(client: rest.D1Client, argv: list[str]) -> int:
    """Parse `argv` and run it against `client`, mapping each outcome to a status."""
    args = _parser().parse_args(argv)
    try:
        handler = args.handler
        result: int = handler(client, args)
    except (Refusal, WorkdirInUse, WorkdirUnsafe) as refusal:
        print(f"[REFUSED] {refusal}", file=sys.stderr)
        return 2
    except (rest.D1Error, snapshot.SnapshotError) as failure:
        print(f"[FAIL] {failure}", file=sys.stderr)
        return 1
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    # Parsed before credentials are read, so --help and a usage error need none.
    _parser().parse_args(arguments)
    try:
        client = _client()
    except Refusal as refusal:
        print(f"[REFUSED] {refusal}", file=sys.stderr)
        return 2
    return run(client, arguments)
