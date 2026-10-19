---
description: "What upgrading Ferro asks of your code, release by release, and how to make each change."
---

<!--
  MAINTAINERS: This is the single, cumulative upgrade guide. When a release
  asks something of its users, ADD a new "## Upgrading to X.Y" section at the
  TOP of the section list (above the previous newest one), and a row to the
  "needs no action" table when it asks nothing. Do NOT create a new per-version
  page. Keep each section's copy/paste agent prompt in sync with the prose
  directly beneath it. Every recipe uses the `Account` model and runs: inline
  blocks under `{test=run prelude=upgrade}`, field-declaring recipes in
  `docs/examples/upgrade_*.py` with an Annotated twin.
-->

# Upgrading between versions

This guide lists what upgrading Ferro asks of your code, release by release from `0.11` to `0.25`; the [changelog](changelog.md) is the exhaustive record. After an upgrade, a query reads like this, inside a session:

```python {test=run prelude=upgrade}
from ferro import engines

async with engines.session():
    adults = await Account.where(lambda account: account.age >= 40).order_by("name").all()
    assert [account.name for account in adults] == ["Alan Turing", "Grace Hopper"]
```

The query returns the two accounts aged 40 or over, in name order. Every recipe on this page reads one model, `Account`, declared here once:

=== "Assignment"

    ```python
    --8<-- "docs/examples/upgrade_account.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/upgrade_account_annotated.py:models"
    ```

