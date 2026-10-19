---
description: "API reference for RowSecurity, RowPolicy and the session settings their policies read."
status: new
---

# Row-level security

This page lists Ferro's row-level security API: `RowSecurity` and `RowPolicy`, which declare a table's policies, and the session settings the policies read.

A model declares its policies on `__ferro_rls__`, and a session's `settings=` supplies the value they compare against. With the `Invoice` model below, a session scoped to one tenant reads that tenant's invoices:

```python
--8<-- "docs/examples/row_level_security.py:scoped"
```

Postgres returns Acme's two invoices; the policy filtered out Globex's. The examples on this page run against Postgres in the docs test suite. [Scope rows by tenant](../guide/row-level-security.md) walks through the setup, and [Row-level security and session settings](../concepts/row-level-security.md) explains how it works.

## `RowSecurity`

`RowSecurity` declares a table's row security, its policies and its table flags:

```python {test=compile reason="a signature"}
class RowSecurity:
    def __init__(self, *policies: RowPolicy, force: bool = True) -> None: ...
```

Assign it to the model's `__ferro_rls__` class variable, annotated `ClassVar`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/row_level_security.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/row_level_security_annotated.py:models"
    ```

`Invoice` gets `ENABLE` and `FORCE ROW LEVEL SECURITY` and one policy, `rls_invoice_tenant_id`.

| Parameter | Default | Meaning |
| :--- | :--- | :--- |
| `*policies` | none | The table's `RowPolicy` values, in any number. Permissive policies combine with OR, restrictive ones with AND (see [Composing policies](#composing-policies)). |
| `force` | `True` | Also emit `FORCE ROW LEVEL SECURITY`, so the table's owner is filtered too. With `False`, a role that owns the table skips every policy. |

Raises `TypeError` when a positional argument is not a `RowPolicy` or `force` is not a `bool`, and, when the model is defined, when two policies share a name.

## `RowPolicy`

`RowPolicy` declares one policy, the rows a command may see or write:

```python {test=compile reason="a signature"}
@dataclass(frozen=True)
class RowPolicy:
    name: str | None = None
    command: str = "all"
    restrictive: bool = False
    column: str | None = None
    setting: str | None = None
    using: str | None = None
    with_check: str | None = None
