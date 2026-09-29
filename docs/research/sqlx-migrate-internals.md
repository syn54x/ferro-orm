---
title: What sqlx 0.8.6's migrate module can lend a Ferro-owned runner
type: research
tags: [rust, migrations, sqlx, runner, locking, tracking-table]
related_files:
  - Cargo.toml
  - src/migrate.rs
  - src/backend.rs
related_issues: [452, 457, 459, 465]
captured: 2026-09-28
sqlx_version: 0.8.6
---

# What sqlx 0.8.6's migrate module can lend a Ferro-owned runner

Research for #459 (wayfinder map #452). Every claim below is cited to the
sqlx source at the version Ferro's `Cargo.lock` pins, **sqlx 0.8.6**
(`sqlx-core 0.8.6`, `sqlx-postgres 0.8.6`, `sqlx-sqlite 0.8.6`), read from
the vendored crates in `~/.cargo/registry/src/index.crates.io-*/`, plus
`sqlx-cli` at the `v0.8.6` git tag and, for the "what changed later"
section only, the `v0.9.0` tag. Citations are `crate/path:line` at that
version.

Ferro enables the `migrate` feature today without asking for it: the `sqlx`
facade's default features are `any`, `macros`, `migrate`, `json`
(`sqlx-0.8.6/Cargo.toml:397-402`) and Ferro's dependency line does not set
`default-features = false` (`Cargo.toml:41`); the lockfile carries `crc`
under `sqlx-core`, which only the `migrate` feature pulls in
(`sqlx-core-0.8.6/Cargo.toml:275-278`). So `sqlx::migrate::*` is already
compiled into `_core` and costs nothing extra to use.

## The one-paragraph verdict

sqlx's migrate module is a small, closed loop built for one shape: a
directory of `NNNN_description.sql` files, one row per file in a table
named `_sqlx_migrations`, whose primary key is the migration version.
Ferro's runner needs a different shape (a migration is a directory of
ordered SQL **and Python** steps, recorded per step, #454/#457), so the
loop itself (`Migrator`) and the row writers (`apply`/`revert`) are not
reusable. What *is* reusable is smaller and cleaner than the ticket
guessed: the Postgres advisory-lock pair (`lock`/`unlock`, public, callable
on a `PgConnection` without a `Migrator`), the checksum recipe (SHA-384 of
the file bytes, two lines), the `-- no-transaction` convention, and the
error vocabulary. SQLite gets no lock from sqlx at all. Nothing in 0.8.6
lets the table name change, and the table's schema cannot hold per-step
rows. Read-compatibility with `sqlx-cli migrate info` is technically
reachable but would require lying in the `version` column and would let
`sqlx-cli` mutate Ferro's ledger; it is not worth having.

## What sqlx does, concretely

A project has `migrations/0001_users.sql`. `Migrator::new(Path)` reads the
directory, keeps files whose name splits as `<i64>_<rest>.sql`, computes
`checksum = SHA-384(file bytes)`, flags `no_tx` when the file starts with
`-- no-transaction`, and sorts by version
(`sqlx-core-0.8.6/src/migrate/source.rs:97-142`,
`migration.rs:17-35`). `Migrator::run(&pool)` then does, on **one**
connection (`migrator.rs:131-192`):

```
lock()                        -- pg_advisory_lock(<key>)   (SQLite: no-op)
ensure_migrations_table()     -- CREATE TABLE IF NOT EXISTS _sqlx_migrations (...)
dirty_version()               -- SELECT version ... WHERE success = false
list_applied_migrations()     -- SELECT version, checksum ... ORDER BY version
validate: every applied version must exist in the directory (unless ignore_missing)
for each migration in version order, skipping *.down.sql:
    applied?  -> checksum must match, else VersionMismatch
    not?      -> apply(): BEGIN; <file sql>; INSERT INTO _sqlx_migrations; COMMIT
unlock()                      -- pg_advisory_unlock(<key>)
```

On Postgres the table it creates is
(`sqlx-postgres-0.8.6/src/migrate.rs:119-126`):

