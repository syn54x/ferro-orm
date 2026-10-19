# Queries

This guide shows how to read rows with Ferro: filter them with a lambda, order and page them, reach across relations, populate related rows and select only the columns you need.

Here is a whole program. It connects, opens a session, and asks for every transaction of 100 or more going out, largest first:

```python
--8<-- "docs/examples/predicates.py:first-query"
```

It prints:

```text
Rent -1450
To savings -500
```

`where()` takes a lambda whose parameter stands for one row, and `order_by()` takes a lambda naming the column to sort by. Nothing reaches the database until you await `all()`. The values in a query are always sent as parameters, never pasted into the SQL. Every query on this page runs inside a session like this one; see [Sessions](connections.md#sessions) for why and how to open one.

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

Ferro checks every column name when you build the query, before anything reaches the database. A misspelled column raises `AttributeError` with the closest match and every column and relation you can use:

```python
--8<-- "docs/examples/predicates.py:typo"
```

The list includes the `account_id` and `category_id` columns that each foreign key adds, so you can filter on them directly.

## Predicate style

A predicate is the lambda you pass to `where()`. It is the only form `where()` accepts: anything that is not callable raises `TypeError` naming the lambda form. Name the parameter after the model in lowercase singular, so the predicate reads as a sentence: `lambda transaction: transaction.amount < 0`.

The lambda's return value is type-checked as well: `where()` expects a callable that returns a condition, so `lambda transaction: True` fails your type checker. [Typed query predicates](../concepts/query-typing.md) explains what the checker sees and where checking stops.

!!! note "Changed in 0.14"
    The operator style (`Transaction.where(Transaction.amount < 0)`) and the `col()` helper were removed in 0.14. [Upgrading to 0.14](../howto/upgrade-guide.md#predicates-operator-style-and-col-are-gone) shows how to rewrite them as lambdas.

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

## Combining conditions

Join conditions with `&` (and) and `|` (or), or chain several `where()` calls, which are joined with and:

```python
--8<-- "docs/examples/predicates.py:combining"
```

Only the two Lyon rows are uncleared and above -90; Weekly shop is uncleared but spent -95.00. The chained query keeps the debits posted on or after 10 March.

!!! warning "Parenthesize each side of `&` and `|`"
    In Python, `&` and `|` bind tighter than comparisons, so `transaction.amount < 0 & transaction.cleared_on == None` parses as `transaction.amount < (0 & transaction.cleared_on) == None`. Wrap each condition in parentheses: `(transaction.amount < 0) & (transaction.cleared_on == None)`.

Python's own `and`, `or` and `not` cannot combine conditions: Python asks each side whether it is true, and a condition has no answer until the database runs it. Ferro raises `TypeError` and names `&`, `|` and `~` instead.

## Negating conditions

Put `~` in front of any condition to negate it: a comparison, `.like()`, `.in_()` or a whole group in parentheses. There is no `not_like()` or `not_in()`; `~` covers them all:

```python
--8<-- "docs/examples/predicates.py:negation"
```

The four debits below -50 come back, minus Train to Lyon, whose memo matches `"%Lyon"`. Each `~` becomes SQL `NOT (...)` around the condition it wraps, so negated conditions combine with `&` and `|` like any other, and `~~` cancels out.

### Negation and NULL values

SQL compares `NULL` to nothing: `cleared_on < '2026-03-05'` is neither true nor false for a row whose `cleared_on` is `NULL`, and `WHERE` keeps only the rows where the condition is true. `NOT` of an unknown is still unknown, so a negated comparison drops the `NULL` rows too:

```python
--8<-- "docs/examples/predicates.py:three-valued"
```

Two transactions cleared before 5 March and four cleared later, which leaves out the three uncleared ones from both lists. `!=` behaves the same way: `transaction.cleared_on != date(2026, 3, 2)` also leaves out the uncleared rows.

To keep the `NULL` rows, ask for them with an `== None` branch:

```python
--8<-- "docs/examples/predicates.py:keep-nulls"
```

The four later rows and the three uncleared rows come back.

## Fetching by primary key

[`Model.get(pk)`][ferro.models.Model.get] loads one row by its primary key and returns the model, never `None`. A missing row raises `ModelDoesNotExist`, a `LookupError` that carries `.model` and `.pk`, so an HTTP layer can turn it into a 404. When a missing row is normal, use [`get_or_none()`][ferro.models.Model.get_or_none]:

```python
--8<-- "docs/examples/predicates.py:get"
```

`get(2)` returns Rent, `get(99)` raises, and `get_or_none(99)` returns `None`. Both methods also exist on `Model.using("name")` for [named connections](connections.md#named-connections).

## Ordering, limit & offset

[`order_by()`][ferro.query.builder.Query.order_by] takes a lambda naming the column, and an optional direction (`"asc"`, the default, or `"desc"`). `limit()` and `offset()` cut a slice out of the ordered rows:

```python
--8<-- "docs/examples/predicates.py:ordering-slicing"
```

The first three are the largest debits; `offset(3)` skips them and returns the next three. Chain `order_by()` again to break ties on a second column.

`order_by()` also accepts a column name as a string (`order_by("amount", "desc")`). The lambda is the documented form because only a lambda can [order by a related column](#ordering-by-a-related-column).

`NULL` values sort last on every database unless you say otherwise. Pass `nulls="first"` to put them first, or `nulls="native"` for each database's own default, which differs between PostgreSQL and SQLite for `"desc"`:

```python
--8<-- "docs/examples/predicates.py:nulls-ordering"
```

The three uncleared transactions lead, then the latest cleared one, Office coffee.

## Paging with `after()` and `before()`

[`after()`][ferro.query.builder.Query.after] starts a page right after a row you already have, in the order the query declares. Pass it the last row of the page you showed:

```python
--8<-- "docs/examples/predicates.py:after-paging"
```

The second page starts at To savings, the row after Market run. Unlike `offset()`, it does not skip or repeat rows when rows are added or removed between the two requests.

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

[`before()`][ferro.query.builder.Query.before] pages backwards. With a limit it returns the page immediately before the bound, still in the declared order; without one it returns every earlier row:

```python
--8<-- "docs/examples/predicates.py:before-paging"
```

`before(...).limit(2)` returns the two rows before To savings, and `before(...)` alone returns all three earlier rows. `first()` returns the row immediately before the bound, while `all()[0]` returns the earliest row, so use `first()` for "the previous row".

A `None` order-key value is how paging continues through `NULL`s. Ordered by `cleared_on` descending, the uncleared rows sort last; to continue after Train to Lyon, pass `None` for its `cleared_on`:

```python
--8<-- "docs/examples/predicates.py:after-null-paging"
```

The two uncleared rows after Train to Lyon come back. The same methods page a query ordered by a [related column](#ordering-by-a-related-column) or a [projected query](#composing-projections). [Pagination](../howto/pagination.md) shows how to wire this into an API.

## Executing queries

A query runs when you await one of its terminals: [`all()`][ferro.query.builder.Query.all] returns a list of instances, [`first()`][ferro.query.builder.Query.first] returns the first row or `None`, [`count()`][ferro.query.builder.Query.count] returns how many rows match, and [`exists()`][ferro.query.builder.Query.exists] returns whether any does:

```python
--8<-- "docs/examples/predicates.py:terminals"
```

`count()` and `exists()` hydrate no instances. `Model.all()` is short for `Model.select().all()`.

!!! tip "Prefer `exists()` to `count() > 0`"
    `exists()` lets the database stop at the first match instead of counting every row.

## Query immutability

`where()`, `order_by()`, `limit()` and the other chain methods each return a new query and leave the one you called them on unchanged. You can keep a base query and branch from it:

```python
--8<-- "docs/examples/predicates.py:immutability"
```

The three queries built from `debits` are independent, and `debits` still counts all seven debits afterwards.

## Querying across relationships

A `where()` or `order_by()` lambda can follow a foreign key to the related model's columns. Write the relation, then the column:

```python
--8<-- "docs/examples/traversal.py:basic"
```

`transaction.account.label` reads the label of each transaction's account, and From checking is the one transaction on Savings. You do not write the join: following `transaction.account` is the join, and the query still returns `Transaction` instances. These examples run inside the session from [the first block](#queries).

### Traversing at any depth

Each step resolves against the next model, so you can chain as far as the foreign keys go. `transaction.account.owner.email` walks from `Transaction` to `Account` to `Owner` in one statement:

```python
--8<-- "docs/examples/traversal.py:multi-hop"
```

Grace Hopper owns Travel, so both Travel transactions come back. Ferro checks each step against its own model when you build the query, and a misspelled step names that model and the closest match:

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

The join applies to the whole query, not to one side of an `|`:

```python
--8<-- "docs/examples/traversal.py:inner-or"
```

Office coffee matches the second branch, but the join on `account.owner` has already dropped it.

`count()` counts the same rows. Following a foreign key reaches one related row per row, so the join never multiplies results:

```python
--8<-- "docs/examples/traversal.py:count"
```

Ada's two accounts hold six transactions.

### Ordering by a related column

`order_by()` follows relations the same way:

```python
--8<-- "docs/examples/traversal.py:order-by"
```

Accounts sort by label, descending: Travel, Savings, Petty cash, then Checking after the limit. Sorting is an inner join too, so ordering by a column that some rows cannot reach drops those rows:

```python
--8<-- "docs/examples/traversal.py:order-by-narrows"
```

Eight of the nine transactions come back. `after()` and `before()` take a tuple of the related values, so you can page without loading the relation:

```python
--8<-- "docs/examples/traversal.py:traversed-paging"
```

The page starts after Office coffee, the last Petty cash row. `position_of()` and `after(row)` read the related value off the instance, so they need the relation [populated with `include()`](#populating-relations-with-include); a tuple does not.

### One join per relation path

The path decides the join. Use `transaction.account` in two `where()` calls, in both sides of an `|`, or in `where()` and `order_by()`, and the query still joins `account` once. You never name or manage an alias:

```python
--8<-- "docs/examples/traversal.py:composed"
```

The traversal filter, a plain column filter, an ordering and a limit run as one statement, and Ada's two largest debits come back.

### Comparing to a related instance

Each foreign key adds a `<relation>_id` column, and filtering on it needs no join:

```python
--8<-- "docs/examples/traversal.py:shadow-column"
```

From checking is the one transaction whose `account_id` is Savings' `id`. Comparing the relation to a saved instance means the same thing, with the same join-free query:

```python
--8<-- "docs/examples/traversal.py:instance-eq"
```

`transaction.account == savings` is `transaction.account_id == savings.id`; `!=` returns the other eight. At the end of a longer path, the comparison uses the last step's foreign key column:

```python
--8<-- "docs/examples/traversal.py:deep-instance-eq"
```

`transaction.account.owner == grace` joins `account` and compares its `owner_id`. An instance that has not been saved has no primary key to compare, so Ferro refuses it when you build the query:

```python
--8<-- "docs/examples/traversal.py:unpersisted"
```

Save the account first, then compare.

### Testing for a relation

A bare `join()` with no condition keeps the rows whose relation exists:

```python
--8<-- "docs/examples/traversal.py:existence"
```

Petty cash has no owner and is left out. To ask the opposite, compare the relation to `None`: `== None` and `!= None` become `IS NULL` and `IS NOT NULL` on the foreign key column, with no join:

```python
--8<-- "docs/examples/traversal.py:is-null"
```

Both forms ask about a foreign key on the model you query. To ask whether a row has related rows on another table, such as "has at least one split line", use an [existence test](#existence-tests-on-reverse-many-to-many-relations).

### Keeping rows that have no relation

[`left_join()`][ferro.query.builder.Query.left_join] keeps the rows whose relation is missing. It makes every step of its path a left join, and a `where()` or `order_by()` on the same path uses it too:

```python
--8<-- "docs/examples/traversal.py:left-join"
```

All four accounts come back, Petty cash last because its owner name is `NULL`. Without `left_join()`, ordering by `account.owner.name` would drop it. Declaring `join()` and `left_join()` on the same path raises `ValueError`.

### Two foreign keys to the same table

Two foreign keys to one model are two paths, so they are two joins:

```python
--8<-- "docs/examples/traversal.py:two-fk-query"
```

`transfer.outflow` and `transfer.inflow` both point at `Transaction`, and each reaches its own account. The one transfer runs from transaction 4 on Checking to transaction 5 on Savings.

### Self-referential traversal

A foreign key to the same model traverses like any other:

```python
--8<-- "docs/examples/traversal.py:self-fk-query"
```

Groceries and Dining have Food as their parent.

### Traversing from a many-to-many

A many-to-many relation on an instance is a query, and its lambda can follow foreign keys on the other model:

```python
--8<-- "docs/examples/traversal.py:m2m-query"
```

Of the two reimbursable transactions, only Dinner in Lyon is in Dining.

### Filtering reverse relations

A reverse relation on an instance (`account.transactions`) is a query too. Filter, order and count it before you await it:

```python
--8<-- "docs/examples/traversal.py:reverse-filter"
```

Checking holds five transactions, and its two largest debits are Rent and To savings. That query starts from one account. To filter accounts by what their transactions hold, use an [existence test](#existence-tests-on-reverse-many-to-many-relations).

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

### Bare tests and negation

A bare `.exists()` asks whether any related row is there, and `~` asks whether none is:

```python
--8<-- "docs/examples/existence_tests.py:bare"
```

`transfer_out` and `transfer_in` are the one-to-one reverse relations of `Transfer.outflow` and `Transfer.inflow`. The two halves of the transfer come back, and the other seven rows are in no transfer.

### Scoping with an inner predicate

Pass `.exists()` a lambda to choose which related rows count. The inner lambda is a full predicate over the related model, with every operator, `&`, `|`, `~`, traversal and further existence tests:

```python
--8<-- "docs/examples/existence_tests.py:scoped"
```

Market run has Groceries itself, and Weekly shop has it on a split line. The result has one row per transaction, so ordering and paging work as on any query:

```python
--8<-- "docs/examples/existence_tests.py:composes"
```

Weekly shop, at -95.00, is the larger of the two.

### Grouping conditions

Two conditions on the same relation can ask two different questions: is there one related row that matches both, or a row for each? Where you put the conditions decides which:

```python
--8<-- "docs/examples/existence_tests.py:grouping"
```

No single split line is both Dining and below -50, but Weekly shop has a Dining line and a different line below -50. Writing `transaction.lines.category_id` directly raises an error, because nothing on the page would say which of the two questions it asks.

### Traversal inside the test, and nesting

The inner lambda can follow foreign keys on the related model. Those joins run inside the `EXISTS` subquery, with the same [inner-join rule](#join-type-for-traversed-relations), so they filter the related rows and never the outer ones:

```python
--8<-- "docs/examples/existence_tests.py:traversal-inside"
```

Weekly shop's lines are in Groceries and Dining, both under Food. Existence tests nest to any depth:

```python
--8<-- "docs/examples/existence_tests.py:nested"
```

Only Ada has an account holding a split transaction.

### Many-to-many

A many-to-many relation reads the same from either side. The test goes through the link table, and the inner lambda scopes over the other model:

```python
--8<-- "docs/examples/existence_tests.py:m2m"
```

Rent carries `tax`, `reimbursable` is on the uncleared Lyon rows, and six transactions have no tag.

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

```text
Weekly shop | Checking
Office coffee | Petty cash
Dinner in Lyon | Travel
```

The query still returns `list[Transaction]`, and `transaction.account` is now the `Account` instance itself, as the field's annotation (`account: Account`) says. Reading it needs no `await` and runs no query. These examples run inside the session from [the first block](#queries).

### The population contract

A relation on an instance is either populated or not:

- **Populated**, because a query included it: the attribute is the related instance. A nullable foreign key with no row populates as `None`.
- **Not populated**: the attribute is awaitable and runs its own query:

```python
--8<-- "docs/examples/populated_relations.py:awaitable"
```

`await transaction.account` fetched the Travel account with a second query.

!!! warning "Awaiting a populated relation raises"
    On a populated instance, `await transaction.account` raises `TypeError: 'Account' object can't be awaited`, because the attribute is the instance, not a coroutine. Decide at the query: if you will read the relation, include it.

There is no separate "loaded" model type: an included query returns `list[Transaction]`, and a populated relation is an ordinary instance. [Typed query predicates](../concepts/query-typing.md#included-queries) explains why.

### Row membership under `include()`

`include()` never changes which rows a query returns. A row whose relation is missing stays, with the relation populated as `None`:

```python
--8<-- "docs/examples/populated_relations.py:membership"
```

All nine transactions come back, and Salary's `category` is `None`. A relation only `include()` uses becomes a left join; a relation a `where()` or `order_by()` also follows keeps that clause's inner join, so the filter still decides membership:

```python
--8<-- "docs/examples/populated_relations.py:interplay"
```

The `where()` keeps the two Travel rows, and `include()` attaches their account. `count()` and `exists()` measure the same rows with or without `include()`:

```python
--8<-- "docs/examples/populated_relations.py:count"
```

Both count the seven debits.

### Multi-hop paths

Including a path populates every step of it. A `NULL` along the way ends the chain with `None`, and the row stays:

```python
--8<-- "docs/examples/populated_relations.py:multi-hop"
```

Salary's account and its owner are both loaded. Office coffee's account is Petty cash, whose owner is `None`.

Includes add up, in any order, and repeating one changes nothing: `.include(lambda transaction: transaction.account)` followed by `.include(lambda transaction: transaction.account.owner)` is the same query as the second one alone.

### Identity of populated instances

In a session, each row is one object. A populated account is the same object a direct fetch of that account returns, shared by every row that points at it:

```python
--8<-- "docs/examples/populated_relations.py:identity"
```

Both Travel transactions point at the `travel` object fetched first. Populations also accumulate on the objects a session holds: a later query's `include()` attaches to instances an earlier query returned:

```python
--8<-- "docs/examples/populated_relations.py:accumulate"
```

`market_run` was fetched with nothing populated, and the two later queries populated its account and its category. Outside a session there is no identity map, so each row of an included query gets its own related instance. [Identity map](../concepts/identity-map.md) covers sessions and shared instances.

### Population after a refresh

Every fetch refreshes the instances a session already holds. A refresh keeps a population only while the row's foreign key still points at it; if the foreign key changed, the population is dropped and the relation is awaitable again:

```python
--8<-- "docs/examples/populated_relations.py:refresh-rule"
```

After the row moved to Checking, `from_checking.account` no longer claims Savings; awaiting it fetches Checking.

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

### The `Rows` container

`Rows` behaves like a list: index it, slice it, iterate it and take its `len()`. `model_dump()` returns a list of dicts, and `Rows[Row]` works as a FastAPI `response_model`:

```python
--8<-- "docs/examples/partial_selects.py:container"
```

A slice of a `Rows` is a `Rows` too, with the two newest records.

### Column-name strings

`select()` also takes column names as strings, with the same rules as `order_by()`: columns on the queried model only, the `<relation>_id` columns included, checked when you build the query:

```python
--8<-- "docs/examples/partial_selects.py:strings"
```

The lambda form is the documented one because only a lambda can reach across a relation.

### Reaching across a relation

A selected column can follow a foreign key, at any depth, as in a `where()` lambda. A column reached this way is named after its last step:

```python
--8<-- "docs/examples/partial_selects.py:traversed"
```

`transaction.account.label` becomes the field `label`. Strings never follow relations: `select("account.label")` raises.

### Naming output fields

Return a dict from the lambda to name the record's fields. The keys name the fields, in their order:

```python
--8<-- "docs/examples/partial_selects.py:aliases"
```

The record has the three names the dict gave it. The dict form also settles a name clash: `transaction.id` and `transaction.account.id` would both be `id`, and selecting both without names raises `ValueError` that suggests the dict:

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

### Composing projections

A selected query takes `where()` (traversal included), `order_by()` (even on a column you did not select), `limit()`, `offset()` and `first()`:

```python
--8<-- "docs/examples/partial_selects.py:compose"
```

The Travel rows come back ordered by `amount`, which is not in the records. `after()` and `before()` page a selected query too. `after(row)` reads the order keys from the record, so every order key must be selected; otherwise pass a tuple:

```python
--8<-- "docs/examples/partial_selects.py:projected-paging"
```

The first page holds the five Checking rows, so the next starts at Petty cash. `count()` and `exists()` count the same rows as the query without `select()`:

```python
--8<-- "docs/examples/partial_selects.py:count"
```

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
- [Pagination](../howto/pagination.md): offset and keyset paging in an API
