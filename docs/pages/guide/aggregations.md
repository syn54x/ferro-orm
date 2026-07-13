# Aggregations & Grouped Queries

Ferro aggregates with five methods on the columns themselves — `count()`, `sum()`, `avg()`, `min()`, `max()` — inside the same `select()` lambda that projects columns. There is no `group_by()` chainer anywhere in the API, and you will not miss it: **the record shape is the grouping**. Bare fields are the keys; aggregate fields are the measures.

The examples on this page use this schema:

=== "Assignment"

    ```python
    --8<-- "docs/examples/aggregations.py:schema"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/aggregations_annotated.py:schema"
    ```

## Global Aggregates

An aggregate is a method on a column reference: `t.amount.sum()`. Aggregate fields are **user-named** — they live in the dict selector form, where the key names the output field. A projection containing *only* aggregates collapses the whole result to exactly one record, read idiomatically with `first()`:

```python
--8<-- "docs/examples/aggregations.py:global"
```

Aggregates measure whatever `where()` leaves in — relation traversal included:

```python
--8<-- "docs/examples/aggregations.py:global-where"
```

The source column may itself traverse (`t.account.balance.avg()`): the traversal narrows to rows where the relation exists, exactly like a `where()` predicate on the same path, and shares that path's join with any other clause that references it.

### What each aggregate returns

Result types are a **pinned cross-backend contract**, derived from the source column's Python type — the same query returns the same Python types on SQLite and Postgres, even where the databases themselves disagree (Postgres widens `SUM(bigint)` to `numeric`; SQLite averages everything as a float):

| Aggregate | Source column | Result type |
| :--- | :--- | :--- |
| `count()` | any column | `int` — never `None` |
| `sum()` | `int` / `float` / `Decimal` | the source numeric type |
| `avg()` | `int` / `float` | `float` |
| `avg()` | `Decimal` | `Decimal` — never a silently lossy `float` |
| `min()` / `max()` | numeric, `str`, `datetime` / `date` / `time` | the source type, via the source codec |

```python
--8<-- "docs/examples/aggregations.py:decimal-contract"
```

Every aggregate result except `count` is `T | None`, because of what comes next.

### Empty input: `None` and `0`, no COALESCE

SQL answers an aggregate over zero rows with `NULL` (`COUNT` with `0`), and Ferro passes that through verbatim — "sum of no rows" and "sum of rows totaling zero" are different facts, and zero would be the wrong identity for `min`/`max` anyway:

```python
--8<-- "docs/examples/aggregations.py:empty"
```

### `count()` counts non-NULL values

`t.price.count()` is SQL's `COUNT(price)`: it counts rows where *that column* is non-NULL. Count rows regardless of any column with the primary key (`t.id.count()`):

```python
--8<-- "docs/examples/aggregations.py:count-nulls"
```

## Grouped Queries: Bare Fields Are the Keys

Mix a plain field into an aggregate projection and the query becomes **grouped** — every non-aggregate field is a group key, and each group collapses to exactly one record:

```python
--8<-- "docs/examples/aggregations.py:grouped"
```

This is SQL's own rule, made unwritable to violate: SQL already requires every bare selected column to be a group key, so declaring the grouping separately could only ever restate the projection or contradict it (Postgres rejects the contradiction at runtime; SQLite silently answers with an arbitrary row's value). Deriving `GROUP BY` from the record shape deletes that entire error class — the dict literal is the whole story.

Group keys may traverse, and traversal narrows exactly like a predicate:

```python
--8<-- "docs/examples/aggregations.py:grouped-traversed"
```

### "Has no relation" is a visible bucket

Add `left_join()` and relation-less rows stay in — grouped under a `None` key instead of silently dropped:

```python
--8<-- "docs/examples/aggregations.py:none-bucket"
```

### Zero rows, zero groups

A grouped query over zero matching rows returns zero records — unlike a global aggregate, there is no group to report on:

```python
--8<-- "docs/examples/aggregations.py:zero-groups"
```

### Grouping is not partitioning

Grouping **collapses** rows: each group becomes one projected record of keys and measures, and the individual rows are gone. If you want complete model instances *bucketed* by a key — every transaction, arranged per account — that is a different, client-side operation over a full query:

```python
from collections import defaultdict

by_account: dict[int | None, list[Transaction]] = defaultdict(list)
for txn in await Transaction.select().all():
    by_account[txn.account_id].append(txn)
```

Reach for aggregation when you want *facts about groups*; partition in Python when you want *the rows themselves, organized*.

## Ordering Grouped Results

`order_by` on a projected query follows SQL's own ORDER BY scoping, pinned as three rules.

**Strings resolve output field names first, then root columns.** `order_by("total")` sorts by the aggregate you named `total`, even if the model happens to have a column with the same name:

```python
--8<-- "docs/examples/aggregations.py:top-n"
```

That is the **top-N idiom** — keys plus aggregates, ordered by the measure's output name, `limit()` applied to the *groups*. `limit()`/`offset()` always act on groups in a grouped query, so pagination pages through the summary, not the underlying rows.

**The lambda form spells source expressions — aggregates included.** Where strings name outputs, lambdas write the expression itself:

```python
--8<-- "docs/examples/aggregations.py:order-by-lambda"
```

An aggregate sort key must match a projected aggregate field (same function, same column, same path); an expression you did not project is a build-time error telling you to name it in the projection.

**On an aggregate projection, every sort key must be a group key or an aggregate.** Sorting groups by a column that is neither is the arbitrary-row trap again — SQLite would happily pick *some* row's value per group — so Ferro rejects it when you build the query, whether the key is a string, a lambda, or was chained before the `select()`:

```text
>>> Transaction.select(lambda t: {"acct": t.account_id, "total": t.amount.sum()}).order_by("memo")
ValueError: order_by('memo') on an aggregate projection must name a group key or an aggregate: ...
```

On a plain (non-aggregate) projection nothing changes: unselected root columns still sort, as they always have.

## The Loud Limits

Everything below fails **when you build the query** — before any SQL, with an error that names the fix:

- **Source families with no portable meaning.** `sum()`/`avg()` take numeric columns (`int`, `float`, `Decimal`); `min()`/`max()` take orderable ones (numeric, text, date/time); `count()` takes anything. Enum, UUID, JSON, and bool columns are rejected — `min()` over a UUID does not exist on Postgres, and `max()` over a native enum silently diverges between backends (definition order vs. lexical order). Where no portable meaning exists, build time is the only honest place to fail.
- **No aggregates in `where()`.** `WHERE` filters rows *before* aggregation, so `where(lambda t: t.amount.sum() > 100)` raises pointing at `having()` — the post-aggregation filter, tracked in [#291](https://github.com/syn54x/ferro-orm/issues/291). Until it lands, filter rows with `where()` and compare aggregated results in Python.
- **The builtin-`sum` trap.** `sum(t.amount)` (Python's builtin over a column reference) raises `did you mean t.amount.sum()?` instead of failing obscurely.
- **Aggregates are user-named.** An aggregate outside the dict form (`select(lambda t: t.amount.sum())`) raises: give it an output name.
- **`count()`/`exists()` on an aggregate projection.** "Count" is ambiguous between rows and groups, so both raise with both spellings: count matching rows with an unprojected query (`Transaction.where(...).count()`), count groups with `len(await q.all())`.

```python
--8<-- "docs/examples/aggregations.py:errors"
```

## See Also

- [Selecting a Column Subset](queries.md#selecting-a-column-subset) — projections, traversed fields, and output aliases, which aggregations build on
- [Querying Across Relationships](queries.md#querying-across-relationships) — traversal and join semantics (ADR-0006)
- [Typed Query Predicates](../concepts/query-typing.md#projected-queries) — how aggregate expressions type
