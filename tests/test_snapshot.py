"""Behavior tests for the exact comparison a copy is judged by, and the import
file it is made from.

Every comparison case is a way a copy can be wrong while looking right to a
cheaper check: equal row counts, equal primary keys, equal everything a person
would glance at. They run against real SQLite databases built from dump-shaped
SQL, because what is under test is what SQLite does with those statements.
"""

from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest
from typing import override

from d1_copy import snapshot
from tests.fake_d1 import exported

# The shape a D1 export has: a deferred-foreign-key pragma, the schema, the rows,
# the sequence table. A parent and a cascading child, and a BLOB-keyed table, so
# each case below has something real to break.
SCHEMA = """
PRAGMA defer_foreign_keys=TRUE;
CREATE TABLE authors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  handle TEXT NOT NULL UNIQUE,
  joined_at INTEGER
) STRICT;
CREATE TABLE posts (
  id BLOB PRIMARY KEY CHECK(length(id) = 16),
  author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
  note TEXT
) STRICT;
CREATE INDEX idx_posts_author ON posts(author_id);
"""

ROWS = """
INSERT INTO "authors" ("id","handle","joined_at") VALUES(1,'a',100);
INSERT INTO "authors" ("id","handle","joined_at") VALUES(2,'b',NULL);
INSERT INTO "posts" ("id","author_id","note") VALUES(X'00000000000000000000000000000001',1,'x');
INSERT INTO "posts" ("id","author_id","note") VALUES(X'00000000000000000000000000000002',2,NULL);
DELETE FROM sqlite_sequence;
INSERT INTO "sqlite_sequence" ("name","seq") VALUES('authors',2);
"""

# The deploy lock of a database cloudflare-deploy-toolkit manages, as its own
# migration creates it.
LOCK_SCHEMA = """
CREATE TABLE _deploy_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  locked INTEGER NOT NULL DEFAULT 0
) STRICT;
"""
LOCK_ROW = """INSERT INTO "_deploy_lock" ("id","locked") VALUES(1,0);\n"""


def _load_one_statement_per_transaction(
    sql: pathlib.Path, database: pathlib.Path
) -> None:
    """Foreign keys enforced and every statement its own transaction.

    Stricter than D1, which commits in batches. The deferred-key pragma resets at
    every commit, so a file that loads this way is valid statement by statement
    and cannot depend on where D1 happens to put its batch boundaries.
    """
    connection = sqlite3.connect(database, autocommit=True)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(sql.read_text(encoding="utf-8"))
    finally:
        connection.close()


