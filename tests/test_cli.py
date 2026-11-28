"""The commands end to end, against a stand-in for the D1 API (fake_d1.py).

Each case is a promise the README makes about what a run does to two real
databases: what it copies, what it refuses before anything is written, and what
it leaves behind on disk.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sqlite3
import tempfile
import unittest
from typing import override
from unittest import mock

from d1_copy import cli, rest, snapshot, workdir
from tests.fake_d1 import ACCOUNT, FakeD1

# A child table cataloged before its parent, the order D1's own import refuses.
SOURCE = """
CREATE TABLE comments (
  id INTEGER PRIMARY KEY,
  author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
  body TEXT
) STRICT;
CREATE TABLE authors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  handle TEXT NOT NULL UNIQUE
) STRICT;
CREATE INDEX idx_comments_author ON comments(author_id);
INSERT INTO authors (id, handle) VALUES (1, 'a'), (2, 'b'), (5, 'c');
DELETE FROM authors WHERE id = 5;
INSERT INTO comments (id, author_id, body) VALUES (10, 1, 'it''s'), (11, 2, NULL);
"""

LOCK = """
CREATE TABLE _deploy_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  locked INTEGER NOT NULL DEFAULT 0
) STRICT;
INSERT INTO _deploy_lock (id, locked) VALUES (1, 0);
"""


class CommandTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = pathlib.Path(scratch.name)
        (self.root / "api").mkdir()
        self.api = FakeD1(self.root / "api")
        self.work = self.root / "work"
        for patch in (
            mock.patch.object(workdir, "WORK_PARENT", self.work),
            # The export poll waits between attempts; nothing here needs to.
            mock.patch("d1_copy.rest.time.sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_command(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        client = rest.D1Client(ACCOUNT, "test-token")
        with (
            self.api.serving(),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            status = cli.run(client, list(argv))
        return status, out.getvalue() + err.getvalue()

    def assert_exact(self, source: pathlib.Path, target: pathlib.Path) -> None:
        self.assertEqual(snapshot.compare(source, target), [])

    def test_copy_fills_an_empty_target_with_an_exact_copy(self) -> None:
        """The export of this source cannot be imported as it arrives; the copy
        lands anyway, sequence mark and index included."""
        source = self.api.create("source", SOURCE)
        target = self.api.create("target")
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 0, output)
        self.assertIn("[OK] the target is an exact copy of the source", output)
        self.assert_exact(source, target)

    def test_a_target_that_holds_a_table_is_refused_before_any_export(self) -> None:
        """A copy is made only into an empty database; this tool drops nothing,
        and an export blocks the source, so the refusal comes first."""
        self.api.create("source", SOURCE)
        target = self.api.create(
            "target", "CREATE TABLE kept (id INTEGER PRIMARY KEY);"
        )
        before = target.read_bytes()
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("already holds 1 application table", output)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(any("/export" in path for path in self.api.paths("POST")))

    def test_the_same_database_twice_is_refused(self) -> None:
        self.api.create("source", SOURCE)
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "source"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("same database", output)

    def test_a_source_holding_an_integer_an_export_rounds_is_refused_before_export(
        self,
    ) -> None:
        """The export would round it and the comparison of exports would agree
        with itself, so the live source is asked before it is exported."""
        self.api.create(
            "source", SOURCE + "INSERT INTO authors VALUES (9007199254740993, 'd');"
        )
        target = self.api.create("target")
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("authors.id holds an integer beyond 2^53", output)
        self.assertFalse(any("/export" in path for path in self.api.paths("POST")))
        self.assertEqual(snapshot_tables(target), [])

    def test_a_whole_number_real_is_refused_before_export(self) -> None:
        """Written as `1`, it reads back into a column of no type as an INTEGER,
        and both sides' exports agree on the INTEGER: so the copy would verify
        while differing from the live source. The refusal names the column."""
        self.api.create(
            "source",
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, v); "
            "INSERT INTO notes VALUES (1, 1.0);",
        )
        self.api.create("target")
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("notes.v holds a REAL value", output)
        self.assertFalse(any("/export" in path for path in self.api.paths("POST")))

    def test_text_an_export_escapes_or_an_import_would_cut_is_copied_exactly(
        self,
    ) -> None:
        """Line breaks beside their own escaped forms come back from D1's export
        as they went in, so they copy rather than refuse. A NUL ends D1's import
        of a file that holds it raw, silently and with success reported, so the
        import file must never hold one: a row lost that way would show here as
        a difference."""
        values = [
            "a\\nb\nc",
            "a\\rb\rc",
            "a\r\nb\\r\\nc\\012(\\n0)",
            "a\x00b",
        ]
        rows = ", ".join(
            f"({index}, CAST(X'{value.encode().hex()}' AS TEXT))"
            for index, value in enumerate(values, start=1)
        )
        source = self.api.create(
            "source",
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, v TEXT) STRICT; "
            f"INSERT INTO notes VALUES {rows}; "
            "CREATE TABLE after_notes (id INTEGER PRIMARY KEY) STRICT; "
            "INSERT INTO after_notes VALUES (1);",
        )
        target = self.api.create("target")
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 0, output)
        self.assert_exact(source, target)
        connection = sqlite3.connect(target)
        try:
            copied = [
                bytes.fromhex(hexed).decode()
                for (hexed,) in connection.execute(
                    "SELECT hex(v) FROM notes ORDER BY id"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(copied, values)

    def test_a_table_name_shaped_like_a_wildcard_is_still_checked(self) -> None:
        """`_` matches any character in LIKE, so a filter written as
        `NOT LIKE '_cf_%'` would skip a table named `xcf_y` - past both the
        empty-target check and the check of what an export would change."""
        self.api.create(
            "source",
            "CREATE TABLE xcf_y (id INTEGER PRIMARY KEY, n INTEGER); "
            "INSERT INTO xcf_y VALUES (1, 9007199254740993);",
        )
        self.api.create("target", "CREATE TABLE xcf_y (id INTEGER PRIMARY KEY);")
        self.api.create("empty")
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("already holds 1 application table", output)
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "empty"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("xcf_y.n holds an integer beyond 2^53", output)

    def test_a_table_created_on_the_target_mid_copy_keeps_its_marks(self) -> None:
        """The empty-target check is a read made before the import, so a table
        someone creates in between is not refused. Its AUTOINCREMENT mark must
        survive the import's own bookkeeping, or its next insert reuses an id;
        and the copy must not be called exact."""

        def someone_else_writes(database: pathlib.Path) -> None:
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    "CREATE TABLE theirs (id INTEGER PRIMARY KEY AUTOINCREMENT);"
                    "INSERT INTO theirs (id) VALUES (41);"
                    "DELETE FROM theirs;"
                )
            finally:
                connection.close()

        self.api.create("source", SOURCE)
        target = self.api.create("target")
        self.api.before_import = someone_else_writes
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 1, output)
        self.assertIn("theirs: exists only in the target", output)
        self.assertEqual(sorted(marks_of(target)), [("authors", 5), ("theirs", 41)])

    def test_a_source_mark_naming_no_table_is_refused_before_the_target(
        self,
    ) -> None:
        """SQLite lets an application write a mark for a name no table has.
        Copied, it could land on a table of that name someone creates on the
        target meanwhile, with or without a mark of its own, and move the ids
        that table hands out."""
        self.api.create(
            "source",
            SOURCE + "INSERT INTO sqlite_sequence VALUES ('never-print-this', 100);",
        )
        target = self.api.create("target")
        before = target.read_bytes()
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 1, output)
        self.assertIn("sqlite_sequence.name holds 1 name(s) no table has", output)
        self.assertNotIn("never-print-this", output)
        self.assertNotIn("/import", " ".join(self.api.paths("POST")))
        self.assertEqual(target.read_bytes(), before)

    def test_every_mark_on_a_table_the_file_creates_is_carried_exactly(
        self,
    ) -> None:
        """SQLite hands out max(mark, highest id) + 1, uses a mark only for an
        AUTOINCREMENT table, and reads the first of several for one name. A
        copy that carries each of these as the source holds it hands out the
        same ids the source would."""
        shapes = {
            "above the highest id": "UPDATE sqlite_sequence SET seq = 900 WHERE name = 'authors';",
            "below the highest id": "UPDATE sqlite_sequence SET seq = 1 WHERE name = 'authors';",
            "on a table that is not AUTOINCREMENT": "INSERT INTO sqlite_sequence VALUES ('comments', 70);",
            "twice for one table": "INSERT INTO sqlite_sequence VALUES ('authors', 3);",
        }
        for index, (shape, sql) in enumerate(shapes.items()):
            with self.subTest(shape=shape):
                source = self.api.create(f"source-{index}", SOURCE + sql)
                target = self.api.create(f"target-{index}")
                status, output = self.run_command(
                    "copy", "--source", f"source-{index}", "--target", f"target-{index}"
                )
                self.assertEqual(status, 0, output)
                self.assert_exact(source, target)
                self.assertEqual(marks_of(target), marks_of(source))

    def test_a_mark_naming_a_table_in_another_case_is_refused(self) -> None:
        """SQLite finds a table's mark by its exact name, so this one belongs to
        no table, and is refused like any other such mark."""
        self.api.create(
            "source", SOURCE + "INSERT INTO sqlite_sequence VALUES ('AUTHORS', 100);"
        )
        self.api.create("target")
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 1, output)
        self.assertIn("sqlite_sequence.name holds 1 name(s) no table has", output)

    def test_verify_cannot_be_fooled_by_a_value_the_exports_round_alike(self) -> None:
        """2^53 + 1 and 2^53 export as the same number. A verify that trusted the
        exports would call these databases equal."""
        self.api.create(
            "source", SOURCE + "INSERT INTO authors VALUES (9007199254740993, 'd');"
        )
        self.api.create(
            "target", SOURCE + "INSERT INTO authors VALUES (9007199254740992, 'd');"
        )
        status, output = self.run_command(
            "verify", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("authors.id holds an integer beyond 2^53", output)

    def test_verify_reports_a_difference_by_table_and_count(self) -> None:
        self.api.create("source", SOURCE)
        self.api.create("target", SOURCE.replace("'it''s'", "'never-print-this-body'"))
        status, output = self.run_command(
            "verify", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 1, output)
        self.assertIn("comments: 1 row(s) of 2 in the source", output)
        self.assertNotIn("never-print-this-body", output)

    def test_verify_of_a_copy_writes_nothing_and_passes(self) -> None:
        source = self.api.create("source", SOURCE)
        target = self.api.create("target")
        self.assertEqual(
            self.run_command("copy", "--source", "source", "--target", "target")[0], 0
        )
        before = (source.read_bytes(), target.read_bytes())
        status, output = self.run_command(
            "verify", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 0, output)
        self.assertEqual((source.read_bytes(), target.read_bytes()), before)

    def test_restore_fills_a_target_from_a_dump_file(self) -> None:
        source = self.api.create("source", SOURCE)
        dump = self.root / "backup.sql"
        with self.api.serving():
            rest.D1Client(ACCOUNT, "test-token").export_to(
                "00000000-0000-4000-8000-000000000001", dump
            )
        target = self.api.create("target")
        self.api.requests.clear()
        status, output = self.run_command(
            "restore", "--dump", str(dump), "--target", "target"
        )
        self.assertEqual(status, 0, output)
        self.assert_exact(source, target)
        # No live database stands behind a dump, so none but the target is read.
        self.assertFalse(
            any("000000000001" in where for _, where, _ in self.api.requests)
        )

    def test_check_names_the_table_an_export_would_change(self) -> None:
        self.api.create("clean", SOURCE)
        self.api.create(
            "big", SOURCE + "INSERT INTO authors VALUES (-9223372036854775808, 'd');"
        )
        self.assertEqual(self.run_command("check", "--source", "clean")[0], 0)
        status, output = self.run_command("check", "--source", "big")
        self.assertEqual(status, 1, output)
        self.assertIn("- authors.id holds an integer beyond 2^53", output)

    def test_copy_preserves_a_legacy_deploy_lock_value(self) -> None:
        """An older dump's control state is data that must round trip exactly."""
        source = self.api.create("source", SOURCE + LOCK)
        target = self.api.create("target")
        status, output = self.run_command(
            "copy", "--source", "source", "--target", "target"
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(lock_of(target), 0)
        self.assert_exact(source, target)

    def test_the_token_goes_to_the_api_and_nowhere_else(self) -> None:
        """The export download and the import upload are pre-signed object-store
        URLs; the token has no business there."""
        self.api.create("source", SOURCE)
        self.api.create("target")
        self.run_command("copy", "--source", "source", "--target", "target")
        for _, where, authorized in self.api.requests:
            host = where.split("/", 1)[0]
            self.assertEqual(authorized, host == "api.cloudflare.com", where)

    def test_no_working_file_outlives_the_run(self) -> None:
        """The dumps and working databases are full copies of the source."""
        self.api.create("source", SOURCE)
        self.api.create("target")
        self.run_command("copy", "--source", "source", "--target", "target")
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["lock"])

    def test_a_run_killed_earlier_has_its_leftovers_removed(self) -> None:
        self.work.mkdir(mode=0o700)
        (self.work / "run-killed").mkdir()
        (self.work / "run-killed" / "source.sql").write_text("data", encoding="utf-8")
        self.api.create("clean", SOURCE)
        self.api.create("target")
        self.run_command("copy", "--source", "clean", "--target", "target")
        self.assertFalse((self.work / "run-killed").exists())


def marks_of(database: pathlib.Path) -> list[tuple[str, int]]:
    """Every sequence mark, in the order SQLite reads them."""
    connection = sqlite3.connect(database)
    try:
        return list(
            connection.execute("SELECT name, seq FROM sqlite_sequence ORDER BY rowid")
        )
    finally:
        connection.close()


class CredentialsTest(unittest.TestCase):
    def test_missing_credentials_are_a_refusal_before_any_call(self) -> None:
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("urllib.request.urlopen") as urlopen,
            contextlib.redirect_stderr(err),
        ):
            status = cli.main(["check", "--source", "anything"])
        self.assertEqual(status, 2)
        self.assertIn("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN", err.getvalue())
        urlopen.assert_not_called()


def lock_of(database: pathlib.Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute("SELECT locked FROM _deploy_lock").fetchone()[0])
    finally:
        connection.close()


def snapshot_tables(database: pathlib.Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return snapshot.application_tables(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
