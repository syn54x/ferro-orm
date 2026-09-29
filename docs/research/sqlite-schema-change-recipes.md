---
title: SQLite schema-change recipes
type: research
tags: [sqlite, migrations, alembic, schema, rust]
related_files:
  - crates/ferro-migrate/src/emit.rs
  - crates/ferro-ddl-lowering/src/lib.rs
  - src/migrate.rs
  - src/ferro/migrations/alembic.py
  - docs/pages/guide/migrations.md
  - docs/adr/0014-table-checks-sqlite-create-only.md
related_issues: [452, 458, 470]
captured: 2026-09-28
---

# SQLite schema-change recipes

Research for [#458](https://github.com/syn54x/ferro-orm/issues/458), feeding
the grilling ticket [#470](https://github.com/syn54x/ferro-orm/issues/470)
("SQLite: rebuild recipe or stated boundary for the DDL door"). It records
what the primary sources say and what Ferro does today; it does not decide.

## Summary

- SQLite's native `ALTER TABLE` is `RENAME TO`, `RENAME COLUMN`, `ADD COLUMN`,
  `DROP COLUMN` (3.35.0+) and, **new in 3.53.0 (2026-04-09)**,
  `ALTER COLUMN … SET/DROP NOT NULL`. Everything else — type change, adding
  or dropping a CHECK/FOREIGN KEY/UNIQUE/PRIMARY KEY on an existing table —
  is the sqlite.org "12-step" rebuild: new table under a temporary name, copy,
  drop old, rename new into place, recreate indexes/triggers/views. The order
  matters: rename-first corrupts references since 3.26.0.
- The rebuild has two hard bracketing rules. `PRAGMA foreign_keys=OFF` must be
  issued **outside** the transaction (it is a no-op inside one) and the
  transaction must end with `PRAGMA foreign_key_check` before `COMMIT`. With
  enforcement on, `DROP TABLE` runs an implicit `DELETE` that fires FK
  actions and can fail outright.
- Ferro's SQLite connections run with `foreign_keys=ON` (sqlx's default) on a
  pool, and the reconciliation pass executes SQLite statements one at a time
  with no transaction. Both facts are the opposite of what the recipe needs.
  Ferro bundles SQLite 3.46.0, so the 3.53.0 `ALTER COLUMN` form is not
  available until `libsqlite3-sys` is bumped (0.38.x bundles 3.53.4).
- Alembic batch mode is exactly the sqlite.org recipe minus steps 1, 10 and
  12: it reflects the table, creates `_alembic_tmp_<name>`, `INSERT … SELECT`,
  drops the old table, renames, recreates indexes. It does **not** touch
  `PRAGMA foreign_keys` (the docs say the pragma "must be disabled" by the
  user), drops unnamed CHECK constraints from the recreate (with a warning
  since 1.20), cannot run offline without `copy_from`, and Alembic's SQLite
  impl raises `NotImplementedError` for any `add_constraint` /
  `drop_constraint` outside a batch block.
- Of the tools surveyed, Atlas and Django implement the rebuild (Atlas names
  the temp table `new_<table>` and brackets the plan with the two pragmas;
  Django refuses to start unless the pragma is already off and runs
  `foreign_key_check` on exit). sqldef's `sqlite3def` rebuilds nothing: it
  uses the native ALTERs and silently emits no DDL for a type, nullability,
  default or CHECK change on an existing column. pg-delta is Postgres 15+
  only.
- Nine of Ferro's fifteen SQLite warn-skips are the same one operation
  (rebuild); two are in-place gaps of the current `ADD COLUMN` emitter, not
  rebuild cases; the rest are true boundaries or already native.

## 1. What SQLite's `ALTER TABLE` can and cannot do

Source: [sqlite.org/lang_altertable.html][sq-alter] unless stated.

### 1.1 Native forms and their restrictions

- **`RENAME TO`** — always available. Since 3.25.0 "references to the table
  within trigger bodies and view definitions are also renamed"; since 3.26.0
  "FOREIGN KEY constraints are always converted when a table is renamed,
  unless the PRAGMA legacy_alter_table=ON setting is engaged" ([§2][sq-alter]).
- **`RENAME COLUMN`** — 3.25.0+. "The column name is changed both within the
  table definition itself and also within all indexes, triggers, and views
  that reference the column" ([§3][sq-alter]).
- **`ADD COLUMN`** — the new column "may take any of the forms permissible in
  a CREATE TABLE statement, with the following restrictions" ([§4][sq-alter]):
  - "The column may not have a PRIMARY KEY or UNIQUE constraint."
  - "The column may not have a default value of CURRENT_TIME, CURRENT_DATE,
    CURRENT_TIMESTAMP, or an expression in parentheses."
  - "If a NOT NULL constraint is specified, then the column must have a
    default value other than NULL."
  - "If foreign key constraints are enabled and a column with a REFERENCES
    clause is added, the column must have a default value of NULL."
  - "The column may not be GENERATED ALWAYS ... STORED, though VIRTUAL
    columns are allowed."
  - A column `CHECK` **is** allowed, and since 3.37.0 "the added constraints
    are tested against all preexisting rows in the table and the ADD COLUMN
    fails if any constraint fails."
- **`DROP COLUMN`** — 3.35.0+. "The DROP COLUMN command only works if the
  column is not referenced by any other parts of the schema and is not a
  PRIMARY KEY and does not have a UNIQUE constraint" ([§5][sq-alter]). It
  fails when the column is a PRIMARY KEY or part of one, has a UNIQUE
  constraint, is indexed, is named in a partial index's WHERE, is "named in a
  table or column CHECK constraint not associated with the column being
  dropped", is used in a foreign key constraint, is used in a generated
  column's expression, or "appears in a trigger or view". Unlike the other
  forms, it "rewrites its content to purge the data associated with that
  column" (O(rows)).
- **`ALTER COLUMN … SET NOT NULL` / `DROP NOT NULL`** — "was added in SQLite
  3.53.0 (2026-04-09)" ([§6][sq-alter]). `SET NOT NULL` is a no-op on an
  already-NOT-NULL column; `DROP NOT NULL` on a column declared with several
  redundant NOT NULLs "is guaranteed to remove one or more of them, but not
  necessarily all of them." Nothing else about `ALTER COLUMN` (no type
  change, no default change) is on the page.