class _Databases(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = pathlib.Path(self._dir.name)
        self._count = 0

    def _path(self, stem: str, suffix: str) -> pathlib.Path:
        self._count += 1
        return self.root / f"{stem}-{self._count}{suffix}"

    def database(self, sql: str | bytes) -> pathlib.Path:
        dump = self._path("dump", ".sql")
        if isinstance(sql, bytes):
            dump.write_bytes(sql)
        else:
            dump.write_text(sql, encoding="utf-8")
        database = self._path("db", ".db")
        snapshot.load_dump(dump, database)
        return database

    def copied(self, source: pathlib.Path) -> pathlib.Path:
        """What a target holds once the regenerated import file has run on it."""
        import_sql = self._path("import", ".sql")
        snapshot.write_import_sql(source, import_sql)
        target = self._path("target", ".db")
        _load_one_statement_per_transaction(import_sql, target)
        return target

    def differences(self, source_sql: str, target_sql: str) -> list[str]:
        return [
            str(difference)
            for difference in snapshot.compare(
                self.database(source_sql), self.database(target_sql)
            )
        ]


class CompareTest(_Databases):
    def test_an_exact_copy_has_no_differences(self) -> None:
        self.assertEqual(self.differences(SCHEMA + ROWS, SCHEMA + ROWS), [])

    def test_a_changed_value_under_an_unchanged_key_is_found(self) -> None:
        """Counts and keys agree; only a row-value comparison sees this one."""
        changed = ROWS.replace("VALUES(1,'a',100)", "VALUES(1,'a',101)")
        found = self.differences(SCHEMA + ROWS, SCHEMA + changed)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("authors:"))

    def test_a_difference_never_carries_a_row_value(self) -> None:
        """The working databases hold the database's data, and this output can
        reach a CI log."""
        marker = "'never-print-this-handle'"
        changed = ROWS.replace("'a'", marker)
        found = "\n".join(self.differences(SCHEMA + ROWS, SCHEMA + changed))
        self.assertIn("authors", found)
        self.assertNotIn("never-print-this-handle", found)

    def test_a_row_missing_from_the_target_is_found(self) -> None:
        dropped = ROWS.replace(
            """INSERT INTO "authors" ("id","handle","joined_at") VALUES(2,'b',NULL);\n""",
            "",
        ).replace(
            """INSERT INTO "posts" ("id","author_id","note") VALUES(X'00000000000000000000000000000002',2,NULL);\n""",
            "",
        )
        found = self.differences(SCHEMA + ROWS, SCHEMA + dropped)
        self.assertEqual(
            sorted(line.split(":")[0] for line in found), ["authors", "posts"]
        )

    def test_insertion_order_of_blob_keyed_rows_does_not_matter(self) -> None:
        """A BLOB-keyed table has an implicit rowid a dump does not carry, so an
        import assigns fresh ones; comparing by rowid would fail every copy."""
        lines = ROWS.strip().split("\n")
        first, second = lines[2], lines[3]
        self.assertIn("posts", first)
        reordered = "\n".join([*lines[:2], second, first, *lines[4:]]) + "\n"
        self.assertEqual(self.differences(SCHEMA + ROWS, SCHEMA + reordered), [])

    def test_a_lost_sequence_high_water_mark_is_found_with_every_row_equal(
        self,
    ) -> None:
        """The source issued id 3 and deleted it. A target whose mark restarts lower
        hands that id to the next insert, colliding with anything that kept it."""
        source = ROWS.replace("VALUES('authors',2)", "VALUES('authors',3)")
        found = self.differences(SCHEMA + source, SCHEMA + ROWS)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("sqlite_sequence:"))

    def test_integers_beyond_double_precision_are_compared_exactly(self) -> None:
        """2^53 and 2^53 + 1 are one number to anything that parsed them as a
        double. The local comparison is exact; D1's export is not, which is what
        UnsafeIntegerTest is about."""
        source = ROWS.replace("VALUES(1,'a',100)", "VALUES(1,'a',9007199254740993)")
        target = ROWS.replace("VALUES(1,'a',100)", "VALUES(1,'a',9007199254740992)")
        self.assertEqual(len(self.differences(SCHEMA + source, SCHEMA + target)), 1)

    def test_a_missing_index_is_found(self) -> None:
        """Every row equal and every query still correct, only slower: the
        difference a data comparison alone would never report."""
        without_index = SCHEMA.replace(
            "CREATE INDEX idx_posts_author ON posts(author_id);\n", ""
        )
        found = self.differences(SCHEMA + ROWS, without_index + ROWS)
        self.assertEqual(len(found), 1)
        self.assertIn("idx_posts_author", found[0])

    def test_a_real_value_is_refused_rather_than_compared(self) -> None:
        """A dump writes a REAL as text. Source and target would load the same text and
        agree, so equality cannot show the value survived; refusing can."""
        loose = SCHEMA.replace(
            "joined_at INTEGER\n) STRICT;", "joined_at INTEGER\n);", 1
        )
        real = ROWS.replace("VALUES(1,'a',100)", "VALUES(1,'a',0.1)")
        found = self.differences(loose + real, loose + real)
        self.assertTrue(found)
        self.assertTrue(all("REAL" in line for line in found))

    def test_an_orphaned_child_in_the_target_is_found(self) -> None:
        """Deferred foreign keys let an import commit what a live write could not."""
        orphan = ROWS.replace(
            "X'00000000000000000000000000000002',2,NULL",
            "X'00000000000000000000000000000002',99,NULL",
        )
        found = self.differences(SCHEMA + ROWS, SCHEMA + orphan)
        self.assertTrue(any("foreign-key" in line for line in found))


class DeployLockTest(_Databases):
    """The one table this tool knows by name: cloudflare-deploy-toolkit's lock.

    Its Worker refuses requests while the row reads locked, so a copy that held
    data and read unlocked would be open before anyone chose to open it.
    """

    LOCKED = SCHEMA + LOCK_SCHEMA + ROWS + LOCK_ROW

    def test_an_unlocked_source_yields_a_locked_target_in_the_import_itself(
        self,
    ) -> None:
        """Locked by the same all-or-nothing import that writes the rows, so no
        failure between two steps can leave a full copy open."""
        source = self.database(self.LOCKED)
        target = self.copied(source)
        connection = sqlite3.connect(target)
        try:
            (locked,) = connection.execute("SELECT locked FROM _deploy_lock").fetchone()
        finally:
            connection.close()
        self.assertEqual(locked, 1)
        # Still an exact copy: the lock row is control state, not data.
        self.assertEqual(snapshot.compare(source, target), [])

    def test_a_held_target_lock_is_not_a_difference(self) -> None:
        held = self.LOCKED.replace(
            '("id","locked") VALUES(1,0)', '("id","locked") VALUES(1,1)'
        )
        self.assertEqual(self.differences(self.LOCKED, held), [])

    def test_a_target_without_its_lock_row_is_a_difference(self) -> None:
        """Excluded from equality is not excluded from existence: a target with
        no lock row answers every request as locked, forever."""
        unlocked = self.LOCKED.replace(LOCK_ROW, "")
        found = self.differences(self.LOCKED, unlocked)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("_deploy_lock:"))

    def test_a_lock_table_on_one_side_only_is_a_difference(self) -> None:
        found = self.differences(self.LOCKED, SCHEMA + ROWS)
        self.assertIn("_deploy_lock: missing from the target", found)

    def test_a_database_without_the_table_is_an_ordinary_copy(self) -> None:
        """Nothing about the lock applies to a database that never had one."""
        source = self.database(SCHEMA + ROWS)
        target = self.copied(source)
        self.assertFalse(snapshot.has_lock_table(target))
        self.assertEqual(snapshot.compare(source, target), [])