```sql
CREATE TABLE IF NOT EXISTS _sqlx_migrations (
    version BIGINT PRIMARY KEY,
    description TEXT NOT NULL,
    installed_on TIMESTAMPTZ NOT NULL DEFAULT now(),
    success BOOLEAN NOT NULL,
    checksum BYTEA NOT NULL,
    execution_time BIGINT NOT NULL
);
```

SQLite's is the same six columns with `TIMESTAMP ... DEFAULT
CURRENT_TIMESTAMP` and `BLOB` (`sqlx-sqlite-0.8.6/src/migrate.rs:72-79`).
The row is written inside the migration's own transaction as
`(version, description, TRUE, checksum, -1)`
(`sqlx-postgres-0.8.6/src/migrate.rs:285-295`,
`sqlx-sqlite-0.8.6/src/migrate.rs:150-160`), and `execution_time` is
patched by a second statement after commit
(`sqlx-postgres-0.8.6/src/migrate.rs:232-244`).

## Q1. Which `Migrate` pieces are public and usable outside `Migrator`

The trait is `pub` and every method is a required trait method with no
extra visibility gate (`sqlx-core-0.8.6/src/migrate/migrate.rs:27-65`);
it is re-exported at `sqlx::migrate::Migrate`
(`sqlx-core-0.8.6/src/migrate/mod.rs:10`, `sqlx-0.8.6/src/lib.rs:42-43`)
and implemented for `PgConnection`, `SqliteConnection`, `MySqlConnection`
and `AnyConnection` (docs.rs `sqlx/0.8.6/sqlx/migrate/trait.Migrate.html`,
implementors; `sqlx-core-0.8.6/src/any/migrate.rs:46-82` forwards each
call to the underlying driver). Ferro's pools are concrete `PgPool` /
`SqlitePool` (`src/backend.rs:207-208`), so a `PoolConnection<Postgres>`
derefs to `PgConnection` and every method below is callable directly.

| Method | Public? | Reusable for Ferro? | Why |
|---|---|---|---|
| `lock()` / `unlock()` | yes | **yes** (Postgres) | Standalone advisory lock pair; no table, no `Migrator` needed. Details below. |
| `ensure_migrations_table()` | yes | no | Hard-codes `_sqlx_migrations` and its six columns. |
| `dirty_version()` | yes | no | Reads `_sqlx_migrations.success = false`; see the "dead code" note below. |
| `list_applied_migrations()` | yes | no | Reads `(version, checksum)` from `_sqlx_migrations` only. |
| `apply()` / `revert()` | yes | no | Executes SQL **and** writes/deletes the `_sqlx_migrations` row in the same transaction; no hook between the two. |

Helpers and types:

- **Advisory-lock key derivation is private.** `generate_lock_id` and
  `current_database` are free `fn`s in the driver crate with no `pub`
  (`sqlx-postgres-0.8.6/src/migrate.rs:318-330`). The key is

  ```rust
  0x3d32ad9e * (crc32_iso_hdlc(current_database()) as i64)   // i64
  ```

  computed from `SELECT current_database()`. The product cannot overflow:
  `0x3d32ad9e` is about `1.03e9`, CRC-32 is at most `4.29e9`, so the key is
  at most about `4.4e18`, under `i64::MAX`. The lock is the *session-level*
  `pg_advisory_lock($1)` / `pg_advisory_unlock($1)` (one `bigint` key,
  blocking, no timeout) (`sqlx-postgres-0.8.6/src/migrate.rs:170-204`).
  Ferro does not need to re-derive the key to *use* the lock: calling
  `conn.lock()` on a `PgConnection` derives it internally. Ferro only needs
  the formula if it wants its own key, and the reason it might is that this
  key is **shared with every sqlx-based migrator on the same database
  name**, including `sqlx-cli migrate run` of an unrelated Rust service.
  That is either a feature (one lock for all migrators) or a hazard (Ferro
  blocks behind, or is blocked by, someone else's tool); it is a #465
  decision, not a constraint. Note also that session-level advisory locks
  belong to the connection: `lock()` and `unlock()` must run on the same
  pooled connection, and if the process dies the server releases the lock
  with the session. `Migrator` guarantees the same-connection property by
  acquiring once (`migrator.rs:136-137`); a Ferro runner that holds a
  connection across Python data steps has to keep that one connection
  checked out for the whole run.

- **Checksum.** There is no standalone checksum function. `Migration::new`
  computes `Sha384::digest(sql.as_bytes())` and stores it as `Vec<u8>`
  (`sqlx-core-0.8.6/src/migrate/migration.rs:25`). `Migration` is a plain
  public struct with public fields and a public constructor (no
  `#[non_exhaustive]` in 0.8.6; the comment in `source.rs:55-56` is stale),
  so Ferro *could* build `Migration` values, but `sha2` is not re-exported
  by sqlx (grep of `sqlx-core-0.8.6/src` and `sqlx-0.8.6/src` finds no
  `pub use sha2`), so Ferro would add `sha2` as a direct dependency (it is
  already in the build transitively) and write the digest itself. Staying
  on SHA-384 over raw file bytes keeps Ferro's hex strings the same length
  and shape as sqlx's, which matters only if the two are ever displayed
  side by side.

