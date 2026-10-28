# A generated column's value comes only from the database

```python
class Transaction(Model):
    amount_minor: int
    direction: int = Field(generated=lambda transaction: fn.sign(transaction.amount_minor))

transaction = Transaction(amount_minor=-5)
transaction.direction        # None: the row does not exist yet
await transaction.save()     # INSERT ... RETURNING "direction"
transaction.direction        # -1
transaction.amount_minor = 7
transaction.direction        # None again: an input changed, the stored -1 is stale
```

A generated column is computed by the database on every write, and the database is its only evaluator. The instance holds the value the database returned, or `None` while that value is unknown: before the row is first saved, and after any column the expression reads is assigned. `save()` reads the value back with `RETURNING` on both dialects; INSERT and UPDATE never list the column; `bulk_create` leaves it `None`, as it leaves an auto primary key. Constructing, assigning or `update()`-ing a generated field is refused, naming the fix. The annotation states the column's type (`int` is `NOT NULL`), so the checker reads `int` for a value that is `None` in the unsaved window, the gap SQLAlchemy's `Mapped[int]` accepts for every server-computed column.

The reason is that the attribute and the query can never disagree. Ferro's migrations run code and schema out of step on purpose (ADR-0038, expand and contract), and in that window an instance computed in Python would show the new expression's value on a row that `where()` matched with the old one. The same mechanism carries every value the database writes: an autoincrement key today, trigger-maintained columns later.

Decision by owner (2026-10-28), grilling #789.

Rejected alternatives:

- **A Python twin of every function, so the value exists before save** (SQLAlchemy `hybrid_property`'s shape): a stale-free preview, but a second evaluator that must match two databases that already disagree with each other (`least(NULL, 28)`, a float into an integer column, `bigint` overflow, `numeric` division scale). It shows a wrong value during a deploy window, cannot extend to trigger-set columns or views, and cannot be withdrawn later without breaking users; adding it later as a preview is additive.
- **Pydantic `computed_field` as the declaration**: a property ignores the value Ferro's hydration writes, is absent from `model_fields`, silently drops constructor input, and its body is arbitrary Python that cannot be lowered to SQL.
- **An honest `int | None` annotation**: the annotation decides nullability, so a `NOT NULL` column would need `Field(nullable=False)`, and every read after a fetch would need narrowing.
- **An attribute that raises before save**: `model_dump()` and `repr()` would raise on an unsaved instance.
- **Keep the stale value after an input is assigned** (Django, SQLAlchemy): the instance would show a value the row will not hold.