# The order a D1 export can really have: `comments` sorts and is cataloged before
# the `authors` it references, so its rows arrive while that table does not yet
# exist.
CHILD_FIRST = """
PRAGMA defer_foreign_keys=TRUE;
CREATE TABLE comments (
  id BLOB PRIMARY KEY,
  author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
  body TEXT
) STRICT;
INSERT INTO "comments" ("id","author_id","body") VALUES(X'0a0b',7,'it''s two
lines');
INSERT INTO "comments" ("id","author_id","body") VALUES(X'0c',7,NULL);
CREATE TABLE authors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  big INTEGER
) STRICT;
INSERT INTO "authors" ("id","big") VALUES(7,9007199254740993);
CREATE INDEX idx_comments_author ON comments(author_id);
DELETE FROM sqlite_sequence;
INSERT INTO "sqlite_sequence" ("name","seq") VALUES('authors',12);
"""


class WriteImportSqlTest(_Databases):
    """The import file is regenerated because D1 refuses its own export's order.

    The regenerated file is loaded here with foreign keys ENFORCED, which a
    plain SQLite connection does not do and is the whole reason the raw dump
    looks fine locally while failing remotely - and one statement per
    transaction, which is stricter than D1. The deferred-key pragma resets at
    every commit, so loading this way passes only if the file's own order is
    safe, whatever D1 does about transactions inside an import.
    """

    def test_regenerated_sql_loads_under_enforcement_and_is_an_exact_copy(self) -> None:
        source = self.database(CHILD_FIRST)
        # Exact, including the pieces a careless regeneration drops: the
        # apostrophe and newline inside a string, a blob key, a NULL, an integer
        # no double can hold, the index, and a sequence mark that is higher than
        # any surviving row.
        self.assertEqual(snapshot.compare(source, self.copied(source)), [])

    def test_the_raw_export_order_is_what_enforcement_refuses(self) -> None:
        """Pins the premise, so that if the platform ever stops refusing this the
        regeneration is known to be removable rather than kept as superstition."""
        enforcing = sqlite3.connect(":memory:")
        try:
            enforcing.execute("PRAGMA foreign_keys=ON")
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                enforcing.executescript(CHILD_FIRST)
        finally:
            enforcing.close()


# A cycle: an author optionally features one of their posts, and a post must
# name an author and an editor. Two more tables hang off `authors`, one of them
# keyed by its foreign key.
CYCLE = """
PRAGMA defer_foreign_keys=TRUE;
CREATE TABLE authors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  featured BLOB REFERENCES posts(id) ON DELETE SET NULL
) STRICT;
CREATE TABLE posts (
  id BLOB PRIMARY KEY,
  author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
  editor_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE
) STRICT;
CREATE TABLE comments (
  id INTEGER PRIMARY KEY,
  author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE
) STRICT;
CREATE TABLE settings (
  author_id INTEGER PRIMARY KEY REFERENCES authors(id) ON DELETE CASCADE,
  quiet INTEGER
) STRICT;
INSERT INTO "authors" ("id","featured") VALUES(1,X'aa');
INSERT INTO "authors" ("id","featured") VALUES(2,NULL);
INSERT INTO "posts" ("id","author_id","editor_id") VALUES(X'aa',1,2);
INSERT INTO "comments" ("id","author_id") VALUES(10,1);
INSERT INTO "comments" ("id","author_id") VALUES(11,2);
INSERT INTO "settings" ("author_id","quiet") VALUES(2,1);
"""


class ForeignKeyCycleTest(_Databases):
    def test_a_cycle_is_broken_at_its_nullable_column_and_restored(self) -> None:
        """With `authors` stuck behind the cycle, every table that depends on it
        would be written ahead of it. And `settings` is the shape that fools a
        naive fix: its key is also its foreign key, SQLite calls an INTEGER
        PRIMARY KEY nullable, and a NULL written there becomes an invented rowid
        pointing at no author."""
        source = self.database(CYCLE)
        # Exact includes the restored pointer: a target whose authors kept NULL
        # would load cleanly and be wrong.
        self.assertEqual(snapshot.compare(source, self.copied(source)), [])

    def test_a_cycle_with_no_nullable_column_is_refused(self) -> None:
        """No order writes it one statement at a time, so saying so beats
        producing a file that fails part-way through an import."""
        unbreakable = CYCLE.replace(
            "featured BLOB REFERENCES", "featured BLOB NOT NULL REFERENCES"
        ).replace("VALUES(2,NULL)", "VALUES(2,X'aa')")
        source = self.database(unbreakable)
        with self.assertRaisesRegex(snapshot.SnapshotError, "cycle"):
            snapshot.write_import_sql(source, self.root / "import.sql")