Every block below that calls a model runs inside a session on four accounts, as the first block does; see [Connections › Sessions](guide/connections.md#sessions). Each section shows only what changed.

## Which sections apply

Sections run newest first. From version X, read every section for a release newer than X, up to the release you are moving to, top to bottom within that span:

| You are on | Moving to | Read |
| --- | --- | --- |
| `0.24.x` | `0.25` | [Upgrading to 0.25](#upgrading-to-025) |
| `0.15.x` | `0.25` | Every `0.16` to `0.25` section, and the [no-action table](#releases-that-need-no-action) |
| `0.14.x` | `0.25` | The `0.15` section as well |
| `0.12.x` or `0.13.x` | `0.25` | The `0.14` section as well |
| `0.11.x` or earlier | `0.25` | Every section |
| `0.11.x` or earlier | `0.13` | [Upgrading to 0.12](#upgrading-to-012) only |

Each section with code to change includes a copy/paste prompt for a coding agent. If you cross more than one boundary, run the prompts **newest first, one at a time**, and run your tests between them.

## Before you start

Turn deprecation warnings into failures on a throwaway branch, so nothing that still warns slips through:

```bash
uv run pytest -W error::DeprecationWarning
```

The run catches every change a release announced with a `DeprecationWarning`. It does not catch the silent behaviour changes in `0.14` (`create()` and `save()` stop upserting, and `limit()` or `offset()` on a mutation raise), the `NULL` ordering change in `0.21`, or the Postgres timeouts of `0.22`: those emit no warning, so read their sections.

If you are moving from `0.12` or earlier straight past `0.14`, the deprecated APIs are already removed there and fail with `AttributeError` or `RuntimeError`, not a warning, so the run above catches nothing. Run your suite on `0.13` first, or apply the prompts before you upgrade.

## Upgrading to 0.25

`0.25` adds [`ferro migrate squash`](reference/cli.md#ferro-migrate-squash-name-from-target), which asks nothing of you. It also writes two things `0.24` cannot read, which matters only while both versions share one migrations directory and one database, as in a rolling deploy:

- **Schema snapshots.** Every snapshot `0.25` writes uses a newer snapshot format. A `0.24` checkout refuses the first migration that carries one, so finish rolling out `0.25` before anyone runs `ferro migrate new`.
- **Inherited rows.** A database that inherited a squash holds rows with `origin = inherited`. A `0.24` runner fails every verb on them with `the tracking table holds a row ferro cannot read (origin)`. A squash is made before a branch merges, so only development databases meet this.

[Deploying migrations](guide/schema/deploying.md) lists the three ways back from the second. Reference: [Squashing a feature branch's migrations](guide/schema/squash.md).

## Upgrading to 0.24

`0.24` rewrites the output of `ferro migrate`. Every verb but `init` now opens with one line naming the database it works on (`default · sqlite:///./app.db`), a terminal gets a headed layout with colour, and a pipe or a CI log gets a plain form with ASCII markers. A script that reads the output needs a look in three places:

- The first line is new. Skip it, or read the exit code instead of the text.
- The one-line results scripts grep for (`ok: models match`, `no drift against`, `nothing to apply`), every refusal sentence and every `warning:` line are unchanged.
- A connection error masks the password as `***`; it used to print `<redacted>`.

`rich` now ships in the `cli` extra, so `pip install "ferro-orm[cli]"` brings it. Reference: [What the output looks like](reference/cli.md#what-the-output-looks-like).

## Upgrading to 0.23

`0.23` makes a server default that no model declares count as drift, and makes the migrations door tighter about concurrent deploys. Two things ask something of you:

- **A hand-set server default is reported on every start.** Ferro keeps no server default of its own, so a column default someone added with raw SQL is now a *foreign default*. Under `migrate_updates` the pass warns and leaves it; under `migrate_destructive` it drops it on Postgres. `ferro migrate drift` lists it and `ferro migrate baseline` refuses it. Say a hand-set default sits on `seen_at`:

    ```python
    --8<-- "docs/examples/upgrade_foreign_default.py:warning"
    ```

    The pass reports one warning and changes nothing. To silence it, drop the default yourself (`ALTER TABLE "account" ALTER COLUMN "seen_at" DROP DEFAULT` on Postgres), or run the pass with `migrate_destructive=True`. On SQLite only a table rebuild drops a default, so the warning names the hand-written step that does it.

- **A start-up `baseline()` can branch on a type.** Two services that baseline one database at the same moment used to get a plain `MigrationRefused` from the loser. The loser now gets `AlreadyTrackedError`, a subclass, so `except MigrationRefused` still works and you can catch the narrower type. See [An app that migrates at start-up](guide/schema/adopting-migrations.md#an-app-that-migrates-at-start-up).

Also new, and opt-in: [`Field(retired=True)`](guide/schema/renames-and-retirements.md#retiring-a-column) and retired enum labels, which let a column or a label leave in two releases, and [`allow_ahead="expand-only"`](guide/schema/deploying.md), which lets an older release start beside a newer schema. A database whose last migration ran on `0.22` has its steps classed on the first run of `0.23`; [Deploying migrations](guide/schema/deploying.md) says how.

??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.22.x to 0.23.
    Reference: https://ferro-orm.x54.sh/upgrading/#upgrading-to-023

    1. Server defaults: search the repo's SQL and migration files for
       `SET DEFAULT` and `DEFAULT` clauses on columns no Ferro model declares a
       default for. List each as file:line. Do not drop any: I decide between
       dropping the default and keeping it.
    2. Start-up code that calls ferro.migrations.baseline(): if it catches
       MigrationRefused and matches the message text to detect "already
       adopted", change it to catch ferro.migrations.AlreadyTrackedError.
    ```

## Upgrading to 0.22

`0.22` adds Ferro's own migrations door: the `ferro` command, a project configuration file and numbered migration directories (see [Schema management](guide/schema/overview.md)). Nothing about adopting it is forced, but four things change for code that stays on auto-migrate:

- **A database that has run a migration refuses auto-migrate.** `connect(auto_migrate=…)`, `create_tables()` and `migrate()` raise before any DDL on a database that carries the `_ferro_migrations` tracking table, and the message names the two ways out. A database that never ran a migration, such as a test database, is unaffected. See [One door per database](guide/schema/overview.md#one-door-per-database).
- **Postgres passes wait under timeouts.** Each DDL statement waits up to `ddl_lock_timeout` (default `5s`) for its table lock and is retried up to ten times. A pass waits up to `lock_timeout` (default `30s`) for another pass or run that holds the run lock, then fails. Set both in the [project configuration](reference/configuration.md#ddl_lock_timeout) if your deploys hold locks longer.
- **`migrate()` and `create_tables()` return a report.** They returned `None`; they now return a `PassReport` of the statements the pass ran. Code that ignored the return value needs no change:

    ```python {test=run prelude=upgrade}
    import ferro

    report = await ferro.migrate()
    assert report.statements == () and report.warnings == ()
    ```

    The database already matched the models, so the pass ran nothing and warned of nothing.

- **A cancelled transaction rolls back.** A `transaction()` interrupted by task cancellation or `KeyboardInterrupt` now rolls back and releases its connection; before, the connection stayed pinned to the dead transaction:

    ```python {test=run prelude=upgrade}
    import asyncio

    from ferro import transaction


    async def work():
        async with transaction():
            await Account.create(name="Ghost", email="ghost@example.com", age=1)
            await asyncio.sleep(10)


    task = asyncio.ensure_future(work())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert await Account.where(lambda account: account.name == "Ghost").count() == 0
    ```

    The cancelled task left no `Ghost` row behind.

The `ferro` command is in an extra: `pip install "ferro-orm[cli]"`. Reference: [Configuration](reference/configuration.md) and [CLI](reference/cli.md).

## Upgrading to 0.21

`0.21` changes how `NULL` sorts and how a datetime, date, time, UUID or decimal in a query matches what `save()` stored. Both change results without an error.

### `NULL` ordering

An `order_by()` with no `nulls=` now puts `NULL` **last** on every database. It followed each database's own default before: Postgres sorted `NULL` first under `DESC`, and SQLite sorted it first under `ASC`. Name the placement you want, or ask for the database's own:

=== "Before (database default)"

    ```python {test=compile reason="shows the pre-0.21 call, which still runs but now sorts NULL last"}
    accounts = await Account.select().order_by(lambda account: account.seen_at).all()
    ```

=== "After (NULL first, as SQLite sorted it)"

    ```python {test=run prelude=upgrade}
    accounts = await Account.select().order_by(lambda account: account.seen_at, nulls="first").all()
    assert [account.name for account in accounts] == [
        "Grace Hopper",
        "Alan Turing",
        "Ada Lovelace",
        "Linus Torvalds",
    ]
    ```

Grace Hopper has never been seen, so her `seen_at` is `NULL` and she sorts first, ahead of the other three in date order. `nulls="native"` keeps each database's own behaviour, which differs between Postgres and SQLite for `"desc"`, so a query that must return the same pages on both should name `"first"` or `"last"`.

### Query literals

A `datetime`, `date`, `time`, `UUID` or `Decimal` in `where()`, `after()` or `update()` is now sent in the form `save()` writes. On SQLite a `datetime` is stored as text, and the old literal (`2026-03-01T15:00:00+00:00`) never equalled the stored one (`2026-03-01T15:00:00Z`), so an equality or a keyset page could miss its row. Pass the value itself, and drop any hand-formatted string you used to dodge the miss:

```python {test=run prelude=upgrade}
seen = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
by_value = await Account.where(lambda account: account.seen_at == seen).all()
assert [account.name for account in by_value] == ["Ada Lovelace"]
by_text = await Account.where(lambda account: account.seen_at == seen.isoformat()).all()
assert by_text == []
```

The value matches Ada's row. A string from `isoformat()` still misses, because the query sends a string as it is and the stored text ends in `Z`.

??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.20.x to 0.21.
    Reference: https://ferro-orm.x54.sh/upgrading/#upgrading-to-021

    1. Find every order_by() over a nullable column that omits `nulls=`. List
       each as file:line with the column, and ask me whether NULL should sort
       first, last, or follow the database ("native"). Do not guess.
    2. Find every where()/after()/update() that passes a datetime, date, time,
       UUID or Decimal, or a string built from one by isoformat()/str(). Pass the
       value itself. Query lambdas name their parameter after the model in
       lowercase singular: `lambda account: account.seen_at == seen`.
    ```

## Upgrading to 0.18

`0.18` makes a grown enum reach an existing Postgres database. Under `migrate_updates=True`, the pass appends each label the model declares and the live type lacks (`ALTER TYPE … ADD VALUE`), and warns of each live label the model no longer declares. Under plain `auto_migrate=True` an existing enum type is never changed, so a new member passes every test that builds a fresh schema and fails in production with `invalid input value for enum`. If your enums evolve, connect with `migrate_updates=True` or generate the change as a migration. SQLite stores enums as text and needs nothing. Reference: [Evolving enums](guide/schema/auto-migrate.md#evolving-enums-label-addition).

## Upgrading to 0.17.1

`0.17.1` makes the create pass leave existing tables alone: a plain `auto_migrate=True` no longer adds an index to a table that already exists. Add it under `migrate_updates=True`, or in a migration. The same release makes `migrate_updates` read live foreign keys, so a changed `on_delete` is now acted on: Postgres rebuilds the constraint, and SQLite warns, since it cannot alter one in place. Reference: [Applying column changes with `migrate_updates`](guide/schema/auto-migrate.md#applying-column-changes-with-migrate_updates).

## Upgrading to 0.16.3

`0.16.3` refuses a model that declares more than one `primary_key=True` field. The model used to save by its first key and raise when you traversed a relation; it now raises a `TypeError` naming the fields when the class is defined:

=== "Assignment"

    ```python
    --8<-- "docs/examples/upgrade_primary_key.py:refused"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/upgrade_primary_key_annotated.py:refused"
    ```

The class is refused at definition. Keep one primary key and let the other column be unique, as `Account` does:

=== "Assignment"

    ```python
    --8<-- "docs/examples/upgrade_account.py:primary-key"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/upgrade_account_annotated.py:primary-key"
    ```

If the pair of columns together must be unique, declare a [composite unique](guide/models-and-fields.md#composite-uniques). Reference: [Models and fields](guide/models-and-fields.md).

## Releases that need no action

These releases from `0.13` onward change nothing you have to do. Releases with a section above or below ask something of you.

| Release | What it adds | Action |
| --- | --- | --- |
| `0.21.2` | Alembic revisions create enum types a revision only adds a column of | None |
| `0.21.1` | Alembic downgrades drop the enum types their `create_table` made | None |
| `0.20.0` | `order_by(..., nulls=)` | None; the default moved in `0.21` |
| `0.19.0` | Named table checks (`__ferro_checks__`); Postgres 18's `RESTRICT` error is a `ForeignKeyViolationError` | None |
| `0.17.0` | `.exists()` on a reverse foreign key and `~` negation of any predicate | None |
| `0.16.2` | A self-referential foreign key no longer breaks `CREATE TABLE` ordering | None |
| `0.16.1` | `bulk_create()` splits batches under the backend's bind-parameter limit | None |
| `0.16.0` | Aggregates and grouped aggregates | None |
| `0.13.0` | Ferro warns and skips, rather than reinterpreting, a Postgres column whose `timestamp` or `timestamptz` storage differs from the model | None; the [`0.14` schema defaults](#schema-emission-defaults) build on it |

## Upgrading to 0.15

`0.15` adds JSONB support and makes **JSONB the default storage** for derived json-family fields (`dict`, `list` or a nested model with no `db_type`) on PostgreSQL. Your model definitions do not change; your PostgreSQL columns might. SQLite is unaffected, because both JSON tokens store the same there.

On the first connect with `migrate_updates=True`, each existing plain-`json` column with a derived declaration is rewritten in place (`ALTER … TYPE jsonb USING …`). The values survive, but `jsonb` does not preserve the order of a dict's keys. If your code iterates a field's keys in insertion order, or compares stored JSON text byte for byte, keep plain `json` before you upgrade:

=== "Assignment"

    ```python
    --8<-- "docs/examples/upgrade_keep.py:keep-json"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/upgrade_keep_annotated.py:keep-json"
    ```

The column stays plain `json`, and `0.15` writes no migration for it. `db_type="jsonb"` stays valid and states the default. Alembic autogenerate proposes the same type changes; review the migration instead. Reference: [JSON storage](guide/models-and-fields.md#json-storage-json-and-jsonb).

??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.14.x to 0.15.
    Reference: https://ferro-orm.x54.sh/upgrading/#upgrading-to-015

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

## Upgrading to 0.14

`0.14` makes the mutation surface strict, so no write silently does more or different work than you asked. It routes every database failure through a typed exception hierarchy, derives column types and names in one place for the runtime emitter and the Alembic bridge, and **removes** the operator-style predicates, the `col()` bridge and implicit connection routing that `0.12` deprecated. Your model definitions do not change.

??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.13.x to 0.14.
    Reference: https://ferro-orm.x54.sh/upgrading/#upgrading-to-014

    Work in two passes. Do NOT open a PR until I have reviewed Pass 2.

    PASS 1 — mechanical replacements (safe to apply directly):
    1. Query predicates: replace operator-style `where(Model.field == value)` and any
       `col(...)` bridges with lambda predicates named after the model in lowercase
       singular, e.g. `where(lambda account: account.field == value)`. `order_by`
       takes a lambda or a validated column-name string. Operator style and
       `col()` were removed in 0.14.
    2. Connection routing: any ORM or raw call that ran outside a session
       (`Account.all()`, `ferro.execute(...)` with no active session) must run
       inside `async with ferro.engines.session("name"):`. Unsessioned calls now
       raise.
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
       me to choose keep (`db_type=...`) vs a reviewed migration.

    VERIFY after Pass 1:
    - Run the test suite.
    - Run `pytest -W error::DeprecationWarning`. This catches the deprecation-based
      changes ONLY — items 4, 5, and 6 are silent and will not appear here.
    - Grep helper: grep -rn "\.limit(\|\.offset(" --include="*.py" src/ | grep "delete()\|update("

    Report a summary: what you changed in Pass 1, and the Pass 2 sites awaiting my decision.
    ```

### Operator predicates and `col()` {#predicates-operator-style-and-col-are-gone}

`where()` accepts only lambda predicates. Operator-style predicates (`Account.age >= 18`) and the `col()` bridge, both deprecated in `0.12`, are removed. Class attributes are no longer replaced with a field proxy, so `Account.age` at class scope raises `AttributeError` under normal Pydantic semantics. `order_by()` takes a lambda (`order_by(lambda account: account.seen_at, "desc")`) or a validated column-name string (`order_by("seen_at", "desc")`):

=== "Before (removed)"

    ```python {test=compile reason="shows the pre-0.14 operator style, which now raises"}
    adults = await Account.where(Account.age >= 40).all()
    admins = await Account.where(Account.role == "admin").all()
    ```

=== "After"

    ```python {test=run prelude=upgrade}
    adults = await Account.where(lambda account: account.age >= 40).order_by("name").all()
    assert [account.name for account in adults] == ["Alan Turing", "Grace Hopper"]
    admins = await Account.where(lambda account: account.role == Role.ADMIN).all()
    assert [account.name for account in admins] == ["Ada Lovelace"]
    ```

Both queries return the same rows they always did; only the predicate spelling changed. Reference: [Typed query predicates](concepts/query-typing.md).

### Session routing {#connection-routing-requires-a-session}

Unqualified operations (`Account.all()`, `ferro.execute(...)`) used to fall back to an implicit default connection. That fallback is removed. Wrap request- or task-scoped work in a session, so routing, the identity map and transactions are explicit and isolated under concurrency. A call with no active session and no `using=` raises `RuntimeError`; an explicit `using=` that names a different connection than the ambient session raises `ValueError`:

=== "Before (removed)"

    ```python {test=compile reason="shows the pre-0.14 implicit routing, which now raises RuntimeError"}
    import ferro

    accounts = await Account.where(lambda account: account.active == True).all()  # noqa: E712
    await ferro.execute("UPDATE account SET active = 1")
    ```

=== "After (ambient session)"

    ```python {test=run prelude=upgrade}
    import ferro

    async with ferro.engines.session():
        accounts = await Account.where(lambda account: account.active == True).all()  # noqa: E712
        await ferro.execute("UPDATE account SET active = 1")
        assert len(accounts) == 3
    ```

The session routes both calls to its connection, and the query returns the three active accounts. Reference: [Identity map](concepts/identity-map.md) and [Sessions](guide/connections.md#sessions).

### `create()` on a primary-key conflict

`create()` used to compile to `INSERT … ON CONFLICT (pk) DO UPDATE`, so a duplicate primary key silently **replaced** the existing row. It is a plain `INSERT` now: a duplicate raises [`UniqueViolationError`](api/exceptions.md) and the stored row stays as it was:

=== "Before (silent clobber)"

    ```python {test=compile reason="shows the pre-0.14 create(), which overwrote the stored row"}
    await Account.create(id=1, name="Ada", email="ada@example.com", age=1)
    ```

=== "After (insert or update, explicit)"

    ```python {test=run prelude=upgrade}
    account = await Account.upsert(id=1, name="Ada Lovelace", email="ada@example.com", age=37)
    assert account.age == 37
    ```

=== "After (insert only)"

    ```python {test=run prelude=upgrade}
    from ferro import UniqueViolationError

    try:
        await Account.create(id=1, name="Ada", email="ada@example.com", age=1)
    except UniqueViolationError:
        ada = await Account.where(lambda account: account.id == 1).first()
        assert ada.age == 36  # the stored row is untouched
    else:
        raise AssertionError("create() overwrote the stored row")
    ```

`upsert()` replaces the row; `create()` raises and leaves Ada at 36. Reference: [Mutations](guide/mutations.md).

### `save()` and upserts

`save()` now tracks whether the instance has been persisted: a new instance is inserted, and a fetched or previously saved instance is updated by primary key. Two consequences for code written against the old behaviour:

- Saving a *new* instance whose primary key is taken raises `UniqueViolationError` instead of overwriting the stored row.
- Saving a *persisted* instance whose row was deleted underneath raises `ModelDoesNotExist` instead of silently re-inserting it.

Where you used `save()` on purpose to insert or update, say so:

=== "Before (every save upserted)"

    ```python {test=compile reason="shows the pre-0.14 save(), which upserted every call"}
    account = Account(id=2, name="Grace Hopper", email="grace@example.com", age=46)
    await account.save()  # inserted or replaced, whichever applied
    ```

=== "After (explicit upsert)"

    ```python {test=run prelude=upgrade}
    account = Account(id=2, name="Grace Hopper", email="grace@example.com", age=46)
    await account.save(on_conflict="update")
    grace = await Account.where(lambda account: account.id == 2).first()
    assert grace.age == 46
    ```

The save updated Grace's row. The conflict target is the primary key only, and the whole row is written on update; see [Upsert](guide/mutations.md#upsert).

### `limit()` and `offset()` on mutations

Portable SQL has no `DELETE … LIMIT`. The pagination used to be silently dropped, so the mutation touched **every** matching row. `update()` and `delete()` now raise `ValueError` when the query carries `limit()` or `offset()`. To mutate a bounded subset, fetch primary keys first:

=== "Before (limit silently ignored)"

    ```python {test=compile reason="shows the pre-0.14 behaviour, which deleted every active account"}
    await Account.where(lambda account: account.active == True).limit(2).delete()  # noqa: E712
    ```

=== "After (bounded by a primary-key set)"

    ```python {test=run prelude=upgrade}
    try:
        await Account.where(lambda account: account.active == True).limit(2).delete()  # noqa: E712
    except ValueError:
        pass  # the bounded delete is refused
    else:
        raise AssertionError("delete() accepted limit()")

    batch = await Account.where(lambda account: account.active == True).order_by("id").limit(2).all()  # noqa: E712
    ids = [account.id for account in batch]
    await Account.where(lambda account: account.id.in_(ids)).delete()
    remaining = await Account.select().order_by("id").all()
    assert [account.name for account in remaining] == ["Alan Turing", "Linus Torvalds"]
    ```

The delete removed Ada and Grace, the first two active accounts, and no others. Reference: [Mutations](guide/mutations.md).

### Typed exceptions

Database failures raise a hierarchy rooted at [`ferro.FerroError`](api/exceptions.md) instead of bare `RuntimeError` or `ConnectionError`. Broad handlers keep working during the move (`FerroError` is still an `Exception`), but match on the type:

=== "Before"

    ```python {test=compile reason="shows the pre-0.14 handler, which matched driver text"}
    try:
        await account.save()
    except RuntimeError as error:
        if "unique" in str(error).lower():  # matching driver text
            ...
    ```

=== "After"

    ```python {test=run prelude=upgrade}
    from ferro import UniqueViolationError

    try:
        await Account(id=1, name="Dup", email="dup@example.com", age=1).save()
    except UniqueViolationError as error:
        assert error.driver_message  # error.sqlstate and error.constraint carry detail
    else:
        raise AssertionError("a taken primary key was accepted")
    ```

The handler runs on the type, with the driver's text kept as `driver_message`. `connect()` failures are `ferro.OperationalError` (server or environment) or `ferro.InterfaceError` (bad scheme or configuration) instead of `ConnectionError`. Reference: [Exceptions](api/exceptions.md).

### Schema-emission defaults {#schema-emission-defaults}

`0.14` also derives column types and artifact names in one place for the runtime emitter and the Alembic bridge. Three storage defaults change for *derived* types (fields with no explicit `db_type`). **Auto-migrate never rewrites an existing column silently**: where an older emitter built your database, Ferro warns and skips, and you choose between keeping the old storage and converting with a reviewed migration (a hand-written step in `ferro migrate`, or an Alembic revision).

#### Plain `datetime` fields are timezone-aware

The Alembic bridge used to map a plain `datetime` field to a naive `timestamp`; the runtime already used `timestamptz`. Both now emit `timestamptz`. If your schema has naive `timestamp` columns, auto-migrate refuses the conversion and warns. Keep the column naive by declaring it:

=== "Assignment"

    ```python
    --8<-- "docs/examples/upgrade_keep.py:keep-timestamp"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/upgrade_keep_annotated.py:keep-timestamp"
    ```

The column stays `timestamp` and the warning goes away. To convert it instead, run this in a reviewed migration, with your source time zone in place of `UTC`:

```sql
ALTER TABLE "account"
    ALTER COLUMN "seen_at" TYPE timestamptz
    USING "seen_at" AT TIME ZONE 'UTC';
```

The statement reads each stored value as UTC and rewrites the column as `timestamptz`.

#### Enum fields are native Postgres enums

The runtime emitter used to store an `Enum` field as `varchar`; the Alembic bridge already emitted a native enum type. Both now emit the native type, created idempotently (on SQLite the column is `varchar(<longest label>)`). If your schema has `varchar` enum columns, auto-migrate refuses the conversion and warns. Keep the text storage, with its check, by declaring it:

=== "Assignment"

    ```python
    --8<-- "docs/examples/upgrade_keep.py:keep-varchar"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/upgrade_keep_annotated.py:keep-varchar"
    ```

The column stays text, and `db_check=True` keeps its `CHECK` over the labels. To convert it, run this in a reviewed migration:

```sql
CREATE TYPE "role" AS ENUM ('admin', 'member');
ALTER TABLE "account"
    ALTER COLUMN "role" TYPE "role" USING "role"::"role";
```

The statement creates the enum type and casts each stored label into it.

#### `datetime.time` fields store as `time`

The runtime emitter used to store a `datetime.time` field as `varchar` (the bridge already said `TIME`). Both now emit `time`. A `time` annotation accepts no text `db_type`, so there is no declaration that keeps the old column: auto-migrate refuses the conversion and warns, and you convert with a reviewed migration that casts explicitly, as in `ALTER COLUMN "starts" TYPE time USING "starts"::time`.

#### Constraint and index names

- **Foreign keys are named** (`fk_<table>_<col>_<to_table>`) in the DDL of both emitters. Existing databases need no action: Alembic autogenerate compares foreign keys by signature, not name.
- **Single-column uniques become named unique indexes** (`uq_<table>_<col>`), the shape composite uniques always had. On an existing database auto-migrate adds the named index (`CREATE UNIQUE INDEX IF NOT EXISTS`, additive and idempotent); the old inline artifact (on Postgres, `account_email_key`) stays as a redundant duplicate until you drop it:

    ```sql
    ALTER TABLE "account" DROP CONSTRAINT account_email_key;
    ```

- **Identifiers longer than 63 characters truncate the same way** on both emitters. PostgreSQL used to truncate some of them silently.
- **SQLite declared types use SQLAlchemy's spellings** (`DATETIME`, `DATE`, `TIME`, `CHAR(32)`, `JSON`, `NUMERIC`), and primary-key columns carry an explicit `NOT NULL`. Existing SQLite databases need no action, because type affinity makes the storage identical; only new tables show the new spellings.

### Changed surfaces

| Surface | `0.13` behaviour | `0.14` behaviour | What you do |
| --- | --- | --- | --- |
| `Model.create(**fields)` on an existing primary key | Replaced the row | Raises `UniqueViolationError` | `Model.upsert(...)` if you wanted replace |
| `instance.save()` on a new instance with a taken primary key | Replaced the row | Raises `UniqueViolationError` | `save(on_conflict="update")` |
| `instance.save()` after the row was deleted | Re-inserted it | Raises `ModelDoesNotExist` | Catch it and `create()` again if intended |
| `query.limit(...).update()` or `.delete()` | Ignored the pagination | Raises `ValueError` | Fetch primary keys, mutate by that set |
| Database failures | `RuntimeError` with driver text | Typed `FerroError` subclasses | Catch by type |
| Plain `datetime` field (Alembic bridge) | Naive `timestamp` | `timestamptz` | Keep with `db_type="timestamp"`, or convert with `USING … AT TIME ZONE` |
| Enum field (runtime emitter) | `varchar` | Native Postgres enum | Keep with `db_type="text"`, or convert with `USING col::<enum>` |
| `datetime.time` field (runtime emitter) | `varchar` | `time` | Convert with a reviewed `USING` cast |
| Foreign keys | Anonymous | Named `fk_<table>_<col>_<to_table>` | Nothing; names are not compared |
| Single-column unique | Inline column `UNIQUE` | Named `uq_<table>_<col>` unique index | Optionally drop the old inline constraint |

## Upgrading to 0.12

`0.12` is the first release built on Ferro's IR-first architecture: query execution, schema and migration planning, codecs, hydration and connection routing all flow through one shared intermediate representation instead of several independent code paths. One source of truth removes whole classes of drift bugs, and gives predictable schema diffs, identical bind and null semantics across SQLite and PostgreSQL, and explicit session-scoped runtime state. Your model definitions do not change; a few APIs are called differently.

`0.12` introduced these as **deprecation warnings**; the removals landed in `0.14`, see [Upgrading to 0.14](#upgrading-to-014).

??? example "Automate this: copy this prompt to your coding agent"

    ```text
    You are helping upgrade a Python codebase from Ferro ORM 0.11.x to 0.12.
    Reference: https://ferro-orm.x54.sh/upgrading/#upgrading-to-012

    All 0.12 changes are deprecation-based and safe to apply mechanically. Apply them,
    then verify.

    1. Query predicates: replace operator-style `where(Model.field == value)` with
       lambda predicates named after the model in lowercase singular:
       `where(lambda account: account.field == value)`. (These were removed
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

### Alembic metadata from `get_metadata()`

The private helpers `ferro.migrations.alembic._build_sa_table` and `ferro.migrations.alembic._map_to_sa_type` were removed in `0.14`. Schema metadata comes from the public `get_metadata()` entry point; use it in your Alembic `env.py`:

=== "Before (removed)"

    ```python {test=compile reason="shows the removed private helpers"}
    from ferro.migrations.alembic import _build_sa_table, _map_to_sa_type
    ```

=== "After"

    ```python {test=run}
    from ferro.migrations import get_metadata

    target_metadata = get_metadata()
    assert target_metadata is not None
    ```

`target_metadata` is the SQLAlchemy metadata Alembic compares against. Reference: [Alembic](guide/schema/alembic.md).

### Deprecations of 0.12

| Deprecated in `0.12` | Replacement | Status |
| --- | --- | --- |
| `Model.where(Model.field OP value)` and `col()` | `where(lambda account: …)` | Removed in `0.14`, see [Operator predicates](#predicates-operator-style-and-col-are-gone) |
| Unqualified operations outside an active session | `async with ferro.engines.session("name")` | Removed in `0.14`, see [Session routing](#connection-routing-requires-a-session) |
| `ferro.migrations.alembic._build_sa_table` | `ferro.migrations.get_metadata()` | Removed in `0.14`, see [Alembic metadata](#alembic-metadata-from-get_metadata) |
| `ferro.migrations.alembic._map_to_sa_type` | `ferro.migrations.get_metadata()` | Removed in `0.14`, see [Alembic metadata](#alembic-metadata-from-get_metadata) |
