# Transactions

Group multiple operations into a single atomic unit with the `ferro.transaction()` async context manager: commit on clean exit, rollback if the block raises.

## Basic Usage

```python
--8<-- "docs/examples/transactions.py:basic"
```

Every ORM operation inside the block — `create`, `save`, `delete`, batch `update`, `bulk_create`, queries — runs on the **same database connection** (connection affinity is tracked through `contextvars`, so it is safe under concurrent asyncio tasks: each task gets its own transaction).

## Rollback on Error

If any exception escapes the block, everything performed inside it is rolled back and the exception re-raises:

```python
--8<-- "docs/examples/transactions.py:rollback"
```

## Using Named Connections

A transaction is pinned to one connection. Open it against a [named connection](connections.md#named-connections) with `using=`, and everything inside inherits that connection — unqualified operations included. Trying to route to a *different* connection inside the block raises:

```python
--8<-- "docs/examples/multiple_databases.py:transaction"
```

## Raw SQL Inside a Transaction

`transaction()` yields a `Transaction` handle exposing `execute` / `fetch_all` / `fetch_one` for raw SQL on the transaction's own connection — useful for Postgres GUCs (`set_config`, `SET LOCAL`), advisory locks, or any one-off statement that doesn't fit a model:

```python
--8<-- "docs/examples/transactions.py:handle"
```

The handle becomes invalid when the block exits — calling it afterwards raises `RuntimeError`. If you don't need it, simply write `async with transaction():` and the handle is discarded. The top-level `ferro.execute` / `fetch_all` / `fetch_one` functions also automatically join the active transaction — see [Raw SQL](raw-sql.md#raw-sql-in-transactions).

## Nesting Behavior

Nested `transaction()` blocks map to **savepoints** on the outer transaction's connection:

- If an **inner** block raises, only its work is rolled back (to the savepoint); the outer transaction can continue and commit.
- If the **outer** block raises, everything rolls back — including inner blocks that completed "successfully".

```python
from ferro import transaction


async def import_rows(rows: list[dict]) -> int:
    imported = 0
    async with transaction():
        await AuditLog.create(event="import-started")
        for row in rows:
            try:
                async with transaction():  # savepoint per row
                    await Record.create(**row)
                    imported += 1
            except ValueError:
                continue  # this row rolled back; the import continues
    return imported
```

A nested block never commits independently of its parent — only the outermost block's clean exit commits to the database.

## Patterns

### Keep transactions short

A transaction holds a pooled connection (and database locks) for its entire duration. Do slow work — HTTP calls, file I/O, expensive computation — *outside* the block, and keep only the database writes inside:

```python
async def publish(post_id: int) -> None:
    rendered = await render_markdown(post_id)  # slow work first, no tx held

    async with transaction():
        post = await Post.get(post_id)
        post.body_html = rendered
        post.published = True
        await post.save()
```

### Error handling

Catch exceptions *outside* the block when the whole unit should roll back, and *inside* it only for work you genuinely want to keep partial (paired with a nested block, as above). Catching an exception inside the block and continuing means the surviving operations **will commit**:

```python
async def transfer(src: Account, dst: Account, amount: int) -> bool:
    try:
        async with transaction():
            src.balance -= amount
            await src.save()
            if src.balance < 0:
                raise ValueError("insufficient funds")
            dst.balance += amount
            await dst.save()
    except ValueError:
        return False  # nothing was committed
    return True
```

## See Also

- [Raw SQL](raw-sql.md) — placeholders, bind types, and the `Transaction` handle
- [Connections & Databases](connections.md) — named connections and pools
- [Mutations](mutations.md) — the operations you'll wrap in transactions
- [Multiple Databases](../howto/multiple-databases.md) — routing patterns