class ForeignKeyPlanTest(_Databases):
    """Which foreign keys order the rows, and which columns may be held back to
    break a cycle. Each case is a schema D1 accepts and the import file once
    got wrong; each copy is loaded with foreign keys enforced, one statement at
    a time."""

    def assert_copies_exactly(self, dump: str) -> None:
        source = self.database(dump)
        self.assertEqual(snapshot.compare(source, self.copied(source)), [])

    def test_a_reference_naming_its_parent_in_another_case_still_orders_it(
        self,
    ) -> None:
        """SQLite matches the name without regard to case, so the parent must
        still be written first."""
        self.assert_copies_exactly(
            "CREATE TABLE letters (id INTEGER PRIMARY KEY, "
            "writer INTEGER NOT NULL REFERENCES PEOPLE(ID)) STRICT;\n"
            "CREATE TABLE people (id INTEGER PRIMARY KEY) STRICT;\n"
            'INSERT INTO "people" ("id") VALUES(1);\n'
            'INSERT INTO "letters" ("id","writer") VALUES(1,1);\n'
        )

    def test_names_alike_only_beyond_ascii_case_stay_apart(self) -> None:
        """SQLite folds case for ASCII letters only, so `k` and the Kelvin sign
        name different tables; folded together, this chain would read as a
        cycle with no break and be refused."""
        kelvin = "\u212a"
        self.assert_copies_exactly(
            f'CREATE TABLE "{kelvin}" (id INTEGER PRIMARY KEY, '
            "a_id INTEGER NOT NULL REFERENCES a(id)) STRICT;\n"
            "CREATE TABLE a (id INTEGER PRIMARY KEY, "
            "k_id INTEGER NOT NULL REFERENCES k(id)) STRICT;\n"
            "CREATE TABLE k (id INTEGER PRIMARY KEY) STRICT;\n"
            'INSERT INTO "k" ("id") VALUES(1);\n'
            'INSERT INTO "a" ("id","k_id") VALUES(1,1);\n'
            f'INSERT INTO "{kelvin}" ("id","a_id") VALUES(1,1);\n'
        )

    def test_a_table_named_like_a_pragma_function_is_copied(self) -> None:
        """An application table takes precedence over the table-valued function
        of the same name, so the plan must not read SQLite's metadata through
        one."""
        self.assert_copies_exactly(
            "CREATE TABLE pragma_table_list (k TEXT PRIMARY KEY, v TEXT);\n"
            'INSERT INTO "pragma_table_list" ("k","v") VALUES(\'a\',\'b\');\n'
        )

    def test_a_cycle_breaks_at_one_nullable_column_of_a_composite_key(
        self,
    ) -> None:
        """SQLite checks a composite key only when every column holds a value,
        so one NULL column is enough to write the row first."""
        self.assert_copies_exactly(
            "CREATE TABLE shelves (id INTEGER PRIMARY KEY, room INTEGER NOT NULL, "
            "spot INTEGER, FOREIGN KEY (room, spot) REFERENCES places(room, spot)) "
            "STRICT;\n"
            "CREATE TABLE places (room INTEGER NOT NULL, spot INTEGER NOT NULL, "
            "shelf INTEGER NOT NULL REFERENCES shelves(id), PRIMARY KEY (room, spot)) "
            "STRICT;\n"
            'INSERT INTO "shelves" ("id","room","spot") VALUES(1,4,7);\n'
            'INSERT INTO "places" ("room","spot","shelf") VALUES(4,7,1);\n'
        )

    def test_a_column_another_key_points_at_is_never_held_back(self) -> None:
        """Held back, it would be NULL when the rows that point at it are
        written; the cycle is broken at a column nothing points at instead."""
        self.assert_copies_exactly(
            "CREATE TABLE authors (id INTEGER PRIMARY KEY, "
            "featured BLOB UNIQUE REFERENCES posts(id)) STRICT;\n"
            "CREATE TABLE posts (id BLOB PRIMARY KEY, "
            "author_id INTEGER REFERENCES authors(id)) STRICT;\n"
            "CREATE TABLE quotes (id INTEGER PRIMARY KEY, "
            "featured BLOB NOT NULL REFERENCES authors(featured)) STRICT;\n"
            'INSERT INTO "authors" ("id","featured") VALUES(1,X\'aa\');\n'
            'INSERT INTO "posts" ("id","author_id") VALUES(X\'aa\',1);\n'
            'INSERT INTO "quotes" ("id","featured") VALUES(1,X\'aa\');\n'
        )

    def test_a_cycle_that_breaks_only_where_a_key_can_hold_null_is_refused(
        self,
    ) -> None:
        """A held-back column is restored by primary key, and a row whose key
        is NULL cannot be found that way. SQLite allows that key only in an
        ordinary table, outside a lone INTEGER PRIMARY KEY - and one declared
        DESC is not the rowid, so it allows it too."""
        for key in ("k TEXT PRIMARY KEY", "k INTEGER PRIMARY KEY DESC"):
            with self.subTest(key=key):
                source = self.database(
                    f"CREATE TABLE nodes ({key}, link INTEGER REFERENCES links(id));\n"
                    "CREATE TABLE links (id INTEGER PRIMARY KEY, "
                    "node NOT NULL REFERENCES nodes(k));\n"
                )
                with self.assertRaisesRegex(snapshot.SnapshotError, "cycle"):
                    snapshot.write_import_sql(source, self.root / "import.sql")
        self.assert_copies_exactly(
            "CREATE TABLE nodes (k INTEGER PRIMARY KEY, "
            "link INTEGER REFERENCES links(id));\n"
            "CREATE TABLE links (id INTEGER PRIMARY KEY, "
            "node INTEGER NOT NULL REFERENCES nodes(k));\n"
            'INSERT INTO "nodes" ("k","link") VALUES(1,NULL);\n'
            'INSERT INTO "links" ("id","node") VALUES(1,1);\n'
            'UPDATE "nodes" SET "link" = 1;\n'
        )


