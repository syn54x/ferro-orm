# Value expressions are lambdas over a closed function table, admitted only under value parity

```python
day_class: int = Field(generated=lambda transaction: fn.least(fn.day(transaction.date), 28))
Check("large", lambda payment: fn.abs(payment.amount) > 1000)
```

A value expression, the body of a generated column and either side of a check's comparison, is a lambda over the model's own columns built from columns, literals, `+ - * /`, unary `-` and the functions in `fn`. `fn` is a closed table decided in Rust: each function's name, arity, argument and result types, and its rendering per dialect (`least` is `min` on SQLite, `extract(day from ...)` is `strftime`). An expression is admitted only under **value parity**: SQLite and PostgreSQL store the same value for it on every row. Ferro checks that when the model is defined, from the field types: the result type must match the column, `least` and `greatest` take non-nullable operands (or a `coalesce`), and a function the two databases disagree on stays out of the table. One test runs the whole table, edge values and `NULL` included, on both dialects. v1 holds `sign`, `abs`, `least`, `greatest`, `coalesce`, `length`, `year`, `month` and `day`; `year`, `month` and `day` take a `date`, since Postgres refuses a session-time-zone-dependent expression in a generated column. `fn` in `where`, `select`, `order_by` and `update()` arrives with views (#789 epic 2); until then it is refused there, naming a generated column as the route.

This is ADR-0012 and ADR-0016's path, "SQL functions join by expanding the predicate dialect", taken: one body language for every schema body, never a second one in SQL text. The table is closed because every function needs a rendering per dialect and a parity pin; the parity rule exists because a model that stores a different value on each database breaks the promise that the backend is a deployment choice.

Decision by owner (2026-10-28), grilling #789.

Rejected alternatives:

- **SQL strings** (SQLAlchemy `Computed`, Rails, Drizzle): rejected by ADR-0012.
- **Methods on the column** (`transaction.date.day().least(28)`): multi-argument functions read lopsided, and every function would crowd `where()` autocomplete.
- **An open `func` namespace** (SQLAlchemy): no per-dialect rendering, no parity, no known result type.
- **Document the divergence** (`least(NULL, 28)` is `28` on Postgres and `NULL` on SQLite): the same model would store different values per database. Rendering SQLite so it agrees stays open, function by function.
- **Generated columns only, checks later**: `fn` would work on one line of a model and fail on the next.