- **`no_tx`.** A `bool` field on `Migration`, set when the file's first
  bytes are exactly `-- no-transaction` (`source.rs:127`). Honored by the
  Postgres `apply`/`revert`: without a transaction the SQL runs bare and
  the tracking row is inserted by a second statement, so a crash between
  the two leaves DDL applied and no row
  (`sqlx-postgres-0.8.6/src/migrate.rs:214-225,257-266`). **Ignored by the
  SQLite `apply` in 0.8.6**: it unconditionally opens a transaction and
  never reads `migration.no_tx` (`sqlx-sqlite-0.8.6/src/migrate.rs:136`;
  the only other reference to `no_tx` in the sqlite crate's dependency
  chain is the parser in `source.rs`). The convention (a leading comment
  that opts a step out of its transaction) is worth borrowing for
  `CREATE INDEX CONCURRENTLY` steps; the implementation is one `if`.

- **`dirty_version()` is effectively dead code on Postgres and SQLite.**
  It selects the lowest `version` with `success = false`
  (`sqlx-postgres-0.8.6/src/migrate.rs:138-141`), but the only `INSERT`
  either driver issues writes `success = TRUE`, inside the same transaction
  as the migration SQL (`:285-295`, sqlite `:150-160`). Nothing in 0.8.6
  writes `FALSE`; the variant's own comment says it exists for databases
  without transactional DDL (`error.rs:37-41`), and the `no_tx` path does
  not write a row on failure either. Ferro's per-step ledger (#457) needs a
  real in-progress marker that is written *before* a step runs; sqlx has no
  such thing to lend.

- **`Migrator` knobs.** `ignore_missing`, `locking`, `no_tx` and
  `migrations` are `pub` fields marked `#[doc(hidden)]` and
  "semver-exempt" (`migrator.rs:15-25`); the supported surface is
  `Migrator::new`, `set_ignore_missing`, `set_locking`, `iter`,
  `version_exists`, `run`, `undo` (`migrator.rs:74-257`). The struct-level
  `no_tx` field is never read by `run_direct` (only `Migration.no_tx` is).
  `run_direct` is public but `#[doc(hidden)]` (`migrator.rs:141-142`).

- **`MigrationSource`** is a public trait (`source.rs:25-27`), so Ferro
  could feed a custom source into `Migrator`, but that only buys the
  SQL-only loop above; a Python step cannot be expressed as a `Migration`.

- **`MigrateError`** (`error.rs:5-42`) is `#[non_exhaustive]`; its
  vocabulary (`VersionMissing`, `VersionMismatch`, `VersionNotPresent`,
  `VersionTooOld`, `VersionTooNew`, `Dirty`) is a good checklist for the
  Ferro runner's own error type. Note `VersionTooOld` / `VersionTooNew` are
  never raised by the library (grep of `sqlx-core`, `sqlx-postgres`,
  `sqlx-sqlite`, `sqlx-macros-core` at 0.8.6 finds only their definition);
  only `sqlx-cli` raises them, for `--target-version`
  (`sqlx-cli/src/migrate.rs:305-309,398-402` at `v0.8.6`).

## Q2. Is the tracking table name or schema configurable in 0.8.6?

