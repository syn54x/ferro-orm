---
description: "How Postgres row-level security policies read session settings, and how Ferro delivers those settings on each connection."
status: new
---

# Row-level security and session settings

Postgres row-level security filters rows inside the database, using policies that read a setting on the connection; Ferro declares the policies on the model and sends the setting on every connection a session uses.

A session opened with a tenant's id reads only that tenant's invoices:

```python
--8<-- "docs/examples/row_level_security.py:scoped"
```

Two things made that happen. The `invoice` table carries a policy that compares each row's `tenant_id` with the connection's `app.tenant_id` setting, and Ferro put `app.tenant_id` on the connection before the query ran. Outside a `transaction()` block, the query reached Postgres as:

```sql
BEGIN;
SELECT set_config($1, $2, true);  -- 'app.tenant_id', 'a1a1a1a1-0000-4000-8000-000000000001'
-- the query itself
COMMIT;
```

The value lives until `COMMIT`, so the connection goes back to the pool with no tenant on it. This page explains each half: what the policy does with the setting, and how and when Ferro delivers it. [Scope rows by tenant](../guide/row-level-security.md) is the step-by-step setup, and the [reference](../reference/row-level-security.md) lists every option.

## Policy expressions

The `RowPolicy(column="tenant_id", setting="app.tenant_id")` shorthand renders this expression for both `USING` (rows a query sees) and `WITH CHECK` (rows a write may leave behind):

```sql
"tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid
```

`current_setting(key, true)` returns the setting's value, or `NULL` when it was never set, instead of raising. The `NULLIF` covers a second state. A custom setting that was set and later reset on a connection reads back as an empty string, not `NULL`, and `''::uuid` is an error. Closing a session under [connection delivery](#connection-delivery) resets the keys it set, so this state is common on a reused connection. With the `NULLIF`, both states fail closed:

| The setting on the connection | `current_setting(key, true)` | `NULLIF(..., '')::uuid` | Rows |
| :--- | :--- | :--- | :--- |
| never set | `NULL` | `NULL` | none |
| set, then reset | `''` | `NULL` | none |
| `'a1a1…'` | `'a1a1…'` | `'a1a1…'::uuid` | that tenant's |

Without the `NULLIF`, the second row raises `invalid input syntax for type uuid: ""` on every query against the table. Write the same wrapper into a policy you write in SQL.

## `FORCE` and `BYPASSRLS`

Postgres exempts a table's owner from its policies unless the table has `FORCE ROW LEVEL SECURITY`. An application that connects as the owner of its tables, the common single-role setup, would otherwise declare policies that never apply. `RowSecurity(force=True)`, the default, emits `FORCE` as well as `ENABLE`.

Superusers and roles with `BYPASSRLS` skip every policy, `FORCE` included. A connection that must see every row, such as the migrating role or an admin job, gets `BYPASSRLS` as a role property, which the database records and audits. Leaving a setting out never grants access; it shows no rows.

## Session settings

A **session setting** is a Postgres setting (a GUC) that a session sends with each operation it runs. You declare settings when you open the session, or later with `Session.set_config`.

### Round trips per operation

How the settings reach the database depends on whether the operation runs inside a `transaction()` block. Under the default `transaction` delivery:

- **Inside a `transaction()` block**, the settings follow its `BEGIN` as one `SELECT set_config(...)` statement: one extra round trip for the whole block. Savepoints inside it inherit them.
- **Outside one**, each operation wraps itself in a transaction to carry them: `BEGIN`, the `set_config` statement, the operation's own statements, then `COMMIT`. That is three extra round trips per operation. An operation is one call into Ferro (`all()`, `create()`, `save()`, a raw query), so a chunked `bulk_create` is one operation that pays once.
- **A session without settings** sends nothing extra.

A flow of several operations is cheaper inside one `transaction()` block, which pays once. `ferro.raw.execute(..., autocommit=True)` opts one statement out of the wrap, and out of the scope with it, for the few statements Postgres refuses to run inside a transaction.

### Visibility of a new value

`set_config` changes the session's settings and returns once the change is recorded:

- An operation or `transaction()` block that **starts after** `set_config` returns runs with the new value, whichever task starts it.
- A `transaction()` block of this session that is **already open**, in any task sharing the session, gets the new value applied to its own connection before `set_config` returns. Its next statement sees it.
- An operation **already running** outside a `transaction()` block (a chunked `bulk_create` in another task, say) finishes with the value it started with.

`set_config` also empties the session's identity map, so an instance loaded under the old value is never handed back under the new one. Setting a key to the value it already holds changes nothing and sends nothing.

If delivering the new value to an already-open `transaction()` block fails, `set_config` raises `RuntimeError`, but the session's settings and identity map have already changed. The failed statement leaves that transaction aborted, so it cannot go on to read under the old value; roll it back and carry on with the new one.

### Nested sessions

A session opened inside another starts from the outer session's settings and overrides them key by key. The merge is a snapshot taken when the inner session opens, and nothing flows back to the outer one:

```python
--8<-- "docs/examples/row_level_security.py:nested"
```

The inner session kept the outer `app.tenant_id` and replaced `app.user`; the outer session's settings are unchanged.

Settings follow the session, not the connection. A session nested on another named connection inherits the outer settings there too, and that is the way to reach a second Postgres connection with the same scope. `transaction(using=...)` cannot do it alone: inside a session, `using=` that names a connection other than the session's own raises `ValueError`.