- **How it works.** "SQLite stores the schema as plain text in the
  sqlite_schema table. All ALTER TABLE commands modify that text and then
  attempt to reparse the entire schema. The command is only successful if the
  schema is still valid after the text has been modified" ([§5.1][sq-alter]).
  This is why an unparseable view or trigger makes every ALTER on that table
  fail; 3.38.0+ lets `PRAGMA writable_schema=ON` suppress that check
  ([§7][sq-alter]).

### 1.2 The 12-step procedure ([§8][sq-alter], verbatim numbering)

"The only schema altering commands directly supported by SQLite are the
'rename table', 'rename column', 'add column', 'drop column' commands shown
above. However, applications can make other arbitrary changes to the format
of a table using a simple sequence of operations."

1. "If foreign key constraints are enabled, disable them using
   `PRAGMA foreign_keys=OFF`."
2. "Start a transaction."
3. "Remember the format of all indexes, triggers, and views associated with
   table X. … `SELECT type, sql FROM sqlite_schema WHERE tbl_name='X'`."
4. "Use CREATE TABLE to construct a new table 'new_X' that is in the desired
   revised format of table X. Make sure that the name 'new_X' does not
   collide with any existing table name, of course."
5. "Transfer content from X into new_X using a statement like:
   `INSERT INTO new_X SELECT ... FROM X`."
6. "Drop the old table X: `DROP TABLE X`."
7. "Change the name of new_X to X using: `ALTER TABLE new_X RENAME TO X`."
8. "Use CREATE INDEX, CREATE TRIGGER, and CREATE VIEW to reconstruct indexes,
   triggers, and views associated with table X."
9. "If any views refer to table X in a way that is affected by the schema
   change, then drop those views using DROP VIEW and recreate them …"
10. "If foreign key constraints were originally enabled then run
    `PRAGMA foreign_key_check` to verify that the schema change did not break
    any foreign key constraints."
11. "Commit the transaction started in step 2."
12. "If foreign keys constraints were originally enabled, reenable them now."

The page states the procedure's reach explicitly: it "is appropriate for
dropping a column, changing the order of columns, adding or removing a
UNIQUE constraint or PRIMARY KEY, adding CHECK or FOREIGN KEY or NOT NULL
constraints, or changing the datatype for a column."

**Ordering caution (verbatim).** "Take care to follow the procedure above
precisely. … the procedure on the right does not always work, especially
with the enhanced rename table capabilities added by versions 3.25.0 and
3.26.0. In the procedure on the right, the initial rename of the table to a
temporary name might corrupt references to that table in triggers, views,
and foreign key constraints. The safe procedure on the left constructs the
revised table definition using a new temporary name, then renames the table
into its final name, which does not break links." Correct: create new, copy,
drop old, rename new into old. Incorrect: rename old, create new, copy, drop
old.

**The `writable_schema` shortcut.** For changes "that do not affect the
on-disk content in any way" — "removing CHECK or FOREIGN KEY or NOT NULL
constraints, or adding, removing, or changing default values on a column" —
the page offers a second procedure that `UPDATE`s `sqlite_schema` directly
under `PRAGMA writable_schema=ON` and bumps `PRAGMA schema_version`, with the
caution, twice, that a mistake "will render the database corrupt and
unreadable". It is documented; it is not a door a library should open on a
user's database.

### 1.3 The pragmas, and what breaks when a step is skipped