class UniqueParentIndexTest(_Databases):
    """A foreign key may point at a key that only a separate CREATE UNIQUE INDEX
    makes unique. SQLite refuses a child row until that index exists, so the
    import file cannot leave it until after the rows."""

    DUMP = (
        "CREATE TABLE codes (id INTEGER PRIMARY KEY, code TEXT NOT NULL, "
        "note TEXT) STRICT;\n"
        "CREATE TABLE uses (id INTEGER PRIMARY KEY, "
        "code TEXT NOT NULL REFERENCES codes(code)) STRICT;\n"
        'INSERT INTO "codes" ("id","code","note") VALUES(1,\'a\',\'x\');\n'
        'INSERT INTO "uses" ("id","code") VALUES(1,\'a\');\n'
        "CREATE INDEX idx_codes_note ON codes(note);\n"
        "CREATE UNIQUE INDEX idx_codes_code ON codes(code);\n"
        "CREATE INDEX idx_uses_code ON uses(code);\n"
    )

    def test_the_parent_key_index_exists_before_the_child_rows(self) -> None:
        source = self.database(self.DUMP)
        self.assertEqual(snapshot.compare(source, self.copied(source)), [])

    def test_that_tables_indexes_keep_the_sources_order(self) -> None:
        """All of the parent table's indexes move together, so SQLite still
        settles an exact cost tie between two of them as the source does."""
        source = self.database(self.DUMP)
        target = self.copied(source)
        connection = sqlite3.connect(target)
        try:
            names = [
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'index' "
                    "AND tbl_name = 'codes' ORDER BY rowid"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(names, ["idx_codes_note", "idx_codes_code"])

    def test_a_key_unique_by_constraint_moves_nothing(self) -> None:
        """A UNIQUE constraint exists with its table, so the indexes stay after
        the rows, as they always have."""
        dump = self.DUMP.replace(
            "code TEXT NOT NULL, note", "code TEXT NOT NULL UNIQUE, note"
        )
        source = self.database(dump)
        import_sql = self.root / "import.sql"
        snapshot.write_import_sql(source, import_sql)
        lines = import_sql.read_text(encoding="utf-8").splitlines()
        first_row = next(i for i, line in enumerate(lines) if line.startswith("INSERT"))
        first_index = next(
            i for i, line in enumerate(lines) if line.startswith("CREATE INDEX")
        )
        self.assertLess(first_row, first_index)


class EmbeddedNulTest(_Databases):
    """TEXT can hold a NUL whenever callers can store arbitrary strings.

    D1 keeps such a value and exports it faithfully, as the raw byte inside the
    literal. The loader and the renderer each have a way to mishandle it, and
    neither would show up until a caller sent one.
    """

    # The shape a D1 export of such a row takes, byte for byte.
    DUMP = (
        b"PRAGMA defer_foreign_keys=TRUE;\n"
        b"CREATE TABLE authors (\n  id INTEGER PRIMARY KEY,\n  name TEXT UNIQUE\n) STRICT;\n"
        b'INSERT INTO "authors" ("id","name") VALUES(1,\'ab\x00cd\');\n'
        b'INSERT INTO "authors" ("id","name") VALUES(2,\'it\'\'s\x00\x00x\');\n'
        b'INSERT INTO "authors" ("id","name") VALUES(3,\'plain\');\n'
    )

    def test_an_export_holding_a_raw_nul_loads_with_the_value_intact(self) -> None:
        """Python refuses to execute SQL containing the byte, so without care the
        first such value stops a copy at its first step."""
        source = self.database(self.DUMP)
        connection = sqlite3.connect(source)
        try:
            held = connection.execute(
                "SELECT id, hex(name), typeof(name) FROM authors ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            held,
            [
                (1, b"ab\x00cd".hex().upper(), "text"),
                (2, b"it's\x00\x00x".hex().upper(), "text"),
                (3, b"plain".hex().upper(), "text"),
            ],
        )

    def test_the_import_file_carries_the_value_exactly_and_holds_no_raw_nul(
        self,
    ) -> None:
        """SQLite's quote() renders 'ab<NUL>cd' as 'ab'. A copy built on it would
        shorten the value in the target and report nothing at all while doing so."""
        source = self.database(self.DUMP)
        import_sql = self.root / "import.sql"
        snapshot.write_import_sql(source, import_sql)
        self.assertNotIn(b"\x00", import_sql.read_bytes())
        self.assertEqual(snapshot.compare(source, self.copied(source)), [])

    def test_a_shortened_value_in_the_target_is_a_difference(self) -> None:
        """The comparison has to see what quote() would have done, or a
        regression to it would pass its own verification."""
        source = self.database(self.DUMP)
        short = self.database(self.DUMP.replace(b"'ab\x00cd'", b"'ab'"))
        found = [str(d) for d in snapshot.compare(source, short)]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("authors:"))


