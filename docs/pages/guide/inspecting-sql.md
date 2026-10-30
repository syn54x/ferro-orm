---
description: "See every statement Ferro sends with the ferro.sql log, and render a query's SQL without running it with .sql()."
---

# Inspecting SQL

Add `.sql()` to a query's terminal and Ferro returns the statement it would send, without sending it; turn on the `ferro.sql` logger and Ferro records every statement it does send.

Every example on this page uses one `Account` model. Its `token` is a `SecretStr`, so you can see what Ferro does with a value that must not reach a log:

=== "Assignment"

    ```python
    --8<-- "docs/examples/inspecting_sql.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/inspecting_sql_annotated.py:models"
    ```

Here is a whole program. It connects, opens a session and renders the query that finds Ada's account:

```python
--8<-- "docs/examples/inspecting_sql.py:first"
```

It prints:

<!-- prints: inspecting_sql.py:first -->
```text
SELECT "account"."id", "account"."owner", "account"."token" FROM "account" WHERE "account"."owner" = ?
('Ada',)
```

The first line is the SQL, with a placeholder where the value goes. The second is the value Ferro would bind to it. Nothing reached the database. Every example on this page runs inside a session like this one; see [Sessions](connections.md#sessions) for why and how to open one.

## Rendering a statement with `.sql()`

Every terminal of a query has a `.sql()` form that you await in its place: `all`, `first`, `count`, `exists`, `update` and `delete`. It returns a [`RenderedSql`][ferro.RenderedSql] with three fields, `sql`, `params` and `dialect`, and `print()` of one shows the SQL alone. Use it to check what a lambda predicate turned into, or to put a statement in a bug report.

`count` renders its aggregate, and `update` takes the same arguments it takes when it runs, either keyword values or a recipe lambda:

```python
--8<-- "docs/examples/inspecting_sql.py:count-and-update"
```

It prints:

<!-- prints: inspecting_sql.py:count-and-update -->
```text
SELECT COUNT(*) FROM "account" WHERE TRUE
UPDATE "account" SET "owner" = ? WHERE "account"."owner" = ?
('Ada Lovelace', 'Ada')
```

The `count` has no filter, so Ferro writes `WHERE TRUE`. The `update` lists the new value first and the filter's value second, which is the order of its placeholders. On PostgreSQL the placeholders are `$1` and `$2`, and `dialect` is `"postgres"`; nothing else in these statements changes.

The query renders for the database it would run on, so `.sql()` needs a connection and a session like the call itself. For the terminals that return model instances, see [Queries](queries.md).

## Turning on the statement log

The `ferro.sql` logger writes one `DEBUG` record for each statement Ferro sends, after the statement finishes. Ferro collects nothing while the logger is not enabled for `DEBUG`, so the switch costs nothing when it is off. To see the records in your terminal, give the logger a level and your application a handler, as [Logging](connections.md#logging) shows with `logging.basicConfig`.

The examples that follow need the records themselves, so this handler keeps them in a list:

```python
--8<-- "docs/examples/inspecting_sql.py:statement-log"
```

Every statement Ferro sends from here on lands in `records`. The program above had already called `connect()`, so the schema statements it ran are not in the list; put these lines before `connect()` and they are, as `migrate` records.

## A log record

Save an account and read the record the `INSERT` left:

```python
--8<-- "docs/examples/inspecting_sql.py:a-record"
```

A plain handler prints the record as one line, the statement, its parameters and how long it took. The page's example drops the timing, the last word of the line, because it differs on every run:

<!-- prints: inspecting_sql.py:a-record -->
```text
INSERT INTO "account" ("owner", "token") VALUES (?, ?) RETURNING "id" params=('Ada', ***)
```

A structured handler, such as a JSON formatter, reads the same record as attributes instead of parsing the line:

```python
--8<-- "docs/examples/inspecting_sql.py:attributes"
```

It prints:

<!-- prints: inspecting_sql.py:attributes -->
```text
sql: 'INSERT INTO "account" ("owner", "token") VALUES (?, ?) RETURNING "id"'
params: ('Ada', ***)
dialect: 'sqlite'
operation: 'save'
subject: None
connection: 'default'
rows: None
```

`operation` says which call sent the statement. `rows` is `None` here because the `INSERT` returns the new row's key instead of a count; it holds the rows affected for an `update()` or a `delete()`. Two more attributes, `duration_ms` and `error`, hold the time the database took and its error text when the statement failed. The [Logging](connections.md#logging) section lists every attribute and every `operation`.

## Records you did not ask for

Not every record comes from a line you wrote. Clear the list, run a transaction and print the `operation` of each record it left:

```python
--8<-- "docs/examples/inspecting_sql.py:operations"
```

It prints:

<!-- prints: inspecting_sql.py:operations -->
```text
['transaction', 'save', 'fetch', 'transaction']
```

A `transaction()` block logs its own `BEGIN` and `COMMIT` with the operation `"transaction"`, and a session with settings logs a `"settings"` record. On PostgreSQL, the first operation that touches a table also reads the table's columns from the catalog, and that read is a `catalog` record; this program's `.sql()` call touched `Account` before the log was on, so none shows here. To keep the log to the statements your code wrote, skip those operations in your handler or filter.

## Secret columns

A `SecretStr` or `SecretBytes` column never shows its value in a record or in a rendered statement. Ferro knows which bound parameters are secrets and puts `***` in their place, in the line and in `params` alike. The record above already showed it for the `INSERT`. Filtering on the token does the same:

```python
--8<-- "docs/examples/inspecting_sql.py:secrets"
```

It prints:

<!-- prints: inspecting_sql.py:secrets -->
```text
(***,)
SELECT "account"."id", "account"."owner", "account"."token" FROM "account" WHERE "account"."token" = ? params=(***,)
```

The first line is `.sql()`'s `params`, the second the record of the query that ran. The database received the real token; only what you see is masked. Each masked value is [`REDACTED`][ferro.REDACTED], an object whose `repr` and `str` are `***`.

## Asserting on records in a test

pytest's `caplog` fixture is a handler on the root logger, so it collects the `ferro.sql` records once you set the logger's level. Use it to pin how many statements a call sends, or what it binds:

```python {test=compile reason="uses pytest's caplog fixture, which exists only inside a pytest run"}
import logging

from pydantic import SecretStr

from myapp.models import Account


async def test_delete_sends_one_statement(db, caplog):
    await Account(owner="Grace", token=SecretStr("sk_live_grace")).save()

    with caplog.at_level(logging.DEBUG, logger="ferro.sql"):
        await Account.where(lambda account: account.owner == "Grace").delete()

    [record] = [record for record in caplog.records if record.operation == "delete"]
    assert record.params == ("Grace",)
```

The test finds the one `delete` record and checks the value it bound. The `db` fixture comes from [Testing](../howto/testing.md).

## See also

- [Queries](queries.md): the terminals `.sql()` renders
- [Raw SQL](raw-sql.md): statements you write yourself, which the log records as `"raw"`
- [Connections and databases](connections.md#logging): every record attribute and `operation`
- [Auto-migrate](schema/auto-migrate.md): the `PassReport`, the structured answer to what a schema pass ran
- [Query reference](../api/queries.md): `RenderedSql`, `REDACTED` and every terminal
