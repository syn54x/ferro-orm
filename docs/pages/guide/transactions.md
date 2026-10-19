# Transactions

`transaction()` groups database calls into one unit: the block commits when it exits cleanly and rolls back if it raises.

A transaction runs inside a session. Open one, create two accounts, and move 25 from checking to savings in one unit:

```python
--8<-- "docs/examples/transactions.py:basic"
```

Both rows changed together; if either `save()` had failed, neither would have. Outside a session, `async with transaction():` raises `RuntimeError: No database route`. The blocks below run inside this session; see [Sessions](connections.md#sessions).

The examples use one model, a bank account with a unique account number:

=== "Assignment"

    ```python
    --8<-- "docs/examples/transactions.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/transactions_annotated.py:models"
    ```

Every call inside the block, from `create()` and `save()` to batch `update()`, `bulk_create()` and queries, runs on the same database connection. Ferro tracks that connection per asyncio task, so concurrent tasks each get their own transaction.

## Rollback

If an exception leaves the block, Ferro rolls back everything the block did and raises the exception again:

```python
--8<-- "docs/examples/transactions.py:rollback"
```

The row still holds 75, but the `checking` instance held `-925` until `refresh()` reloaded it.

!!! warning "Rollback does not undo changes to Python objects"
    A rollback restores the database, not the instances you changed in the block. Saving such an instance later writes the rolled-back value back to the row. Call `refresh()` on each instance you changed before you use it again. Ferro also clears the session's [identity map](../concepts/identity-map.md) on rollback, so a later load returns a new instance rather than the stale one.

## Catching errors inside a transaction

Catch an exception outside the block when the whole unit should roll back. A `transfer()` function that refuses to overdraw an account:

```python
--8<-- "docs/examples/transactions.py:transfer"
```

The refused transfer left both balances where they were, in the database and in memory, because the `except` branch refreshed `src`. The transfer of 50 went through.

If you catch an exception inside the block and carry on, the work that did not raise commits when the block exits. Keep that to work you want to keep in part, inside a nested block, as the next section shows.

## Nested transactions

A `transaction()` inside another one is a savepoint on the outer transaction's connection. If the inner block raises, only its work rolls back, and the outer block carries on. Open three accounts, one of which reuses a taken number:

```python
--8<-- "docs/examples/transactions.py:nested"
```

`CHK-001` raised [`IntegrityError`](../api/exceptions.md#ferro.IntegrityError) and rolled back to its savepoint; the other two accounts were committed. If the outer block raises, everything rolls back, including inner blocks that finished. A nested block never commits on its own: only a clean exit from the outermost block commits.

## Raw SQL in a transaction

`transaction()` yields a handle with `execute`, `fetch_all` and `fetch_one` for raw SQL on the transaction's own connection. Use it for Postgres settings (`set_config`, `SET LOCAL`), advisory locks, or a statement that does not fit a model:

```python
--8<-- "docs/examples/transactions.py:handle"
```

The count ran inside the transaction, so it saw Carol's account before the commit. The handle stops working when the block exits; a later call on it raises `RuntimeError`. The top-level `ferro.execute`, `fetch_all` and `fetch_one` also join the active transaction; see [Raw SQL](raw-sql.md#raw-sql-in-transactions).

## SQLite write locks: `immediate=True`

A SQLite transaction that reads and then writes can fail with `SQLITE_BUSY` when it tries to upgrade its lock while another connection writes. `transaction(immediate=True)` opens it with `BEGIN IMMEDIATE`, which takes the write lock at the start:

```python
--8<-- "docs/examples/transactions.py:immediate"
```

The read and the write ran under one write lock, and savings now holds 85. Another writer waits on SQLite's busy timeout instead of failing. On PostgreSQL `immediate=True` is a plain `BEGIN`, and a nested transaction ignores it.

## Short transactions

A transaction holds a pooled connection, and the database's locks, until it ends. Do slow work such as HTTP calls, file I/O or heavy computation before the block, and keep only the writes inside it:

```python
--8<-- "docs/examples/transactions.py:short"
```

`quote_interest()` stands for any slow call; the transaction lasted only as long as the `save()`.

## Transactions in a session

`transaction()` uses the session you are in. To pin it to a session handle you hold instead, pass `transaction(session=handle)`. `Session.close()` raises `RuntimeError` while a transaction in that session is still open, so leave every `transaction()` block before you close it.

## Transactions on a named connection

`transaction(using="analytics")` pins a transaction to a [named connection](connections.md#named-connections). Every call inside the block runs on that connection, unqualified calls included, and routing a call to another connection raises `ValueError`. One transaction spans one connection, so writes to two databases never commit together. [How to use several databases](../howto/multiple-databases.md#transactions-on-a-named-connection) shows it with two connections.

The [`transaction` reference](../api/transactions.md#ferro.transaction) lists every argument.

## See also

- [Mutations](mutations.md): the writes you group in a transaction
- [Raw SQL](raw-sql.md): placeholders, bind types and the `Transaction` handle
- [Connections](connections.md): sessions, pools and named connections