```

It takes one of two forms, the shorthand (`column=` and `setting=`) or SQL (`using=` and `with_check=`); passing both, or neither, raises `TypeError`.

| Parameter | Default | Meaning |
| :--- | :--- | :--- |
| `name` | the column, in the shorthand | The policy's name suffix; the live policy is `rls_<table>_<name>`. Required in the SQL form. |
| `command` | `"all"` | The command the policy covers: `"all"`, `"select"`, `"insert"`, `"update"` or `"delete"` (see [Commands](#commands)). |
| `restrictive` | `False` | `True` makes the policy `AS RESTRICTIVE`: it combines with every other policy by AND instead of OR. |
| `column` | `None` | Shorthand: the model column holding the scope value. |
| `setting` | `None` | Shorthand: the session setting to compare it with, a dotted name such as `"app.tenant_id"`. |
| `using` | `None` | SQL form: the `USING` expression, which decides the rows a command sees. |
| `with_check` | `None` | SQL form: the `WITH CHECK` expression, which decides the rows a write may leave. |

### The shorthand

`RowPolicy(column="tenant_id", setting="app.tenant_id")` renders one comparison and uses it for both `USING` and `WITH CHECK`:

```sql
"tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid
```

The cast comes from the column's storage type on Postgres:

| Column storage | Cast |
| :--- | :--- |
| `uuid` | `::uuid` |
| `text`, `varchar` | none |
| `smallint`, `integer`, `bigint` | `::smallint`, `::integer`, `::bigint` |
| a native enum, or any other type (`timestamptz`, `jsonb`, ...) | not supported: defining the model raises `TypeError`, naming the SQL form as the alternative |

`setting` must be dotted identifiers, matching `[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+`, or the constructor raises `ValueError`. A `column` the model does not have raises `TypeError` when the model is defined, listing the valid columns. [Policy expressions](../concepts/row-level-security.md#policy-expressions) explains the `NULLIF`.

### The SQL form

Pass `using=`, `with_check=` or both, with `name=`, when one column compared with one setting is not enough: a function call, a subquery, boolean logic. Each expression must be a non-empty string. The constructor checks it against the clauses Postgres accepts for the command, and raises `TypeError`:

- when `name=` is missing;
- for `using=` on `"insert"`, or `with_check=` on `"select"` or `"delete"` (see [Commands](#commands));
- when an `"insert"` policy has no `with_check=`, or any other command has no `using=`.

Write the same `NULLIF(current_setting(key, true), '')` wrapper around a setting you read in SQL, so an unset setting matches no rows instead of raising.

!!! note "A policy that reads another table"
    Postgres resolves the tables a policy's SQL reads when it creates the policy, so they must exist by then. Ferro orders the tables it creates by their foreign keys, not by what a policy reads: a policy on `document` that reads `membership`, created in the same pass, fails with `relation "membership" does not exist`. Create the table the policy reads in an earlier migration, or declare the policy once that table exists.

### Policy names

The live name is `rls_<table>_<name>`: `RowPolicy(name="tenant", ...)` on `Document` is `rls_document_tenant`. The `name` must match `[a-z][a-z0-9_]*` and must not start with `rls_`, or the constructor raises `TypeError`.

Names are unique per model. Postgres limits identifiers to 63 bytes, so Ferro truncates a long name; two names that are equal after truncation raise `TypeError` when the model is defined, as a repeated name does.

Ferro manages only the policies whose names start with `rls_`. It reports any other policy on a table and never changes it.

### Commands

`command=` scopes a policy to one command, and each command takes the clauses Postgres accepts for it:

| `command` | `using=` | `with_check=` |
| :--- | :--- | :--- |
| `"all"` | required | accepted |
| `"select"` | required | refused |
| `"insert"` | refused | required |
| `"update"` | required | accepted |
| `"delete"` | required | refused |

The table holds for the SQL form. The shorthand fills in every clause its command accepts.

## Composing policies

Postgres combines a table's policies the same way Ferro declares them: permissive policies (the default) with OR, and restrictive ones (`restrictive=True`) with AND against the rest. A restrictive tenant fence plus permissive grants gives "within the tenant, the owner has full access and everyone reads published documents":

=== "Assignment"

    ```python
    --8<-- "docs/examples/row_level_security.py:composed"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/row_level_security_annotated.py:composed"
    ```

`tenant` is restrictive, so the two permissive policies apply only inside the tenant. The three policies render as:

```sql
CREATE POLICY "rls_document_tenant" ON "document" AS RESTRICTIVE FOR ALL USING ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY "rls_document_owner_all" ON "document" FOR ALL USING ("owner" = NULLIF(current_setting('app.user', true), '')) WITH CHECK ("owner" = NULLIF(current_setting('app.user', true), ''));

CREATE POLICY "rls_document_published_read" ON "document" FOR SELECT USING ("published");
```

With four documents (Ada's "Q3 plan", Grace's published "Handbook" and her unpublished draft in Acme, and Ada's published "Globex roadmap" in Globex), Ada in Acme reads her own document and the published one:

```python
--8<-- "docs/examples/row_level_security.py:composed_query"
```

Grace's draft fails both permissive policies, and the Globex roadmap passes `published_read` but fails the restrictive tenant fence.

## Session settings

The session settings API lives on `engines` and `Session`; the [API reference](../api/connection.md) has the full signatures.

| Call | What it does |
| :--- | :--- |
| `engines.session(name=None, *, settings=None)` | Opens a session whose operations carry `settings`, a mapping of setting name to value. |
| `Session.set_config(key, value)` | Sets one setting on an open session (see [Visibility of a new value](../concepts/row-level-security.md#visibility-of-a-new-value)). |
| `Session.effective_settings` | The settings the session runs with, inherited ones included. |
| `ferro.current_session()` | The session open in the current task, or `None`. |
| `PoolConfig(settings_delivery="transaction")` | How a connection delivers settings: `"transaction"` (the default) or `"connection"` (see [Connection delivery](../concepts/row-level-security.md#connection-delivery)). |

Both `settings=` and `set_config` check their arguments before touching a connection:

- a key or value that is not a `str` raises `TypeError` (convert a UUID with `str()`);
- a key with no dot raises `ValueError`: built-in settings such as `timezone` or `role` cannot be set this way;
- `settings=` on a non-Postgres connection, or `set_config` on a session whose connection is not Postgres, raises `RuntimeError`; `set_config` on a session that is not open raises `RuntimeError` too.

Keys and values travel as bound parameters, never as SQL text.

## SQLite

SQLite has no row-level security. A model with `__ferro_rls__` still registers, and its table is created without policies, with one warning per table:

```python
--8<-- "docs/examples/row_level_security.py:sqlite"
```

Rows on SQLite are not filtered. Declaring settings on a SQLite session raises, since nothing could honour them:

```python
--8<-- "docs/examples/row_level_security.py:sqlite_settings"
```

A session that only inherits settings from an outer Postgres session opens on SQLite without them.

## Warnings

`connect(migrate_updates=True)` (or `migrate_destructive=True`) warns when the connecting role is neither a superuser nor `BYPASSRLS` and an existing table it migrates declares `RowSecurity(force=True)`:

```text
ferro auto-migrate: The connected role is neither a superuser nor BYPASSRLS, and this migration touches table(s) 'invoice' with FORCE ROW LEVEL SECURITY. Row policies apply to the migrating role too, so a backfill or data step can silently see and update zero rows. Migrate as a role with BYPASSRLS if this pass moves data.
```

It does not check whether the table's live `FORCE` flag was already set. A table created by the same connect has no rows to miss, so `auto_migrate=True` and `create_tables()` never print it.

The reconciliation pass also warns about a drifted policy written in SQL, a policy Ferro does not own, a removed declaration whose flags are still on, and an orphaned `rls_*` policy; [Drift and ownership](../concepts/row-level-security.md#drift-and-ownership) explains each.