class RefusedBeforeWritingTest(_Databases):
    def test_a_real_value_stops_the_import_file_from_being_written(self) -> None:
        """compare() would report it too, but only after the target had been
        filled. Rendering is the last step before anything is touched, so this
        is where it has to stop."""
        loose = SCHEMA.replace(
            "joined_at INTEGER\n) STRICT;", "joined_at INTEGER\n);", 1
        )
        real = ROWS.replace("VALUES(1,'a',100)", "VALUES(1,'a',0.1)")
        source = self.database(loose + real)
        with self.assertRaisesRegex(snapshot.SnapshotError, "authors.joined_at"):
            snapshot.write_import_sql(source, self.root / "import.sql")


# Lines of an export taken from a remote D1 database, kept byte for byte
# (tests/data/remote-export-text.sql), and what the same database answered for
# hex(t) when read live. The two were captured from the same database, so the
# export's claim to carry text exactly is checked against D1 itself rather than
# against a model of it.
REMOTE_EXPORT = pathlib.Path(__file__).parent / "data" / "remote-export-text.sql"
REMOTE_TEXT_HEX = {
    1: "610A62",  # lf_only
    2: "615C6E62",  # lit_n_only
    3: "610A625C6E63",  # lf_and_lit_n
    4: "610D62",  # cr_only
    5: "615C7262",  # lit_r_only
    6: "610D625C7263",  # cr_and_lit_r
    7: "610D0A625C725C6E63",  # crlf_and_lit_rn
    8: "610A625C7263",  # lf_and_lit_r
    9: "615C0A62",  # backslash_then_lf
    10: "615C5C6E0A62",  # lit_bs_bs_n_and_lf
    13: "610A625C6E635C30313264",  # lf_lit_n_lit_012
    14: "610A5C6E5C3031325C303031325C30303031325C7830615C75303030615C31305C3132",  # lf_many_sentinels
    15: "610D625C72635C30313564",  # cr_lit_r_lit_015
    16: "610A625C30313263",  # lf_lit_012_only
    17: "610A625C",  # lf_trailing_bs
    18: "615C0A5C6E62",  # bs_lf_lit_n
    19: "610A5C5C303132",  # lf_bs_012
    20: "610A5C6E5C5C303132",  # lf_lit_n_bs_012
    21: "610A0D5C6E5C725C3031325C3031355C303031325C30303135",  # lf_cr_both_lits_and_octals
    22: "5C6E5C3031320A",  # lit_012_trailing_lf
    23: "610A5C6E5C303132285C6E3029",  # lf_lits_n_012_paren0
    24: "610A5C6E5C303132285C6E3029285C6E3129285C6E3229285C6E3329285C6E3429285C6E3529285C6E3629285C6E3729285C6E3829285C6E3929285C6E313029285C6E313129",  # lf_lits_paren0_to_9
    25: "285C6E0A30295C6E5C303132285C6E300A29",  # lf_paren_fragments
    26: "285C7230295C725C3031355C6E5C303132285C6E30290D0A0D",  # cr_lf_paren_cross
    27: "5C285C6E30290A5C6E5C303132",  # lf_lit_bs_paren
}


class RemoteExportTest(_Databases):
    def _text_hex(self, database: pathlib.Path) -> dict[int, str]:
        connection = sqlite3.connect(database)
        try:
            return dict(connection.execute("SELECT id, hex(t) FROM s"))
        finally:
            connection.close()

    def test_text_with_line_breaks_beside_their_escapes_loads_back_exactly(
        self,
    ) -> None:
        """The reason text is not refused: remote D1 escapes each line break
        with something the value does not hold, so every one of these - a
        literal backslash-n beside a newline, and the values built to make an
        escape match in the wrong place - reads back as D1 held it."""
        source = self.root / "source.db"
        snapshot.load_dump(REMOTE_EXPORT, source)
        self.assertEqual(self._text_hex(source), REMOTE_TEXT_HEX)

    def test_the_copy_of_that_text_is_exact(self) -> None:
        source = self.root / "source.db"
        snapshot.load_dump(REMOTE_EXPORT, source)
        # The captured rows carry REAL values too, which a copy refuses; only
        # the text is under test here.
        connection = sqlite3.connect(source)
        try:
            for column in ("r", "i", "a"):
                connection.execute(f"ALTER TABLE s DROP COLUMN {column}")
            connection.commit()
        finally:
            connection.close()
        target = self.copied(source)
        self.assertEqual(snapshot.compare(source, target), [])
        self.assertEqual(self._text_hex(target), REMOTE_TEXT_HEX)

    def test_the_stand_in_writes_text_as_remote_d1_does(self) -> None:
        """tests/fake_d1.py's export is what the command-line tests copy
        through, so it is held to the captured bytes."""
        lines = REMOTE_EXPORT.read_text(encoding="ascii").splitlines()
        for row, hexed in REMOTE_TEXT_HEX.items():
            with self.subTest(row=row):
                written = exported(bytes.fromhex(hexed).decode())
                (line,) = [
                    line
                    for line in lines
                    if line.startswith('INSERT INTO "s" ') and f"VALUES({row}," in line
                ]
                self.assertIn(f",{written},", line)


