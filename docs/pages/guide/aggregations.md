# Aggregations and grouped queries

This guide shows how to total, count and average rows with the five aggregate methods (`count()`, `sum()`, `avg()`, `min()` and `max()`) inside a `select()` lambda, and how a record that mixes plain fields with aggregates becomes a grouped query. The full signatures are in the [aggregates reference](../api/queries.md#aggregates-and-grouped-queries).

Here is a whole program. It adds four transactions and asks for the sum of the amount, the largest amount and the average amount over all of them:

```python
--8<-- "docs/examples/aggregations.py:global"
```

The single record holds four numbers over the four rows: they total 115, so the average is 28.75. Every query on this page runs inside a session like this one; see [Sessions](connections.md#sessions-recommended) for why and how to open one.

## The ledger

Every example on this page queries one small ledger. An account holds transactions, and a transaction may have no account:

=== "Assignment"

    ```python
    --8<-- "docs/examples/aggregations.py:schema"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/aggregations_annotated.py:schema"
    ```

The ledger holds these four transactions:

| `id` | `memo` | `amount` | `price` | account |
| ---: | :--- | ---: | ---: | :--- |
| 1 | coffee | 10 | 1.25 | Checking |
| 2 | lunch | 20 | 2.75 | Checking |
| 3 | dinner | 45 | 5.00 | Savings |
| 4 | orphan | 40 | | |

Checking totals 30, Savings totals 45, and the orphan has no account and no price.

## Global aggregates

An aggregate is a method on a column reference, such as `transaction.amount.sum()`. Aggregate fields are named by you, so they live in the dict form of `select()`, where each key names an output field. A projection of only aggregates collapses the result to exactly one record, which you read with `first()`, as the first example did.

Aggregates measure whatever `where()` leaves in, relation traversal included:

```python
--8<-- "docs/examples/aggregations.py:global-where"
```

Only the two Checking transactions are measured, so the total is 30. The source column can traverse as well (`transaction.account.label.min()`): the traversal keeps rows where the relation exists, the way a `where()` on the same path does, and shares that path's join with any other clause that uses it.

### Result types

The result type comes from the source column's Python type, so a query returns the same Python types on SQLite and Postgres, even where the databases differ (Postgres widens `SUM(bigint)` to `numeric`; SQLite averages everything as a float):

| Aggregate | Source column | Result type |
| :--- | :--- | :--- |
| `count()` | any column | `int`, never `None` |
| `sum()` | `int` / `float` / `Decimal` | the source numeric type |
| `avg()` | `int` / `float` | `float` |
| `avg()` | `Decimal` | `Decimal`, never a lossy `float` |
| `min()` / `max()` | numeric, `str`, `datetime` / `date` / `time` | the source type |

For example, the average of the three prices stays a `Decimal`:

```python
--8<-- "docs/examples/aggregations.py:decimal-contract"
```

The prices total 9.00 over three rows, so the average is `Decimal("3")`. Every aggregate result except `count()` can be `None`, as the next section shows.

### Empty input

SQL answers an aggregate over zero rows with `NULL` (`COUNT` answers `0`), and Ferro passes that through. "Sum of no rows" and "sum of rows that total zero" are different facts, and zero would be the wrong answer for `min()` and `max()` anyway:

```python
--8<-- "docs/examples/aggregations.py:empty"
```

No transaction is over 10,000, so the query still returns one record: `n` is `0` and `total` is `None`.

### Counting a column

`transaction.price.count()` is SQL's `COUNT(price)`: it counts the rows where that column is not `NULL`. To count rows whatever else they hold, count the primary key (`transaction.id.count()`):

```python
--8<-- "docs/examples/aggregations.py:count-nulls"
```

All four transactions are counted by `id`, and three by `price`, because the orphan has no price.

## Grouped queries

Mix a plain field into an aggregate projection and the query is grouped. Every plain field is a group key, and each group collapses to one record:

```python
--8<-- "docs/examples/aggregations.py:grouped"
```

Account 1 totals 30 and account 2 totals 45, one record each. There is no `group_by()`: the record shape is the grouping. SQL already requires every selected bare column to be a group key, so a separate `group_by()` could only repeat the projection or contradict it. Postgres rejects the contradiction at run time, and SQLite answers with an arbitrary row's value. Deriving the grouping from the projection removes that whole class of error.

Group keys can traverse, and traversal narrows rows the way a predicate does:

```python
--8<-- "docs/examples/aggregations.py:grouped-traversed"
```

The orphan has no account, so it drops out and the two labelled groups remain.

### The `None` group for missing relations

Add `left_join()` and rows without a relation stay in, grouped under a `None` key instead of being dropped:

```python
--8<-- "docs/examples/aggregations.py:none-bucket"
```

The orphan's 40 now appears as its own group.

### Grouped queries over zero rows

A grouped query over zero matching rows returns zero records. Unlike a global aggregate, there is no group to report on:

```python
--8<-- "docs/examples/aggregations.py:zero-groups"
```

### Grouping and partitioning

Grouping collapses rows: each group becomes one record of keys and measures, and the individual rows are gone. If you want whole model instances bucketed by a key, every transaction arranged per account, do that in Python over a full query:

```python {test=run prelude=aggregations}
from collections import defaultdict

by_account: dict[int | None, list[Transaction]] = defaultdict(list)
for transaction in await Transaction.select().all():
    by_account[transaction.account_id].append(transaction)

assert {key: len(rows) for key, rows in by_account.items()} == {1: 2, 2: 1, None: 1}
```

Account 1 holds two transactions, account 2 holds one, and the orphan sits under `None`. Reach for aggregation when you want facts about groups, and partition in Python when you want the rows themselves, organized. This block runs inside a session, as [Sessions](connections.md#sessions-recommended) describes.

## Ordering grouped results

`order_by()` on a projected query follows SQL's own ORDER BY scoping. Three rules apply.

### Sorting by an output name

A string resolves output field names first, then root columns. `order_by("total")` sorts by the aggregate you named `total`, even if the model has a column of the same name. Combined with `limit()`, this is the top-N idiom: keys plus aggregates, ordered by the measure's output name, with `limit()` applied to the groups:

```python
--8<-- "docs/examples/aggregations.py:top-n"
```

Savings has the largest total, so it is the one group returned. `limit()` and `offset()` always act on groups in a grouped query, so pagination pages through the summary, not the underlying rows.

### Sorting by an aggregate expression

Where strings name outputs, a lambda writes the source expression itself, aggregates included:

```python
--8<-- "docs/examples/aggregations.py:order-by-lambda"
```

The groups come back with the largest total first. An aggregate sort key must match a projected aggregate (same function, same column, same path). An expression you did not project raises when you build the query and tells you to name it in the projection.

### Allowed sort keys

On an aggregate projection, every sort key must be a group key or an aggregate. Sorting groups by any other column is the arbitrary-row trap again, so Ferro rejects it when you build the query, whether the key is a string, a lambda, or was chained before the `select()`. On a plain, non-aggregate projection nothing changes: unselected root columns still sort.

## Unsupported aggregate forms

Each form below fails when you build the query, before any SQL is sent, with an error that names the fix. Each example runs inside a session and raises if the expected error does not appear.

`sum()` and `avg()` take numeric columns (`int`, `float`, `Decimal`), `min()` and `max()` take orderable ones (numeric, text, date and time), and `count()` takes anything. Enum, UUID, JSON and `bool` columns are rejected, because `min()` over a UUID does not exist on Postgres and `max()` over a native enum differs between backends (definition order against lexical order):

```python
--8<-- "docs/examples/aggregations.py:error-family"
```

```text
TypeError: t.memo.sum() is not supported: Transaction.memo is string-typed, and sum() takes a numeric column (int, float, or Decimal). No portable cross-backend meaning exists for this aggregate over a string column.
```

`where()` filters rows before aggregation, so it cannot hold an aggregate. The error points at `having()`, the post-aggregation filter, which does not exist yet. Until it does, filter rows with `where()` and compare the aggregated results in Python:

```python
--8<-- "docs/examples/aggregations.py:error-where"
```

```text
TypeError: t.amount.sum() > ... is not a where() predicate: WHERE filters rows before aggregation, so an aggregate cannot appear in it. Post-aggregation filtering is having() ...
```

Python's builtin `sum()` over a column reference raises instead of failing obscurely. The error text writes the lambda parameter as `t`, whatever you named it:

```python
--8<-- "docs/examples/aggregations.py:error-builtin-sum"
```

```text
TypeError: t.amount is a column reference and cannot be iterated; did you mean t.amount.sum()? ...
```

An aggregate outside the dict form has no output name, so it raises:

```python
--8<-- "docs/examples/aggregations.py:error-unnamed"
```

```text
TypeError: aggregate fields are user-named: give t.amount.sum() an output name with the dict selector form ...
```

Sorting groups by a column that is not a group key raises, as the ordering rules above say:

```python
--8<-- "docs/examples/aggregations.py:error-order-key"
```

```text
ValueError: order_by('memo') on an aggregate projection must name a group key or an aggregate: ...
```

`count()` and `exists()` on an aggregate projection are ambiguous between rows and groups, so both raise and name both spellings. Count matching rows with an unprojected query (`Transaction.where(...).count()`), and count groups with `len(await query.all())`:

```python
--8<-- "docs/examples/aggregations.py:error-count"
```

```text
ValueError: count() on an aggregate projection is ambiguous: count the matching rows with an unprojected query (Model.where(...).count()), or count the groups with len(await q.all()).
```

For the full list of aggregate forms and their result types, see [Aggregates and grouped queries](../api/queries.md#aggregates-and-grouped-queries) in the reference.

## See also

- [Selecting a column subset](queries.md#selecting-a-column-subset): projections, traversed fields and output aliases, which aggregations build on.
- [Querying across relationships](queries.md#querying-across-relationships): traversal and join semantics.
- [Typed query predicates](../concepts/query-typing.md#projected-queries): how aggregate expressions are typed.
