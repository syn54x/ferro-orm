---
title: Prior art for historical models, data steps, expand/contract splitting, and snapshot integrity
type: research
tags: [migrations, alembic, django, pgroll, reshape, prisma, drizzle, rails, ecto, atlas, convention]
related_issues: [452, 455, 457, 460, 463, 468, 475]
captured: 2026-09-28
---

# Migration prior art

What Django, Alembic, Rails, Ecto, Prisma, Drizzle, pgroll, Reshape and Atlas
already learned about the four things Ferro's in-house migration system has to
decide (map #452): historical models for data steps, data-step ergonomics,
expand/contract op splitting, and per-migration schema snapshots. Every claim
is cited to a primary source: the tool's own docs or its source code. Source
files are cited at the commit that was read (listed under *Sources*).

The short version, per ticket bullet:

- **Historical models.** Django's historical model is the fields, relations,
  `Meta` and opted-in managers of the model *as of that migration*, and nothing
  else: no custom methods, no custom `save()`, no constructors. It is rebuilt
  on every run by replaying every migration file's state operations; nothing is
  stored. Alembic has no such thing and says so: it recommends `sa.table()`
  stubs or raw SQL because a migration that imports the live model breaks the
  day the model changes. Rails learned the same scar (a local model class
  inside the migration) and then removed the advice altogether in favour of
  "don't do data migrations in migrations".
- **Data steps.** Every ORM-bundled tool makes reversibility explicit and
  opt-in (a second callable, a second SQL string, or `IrreversibleMigration`).
  None has a first-class batched or resumable data step; batching is a
  documented idiom (Django's `atomic = False` loop, Rails `in_batches`) or
  absent. Progress output exists only in Rails (`say_with_time`). Only Prisma
  and Atlas record *partial* progress of a migration in their tracking table.
- **Expand/contract.** pgroll and Reshape do not split a change into separate
  expand and contract *migrations*. Each operation is one declaration with
  three phases (start / complete / rollback-or-abort); additive work runs at
  start, destructive work at complete. NOT NULL is staged as a `CHECK (col IS
  NOT NULL) NOT VALID` constraint, validated at complete, then promoted to the
  column attribute. Backfills are declared as `up` / `down` SQL expressions on
  the operation, run in primary-key-ordered batches of 1000, and kept current
  by triggers. Both refuse to backfill a table with no primary key (pgroll
  falls back to a unique NOT NULL column), and pgroll refuses to mix a raw
  `sql` op with other ops.
- **Snapshots.** Atlas hashes the directory (`atlas.sum`, SHA-256, one line
  per file plus a directory sum) and refuses to run on mismatch; it also
  records a per-statement hash trail in the revisions table so an edited,
  partially applied file is caught. Prisma stores no snapshot; it stores a
  SHA-256 per applied migration in `_prisma_migrations` and derives "current
  schema" by replaying the directory into a shadow database. Django stores
  neither snapshot nor checksum, only `(app, name, applied)`; edits to applied
  files go undetected. Drizzle stores a snapshot per migration chained by
  `prevId`, hashes the SQL into `__drizzle_migrations`, and never verifies the
  hash on apply.

---

## 1. Historical models

### 1.1 Django: what `apps.get_model` hands a `RunPython` step

A Django data migration is a `RunPython(code, reverse_code=None, atomic=None,
hints=None, elidable=False)` operation whose callable receives "an instance of
`django.apps.registry.Apps` containing historical models that match the
operation's place in the project history" and a `SchemaEditor`
[source: https://docs.djangoproject.com/en/5.2/ref/migration-operations/#runpython].

**What the historical class has and lacks.** The topic guide is explicit:

> Because it's impossible to serialize arbitrary Python code, these historical
> models will not have any custom methods that you have defined. They will,
> however, have the same fields, relationships, managers (limited to those with
> `use_in_migrations = True`) and `Meta` options (also versioned, so they may
> be different from your current ones).
>
> This means that you will NOT have custom `save()` methods called on objects
> when you access them in migrations, and you will NOT have any custom
> constructors or instance methods. Plan appropriately!
[source: https://docs.djangoproject.com/en/5.2/topics/migrations/#historical-models]

The code that decides this is `ModelState.from_model`: it clones
`_meta.local_fields`, copies the `Meta` options it knows how to serialize,
flattens abstract bases and stores concrete bases as pointers, and keeps a
manager only when `manager.use_in_migrations` is true (or it is the base /
default manager, which gets a stub)
[source: django/django/db/migrations/state.py:`ModelState.from_model`].
`ModelState.render` then builds a fresh class with `type(self.name, bases,
body)` where `body` is the cloned fields, a synthesized `Meta`, `__module__ =
"__fake__"`, and the reconstructed managers
[source: django/django/db/migrations/state.py:`ModelState.render`]. Nothing
from the user's class body other than fields and opted-in managers survives.

Two things *do* survive, and they are the maintenance burden:

- **Field classes.** Custom field classes are imported by the migration file,
  so their `__init__`, `deconstruct()` and `get_internal_type()` must be kept
  alive "for as long as any migrations which reference the field exist"
  [source: https://docs.djangoproject.com/en/5.2/topics/migrations/#considerations-when-removing-model-fields].
- **Referenced callables and base classes.** Functions in `upload_to` /
  `limit_choices_to`, `use_in_migrations` managers and concrete base classes
  are serialized as import paths, "so you must always keep base classes around
  for as long as there is a migration that contains a reference to them"
  [source: https://docs.djangoproject.com/en/5.2/topics/migrations/#historical-models].

**Why direct imports are a trap.** "If you import models directly rather than
using the historical models, your migrations *may work initially* but will
fail in the future when you try to rerun old migrations (commonly, when you
set up a new installation and run through all the migrations to set up the
database)" [source: same section]. This is the failure Ferro's snapshot design
(#455) exists to prevent.

### 1.2 How Django reconstructs state

Nothing is stored. `MigrationExecutor._create_project_state` starts from an
empty `ProjectState(real_apps=unmigrated_apps)` and, when applied migrations
are wanted, computes the full forward plan from a clean start and calls
`migration.mutate_state(state, preserve=False)` for every applied migration in
plan order [source: django/django/db/migrations/executor.py:`_create_project_state`].
`Migration.mutate_state` folds each operation's `state_forwards` into the
`ProjectState` [source: django/django/db/migrations/migration.py:`mutate_state`].
The model classes are rendered lazily: `ProjectState.apps` is a cached
`StateApps`, which renders every `ModelState` into a fake app registry
(`StateApps.__init__` → `render_multiple`), and `RunPython.database_forwards`
calls `from_state.clear_delayed_apps_cache()` before invoking the user code so
the registry reflects the state at that point
[source: django/django/db/migrations/state.py:`StateApps`;
django/django/db/migrations/operations/special.py:`RunPython.database_forwards`].
The `migrate` command prints "Rendering model states..." for exactly this step
[source: django/django/core/management/commands/migrate.py].

The consequence for a replay-based design: every `migrate` run pays for
replaying the entire history and rendering every model, and state mutations
have to maintain a relations cache (`ProjectState._relations`,
`reload_model`) to keep that tolerable
[source: django/django/db/migrations/state.py:`ProjectState`]. A stored,
fingerprinted snapshot per migration (#455) sidesteps the replay entirely;
the price is that the snapshot must be trusted, which is section 4's subject.

### 1.3 What can be written into a Django migration file

Because the file *is* the snapshot, Django restricts what it can serialize:
built-in scalars and containers, `datetime`/`Decimal`/`UUID`/enums,
`functools.partial`, pathlib paths, "Any Django field", "Any function or method
reference (e.g. `datetime.datetime.today`) (must be in module's top-level
scope)", "Any class reference (must be in module's top-level scope)", and
"Anything with a custom `deconstruct()` method". It cannot serialize "Nested
classes", "Arbitrary class instances", or "Lambdas"
[source: https://docs.djangoproject.com/en/5.2/topics/migrations/#serializing-values].
A custom `deconstruct()` returns `(path, args, kwargs)` and the class should
implement `__eq__` so the autodetector can compare states
[source: same page, "Adding a deconstruct() method"].

For Ferro this is the argument for an IR snapshot (`ir.json`) over a Python
snapshot: the IR is data by construction, so the "what can I put in a
migration file" question never arises; the lambda-based predicates and checks
Ferro already uses would be unserializable under Django's rules.

### 1.4 Documented `RunPython` gotchas

- **Reversibility is opt-in.** "If this callable is omitted, migrating
  backwards will raise an exception"; `reversible` is simply
  `self.reverse_code is not None`, and `RunPython.noop` exists to make a
  one-way step formally reversible
  [source: https://docs.djangoproject.com/en/5.2/ref/migration-operations/#runpython;
  django/django/db/migrations/operations/special.py:`RunPython.reversible`].
- **DDL and data in one transaction bite on Postgres.** "On PostgreSQL, for
  example, you should avoid combining schema changes and `RunPython`
  operations in the same migration or you may hit errors like
  `OperationalError: cannot ALTER TABLE "mytable" because it has pending
  trigger events`" [source: same RunPython section]. Ferro's "a data step is
  data-only" rule (#455) is this gotcha turned into a design constraint.
- **`atomic` semantics differ by backend.** On SQLite and PostgreSQL "all
  migration operations will run inside a single transaction by default"; on
  MySQL and Oracle they run without one. `RunPython(atomic=...)` and
  `Migration.atomic = False` are the escape hatches
  [source: https://docs.djangoproject.com/en/5.2/topics/migrations/#transactions;
  django/django/db/migrations/migration.py:`Migration.apply`].
- **Routing is manual.** "`RunPython` does not magically alter the connection
  of the models for you; any model methods you call will go to the default
  database unless you give them the current database alias"
  [source: RunPython section]. `hints` are forwarded to routers'
  `allow_migrate` [source: https://docs.djangoproject.com/en/5.2/howto/writing-migrations/#data-migrations-and-multiple-databases].
- **Cross-app models need explicit dependencies** or `apps.get_model` raises
  `LookupError` [source: https://docs.djangoproject.com/en/5.2/topics/migrations/#accessing-models-from-other-apps].
- **Defaults are applied once, in Python.** `AddField(preserve_default=False)`
  exists because "Django never sets database defaults and always applies them
  in the Django ORM code" [source: https://docs.djangoproject.com/en/5.2/ref/migration-operations/#addfield].
  Adding a unique non-null field therefore needs three migrations: add
  nullable, backfill with `RunPython`, alter to unique; the docs note the race
  ("Objects created after the `AddField` and before `RunPython` will have their
  original `uuid`'s overwritten")
  [source: https://docs.djangoproject.com/en/5.2/howto/writing-migrations/#migrations-that-add-unique-fields].
- **`SeparateDatabaseAndState` / `RunSQL(state_operations=...)`** let the
  database change and the state change diverge deliberately, with the warning
  that getting them out of sync "can break the migration framework, even
  leading to data loss"
  [source: https://docs.djangoproject.com/en/5.2/ref/migration-operations/#separatedatabaseandstate].
- **`elidable`** marks a `RunPython`/`RunSQL` as droppable when squashing;
  otherwise squashing cannot optimize through it
  [source: https://docs.djangoproject.com/en/5.2/topics/migrations/#squashing-migrations].

### 1.5 Alembic: no historical models, by design

Alembic has no migration state and no historical model. Autogenerate compares
the live database against `target_metadata` and "is not intended to be
perfect. It is *always* necessary to manually review and correct the candidate
migrations"; it does not detect table or column renames, anonymously named
constraints, or (without flags) type and server-default changes
[source: https://alembic.sqlalchemy.org/en/latest/autogenerate.html#what-does-autogenerate-detect-and-what-does-it-not-detect].

Its guidance on data migrations follows from that:

- `op.execute` docs: "it's a recommended practice to at least ensure the
  definition of a table is self-contained within the migration script, rather
  than imported from a module that may break compatibility with older
  migrations"; and "Parameterized statements are discouraged here, as they
  *will not work* in offline mode"
  [source: https://alembic.sqlalchemy.org/en/latest/ops.html#alembic.operations.Operations.execute].
- `op.bulk_insert` takes "an ad-hoc table" built from `sa.table()` /
  `sa.column()`; `multiinsert=False` renders one INSERT per row so
  `inline_literal` values work offline
  [source: https://alembic.sqlalchemy.org/en/latest/ops.html#alembic.operations.Operations.bulk_insert].
- The cookbook's "Data Migrations - General Techniques" recipe: "Alembic
  migrations are designed for schema migrations. The nature of data migrations
  are inherently different and it's not in fact advisable in the general case
  to write data migrations that integrate with Alembic's schema versioning
  model. For example downgrades are difficult to address since they might
  require deletion of data, which may even not be possible to detect." It
  offers three approaches: small data via `bulk_insert`; a **separate script**
  run between two Alembic migrations ("The data migration script may also need
  a separate ORM model to handle intermediate state of the database"); and an
  **online migration** with dual writes, "very challenging and time demanding"
  [source: alembic/docs/build/cookbook.rst:"Data Migrations - General Techniques"].

The "separate ORM model to handle intermediate state" is exactly the
historical model Alembic declines to build. This is why Alembic data
migrations degrade to raw SQL, and it is the gap #455 fills.

### 1.6 Rails: the same scar, then a retreat

Older Rails guides carried a "Using Models in Your Migrations" section: using
an application model in a migration "can be done, but some caution should be
observed", because a later-added validation can reference a column "which is
not in the database when the first migration runs"; the fix was "to create a
local model within the migration" (`class Product < ActiveRecord::Base; end`
nested in the migration class) and to call `Product.reset_column_information`
after `add_column` "to refresh the ActiveRecord cache"
[source: https://guides.rubyonrails.org/v3.2/migrations.html#using-models-in-your-migrations].
The current guide no longer has that section; it now says "it is generally not
advised to perform data migrations using migration files" for three reasons
(separation of concerns, rollback complexity, performance) and points to
`script/` or the `maintenance_tasks` gem
[source: https://guides.rubyonrails.org/active_record_migrations.html, "Data Migrations"].
`reset_column_information` remains documented in the API
[source: https://api.rubyonrails.org/classes/ActiveRecord/Migration.html, "Using a model after changing its table"].

Read together: Django built the historical model and lives with its
maintenance cost; Alembic and Rails declined and pushed data migrations out of
the migration system. Ferro's IR snapshot is the third option: Django's
guarantee without the replay and without the Python-serialization rules.

---

## 2. Data-step ergonomics

### 2.1 Django `RunPython`

Covered in 1.4. Batching is an idiom, not a feature: the docs' example sets
`atomic = False` on the migration and loops `while
MyModel.objects.filter(uuid__isnull=True).exists(): with transaction.atomic():
... [:1000]` [source: https://docs.djangoproject.com/en/5.2/howto/writing-migrations/#non-atomic-migrations].
No progress output; no partial-completion record: `django_migrations` has only
`app`, `name`, `applied` [source: django/django/db/migrations/recorder.py:`MigrationRecorder.Migration`].
A failed non-atomic data migration is re-run from the top, which is why the
documented loop filters on "rows not yet done".

### 2.2 Rails / Active Record

- **Reversibility.** `change` auto-reverses a fixed list of commands
  (`add_column`, `add_index`, `create_table`, `rename_column`, ... ); anything
  else needs `reversible do |dir| dir.up {..}; dir.down {..} end` or explicit
  `up`/`down`; raise `ActiveRecord::IrreversibleMigration` to refuse
  [source: https://guides.rubyonrails.org/active_record_migrations.html, "Using the change Method", "Using reversible"].
- **Transactions.** "If the database adapter supports DDL transactions, all
  migrations will automatically be wrapped in a transaction"; opt out per
  migration with `disable_ddl_transaction!` ("you can still open your own
  transactions")
  [source: https://api.rubyonrails.org/classes/ActiveRecord/Migration.html, "Transactional Migrations"].
- **Batching.** `find_each` / `find_in_batches` / `in_batches` default to 1000
  rows ordered ascending by primary key, with `of:`, `start:`, `finish:`,
  `load:`, `order:` and `use_ranges:`; `in_batches` composes with
  `update_all` / `delete_all`
  [source: https://api.rubyonrails.org/classes/ActiveRecord/Batches.html].
  These are ORM helpers, not migration features.
- **Progress.** `say`, `say_with_time` ("Outputs text along with how long it
  took to run its block") and `suppress_messages`; `VERBOSE=false` silences
  [source: guide, "Controlling Output"].
- **Resume.** None; `schema_migrations` holds versions only.
- **Official posture.** Don't do data migrations in migrations (section 1.6).

### 2.3 Ecto (`Ecto.Migration`)

- **Reversibility.** `change/0` auto-reverses; "not all commands are
  reversible. Trying to rollback a non-reversible command will raise an
  `Ecto.MigrationError`". `execute/1` is one-way; `execute/2` takes "a pair of
  plain SQL strings. The first is run on forward migrations (`up/0`) and the
  second when rolling back (`down/0`)"
  [source: https://ecto-sql.hexdocs.pm/Ecto.Migration.html].
- **Deferred execution and `flush/0`.** "Most functions in this module, when
  executed inside of migrations, are not executed immediately. Instead they
  are performed after the relevant `up`, `change`, or `down` callback
  terminates." `flush/0` forces them so data code can see the new schema, and
  "will raise if it would be called from `change` function when doing a
  rollback" [source: same].
- **Transactions and locks.** A migration runs as one transaction;
  `@disable_ddl_transaction true` "removes the guarantee that all of the
  changes in the migration will happen at once, so you will want to keep it
  short", and `CREATE INDEX CONCURRENTLY` needs it together with
  `migration_lock: :pg_advisory_lock` or `@disable_migration_lock true`.
  "By default, Ecto will lock the migration source to throttle multiple nodes
  to run migrations one at a time." The migrator needs "at least two database
  connections... One is used to lock the "schema_migrations" table and the
  other one to effectively run the migrations"
  [source: same; https://ecto-sql.hexdocs.pm/Ecto.Migrator.html].
- **Hooks.** `after_begin/0` and `before_commit/0` run inside the migration
  transaction and "should consider both the up *and* down cases"
  [source: Ecto.Migration, "Transaction Callbacks"].
- **Batching / progress / resume.** None built in.

### 2.4 Prisma Migrate

- **No down migrations.** Down SQL is hand-generated with `prisma migrate diff
  --from-schema ... --to-migrations ... --script`, applied with `prisma db
  execute`, and recorded with `prisma migrate resolve --rolled-back <name>`,
  which "can only be used on failed migrations"
  [source: https://www.prisma.io/docs/orm/prisma-migrate/workflows/generating-down-migrations].
  The engine's own FAQ, "Why does Migrate not have down/rollback migrations?",
  argues they serve different purposes in development and production
  [source: prisma-engines schema-engine/ARCHITECTURE.md, FAQ].
- **Data migrations are custom SQL.** The expand-and-contract guide: "first
  add the new column and copy the data across (expand), then remove the old
  column once nothing reads it (contract)", with the data step written into
  the migration
  [source: https://www.prisma.io/docs/guides/data-migration].
- **Transactions.** The Postgres connector applies a migration script with a
  single `raw_cmd(script)`; no `BEGIN`/`COMMIT` is added around user
  migrations (the only explicit `BEGIN` is for the shadow-database init
  script) [source: prisma-engines
  schema-engine/connectors/sql-schema-connector/src/flavour/postgres.rs:`apply_migration_script`].
  The docs list "add a mandatory (`NOT NULL`) column to a table that already
  has data" among the ways a migration fails and leaves the history dirty
  [source: https://www.prisma.io/docs/orm/prisma-migrate/workflows/patching-and-hotfixing].
- **Failure recording and resume.** `_prisma_migrations` stores `started_at`
  before applying, `finished_at` only on success, `logs` on error, and
  `rolled_back_at` from `resolve`; "started_at without finished_at nor
  rolled_back_at is the error state, by definition", and `deploy` "stops there
  with a detailed error". `applied_steps_count` "should be considered
  deprecated". `resolve --applied` never overwrites the failed row: it marks
  it rolled back and inserts a fresh one "because that would erase an event
  that actually happened from the record"
  [source: prisma-engines schema-engine/ARCHITECTURE.md, "_prisma_migrations";
  sql_migration_persistence.rs:`record_migration_started_impl`,
  `record_failed_step`, `record_migration_finished`].
- **Batching / progress.** None.

### 2.5 Drizzle Kit

- **No down migrations**; `drizzle-kit generate --custom` prepares an "empty
  migration file for custom SQL" for data migrations; `--> statement-breakpoint`
  splits statements [source: https://orm.drizzle.team/docs/migrations;
  drizzle-orm/drizzle-kit/src/cli/schema.ts; drizzle-orm/drizzle-orm/src/migrator.ts].
- **Transactions.** The Postgres, MySQL and SQLite migrators open one
  `session.transaction` around *all* pending migrations, executing each
  statement and inserting `(hash, created_at)` inside it
  [source: drizzle-orm/drizzle-orm/src/pg-core/dialect.ts:`migrate`;
  mysql-core/dialect.ts:`migrate`; sqlite-core/dialect.ts:`migrate`].
- **Batching / progress / resume.** None.

### 2.6 Alembic

Each revision's `downgrade()` is hand-written (autogenerate emits the inverse
for schema ops only). Transaction scope is the whole `upgrade` run unless
`transaction_per_migration=True` ("nest each migration script in a transaction
rather than the full series of migrations to run"); `transactional_ddl` is
per-dialect (PostgreSQL and MSSQL `True`; SQLite, MySQL, Oracle `False`), and
`autocommit_block()` exists because on PostgreSQL "many of its DDL operations
must be run outside of transaction blocks"
[source: alembic/alembic/runtime/environment.py:`configure`;
alembic/alembic/ddl/{postgresql,sqlite,mysql,oracle,mssql}.py:`transactional_ddl`;
https://alembic.sqlalchemy.org/en/latest/api/runtime.html].
`alembic_version` stores only `version_num` [source: runtime docs, `version_table`].

### 2.7 Comparison

| Tool | Reversibility of a data step | Transaction control | Batching helper | Progress | Records partial completion |
|---|---|---|---|---|---|
| Django `RunPython` | explicit `reverse_code`; `noop`; irreversible otherwise | per migration (`atomic`), per op (`RunPython(atomic=)`); DDL-tx backends only | none (documented loop idiom) | none | no (`app, name, applied`) |
| Rails | `change` auto-reverse list; `reversible`; `IrreversibleMigration` | per migration; `disable_ddl_transaction!` | `in_batches` / `find_each` (ORM, 1000, PK-ordered) | `say_with_time` | no |
| Ecto | `change` auto-reverse; `execute/2` up+down; `MigrationError` | per migration; `@disable_ddl_transaction`; migration lock | none | none | no |
| Prisma | none (hand-made down via `migrate diff`) | none added by the engine | none | none | yes (`started_at`/`finished_at`/`logs`/`rolled_back_at`) |
| Drizzle | none | one tx around all pending migrations | none | none | no |
| Alembic | hand-written `downgrade()` | whole run or per migration; dialect-dependent DDL tx | none | none | no (`version_num`) |

None of the ORM-bundled tools has a first-class resumable or chunked data
step. Ferro's per-step records (#457) already go past all of them; a data step
that records its own progress (row cursor) would be new ground.

---

## 3. Expand/contract generators: pgroll and Reshape

### 3.1 The shared model: one operation, three phases

Neither tool splits a schema change into separate expand and contract
*migrations*. A migration is a list of operations, each implementing
`Start` (additive; may declare a backfill), `Complete` (destructive; "should
be called once the previous version is no longer used") and
`Rollback` ("It is not possible to rollback a completed migration")
[source: pgroll/pkg/migrations/migrations.go:`Operation`]. Reshape's trait has
`run`, `complete` and `abort` with the same meanings, and its state machine is
`Idle → Applying → InProgress → Completing|Aborting → Idle`
[source: reshape/src/migrations/mod.rs; reshape/src/state.rs:`State`].

pgroll's docs describe the split: "During the migration start phase, `pgroll`
will perform only additive changes to the database schema. This includes:
creating new tables, adding new columns, and creating new indexes. ... During
the complete phase `pgroll` will perform all non-additive changes to the
database schema. This includes: dropping tables, dropping columns, and
dropping indexes" [source: pgroll/docs/concepts.md]. Both expose the old and
new shapes simultaneously through a Postgres schema of views per migration
(`SET search_path TO migration_<name>` in Reshape; a version schema named after
the migration file in pgroll) [source: reshape README "How it works";
pgroll/docs/operations/README.md].

The generator question in #475 ("split into `01_expand`, `02_backfill.py`,
`03_contract`") is therefore answered differently by these tools: they keep
the *declaration* whole and split the *execution* into phases, with the
backfill owned by the operation rather than by a separate step.

### 3.2 pgroll operation catalog

Files under `pgroll/pkg/migrations/` at commit 777a535:

| Operation (`op_*.go`) | Start | Complete | Backfill (`up`/`down`) |
|---|---|---|---|
| `create_table` | create the table under its final name | nothing (`Complete` returns nil); `Rollback` drops it | no |
| `add_column` | add `_pgroll_new_<col>` (nullable, no volatile default), NOT VALID check for NOT NULL, `up` trigger | rename temp column, validate+promote NOT NULL, drop trigger | `up` required when NOT NULL with no default (`Validate`: "`!o.Column.IsNullable() && o.Column.Default == nil && o.Up == ""`") |
| `alter_column` (sub-ops `change_type`, `add_not_null_constraint`, `drop_not_null_constraint`, `add_check_constraint`, `add_unique_constraint`, `add_foreign_key`, `change_default`, `change_comment`) | duplicate column + triggers | swap columns | `up` and `down` required for type change and NOT NULL ("Use `up` to migrate values from the nullable column in the old schema view to the `NOT NULL` column") |
| `rename_column`, `rename_table`, `rename_constraint` | view-only rename | physical rename | no |
| `drop_column` | hide column in new view; `down` trigger fills it for old-schema rows | drop column | `down` "required in order to backfill the previous version of the schema during an active migration" |
| `drop_table`, `drop_index`, `drop_constraint`, `drop_multicolumn_constraint` | hide | drop | `drop_constraint` refuses multi-column constraints (`MultiColumnConstraintsNotSupportedError`) |
| `create_index` | create the index | nothing; `Rollback` drops it | no |
| `create_constraint` | NOT VALID + triggers | validate | `up`/`down` per column |
| `set_replica_identity`, `set_default` | applied at start | nothing | no |
| `set_comment` (alter_column sub-op) | comment on the temporary column | comment on the final column | no |
| `sql` (raw) | runs `up` at start (or at complete with `onComplete`) | – | `down` optional, "must be idempotent"; not allowed with `onComplete` |

[sources: pgroll/docs/operations/*.mdx; pgroll/pkg/migrations/op_add_column.go:`Validate`;
op_create_table.go, op_create_index.go, op_drop_table.go, op_rename_column.go,
op_set_default.go, op_set_comment.go (`Start`/`Complete`/`Rollback`);
op_drop_constraint.go; op_raw_sql.go:`Validate`, `IsIsolated`]

**How NOT NULL is staged.** `add_column` and `add_not_null_constraint` add
`CONSTRAINT _pgroll_check_not_null_<col> CHECK (<col> IS NOT NULL) NOT VALID`
at start; at complete, `upgradeNotNullConstraintToNotNullAttribute` runs
`VALIDATE CONSTRAINT`, then `ALTER COLUMN ... SET NOT NULL`, then drops the
check ("Existing NULL values in the old column were rewritten using the `up`
SQL during backfill") [source: pgroll/pkg/migrations/op_add_column.go:
`upgradeNotNullConstraintToNotNullAttribute`, `NotNullConstraintName`;
op_set_notnull.go:`Start`, `Complete`]. This is the Postgres idiom for adding
NOT NULL without a full-table `ACCESS EXCLUSIVE` scan. Volatile defaults get
the same treatment: the column is added without the default, `up` must equal
the default expression (`UpSQLMustBeColumnDefaultError`: "volatile default
expression for column %q; "up" must be equal to "default""), and the default
is attached at complete [source: pgroll/docs/operations/add_column.mdx,
"Volatile and non-volatile defaults"; pkg/migrations/errors.go].

**How backfills are declared and run.** `up` and `down` are "PL/pgSQL
assignments executed by the triggers": `up` triggers "backfill existing
columns with the new values during `start` phase" and fire on writes to the
old schema; `down` triggers fire on writes to the new schema
[source: pgroll/docs/guides/updown.mdx]. The backfill "1. Get the primary key
column for the table. 2. Get the first batch of rows from the table, ordered by
the primary key. 3. Update each row in the batch, setting the value of the
primary key column to itself" so the trigger computes the value; "If there is
no primary key, look for a unique not null column"; "The table must have a PK
or a unique column"; `DefaultBatchSize = 1000`, with `--backfill-batch-size`
and `--backfill-batch-delay` flags on `start` and `migrate`
[source: pgroll/pkg/backfill/backfill.go:`Backfill.Start`;
pkg/backfill/config.go; cmd/start.go; cmd/migrate.go].

**What pgroll refuses.** Validation errors in `pkg/migrations/errors.go`
include: missing `up` for a NOT NULL column without default
(`ColumnMigrationMissingError`), redundant `up` for a nullable one
(`ColumnMigrationRedundantError`), `ColumnIsNotNullableError` /
`ColumnIsNullableError` for NOT NULL ops that would be no-ops,
`AlterColumnNoChangesError`, `MultiColumnConstraintsNotSupportedError` for
`drop_constraint`, identifier-length errors, and `InvalidMigrationError{"down
is not allowed with onComplete"}`. A raw `sql` op is *isolated*: "By default,
a `sql` operation cannot run together with other operations in the same
migration. This is to ensure pgroll can correctly track the state of the
database" (`IsIsolated() = !o.OnComplete`, enforced in
`Migration.Validate`: "operation %q cannot be executed with other
operations"), and the docs warn "`pgroll` is unable to guarantee that raw SQL
migrations are safe and will not result in application downtime"
[source: pgroll/docs/operations/raw_sql.mdx; pkg/migrations/migrations.go].

**State.** `pgroll.migrations (schema, name, migration jsonb, parent, done,
resulting_schema jsonb, ...)` with `CREATE UNIQUE INDEX only_one_active ON
migrations (schema, name, done) WHERE done = FALSE` ("Only one migration can be
active at a time") [source: pgroll/pkg/state/init.sql]. `resulting_schema` is
a stored snapshot of the schema after each migration, which is how pgroll
avoids re-introspecting for the next migration's `Validate`.

### 3.3 Reshape operation catalog

Files under `reshape/src/migrations/` at commit 1c52737: `create_table`,
`rename_table`, `remove_table`, `add_column`, `alter_column`, `remove_column`,
`add_index`, `remove_index`, `add_foreign_key`, `remove_foreign_key`,
`add_check`, `remove_check`, `create_enum`, `remove_enum`, `custom`.

- **`add_column`.** Adds a temporary column, and if `up` is given creates a
  `BEFORE INSERT OR UPDATE` trigger assigning `NEW."<temp>" = {up}` and calls
  `batch_touch_rows` to backfill. "Add a temporary NOT NULL constraint if the
  column shouldn't be nullable. This constraint is set as NOT VALID so it
  doesn't apply to existing rows": `CHECK ("<col>" IS NOT NULL) NOT VALID`;
  `complete` runs `VALIDATE CONSTRAINT` then `ALTER COLUMN ... SET NOT NULL`
  and drops the check; the schema view reports the column nullable "until the
  migration completes"
  [source: reshape/src/migrations/add_column.rs:`run`, `complete`].
  `up` may be a cross-table form (`table`, `value`, `where`) for
  "Complex changes across tables" [source: reshape README, "Add column"].
- **`alter_column`.** Rename is view-only. Type or value changes create a
  temporary column with up and down triggers in both directions and
  `batch_touch_rows`; "When performing more complex changes than a rename,
  `up` and `down` should be provided" [source: README "Alter column";
  src/migrations/alter_column.rs:`run`]. NOT NULL tightening uses the same
  NOT VALID check → VALIDATE → SET NOT NULL sequence
  [source: alter_column.rs lines ~269–355].
- **`remove_column`.** The column stays until `complete`; a `down` expression
  installs a trigger so new-schema writes still populate it. "The `down`
  setting must be provided when the removed column is `NOT NULL` or doesn't
  have a default value" [source: README "Remove column";
  src/migrations/remove_column.rs]. Removing a NOT NULL column that has a
  cross-table `down` temporarily replaces NOT NULL by "two triggers, as NOT
  NULL itself can't be deferred" [source: remove_column.rs comments].
- **`custom`.** Raw SQL with optional `start`, `complete`, `abort` strings;
  `update_schema` is a no-op, so custom SQL is invisible to Reshape's schema
  model [source: reshape/src/migrations/custom.rs].
- **Backfill batching.** `batch_touch_rows`: `BATCH_SIZE = 1000`, keyset
  pagination on the primary key (`WHERE (pk...) > $1 ORDER BY pk... LIMIT
  1000`), updating a column to itself so the trigger fires; it fails when the
  table "has no primary key, which is required to identify its rows"
  [source: reshape/src/migrations/common.rs:`batch_touch_rows`,
  `get_primary_key_columns_for_table`].
- **Atomicity.** Migrations cannot run "and state update as a single
  transaction. If a migration unexpectedly fails without automatically
  aborting, this state saves us from dangling migrations. It forces the user
  to either run migrate again (which works as all migrations are idempotent)
  or abort" [source: reshape/src/lib.rs:`migrate`]. Idempotent actions plus a
  persisted `Applying` state is Reshape's resume story.
- **State.** `reshape.data (key, value jsonb)` and `reshape.migrations (name,
  description, actions)` [source: reshape/src/state.rs].

### 3.4 Side by side

| Rule | pgroll | Reshape |
|---|---|---|
| Additive at start, no rewrite | create table, add column, create index, renames (view-only), add constraints as NOT VALID | same; `add_index`, `create_enum`, renames |
| Shadow column + triggers + backfill | `add_column` with `up`; `alter_column` type / NOT NULL / check / unique / FK; `create_constraint` | `add_column` with `up`; `alter_column` beyond rename; `remove_column` with `down` |
| Destructive only at complete | drop table/column/index/constraint; physical renames | same; `remove_index` "deferred until migration completion" |
| NOT NULL staging | `CHECK (col IS NOT NULL) NOT VALID` → `VALIDATE` → `SET NOT NULL` → drop check | identical sequence |
| Backfill declaration | `up` / `down` SQL expressions on the op; PL/pgSQL assignment | `up` / `down` SQL expressions; cross-table `up`/`down` form |
| Batch | 1000 rows, PK order, `--backfill-batch-size/-delay` | 1000 rows, PK keyset, fixed |
| Table without PK | falls back to a unique NOT NULL column, else error | error |
| Raw SQL | `sql` op, isolated unless `onComplete`, `down` must be idempotent, "unable to guarantee ... safe" | `custom` op, no schema tracking |
| Concurrency | one active migration (partial unique index) | state machine in `reshape.data` |
| Rollback after complete | not possible | not possible |

Neither project documents a rationale for keeping expand and contract in one
declaration beyond the model itself; the design is implicit in the
`Start`/`Complete` interface and the per-migration version schema.

---

## 4. Snapshots and integrity

### 4.1 Atlas: `atlas.sum` plus per-statement hashes

**Directory hash.** `NewHashFile` walks the files in order, feeding each
file's *name* then its *bytes* into one running SHA-256 and recording, per
file, the base64 digest *so far*; `Sum()` hashes the (name, hash) pairs again
for the directory line. `MarshalText` renders `h1:<dirsum>\n` then `<file>
h1:<hash>\n` per file [source: atlas/sql/migrate/dir.go:`NewHashFile`,
`HashFile.Sum`, `HashFile.MarshalText`]. Because each entry is a running
hash, reordering or editing an earlier file changes every later line, and
the docs note "two branches that each add a migration conflict in two places:
the directory sum, and the end of the file where each appended its record"
[source: https://atlasgo.io/concepts/migration-directory-integrity].

**Validation.** `Validate(dir)` recomputes and compares; on mismatch it walks
the entries to report the first differing file and whether it was `removed`,
`edited` or added (`ChecksumError{File, Line, Pos, Reason}`); a directory with
files but no sum file is `ErrChecksumNotFound`
[source: atlas/sql/migrate/dir.go:`Validate`, `ErrChecksumMismatch`].
`Executor.ValidateDir` runs it before applying
[source: atlas/sql/migrate/migrate.go:`Executor.ValidateDir`]. "Atlas reports
a checksum mismatch on the next command, in CI as well as locally"; `atlas
migrate hash` recomputes after a merge, and `atlas migrate lint` gates logical
conflicts in CI [source: migration-directory-integrity docs].

**Revisions table.** `Revision{Version, Description, Type, Applied, Total,
ExecutedAt, ExecutionTime, Error, ErrorStmt, Hash, PartialHashes,
OperatorVersion}`; `Hash` is the file's sum-file hash and `PartialHashes` "is
the hashes of applied statements" [source: atlas/sql/migrate/migrate.go:
`Revision`]. On re-execution of a partially applied file, `Execute` checks
each already-applied statement's hash against the recorded partial and raises
`HistoryChangedError{file, stmt}` if they differ, then resumes from
`stmts[r.Applied:]`; on success `PartialHashes` is cleared
[source: migrate.go:`Executor.Execute`]. `--tx-mode file` (default) wraps each
file; `none` lets a retry "continue with the failed statement"
[source: https://atlasgo.io/versioned/apply].

**Current schema.** Not a stored snapshot. `migrate diff` replays the
directory into a dev database because "Parsing schemas and migrations is not
enough: every expression, constraint, default, function call, and DDL/DML
statement must be semantically valid and accepted by a real database", and
the dev database also normalizes the desired schema so comparisons don't
propose spurious changes [source: https://atlasgo.io/concepts/dev-database].

### 4.2 Prisma: checksums in the database, state from a shadow database

- **No stored snapshot.** "Migrations are black boxes: we do not parse SQL ...
  The only way to figure out what the effect of a migration is is to run it."
  Generating a migration replays the directory into the shadow database,
  introspects it as the starting point, computes the expected schema from the
  Prisma schema, and diffs [source: prisma-engines
  schema-engine/ARCHITECTURE.md, "How does Migrate use the shadow database?"].
  The shadow database is "a second, *temporary* database that is created and
  deleted automatically" per `migrate dev`, used to detect drift and to
  generate migrations; `shadowDatabaseUrl` for hosts that forbid `CREATE
  DATABASE` [source: https://www.prisma.io/docs/orm/prisma-migrate/understanding-prisma-migrate/shadow-database].
- **Checksum.** `compute_checksum` is SHA-256 of the script, formatted as
  64 hex chars; matching tolerates `\r\n`/`\n` differences "because git messes
  with line endings" and an older non-zero-padded format
  [source: prisma-engines schema-engine/connectors/schema-connector/src/checksum.rs].
  "`checksum` is the sha256 checksum of the migration file. We never overwrite
  this once it has been written" [source: ARCHITECTURE.md].
- **Mismatch handling.** `_prisma_migrations` is "used to check: If a
  migration was run against the database; If an applied migration was deleted;
  If an applied migration was changed"; `migrate dev` errors (and offers a
  reset), `migrate deploy` warns
  [source: https://www.prisma.io/docs/orm/prisma-migrate/understanding-prisma-migrate/migration-histories].
- **`migration_lock.toml`** is not state: it "is used to detect if you have
  attempted to change providers" [source: same page].

### 4.3 Django: replayed state, no checksums

- State is recomputed from files on every run (section 1.2). The recorder
  table has `app`, `name`, `applied` and nothing else
  [source: django/django/db/migrations/recorder.py]. There is no check that an
  applied file still matches what was applied.
- **Graph integrity.** `detect_conflicts` finds apps "with more than one leaf
  migration" (branch merges; resolved by `makemigrations --merge`);
  `check_consistent_history` raises `InconsistentMigrationHistory("Migration
  X is applied before its dependency Y")`, skipping unapplied squashed
  migrations whose `replaces` are all applied; a dependency on a missing node
  is `NodeNotFoundError` [source: django/django/db/migrations/loader.py:
  `detect_conflicts`, `check_consistent_history`, `build_graph`].
- **Drift.** The autodetector compares state-from-migrations with
  state-from-models (`makemigrations --check`), so file/model drift is
  caught; file/database drift is not, other than by `migrate` failing.
  `SeparateDatabaseAndState` is documented as the way to let them diverge on
  purpose, with the data-loss warning quoted in 1.4.

### 4.4 Drizzle: a snapshot per migration, chained by `prevId`

- `drizzle/meta/_journal.json` plus one `snapshot.json` per migration, "used to
  generate the next migration" [source: https://orm.drizzle.team/docs/migrations].
  Snapshots carry `id` and `prevId`
  [source: drizzle-orm/drizzle-kit/src/serializer/pgSchema.ts].
- **Integrity checks.** `validateWithReport` groups snapshots by `prevId`;
  `drizzle-kit check` reports "[a, b] are pointing to a parent snapshot:
  X/snapshot.json which is a collision" and aborts, i.e. it catches two
  branches generating from the same parent, and malformed or outdated
  snapshots (`drizzle-kit up` upgrades format)
  [source: drizzle-orm/drizzle-kit/src/utils.ts:`validateWithReport`;
  src/cli/commands/check.ts:`checkHandler`; https://orm.drizzle.team/docs/drizzle-kit-check].
- **Applied-file hash is written, never read.** `readMigrationFiles` stores
  `hash: sha256(query).hex`; the migrators select only the latest row's
  `created_at` and apply every folder whose timestamp is newer, then insert
  `(hash, created_at)`. The hash is not compared to anything
  [source: drizzle-orm/drizzle-orm/src/migrator.ts:`readMigrationFiles`;
  pg-core/dialect.ts:`migrate`]. Reordered or edited applied files go
  undetected at apply time.

### 4.5 Alembic

No snapshot, no checksum; `alembic_version.version_num` only. Multiple heads
are refused until `alembic merge`; `alembic check` (1.9+) runs the autogenerate
comparison and fails if it would emit operations, for CI
[source: https://alembic.sqlalchemy.org/en/latest/autogenerate.html#running-alembic-check-to-test-for-new-upgrade-operations;
https://alembic.sqlalchemy.org/en/latest/api/runtime.html].

### 4.6 Comparison

| | Snapshot per migration | Per-file checksum | Where | On mismatch | "Current schema" comes from |
|---|---|---|---|---|---|
| Atlas | no | yes, `atlas.sum` (SHA-256 running hash, name+bytes) + per-statement partials in `atlas_schema_revisions` | repo file + DB table | refuse (`checksum mismatch`, names file + reason); `HistoryChangedError` on partial replay | replay directory into dev database |
| Prisma | no | yes, SHA-256 hex in `_prisma_migrations.checksum` | DB table | `migrate dev` errors / `deploy` warns | replay directory into shadow database |
| Django | no (replayed in memory) | no | `django_migrations (app, name, applied)` | undetected | replay migration files in memory |
| Drizzle | yes, `snapshot.json` with `id`/`prevId` | SHA-256 written to `__drizzle_migrations.hash`, never compared | repo + DB table | `drizzle-kit check` catches `prevId` collisions only | last snapshot |
| Alembic | no | no | `alembic_version.version_num` | n/a (`alembic check` compares models vs live DB) | live database introspection |
| pgroll | yes, `resulting_schema jsonb` per migration in `pgroll.migrations` | no | DB table | n/a | stored `resulting_schema` |

**Failure modes, caught or missed.**

| Failure | Atlas | Prisma | Django | Drizzle |
|---|---|---|---|---|
| Applied file edited | caught (`atlas.sum` + partial hashes) | caught (checksum) | missed | missed |
| Files reordered / renamed | caught (running hash) | caught (name + checksum) | caught only via graph deps | missed |
| Two branches each add a migration | caught (sum conflict, `migrate lint`) | caught at replay (shadow DB failure) | caught (two leaves → `--merge`) | caught (`prevId` collision) |
| Live DB drifted by hand | caught by `schema apply`/`migrate lint` against dev DB, not by `apply` | caught by `migrate dev` drift check, not `deploy` | missed until `migrate` fails | missed |
| Snapshot disagrees with its SQL | n/a | n/a | n/a | missed (no cross-check) |

For Ferro's floor (#457: checksums on every `.sql`, `.py` and `ir.json`,
per-step records) the relevant scars are: Atlas's running hash makes
reordering visible and its per-statement partial hashes make *partial*
edits visible; Prisma's "never overwrite a written checksum" rule preserves the
audit trail; Drizzle shows that writing a hash you never read is not
integrity. The open question for #463 is the last row: a stored snapshot can
disagree with the SQL beside it, and only a replay (Atlas, Prisma) or a
snapshot-vs-live comparison (`status --check`) can catch that.

---

## Sources

Source repositories were cloned at these commits on 2026-09-28:

- pgroll `xataio/pgroll` @ `777a535` (`pkg/migrations/`, `pkg/backfill/`,
  `pkg/state/`, `cmd/`, `docs/`)
- Reshape `fabianlindfors/reshape` @ `1c52737` (`src/migrations/`, `src/lib.rs`,
  `src/state.rs`, `README.md`)
- Django `django/django` @ `9332b16` (`django/db/migrations/`)
- Alembic `sqlalchemy/alembic` @ `b42ebe1` (`alembic/runtime/environment.py`,
  `alembic/ddl/`, `docs/build/cookbook.rst`)
- Atlas `ariga/atlas` @ `ab87fbe` (`sql/migrate/dir.go`, `sql/migrate/migrate.go`)
- Drizzle `drizzle-team/drizzle-orm` @ `15454db` (`drizzle-orm/src/migrator.ts`,
  `drizzle-orm/src/*-core/dialect.ts`, `drizzle-kit/src/`)
- Prisma `prisma/prisma-engines` @ `main` via the GitHub API
  (`schema-engine/ARCHITECTURE.md`,
  `schema-engine/connectors/schema-connector/src/checksum.rs`,
  `schema-engine/connectors/sql-schema-connector/src/sql_migration_persistence.rs`,
  `.../flavour/postgres.rs`)

Documentation pages:

- Django 5.2: `topics/migrations/`, `ref/migration-operations/`,
  `howto/writing-migrations/`
- Alembic: `ops.html`, `cookbook.html`, `api/runtime.html`, `autogenerate.html`
- Rails: `guides.rubyonrails.org/active_record_migrations.html`,
  `guides.rubyonrails.org/v3.2/migrations.html`,
  `api.rubyonrails.org/classes/ActiveRecord/Migration.html`,
  `api.rubyonrails.org/classes/ActiveRecord/Batches.html`
- Ecto: `ecto-sql.hexdocs.pm/Ecto.Migration.html`, `Ecto.Migrator.html`
- Prisma: `orm/prisma-migrate/understanding-prisma-migrate/shadow-database`,
  `.../migration-histories`, `workflows/generating-down-migrations`,
  `workflows/patching-and-hotfixing`, `guides/data-migration`
- Drizzle: `orm.drizzle.team/docs/migrations`, `docs/drizzle-kit-check`
- Atlas: `atlasgo.io/concepts/migration-directory-integrity`,
  `concepts/dev-database`, `versioned/apply`