class UnexportableValueTest(unittest.TestCase):
    """The values an export does not carry exactly, asked of a live database.

    The source's export and the target's would agree on each changed value, so
    none of these can be caught by comparing them. They are caught by asking
    the live source first, and this is the question that gets asked.
    """

    def _found(self, *values: object) -> list[str]:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            # Not STRICT, and `v` has no declared type, so a value of any
            # storage class can sit in it the way only a STRICT table prevents.
            connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v)")
            connection.executemany(
                "INSERT INTO t (v) VALUES (?)", [(value,) for value in values]
            )
            answer = connection.execute(
                snapshot.unexportable_sql("t", ["id", "v"])
            ).fetchone()
            return snapshot.unexportable_values("t", ["id", "v"], dict(answer))
        finally:
            connection.close()

    def test_every_value_an_export_carries_exactly_is_accepted(self) -> None:
        limit = snapshot.EXPORT_SAFE_INTEGER
        self.assertEqual(
            self._found(
                0,
                limit,
                -limit,
                None,
                b"\x00\n",
                "a\nb",
                "a\\nb",
                "a\r\nb",
                "a\\rb",
                # Remote D1's export escapes a line break with something the
                # value does not hold, so the literal beside it survives.
                "a\\nb\nc",
                "a\\rb\rc",
            ),
            [],
        )

    def test_the_first_integer_beyond_2_53_is_found_in_either_direction(self) -> None:
        """2^53 + 1 leaves an export as 2^53."""
        limit = snapshot.EXPORT_SAFE_INTEGER
        self.assertEqual(self._found(limit + 1), ["t.v holds an integer beyond 2^53"])
        self.assertEqual(self._found(-limit - 1), ["t.v holds an integer beyond 2^53"])

    def test_the_most_negative_int64_is_found_rather_than_raising(self) -> None:
        """abs() of it overflows and SQLite raises instead of answering, which
        would turn the one value most worth catching into a crashed check."""
        self.assertTrue(self._found(-(2**63)))
        self.assertTrue(self._found(2**63 - 1))

    def test_a_whole_number_real_is_found(self) -> None:
        """Written as `1`, it reads back into a column of no declared type as
        the INTEGER 1, and both exports agree on the INTEGER."""
        self.assertEqual(self._found(1.0), ["t.v holds a REAL value"])

    def test_an_answer_missing_a_column_is_refused_rather_than_read_as_clean(
        self,
    ) -> None:
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.unexportable_values("t", ["id", "v"], {"i0": 0, "r0": 0, "i1": 0})

    def test_the_finding_names_the_column_and_never_the_value(self) -> None:
        value = snapshot.EXPORT_SAFE_INTEGER + 123457
        found = "".join(self._found(value))
        self.assertIn("t.v", found)
        self.assertNotIn(str(value), found)


class UnexportableValueInADumpTest(_Databases):
    """A dump file has no live origin to ask, so it is judged once loaded.

    A dump holding such a value imports exactly. What cannot happen is
    confirming it: the target is read back through an export, which changes the
    value, so the copy would be reported as differing when it does not, or - for
    a value that rounds to itself on each side - as exact when nobody checked.
    """

    def _found(self, rows: str) -> list[str]:
        return snapshot.values_an_export_would_change(self.database(SCHEMA + rows))

    def test_a_dump_of_ordinary_values_names_nothing(self) -> None:
        self.assertEqual(self._found(ROWS), [])

    def test_the_column_holding_one_is_named_and_no_other(self) -> None:
        beyond = ROWS.replace(
            "VALUES(1,'a',100)", f"VALUES(1,'a',{snapshot.EXPORT_SAFE_INTEGER + 1})"
        )
        self.assertEqual(
            self._found(beyond), ["authors.joined_at holds an integer beyond 2^53"]
        )

    def test_planner_statistics_are_checked_like_any_copied_table(self) -> None:
        """sqlite_stat1 is copied and compared, and its columns have no declared
        type, so it can hold a value an export changes like any other table."""
        database = self.database(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, k INTEGER);\n"
            "CREATE INDEX idx_t ON t(k);\n"
            "ANALYZE sqlite_schema;\n"
            f"INSERT INTO sqlite_stat1 VALUES('t', 'idx_t', {snapshot.EXPORT_SAFE_INTEGER + 1});\n"
        )
        found = snapshot.values_an_export_would_change(database)
        self.assertIn("sqlite_stat1.stat holds an integer beyond 2^53", found)

    def test_a_sequence_mark_beyond_it_is_found_too(self) -> None:
        """sqlite_sequence is copied like any table and read back the same way."""
        beyond = ROWS.replace(
            "VALUES('authors',2)",
            f"VALUES('authors',{snapshot.EXPORT_SAFE_INTEGER + 1})",
        )
        self.assertEqual(
            self._found(beyond), ["sqlite_sequence.seq holds an integer beyond 2^53"]
        )


# Indexes the planner ranks one way with no statistics and the other way with
# these. Left alone it prefers the index that pins the most columns; told that
# `sensor` is nearly unique and that the wide index narrows almost nothing, it
# has to switch. The wide index pins enough columns that the unanalyzed choice
# does not turn on which index was created later, as it does against an index
# that pins one column fewer (IndexOrderTest).
PLANNED = """
CREATE TABLE readings (
  id INTEGER PRIMARY KEY,
  sensor INTEGER NOT NULL,
  day INTEGER NOT NULL,
  kind INTEGER NOT NULL,
  site INTEGER NOT NULL
) STRICT;
CREATE INDEX idx_readings_sensor ON readings(sensor);
CREATE INDEX idx_readings_day_kind_site ON readings(day, kind, site);
"""
STATISTICS = """
ANALYZE sqlite_schema;
INSERT INTO "sqlite_stat1" ("tbl","idx","stat") VALUES('readings','idx_readings_sensor','1000000 1');
INSERT INTO "sqlite_stat1" ("tbl","idx","stat") VALUES('readings','idx_readings_day_kind_site','1000000 500000 250000 125000');
"""


