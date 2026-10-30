---
description: "Filter rows with lambda predicates, order and page them, reach across relations and select only the columns you need."
---

# Queries

This guide shows how to read rows with Ferro: filter them with a lambda, order and page them, reach across relations, populate related rows and select only the columns you need.

Here is a whole program. It connects, opens a session, and asks for every transaction of 100 or more going out, largest first:

```python
--8<-- "docs/examples/predicates.py:first-query"
```

It prints:

<!-- prints: predicates.py:first-query -->
```text
Rent -1450
To savings -500
```

`where()` takes a lambda whose parameter stands for one row, and `order_by()` takes a lambda naming the column to sort by. Nothing reaches the database until you await `all()`. The values in a query are always sent as parameters, never pasted into the SQL. Every query on this page runs inside a session like this one; see [Sessions](connections.md#sessions) for why and how to open one.

Every example on this page is followed by the SQL it sends, captured from Ferro when the example runs. Awaiting `all()` sends this statement; on PostgreSQL only the placeholders differ, `$1` where SQLite has `?`:

```sql
-- predicates.py:first-query (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."amount" <= ?
ORDER BY "transaction"."amount" ASC NULLS LAST;
```

The query names every column of `transaction`, the filter compares `amount` with a placeholder, and `NULLS LAST` fixes where `NULL` values sort on both databases.

## The ledger

Every example on this page queries one small ledger. An owner holds accounts, an account holds transactions, a transaction may have a category and split lines, a transfer links the two halves of a move between accounts, and tags attach to transactions many-to-many:

=== "Assignment"

    ```python
    --8<-- "docs/examples/predicates.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/predicates_annotated.py:models"
    ```

`Category.parent` points back at `Category`. A class cannot name itself in its own annotation, so the self-reference is a string (`"Category"`) and `nullable=True` makes the column optional.

The ledger holds these nine transactions:

| `id` | `memo` | `amount` | `posted_on` | `cleared_on` | account | category |
| ---: | :--- | ---: | :--- | :--- | :--- | :--- |
| 1 | Salary | 3200.00 | 2026-03-01 | 2026-03-02 | Checking | |
| 2 | Rent | -1450.00 | 2026-03-02 | 2026-03-03 | Checking | Housing |
| 3 | Market run | -86.40 | 2026-03-05 | 2026-03-06 | Checking | Groceries |
| 4 | To savings | -500.00 | 2026-03-07 | 2026-03-08 | Checking | |
| 5 | From checking | 500.00 | 2026-03-07 | 2026-03-08 | Savings | |
| 6 | Train to Lyon | -64.00 | 2026-03-09 | | Travel | |
| 7 | Dinner in Lyon | -48.50 | 2026-03-10 | | Travel | Dining |
| 8 | Office coffee | -12.75 | 2026-03-11 | 2026-03-12 | Petty cash | Dining |
| 9 | Weekly shop | -95.00 | 2026-03-12 | | Checking | |

Ada Lovelace owns Checking and Savings, Grace Hopper owns Travel, and Petty cash has no owner. Groceries and Dining sit under Food. Weekly shop is split into a Groceries line of -72.00 and a Dining line of -23.00. One transfer links To savings (its outflow) with From checking (its inflow). Rent carries the tag `tax`; Train to Lyon and Dinner in Lyon carry `reimbursable`.

## Filtering with `where()`

[`where()`][ferro.query.builder.Query.where] takes a lambda. Its parameter, named after the model in lowercase (`transaction` for `Transaction`), stands for one row; each attribute on it is a column, and comparing a column builds a condition:

```python
--8<-- "docs/examples/predicates.py:filtering"
```

The three transactions with no `cleared_on` come back. `Model.where(...)` starts a query and the `await ... .all()` runs it; `Model.select()` starts a query with no filter, for when you only want to order, page or count.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:filtering (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."cleared_on" IS NULL;
```

`== None` became `IS NULL`: in SQL, `= NULL` matches no row.

Ferro checks every column name when you build the query, before anything reaches the database. A misspelled column raises `AttributeError` with the closest match and every column and relation you can use:

```python
--8<-- "docs/examples/predicates.py:typo"
```

The list includes the `account_id` and `category_id` columns that each foreign key adds, so you can filter on them directly.

## Predicate style

A predicate is the lambda you pass to `where()`. It is the only form `where()` accepts: anything that is not callable raises `TypeError` naming the lambda form. Name the parameter after the model in lowercase singular, so the predicate reads as a sentence: `lambda transaction: transaction.amount < 0`.

The lambda's return value is type-checked as well: `where()` expects a callable that returns a condition, so `lambda transaction: True` fails your type checker. [Typed query predicates](../concepts/query-typing.md) explains what the checker sees and where checking stops.

!!! note "Changed in 0.14"
    The operator style (`Transaction.where(Transaction.amount < 0)`) and the `col()` helper were removed in 0.14. [Upgrading to 0.14](../upgrading.md#predicates-operator-style-and-col-are-gone) shows how to rewrite them as lambdas.

## Operators

Each comparison maps to one SQL operator:

| Python | SQL | Example |
| :--- | :--- | :--- |
| `==` | `=` | `lambda transaction: transaction.memo == "Rent"` |
| `!=` | `!=` | `lambda transaction: transaction.memo != "Rent"` |
| `>` `>=` `<` `<=` | `>` `>=` `<` `<=` | `lambda transaction: transaction.amount < 0` |
| `.like(pattern)` | `LIKE` | `lambda transaction: transaction.memo.like("%coffee")` |
| `.in_(values)` | `IN` | `lambda transaction: transaction.account_id.in_([1, 2])` |
| `== None` | `IS NULL` | `lambda transaction: transaction.cleared_on == None` |
| `!= None` | `IS NOT NULL` | `lambda transaction: transaction.cleared_on != None` |
| `~` | `NOT` | `lambda transaction: ~transaction.memo.like("%Lyon")` |
| `&` | `AND` | `lambda transaction: (transaction.amount < 0) & (transaction.cleared_on == None)` |
| `\|` | `OR` | `lambda transaction: (transaction.amount < -1000) \| (transaction.amount > 1000)` |
| `.exists(...)` | `EXISTS (...)` | `lambda transaction: transaction.lines.exists()`; see [existence tests](#existence-tests-on-reverse-many-to-many-relations) |

`.like()` and `.in_()` work on any column, a date included:

```python
--8<-- "docs/examples/predicates.py:operators"
```

`%` in a `LIKE` pattern matches any run of characters, so `"%coffee"` matches Office coffee, and the two dates match the two Lyon rows.

The two queries send these statements. PostgreSQL also casts each date parameter to `date`:

=== "SQLite"

    ```sql
    -- predicates.py:operators (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."memo" LIKE ?;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" IN (?, ?);
    ```

=== "PostgreSQL"

    ```sql
    -- predicates.py:operators (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."memo" LIKE $1;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" IN (CAST($1 AS date), CAST($2 AS date));
    ```

`.like()` became `LIKE`, and `.in_()` became `IN` with one placeholder per value.

## Combining conditions

Join conditions with `&` (and) and `|` (or), or chain several `where()` calls, which are joined with and:

```python
--8<-- "docs/examples/predicates.py:combining"
```

Only the two Lyon rows are uncleared and above -90; Weekly shop is uncleared but spent -95.00. The chained query keeps the debits posted on or after 10 March.

The two queries send these statements. PostgreSQL also casts the date parameter to `date`:

=== "SQLite"

    ```sql
    -- predicates.py:combining (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."cleared_on" IS NULL AND "transaction"."amount" > ?;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."amount" < ? AND "transaction"."posted_on" >= ?;
    ```

=== "PostgreSQL"

    ```sql
    -- predicates.py:combining (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."cleared_on" IS NULL AND "transaction"."amount" > $1;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."amount" < $1 AND "transaction"."posted_on" >= CAST($2 AS date);
    ```

`&` and the chained `where()` both became `AND`.

!!! warning "Parenthesize each side of `&` and `|`"
    In Python, `&` and `|` bind tighter than comparisons, so `transaction.amount < 0 & transaction.cleared_on == None` parses as `transaction.amount < (0 & transaction.cleared_on) == None`. Wrap each condition in parentheses: `(transaction.amount < 0) & (transaction.cleared_on == None)`.

Python's own `and`, `or` and `not` cannot combine conditions: Python asks each side whether it is true, and a condition has no answer until the database runs it. Ferro raises `TypeError` and names `&`, `|` and `~` instead.

## Negating conditions

Put `~` in front of any condition to negate it: a comparison, `.like()`, `.in_()` or a whole group in parentheses. There is no `not_like()` or `not_in()`; `~` covers them all:

```python
--8<-- "docs/examples/predicates.py:negation"
```

The four debits below -50 come back, minus Train to Lyon, whose memo matches `"%Lyon"`. Each `~` becomes SQL `NOT (...)` around the condition it wraps, so negated conditions combine with `&` and `|` like any other, and `~~` cancels out.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:negation (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE (NOT "transaction"."memo" LIKE ?) AND "transaction"."amount" < ?;
```

`~` wrapped the `LIKE` in `NOT`, kept in parentheses beside the `AND`.

### Negation and NULL values

SQL compares `NULL` to nothing: `cleared_on < '2026-03-05'` is neither true nor false for a row whose `cleared_on` is `NULL`, and `WHERE` keeps only the rows where the condition is true. `NOT` of an unknown is still unknown, so a negated comparison drops the `NULL` rows too:

```python
--8<-- "docs/examples/predicates.py:three-valued"
```

Two transactions cleared before 5 March and four cleared later, which leaves out the three uncleared ones from both lists. `!=` behaves the same way: `transaction.cleared_on != date(2026, 3, 2)` also leaves out the uncleared rows.

The two queries send these statements. PostgreSQL also casts the date parameter to `date`:

=== "SQLite"

    ```sql
    -- predicates.py:three-valued (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."cleared_on" < ?;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE NOT "transaction"."cleared_on" < ?;
    ```

=== "PostgreSQL"

    ```sql
    -- predicates.py:three-valued (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."cleared_on" < CAST($1 AS date);

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE NOT "transaction"."cleared_on" < CAST($1 AS date);
    ```

Neither `WHERE` mentions `NULL`, so neither keeps the uncleared rows.

To keep the `NULL` rows, ask for them with an `== None` branch:

```python
--8<-- "docs/examples/predicates.py:keep-nulls"
```

The four later rows and the three uncleared rows come back.

The query sends this. PostgreSQL also casts the date parameter to `date`:

=== "SQLite"

    ```sql
    -- predicates.py:keep-nulls (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE (NOT "transaction"."cleared_on" < ?) OR "transaction"."cleared_on" IS NULL;
    ```

=== "PostgreSQL"

    ```sql
    -- predicates.py:keep-nulls (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE (NOT "transaction"."cleared_on" < CAST($1 AS date)) OR "transaction"."cleared_on" IS NULL;
    ```

The `IS NULL` branch is what brings the uncleared rows back.

## Fetching by primary key

[`Model.get(pk)`][ferro.models.Model.get] loads one row by its primary key and returns the model, never `None`. A missing row raises `ModelDoesNotExist`, a `LookupError` that carries `.model` and `.pk`, so an HTTP layer can turn it into a 404. When a missing row is normal, use [`get_or_none()`][ferro.models.Model.get_or_none]:

```python
--8<-- "docs/examples/predicates.py:get"
```

`get(2)` returns Rent, `get(99)` raises, and `get_or_none(99)` returns `None`. Both methods also exist on `Model.using("name")` for [named connections](connections.md#named-connections).

The three calls send one statement each, the same statement; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:get (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."id" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."id" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."id" = ?
LIMIT ?;
```

Each is a primary-key filter with a `LIMIT` of 1. `get(99)` sent its statement too; the error comes from the empty result.

## Ordering, limit & offset

[`order_by()`][ferro.query.builder.Query.order_by] takes a lambda naming the column, and an optional direction (`"asc"`, the default, or `"desc"`). `limit()` and `offset()` cut a slice out of the ordered rows:

```python
--8<-- "docs/examples/predicates.py:ordering-slicing"
```

The first three are the largest debits; `offset(3)` skips them and returns the next three. Chain `order_by()` again to break ties on a second column.

The two queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:ordering-slicing (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE TRUE
ORDER BY "transaction"."amount" ASC NULLS LAST
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE TRUE
ORDER BY "transaction"."amount" ASC NULLS LAST
LIMIT ?
OFFSET ?;
```

`WHERE TRUE` stands in for the missing filter, and `limit()` and `offset()` became `LIMIT` and `OFFSET`.

`order_by()` also accepts a column name as a string (`order_by("amount", "desc")`). The lambda is the documented form because only a lambda can [order by a related column](#ordering-by-a-related-column).

`NULL` values sort last on every database unless you say otherwise. Pass `nulls="first"` to put them first, or `nulls="native"` for each database's own default, which differs between PostgreSQL and SQLite for `"desc"`:

```python
--8<-- "docs/examples/predicates.py:nulls-ordering"
```

The three uncleared transactions lead, then the latest cleared one, Office coffee.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:nulls-ordering (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE TRUE
ORDER BY "transaction"."cleared_on" DESC NULLS FIRST, "transaction"."id" ASC NULLS LAST
LIMIT ?;
```

`nulls="first"` became `NULLS FIRST`; the tie-breaker on `id` keeps the default, `NULLS LAST`.

## Paging with `after()` and `before()`

[`after()`][ferro.query.builder.Query.after] starts a page right after a row you already have, in the order the query declares. Pass it the last row of the page you showed:

```python
--8<-- "docs/examples/predicates.py:after-paging"
```

The second page starts at To savings, the row after Market run. Unlike `offset()`, it does not skip or repeat rows when rows are added or removed between the two requests.

The two pages send these statements. PostgreSQL also casts the date parameters to `date`:

=== "SQLite"

    ```sql
    -- predicates.py:after-paging (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE TRUE
    ORDER BY "transaction"."posted_on" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST
    LIMIT ?;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" > ? OR "transaction"."posted_on" IS NULL OR ("transaction"."posted_on" = ? AND ("transaction"."id" > ? OR "transaction"."id" IS NULL))
    ORDER BY "transaction"."posted_on" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- predicates.py:after-paging (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE TRUE
    ORDER BY "transaction"."posted_on" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST
    LIMIT $1;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" > CAST($1 AS date) OR "transaction"."posted_on" IS NULL OR ("transaction"."posted_on" = CAST($2 AS date) AND ("transaction"."id" > $3 OR "transaction"."id" IS NULL))
    ORDER BY "transaction"."posted_on" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST
    LIMIT $4;
    ```

The second page starts with a condition on the order keys instead of an `OFFSET`: a later `posted_on`, or Market run's `posted_on` with a larger `id`. The `IS NULL` branches match the `NULLS LAST` order.

`after()` follows four rules:

- The order keys must include the primary key, so every row has exactly one place in the order. Ferro does not add it for you: `after()` raises `ValueError` if `id` is missing. Here, To savings and From checking share a `posted_on`, and `id` settles which comes first.
- The bound is exclusive: the row you pass is not on the next page.
- A query has one start. `after()` cannot be combined with `offset()` or with `before()`.
- Any order key except the primary key may be `None`.

`after(row)` reads the row's order-key values for you. [`position_of()`][ferro.query.builder.Query.position_of] returns those values as a tuple, which you can put in a cursor and pass back later:

```python
--8<-- "docs/examples/predicates.py:position-of"
```

Market run's position is its `posted_on` and its `id`, and paging after that tuple returns the same page as paging after the row. `query.after(row)` is `query.after(query.position_of(row))`.

Paging after the tuple sends the same statement as paging after the row. PostgreSQL also casts the date parameters to `date`:

=== "SQLite"

    ```sql
    -- predicates.py:position-of (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" > ? OR "transaction"."posted_on" IS NULL OR ("transaction"."posted_on" = ? AND ("transaction"."id" > ? OR "transaction"."id" IS NULL))
    ORDER BY "transaction"."posted_on" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- predicates.py:position-of (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" > CAST($1 AS date) OR "transaction"."posted_on" IS NULL OR ("transaction"."posted_on" = CAST($2 AS date) AND ("transaction"."id" > $3 OR "transaction"."id" IS NULL))
    ORDER BY "transaction"."posted_on" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST
    LIMIT $4;
    ```

`position_of()` itself sends nothing; it reads the values off the row.

[`before()`][ferro.query.builder.Query.before] pages backwards. With a limit it returns the page immediately before the bound, still in the declared order; without one it returns every earlier row:

```python
--8<-- "docs/examples/predicates.py:before-paging"
```

`before(...).limit(2)` returns the two rows before To savings, and `before(...)` alone returns all three earlier rows. `first()` returns the row immediately before the bound, while `all()[0]` returns the earliest row, so use `first()` for "the previous row".

The three queries send these statements. PostgreSQL also casts the date parameters to `date`:

=== "SQLite"

    ```sql
    -- predicates.py:before-paging (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" < ? OR ("transaction"."posted_on" = ? AND "transaction"."id" < ?)
    ORDER BY "transaction"."posted_on" DESC NULLS FIRST, "transaction"."id" DESC NULLS FIRST
    LIMIT ?;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" < ? OR ("transaction"."posted_on" = ? AND "transaction"."id" < ?)
    ORDER BY "transaction"."posted_on" DESC NULLS FIRST, "transaction"."id" DESC NULLS FIRST;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" < ? OR ("transaction"."posted_on" = ? AND "transaction"."id" < ?)
    ORDER BY "transaction"."posted_on" DESC NULLS FIRST, "transaction"."id" DESC NULLS FIRST
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- predicates.py:before-paging (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" < CAST($1 AS date) OR ("transaction"."posted_on" = CAST($2 AS date) AND "transaction"."id" < $3)
    ORDER BY "transaction"."posted_on" DESC NULLS FIRST, "transaction"."id" DESC NULLS FIRST
    LIMIT $4;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" < CAST($1 AS date) OR ("transaction"."posted_on" = CAST($2 AS date) AND "transaction"."id" < $3)
    ORDER BY "transaction"."posted_on" DESC NULLS FIRST, "transaction"."id" DESC NULLS FIRST;

    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    WHERE "transaction"."posted_on" < CAST($1 AS date) OR ("transaction"."posted_on" = CAST($2 AS date) AND "transaction"."id" < $3)
    ORDER BY "transaction"."posted_on" DESC NULLS FIRST, "transaction"."id" DESC NULLS FIRST
    LIMIT $4;
    ```

`before()` flips each comparison and reverses each sort key with its `NULLS` placement, so the `LIMIT` keeps the rows nearest the bound; Ferro puts them back in the declared order.

A `None` order-key value is how paging continues through `NULL`s. Ordered by `cleared_on` descending, the uncleared rows sort last; to continue after Train to Lyon, pass `None` for its `cleared_on`:

```python
--8<-- "docs/examples/predicates.py:after-null-paging"
```

The two uncleared rows after Train to Lyon come back. The same methods page a query ordered by a [related column](#ordering-by-a-related-column) or a [projected query](#composing-projections). [Pagination](../howto/pagination.md) shows how to wire this into an API.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:after-null-paging (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."cleared_on" IS NULL AND ("transaction"."id" > ? OR "transaction"."id" IS NULL)
ORDER BY "transaction"."cleared_on" DESC NULLS LAST, "transaction"."id" ASC NULLS LAST;
```

With `None` for `cleared_on`, the condition stays among the `NULL` rows and continues by `id`.

## Executing queries

A query runs when you await one of its terminals: [`all()`][ferro.query.builder.Query.all] returns a list of instances, [`first()`][ferro.query.builder.Query.first] returns the first row or `None`, [`count()`][ferro.query.builder.Query.count] returns how many rows match, and [`exists()`][ferro.query.builder.Query.exists] returns whether any does:

```python
--8<-- "docs/examples/predicates.py:terminals"
```

`count()` and `exists()` hydrate no instances. `Model.all()` is short for `Model.select().all()`.

The four calls send these statements; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:terminals (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction";

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE TRUE
ORDER BY "transaction"."posted_on" DESC NULLS LAST
LIMIT ?;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."cleared_on" IS NULL;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."memo" LIKE ?;
```

`count()` and `exists()` both send `SELECT COUNT(*)`; `exists()` compares the count with zero.

## Query immutability

`where()`, `order_by()`, `limit()` and the other chain methods each return a new query and leave the one you called them on unchanged. You can keep a base query and branch from it:

```python
--8<-- "docs/examples/predicates.py:immutability"
```

The three queries built from `debits` are independent, and `debits` still counts all seven debits afterwards.

The four calls send these statements; on PostgreSQL only the placeholders differ:

```sql
-- predicates.py:immutability (sqlite)
SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."amount" < ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."amount" < ?
ORDER BY "transaction"."amount" ASC NULLS LAST
LIMIT ?;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."amount" < ? AND "transaction"."cleared_on" IS NULL;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."amount" < ?;
```

Every statement carries the `amount < ?` filter of `debits`; only the branches add to it.

## Querying across relationships

A `where()` or `order_by()` lambda can follow a foreign key to the related model's columns. Write the relation, then the column:

```python
--8<-- "docs/examples/traversal.py:basic"
```

`transaction.account.label` reads the label of each transaction's account, and From checking is the one transaction on Savings. You do not write the join: following `transaction.account` is the join, and the query still returns `Transaction` instances. These examples run inside the session from [the first block](#queries). The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:basic (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "j1_account"."label" = ?;
```

Following `transaction.account` became one `INNER JOIN` on `account`, under the alias `j1_account`.

### Traversing at any depth

Each step resolves against the next model, so you can chain as far as the foreign keys go. `transaction.account.owner.email` walks from `Transaction` to `Account` to `Owner` in one statement:

```python
--8<-- "docs/examples/traversal.py:multi-hop"
```

Grace Hopper owns Travel, so both Travel transactions come back. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:multi-hop (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE "j2_owner"."email" = ?;
```

Each step is one more join: `j2_owner` joins on the account's `owner_id`. Ferro checks each step against its own model when you build the query, and a misspelled step names that model and the closest match:

```python
--8<-- "docs/examples/traversal.py:typo"
```

The error comes from `Owner`, the model at the misspelled step.

### Join type for traversed relations

Every step is an inner join, whether or not the foreign key is nullable. A row whose relation is missing therefore drops out of a query that follows that relation:

```python
--8<-- "docs/examples/traversal.py:inner-narrows"
```

Only Grace's two transactions come back. Office coffee is on Petty cash, which has no owner; it has no owner name to compare, so the join drops it. The join type never depends on nullability, so making a foreign key nullable later never changes what an existing query returns. To keep the rows without a relation, use [`left_join()`](#keeping-rows-that-have-no-relation).

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:inner-narrows (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE "j2_owner"."name" <> ?;
```

The `INNER JOIN` on `owner` drops Office coffee: Petty cash has no `owner_id` to join on.

The join applies to the whole query, not to one side of an `|`:

```python
--8<-- "docs/examples/traversal.py:inner-or"
```

Office coffee matches the second branch, but the join on `account.owner` has already dropped it.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:inner-or (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE "j2_owner"."name" = ? OR "transaction"."memo" = ?;
```

The `OR` sits in the `WHERE`, after both joins.

`count()` counts the same rows. Following a foreign key reaches one related row per row, so the join never multiplies results:

```python
--8<-- "docs/examples/traversal.py:count"
```

Ada's two accounts hold six transactions.

`count()` sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:count (sqlite)
SELECT COUNT(*)
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE "j2_owner"."name" = ?;
```

`count()` counts over the same two joins.

### Ordering by a related column

`order_by()` follows relations the same way:

```python
--8<-- "docs/examples/traversal.py:order-by"
```

Accounts sort by label, descending: Travel, Savings, Petty cash, then Checking after the limit. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:order-by (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE TRUE
ORDER BY "j1_account"."label" DESC NULLS LAST, "transaction"."id" ASC NULLS LAST
LIMIT ?;
```

The sort reads the joined `label`, then `id` breaks ties. Sorting is an inner join too, so ordering by a column that some rows cannot reach drops those rows:

```python
--8<-- "docs/examples/traversal.py:order-by-narrows"
```

Eight of the nine transactions come back. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:order-by-narrows (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE TRUE
ORDER BY "j2_owner"."name" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST;
```

Office coffee drops out at the `owner` join. `after()` and `before()` take a tuple of the related values, so you can page without loading the relation:

```python
--8<-- "docs/examples/traversal.py:traversed-paging"
```

The page starts after Office coffee, the last Petty cash row. `position_of()` and `after(row)` read the related value off the instance, so they need the relation [populated with `include()`](#populating-relations-with-include); a tuple does not.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:traversed-paging (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "j1_account"."label" > ? OR "j1_account"."label" IS NULL OR ("j1_account"."label" = ? AND ("transaction"."id" > ? OR "transaction"."id" IS NULL))
ORDER BY "j1_account"."label" ASC NULLS LAST, "transaction"."id" ASC NULLS LAST
LIMIT ?;
```

The position compares the joined `label` and then `id`, as a position on the model's own columns would.

### One join per relation path

The path decides the join. Use `transaction.account` in two `where()` calls, in both sides of an `|`, or in `where()` and `order_by()`, and the query still joins `account` once. You never name or manage an alias:

```python
--8<-- "docs/examples/traversal.py:composed"
```

The traversal filter, a plain column filter, an ordering and a limit run as one statement, and Ada's two largest debits come back.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:composed (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE "j2_owner"."name" = ? AND "transaction"."amount" < ?
ORDER BY "transaction"."amount" ASC NULLS LAST
LIMIT ?;
```

`account` and `owner` are joined once each; the plain filter and the ordering read `transaction` itself.

### Comparing to a related instance

Each foreign key adds a `<relation>_id` column, and filtering on it needs no join:

```python
--8<-- "docs/examples/traversal.py:shadow-column"
```

From checking is the one transaction whose `account_id` is Savings' `id`. Fetching Savings and the query send these statements; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:shadow-column (sqlite)
SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
WHERE "account"."id" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."account_id" = ?;
```

The filter reads `account_id` on `transaction`, with no join. Comparing the relation to a saved instance means the same thing, with the same join-free query:

```python
--8<-- "docs/examples/traversal.py:instance-eq"
```

`transaction.account == savings` is `transaction.account_id == savings.id`; `!=` returns the other eight. The two queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:instance-eq (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."account_id" = ?;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."account_id" <> ?;
```

Both compare `account_id`, the second with `<>`. At the end of a longer path, the comparison uses the last step's foreign key column:

```python
--8<-- "docs/examples/traversal.py:deep-instance-eq"
```

`transaction.account.owner == grace` joins `account` and compares its `owner_id`. Fetching Grace and the query send these statements; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:deep-instance-eq (sqlite)
SELECT "owner"."email", "owner"."id", "owner"."name"
FROM "owner"
WHERE "owner"."id" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "j1_account"."owner_id" = ?;
```

The first statement fetched Grace; the query joins `account` and never `owner`. An instance that has not been saved has no primary key to compare, so Ferro refuses it when you build the query:

```python
--8<-- "docs/examples/traversal.py:unpersisted"
```

Save the account first, then compare.

### Testing for a relation

A bare `join()` with no condition keeps the rows whose relation exists:

```python
--8<-- "docs/examples/traversal.py:existence"
```

Petty cash has no owner and is left out. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:existence (sqlite)
SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
INNER JOIN "owner" AS "j1_owner" ON "account"."owner_id" = "j1_owner"."id"
WHERE TRUE;
```

The bare `join()` is an `INNER JOIN` with nothing added to the `WHERE`. To ask the opposite, compare the relation to `None`: `== None` and `!= None` become `IS NULL` and `IS NOT NULL` on the foreign key column, with no join:

```python
--8<-- "docs/examples/traversal.py:is-null"
```

Both forms ask about a foreign key on the model you query. To ask whether a row has related rows on another table, such as "has at least one split line", use an [existence test](#existence-tests-on-reverse-many-to-many-relations).

The two queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:is-null (sqlite)
SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
WHERE "account"."owner_id" IS NULL;

SELECT COUNT(*)
FROM "account"
WHERE "account"."owner_id" IS NOT NULL;
```

Neither statement joins `owner`.

### Keeping rows that have no relation

[`left_join()`][ferro.query.builder.Query.left_join] keeps the rows whose relation is missing. It makes every step of its path a left join, and a `where()` or `order_by()` on the same path uses it too:

```python
--8<-- "docs/examples/traversal.py:left-join"
```

All four accounts come back, Petty cash last because its owner name is `NULL`. Without `left_join()`, ordering by `account.owner.name` would drop it. Declaring `join()` and `left_join()` on the same path raises `ValueError`.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:left-join (sqlite)
SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
LEFT JOIN "owner" AS "j1_owner" ON "account"."owner_id" = "j1_owner"."id"
WHERE TRUE
ORDER BY "j1_owner"."name" ASC NULLS LAST, "account"."id" ASC NULLS LAST;
```

The `LEFT JOIN` keeps Petty cash with `NULL` in every `j1_owner` column, and `NULLS LAST` sorts it to the end.

### Two foreign keys to the same table

Two foreign keys to one model are two paths, so they are two joins:

```python
--8<-- "docs/examples/traversal.py:two-fk-query"
```

`transfer.outflow` and `transfer.inflow` both point at `Transaction`, and each reaches its own account. The one transfer runs from transaction 4 on Checking to transaction 5 on Savings.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:two-fk-query (sqlite)
SELECT "transfer"."id", "transfer"."inflow_id", "transfer"."outflow_id"
FROM "transfer"
INNER JOIN "transaction" AS "j1_outflow" ON "transfer"."outflow_id" = "j1_outflow"."id"
INNER JOIN "account" AS "j2_account" ON "j1_outflow"."account_id" = "j2_account"."id"
INNER JOIN "transaction" AS "j3_inflow" ON "transfer"."inflow_id" = "j3_inflow"."id"
INNER JOIN "account" AS "j4_account" ON "j3_inflow"."account_id" = "j4_account"."id"
WHERE "j2_account"."label" = ? AND "j4_account"."label" = ?;
```

Each path has its own aliases, `j1_outflow` and `j3_inflow`, each with its own join on `account`.

### Self-referential traversal

A foreign key to the same model traverses like any other:

```python
--8<-- "docs/examples/traversal.py:self-fk-query"
```

Groceries and Dining have Food as their parent.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:self-fk-query (sqlite)
SELECT "category"."id", "category"."name", "category"."parent_id"
FROM "category"
INNER JOIN "category" AS "j1_parent" ON "category"."parent_id" = "j1_parent"."id"
WHERE "j1_parent"."name" = ?;
```

The `category` table joins itself under the alias `j1_parent`.

### Traversing from a many-to-many

A many-to-many relation on an instance is a query, and its lambda can follow foreign keys on the other model:

```python
--8<-- "docs/examples/traversal.py:m2m-query"
```

Of the two reimbursable transactions, only Dinner in Lyon is in Dining.

Fetching the tag and the query send these statements; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:m2m-query (sqlite)
SELECT "tag"."id", "tag"."name"
FROM "tag"
WHERE "tag"."name" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
INNER JOIN "tag_transactions" ON "transaction"."id" = "tag_transactions"."transaction_id"
INNER JOIN "category" AS "j1_category" ON "transaction"."category_id" = "j1_category"."id"
WHERE "tag_transactions"."tag_id" = ? AND "j1_category"."name" = ?;
```

The many-to-many query joins the link table, `tag_transactions`, and `transaction.category` adds a join on `category`.

### Filtering reverse relations

A reverse relation on an instance (`account.transactions`) is a query too. Filter, order and count it before you await it:

```python
--8<-- "docs/examples/traversal.py:reverse-filter"
```

Checking holds five transactions, and its two largest debits are Rent and To savings. That query starts from one account. To filter accounts by what their transactions hold, use an [existence test](#existence-tests-on-reverse-many-to-many-relations).

Fetching Checking and the two queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- traversal.py:reverse-filter (sqlite)
SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
WHERE "account"."id" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."account_id" = ? AND "transaction"."amount" < ?
ORDER BY "transaction"."amount" ASC NULLS LAST
LIMIT ?;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."account_id" = ?;
```

The reverse relation became a filter on `account_id`.

### Result type of traversed queries

A traversed query returns the model you queried: filtering `Transaction` through `transaction.account.owner` still returns `Transaction` instances, and it does not load the accounts. `await transaction.account` still runs its own query. To load related rows with the results, use [`include()`](#populating-relations-with-include).

### Traversal in `update()` and `delete()`

Portable SQL has no `UPDATE ... JOIN` or `DELETE ... JOIN`, so `update()` and `delete()` refuse a traversed predicate before any SQL runs:

```python
--8<-- "docs/examples/traversal.py:mutate-error"
```

The message names the fix. Fetch the primary keys through the join first, then update or delete by that key set:

```python
--8<-- "docs/examples/traversal.py:mutate-limitation"
```

Both Travel transactions are marked cleared. A join-free relation filter, such as `transaction.account == travel` or `transaction.category == None`, is allowed on `update()` and `delete()` as it is.

The fetch and the update send these statements. PostgreSQL also casts the date parameter to `date`:

=== "SQLite"

    ```sql
    -- traversal.py:mutate-limitation (sqlite)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
    WHERE "j1_account"."label" = ?;

    UPDATE "transaction"
    SET "cleared_on" = ?
    WHERE "transaction"."id" IN (?, ?);
    ```

=== "PostgreSQL"

    ```sql
    -- traversal.py:mutate-limitation (postgres)
    SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
    FROM "transaction"
    INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
    WHERE "j1_account"."label" = $1;

    UPDATE "transaction"
    SET "cleared_on" = CAST($1 AS date)
    WHERE "transaction"."id" IN ($2, $3);
    ```

The join stays in the `SELECT`; the `UPDATE` filters on the fetched keys alone.

### Unsupported predicate forms

A lambda that returns a bare relation (`lambda transaction: transaction.account`) filters nothing, so it raises `TypeError` and names the forms that do: `== None`, `== instance` or a column comparison. Python's `and` and `or` raise `TypeError` too:

```python
--8<-- "docs/examples/traversal.py:guardrails"
```

Both refusals happen when you build the query. [Relationships](relationships.md) shows how to declare foreign keys and reverse relations. The [`Query` reference](../api/queries.md) lists `join()` and `left_join()` with their parameters.

## Existence tests on reverse & many-to-many relations

A foreign key points from the row you query to one related row. A reverse relation (`BackRef`) or a many-to-many relation goes the other way, to any number of rows on another table. In a predicate, those relations answer one question, "is there at least one?", with `.exists()`:

```python
--8<-- "docs/examples/existence_tests.py:first"
```

Weekly shop is the only transaction with split lines. `.exists()` becomes a correlated `EXISTS` subquery, not a join, so each matching row comes back once however many related rows it has. It works the same for a one-to-one reverse relation, a one-to-many one and a many-to-many one, so changing a relation's cardinality never breaks a query. These examples run inside the session from [the first block](#queries).

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:first (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE EXISTS(SELECT ? FROM "splitline" AS "x1_lines" WHERE "x1_lines"."transaction_id" = "transaction"."id");
```

The `EXISTS` subquery over `splitline` refers back to the outer `transaction` row. What it selects does not matter to `EXISTS`, so it selects a bound constant.

### Bare tests and negation

A bare `.exists()` asks whether any related row is there, and `~` asks whether none is:

```python
--8<-- "docs/examples/existence_tests.py:bare"
```

`transfer_out` and `transfer_in` are the one-to-one reverse relations of `Transfer.outflow` and `Transfer.inflow`. The two halves of the transfer come back, and the other seven rows are in no transfer.

The two queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:bare (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE EXISTS(SELECT ? FROM "transfer" AS "x1_transfer_out" WHERE "x1_transfer_out"."outflow_id" = "transaction"."id") OR EXISTS(SELECT ? FROM "transfer" AS "x2_transfer_in" WHERE "x2_transfer_in"."inflow_id" = "transaction"."id");

SELECT COUNT(*)
FROM "transaction"
WHERE (NOT EXISTS(SELECT ? FROM "transfer" AS "x1_transfer_out" WHERE "x1_transfer_out"."outflow_id" = "transaction"."id")) AND (NOT EXISTS(SELECT ? FROM "transfer" AS "x2_transfer_in" WHERE "x2_transfer_in"."inflow_id" = "transaction"."id"));
```

`~` became `NOT EXISTS`, and each relation has its own subquery.

### Scoping with an inner predicate

Pass `.exists()` a lambda to choose which related rows count. The inner lambda is a full predicate over the related model, with every operator, `&`, `|`, `~`, traversal and further existence tests:

```python
--8<-- "docs/examples/existence_tests.py:scoped"
```

Market run has Groceries itself, and Weekly shop has it on a split line. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:scoped (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."category_id" = ? OR EXISTS(SELECT ? FROM "splitline" AS "x1_lines" WHERE "x1_lines"."transaction_id" = "transaction"."id" AND "x1_lines"."category_id" = ?);
```

The inner lambda's condition joined the subquery's `WHERE`. The result has one row per transaction, so ordering and paging work as on any query:

```python
--8<-- "docs/examples/existence_tests.py:composes"
```

Weekly shop, at -95.00, is the larger of the two.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:composes (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."category_id" = ? OR EXISTS(SELECT ? FROM "splitline" AS "x1_lines" WHERE "x1_lines"."transaction_id" = "transaction"."id" AND "x1_lines"."category_id" = ?)
ORDER BY "transaction"."amount" ASC NULLS LAST
LIMIT ?;
```

`ORDER BY` and `LIMIT` apply to the outer query.

### Grouping conditions

Two conditions on the same relation can ask two different questions: is there one related row that matches both, or a row for each? Where you put the conditions decides which:

```python
--8<-- "docs/examples/existence_tests.py:grouping"
```

No single split line is both Dining and below -50, but Weekly shop has a Dining line and a different line below -50. Writing `transaction.lines.category_id` directly raises an error, because nothing on the page would say which of the two questions it asks.

The two queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:grouping (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE EXISTS(SELECT ? FROM "splitline" AS "x1_lines" WHERE "x1_lines"."transaction_id" = "transaction"."id" AND ("x1_lines"."category_id" = ? AND "x1_lines"."amount" < ?));

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE EXISTS(SELECT ? FROM "splitline" AS "x1_lines" WHERE "x1_lines"."transaction_id" = "transaction"."id" AND "x1_lines"."category_id" = ?) AND EXISTS(SELECT ? FROM "splitline" AS "x2_lines" WHERE "x2_lines"."transaction_id" = "transaction"."id" AND "x2_lines"."amount" < ?);
```

One subquery holding both conditions asks for one line that matches both; two subqueries ask for a line for each.

### Traversal inside the test, and nesting

The inner lambda can follow foreign keys on the related model. Those joins run inside the `EXISTS` subquery, with the same [inner-join rule](#join-type-for-traversed-relations), so they filter the related rows and never the outer ones:

```python
--8<-- "docs/examples/existence_tests.py:traversal-inside"
```

Weekly shop's lines are in Groceries and Dining, both under Food. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:traversal-inside (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE EXISTS(SELECT ? FROM "splitline" AS "x1_lines" INNER JOIN "category" AS "x2_category" ON "x1_lines"."category_id" = "x2_category"."id" INNER JOIN "category" AS "x3_parent" ON "x2_category"."parent_id" = "x3_parent"."id" WHERE "x1_lines"."transaction_id" = "transaction"."id" AND "x3_parent"."name" = ?);
```

Both `category` joins sit inside the subquery. Existence tests nest to any depth:

```python
--8<-- "docs/examples/existence_tests.py:nested"
```

Only Ada has an account holding a split transaction.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:nested (sqlite)
SELECT "owner"."email", "owner"."id", "owner"."name"
FROM "owner"
WHERE EXISTS(SELECT ? FROM "account" AS "x1_accounts" WHERE "x1_accounts"."owner_id" = "owner"."id" AND EXISTS(SELECT ? FROM "transaction" AS "x2_transactions" WHERE "x2_transactions"."account_id" = "x1_accounts"."id" AND EXISTS(SELECT ? FROM "splitline" AS "x3_lines" WHERE "x3_lines"."transaction_id" = "x2_transactions"."id")));
```

Each level is an `EXISTS` inside the one before, tied to its parent's row.

### Many-to-many

A many-to-many relation reads the same from either side. The test goes through the link table, and the inner lambda scopes over the other model:

```python
--8<-- "docs/examples/existence_tests.py:m2m"
```

Rent carries `tax`, `reimbursable` is on the uncleared Lyon rows, and six transactions have no tag.

The three queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- existence_tests.py:m2m (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE EXISTS(SELECT ? FROM "tag_transactions" AS "x1_tags" INNER JOIN "tag" AS "x2_tags" ON "x1_tags"."tag_id" = "x2_tags"."id" WHERE "x1_tags"."transaction_id" = "transaction"."id" AND "x2_tags"."name" = ?);

SELECT "tag"."id", "tag"."name"
FROM "tag"
WHERE EXISTS(SELECT ? FROM "tag_transactions" AS "x1_transactions" INNER JOIN "transaction" AS "x2_transactions" ON "x1_transactions"."transaction_id" = "x2_transactions"."id" WHERE "x1_transactions"."tag_id" = "tag"."id" AND "x2_transactions"."cleared_on" IS NULL);

SELECT COUNT(*)
FROM "transaction"
WHERE NOT EXISTS(SELECT ? FROM "tag_transactions" AS "x1_tags" INNER JOIN "tag" AS "x2_tags" ON "x1_tags"."tag_id" = "x2_tags"."id" WHERE "x1_tags"."transaction_id" = "transaction"."id");
```

Each test reaches the other model through the link table, `tag_transactions`, from either side.

### Unsupported reverse-relation forms

A reverse or many-to-many relation is tested, never traversed. Every other use in a query fails when you build it, and the message names the `.exists()` form to use:

- Reading a column (`transaction.lines.category_id`) raises `AttributeError`. Put the condition in the inner lambda: `transaction.lines.exists(lambda split_line: split_line.category_id == ...)`.
- Comparing the relation, including `transaction.transfer_out != None`, raises `TypeError`. The relation has no column on this table to be `NULL`; write `transaction.transfer_out.exists()`, or `~transaction.transfer_out.exists()` for none.
- `in_()` with a query (`transaction.id.in_(query)`) raises `TypeError`. An existence test answers the same question without matching ids by hand.
- `join()` and `left_join()` on a reverse relation raise `TypeError`, because the join would repeat each row once per related row.
- The inner lambda sees only its own parameter. Referring to the outer lambda's parameter raises an error; see [Not yet supported](#not-yet-supported).

```python
--8<-- "docs/examples/existence_tests.py:dead-ends"
```

Each of the three refusals names the existence test to write instead.

!!! note "Two meanings of `exists`"
    `transaction.lines.exists(...)` inside a predicate tests a relation. `await query.exists()` is the terminal that asks whether the whole query matches any row. Both ask "is there at least one?", at different levels.

The [`Query` reference](../api/queries.md) describes `.exists()` with its parameters.

## Populating relations with `include()`

Reading a relation runs a query. A list of 100 transactions that shows each account's label awaits `transaction.account` 100 times: 101 statements for one screen. [`include()`][ferro.query.builder.Query.include] asks the query to load the related rows in the same statement:

```python
--8<-- "docs/examples/populated_relations.py:basic"
```

It prints:

<!-- prints: populated_relations.py:basic -->
```text
Weekly shop | Checking
Office coffee | Petty cash
Dinner in Lyon | Travel
```

The query still returns `list[Transaction]`, and `transaction.account` is now the `Account` instance itself, as the field's annotation (`account: Account`) says. Reading it needs no `await` and runs no query. These examples run inside the session from [the first block](#queries).

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:basic (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_account"."id" AS "j1_account__id", "j1_account"."label" AS "j1_account__label", "j1_account"."owner_id" AS "j1_account__owner_id"
FROM "transaction"
LEFT JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE TRUE
ORDER BY "transaction"."id" DESC NULLS LAST
LIMIT ?;
```

The account's columns come back in the same rows, through a `LEFT JOIN`, under `j1_account__` names.

### The population contract

A relation on an instance is either populated or not:

- **Populated**, because a query included it: the attribute is the related instance. A nullable foreign key with no row populates as `None`.
- **Not populated**: the attribute is awaitable and runs its own query:

```python
--8<-- "docs/examples/populated_relations.py:awaitable"
```

`await transaction.account` fetched the Travel account with a second query.

The fetch and the await send these statements; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:awaitable (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."id" = ?
LIMIT ?;

SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
WHERE "account"."id" = ?
LIMIT ?;
```

The second statement is the one `await transaction.account` ran.

!!! warning "Awaiting a populated relation raises"
    On a populated instance, `await transaction.account` raises `TypeError: 'Account' object can't be awaited`, because the attribute is the instance, not a coroutine. Decide at the query: if you will read the relation, include it.

There is no separate "loaded" model type: an included query returns `list[Transaction]`, and a populated relation is an ordinary instance. [Typed query predicates](../concepts/query-typing.md#included-queries) explains why.

### Row membership under `include()`

`include()` never changes which rows a query returns. A row whose relation is missing stays, with the relation populated as `None`:

```python
--8<-- "docs/examples/populated_relations.py:membership"
```

All nine transactions come back, and Salary's `category` is `None`. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:membership (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_category"."id" AS "j1_category__id", "j1_category"."name" AS "j1_category__name", "j1_category"."parent_id" AS "j1_category__parent_id"
FROM "transaction"
LEFT JOIN "category" AS "j1_category" ON "transaction"."category_id" = "j1_category"."id"
WHERE TRUE;
```

A relation only `include()` uses becomes a `LEFT JOIN`, which keeps Salary. A relation a `where()` or `order_by()` also follows keeps that clause's inner join, so the filter still decides membership:

```python
--8<-- "docs/examples/populated_relations.py:interplay"
```

The `where()` keeps the two Travel rows, and `include()` attaches their account. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:interplay (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_account"."id" AS "j1_account__id", "j1_account"."label" AS "j1_account__label", "j1_account"."owner_id" AS "j1_account__owner_id"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "j1_account"."label" = ?;
```

The filter and the population share one `INNER JOIN`. `count()` and `exists()` measure the same rows with or without `include()`:

```python
--8<-- "docs/examples/populated_relations.py:count"
```

Both count the seven debits.

The two calls send the same statement; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:count (sqlite)
SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."amount" < ?;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."amount" < ?;
```

`include()` adds nothing to a count.

### Multi-hop paths

Including a path populates every step of it. A `NULL` along the way ends the chain with `None`, and the row stays:

```python
--8<-- "docs/examples/populated_relations.py:multi-hop"
```

Salary's account and its owner are both loaded. Office coffee's account is Petty cash, whose owner is `None`.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:multi-hop (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_account"."id" AS "j1_account__id", "j1_account"."label" AS "j1_account__label", "j1_account"."owner_id" AS "j1_account__owner_id", "j2_owner"."email" AS "j2_owner__email", "j2_owner"."id" AS "j2_owner__id", "j2_owner"."name" AS "j2_owner__name"
FROM "transaction"
LEFT JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
LEFT JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE TRUE
ORDER BY "transaction"."id" ASC NULLS LAST;
```

Each step is one more `LEFT JOIN`, and each related row's columns come back under its alias.

Includes add up, in any order, and repeating one changes nothing: `.include(lambda transaction: transaction.account)` followed by `.include(lambda transaction: transaction.account.owner)` is the same query as the second one alone.

### Identity of populated instances

In a session, each row is one object. A populated account is the same object a direct fetch of that account returns, shared by every row that points at it:

```python
--8<-- "docs/examples/populated_relations.py:identity"
```

Both Travel transactions point at the `travel` object fetched first. Fetching Travel and the query send these statements; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:identity (sqlite)
SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
WHERE "account"."id" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_account"."id" AS "j1_account__id", "j1_account"."label" AS "j1_account__label", "j1_account"."owner_id" AS "j1_account__owner_id"
FROM "transaction"
LEFT JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "transaction"."account_id" = ?;
```

The account's columns come back with each row, and the session hands back the object it already holds. Populations also accumulate on the objects a session holds: a later query's `include()` attaches to instances an earlier query returned:

```python
--8<-- "docs/examples/populated_relations.py:accumulate"
```

`market_run` was fetched with nothing populated, and the two later queries populated its account and its category. Outside a session there is no identity map, so each row of an included query gets its own related instance. [Identity map](../concepts/identity-map.md) covers sessions and shared instances.

The three queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- populated_relations.py:accumulate (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."id" = ?
LIMIT ?;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_account"."id" AS "j1_account__id", "j1_account"."label" AS "j1_account__label", "j1_account"."owner_id" AS "j1_account__owner_id"
FROM "transaction"
LEFT JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE TRUE;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_category"."id" AS "j1_category__id", "j1_category"."name" AS "j1_category__name", "j1_category"."parent_id" AS "j1_category__parent_id"
FROM "transaction"
LEFT JOIN "category" AS "j1_category" ON "transaction"."category_id" = "j1_category"."id"
WHERE TRUE;
```

Neither later query names `market_run`; the session matched its row by primary key.

### Population after a refresh

Every fetch refreshes the instances a session already holds. A refresh keeps a population only while the row's foreign key still points at it; if the foreign key changed, the population is dropped and the relation is awaitable again:

```python
--8<-- "docs/examples/populated_relations.py:refresh-rule"
```

After the row moved to Checking, `from_checking.account` no longer claims Savings; awaiting it fetches Checking.

The four statements are the same on PostgreSQL, apart from the placeholders Ferro writes:

```sql
-- populated_relations.py:refresh-rule (sqlite)
SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on", "j1_account"."id" AS "j1_account__id", "j1_account"."label" AS "j1_account__label", "j1_account"."owner_id" AS "j1_account__owner_id"
FROM "transaction"
LEFT JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "transaction"."id" = ?
LIMIT ?;

UPDATE "transaction"
SET account_id = $1
WHERE id = $2;

SELECT "transaction"."account_id", "transaction"."amount", "transaction"."category_id", "transaction"."cleared_on", "transaction"."id", "transaction"."memo", "transaction"."posted_on"
FROM "transaction"
WHERE "transaction"."id" = ?
LIMIT ?;

SELECT "account"."id", "account"."label", "account"."owner_id"
FROM "account"
WHERE "account"."id" = ?
LIMIT ?;
```

Ferro sends the raw `UPDATE` as written. The last `SELECT` is the await, fetching Checking.

### Unsupported `include()` forms

Ferro refuses these when you build the query, before any SQL:

| Form | Error | Use instead |
| :--- | :--- | :--- |
| A `BackRef` or `ManyToMany` relation | `TypeError` | `await account.transactions.all()`; to filter on the relation, an [existence test](#existence-tests-on-reverse-many-to-many-relations) |
| A string (`include("account")`) or a column (`include(lambda transaction: transaction.account.label)`) | `TypeError` | A lambda that names a relation; each populated step is a whole row |
| `include()` with `select(...)`, in either order | `ValueError` | [A projection that reaches across the relation](#reaching-across-a-relation) |
| `update()` or `delete()` on an included query | `ValueError` | The same query without `include()` |

```python
--8<-- "docs/examples/populated_relations.py:limits"
```

Each refusal names the form to use. `include()` also works on a many-to-many query (`tag.transactions.include(lambda transaction: transaction.account)`), and with `where()`, `order_by()`, `limit()`, `offset()` and `first()`. The [`Query` reference](../api/queries.md#ferro.query.Query.include) lists its parameters.

## Selecting a column subset

A query loads whole rows into whole model instances. When a view only reads two columns of a wide table, pass [`select()`][ferro.query.builder.Query.select] a lambda naming the columns, and the query returns records with only those columns:

```python
--8<-- "docs/examples/partial_selects.py:basic"
```

Each record has a `memo` and an `amount`, read as attributes like a model's. These examples run inside the session from [the first block](#queries).

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:basic (sqlite)
SELECT "transaction"."memo" AS "memo", "transaction"."amount" AS "amount"
FROM "transaction"
WHERE TRUE
ORDER BY "transaction"."id" ASC NULLS LAST
LIMIT ?;
```

Only the two selected columns are in the `SELECT`, each named with `AS`.

### The `Row` result type

A selected query does not return `Transaction` instances. It returns [`Row`][ferro.query.Row] records in a [`Rows`][ferro.query.Rows] list:

```python
--8<-- "docs/examples/partial_selects.py:not-a-model"
```

A `Row` holds the selected columns, in the order you selected them, with the same Python types the model would give them. It has no `save()`, is not refreshed, and never enters the identity map. A model instance always holds a whole row; [Typed query predicates](../concepts/query-typing.md#projected-queries) explains why a selected query returns records instead.

One column needs no tuple:

```python
--8<-- "docs/examples/partial_selects.py:single"
```

Each record holds only the `memo` of an uncleared transaction.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:single (sqlite)
SELECT "transaction"."memo" AS "memo"
FROM "transaction"
WHERE "transaction"."cleared_on" IS NULL
ORDER BY "transaction"."id" ASC NULLS LAST;
```

The statement selects `memo` alone.

### The `Rows` container

`Rows` behaves like a list: index it, slice it, iterate it and take its `len()`. `model_dump()` returns a list of dicts, and `Rows[Row]` works as a FastAPI `response_model`:

```python
--8<-- "docs/examples/partial_selects.py:container"
```

A slice of a `Rows` is a `Rows` too, with the two newest records.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:container (sqlite)
SELECT "transaction"."id" AS "id", "transaction"."memo" AS "memo"
FROM "transaction"
WHERE TRUE
ORDER BY "id" DESC NULLS LAST;
```

Indexing, slicing and `model_dump()` run in Python, over the records this one statement returned.

### Column-name strings

`select()` also takes column names as strings, with the same rules as `order_by()`: columns on the queried model only, the `<relation>_id` columns included, checked when you build the query:

```python
--8<-- "docs/examples/partial_selects.py:strings"
```

The lambda form is the documented one because only a lambda can reach across a relation.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:strings (sqlite)
SELECT "transaction"."id" AS "id", "transaction"."memo" AS "memo"
FROM "transaction"
WHERE TRUE
ORDER BY "id" ASC NULLS LAST
LIMIT ?;
```

Each string became a column of `transaction`, named after itself.

### Reaching across a relation

A selected column can follow a foreign key, at any depth, as in a `where()` lambda. A column reached this way is named after its last step:

```python
--8<-- "docs/examples/partial_selects.py:traversed"
```

`transaction.account.label` becomes the field `label`. Strings never follow relations: `select("account.label")` raises.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:traversed (sqlite)
SELECT "transaction"."memo" AS "memo", "j1_account"."label" AS "label"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE TRUE
ORDER BY "transaction"."id" ASC NULLS LAST
LIMIT ?;
```

The traversal joins `account`, and `AS "label"` names the field after the last step.

### Naming output fields

Return a dict from the lambda to name the record's fields. The keys name the fields, in their order:

```python
--8<-- "docs/examples/partial_selects.py:aliases"
```

The record has the three names the dict gave it. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:aliases (sqlite)
SELECT "transaction"."memo" AS "memo", "j1_account"."label" AS "account", "j2_owner"."email" AS "owner_email"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE TRUE
ORDER BY "transaction"."id" ASC NULLS LAST
LIMIT ?;
```

Each dict key became an `AS` name. The dict form also settles a name clash: `transaction.id` and `transaction.account.id` would both be `id`, and selecting both without names raises `ValueError` that suggests the dict:

```python
--8<-- "docs/examples/partial_selects.py:collision"
```

The message names the dict form as the fix.

### Traversal and `left_join()` in projections

A selected column that follows a relation uses the same [inner join](#join-type-for-traversed-relations) as a `where()` on that path, and shares it. Rows without the relation drop out; `left_join()` keeps them, with `None` in the fields they cannot reach:

```python
--8<-- "docs/examples/partial_selects.py:traversal-narrows"
```

Office coffee drops out of the first query and comes back in the second with `owner_email` set to `None`. A field filled from a missing relation is `None` even when its column is not nullable: the record describes the row you got.

The two queries send these statements; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:traversal-narrows (sqlite)
SELECT "transaction"."memo" AS "memo", "j2_owner"."email" AS "owner_email"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
INNER JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE TRUE;

SELECT "transaction"."memo" AS "memo", "j2_owner"."email" AS "owner_email"
FROM "transaction"
LEFT JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
LEFT JOIN "owner" AS "j2_owner" ON "j1_account"."owner_id" = "j2_owner"."id"
WHERE TRUE
ORDER BY "transaction"."id" ASC NULLS LAST;
```

`left_join()` turned both joins into `LEFT JOIN`s.

### Composing projections

A selected query takes `where()` (traversal included), `order_by()` (even on a column you did not select), `limit()`, `offset()` and `first()`:

```python
--8<-- "docs/examples/partial_selects.py:compose"
```

The Travel rows come back ordered by `amount`, which is not in the records. The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:compose (sqlite)
SELECT "transaction"."id" AS "id", "transaction"."memo" AS "memo"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "j1_account"."label" = ?
ORDER BY "transaction"."amount" ASC NULLS LAST;
```

`ORDER BY` reads `transaction.amount`, a column the `SELECT` does not list. `after()` and `before()` page a selected query too. `after(row)` reads the order keys from the record, so every order key must be selected; otherwise pass a tuple:

```python
--8<-- "docs/examples/partial_selects.py:projected-paging"
```

The first page holds the five Checking rows, so the next starts at Petty cash. The two pages send these statements; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:projected-paging (sqlite)
SELECT "j1_account"."label" AS "account", "transaction"."id" AS "id", "transaction"."memo" AS "memo"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE TRUE
ORDER BY "j1_account"."label" ASC NULLS LAST, "id" ASC NULLS LAST
LIMIT ?;

SELECT "j1_account"."label" AS "account", "transaction"."id" AS "id", "transaction"."memo" AS "memo"
FROM "transaction"
INNER JOIN "account" AS "j1_account" ON "transaction"."account_id" = "j1_account"."id"
WHERE "j1_account"."label" > ? OR "j1_account"."label" IS NULL OR ("j1_account"."label" = ? AND ("transaction"."id" > ? OR "transaction"."id" IS NULL))
ORDER BY "j1_account"."label" ASC NULLS LAST, "id" ASC NULLS LAST
LIMIT ?;
```

The second page's position compares the selected `account` and `id`. `count()` and `exists()` count the same rows as the query without `select()`:

```python
--8<-- "docs/examples/partial_selects.py:count"
```

Both count the three uncleared transactions. The two calls send the same statement; on PostgreSQL only the placeholders differ:

```sql
-- partial_selects.py:count (sqlite)
SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."cleared_on" IS NULL;

SELECT COUNT(*)
FROM "transaction"
WHERE "transaction"."cleared_on" IS NULL;
```

A projection changes no count: both calls send `COUNT(*)` with the query's filter.

### Unsupported projection forms

A misspelled column fails when you build the query, as in `where()`; a column that follows a relation is checked at each step:

```python
--8<-- "docs/examples/partial_selects.py:typo"
```

The message lists the valid columns, as in `where()`. Ferro also refuses these, before any SQL:

| Form | Error | Use instead |
| :--- | :--- | :--- |
| Two fields with the same name | `ValueError` | The dict form, to name them |
| A dict inside a tuple, a tuple or dict as a dict value, or a key that is not a string | `TypeError` | One flat tuple or one flat dict |
| `update()` or `delete()` on a selected query | `ValueError` | The same query without `select(...)` |
| A second `select(...)` on a selected query | `ValueError` | One `select()` naming every field |
| Strings and a lambda in one `select()` | `TypeError` | One form |
| `include()` with `select(...)`, in either order | `ValueError` | A selected column that reaches across the relation |

Your type checker sees the change too: after `select(...)`, `all()` is `Rows[Row]` and `first()` is `Row | None`, so passing a record where a `Transaction` is expected fails the check.

Aggregates build on projection: `select(lambda transaction: {"total": transaction.amount.sum()})` sums a column, and mixing aggregates with plain columns groups the rows. [Aggregations & grouped queries](aggregations.md) covers them. The [`select()` reference](../api/queries.md#select-overloads) lists every selector form.

## Not yet supported

These query features are not available yet; the [Roadmap](../roadmap.md) tracks them:

- `having()`, to filter on an aggregate. `where()` refuses an aggregate and names `having()`.
- Populating a reverse or many-to-many relation with its rows. `include()` covers foreign keys; until then, await the relation (`await account.transactions.all()`). Filtering on one is the [existence test](#existence-tests-on-reverse-many-to-many-relations).
- Referring to the outer row inside an existence test's inner lambda, for example comparing a split line's amount to its transaction's amount. Ferro refuses it when you build the query.
- Case-insensitive `ilike()`.

## See also

- [Query reference](../api/queries.md): `Query`, `ProjectedQuery`, `Row`, `Rows` and every method on this page
- [Aggregations & grouped queries](aggregations.md): `count`, `sum`, `avg`, `min`, `max` and grouping
- [Mutations](mutations.md): creating, updating and deleting rows
- [Relationships](relationships.md): declaring foreign keys and reverse relations
- [Typed query predicates](../concepts/query-typing.md): how lambda predicates are type-checked
- [Raw SQL](raw-sql.md): for queries the query builder cannot express
- [Inspecting SQL](inspecting-sql.md): `.sql()` renders any terminal's statement without running it
- [Pagination](../howto/pagination.md): offset and keyset paging in an API