**No.** The name `_sqlx_migrations` is a literal in every SQL string of
all three driver impls (Postgres `:119,139,154,236,287,310`; SQLite
`:72,92,107,152,174,201`). `Migrator` in 0.8.6 has no `table_name` field
and no setter (`migrator.rs:14-26,47-101`), and there is no config module
in `sqlx-core 0.8.6` at all (no `src/config/` directory; `sqlx.toml` does
not exist in this version).

Both arrived in **sqlx 0.9.0 (released 2026-05-06)**: the changelog lists
per-crate `sqlx.toml` with "Rename or relocate the `_sqlx_migrations`
table (for multiple crates using the same database)" and "Set characters
to ignore when hashing migrations" under 0.9.0. At the `v0.9.0` tag,
`Migrator` gains `table_name: Cow<'static, str>` defaulting to
`"_sqlx_migrations"`, `dangerous_set_table_name`, `create_schema`, and
every `Migrate` method takes `table_name: &str`
(`sqlx-core/src/migrate/migrator.rs:27-41,111-125` and
`sqlx-core/src/migrate/migrate.rs:28-90` at `v0.9.0`); the config field is
`[migrate] table-name = "foo._sqlx_migrations"` with the doc warning that
changing it on a production database "will likely result in data loss or
corruption" (`sqlx-core/src/config/migrate.rs:46-58` at `v0.9.0`). Ferro is
on 0.8.6 and none of this is available to it today.

**Could per-step rows fit the schema at all?** No, on two counts. First,
`version BIGINT PRIMARY KEY` allows exactly one row per migration; a
migration with three steps cannot record three rows without encoding the
step into `version` (e.g. `0003 * 1000 + step`), which is a lie every sqlx
reader would then believe. Second, the table has no column for what Ferro
must record per #455/#457: the step's kind (SQL or Python), its file
checksum *and* the `ir.json` fingerprint it ran against, and an
in-progress state that is not `success = false` after the fact. The
schema is a record of "this SQL file ran", nothing more.

## Q3. What the SQLite `Migrate` impl does for locking

