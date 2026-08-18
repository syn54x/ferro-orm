# Schema Migrations

Ferro offers a ladder of schema-management options: zero-config auto-migration for development, opt-in schema updates for fast iteration, and an Alembic bridge for production.

## Three Ways to Manage Schema

| Approach | Flag / tool | What it does | Best for |
| :--- | :--- | :--- | :--- |
| Auto-create | `connect(..., auto_migrate=True)` | Creates missing tables; never touches existing ones. | Development, tests, local-first apps |
| Auto-update | `connect(..., migrate_updates=True)` and optionally `migrate_destructive=True` | Additionally `ALTER`s existing tables to match the models. *0.11.0+* | Development while the schema is moving |
| Alembic | `ferro-orm[alembic]` + `alembic` CLI | Versioned, reviewable migration scripts. | Production |

The flags form a ladder — `migrate_destructive` implies `migrate_updates`, which implies `auto_migrate` — so passing just the strongest flag you want is enough.

## Auto-Migration

### Creating tables with `auto_migrate=True`

```python
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", auto_migrate=True)
```

Creates tables for every registered model (including many-to-many join tables) and leaves existing tables untouched.

### Applying column changes with `migrate_updates`

*Added in 0.11.0.* When models gain or change fields between runs, `migrate_updates=True` reconciles existing tables at connect time:

```python
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", migrate_updates=True)
```

What it covers is capability-relative per backend:

| Change | SQLite | PostgreSQL |
| :--- | :--- | :--- |
| Add missing column | ✅ `ADD COLUMN` | ✅ `ADD COLUMN` |
| Add the column's index (`index=True`) | ✅ `CREATE INDEX` | ✅ `CREATE INDEX` |
| Add composite index (`__ferro_composite_indexes__`) to existing columns | ✅ `CREATE INDEX` | ✅ `CREATE INDEX` |
| Add table check (`__ferro_checks__`) on CREATE TABLE | ✅ inline CHECK | ✅ inline CHECK |
| Add table check to existing table | ⚠️ `UserWarning`, no DDL | ✅ `ADD CONSTRAINT` |
| Rebuild table check on body drift | ⚠️ `UserWarning`, no DDL | ✅ rebuild: `DROP CONSTRAINT` + `ADD CONSTRAINT` |
| Add column `db_check=True` on existing column | ⚠️ `UserWarning`, no DDL | ✅ `ADD CONSTRAINT` |
| Leftover ferro check (`ck_*` live, removed from model) under `migrate_updates` | ⚠️ `UserWarning`, constraint stays | ⚠️ `UserWarning`, constraint stays |
| Drop orphaned ferro check (`ck_*`) | ⚠️ `UserWarning`, no DDL | ✅ with `migrate_destructive=True` |
| Add unique column (`unique=True`) | ✅ via explicit unique index + warning | ✅ inline `UNIQUE` |
| Add foreign-key column | ✅ column only, no FK constraint + warning | ✅ column + FK constraint |
| Add missing FK constraint to an existing column | ⚠️ `UserWarning`, no DDL | ✅ `ADD CONSTRAINT` |
| Change a foreign key's `on_delete` (or target) | ⚠️ `UserWarning`, no DDL | ✅ rebuild: `DROP CONSTRAINT` + `ADD CONSTRAINT` |
| Change column type | ⚠️ `UserWarning`, no DDL (SQLite type affinity makes drift mostly cosmetic) | ✅ `ALTER COLUMN ... TYPE ... USING` cast |
| Change nullability | ⚠️ `UserWarning`, no DDL | ✅ `SET NOT NULL` / `DROP NOT NULL` |
| Drop orphaned Ferro-named index (`idx_*` / `uq_*`) | ✅ with `migrate_destructive=True` | ✅ with `migrate_destructive=True` |
| Add a missing enum label (a `StrEnum` grew a member) | ✅ nothing to do — enums store as text | ✅ `ALTER TYPE ... ADD VALUE` *0.18.0+* |
| Remove or rename an enum label | ✅ nothing to do | ⚠️ `UserWarning`, no DDL — Alembic territory |
| Inline single-column `UNIQUE` on existing column, index option changes | ❌ never — Alembic territory | ❌ never |
| Rename column/table, change primary key, drop table | ❌ never — Alembic territory | ❌ never |

Rules worth knowing:

- **NOT NULL additions need a literal default.** Existing rows must be backfilled, so a new required field without a literal default fails the connect with a clear error. Make it nullable, give it a default, or use Alembic.
- **Added columns reuse the exact `CREATE TABLE` DDL**, so a database brought forward by `migrate_updates` matches one created fresh, and `alembic revision --autogenerate` stays clean afterwards.
- **Only ferro-owned constraints are rebuilt.** FK reconciliation matches the `fk_<table>_<col>_<to_table>` names ferro emits (just as index reconciliation only touches `idx_*`/`uq_*`). A drifting constraint with any other name is left untouched and reported with a `UserWarning` — user-created schema survives auto-migrate. Rebuilding is metadata-only: rows are never touched, and the new `ADD CONSTRAINT` validates existing rows, failing loudly (and rolling back the table's plan on Postgres) if they violate it.
- **Table checks and column `db_check` share the `ck_*` prefix.** Every ferro-owned `ck_*` — table checks from `__ferro_checks__` and column checks from `Field(db_check=True)` — participates in the same reconciliation pass on PostgreSQL: missing checks are added on `migrate_updates`, same-name body drift triggers a rebuild, and orphaned ferro-owned checks drop only on `migrate_destructive`. A leftover `ck_*` that the model no longer declares stays live under `migrate_updates` and emits a `UserWarning` (silence would leave the database rejecting rows the model now allows). On SQLite, table checks are inline at CREATE time but cannot be added, rebuilt, or dropped on an existing table without a full rebuild — the reconcile pass warns with the constraint name and skips. Column `db_check` on SQLite follows the same ALTER-shaped limitation.
- **Postgres type changes take an exclusive lock** and fail the connect if existing data does not cast cleanly — fine for a development flag, but worth knowing.
- **The pool refreshes after any schema change**, so no cached statement or stale identity-mapped instance can observe the pre-migration schema.

### Evolving enums: label addition

*Added in 0.18.0.* On PostgreSQL, `StrEnum` fields create a **native enum type**, and a type that already exists in the database does not learn new members on its own. When a `StrEnum` grows, `migrate_updates=True` performs **label addition**: it compares the model's members against the live type and appends what's missing with `ALTER TYPE ... ADD VALUE IF NOT EXISTS`.

!!! danger "This gap is invisible to your tests"
    Under plain `auto_migrate=True` (without `migrate_updates`), an existing enum type is **never** updated — like every existing object, it belongs to the update pass. The failure mode is nasty: every test suite that creates its schema fresh gets the complete enum and stays green, while every *existing* database rejects the new member at runtime with `invalid input value for enum`. No app-side test against a throwaway schema can catch this. If your models' enums evolve, run with `migrate_updates=True` (or generate the migration with Alembic — the [autogenerate bridge](#alembic-for-production) sees the same drift).

=== "Assignment"

    ```python
    from enum import StrEnum

    import ferro
    from ferro import Model


    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"  # new member — the live type only has 'plaid'


    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider


    await ferro.connect("postgres://...", migrate_updates=True)
    # → ALTER TYPE "provider" ADD VALUE IF NOT EXISTS 'mx'

    feed = await Feed.create(provider=Provider.MX)
    recent = await Feed.where(lambda feed: feed.provider == Provider.MX).all()
    ```

=== "Annotated"

    ```python
    from enum import StrEnum
    from typing import Annotated

    import ferro
    from ferro import FerroField, Model


    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"  # new member — the live type only has 'plaid'


    class Feed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        provider: Provider


    await ferro.connect("postgres://...", migrate_updates=True)
    # → ALTER TYPE "provider" ADD VALUE IF NOT EXISTS 'mx'

    feed = await Feed.create(provider=Provider.MX)
    recent = await Feed.where(lambda feed: feed.provider == Provider.MX).all()
    ```

The contract, precisely:

- **Append-only, metadata-only.** Label addition adds labels and does nothing else; rows are never touched. A shared `StrEnum` used by several models is one type and reconciles once.
- **Removals and renames are never automatic.** A live label the model no longer declares raises a `UserWarning` naming the type and labels — rows may still hold that label, and older code may still be running against the schema mid-deploy — and the label stays. Remove or rename labels in a reviewed Alembic migration.
- **Labels commit before table changes.** Additions run as their own autocommit statements ahead of the per-table plans, so a new column whose literal default is a brand-new member works in a single deploy, on every supported PostgreSQL version.
- **Appended labels sort last.** `ADD VALUE` appends: a member inserted mid-enum in Python lands at the end of the database ordering, and `ORDER BY` on an enum column follows *database* order, not declaration order.
- **SQLite is unaffected.** Enums store as text there; a new member needs no DDL.

### Destructive drops with `migrate_destructive`

*Added in 0.11.0.* Also **drop** live columns that no longer exist on the model (never whole tables):

```python
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", migrate_destructive=True)
```

Dropping is dependency-aware and fails loudly rather than skipping silently:

- Explicit indexes covering a dropped column are dropped first (they would be orphaned anyway).
- Columns that are **primary keys**, enforced by table constraints, or referenced by other tables' **foreign keys** abort with a clear error pointing at Alembic.

### On-demand `migrate()`

Run the same pass explicitly on a live connection instead of at connect time:

```python
import ferro

await ferro.migrate()                  # create missing tables + apply updates (default)
await ferro.migrate(destructive=True)  # also drop removed columns
await ferro.migrate(using="service")   # against a named connection
```

### Safety guidance

!!! danger "Never use destructive auto-migration in production"
    `auto_migrate` and its extension flags are for development and local-first apps whose schema is still moving. `migrate_destructive` deletes data the moment a field is removed from a model. For production, use [Alembic](#alembic-for-production) — renames, primary-key changes, and data transforms are deliberately out of auto-migrate's scope.

## Alembic for Production

Ferro doesn't reinvent migrations: it bridges your models into SQLAlchemy metadata that [Alembic](https://alembic.sqlalchemy.org/) — the industry-standard migration tool — uses to autogenerate versioned, reviewable migration scripts.

As of the IR-first cutover work, `get_metadata()` is built from the compiled SchemaIR modelset so runtime DDL and Alembic autogenerate consume the same schema artifacts.

### Install

```bash
pip install "ferro-orm[alembic]"
```

This adds Alembic and SQLAlchemy (used only for migration generation, not at runtime).

### Initialize

```bash
alembic init migrations
```

This scaffolds `alembic.ini` plus a `migrations/` directory containing `env.py` and `versions/`.

### Configure env.py

Point Alembic's `target_metadata` at Ferro's bridge. Models must be imported so they register:

```python
# migrations/env.py
from ferro.migrations import get_metadata

from myapp.models import Comment, Post, User  # noqa: F401 — importing registers models

target_metadata = get_metadata()

# The rest of env.py stays as generated.
```

`get_metadata()` produces a faithful SQLAlchemy reflection of your models (via SchemaIR):

- **Nullability** follows the same rules as the runtime schema: with the default `nullable="infer"`, a column is nullable iff its annotation allows `None` (a default alone does not make it nullable); shadow `*_id` columns infer from the *relation* annotation; `on_delete="SET NULL"` implies nullable; explicit `nullable=True/False` overrides. Primary keys are always `NOT NULL`.
- **Composite constraints** (`__ferro_composite_uniques__`, `__ferro_composite_indexes__`) emit matching `UniqueConstraint` / `Index` objects, including the automatic constraints on many-to-many join tables.
- **One-to-one** relations (`ForeignKey(unique=True)`) emit the same `UNIQUE` on the shadow column that `auto_migrate` creates at runtime.
- **Enums** map to named `sqlalchemy.Enum` types (class name lowercased, e.g. `UserRole` → `userrole`) so revisions compile on PostgreSQL, which rejects anonymous enum types.
- **Enum label drift is diffed.** *0.18.0+.* Alembic core is blind to enum value changes; ferro's bridge registers an autogenerate comparator that diffs each named enum type against the live PostgreSQL catalog — the same decision (and the same rendered SQL) the auto-migrate pass uses. A grown `StrEnum` generates `ALTER TYPE ... ADD VALUE IF NOT EXISTS` inside an `autocommit_block()` (placed before table operations, runnable on every supported PostgreSQL version); a live label the model no longer declares generates a comment in the revision telling you removal needs a hand-written step. Models in sync generate nothing.

### Autogenerate

```bash
alembic revision --autogenerate -m "add posts table"
```

Alembic diffs the metadata against the live database and writes a script to `migrations/versions/`.

### Review & apply

**Always review generated migrations** before applying them — autogenerate is a diff tool, not a judgment tool:

```bash
alembic upgrade head      # apply
alembic current           # show the applied revision
alembic downgrade -1      # roll back one revision
```

The day-to-day loop: change models → `alembic revision --autogenerate` → review → `alembic upgrade head` → commit the migration file. For data migrations and zero-downtime patterns (additive change → backfill → tighten), create empty revisions with `alembic revision -m "..."` and write the `op.execute(...)` steps yourself.

## Choosing a Workflow

- **Development**: `connect(..., migrate_updates=True)` (add `migrate_destructive=True` if you also want column drops). Your schema follows your models with zero ceremony, and warnings tell you when a change exceeds what in-place DDL can do.
- **Production**: Alembic, exclusively. Migrations are reviewed, versioned, reversible, and can express everything auto-migrate refuses to touch (renames, PK changes, data transforms). Back up before upgrading, and test `downgrade` paths.

Because `migrate_updates` emits the same DDL as a fresh `CREATE TABLE`, you can develop with auto-migration and switch to Alembic when the schema stabilizes — the first `--autogenerate` against an auto-migrated database produces a clean baseline.

## See Also

- [Connections & Databases](connections.md) — `connect()` options
- [Models & Fields](models-and-fields.md) — how fields map to columns
- [Relationships](relationships.md) — FK constraints and join tables
- [Migrations API reference](../api/migrations.md) — `get_metadata()` details