- **`PRAGMA foreign_keys` is a no-op inside a transaction.** "This pragma is
  a no-op within a transaction; foreign key constraint enforcement may only be
  enabled or disabled when there is no pending BEGIN or SAVEPOINT"
  ([pragma.html#pragma_foreign_keys][sq-pragma-fk]). So step 1 must run on the
  connection *before* step 2, on the same connection, and step 12 after step
  11. A pooled connection that `BEGIN`s first and then sets the pragma is
  running with enforcement still on and no error to say so. (Django reads the
  pragma back after setting it, precisely because the set can silently fail;
  see §3.)
- **Skipping step 1 (enforcement on during step 6).** "If foreign key
  constraints are enabled when it is prepared, the DROP TABLE command
  performs an implicit DELETE to remove all rows from the table before
  dropping it. The implicit DELETE does not cause any SQL triggers to fire,
  but may invoke foreign key actions or constraint violations. If an
  immediate foreign key constraint is violated, the DROP TABLE statement
  fails and the table is not dropped" ([foreignkeys.html §5][sq-fk]). For a
  parent table with `ON DELETE CASCADE` children, the implicit DELETE cascades
  into the child rows before the old table is dropped — data loss, not an
  error. Alembic's docs describe the same trap from the other side: "batch
  table operations do not work with foreign keys that enforce referential
  integrity. This because the target table is dropped; if foreign keys refer
  to it, this will raise an error" ([batch.html][al-batch]).
- **Skipping step 10.** The copy in step 5 is never FK-checked while
  enforcement is off; only `PRAGMA foreign_key_check` (one row per violating
  row, "the name of the table that contains the REFERENCES clause, the rowid
  … the name of the table that is referred to … the index of the specific
  foreign key constraint that failed" [pragma.html#pragma_foreign_key_check][sq-pragma-fkc])
  reveals rows the new table's constraints reject. Committing without it
  leaves violations in place that enforcement will only surface on the next
  write.
- **Skipping steps 3/8/9.** Step 6 drops indexes and triggers with the table
  (`DROP TABLE` semantics); views survive but point at a table that, after
  step 7, has the right name and possibly the wrong shape. Everything the
  `sqlite_schema` query in step 3 returns must be re-issued.
- **`PRAGMA legacy_alter_table`.** Default OFF, "which means that all
  references to the table anywhere in the schema are converted to the new
  name" on `RENAME TO`; ON restores the pre-3.25 behaviour where only the
  `CREATE TABLE`/`INDEX`/`TRIGGER` headers are rewritten and references in
  trigger/view bodies, CHECKs and partial-index WHEREs are left alone
  ([pragma.html#pragma_legacy_alter_table][sq-pragma-legacy]). It is
  per-connection and does not persist. Django sets it OFF explicitly on every
  connection because "The macOS bundled SQLite defaults legacy_alter_table
  ON, which prevents atomic table renames" (§3). Ferro's bundled SQLite is
  not the macOS one, so the default is OFF; a recipe that wants determinism
  regardless of build should still pin it.
- **Schema version.** SQLite bumps `schema_version` on every schema change
  and every prepared statement checks it; the rebuild therefore invalidates
  every cached statement on every connection, which is what Ferro's
  `refresh_pool()` already assumes (§4.3).
- **Transactional DDL.** The procedure itself wraps `CREATE`/`DROP`/`ALTER`
  in one transaction and commits at step 11, which only makes sense because
  SQLite's DDL is transactional. Alembic's SQLite impl says the same thing
  and names the one exception: "SQLite supports transactional DDL, but
  pysqlite does not: see: http://bugs.python.org/issue10740"
  ([ddl/sqlite.py][al-sqlite-src]) — the Python `sqlite3` module's implicit
  transaction handling commits before DDL, so Alembic sets
  `transactional_ddl = False`. sqlx does not have that behaviour; Ferro's
  Postgres path already runs DDL in a transaction and could do the same on
  SQLite.

## 2. Alembic batch mode (`op.batch_alter_table`)

Sources: [batch.html][al-batch], [operations/batch.py][al-batch-src],
[ddl/sqlite.py][al-sqlite-src], [changelog][al-changelog].

### 2.1 What it does

The docs frame the problem the same way sqlite.org does: "Migration tools
are instead expected to produce copies of SQLite tables that correspond to
the new structure, transfer the data from the existing table to the new one,
then drop the old table." The implementation is `ApplyBatchImpl._create`
([batch.py][al-batch-src] `_create`): reflect the existing table (or take
`copy_from`), build the new `Table`, then

1. `CREATE TABLE _alembic_tmp_<name>` (`_calc_temp_name`: the name is
   `"_alembic_tmp_%s" % tablename` truncated to 50 chars);
2. `INSERT INTO _alembic_tmp_<name> (…) SELECT … FROM <name>` (built with
   `insert().inline().from_select(...)`; a column whose transfer has no
   `expr` — i.e. a newly added column — is left to its default);
3. `DROP TABLE <name>` — on any exception in 2 or 3 it drops the temp table
   and re-raises;
4. `ALTER TABLE _alembic_tmp_<name> RENAME TO <name>`;
5. `CREATE INDEX` for every index gathered from both the old and new table
   definitions (`_gather_indexes_from_both_tables`).

That is steps 4-8 of the sqlite.org procedure, minus triggers and views
(Alembic does not reflect them) and minus steps 1, 10 and 12 entirely: there
is no `PRAGMA` anywhere in `batch.py` or `ddl/sqlite.py`. The docs put it on
the user: "On SQLite, whether or not foreign keys actually enforce is
controlled by the PRAGMA FOREIGN KEYS pragma; this pragma, if in use, must be
disabled when the workflow mode proceeds." They also note the one thing
enforcement buys: "When SQLite's PRAGMA FOREIGN KEYS mode is turned on, it
does provide the service that foreign key constraints, including
self-referential, will automatically be modified to point to their table
across table renames, however this mode prevents the target table from being
dropped."

The whole flush runs under `_ensure_scope_for_ddl(self.impl.connection)`,
i.e. inside whatever transaction the migration context holds — which, per
§1.3, is exactly where `PRAGMA foreign_keys` cannot be changed.

### 2.2 `recreate` and what triggers a rebuild

`recreate` "may be one of 'auto', 'always', or 'never'" (`BatchOperationsImpl.__init__`).
`auto` asks the dialect impl `requires_recreate_in_batch`; `always` forces
the copy on every backend ("`recreate='always'` can force 'move and copy'
behavior on non-SQLite databases"); `never` applies every op as a plain
ALTER. The default impl returns False, so "the `batch_alter_table()`
directive by default only takes place for SQLite; other backends will behave
just as they normally do in the absence of the batch directives."

`SQLiteImpl.requires_recreate_in_batch` ([ddl/sqlite.py][al-sqlite-src]):

- `add_column` is in place **unless** its `server_default` is a SQL
  expression (a `ClauseElement`) or a persisted `Computed` — both hit the
  `ADD COLUMN` restrictions in §1.1;
- `create_index` / `drop_index` are in place;
- **every other op** (`alter_column`, `drop_column`, `add_constraint`,
  `drop_constraint`, `rename_table`, …) returns True.

Outside a batch block the SQLite impl refuses constraints:
`add_constraint` and `drop_constraint` raise `NotImplementedError("No support
for ALTER of constraints in SQLite dialect. Please refer to the batch mode
feature which allows for SQLite migrations using a copy-and-move strategy.")`
(an implicit, type-generated constraint is skipped with a warning instead).
Note that `drop_column` is *not* the native `DROP COLUMN` in batch mode — it
always recreates — so the §1.1 `DROP COLUMN` restrictions never bite, at the
cost of an O(rows) copy for every drop.

### 2.3 Documented failure cases and limitations

- **Unnamed constraints cannot be targeted.** "SQLite, unlike any other
  database, allows constraints to exist in the database that have no
  identifying name." An unnamed FK "will remain entirely unnamed when they
  are created on the target database"; `drop_constraint()` needs a name, so
  batch mode takes a `naming_convention` to synthesise names for reflected
  unnamed constraints. 1.20.0 fixed the convention being re-applied to
  constraints that already had names ("leading to a name that included the
  convention's own prefix twice", ticket 1845). Ferro names every FK, index,
  unique and check it emits (AGENTS.md I-1), so this only affects foreign
  constraints on a Ferro table.
- **Unnamed CHECK constraints are dropped by the recreate.** From the source:
  "a reflected CHECK constraint that has no name can't be linked to the
  column or datatype it applies to, so it is omitted from the recreate; the
  documented remedy is to restate it using `batch_alter_table.table_args`."
  Until 1.20.0 (ticket 1846) "This omission was previously a silent
  operation"; now it warns unless any `CheckConstraint` is in `table_args`,
  which "is taken to indicate that the case has been accommodated". Named
  CHECKs are carried over — but Boolean/Enum type-generated CHECKs need
  `existing_type` on `alter_column` and, since 1.7, the named constraint
  passed explicitly on `drop_column`. 1.20.0 also fixed those type CHECKs
  being emitted twice (1768) and losing their convention name because they
  were "regenerated against the temporary table" (1844).
- **Referencing foreign keys.** See §2.1: other tables' FKs pointing at the
  rebuilt table either block the `DROP TABLE` (enforcement on) or are left
  pointing at the *name*, which the rename restores — the docs' advice is to
  turn enforcement off, not to touch the referencing tables.
- **Offline (`--sql`) mode needs `copy_from`.** Reflection needs a live
  database; 1.8.0 (ticket 1021) turned the internal error into a
  `CommandError`: "This operation cannot proceed in --sql mode; batch mode
  with dialect sqlite requires a live database connection with which to
  reflect the table … To generate a batch SQL migration script using
  --sql, pass a Table object to `copy_from`." That is the docs' "the full
  table as it intends to be created must be passed to batch_alter_table()
  using copy_from".
- **Autogenerate.** `render_as_batch=True` in `context.configure()` makes
  autogenerate wrap each table's ops in `with op.batch_alter_table(...) as
  batch_op:`. Autogenerate's *detection* is unchanged: SQLAlchemy's SQLite
  dialect reflects FKs and named/unnamed CHECKs, but `compare_server_default`
  and type comparison on SQLite are affinity-loose, and constraint diffs on
  SQLite are the classic source of phantom diffs the naming convention exists
  to prevent.
- **`insert_before` / `insert_after`** on `add_column` require
  `recreate='always'` ("Can't specify insert_before or insert_after when
  using ALTER").
- Nothing in the docs or source mentions `legacy_alter_table` or a minimum
  SQLite version; the temp name truncation to 50 characters is the only
  identifier-length concern handled.

## 3. How other tools handle SQLite

### 3.1 sqldef / `sqlite3def` — native ALTERs only, no rebuild

[cmd-sqlite3def.md][sqldef-doc] lists what it supports: "Tables: CREATE
TABLE, DROP TABLE, ALTER TABLE RENAME TO, CREATE VIRTUAL TABLE; Columns: ADD
COLUMN, DROP COLUMN, ALTER TABLE RENAME COLUMN; Constraints: PRIMARY KEY,
FOREIGN KEY, UNIQUE, CHECK, AUTOINCREMENT; Indexes: CREATE INDEX, DROP
INDEX, ALTER TABLE RENAME INDEX" — constraints are supported *in* `CREATE
TABLE`, not as changes. `DROP TABLE` / `DROP COLUMN` need `--enable-drop`.

The generator ([schema/generator.go][sqldef-gen]) confirms there is no
rebuild path:

- The "Change column data type or order as needed" switch has `Mysql`,
  `Postgres` and `Mssql` arms and an empty `default:` — on SQLite a type,
  NOT NULL or DEFAULT change to an existing same-named column **emits
  nothing**, with no warning.
- CHECK changes: `case GeneratorModeSQLite3: // SQLite does not support ALTER
  TABLE for CHECK constraints // Modifying CHECK constraints requires
  recreating the table, which is not supported` — again nothing emitted.
- The one SQLite-specific column mutation is the `@renamed from=` annotation
  with a type/null/default change, which does add-copy-drop in place rather
  than rebuild ([tests.yml `RenameColumnWithTypeChange`][sqldef-tests]):
  `ALTER TABLE "users" ADD COLUMN "user_name" text NOT NULL; UPDATE "users"
  SET "user_name" = "username"; ALTER TABLE "users" DROP COLUMN "username";`
  — note this only works because SQLite tests the new NOT NULL against
  existing rows *before* the `UPDATE` only if there is a non-NULL default
  (§1.1), and it leaves column order changed.
- Index rename is drop + create ("SQLite doesn't support renaming indexes
  directly"); views are always drop + create (`shouldDropAndCreateView`
  returns true for SQLite); triggers are drop + create.

So sqlite3def's boundary is: ALTER what SQLite can ALTER; for anything that
needs a rebuild, generate nothing. It does not warn.

### 3.2 pg-delta — not a SQLite tool

Supabase's pg-delta "only targets Postgres 15+, which lets us start clean
and take advantage of newer catalog features without workarounds"
([supabase discussion #44938][pgdelta]); it lives in the `pg-toolbelt`
monorepo and SQLite is not mentioned. Likewise stripe/pg-schema-diff and
pgschema are Postgres-only by name and scope. Only Atlas among the charting
survey's tools targets SQLite.

### 3.3 Atlas — implements the recipe, with the pragmas

[sql/sqlite/migrate.go][atlas]:

- `modifyTable`: if every change is "alterable" (`AddIndex`, `DropIndex`,
  `RenameIndex`, `AddColumn`, `RenameColumn` — the `alterTable` switch), it
  emits native statements. Otherwise it sets `s.skipFKs = true`, creates
  `new_<table>` with the target definition (indexes held back), `copyRows`
  emits ``INSERT INTO `new_t` (cols) SELECT cols FROM `t` `` (generated
  columns skipped; a `ModifyColumn` can supply a cast expression per column),
  then `DROP TABLE t`, `ALTER TABLE new_t RENAME TO t`, then `addIndexes`.
  This is the sqlite.org order.
- `PlanChanges`: when `skipFKs` is set, the plan is bracketed with
  `PRAGMA foreign_keys = off` at the front and `PRAGMA foreign_keys = on` at
  the end, with the source comment "Callers should note that these 2 pragmas
  are no-op in transactions, See: https://sqlite.org/pragma.html#pragma_foreign_keys".
  There is no `foreign_key_check` in the planned statements; Atlas leaves
  step 10 to the applier.
- Triggers and views are not re-created by `modifyTable`; Atlas's SQLite
  driver models them as separate objects with their own changes.

### 3.4 Django — implements the recipe, with the guards

[django/db/backends/sqlite3/schema.py][django-schema] and
[base.py][django-base]:

- `_remake_table` docstring: "This follows the correct procedure to perform
  non-rename or column addition operations based on SQLite's documentation
  https://www.sqlite.org/lang_altertable.html#caution. The essential steps
  are: 1. Create a table with the updated definition called
  'new__app_model' 2. Copy the data … 3. Drop the 'app_model' table 4.
  Rename the 'new__app_model' table to 'app_model' 5. Restore any index of
  the previous 'app_model' table." The copy is
  `INSERT INTO new__t (cols) SELECT exprs FROM t`, where a new field's
  expression is its effective default.
- `__enter__` refuses to run if it cannot switch enforcement off:
  "SQLite schema editor cannot be used while foreign key constraint checks
  are enabled. Make sure to disable them before entering a
  transaction.atomic() context because SQLite does not support disabling
  them in the middle of a multi-statement transaction."
  `disable_constraint_checking` executes `PRAGMA foreign_keys = OFF` and then
  **reads the pragma back** ("Foreign key constraints cannot be turned off
  while in a multi-statement transaction. Fetch the current state of the
  pragma to determine if constraints are effectively disabled.") — the
  silent no-op of §1.3 turned into a hard error.
- `__exit__` runs `check_constraints()` — `PRAGMA foreign_key_check`
  (optionally per table) and raises `IntegrityError` naming the offending
  row — then re-enables enforcement. That is steps 10 and 12.
- Every new connection gets `PRAGMA foreign_keys = ON` and
  `PRAGMA legacy_alter_table = OFF` ("The macOS bundled SQLite defaults
  legacy_alter_table ON, which prevents atomic table renames").
- In-place vs rebuild: `add_field` uses native `ADD COLUMN` only when the
  field is nullable, not PK/unique, has no effective default ("DROP DEFAULT is
  not supported in ALTER TABLE") and no non-constant `db_default`; otherwise
  it remakes. `remove_field` uses native `DROP COLUMN` only when the field is
  not PK, not unique, not indexed and not a constrained FK; otherwise it
  remakes. Every `_alter_field` that changes type, null, default or
  constraints remakes.

### 3.5 Plain runners

dbmate, goose, sqlx-cli and Alembic-without-batch apply SQL the user wrote;
none contains SQLite rebuild logic. They are relevant only as the shape a
`.sql` step could take: the twelve statements are ordinary SQL except that
steps 1 and 12 must be outside the runner's transaction, which is why Atlas
emits them as separate plan entries and why Django's guard exists.

## 4. Ferro today: what the reconciliation pass warn-skips on SQLite

Every fact in this section is from the checkout at `8a0e89d` (0.21.2).

### 4.1 The environment the pass runs in

- **SQLite version.** Ferro links `sqlx 0.8.6` with the `bundled` feature,
  which compiles `libsqlite3-sys 0.30.1`'s vendored **SQLite 3.46.0**
  (`~/.cargo/registry/src/*/libsqlite3-sys-0.30.1/sqlite3/sqlite3.h`,
  `#define SQLITE_VERSION "3.46.0"`; `Cargo.toml` line 41 selects the
  `sqlite` feature, and `sqlx-sqlite`'s `Cargo.toml` defaults to `bundled`).
  So every native `ALTER TABLE` form except the 3.53.0 `ALTER COLUMN` is
  available, and the version is fixed by the build, not by the host's
  `libsqlite3`. The current `libsqlite3-sys` is 0.38.2 (crates.io,
  2026-08-08), whose vendored header is 3.53.4; picking up `ALTER COLUMN …
  NOT NULL` means moving sqlx to a release that depends on it. (The host
  Python's `sqlite3` module is 3.50.4 here, which is what Alembic uses
  through SQLAlchemy; the two engines are different binaries and can differ
  in what they accept.)
- **`PRAGMA foreign_keys` is ON.** sqlx enables it on every connection it
  opens (`sqlx-sqlite-0.8.6/src/options/mod.rs` line 185:
  `pragmas.insert("foreign_keys".into(), Some("ON".into()))`, with the
  comment "We choose to enable foreign key enforcement by default, though
  SQLite normally leaves it off for backward compatibility"). Ferro never
  overrides it (`grep -rn foreign_keys src/*.rs` finds only introspection's
  `PRAGMA foreign_key_list`). Any rebuild Ferro runs therefore starts with
  enforcement **on**, on a pooled connection, and must turn it off on the
  *same* connection before `BEGIN`.
- **No transaction on SQLite.** The reconciliation pass runs each table's
  plan inside one transaction on Postgres and statement-at-a-time on SQLite
  (`src/migrate.rs` lines 606-612: "FF-G G3: Postgres DDL is transactional —
  run this table's whole plan in one transaction … SQLite keeps
  statement-at-a-time execution below"; also the `migrate()` docstring at
  line 783). SQLite DDL *is* transactional (§1.3), so this is a choice Ferro
  made, not a SQLite limit; it matters because a rebuild is five to twelve
  statements that must be atomic, and because the pragma has to be toggled
  *around* that transaction.
- **The Alembic bridge does not use batch mode.** `src/ferro/migrations/
  alembic.py` mentions `batch_alter_table` only in two comments (lines
  822-824, 1067-1068); no comparator emits a batch op and nothing sets
  `render_as_batch`. The Alembic door on SQLite today is whatever a plain
  autogenerate emits — and Alembic's own SQLite impl raises
  `NotImplementedError` for `add_constraint` / `drop_constraint` outside
  batch (§2.2), so the "use Alembic's batch mode" pointer in Ferro's
  warnings means "hand-edit the revision into a `with op.batch_alter_table()`
  block, with `PRAGMA foreign_keys` handled in `env.py`".

### 4.2 Inventory of SQLite warn-skips

The ladder table in `docs/pages/guide/migrations.md` (lines 39-59) is the
user-facing statement. This is the same list traced to the code that decides
it, with the sqlite.org fact that makes each one a rebuild.

| # | Change | Where the SQLite branch lives | Today | Native ALTER? | Rebuild would make it |
| :- | :--- | :--- | :--- | :--- | :--- |
| 1 | Add table check (`__ferro_checks__`) to an existing table | `ferro_ddl_lowering::render_add_table_check` (`crates/ferro-ddl-lowering/src/lib.rs` 929-957) | warn: "SQLite cannot add a table constraint to an existing table (it requires a full table rebuild) … use Alembic's batch mode" | No — `ADD CONSTRAINT` does not exist | Doable: re-render `CREATE TABLE` with the check, copy rows (rows that violate the new CHECK fail the `INSERT … SELECT`, the same "validates existing rows" semantics Postgres's `ADD CONSTRAINT` has) |
| 2 | Rebuild table/column check on body drift | `render_check_rebuild` (1019-1053) | warn: "SQLite cannot alter constraints in place (it requires a full table rebuild). The live body remains" | No | Doable, same recipe |
| 3 | Drop orphaned ferro `ck_*` (`migrate_destructive`) | `render_check_drop` (1109-1131) | warn: "SQLite cannot drop a table constraint in place (it requires a full table rebuild)" | No — `DROP CONSTRAINT` does not exist | Doable |
| 4 | Column `db_check=True` on an existing column (add path and create path both) | `render_db_check` (845-868) | warn: "Check constraint '…' is not emitted on SQLite (requires table rebuild)" — note this also elides on `ADD COLUMN`, where SQLite *would* accept an inline column `CHECK` (§1.1) | `ADD COLUMN … CHECK (…)` is legal for a *new* column; not for an existing one | Doable for existing columns via rebuild; for new columns it is an in-place gap, not a rebuild question |
| 5 | Add FK constraint to an existing column (`AddForeignKey`) | `crates/ferro-migrate/src/emit.rs` 766-778 | warn: "SQLite cannot add table constraints to an existing table. Referential integrity … is not database-enforced" | No | Doable via rebuild; `PRAGMA foreign_key_check` after the copy reports the rows that would violate it |
| 6 | Add FK column (`AddColumn` of an FK-bearing column) | `emit.rs` 473-486 | column added, constraint elided with the same warning | `ADD COLUMN … REFERENCES …` **is** legal when the column's default is NULL (§1.1) — so this is an in-place gap the current emitter leaves on the table, not a rebuild case | In-place fix possible for the nullable/NULL-default shape; rebuild for the rest |
| 7 | Change FK `on_delete` or target (`RebuildForeignKey`) | `emit.rs` 841-869 | warn: "SQLite cannot alter constraints in place, so the live behavior remains" | No | Doable via rebuild |
| 8 | Change column type (`AlterColumnType`) | `emit_alter_column_type` (`emit.rs` 523-624, SQLite branch at 599), gated on `sqlite_type_storage_drift` (`ferro-ddl-lowering` 2724-2757: drift is a change of *affinity class*, not of spelling) | warn: "SQLite cannot change column types in place" | No `ALTER COLUMN … TYPE` at any version | Doable via rebuild; the copy step is where a cast/`USING` equivalent goes (`INSERT … SELECT CAST(col AS …)`) |
| 9 | Change nullability (`AlterColumnNullability`) | `emit_alter_column_nullability` (`emit.rs` 627-678, SQLite branch at 655) | warn: "SQLite cannot change column nullability in place" | **Yes on 3.53.0+** (`ALTER COLUMN … SET/DROP NOT NULL`); no on the bundled 3.46.0 | Rebuild today; a one-statement ALTER after a sqlx/libsqlite3-sys bump. NOT NULL tightening fails on existing NULLs either way unless backfilled first |
| 10 | Inline single-column `UNIQUE` on an existing column | never planned (ladder row "❌ never — Alembic territory") | — | `ADD COLUMN` refuses `UNIQUE`; Ferro's canonical unique shape is a standalone `uq_*` unique *index* on both dialects (`emit.rs` 438-448; `docs/solutions/patterns/index-unique-redundancy.md`), which `CREATE UNIQUE INDEX` already handles in place | Not a rebuild case for Ferro-declared uniques; only a foreign inline `UNIQUE` would need one |
| 11 | Rename column / table | never planned | — | **Native** since 3.25.0 / always (§1.1) | In-place, no rebuild; out of auto-migrate scope by policy (renames are indistinguishable from drop+add without a hint — sqldef's `@renamed` is the precedent) |
| 12 | Change primary key | never planned | hard error on `DropColumn` of a PK column (`emit.rs` 722-731) | No | Doable only via rebuild |
| 13 | Drop column | `execute_drop_column` (`src/migrate.rs` 449-490) | native `DROP COLUMN`; explicit covering indexes dropped first; refuses when the column is behind a UNIQUE/PK autoindex ("which SQLite cannot drop separately from the table definition") | Native since 3.35.0 **but** with the restriction list in §1.1 (PK, UNIQUE, indexed, FK-referenced, in a CHECK/generated column/trigger/view) | The refused shapes (PK/UNIQUE-autoindexed, CHECK-referenced, trigger/view-referenced) are exactly what a rebuild lifts — Alembic and Django both rebuild for these |
| 14 | Enum label changes | no-op on SQLite (text storage) | — | n/a | n/a |
| 15 | Row security | one warning per table (`sqlite_row_security_warning`, `ferro-ddl-lowering` 1435) | skip | n/a — SQLite has no RLS | Not a rebuild case; a true boundary |

Reading across the table: items **1-3, 5, 7, 8, 9, 12** and the refused
half of **13** are all the *same* operation on SQLite — rewrite the
`CREATE TABLE`, copy the rows, swap the tables. Item **4** (column check on a
brand-new column) and item **6** (FK on a brand-new nullable column) are
in-place gaps the current `ADD COLUMN` emitter leaves on purpose, and would
be fixed by rendering the inline clause rather than by any rebuild. Item 9
becomes native the moment the bundled SQLite is 3.53.0+. Items 10, 11, 14,
15 are not rebuild cases at all.

### 4.3 What Ferro already has that a rebuild would reuse

- **The full `CREATE TABLE` renderer.** `ferro_migrate::render_create_table`
  (`emit.rs` 166-232) renders the model's whole table — inline FKs with
  their `fk_*` names (`name_sqlite_inline_fks`), inline table checks, the
  post-create `uq_*` / `idx_*` indexes and the `db_check` elision warning —
  from the IR. A rebuild's step 4 ("create the new table") is this function
  with a different table name, which is what the I-1 parity pin would hang
  on: the rebuilt table must be byte-identical to a fresh create. (This is
  the property Alembic's batch mode structurally lacks: it rebuilds from a
  *reflected* table plus deltas, which is why unnamed CHECKs fall out.)
- **Live-state readers.** `src/introspect.rs` already reads
  `PRAGMA table_info`, `index_list` / `index_info` (with the `origin`
  column that distinguishes explicit indexes from constraint autoindexes),
  and `foreign_key_list` (which exposes no constraint names). It does not
  read `sqlite_schema` for triggers or views, which step 3/8 of the recipe
  needs; Ferro emits neither, so on a Ferro-owned table the only such objects
  are foreign, and a rebuild has to decide whether to carry them
  (sqlite.org: re-create from the saved `sql`) or refuse when they exist.
- **The pool refresh.** `EngineHandle::refresh_pool()` runs after any DDL
  so no cached statement observes the old schema (`migrations.md` line 69;
  `docs/solutions/patterns/ddl-on-live-engine.md`). A rebuild that swaps the
  table by rename is precisely the case this primitive exists for. What it
  does not give is a *single dedicated connection* that holds the pragma
  state across `PRAGMA foreign_keys=OFF; BEGIN; …; COMMIT; PRAGMA
  foreign_keys=ON` — the pass today executes each statement through the
  pool, so consecutive statements may land on different connections.

## 5. Facts the decision (#470) needs, in one place

1. **A rebuild is one recipe, not many.** sqlite.org names one procedure for
   type changes, nullability, CHECK/FK/UNIQUE/PK add and drop, and column
   drops that native `DROP COLUMN` refuses. Alembic, Atlas and Django all
   implement that one procedure; sqldef declines it and emits nothing. If
   Ferro rebuilds, nine of its fifteen SQLite warn-skips collapse into one
   `MigrationOp::RebuildTable`-shaped emission.
2. **The recipe has three non-negotiable bracketing facts** that today's
   runtime pass violates: enforcement must be turned off *outside* a
   transaction on the *same* connection (a pooled, autocommit,
   statement-at-a-time executor cannot express this); the statements between
   must be one transaction; `PRAGMA foreign_key_check` must run before
   `COMMIT` and its rows must be an error. Django's guard (read the pragma
   back; refuse if still on) and its exit check are the reference shape.
3. **Skipping step 1 is silent data loss, not an error**, when the rebuilt
   table is a parent with `ON DELETE CASCADE` children: `DROP TABLE` runs an
   implicit `DELETE` that cascades. Ferro's default `on_delete` is CASCADE
   (`fk_action_from_str`), so this is the common case for Ferro tables.
4. **Ordering is create-copy-drop-rename.** Rename-first corrupts trigger,
   view and FK references since 3.26.0; sqlite.org calls it out with a
   "Caution" box. Temp-name precedents: `new_X` (sqlite.org, Atlas),
   `new__<table>` (Django), `_alembic_tmp_<table>` (Alembic, truncated to 50).
5. **Indexes, triggers, views die with the old table.** Ferro can regenerate
   its own indexes from the IR (byte-identical to a fresh create, the I-1
   pin); triggers and views are foreign objects it has to carry from
   `sqlite_schema` or refuse on.
6. **The bundled SQLite is 3.46.0.** `DROP COLUMN` and `RENAME COLUMN` are
   there; `ALTER COLUMN … SET/DROP NOT NULL` (3.53.0, 2026-04-09) is not, and
   arrives only with a `libsqlite3-sys` 0.38.x (SQLite 3.53.4) via a newer
   sqlx. Nullability is the only warn-skip whose answer changes with that
   bump; type changes never get a native ALTER.
7. **Two of the warn-skips are not rebuild questions.** `ADD COLUMN` with an
   inline column `CHECK` (3.37.0+ validates existing rows) and `ADD COLUMN …
   REFERENCES` with a NULL default are legal today; Ferro elides both. They
   are emitter gaps, addressable without any rebuild machinery.
8. **Alembic batch mode is not a complete door either.** It is steps 4-8
   with no pragma handling, drops unnamed CHECKs, cannot run `--sql` without
   `copy_from`, and Ferro's bridge does not enable `render_as_batch`. "Use
   Alembic's batch mode" in Ferro's warnings currently means a hand-written
   block plus `env.py` pragma plumbing the docs do not show.
9. **Rendering as a `.sql` step.** Atlas is the precedent for a planned
   file: the two pragmas as their own entries outside the transactional
   body, the body as ordinary statements. The `.sql` step's runner would
   have to execute the pragma lines outside the step's transaction — which
   is the same "can a DDL step declare itself non-transactional" question
   the map already lists under online-safety hints.
10. **The reverse of a rebuild is a rebuild** with the old `CREATE TABLE`
    (Atlas's `Reverse`), which for a type change means the reverse cast; the
    reverse of a NOT NULL tightening is trivially a rebuild without it.

## Sources

- [sq-alter]: https://www.sqlite.org/lang_altertable.html — §2 RENAME
  (3.25.0/3.26.0 reference rewriting, `legacy_alter_table`), §3 RENAME
  COLUMN, §4 ADD COLUMN restrictions (3.37.0 constraint testing), §5 DROP
  COLUMN restrictions, §5.1 "How It Works", §6 ALTER COLUMN NOT NULL
  (3.53.0, 2026-04-09), §7 `writable_schema`, §8 the 12-step procedure and
  the "Caution" ordering box, §9 "Why ALTER TABLE is such a problem".
- [sq-pragma-fk]: https://www.sqlite.org/pragma.html#pragma_foreign_keys
- [sq-pragma-fkc]: https://www.sqlite.org/pragma.html#pragma_foreign_key_check
- [sq-pragma-legacy]: https://www.sqlite.org/pragma.html#pragma_legacy_alter_table
- [sq-fk]: https://www.sqlite.org/foreignkeys.html#fk_schemacommands — §5
  "CREATE, ALTER and DROP TABLE commands" (implicit DELETE on DROP TABLE;
  `ADD COLUMN … REFERENCES` default-NULL rule).
- SQLite release history: https://www.sqlite.org/changes.html (3.53.0 –
  3.53.4, 2026).
- [al-batch]: https://alembic.sqlalchemy.org/en/latest/batch.html
- [al-batch-src]: https://github.com/sqlalchemy/alembic/blob/main/alembic/operations/batch.py
  (`BatchOperationsImpl.__init__` / `_should_recreate` / `flush`;
  `ApplyBatchImpl._calc_temp_name` / `_grab_table_elements` / `_create`).
- [al-sqlite-src]: https://github.com/sqlalchemy/alembic/blob/main/alembic/ddl/sqlite.py
  (`transactional_ddl`, `requires_recreate_in_batch`, `add_constraint`,
  `drop_constraint`).
- [al-changelog]: https://github.com/sqlalchemy/alembic/blob/main/docs/build/changelog.rst
  (1.20.0, released 2026-09-11: tickets 1768, 1844, 1845, 1846; 1.8.0:
  ticket 1021).
- [sqldef-doc]: https://github.com/sqldef/sqldef/blob/master/cmd-sqlite3def.md
- [sqldef-gen]: https://github.com/sqldef/sqldef/blob/master/schema/generator.go
  (`GeneratorModeSQLite3` arms; the empty `default:` in the modify-column
  switch; the CHECK "not supported" comment).
- [sqldef-tests]: https://github.com/sqldef/sqldef/blob/master/cmd/sqlite3def/tests.yml
  (`RenameColumn`, `RenameColumnWithTypeChange`, `ForeignKeyConstraint`).
- [pgdelta]: https://github.com/orgs/supabase/discussions/44938 — "pg-delta
  only targets Postgres 15+"; repository `supabase/pg-toolbelt`.
- [atlas]: https://github.com/ariga/atlas/blob/master/sql/sqlite/migrate.go
  (`PlanChanges` pragma bracketing, `modifyTable`, `copyRows`, `alterTable`).
- [django-schema]: https://github.com/django/django/blob/main/django/db/backends/sqlite3/schema.py
  (`__enter__` / `__exit__`, `_remake_table`, `add_field`, `remove_field`).
- [django-base]: https://github.com/django/django/blob/main/django/db/backends/sqlite3/base.py
  (`get_new_connection` pragmas, `disable_constraint_checking`,
  `check_constraints`).
- sqlx: `sqlx-sqlite-0.8.6/src/options/mod.rs` (default pragmas);
  `libsqlite3-sys-0.30.1/sqlite3/sqlite3.h` (`SQLITE_VERSION "3.46.0"`);
  https://crates.io/crates/libsqlite3-sys (0.38.2) and
  https://github.com/rusqlite/rusqlite/blob/master/libsqlite3-sys/sqlite3/sqlite3.h
  (`SQLITE_VERSION "3.53.4"`).
- Ferro: `docs/pages/guide/migrations.md` (ladder table),
  `docs/adr/0014-table-checks-sqlite-create-only.md`,
  `crates/ferro-migrate/src/emit.rs`, `crates/ferro-ddl-lowering/src/lib.rs`,
  `src/migrate.rs`, `src/introspect.rs`, `src/ferro/migrations/alembic.py`,
  all at `8a0e89d`.

[sq-alter]: https://www.sqlite.org/lang_altertable.html
[sq-pragma-fk]: https://www.sqlite.org/pragma.html#pragma_foreign_keys
[sq-pragma-fkc]: https://www.sqlite.org/pragma.html#pragma_foreign_key_check
[sq-pragma-legacy]: https://www.sqlite.org/pragma.html#pragma_legacy_alter_table
[sq-fk]: https://www.sqlite.org/foreignkeys.html#fk_schemacommands
[al-batch]: https://alembic.sqlalchemy.org/en/latest/batch.html
[al-batch-src]: https://github.com/sqlalchemy/alembic/blob/main/alembic/operations/batch.py
[al-sqlite-src]: https://github.com/sqlalchemy/alembic/blob/main/alembic/ddl/sqlite.py
[al-changelog]: https://github.com/sqlalchemy/alembic/blob/main/docs/build/changelog.rst
[sqldef-doc]: https://github.com/sqldef/sqldef/blob/master/cmd-sqlite3def.md
[sqldef-gen]: https://github.com/sqldef/sqldef/blob/master/schema/generator.go
[sqldef-tests]: https://github.com/sqldef/sqldef/blob/master/cmd/sqlite3def/tests.yml
[pgdelta]: https://github.com/orgs/supabase/discussions/44938
[atlas]: https://github.com/ariga/atlas/blob/master/sql/sqlite/migrate.go
[django-schema]: https://github.com/django/django/blob/main/django/db/backends/sqlite3/schema.py
[django-base]: https://github.com/django/django/blob/main/django/db/backends/sqlite3/base.py