Nothing. `lock` and `unlock` are `Box::pin(async move { Ok(()) })`
(`sqlx-sqlite-0.8.6/src/migrate.rs:123-129`); unchanged at `v0.9.0`
(`sqlx-sqlite/src/migrate.rs:149-155`). The only thing standing between
two concurrent SQLite migrators is the transaction that `apply` wraps
around `<sql>; INSERT INTO _sqlx_migrations` plus the primary key on
`version`: two runners that both see the same pending migration both try
to apply it, the writer lock serializes them, and the second one's
`INSERT` fails on the primary key and rolls its DDL back (inference from
`sqlx-sqlite-0.8.6/src/migrate.rs:136-162` and the `PRIMARY KEY` at
`:73`; sqlx documents the same-transaction choice as "so we never execute
migrations twice", `:139-140`, citing launchbadge/sqlx#1966). That
transaction is a plain deferred `BEGIN`: `SqliteConnection::begin()`
passes `None` and the worker issues `begin_ansi_transaction_sql`, which
is the string `"BEGIN"` (`sqlx-sqlite-0.8.6/src/transaction.rs:15-31`,
`connection/worker.rs:224`, `sqlx-core-0.8.6/src/transaction.rs:277-283`).
A deferred `BEGIN` takes no lock until the first write, so the "one lock
per run" property of #457 does not exist for SQLite in sqlx.

What sqlx *does* expose that a Ferro SQLite lock can be built from:
`Connection::begin_with(statement)` (`sqlx-sqlite-0.8.6/src/connection/mod.rs:257`)
runs a custom `BEGIN` and verifies the connection really entered a
transaction (`transaction.rs:20-27`). `begin_with("BEGIN IMMEDIATE")`
takes SQLite's RESERVED lock up front on the runner's connection, which
is the closest SQLite equivalent of "one writer for the run". Holding it
across a Python data step is a design choice for #465; the primitive is
there and is public.

## Q4. What a Ferro-owned table keyed by (migration, step) must re-implement, and whether `sqlx-cli migrate info` compatibility is worth anything

Because `apply`/`revert` write the row themselves and every reader is
bound to `_sqlx_migrations`, a Ferro-owned table means re-implementing
the whole loop except the lock. Concretely, with what sqlx does for each
as the reference:

| Concern | sqlx 0.8.6 reference | Ferro must own |
|---|---|---|
| Source resolution | `resolve_blocking`: `<i64>_<desc>.sql`, sort by version, `-- no-transaction` sniff (`source.rs:57-145`) | A migration directory of ordered steps (#454), `.sql` and `.py`, plus the previous migration's `ir.json` (#455). |
| Ensure table | `CREATE TABLE IF NOT EXISTS _sqlx_migrations` | Own DDL, own name, `(migration, step)` key; through the I-1 emitter path so both dialects agree. |
| Applied set | `SELECT version, checksum ... ORDER BY version` | Per-step rows: migration number, step ordinal, step kind, file checksum, IR fingerprint, started/finished. |
| Validate | `VersionMissing` unless `ignore_missing`; `VersionMismatch` on checksum (`migrator.rs:28-45,173-178`) | Same two checks per step, plus the fingerprint check of #455. |
| In-progress / dirty | `success = false` (never written; see Q1) | A row written *before* the step runs, so a crash resumes at the failed step (#457 property 1). |
| Apply | `BEGIN; sql; INSERT row; COMMIT`, `no_tx` opt-out on Postgres | Same shape for SQL steps; for Python steps the "sql" is a PyO3 call, and the step row is the commit boundary. |
| Revert | `sql; DELETE row` in one transaction | Same, if Ferro has a down door (open on the map). |
| Lock | `pg_advisory_lock` on a CRC-derived key; SQLite none | **Borrow** `PgConnection::lock()/unlock()` as-is, or re-derive the key to get a Ferro-private one; build SQLite's from `begin_with("BEGIN IMMEDIATE")`. |
| Checksum | SHA-384 of file bytes (`migration.rs:25`) | Two lines with `sha2`; keep the algorithm. |
| Ordering / out-of-order | apply every unapplied version in sorted order, silently (Q5) | Decide explicitly (#465/#466). |

**Read-compatibility with `sqlx-cli migrate info`.** `info` needs exactly
two things: a directory it can resolve with `Migrator::new` (so
`<i64>_<desc>.sql` files) and a `_sqlx_migrations` table with `(version,
checksum)` (`sqlx-cli/src/migrate.rs:195-252` at `v0.8.6`). It calls
`ensure_migrations_table()` before reading, so pointing it at a Ferro
database **creates an empty `_sqlx_migrations` table as a side effect**
(`:199`) and then prints every resolved file as `pending`. For Ferro to be
readable it would have to write a shadow row per migration into
`_sqlx_migrations` with a checksum `sqlx-cli` can recompute, which it
cannot for a directory of `.py` steps (`Migrator::new` ignores anything
that is not `<i64>_<rest>.sql`, `source.rs:99-102`). And sharing the name
is actively harmful: `sqlx-cli migrate run` on the same database would
happily insert its own rows, and `sqlx migrate add` would infer sequential
numbering from Ferro's files (`sqlx-cli/src/migrate.rs:61-100`). Verdict:
not achievable without falsifying `version`, and not worth anything even
then. Use a distinct name (`_ferro_migrations` or similar) so the two
tools cannot confuse each other's ledgers. What *is* worth borrowing from
`info` is its three-way status vocabulary, `installed` /
`installed (different checksum)` / `pending`, with both checksums printed
on a mismatch (`:216-246`), which maps cleanly onto a per-step
`ferro migrate status`.

## Q5. Out-of-order versions and `ignore_missing`

**Out-of-order merges are applied silently.** `Migrator::run_direct`
walks the resolved list in version order and applies *every* version
absent from the applied map, with no comparison against the highest
applied version (`migrator.rs:168-183`). If `0001` and `0003` are applied
and a branch merge lands `0002`, the next run applies `0002` after `0003`
with no warning. `MigrateError::VersionTooOld` exists (`error.rs:24-25`)
but the library never raises it; `sqlx-cli migrate run` raises it only
when `--target-version` is *lower* than the latest applied version
(`sqlx-cli/src/migrate.rs:300-309` at `v0.8.6`), and otherwise has the
same silent behavior as the library (`:316-355`). With Ferro's sequential
four-digit numbering (#453) an out-of-order pending migration is exactly
the branch-merge case; sqlx offers no precedent for refusing it, so
whether Ferro refuses by default (and what the override is called) is an
open decision for #465/#466.

**`ignore_missing` does one thing.** It skips
`validate_applied_migrations`, the check that every applied `version` in
the table still exists in the source directory; the default is `false`,
so a deleted-but-applied migration is `VersionMissing` and aborts the run
(`migrator.rs:28-45,161`; `sqlx-cli` re-implements the identical check for
its `--ignore-missing` flag, `sqlx-cli/src/migrate.rs:254-272`). It does
not relax the checksum comparison (`VersionMismatch` still fires,
`:173-178`) and does not touch ordering. For Ferro this is the
"applied row with no file" case; the flag's semantics are worth copying
as-is, its default of *fail* especially.

## What changes if Ferro moves to sqlx 0.9

Recorded so the runner design does not paint itself into a corner:

- Every `Migrate` method except `lock`/`unlock` takes `table_name: &str`;
  `Migrator` gets `dangerous_set_table_name`, `create_schema` /
  `create_schemas`, and a `skip()` method that records a row without
  running SQL (`sqlx-core/src/migrate/migrate.rs:28-90`,
  `migrator.rs:27-41,89-125,280` at `v0.9.0`). The trait still has no
  per-step notion and `apply` still writes the row itself, so the
  reusability verdict above does not change.
- `set_ignore_missing` / `set_locking` return `&mut Self` (0.9.0
  changelog, breaking changes).
- The Postgres advisory-lock key derivation is byte-identical at `v0.9.0`
  (`sqlx-postgres/src/migrate.rs:370-374`); a Ferro key derived from the
  0.8.6 formula stays shared with 0.9 sqlx migrators too. The 0.9.0
  changelog also lists "fix(postgres): make advisory lock cancel safe"
  (launchbadge/sqlx#4199); the `lock`/`unlock` bodies themselves read the
  same at `v0.9.0` (`:186-220`), so the fix is elsewhere and not
  characterized here.
- `sqlx.toml` `[migrate] ignored-chars` changes what bytes feed the
  checksum; a Ferro checksum that stays "SHA-384 of the raw file" will not
  match sqlx's for the same file once a project sets that option. This
  only matters if Ferro ever displays the two side by side, which Q4
  argues against.

## Answers to the ticket, in one place

1. **Public and usable outside `Migrator`:** every `Migrate` method, on
   `PgConnection`, `SqliteConnection` and `AnyConnection`. Worth using:
   `lock()`/`unlock()` on Postgres. The lock key derivation, `crc` +
   `current_database()`, is private but three lines. Checksum is SHA-384
   of file bytes via `Migration::new`, no standalone function, `sha2` not
   re-exported. `no_tx` is a leading `-- no-transaction` comment, honored
   on Postgres, ignored by SQLite's `apply` in 0.8.6. `dirty_version()` is
   public but nothing writes `success = false` on Postgres or SQLite.
2. **Table configurability:** none in 0.8.6; `sqlx.toml` `[migrate]
   table-name` and `dangerous_set_table_name` are 0.9.0. Per-step rows
   cannot fit: `version` is the primary key and the columns do not carry
   step kind, IR fingerprint or an in-progress state.
3. **SQLite locking:** `lock`/`unlock` are no-ops; the only guard is the
   deferred-`BEGIN` transaction around SQL + row insert and the version
   primary key. `begin_with("BEGIN IMMEDIATE")` is the public primitive a
   real run lock can be built on.
4. **Ferro-owned table:** re-implement everything but the lock and the
   checksum recipe (table above). `sqlx-cli migrate info` compatibility
   would require falsifying `version`, lets `sqlx-cli` create and write
   the table, and buys nothing; pick a distinct table name and borrow only
   `info`'s status vocabulary.
5. **Out-of-order and `ignore_missing`:** an older pending version is
   applied silently, after the newer ones, by both the library and the
   CLI; `VersionTooOld` is raised only for `--target-version`.
   `ignore_missing` disables only the "applied row has no source file"
   check (default: abort) and never relaxes checksums.

Handed to #465: whether to share sqlx's advisory-lock key or derive a
Ferro-private one; whether the SQLite lock is `BEGIN IMMEDIATE` on the
runner's connection or a lock row; and whether out-of-order pending
migrations are refused by default.
