# d1-copy

## What it is

A command that copies a [Cloudflare D1](https://developers.cloudflare.com/d1/)
database into an empty one, or restores a D1 export into an empty one, and then
proves the copy exact. It exports the target back and compares it with the
source, table by table and row by row, in local SQLite, and it exits non-zero on
any difference.

It is standard-library Python and talks to D1 through its REST API.

## Why it exists

D1 has an export and an import, but an export is not a file you can import
safely, and comparing two exports does not show that a copy is exact. Each trap
below was measured against remote D1, through the REST export and through
`wrangler d1 export --remote`, which wrote byte-identical files.

1. **D1 cannot import its own export.** An export writes each table's rows
   straight after that table's `CREATE TABLE`, in catalog order. A child table
   defined before its parent then has its rows inserted before the parent table
   exists, and the import stops with `no such table: main.parent`. This happened
   with a single child row, and nothing was applied. The export opens with
   `PRAGMA defer_foreign_keys=TRUE`, which does not help: that pragma defers
   the row check, not the lookup of the parent table. It also stops short of
   rows written ahead of a parent table that does exist. SQLite turns it off at
   every commit, and D1 runs a large import as several transactions: while this
   tool was being built, a handful of such child rows imported cleanly and a
   few thousand failed. Cloudflare's import documentation leaves the order to
   you: "ensure you are importing the tables in the right order. You cannot
   refer to a table that does not yet exist"
   ([import and export data](https://developers.cloudflare.com/d1/best-practices/import-export-data/)).

2. **An export changes any integer beyond 2^53, silently.** D1 stores a 64-bit
   integer exactly, but the export passes it through a double. 2^53 + 1 leaves
   as 2^53, still written as an integer, so the source's export and the copy's
   carry the same wrong number and agree, and no comparison of exports can
   catch it. The largest and smallest int64 leave as `9223372036854776000` and
   `-9223372036854776000`, which no 64-bit integer can hold. A STRICT `INTEGER`
   column refuses that on import, and any other column reads it back as a REAL.
   Cloudflare's documentation says large int64 values "may be less precise".
   The rounding is also reported in
   [cloudflare/workers-sdk#15378](https://github.com/cloudflare/workers-sdk/issues/15378).

3. **An export writes a whole-number REAL as an integer.** It writes a number
   as JavaScript prints it, so the REAL `1.0` is written as `1`. An `ANY`
   column or one with no declared type reads that back as the INTEGER `1`. A
   `REAL` column turns it back into `1.0`. Every other REAL measured
   (`0.1`, `0.30000000000000004`, `1e21`, `1e300`, the smallest subnormal and
   the largest double) read back as the same double. That rests on SQLite
   reading the decimal text back exactly, which this tool does not check.

4. **A raw NUL ends an import silently.** Text may hold a NUL byte, and the
   export writes it raw inside the string literal. D1's import of a file that
   holds it reports success and stops there. In the measurement, the statement
   holding the byte and everything after it were dropped: no error, and a
   table defined later in the file was never created.

Text with line breaks is carried exactly. Remote D1 writes a line break inside
text as an escape the value does not already hold: `\n`, or `\012` when the
value holds a literal `\n`, or `(\n0)`, `(\n1)` and on, and the same for a
carriage return. It wraps the literal in a `replace()` that turns back only
that escape. Values built to confuse it, a literal `\n` beside a newline among
them, came back byte for byte. The local export in Miniflare, which
`wrangler d1 export --local` uses, does not do this. It
[always writes `\n`](https://github.com/cloudflare/workers-sdk/blob/e35c4a154ea16a96b47cb2e68a4930c1d833e81d/packages/miniflare/src/workers/d1/dumpSql.ts#L205-L236),
so a literal backslash-n in a value that also holds a newline comes back as a
second newline. A local export passed to `restore` may already hold that
change, and nothing in the file can show it.

D1's other tools do not cover this. [Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/)
restores a database in place and "does not yet allow you to clone or fork an
existing database to a new copy". `wrangler d1 export` and the REST export
produce the file these traps are in.

## How to use it

```sh
python3 -m pip install "d1-copy @ git+https://github.com/curlewlabs-com/d1-copy@v0.2.0"

export CLOUDFLARE_ACCOUNT_ID=...
export CLOUDFLARE_API_TOKEN=...   # D1 Edit on the account

d1-copy copy    --source my-db --target my-db-copy
d1-copy restore --dump backup.sql --target my-db-restored
d1-copy verify  --source my-db --target my-db-copy
d1-copy check   --source my-db
```

`python3 -m d1_copy ...` is the same command. The command is the supported
interface. The Python modules behind it can be imported, but before 1.0 they
can change in any release, so pin an exact version if you use them.

The token needs D1 Edit (write access): the export endpoint is a write-classed
call even though it changes nothing. The token is sent only to the Cloudflare
API, never printed, and never sent to the pre-signed URLs an export is
downloaded from and an import is uploaded to.

### Commands

- **`copy --source NAME --target NAME`** exports the source, imports it into an
  empty target, exports the target back, and compares the two. Both names must
  each match exactly one database in the account.
- **`restore --dump FILE --target NAME`** is `copy` with an export file standing
  in for the source, for example a backup you kept, decompressed. It reads no
  live database except the target.
- **`verify --source NAME --target NAME`** exports both databases and compares
  them. It writes nothing.
- **`check --source NAME`** asks a live database whether it holds a value an
  export would change, and names each column that does. It writes nothing.

A difference is reported as a table and a count, and a refused value as its
table and column, never as a row's values. An error that D1 or SQLite reports
is passed on as reported (D1's cut short), and an SQL error can quote text near
its fault, which in an import can be part of a row: treat the output of a
failed run as you would the data.

Exit status: 0 when the copy is exact (or `check` finds nothing), 1 when it
differs or a call fails, 2 when a precondition refused the run before anything
was written.

### Before you run it

- **An export blocks the database's other queries while it runs.** `copy` and
  `verify` export the source, so pick the moment.
- **"Exact" is a claim about a source nobody is writing.** The tool reads the
  source more than once: the check of its values, then the export. A write that lands
  in between is in the copy or not, depending on when it arrived. Pausing the
  source's writers is up to you; nothing here does it.
- **The target must be empty.** Create a new database for it. The tool never
  drops or deletes anything, and D1 applies none of a failed import, so a failed
  run leaves the target as it was. The check that it is empty is a read made
  before the import, so do not write to a target while it is being filled. A
  table someone else creates there meanwhile is left as it is, sequence marks
  included, and the comparison then fails on it.

## How it works

The export is never imported as it arrived. It is loaded into a local SQLite
database (where foreign keys are not enforced, which is why it loads), and a new
import file is written from that database:

- **Every table is defined before any row is written**, and rows go in
  parents-first, read from SQLite's own `foreign_key_list`, with table and
  column names matched as SQLite matches them, without regard to case. Foreign
  keys that form a cycle are broken at a nullable column: that column is
  written as NULL and restored by `UPDATE` once every row exists. One NULL
  column of a composite key is enough, since SQLite checks such a key only
  when every column holds a value. A column is never held back when it cannot
  be restored safely: one in the primary key (SQLite reports an `INTEGER
PRIMARY KEY` as nullable, but a NULL written there becomes a new rowid), one
  another foreign key points at, or one in a table whose primary key can hold
  NULL, since the restore finds its row by that key. A cycle that leaves no
  such column cannot be written in any order and is refused.
- **`sqlite_sequence` is written after the rows**, because inserting moves it,
  and an AUTOINCREMENT high-water mark can be higher than the highest surviving
  row.
- **Indexes and triggers come after the rows**, in the order the source
  created them, because SQLite settles an exact cost tie between two indexes by
  which was created later. The one exception is an index a foreign key's parent
  key needs, when only a separate `CREATE UNIQUE INDEX` makes that key unique:
  SQLite refuses a child row until it exists. Which index that is, is asked of
  SQLite, by compiling each table's inserts against the tables alone. That
  index is created before the rows, then dropped and created again after them
  with the rest, so the source's order still holds.
- **`sqlite_stat1` is copied and then reloaded** with `ANALYZE sqlite_master`.
  D1 was observed to plan from the statistics it last loaded and to ignore raw
  writes to the table until that reload.
- **Text holding a control character is written as `CAST(X'...' AS TEXT)`.**
  SQLite's `quote()` stops at an embedded NUL, so `'ab<NUL>cd'` would become
  `'ab'`, and a raw NUL in the file would end D1's import there. D1 keeps such
  a value and exports the raw byte, which Python will not execute, so the
  loader swaps it for a token and puts it back afterwards.
- **A dump touches no other file.** It is run with a SQLite authorizer that
  refuses `ATTACH` and `DETACH`. A D1 export uses neither, and a restore file
  that does could otherwise reach another database file on the machine.

The comparison covers every application table as a multiset of rows (never by
rowid, which an import reassigns for a table keyed by a BLOB), `sqlite_sequence`,
`sqlite_stat1`, and the schema. It also runs `integrity_check` and
`foreign_key_check` on both sides.

What it refuses, each before the target is touched:

- a source and a target that are the same database;
- a target that already holds a table;
- a source holding a value an export would change, or that this tool cannot
  show it carries: an integer beyond 2^53, or a REAL value anywhere. See
  [Why it exists](#why-it-exists). `copy` and `verify` ask the live database,
  in three queries per database, before exporting it. `verify` asks
  both sides, because 2^53 + 1 and 2^53 export as the same number. `restore`
  asks the loaded dump. The live check reads each row of the database once,
  and the command prints what it read, since D1 meters rows read;
- foreign keys that form a cycle with no column that can be held back;
- a dump that tries to `ATTACH` another database file;
- an AUTOINCREMENT mark in `sqlite_sequence` whose name is not exactly that of
  a table being copied. SQLite lets an application write one, and finds a
  table's mark by its exact name, so such a mark belongs to no table here.
  Copied, it could land on a table of that name created on the target
  meanwhile and change the ids that table hands out. The refusal gives a
  count, never the names.

Other behaviour of the D1 API that the client handles:

- An import is keyed by the hash of its bytes. Sending a file D1 has already
  seen starts it running at once, with no upload. The client follows that
  import rather than reading it as a refusal.
- An import's failure arrives inside a successful API response, as a second
  success flag. The client reads it.
- A newly minted token can be refused for its first few seconds. The client
  repeats an authentication rejection a few times, even for a call that
  changes something, since a rejected call ran nothing.
- Reads are repeated on a transient failure (429, 5xx, a dropped connection).
  Starting an export and ingesting an import are not, because neither takes an
  idempotency key.

Dumps and working databases are full copies of the source. They live in a
private directory, `d1-copy` under the system temporary directory, which is
removed on every exit the process can observe, a terminating signal or Ctrl-C
included, and a signal that arrives during that removal waits until it is done.
A run that was killed outright leaves its directory behind, and the next run
removes it before starting. A lock file there stops a second run from starting
while one is under way. The tool creates `d1-copy` with mode 0700. If something
is already at that path that is not such a directory of yours - a symlink, a
file, or a directory another user owns or others can open - it refuses to run
until you remove it, since another account on the machine may have put it
there.

### Databases managed by cloudflare-deploy-toolkit

A database managed by
[cloudflare-deploy-toolkit](https://github.com/curlewlabs-com/cloudflare-deploy-toolkit)
carries a `_deploy_lock` table. When the source has it, the copy is written
locked, and the lock row is left out of the comparison; the table's presence
and its single row are still checked. Open the copy with the toolkit when you
are ready for it to serve. A database without that table is copied like any
other.

## What it does not do

- It does not copy a database that is still changing into an exact copy. See
  "Before you run it".
- It does not merge into, or overwrite, a database that holds anything.
- It does not carry integers beyond 2^53. The export rounds them, and the
  live check refuses a source that holds one rather than copy it changed.
- It does not carry REAL values. A whole-number REAL in an `ANY` or untyped
  column comes back as an INTEGER, and for the rest nothing here checks that
  the decimal text reads back as the same double. It refuses them.
- It cannot copy a database that holds a virtual table: D1's export does not
  support them.

## Requirements

- Python 3.12 or newer, with the `sqlite3` module.
- A Cloudflare API token with D1 Edit on the account.

## Known issues

Where one of these ends in a failed import, nothing is applied to the target:
D1 applies none of a failed import.

- **The NUL restoration runs after the dump's triggers exist.** It is done by
  `UPDATE` on the local working copy, so an `UPDATE` trigger in the schema runs
  there too, and can change that copy, on both sides alike. It also covers
  application tables only: a NUL in a table or index name recorded in
  `sqlite_sequence` or `sqlite_stat1` is left as the loader's placeholder.
  Putting the byte into each row as it loads, instead, means rewriting the
  dump's text where it is a row's value and nowhere else, which needs its
  statements read exactly as SQLite reads them; until that is done, the
  placeholder and its `UPDATE` stay.
- **A row that references a later row of its own table** is written in the
  table's own order, and D1's import then fails. Ordering such rows means
  finding each row's parent exactly as SQLite's foreign-key check does and
  telling every row apart, whatever the table names its columns; until that
  is done, the rows keep the table's order.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option.

### Contribution

By submitting a contribution for inclusion in this project, you agree that it
is dual licensed as above, without any additional terms or conditions.
