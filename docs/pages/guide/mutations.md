---
description: "Insert, update and delete rows with create(), save(), batch update() and delete(), and the insert-or-update helpers."
---

# Mutations

This guide shows how to insert, update and delete rows: `create()`, `save()`, batch `update()` and `delete()`, and the insert-or-update helpers.

Every call runs inside a session. Connect, open one, and create a customer:

```python
--8<-- "docs/examples/mutations.py:session"
```

`create()` returns the saved instance, with the primary key the database assigned and `plan` filled from its default. The blocks below run inside this session; see [Sessions](connections.md#sessions).

Every example on this page is followed by the SQL it sends, captured from Ferro when the example runs. `create()` sends one `INSERT`, which asks for the new `id` back with `RETURNING`. PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:session (sqlite)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?)
    RETURNING "id";
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:session (postgres)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz))
    RETURNING "id";
    ```

The examples use one model, a customer with a unique email:

=== "Assignment"

    ```python
    --8<-- "docs/examples/mutations.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/mutations_annotated.py:models"
    ```

Because `email` is unique, a second customer with the same email is refused.

Ferro has four ways to write a row, and each does only what its name says:

| Call | Intent | Behaviour |
| --- | --- | --- |
| `Customer.create(**fields)` | Insert a new row | Raises [`UniqueViolationError`](../api/exceptions.md#ferro.UniqueViolationError) on a duplicate primary key or unique value |
| `Customer.bulk_create(instances)` | Insert many new rows | Inserts the whole batch or none of it |
| `customer.save()` | Persist this instance | Inserts it if it was never saved, otherwise updates its row by primary key |
| `Customer.upsert(**fields)` | Insert or replace by primary key | Updates the existing row on a primary-key conflict |

## Creating rows

`create()` validates the fields, inserts the row and returns the saved instance. `bulk_create()` inserts a list of instances and returns how many it inserted:

```python
--8<-- "docs/examples/mutations.py:create"
```

The table now holds four customers. Fields you leave out take their defaults, so Carol's `plan` is `"free"`.

`create()`, `bulk_create()` and `count()` send these statements. The single-row insert asks for the new `id` with `RETURNING`, and PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:create (sqlite)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?)
    RETURNING "id";

    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?);

    SELECT COUNT(*)
    FROM "customer"
    WHERE TRUE;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:create (postgres)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz))
    RETURNING "id";

    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz)), ($6, $7, $8, $9, CAST($10 AS timestamptz));

    SELECT COUNT(*)
    FROM "customer"
    WHERE TRUE;
    ```

`bulk_create()` sends one multi-row `INSERT` with no `RETURNING`, which is why it writes no IDs back.

To create a row with a foreign key, see [Relationships](relationships.md). Pydantic validates each instance when it is built, so invalid data raises before Ferro sends anything to the database.

A batch can be any size. SQLite binds at most 32,766 parameters in one statement and PostgreSQL 65,535, so Ferro splits a large batch across as many `INSERT` statements as the database needs. The batch stays atomic: inside a [`transaction()`](transactions.md) block the transaction is the boundary, and outside one, a failing row (a unique violation late in the batch, say) leaves none of the batch inserted.

!!! note "`bulk_create()` skips the identity map"
    For speed, `bulk_create()` sends the instances straight to the database. It does not register them in the session's [identity map](../concepts/identity-map.md) and does not write generated IDs back onto them. Query the rows again when you need tracked instances:

    ```python
    --8<-- "docs/examples/mutations.py:bulk-identity"
    ```

    The instance passed to `bulk_create()` still has `id = None`; the one loaded back has the database's ID, `5`.

The batch and the lookup in that note send these statements. PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:bulk-identity (sqlite)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?);

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = ?
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:bulk-identity (postgres)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz));

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = $1
    LIMIT $2;
    ```

The batch's `INSERT` returns no `id` on either database.

`create()` always inserts. A duplicate primary key or unique value raises `UniqueViolationError` and leaves the existing row as it was:

```python
--8<-- "docs/examples/mutations.py:create-strict"
```

Alice's row still holds the name `"Alice"`. Use [`upsert()`](#upsert) when you want insert-or-update instead.

The refused insert and the lookup send these statements. Each `INSERT` asks for the new `id` with `RETURNING`, and PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:create-strict (sqlite)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?)
    RETURNING "id";

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = ?
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:create-strict (postgres)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz))
    RETURNING "id";

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = $1
    LIMIT $2;
    ```

