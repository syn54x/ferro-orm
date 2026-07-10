<!--
  MAINTAINERS: This is the single, cumulative upgrade guide. When a release
  introduces breaking changes, ADD a new "## Upgrading to X.Y" section at the
  TOP of the boundary list (below "Before you start") — do NOT create a new
  per-version page. Keep each section's copy/paste agent prompt in sync with the
  prose directly beneath it.
-->

# Upgrade Guide

This is the curated path for upgrading Ferro across releases that changed
behavior. The [changelog](../changelog.md) is the exhaustive per-version record;
this guide is the task-oriented "here is the work" companion for the boundaries
that need it.

Find your current version below, then work **top to bottom** through every
boundary section you cross. Each section describes the settled end state — what
the API does now, and what you change to get there.

## Which sections apply to you

| You are on | Read |
| --- | --- |
| `0.14.x`, upgrading to `0.15` | [Upgrading to 0.15](#upgrading-to-015) |
| `0.12.x` or `0.13.x`, upgrading to `0.15` | Both sections, top to bottom |
| `0.12.x` or `0.13.x`, upgrading to `0.14` | [Upgrading to 0.14](#upgrading-to-014) |
| `0.11.x` or earlier, upgrading to `0.14` | Both sections, top to bottom |
| `0.11.x` or earlier, stopping at `0.12` | [Upgrading to 0.12](#upgrading-to-012) |
| `0.11.x` or earlier, stopping at `0.13` | [Upgrading to 0.12](#upgrading-to-012), plus the [connection-routing note](#connection-routing-requires-a-session) in Upgrading to 0.14 |

Each section includes a copy/paste prompt for a coding agent. If you are crossing
more than one boundary, run the prompts **newest first, one at a time**, and
verify your suite between them.

## Before you start

Turn deprecation warnings into failures on a throwaway branch so nothing slips
through:

```bash
uv run pytest -W error::DeprecationWarning
```

This catches every **deprecation-based** change. It does **not** catch the silent
behavioral changes in `0.14` (`create()`/`save()` no longer upsert;
`limit()`/`offset()` on a mutation now raise) — those emit no warning and must be
reviewed by reading the relevant section below.

## Upgrading to 0.15

`0.15` adds JSONB support and makes **JSONB the default storage** for derived
json-family fields (`dict`/`list`/nested-model with no explicit `db_type`) on
PostgreSQL. Your model definitions do not change; your PostgreSQL columns might.
SQLite users are unaffected — both JSON tokens store identically there.

<!-- MAINTAINERS: keep this prompt in sync with the subsections below it. -->

??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.14.x to 0.15.
    Reference: https://ferro-orm.x54.sh/howto/upgrade-guide/#upgrading-to-015

    The one breaking change: derived dict/list/nested-model fields now store as
    JSONB on PostgreSQL (previously plain json). On the first connect with
    migrate_updates=True, each existing plain-json column with a derived
    declaration is rewritten in place with ALTER ... TYPE jsonb USING ... —
    values survive, but jsonb does not preserve dict key order.

    For each model field whose annotation is dict/list/a nested Pydantic model
    and which has NO explicit db_type:
    1. If the code iterates that field's keys relying on insertion order, or
       compares stored JSON text byte-for-byte, add db_type="json" to keep
       plain-json storage (zero migration).
    2. Otherwise leave it — the one-time ALTER upgrades it to JSONB.

    List every affected field as file:line with your keep/upgrade call and let
    me review before connecting to production with migrate_updates=True.
    Alembic users: autogenerate will propose the same type changes; review the
    migration instead.
    ```

### Derived JSON storage is JSONB on PostgreSQL

A bare `payload: dict` field now creates (and migrates to) a `jsonb` column.
`db_type="json"` is the explicit opt-out and produces no migration for
existing plain-json columns; `db_type="jsonb"` stays valid and simply states
the default. Key-order-sensitive workloads should opt out **before** upgrading.
See [JSON storage](../guide/models-and-fields.md#json-storage-json-and-jsonb)
and ADR-0005.

## Upgrading to 0.14

`0.14` makes the mutation surface strict — no write silently does more (or
different) work than you asked — routes every database failure through a typed
exception hierarchy, unifies how the Rust runtime emitter and the Alembic bridge
derive column types and names, and **removes** the operator-style predicates and
`col()` bridge that `0.12` deprecated. Your model definitions do not change.

<!-- MAINTAINERS: keep this prompt in sync with the subsections below it. -->
??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.13.x to 0.14.
    Reference: https://ferro-orm.x54.sh/howto/upgrade-guide/#upgrading-to-014

    Work in two passes. Do NOT open a PR until I have reviewed Pass 2.

    PASS 1 — mechanical replacements (safe to apply directly):
    1. Query predicates: replace operator-style `where(Model.field == value)` and any
       `col(...)` bridges with lambda predicates, e.g. `where(lambda m: m.field == value)`.
       `order_by` takes a lambda or a validated column-name string. Operator style and
       `col()` were removed in 0.14.
    2. Connection routing: any ORM or raw call that ran outside a session
       (`User.all()`, `ferro.execute(...)` with no active session) must run inside
       `async with ferro.engines.session("name"):`. Unsessioned calls now raise.
    3. Exceptions: replace `except RuntimeError` / `except ConnectionError` handlers
       around Ferro calls with the typed hierarchy from `ferro` (e.g.
       `UniqueViolationError`, `OperationalError`, `InterfaceError`). Match on type,
       not on driver-message text.

    PASS 2 — semantic changes (DO NOT rewrite; list each site as file:line and ask me):
    4. `Model.create(...)` and `instance.save()` no longer upsert. For each call site,
       tell me whether the code RELIED on the old overwrite-on-conflict behavior. If it
       did, I decide between `Model.upsert(...)` / `save(on_conflict="update")` and
       catching `UniqueViolationError`. Do not guess.
    5. `.limit()` / `.offset()` chained into `.update()` / `.delete()` now raise. List
       each site — bounding a mutation requires fetching primary keys first, and only I
       know whether the bound was intentional.
    6. Schema-emission defaults changed for derived types (plain `datetime` ->
       timestamptz, Enum -> native enum, `datetime.time` -> time). Auto-migrate refuses
       to rewrite existing columns and warns. For each affected model, list it and ask
       me to choose keep (`db_type=...`) vs a reviewed ALTER migration.

    VERIFY after Pass 1:
    - Run the test suite.
    - Run `pytest -W error::DeprecationWarning`. This catches the deprecation-based
      changes ONLY — items 4, 5, and 6 are silent and will not appear here.
    - Grep helper: grep -rn "\.limit(\|\.offset(" --include="*.py" src/ | grep "delete()\|update("

    Report a summary: what you changed in Pass 1, and the Pass 2 sites awaiting my decision.
    ```

### Predicates: operator style and `col()` are gone

`where()` accepts only lambda predicates. Operator-style predicates
(`Model.field == value`) and the `col()` bridge — both deprecated in `0.12` —
were removed. Class attributes are no longer replaced with `FieldProxy`, so
`Model.field` at class scope raises `AttributeError` under normal Pydantic
semantics. `order_by()` takes a lambda (`order_by(lambda t: t.created_at, "desc")`)
or a validated column-name string (`order_by("created_at", "desc")`). See
[Typed Query Predicates](../concepts/query-typing.md).

=== "Before (removed)"

    ```python
    adults = await User.where(User.age >= 18).all()
    admins = await User.where(User.role == "admin").all()
    ```

=== "After"

    ```python
    adults = await User.where(lambda t: t.age >= 18).all()
    admins = await User.where(lambda t: t.role == "admin").all()
    ```

### Connection routing requires a session

Unqualified operations (`User.all()`, `ferro.execute(...)`) once fell back to
implicit default-connection routing. That fallback is gone (it was removed in
`0.13`, one release ahead of the other deprecations, as part of the identity-map
and routing redesign). Wrap request- or task-scoped work in a session so routing,
the identity map, and transactions are explicit and isolated under concurrency. A
call with no active session and no `using=` now raises `RuntimeError`; an explicit
`using=` that names a different connection than the ambient session raises
`ValueError`. See [Identity Map](../concepts/identity-map.md).

=== "Before (removed)"

    ```python
    import ferro

    users = await User.where(lambda t: t.active == True).all()  # noqa: E712
    await ferro.execute("UPDATE users SET active = TRUE")
    ```

=== "After (ambient session)"

    ```python
    import ferro

    async with ferro.engines.session("app"):
        users = await User.where(lambda t: t.active == True).all()  # noqa: E712
        await ferro.execute("UPDATE users SET active = TRUE")
    ```

### create() no longer overwrites on primary-key conflict

Previously `create()` compiled to `INSERT ... ON CONFLICT (pk) DO UPDATE`: a
duplicate primary key silently **replaced** the existing row. It is now a plain
INSERT that raises [`UniqueViolationError`](../api/exceptions.md) and leaves
the existing row untouched.

=== "Before (v0.13: silent clobber)"

    ```python
    # Overwrote the stored row when id 1 already existed
    user = await User.create(id=1, email="taylor@example.com")
    ```

=== "After (insert-or-update, explicit)"

    ```python
    user = await User.upsert(id=1, email="taylor@example.com")
    ```

=== "After (insert only)"

    ```python
    from ferro import UniqueViolationError

    try:
        user = await User.create(id=1, email="taylor@example.com")
    except UniqueViolationError:
        ...  # duplicate — the stored row is untouched
    ```

### save() no longer upserts

`save()` now tracks whether the instance has been persisted: a new instance is
INSERTed, a fetched or previously saved instance is UPDATEd by primary key. Two
consequences for code written against the old behavior:

- Saving a *new* instance whose primary key is already taken raises
  `UniqueViolationError` instead of overwriting the stored row.
- Saving a *persisted* instance whose row has been deleted underneath raises
  `ModelDoesNotExist` instead of silently re-inserting it.

Where you deliberately used `save()` to insert-or-update, say so explicitly:

=== "Before (v0.13: every save upserted)"

    ```python
    profile = Profile(id=user_id, theme="dark")
    await profile.save()  # inserted or replaced, whichever applied
    ```

=== "After (explicit upsert)"

    ```python
    profile = Profile(id=user_id, theme="dark")
    await profile.save(on_conflict="update")

    # or the one-call convenience:
    profile = await Profile.upsert(id=user_id, theme="dark")
    ```

The conflict target is the **primary key only**, and the whole row is written
on update — see [Upsert](../guide/mutations.md#upsert) for the details.

### limit()/offset() on a mutating query raise

Portable SQL has no `DELETE ... LIMIT`; previously the pagination was silently
dropped and the mutation touched **every** matching row. `update()` and
`delete()` now raise `ValueError` when the query carries `limit()` or
`offset()`. To mutate a bounded subset, fetch primary keys first:

=== "Before (v0.13: limit silently ignored!)"

    ```python
    # Looked bounded — actually deleted every matching row
    await Job.where(lambda job: job.done == True).limit(100).delete()  # noqa: E712
    ```

=== "After (bounded by primary-key set)"

    ```python
    batch = await Job.where(lambda job: job.done == True).limit(100).all()  # noqa: E712
    ids = [job.id for job in batch]
    await Job.where(lambda job: job.id.in_(ids)).delete()
    ```

### Catch typed exceptions, not RuntimeError

Database failures now raise a DBAPI-shaped hierarchy rooted at
[`ferro.FerroError`](../api/exceptions.md) instead of bare `RuntimeError` /
`ConnectionError`. Broad handlers keep working during the transition
(`FerroError` is still an `Exception`), but move them over:

=== "Before"

    ```python
    try:
        await order.save()
    except RuntimeError as exc:
        if "unique" in str(exc).lower():  # matching driver text
            ...
    ```

=== "After"

    ```python
    from ferro import UniqueViolationError

    try:
        await order.save()
    except UniqueViolationError as exc:
        ...  # exc.sqlstate / exc.constraint / exc.driver_message for detail
    ```

`connect()` failures are now `ferro.OperationalError` (server or environment)
or `ferro.InterfaceError` (bad scheme or configuration) instead of
`ConnectionError`.

### Schema-emission default changes

`0.14` also unifies how the Rust runtime emitter and the Alembic bridge derive
column types and artifact names — one decision table, consumed by both (see
`docs/solutions/patterns/derived-type-and-naming-decision-table.md`). Three
storage defaults change for *derived* types (fields with no explicit
`db_type`). **Auto-migrate never rewrites an existing column silently**: where
your database was bootstrapped by an older emitter, Ferro warns and skips, and
you choose between the keep-recipe and the convert-recipe below.

#### Plain `datetime` fields are timezone-aware (`timestamptz`)

The Alembic bridge previously mapped a plain `datetime.datetime` field to a
timezone-naive `DateTime`; the runtime already used `timestamptz`. Both now
emit `timestamptz`. If your schema has naive `timestamp` columns (bootstrapped
via Alembic), auto-migrate refuses the conversion and warns.

=== "Keep the column naive"

    ```python
    class Event(Ferro):
        occurred_at: datetime.datetime = FerroField(db_type="timestamp")
    ```

=== "Convert intentionally (reviewed Alembic migration)"

    ```sql
    ALTER TABLE event
        ALTER COLUMN occurred_at TYPE timestamptz
        USING occurred_at AT TIME ZONE 'UTC';  -- your source timezone
    ```

#### Enum fields default to native Postgres enum types

The runtime emitter previously stored Enum fields as `varchar`; the Alembic
bridge already emitted a native enum type. Both now emit the native type
(created idempotently; on SQLite the column is `varchar(<max label length>)`,
matching SQLAlchemy). If your schema has varchar enum columns (bootstrapped by
the runtime), auto-migrate refuses the conversion and warns.

=== "Keep varchar storage"

    ```python
    class Account(Ferro):
        role: Role = FerroField(db_type="varchar", db_check=True)
    ```

=== "Convert intentionally (reviewed Alembic migration)"

    ```sql
    CREATE TYPE role AS ENUM ('admin', 'member');
    ALTER TABLE account
        ALTER COLUMN role TYPE role USING role::role;
    ```

#### `datetime.time` fields store as `time`

The runtime emitter previously stored `datetime.time` fields as `varchar` (the
bridge already said `TIME`). Both now emit `time`. Existing varchar columns:
auto-migrate refuses the conversion and warns — keep with
`db_type="varchar"`, or convert with a reviewed migration using an explicit
`USING` cast.

#### Constraint and index names are single-sourced

- **Foreign keys are now named** (`fk_<table>_<col>_<to_table>`) in the DDL of
  both emitters. Existing databases need **no action**: Alembic autogenerate
  compares foreign keys by signature, not name, and auto-migrate never alters
  existing foreign keys.
- **Single-column uniques become named unique indexes** (`uq_<table>_<col>`),
  the same shape composite uniques always had. On an existing database
  auto-migrate adds the named index (`CREATE UNIQUE INDEX IF NOT EXISTS` —
  additive and idempotent); the old inline artifact (e.g. `account_email_key`
  on Postgres) remains as a redundant duplicate until you drop it:

    ```sql
    ALTER TABLE account DROP CONSTRAINT account_email_key;
    ```

- Identifiers longer than 63 characters now truncate deterministically on both
  emitters (previously only some name kinds guarded, and PostgreSQL truncated
  the rest silently).
- **SQLite declared types now use SQLAlchemy's spellings** (`DATETIME`,
  `DATE`, `TIME`, `CHAR(32)`, `JSON`, `NUMERIC` instead of sea-query's
  `timestamp_with_timezone_text`-style names), and primary-key columns carry
  an explicit `NOT NULL`. Existing SQLite databases need **no action** —
  SQLite's type affinity makes the storage identical, and auto-migrate's
  affinity-class comparison treats both spellings as equivalent. Only newly
  created tables show the new spellings.

### Changed surfaces at a glance

| Surface | v0.13 behavior | v0.14 behavior | Migration |
| --- | --- | --- | --- |
| `Model.create(**fields)` on existing PK | Silently replaced the row | Raises `UniqueViolationError` | `Model.upsert(...)` if you wanted replace |
| `instance.save()` on a new instance with taken PK | Silently replaced the row | Raises `UniqueViolationError` | `save(on_conflict="update")` |
| `instance.save()` after the row was deleted | Silently re-inserted | Raises `ModelDoesNotExist` | Catch and re-`create()` if intended |
| `Query.limit(...).update()/.delete()` | Pagination silently ignored | Raises `ValueError` | Fetch PKs, mutate by PK set |
| Database failures | `RuntimeError(...)` with driver text | Typed `FerroError` subclasses | Catch by type, not message |
| Plain `datetime` field (Alembic bridge) | Naive `timestamp` | `timestamptz` | Keep via `db_type="timestamp"`, or convert with `USING ... AT TIME ZONE` |
| Enum field (runtime emitter) | `varchar` | Native PG enum type | Keep via `db_type="varchar"`, or convert with `USING col::<enum>` |
| `datetime.time` field (runtime emitter) | `varchar` | `time` | Keep via `db_type="varchar"`, or convert with `USING` |
| FK constraints | Anonymous | Named `fk_<table>_<col>_<to_table>` | None (names not compared) |
| Single-column unique | Inline column `UNIQUE` | Named `uq_<table>_<col>` unique index | Optional: drop the old inline constraint |

## Upgrading to 0.12

`0.12` is the first release built on Ferro's IR-first architecture: query
execution, schema/migration planning, codecs, hydration, and connection routing
all flow through one shared intermediate representation instead of several
independent code paths. A single source of truth removes whole classes of drift
bugs — predictable schema diffs, identical bind/null semantics across SQLite and
PostgreSQL, and explicit session-scoped runtime state. Your model definitions do
not change; this is about how a few APIs are called.

`0.12` introduced these as **deprecation warnings**; operator-predicate, routing,
and private Alembic JSON helper removals later landed (see
[Upgrading to 0.14](#upgrading-to-014)).

<!-- MAINTAINERS: keep this prompt in sync with the subsections below it. -->
??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.11.x to 0.12.
    Reference: https://ferro-orm.x54.sh/howto/upgrade-guide/#upgrading-to-012

    All 0.12 changes are deprecation-based and safe to apply mechanically. Apply them,
    then verify.

    1. Query predicates: replace operator-style `where(Model.field == value)` with
       lambda predicates `where(lambda m: m.field == value)`. (These were removed
       outright in 0.14 — if you are upgrading past 0.12, prefer lambdas now.)
    2. Connection routing: wrap request/task-scoped ORM and raw operations in
       `async with ferro.engines.session("name"):` instead of relying on implicit
       default-connection routing.
    3. Alembic metadata: replace
       `from ferro.migrations.alembic import _build_sa_table, _map_to_sa_type`
       with `from ferro.migrations import get_metadata; target_metadata = get_metadata()`
       in your Alembic env.py.

    VERIFY:
    - Run the test suite.
    - Run `pytest -W error::DeprecationWarning`; a clean run means no deprecated
      0.12-era surfaces remain on the paths you exercise.
    ```

### Alembic metadata: build from `get_metadata()`

The private JSON-derivation helpers `ferro.migrations.alembic._build_sa_table`
and `ferro.migrations.alembic._map_to_sa_type` were removed in `0.14`. Schema
metadata derives from SchemaIR through the public `get_metadata()` entry point;
use it directly in your Alembic `env.py`.

=== "Before (removed)"

    ```python
    from ferro.migrations.alembic import _build_sa_table, _map_to_sa_type
    ```

=== "After"

    ```python
    from ferro.migrations import get_metadata

    target_metadata = get_metadata()
    ```

### Deprecations introduced in 0.12, at a glance

| Deprecated in 0.12 | Replacement | Status |
| --- | --- | --- |
| `Model.where(Model.field OP value)` / `col()` | `where(lambda t: ...)` | Removed in `0.14` — see [Predicates](#predicates-operator-style-and-col-are-gone) |
| Unqualified ops outside an active session | `async with ferro.engines.session("name")` | Removed in `0.13` — see [Connection routing](#connection-routing-requires-a-session) |
| `ferro.migrations.alembic._build_sa_table` | `ferro.migrations.get_metadata()` | Removed in `0.14` — see [Alembic metadata](#alembic-metadata-build-from-get_metadata) |
| `ferro.migrations.alembic._map_to_sa_type` | `ferro.migrations.get_metadata()` | Removed in `0.14` — see [Alembic metadata](#alembic-metadata-build-from-get_metadata) |
