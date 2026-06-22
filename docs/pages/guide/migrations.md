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
| Add unique column (`unique=True`) | ✅ via explicit unique index + warning | ✅ inline `UNIQUE` |
| Add foreign-key column | ✅ column only, no FK constraint + warning | ✅ column + FK constraint |
| Change column type | ⚠️ `UserWarning`, no DDL (SQLite type affinity makes drift mostly cosmetic) | ✅ `ALTER COLUMN ... TYPE ... USING` cast |
| Change nullability | ⚠️ `UserWarning`, no DDL | ✅ `SET NOT NULL` / `DROP NOT NULL` |
| Rename column/table, change primary key, drop table | ❌ never — Alembic territory | ❌ never |

Rules worth knowing:

- **NOT NULL additions need a literal default.** Existing rows must be backfilled, so a new required field without a literal default fails the connect with a clear error. Make it nullable, give it a default, or use Alembic.
- **Added columns reuse the exact `CREATE TABLE` DDL**, so a database brought forward by `migrate_updates` matches one created fresh, and `alembic revision --autogenerate` stays clean afterwards.
- **Postgres type changes take an exclusive lock** and fail the connect if existing data does not cast cleanly — fine for a development flag, but worth knowing.
- **The pool refreshes after any schema change**, so no cached statement or stale identity-mapped instance can observe the pre-migration schema.

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