Settings a session declares itself must be honoured, so opening a session with `settings=` on a non-Postgres connection raises `RuntimeError`. Settings a session only inherits are different: a nested session on SQLite opens and runs without them, since SQLite has no row security to scope.

## The pooler constraint

A setting made with plain `SET` stays on the server connection. Behind a pooler in transaction mode (PgBouncer's default, and the front door of most managed Postgres services), that server connection passes to another client between transactions, so two requests can share it seconds apart:

<!-- shows: a sketch of two tenants sharing one pooled server connection -->
```text
tenant A → pooler → server connection 7: SET app.tenant_id = 'A'
tenant B → pooler → server connection 7: SELECT … FROM invoice
                                         -- runs with tenant A's scope
```

Plain `SET` behind a transaction-mode pooler leaks one tenant's scope to the next request. A client cannot see the pooler's mode (`SHOW pool_mode` needs the pooler's admin database), so Ferro cannot detect the case and refuse. Instead its default, `transaction` delivery, uses `set_config(key, value, true)`: the `true` is Postgres's `is_local` flag, so the value ends at `COMMIT` and never reaches the connection's next client. That is safe on a direct connection, PgBouncer in transaction mode, RDS Proxy and the Supabase pooler alike.

## Connection delivery

`PoolConfig(settings_delivery="connection")` trades that safety for fewer round trips. A settings-bearing session takes one pool connection at its first operation and keeps it until it closes. It sends `set_config(key, value, false)` once, so the value lasts for the whole database session, and then sends its statements with no wrap. Use it only when the pool connects to Postgres directly, with no transaction-mode pooler in between: behind one, it is the leak described above.

What changes under connection delivery:

- **Cost.** One `set_config` round trip per session, instead of three extra round trips per operation outside a `transaction()` block.
- **Concurrency.** Each settings-bearing session holds a connection from its first operation to its close, so no more of them run at once than the pool has connections. Size `max_connections` for the peak number of concurrent scoped sessions.
- **One operation at a time.** Sibling tasks sharing one session run their operations one after another, and a sibling's operation waits for an open `transaction()` block instead of running inside it.
- **A check on release.** While any session on the pool holds a connection, every connection released to that pool runs one `SELECT current_setting(...)`, including releases from sessions without settings. While none does, the check costs no query.
- **`autocommit=True` stays scoped.** There is no wrap to opt out of, since the setting lives on the connection.
- **A late first setting.** A session that opens without settings and gets its first one from `set_config` holds no connection yet, so it takes one at its next operation and sends the values then.
- **`set_config` reaches running work.** The new value lands on the held connection at once, so a sibling task's operation that is already running sees it from its next statement. Under `transaction` delivery that operation would finish with its old value.
- **Closing resets.** A closing session resets exactly the keys it set, to the value the connection started with. For a custom setting that is the empty string, which the `NULLIF` handles. A value configured with `ALTER ROLE ... SET` or `ALTER DATABASE ... SET` comes back instead, so never give a tenancy key a role or database default. Nothing else is reset, so the `search_path` the pool sets on every connection stays.
- **Migrations wait.** `connect(migrate_updates=...)` and `migrate()` refresh the pool afterwards, and the refresh waits for every held connection to come back. Ferro warns with the number of connections it is waiting on. Run schema changes before opening tenant sessions.

Sessions without settings never hold a connection, so they behave as they do under `transaction` delivery.

## Drift and ownership

Ferro manages the policies it names `rls_<table>_<name>` and leaves every other policy alone.

When `migrate_updates` finds a shorthand policy whose live body differs from the declaration, it drops and recreates it. Ferro rendered that body itself, so it knows what the catalog should hold, and the rebuild reads no rows.

A policy written in SQL is different, because Postgres rewrites SQL when it stores it (`x BETWEEN a AND b` comes back as `(x >= a) AND (x <= b)`). A textual difference does not prove a real change, and rebuilding on every rewrite would lock the table on every connect. Ferro reports the declared and the stored text and leaves the policy in place. To clear the report, express the policy as a shorthand, or drop the policy so the next `migrate_updates` recreates it. A migration compares two declarations, never the live catalog, so `ferro migrate new` rebuilds a changed SQL policy as written.

The table flags only switch on. `migrate_updates` turns `ENABLE` and `FORCE` on, never off. A model that drops its `__ferro_rls__` or its `force=` keeps the live flags and gets a warning on every connect until the declaration returns or `migrate_destructive` removes them. An orphaned `rls_*` policy, declared once and no longer, warns under `migrate_updates` and is dropped under `migrate_destructive`.

All of that applies only to a table with at least one live `rls_*` policy. A table where a database administrator turned row security on by hand, with only their own policies, gets no warning and no change, even from a destructive run.

Alembic autogenerate writes what `migrate_destructive=True` would do, with no destructive gate, since a revision is reviewed before it runs. It does not carry the two cases the runtime pass only warns about: a drifted policy written in SQL, and a policy Ferro does not own. Watch the `migrate_updates` warnings for those.

## See also

- [Scope rows by tenant with Postgres row-level security](../guide/row-level-security.md): the setup, step by step.
- [Row-level security reference](../reference/row-level-security.md): `RowSecurity`, `RowPolicy`, session settings and `PoolConfig(settings_delivery=...)`.