The database refused the `INSERT`; `UniqueViolationError` carries its report.

## Saving: insert or update

An instance is either **transient** (built with `Customer(...)` and never saved) or **persisted** (loaded from the database, or saved). `save()` reads that state: it inserts a transient instance and updates a persisted one by primary key:

```python
--8<-- "docs/examples/mutations.py:save-insert-update"
```

The first `save()` inserted Frank and set his `id`; the second updated the same row, so there is still one Frank. A transient instance with a duplicate primary key or unique value raises `UniqueViolationError`, as `create()` does.

The two saves and the count send these statements. The insert asks for the new `id` with `RETURNING`, and PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:save-insert-update (sqlite)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?)
    RETURNING "id";

    UPDATE "customer"
    SET "email" = ?, "name" = ?, "plan" = ?, "views" = ?, "updated_at" = ?
    WHERE "id" = ?;

    SELECT COUNT(*)
    FROM "customer"
    WHERE "customer"."name" = ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:save-insert-update (postgres)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz))
    RETURNING "id";

    UPDATE "customer"
    SET "email" = $1, "name" = $2, "plan" = $3, "views" = $4, "updated_at" = CAST($5 AS timestamptz)
    WHERE "id" = $6;

    SELECT COUNT(*)
    FROM "customer"
    WHERE "customer"."name" = $1;
    ```

The second `save()` wrote every column, by primary key.

If the row behind a persisted instance is gone (another writer deleted it, or you changed the primary key before saving), the update matches nothing. `save()` then raises `ModelDoesNotExist` instead of inserting the row again:

```python
--8<-- "docs/examples/mutations.py:save-missing"
```

Frank stays deleted. `instance.delete()` makes an instance transient again, so a later `save()` on it inserts a new row.

The batch delete and the save send these statements. PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:save-missing (sqlite)
    DELETE
    FROM "customer"
    WHERE "customer"."id" = ?;

    UPDATE "customer"
    SET "email" = ?, "name" = ?, "plan" = ?, "views" = ?, "updated_at" = ?
    WHERE "id" = ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:save-missing (postgres)
    DELETE
    FROM "customer"
    WHERE "customer"."id" = $1;

    UPDATE "customer"
    SET "email" = $1, "name" = $2, "plan" = $3, "views" = $4, "updated_at" = CAST($5 AS timestamptz)
    WHERE "id" = $6;
    ```

The `UPDATE` matched no row, so `save()` raised instead of sending an `INSERT`.

!!! note "Copies share persistence state"
    `model_copy()` copies the persistence state too, so saving a copy of a persisted instance updates the same row. To clone a row, build a new instance.

### Partial updates with `only=` and `exclude=`

`save(only=...)` writes only the columns you name and leaves the rest of the row alone:

```python
--8<-- "docs/examples/mutations.py:save-only"
```

The row kept `plan = "pro"`, while the instance still held `"enterprise"` until `refresh()` reloaded it. Ferro runs no `SELECT` after the update, which is why the instance and the row can differ.

The fetch, the partial save and the refresh send these statements. PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:save-only (sqlite)
    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = ?
    LIMIT ?;

    UPDATE "customer"
    SET "name" = ?, "updated_at" = ?
    WHERE "id" = ?;

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = ?
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:save-only (postgres)
    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = $1
    LIMIT $2;

    UPDATE "customer"
    SET "name" = $1, "updated_at" = CAST($2 AS timestamptz)
    WHERE "id" = $3;

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = $1
    LIMIT $2;
    ```

The `UPDATE` sets only `name` and `updated_at`.

`save(exclude=...)` writes every column except the ones you name:

```python
--8<-- "docs/examples/mutations.py:save-exclude"
```

The name changed and the plan did not.

The save and the refresh send these statements. PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:save-exclude (sqlite)
    UPDATE "customer"
    SET "email" = ?, "name" = ?, "views" = ?, "updated_at" = ?
    WHERE "id" = ?;

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = ?
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:save-exclude (postgres)
    UPDATE "customer"
    SET "email" = $1, "name" = $2, "views" = $3, "updated_at" = CAST($4 AS timestamptz)
    WHERE "id" = $5;

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = $1
    LIMIT $2;
    ```