class PlannerStatisticsTest(_Databases):
    """Copied statistics have to be in use, not merely present.

    A connection plans from the statistics it last loaded, and D1 was observed
    to ignore raw writes to sqlite_stat1 until a reload. Rows written there and
    never reloaded leave a target that compares as an exact copy and plans as
    though it had never been analyzed. No comparison of dumps can see that, so
    it is asked of the connection that did the loading.
    """

    _QUERY = (
        "EXPLAIN QUERY PLAN SELECT id FROM readings "
        "WHERE sensor = 1 AND day = 2 AND kind = 3 AND site = 4"
    )

    def _index_chosen(self, connection: sqlite3.Connection) -> str:
        plan = " ".join(str(row[3]) for row in connection.execute(self._QUERY))
        chosen = [
            name
            for name in ("idx_readings_sensor", "idx_readings_day_kind_site")
            if name in plan
        ]
        self.assertEqual(len(chosen), 1, plan)
        return chosen[0]

    def test_the_connection_that_loaded_the_import_plans_with_the_copied_statistics(
        self,
    ) -> None:
        source = self.database(SCHEMA + ROWS + PLANNED + STATISTICS)
        import_sql = self.root / "import.sql"
        snapshot.write_import_sql(source, import_sql)

        target_path = self.root / "target.db"
        target = sqlite3.connect(target_path, autocommit=True)
        try:
            target.execute("PRAGMA foreign_keys=ON")
            target.executescript(import_sql.read_text(encoding="utf-8"))
            self.assertEqual(self._index_chosen(target), "idx_readings_sensor")
        finally:
            target.close()

        # The reload reads the rows and writes none, so the copy is still exact.
        self.assertEqual(snapshot.compare(source, target_path), [])

        # The premise, or the assertion above proves nothing: this same
        # database, with its statistics taken away, plans the other way.
        bare = sqlite3.connect(target_path, autocommit=True)
        try:
            bare.executescript("DELETE FROM sqlite_stat1; ANALYZE sqlite_master;")
            self.assertEqual(self._index_chosen(bare), "idx_readings_day_kind_site")
        finally:
            bare.close()


class IndexOrderTest(_Databases):
    """A table's indexes are created in the order the source's export gave them.

    SQLite settles an exact cost tie between indexes of one table by which was
    created later, and an unanalyzed index on one column against one on a pair
    of columns is such a tie. So a copy that creates them in another order can
    plan a query differently from its source with every row and statistic equal.
    A comparison of dumps does not look at it.
    """

    @staticmethod
    def _created(database: pathlib.Path) -> list[str]:
        connection = sqlite3.connect(database)
        try:
            return [
                str(name)
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'index' "
                    "AND tbl_name = 'readings' AND sql IS NOT NULL ORDER BY rowid"
                )
            ]
        finally:
            connection.close()

    def test_a_tables_indexes_are_created_in_the_sources_order(self) -> None:
        source = self.database(SCHEMA + ROWS + PLANNED)
        created = self._created(source)
        # The premise: an order that sorting by name would not reproduce.
        self.assertNotEqual(created, sorted(created))
        self.assertEqual(self._created(self.copied(source)), created)


class LoadDumpTest(_Databases):
    def test_a_dump_that_attaches_another_file_is_refused_and_writes_nothing(
        self,
    ) -> None:
        """A restore file is SQL, and SQL can reach another database file on
        the machine. A D1 export never does, so one that tries is not run."""
        elsewhere = self.root / "elsewhere.db"
        connection = sqlite3.connect(elsewhere)
        connection.execute("CREATE TABLE kept (v)")
        connection.close()
        before = elsewhere.read_bytes()
        attaching = f"ATTACH '{elsewhere}' AS other;\nDELETE FROM other.kept;\n"
        with self.assertRaisesRegex(snapshot.SnapshotError, "another database file"):
            self.database(SCHEMA + ROWS + attaching)
        self.assertEqual(elsewhere.read_bytes(), before)

    def test_a_dump_that_defines_no_table_is_refused(self) -> None:
        """A failed export loads as an empty database, and an empty source and target
        compare equal - the one way a copy of nothing could pass."""
        with self.assertRaises(snapshot.SnapshotError):
            self.database("PRAGMA defer_foreign_keys=TRUE;\n")

    def test_loading_over_an_existing_database_is_refused(self) -> None:
        """Loading a second dump into the first one's file would compare a
        database against a mixture of itself."""
        dump = self.root / "dump.sql"
        dump.write_text(SCHEMA + ROWS, encoding="utf-8")
        snapshot.load_dump(dump, self.root / "once.db")
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.load_dump(dump, self.root / "once.db")


if __name__ == "__main__":
    unittest.main()
