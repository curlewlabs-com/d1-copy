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
safely, and comparing two exports does not show that a copy is exact. Three
traps lie in the way:

1. **D1 cannot be trusted to import its own export.** An export writes each
   table's rows straight after that table's `CREATE TABLE`, in catalog order. A
   child table defined before its parent then has its rows inserted before the
   parent table exists. D1 enforces foreign keys, so the import stops with
   `no such table`. `PRAGMA defer_foreign_keys` does not rescue it. That pragma
   defers the row check, not the lookup of the parent table, and SQLite turns it
   off at every commit, which matters because D1 runs a large import as several
   transactions. A handful of child rows ahead of their parent imported cleanly
   under the pragma; a few thousand failed. Cloudflare's import documentation
   leaves the order to you: "ensure you are importing the tables in the right
   order. You cannot refer to a table that does not yet exist"
   ([import and export data](https://developers.cloudflare.com/d1/best-practices/import-export-data/)).
   The API reference now describes the default export format as one that "D1
   can import"
   ([export](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/export/)),
   so check this against your own databases. This tool was built against
   exports that failed in exactly this way.

2. **An export changes any integer beyond 2^53, silently.** D1 stores a 64-bit
   integer exactly, but the export passes it through a double: 2^53 + 1 leaves
   as 2^53, and the largest int64 leaves rounded, each still written as an
   integer. The source's export and the copy's export then carry the same wrong
   number and agree, so no comparison of exports can catch it. Cloudflare's
   documentation says large int64 values "may be less precise". The rounding
   was reported against remote D1 and the REST export in
   [cloudflare/workers-sdk#15378](https://github.com/cloudflare/workers-sdk/issues/15378).
   That issue was closed by a change that fixed only how `Infinity` is written
   in the local (Miniflare) export, and the change's own description leaves the
   int64 precision loss as "a separate, larger fix".

3. **An export can rewrite other values too.** Cloudflare's own export source,
   [`dumpSql.ts`](https://github.com/cloudflare/workers-sdk/blob/e35c4a154ea16a96b47cb2e68a4930c1d833e81d/packages/miniflare/src/workers/d1/dumpSql.ts) in Miniflare, its local implementation of the format,
   writes a number as JavaScript prints it
   ([lines 124-128](https://github.com/cloudflare/workers-sdk/blob/e35c4a154ea16a96b47cb2e68a4930c1d833e81d/packages/miniflare/src/workers/d1/dumpSql.ts#L124-L128)), so the REAL `1.0` is written as `1` and
   reads back into a column of no declared type as the INTEGER `1`. It writes a
   newline inside text as the two characters `\n` and wraps the literal in
   `replace(..., '\n', char(10))`
   ([lines 205-236](https://github.com/cloudflare/workers-sdk/blob/e35c4a154ea16a96b47cb2e68a4930c1d833e81d/packages/miniflare/src/workers/d1/dumpSql.ts#L205-L236)), so a literal backslash-n in the same
   value comes back as a second newline; a carriage return and `\r` go the same
   way. In both cases the source's export and the copy's agree on the changed
   value. This was read in that source, not measured against remote D1, and the
   tool refuses such values rather than assume remote D1 behaves differently.

None of these was re-measured against live D1 for this release. The rest of the
tool's behaviour follows from what was observed while building it, set out
under [How it works](#how-it-works).

D1's other tools do not cover this. [Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/)
restores a database in place and "does not yet allow you to clone or fork an
existing database to a new copy". `wrangler d1 export` and the REST export
produce the file these traps are in.

## How to use it

```sh
python3 -m pip install "d1-copy @ git+https://github.com/curlewlabs-com/d1-copy@v0.1.0"

export CLOUDFLARE_ACCOUNT_ID=...
export CLOUDFLARE_API_TOKEN=...   # D1 Edit on the account

d1-copy copy    --source my-db --target my-db-copy
d1-copy restore --dump backup.sql --target my-db-restored
d1-copy verify  --source my-db --target my-db-copy
d1-copy check   --source my-db
```

`python3 -m d1_copy ...` is the same command.

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
  run leaves the target as it was.

## How it works

The export is never imported as it arrived. It is loaded into a local SQLite
database (where foreign keys are not enforced, which is why it loads), and a new
import file is written from that database:

- **Every table is defined before any row is written**, and rows go in
  parents-first, read from SQLite's own `foreign_key_list`. Foreign keys that
  form a cycle are broken at a nullable column: that column is written as NULL
  and restored by `UPDATE` once every row exists. A cycle with no nullable
  column cannot be written in any order and is refused. An `INTEGER PRIMARY
KEY` is never held back, although SQLite reports it as nullable: a NULL
  written there becomes a new rowid rather than an absent value.
- **`sqlite_sequence` is written after the rows**, because inserting moves it,
  and an AUTOINCREMENT high-water mark can be higher than the highest surviving
  row.
- **Indexes and triggers come after the rows**, in the order the source
  created them, because SQLite settles an exact cost tie between two indexes by
  which was created later.
- **`sqlite_stat1` is copied and then reloaded** with `ANALYZE sqlite_master`.
  D1 was observed to plan from the statistics it last loaded and to ignore raw
  writes to the table until that reload.
- **Text holding a control character is written as `CAST(X'...' AS TEXT)`.**
  SQLite's `quote()` stops at an embedded NUL, so `'ab<NUL>cd'` would become
  `'ab'`. D1 keeps such a value and exports the raw byte, which Python will not
  execute, so the loader swaps it for a token and puts it back afterwards.

The comparison covers every application table as a multiset of rows (never by
rowid, which an import reassigns for a table keyed by a BLOB), `sqlite_sequence`,
`sqlite_stat1`, and the schema. It also runs `integrity_check` and
`foreign_key_check` on both sides.

What it refuses, each before the target is touched:

- a source and a target that are the same database;
- a target that already holds a table;
- a source holding a value an export would change: an integer beyond 2^53, a
  REAL value anywhere, or text holding both a newline and a literal `\n` (or a
  carriage return and a literal `\r`). See [Why it exists](#why-it-exists);
  a REAL is also refused because an export writes it as decimal text, which
  need not read back as the same double. `copy` and `verify` ask the live
  database, in three queries per database, before exporting it. `verify` asks
  both sides, because 2^53 + 1 and 2^53 export as the same number. `restore`
  asks the loaded dump. The live check reads each row of the database once,
  and the command prints what it read, since D1 meters rows read;
- foreign keys that form a cycle with no nullable column.

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
removed on every exit the process can observe, a terminating signal included.
A run that was killed outright leaves its directory behind, and the next run
removes it before starting. A lock file there stops a second run from starting
while one is under way.

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
- It does not carry REAL values, integers beyond 2^53, or text holding both a
  line break and its escaped form; it refuses them.
- It cannot copy a database that holds a virtual table: D1's export does not
  support them.

## Requirements

- Python 3.12 or newer, with the `sqlite3` module.
- A Cloudflare API token with D1 Edit on the account.

## Known issues

Where one of these ends in a failed import, nothing is applied to the target:
D1 applies none of a failed import.

- **The empty-target check is a read made before the import.** A table someone
  else creates on the target while a copy runs is not refused. The comparison
  then fails, and by then the import's `DELETE FROM sqlite_sequence` has
  cleared that table's sequence marks. Do not write to a target while it is
  being filled.
- **The NUL restoration runs after the dump's triggers exist.** It is done by
  `UPDATE` on the local working copy, so an `UPDATE` trigger in the schema runs
  there too, and can change that copy, on both sides alike. It also covers
  application tables only: a NUL in a table or index name recorded in
  `sqlite_sequence` or `sqlite_stat1` is left as the loader's placeholder.
- **A terminating signal that arrives while a run is already removing its
  working files stops the removal.** A later signal is held off once one has
  arrived, but the first one is not. What is left is removed by the next run.
- **A dump is executed by SQLite without restriction.** A restore file can
  `ATTACH` another local database file and change it. Restore only from an
  export you trust.
- **A row that references a later row of its own table** is written in the
  table's own order, and D1's import then fails.
- **A foreign key whose referenced table name differs in case** from the
  table's own name is not used for ordering, and the import can fail.
- **A cycle whose only nullable break is one column of a composite foreign key**
  is refused, though it could be written.
- **A nullable column chosen to break a cycle can itself be the key another
  table's foreign key points at.** The import then fails.
- **A held-back column is restored by primary key.** A row whose primary key
  holds NULL, which only a table that is neither STRICT nor WITHOUT ROWID
  allows, is not restored, and the comparison then fails.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option. Releases published under the MIT license alone may also be used
under either license.

### Contribution

By submitting a contribution for inclusion in this project, you agree that it
is dual licensed as above, without any additional terms or conditions.