Every column but `plan` and the primary key is in the `SET`.

Two rules matter most. A partial save is an update, so it needs a persisted instance: on a transient one, `only=` and `exclude=` raise `ValueError`. And a call takes one write-set, not both. The [`Model.save`](../api/model.md#ferro.Model.save) reference lists the rest: which names count as columns, and which write-sets are empty and refused.

## Batch updates and deletes

`update(**values)` and `delete()` on a query change every matching row in one statement, without loading instances. Each returns the number of rows it changed:

```python
--8<-- "docs/examples/mutations.py:update-delete"
```

Three free customers moved to the `"trial"` plan, and Erin's row is gone.

The update and the delete send these statements; on PostgreSQL only the placeholders differ:

```sql
-- mutations.py:update-delete (sqlite)
UPDATE "customer"
SET "plan" = ?
WHERE "customer"."plan" = ?;

DELETE
FROM "customer"
WHERE "customer"."name" = ?;
```

Each is one statement over every matching row, with the query's filter as its `WHERE`.

!!! warning "Batch updates do not touch instances you hold"
    `where(...).update(...)` writes to the database only. Instances already in memory, including those in the identity map, keep their old values until you call `refresh()` on them.

### Expression updates

`update()` with keyword arguments sets literal values. To compute a value in the database, pass a lambda that returns a dict of column expressions:

```python
--8<-- "docs/examples/mutations.py:recipe-update"
```

The database added 1 to Alice's `views` and stamped `updated_at` with its own clock.

The update and the refresh send these statements; on PostgreSQL only the placeholders differ:

```sql
-- mutations.py:recipe-update (sqlite)
UPDATE "customer"
SET "views" = "views" + ?, "updated_at" = CURRENT_TIMESTAMP
WHERE "customer"."id" = ?;

SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
FROM "customer"
WHERE "customer"."id" = ?
LIMIT ?;
```

`customer.views + 1` became `"views" + ?`, and `now` became `CURRENT_TIMESTAMP`.

`+` and `-` follow SQL, so `NULL + 1` is `NULL`. `now` is a value you import (`from ferro import now`), not a function you call, and it can only be assigned to a `datetime` column.

On PostgreSQL, `.merge()` merges a dict into a JSON object column, with the patch winning where keys overlap. It replaces top-level keys only. A `NULL` column counts as `{}`. The examples use a conversation whose `turns` column holds a JSON object:

=== "Assignment"

    ```python
    --8<-- "docs/examples/mutations.py:conversation"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/mutations_annotated.py:conversation"
    ```

This block needs a PostgreSQL database. It creates a conversation and merges one turn into it:

```python
--8<-- "docs/examples/mutations.py:merge"
```

After the update, `turns` holds `{"r1": {"role": "user"}}`. The create, the update and the refresh send these statements:

```sql
-- mutations.py:merge (postgres)
INSERT INTO "conversation" ("turns")
VALUES (CAST($1 AS json))
RETURNING "id";

UPDATE "conversation"
SET "turns" = (COALESCE("turns"::jsonb, '{}'::jsonb)) || CAST($1 AS jsonb)
WHERE "conversation"."id" = $2;

SELECT "conversation"."id", "conversation"."turns"
FROM "conversation"
WHERE "conversation"."id" = $1
LIMIT $2;
```

The `||` operator merges the patch into the column read as `jsonb`, and `COALESCE` stands in `{}` for a `NULL` column. `.merge()` raises when the query is built if the connection is SQLite, the column holds a list, or the patch is a list.

### No `limit()` on batch writes

SQL has no portable `UPDATE ... LIMIT` or `DELETE ... LIMIT`. Rather than change every matching row, `update()` and `delete()` raise `ValueError` on a query with `limit()`, `offset()`, `after()` or `before()`:

```python
--8<-- "docs/examples/mutations.py:mutation-guard"
```

The error prints:

<!-- prints: mutations.py:mutation-guard -->
```text
ValueError: delete() does not support limit/offset/after/before: portable SQL has no DELETE ... LIMIT. Remove the .limit()/.offset()/.after()/.before() call, or fetch primary keys first and delete by primary-key set.
```

To change a bounded set of rows, fetch their primary keys first, then filter on them:

```python
--8<-- "docs/examples/mutations.py:mutation-by-key"
```

Only Alice, the oldest trial customer, was deleted.

The fetch and the delete send these statements; on PostgreSQL only the placeholders differ:

```sql
-- mutations.py:mutation-by-key (sqlite)
SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
FROM "customer"
WHERE "customer"."plan" = ?
ORDER BY "customer"."id" ASC NULLS LAST
LIMIT ?;

DELETE
FROM "customer"
WHERE "customer"."id" IN (?);
```

The `DELETE` filters on the fetched keys with `IN`.

## Deleting one row

`instance.delete()` deletes the row behind an instance:

```python
--8<-- "docs/examples/mutations.py:delete-one"
```

Carol's row is gone. Deleting a parent row applies the `on_delete` rule of every foreign key that points at it, `CASCADE` by default; read [Delete behavior](relationships.md#delete-behavior) before deleting rows that have children.

The lookup, the delete and the check send these statements; on PostgreSQL only the placeholders differ:

```sql
-- mutations.py:delete-one (sqlite)
SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
FROM "customer"
WHERE "customer"."email" = ?
LIMIT ?;

DELETE
FROM "customer"
WHERE "customer"."id" = ?;

SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
FROM "customer"
WHERE "customer"."id" = ?
LIMIT ?;
```

`carol.delete()` deletes one row by primary key.

## `upsert()`

`upsert(**fields)` inserts the row, or updates the existing row when the primary key already exists:

```python
--8<-- "docs/examples/mutations.py:upsert"
```

The second call updated row 100 in place. It wrote the whole row, so `name` went back to its default `""`: pass every field you want to keep.

The two upserts, the count and the lookup send these statements. PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:upsert (sqlite)
    INSERT INTO "customer" ("id", "email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT ("id") DO UPDATE
    SET "email" = "excluded"."email", "name" = "excluded"."name", "plan" = "excluded"."plan", "views" = "excluded"."views", "updated_at" = "excluded"."updated_at";

    INSERT INTO "customer" ("id", "email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT ("id") DO UPDATE
    SET "email" = "excluded"."email", "name" = "excluded"."name", "plan" = "excluded"."plan", "views" = "excluded"."views", "updated_at" = "excluded"."updated_at";

    SELECT COUNT(*)
    FROM "customer"
    WHERE "customer"."id" = ?;

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = ?
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:upsert (postgres)
    INSERT INTO "customer" ("id", "email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, $5, CAST($6 AS timestamptz))
    ON CONFLICT ("id") DO UPDATE
    SET "email" = "excluded"."email", "name" = "excluded"."name", "plan" = "excluded"."plan", "views" = "excluded"."views", "updated_at" = "excluded"."updated_at";

    INSERT INTO "customer" ("id", "email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, $5, CAST($6 AS timestamptz))
    ON CONFLICT ("id") DO UPDATE
    SET "email" = "excluded"."email", "name" = "excluded"."name", "plan" = "excluded"."plan", "views" = "excluded"."views", "updated_at" = "excluded"."updated_at";

    SELECT COUNT(*)
    FROM "customer"
    WHERE "customer"."id" = $1;

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."id" = $1
    LIMIT $2;
    ```

Each upsert is one `INSERT ... ON CONFLICT ("id") DO UPDATE` that sets every column from the row it tried to insert, which is why `name` was reset.

The conflict target is the primary key only, so a conflict on another unique column still raises `UniqueViolationError`. Without a primary key there is nothing to conflict on, and the call inserts a new row. `upsert()` is shorthand for `instance.save(on_conflict="update")`, which does the same for an instance you already hold.

## `get_or_create()` and `update_or_create()`

`get_or_create(defaults={...}, **filters)` looks up a row by exact-match filters and creates it if none matches. It returns `(instance, created)`, and applies `defaults` only when it creates:

```python
--8<-- "docs/examples/mutations.py:get-or-create"
```

The first call created Heidi; the second found the same row.

The two calls send these statements. The insert asks for the new `id` with `RETURNING`, and PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:get-or-create (sqlite)
    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = ?
    LIMIT ?;

    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?)
    RETURNING "id";

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = ?
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:get-or-create (postgres)
    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = $1
    LIMIT $2;

    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz))
    RETURNING "id";

    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = $1
    LIMIT $2;
    ```

The first call looked the row up, found nothing and inserted it; the second stopped after the lookup.

`update_or_create(defaults={...}, **filters)` takes the same arguments, but when a row matches it applies `defaults` to it and saves it:

```python
--8<-- "docs/examples/mutations.py:update-or-create"
```

Heidi's row now has the `"pro"` plan.

The call sends these statements. PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:update-or-create (sqlite)
    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = ?
    LIMIT ?;

    UPDATE "customer"
    SET "email" = ?, "name" = ?, "plan" = ?, "views" = ?, "updated_at" = ?
    WHERE "id" = ?;
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:update-or-create (postgres)
    SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
    FROM "customer"
    WHERE "customer"."email" = $1
    LIMIT $2;

    UPDATE "customer"
    SET "email" = $1, "name" = $2, "plan" = $3, "views" = $4, "updated_at" = CAST($5 AS timestamptz)
    WHERE "id" = $6;
    ```

It looked the row up, then saved it with an `UPDATE` by primary key.

!!! note "Not atomic"
    Both helpers read, then write. Two writers can both miss the row and both try to insert it; with a unique constraint on the filter columns, the second insert raises [`UniqueViolationError`](../api/exceptions.md#ferro.UniqueViolationError), so catch it and look the row up again. For an atomic insert-or-update on the primary key, use [`upsert()`](#upsert).

## `refresh()`

`refresh()` reloads an instance from its row and discards its in-memory values:

```python
--8<-- "docs/examples/mutations.py:refresh"
```

The batch update changed the row but not the instance; `refresh()` brought the new name in. It raises `RuntimeError` if the instance has no primary key or its row no longer exists.

The update and the refresh send these statements; on PostgreSQL only the placeholders differ:

```sql
-- mutations.py:refresh (sqlite)
UPDATE "customer"
SET "name" = ?
WHERE "customer"."id" = ?;

SELECT "customer"."email", "customer"."id", "customer"."name", "customer"."plan", "customer"."updated_at", "customer"."views"
FROM "customer"
WHERE "customer"."id" = ?
LIMIT ?;
```

`refresh()` is a `SELECT` by primary key.

## Database errors

Every database failure raises a typed exception under `FerroError`, so you never match on driver message text. Constraint violations are subclasses of `IntegrityError` and keep the driver's details as attributes:

```python
--8<-- "docs/examples/mutations.py:handling-errors"
```

On SQLite it prints:

<!-- prints: mutations.py:handling-errors -->
```text
2067
None
UNIQUE constraint failed: customer.email
```

`sqlstate` is the database's error code (`"23505"` on PostgreSQL, `"2067"` on SQLite), `constraint` names the violated constraint on PostgreSQL and is `None` on SQLite, and `driver_message` is the driver's own text, for logs. The [exceptions reference](../api/exceptions.md) lists the whole hierarchy.

The refused statement is this `INSERT`. It asks for the new `id` with `RETURNING`, and PostgreSQL also casts the `datetime` parameter to `timestamptz`:

=== "SQLite"

    ```sql
    -- mutations.py:handling-errors (sqlite)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES (?, ?, ?, ?, ?)
    RETURNING "id";
    ```

=== "PostgreSQL"

    ```sql
    -- mutations.py:handling-errors (postgres)
    INSERT INTO "customer" ("email", "name", "plan", "views", "updated_at")
    VALUES ($1, $2, $3, $4, CAST($5 AS timestamptz))
    RETURNING "id";
    ```

The database rejected it, and the exception carries the database's own report.

## See also

- [Transactions](transactions.md): group several writes so they commit or roll back together
- [Queries](queries.md): find the rows to change
- [Identity map](../concepts/identity-map.md): how a session tracks instances
- [`Model` reference](../api/model.md): every write method and its options
