# Queries

Ferro provides a fluent, type-safe API for building queries in Python and executing them on the Rust engine. All values are parameterized — user input is never concatenated into SQL.

The examples on this page use this model:

=== "Assignment"

    ```python
    --8<-- "docs/examples/predicates.py:setup"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/predicates_annotated.py:setup"
    ```

## Queries Are Immutable

Every chain call — `.where()`, `.order_by()`, `.limit()`, `.offset()` — returns a **new** `Query`. None of them mutate the query they were called on, so a partially-built query is safe to keep around and reuse as the base for several different follow-ups:

```python
base = User.where(lambda user: user.role == "admin")

page1 = base.limit(10)             # first 10 admins
page2 = base.limit(10).offset(10)  # next 10 admins
```

`base` still matches every admin with no `limit()` applied — building `page1` and `page2` from it doesn't change it, and `page1` and `page2` don't affect each other either. This is what makes patterns like "build a filtered base query, then branch into a count and a page of results" safe:

```python
active = User.where(lambda user: user.archived == False)  # noqa: E712

total = await active.count()
first_page = await active.order_by(lambda user: user.id).limit(20).all()
```

`active` is never consumed or altered by either await — you can keep branching off it as many times as you like.

## Fetching by Primary Key

`Model.get(pk)` loads exactly one row and returns your model type — not `YourModel | None`. If no row exists it raises `ModelDoesNotExist`, a `LookupError` subclass carrying `.model` and `.pk` (handy for HTTP 404s and structured logging). When a missing row is a normal outcome, use `Model.get_or_none(pk)` instead:

```python
from ferro import ModelDoesNotExist

user = await User.get(42)  # User — raises if missing

try:
    user = await User.get(client_supplied_id)
except ModelDoesNotExist:
    ...  # e.g. return 404 from your HTTP layer

maybe = await User.get_or_none(999)  # User | None — never raises for "not found"
```

Both methods also exist on `Model.using("name")` for [named connections](connections.md#named-connections).

## Filtering with where()

`Model.where(...)` starts a chainable query; terminals like `.all()` execute it. Predicates are written as lambdas — the parameter (`t` by convention) is a query proxy whose attributes stand in for your model's columns:

```python
--8<-- "docs/examples/predicates.py:filtering"
```

`Model.select()` starts an unfiltered query — useful when you only want ordering, slicing, or a count.

## Predicate Style

`where()` accepts a lambda predicate — a callable that receives a query proxy and returns a comparison. The proxy's attributes are validated against your model's columns at build time (a misspelled column raises `AttributeError` naming the closest match, before any query reaches the database):

```python
--8<-- "docs/examples/predicates.py:lambda-style"
```

Typo a column name and you find out immediately, not after the query round-trips to the database:

```text
>>> await User.where(lambda user: user.naem == "alice").all()
AttributeError: User has no queryable column 'naem'. Did you mean 'name'? Valid columns: age, archived, id, name, role.
```

The valid-columns list includes shadow `{fk}_id` foreign-key columns (see [Querying Across Relationships](#querying-across-relationships)), so the error is always a complete picture of what you can filter on.

Lambda predicates keep the call site fully type-checked: the proxy's attributes are real `FieldProxy` objects in the type checker's eyes, not your Pydantic annotations. See [Typed Query Predicates](../concepts/query-typing.md) for the full reasoning.

## Operators

| Python | SQL | Example |
| :--- | :--- | :--- |
| `==` | `=` | `lambda user: user.role == "admin"` |
| `!=` | `!=` | `lambda user: user.role != "admin"` |
| `>` | `>` | `lambda user: user.age > 18` |
| `>=` | `>=` | `lambda user: user.age >= 21` |
| `<` | `<` | `lambda user: user.age < 100` |
| `<=` | `<=` | `lambda user: user.age <= 65` |
| `.like(pattern)` | `LIKE` | `lambda user: user.name.like("a%")` |
| `.in_(values)` | `IN` | `lambda user: user.role.in_(["admin", "moderator"])` |
| `== None` | `IS NULL` | `lambda user: user.deleted_at == None` |
| `!= None` | `IS NOT NULL` | `lambda user: user.deleted_at != None` |
| `~` | `NOT` | `lambda user: ~user.role.in_(["admin", "moderator"])` |
| `.exists(...)` | `EXISTS (SELECT 1 …)` | `lambda user: user.posts.exists(lambda post: post.published == True)` — reverse/M2M relations only; see [Existence Tests](#existence-tests-on-reverse-many-to-many-relations) |

```python
--8<-- "docs/examples/predicates.py:operators"
```

## Combining Conditions

Combine predicates with `&` (AND) and `|` (OR), or chain multiple `.where()` calls (which AND together):

```python
--8<-- "docs/examples/predicates.py:combining"
```

!!! warning "Always parenthesize `&` and `|` operands"
    Python's `&` and `|` bind tighter than comparison operators, so `user.age < 18 | user.archived == True` parses as `user.age < (18 | user.archived) == True` — not what you meant. Wrap each condition in parentheses: `(user.age < 18) | (user.archived == True)`.

## Negating Conditions

Prefix `~` negates **any** predicate — a comparison, `.in_()`, `.like()`, or a whole `&`/`|` group. It is the one negation rule in Ferro: there are no per-operator negative forms (no `not_in()`, no `not_like()`), because `~` already covers every predicate the same way:

```python
--8<-- "docs/examples/predicates.py:negation"
```

Each `~` renders as a faithful SQL `NOT (...)` over the condition it wraps. The last example above ships to the database as:

```sql
SELECT ... FROM user WHERE NOT (user.age < 18 OR user.archived = TRUE)
```

Negated conditions compose exactly like un-negated ones — mix them into `&`/`|` trees at any depth, chain `.order_by()`/`.limit()` after them, and double negation (`~~p`) means what it says. Adding a `~` never restructures your query.

### Negation and NULL values

SQL comparisons follow **three-valued logic**: a comparison against `NULL` is neither true nor false but *unknown*, and a `WHERE` clause only keeps rows whose condition is **true**. `NOT` maps unknown to unknown — so a negated comparison still excludes rows where the column is `NULL`. `~` is SQL's `NOT`, not Python's set complement.

Concretely, with a nullable column:

=== "Assignment"

    ```python
    --8<-- "docs/examples/predicates.py:nullable-model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/predicates_annotated.py:nullable-model"
    ```

`~(invoice.amount > 100)` renders as:

```sql
SELECT ... FROM invoice WHERE NOT (invoice.amount > 100)
```

For a row where `amount` is `NULL`, `amount > 100` is unknown, `NOT unknown` is still unknown, and the row is excluded — from the negated query *and* from the original one. This matches the `!=` operator you already use (`invoice.amount != 100` excludes `NULL` rows too); `~` just makes the rule visible on every predicate. When you want the `NULL` rows kept, say so explicitly with an `IS NULL` branch:

```python
--8<-- "docs/examples/predicates.py:three-valued"
```

## Ordering, Limit & Offset

Sort with `.order_by(field, direction, *, nulls=...)` (direction defaults to ascending; pass `"desc"` to reverse) and slice with `.limit()` / `.offset()`. Omitted `nulls=` means `NULL`s sort last on every backend; pass `nulls="first"` to lead with `NULL`s, or `nulls="native"` for each dialect's default placement. `field` is a lambda naming the column (`order_by(lambda u: u.created_at, "desc")`, matching the `where()` predicate style) or a column-name string (`order_by("created_at", "desc")`). Both forms are validated against the model's queryable columns at build time.

A pinned-first list is the usual reason to care — put unpinned cards (`pinned_at IS NULL`) after pinned ones, then break ties by recency:

=== "Assignment"

    ```python
    --8<-- "docs/examples/predicates.py:card-model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/predicates_annotated.py:card-model"
    ```

```python
--8<-- "docs/examples/predicates.py:nulls-ordering"
```

```sql
ORDER BY pinned_at DESC NULLS LAST, updated_at DESC, id DESC
```

Omitting `nulls=` on a nullable sort key means `NULLS LAST` on every backend. Pass `nulls="first"` to lead with `NULL`s, or `nulls="native"` when you want each dialect's default (PostgreSQL and SQLite disagree on `DESC`).

```python
--8<-- "docs/examples/predicates.py:ordering-slicing"
```

Chain `.order_by()` multiple times for multi-column sorts.

To page forward from a known row, pass that row's place in the declared order to `.after()`. The bound is exclusive and the order keys must include the primary key. `None` is legal in every non-PK slot — that is how a pinned-first list continues through unpinned rows:

```python
--8<-- "docs/examples/predicates.py:after-paging"
```

`after(row)` is the same as `after(position_of(row))`. `after()` cannot be combined with `offset()` — a query has one start. For the pinned-first shape (`order_by(pinned_at, "desc")`, omitted `nulls=` → last), `after((None, id))` continues through the remaining unpinned rows:

```python
--8<-- "docs/examples/predicates.py:after-null-paging"
```

To page backward, `.before(position)` is the other start. With a limit it is the **adjacent previous page**, still yielded in the declared order. Without a limit it is every earlier row in declared order — a prefix, not a page. The bound is exclusive. `after` and `before` cannot share a query.

```python
--8<-- "docs/examples/predicates.py:before-paging"
```

On unbounded `before()`, `first()` and `all()[0]` disagree: `first()` is `limit(1)` (the adjacent previous row) and `all()[0]` is the head of the prefix (the earliest earlier row). That is accepted.

For robust pagination patterns, see [Pagination](../howto/pagination.md).

## Executing Queries

Queries are lazy — nothing hits the database until you await a terminal:

```python
--8<-- "docs/examples/predicates.py:terminals"
```

| Terminal | Returns | Semantics |
| :--- | :--- | :--- |
| `.all()` | `list[Model]` | All matching rows, hydrated to instances. |
| `.first()` | `Model \| None` | First matching row, or `None` if there are no matches. |
| `.count()` | `int` | `COUNT(*)` of matching rows — no instances hydrated. |
| `.exists()` | `bool` | `True` if at least one row matches; stops at the first match. |

!!! tip "Prefer `.exists()` over `.count() > 0`"
    `.exists()` lets the database stop at the first match instead of counting every row.

`Model.all()` is shorthand for `Model.select().all()`.

On a **projected** query (`select(lambda t: (t.id, t.amount))`), `.all()` returns `Rows[Row]` and `.first()` returns `Row | None` — records, not model instances; `count()` and `exists()` are unchanged on a plain projection (on an aggregate projection they raise with guidance). See [Selecting a Column Subset](#selecting-a-column-subset) and [Aggregations & Grouped Queries](aggregations.md).

## Querying Across Relationships

A `where()` or `order_by()` lambda can reach *through* a `ForeignKey` to a column on the related model. Write the relation field, then keep going:

```python
ledger_a = await Ledger.get(1)

rows = await Transaction.where(
    lambda transaction: transaction.account.ledger_id == ledger_a.id
).all()
```

That is one SQL statement — a `SELECT` over `transactions` with a join to `accounts` — and it returns the transactions whose account belongs to ledger A. You never write the join; the relation you traverse (`transaction.account`) *is* the join.

The examples below use this schema — a `Transaction` points at an `Account`, and an `Account` points at both a `Ledger` and an `Owner`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/traversal.py:schema"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/traversal_annotated.py:schema"
    ```

### Traversing at any depth

Each hop resolves against the related model, so you can chain as many as the schema allows. `transaction.account.owner.email` walks `Transaction → Account → Owner` in a single statement with two joins:

```python
--8<-- "docs/examples/traversal.py:multi-hop"
```

Every hop is validated when you build the query — before anything reaches the database. A misspelled column or relation raises `AttributeError` naming the model at that hop and the closest match, and the suggestion pool spans both columns *and* relations:

```text
>>> Transaction.where(lambda transaction: transaction.accont.ledger_id == 1)
AttributeError: Transaction has no queryable column 'accont'. Did you mean 'account'? Valid columns: account_id, amount, id. Valid relations: account.

>>> Transaction.where(lambda transaction: transaction.account.owner.emial == "x")
AttributeError: Owner has no queryable column 'emial'. Did you mean 'email'? Valid columns: email, id.
```

### Every traversed hop is an INNER join

Traversal always renders an **INNER** join, at every hop, no matter whether the foreign key is nullable (ADR-0006). To see what that means for nullable relations, the narrowing examples in this and the following sections add a `Note` model whose `account` relation is **optional** — a note may or may not be attached to an account:

=== "Assignment"

    ```python
    --8<-- "docs/examples/traversal.py:note-model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/traversal_annotated.py:note-model"
    ```

The practical consequence of INNER-everywhere: **traversing a relation narrows the result to rows where that relation exists**. A `Note` whose `account` FK is `NULL` simply does not appear in a query that filters through `note.account`:

```python
--8<-- "docs/examples/traversal.py:inner-narrows"
```

This is deliberate and stable. A hop's nullability never changes the join type, so making a foreign key nullable later never silently rewrites the meaning of an existing query, and an N-hop path is trivial to reason about — no hop poisons the ones after it. Keeping the relation-less rows is an explicit opt-in (`left_join`, below).

The narrowing is query-wide, not per-clause: the join is rendered once for the whole statement, so a traversal branch inside an `|` still narrows the entire result. `(note.account.ledger_id == 1) | (note.body == "orphan")` drops every relation-less note — the INNER `account` join removes it before the `OR` is ever evaluated, so the `body == "orphan"` branch can never rescue it. Reach for `left_join` when a traversal branch of an `|` must keep relation-less rows.

`count()` and the other terminals see exactly this narrowed set — a many-to-one join never multiplies root rows, so `.count()` equals the number of matching transactions, not the number of joined pairs:

```python
--8<-- "docs/examples/traversal.py:count"
```

### Ordering by a related column

`order_by()` traverses the same way, to any depth, and shares its joins with `where()`:

```python
--8<-- "docs/examples/traversal.py:order-by"
```

Because the sort join is INNER too, ordering by a related column drops relation-less rows — the same narrowing as `where()`. (Use `left_join` to keep them; see below.)

### One join per relation path

The relation path is the join's identity. Reference the same path in two `where()` calls, in an `&`/`|` tree, or across `where()` and `order_by()`, and it renders as **one** join. Distinct paths — even to the same table — render as distinct joins. There is no alias to name and none to manage; the path does that job.

That is what lets a traversal filter compose cleanly with plain root-column clauses — the motivating query pairs one `account` join with a root-column filter, ordering, and a limit, all in a single statement:

```python
--8<-- "docs/examples/traversal.py:pinch"
```

### Comparing to a related instance

Every `ForeignKey` also exposes a shadow `{fk}_id` column, so you can filter by a related row's primary key directly — no join at all:

```python
posts = await Post.where(lambda post: post.author_id == user.id).all()
```

Comparing the relation itself to a **persisted** instance is sugar for exactly that shadow-column check — still join-free:

```python
--8<-- "docs/examples/traversal.py:instance-eq"
```

Deep instance equality compares the shadow column of the *last* hop under the prefix join: `transaction.account.owner == some_owner` filters `owner_id` on the joined `accounts` row.

```python
--8<-- "docs/examples/traversal.py:deep-instance-eq"
```

Comparing an **unpersisted** instance is a build-time error naming the model (`cannot compare relation 'account' to an unpersisted Account instance …`) — there is no primary key to match on yet.

### Testing for existence or absence

A bare `.join()` with no predicate is a meaningful **existence filter** on a nullable relation — it narrows to rows where the relation is present (the same INNER narrowing traversal gives you, expressed directly):

```python
--8<-- "docs/examples/traversal.py:existence"
```

For the opposite question — "has *no* related row" — compare the relation to `None`. `relation == None` / `!= None` lower to `IS NULL` / `IS NOT NULL` on the shadow column, join-free, so "has no account" needs no `left_join`:

```python
--8<-- "docs/examples/traversal.py:is-null"
```

Both spellings here are the **forward** direction — the FK column lives on the queried table. Asking the same question in the *reverse* direction ("has at least one / no related child row") is an [existence test](#existence-tests-on-reverse-many-to-many-relations): `t.lines.exists()` / `~t.lines.exists()`.

### Keeping rows that have no relation

When you want the relation-less rows *kept* rather than filtered out, opt into a `left_join`. It marks **every edge of its path** LEFT (the whole-path rule), so a left-marked two-hop path retains rows missing the relation at either hop:

```python
--8<-- "docs/examples/traversal.py:left-join"
```

A bare `left_join` on a path also traversed by `where()` lifts the shared edge to LEFT — an explicit LEFT always beats an implicit INNER on the same edge (declaring `join` **and** `left_join` on one edge is a build-time `ValueError`).

!!! note "NULL ordering defaults to last unless you override it"
    Relation-less rows under `left_join` land as `NULL` sort keys on a related column — omitted `nulls=` still means last. Pass `nulls="first"` to lead with `NULL`s, or `nulls="native"` for dialect-default placement — see [Ordering, Limit & Offset](#ordering-limit--offset).

### Two foreign keys to the same table

Distinct relation paths are distinct joins even when they point at the same table, so two FKs to one model just work — no alias ceremony:

=== "Assignment"

    ```python
    --8<-- "docs/examples/traversal.py:two-fk-model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/traversal_annotated.py:two-fk-model"
    ```

```python
--8<-- "docs/examples/traversal.py:two-fk-query"
```

### Self-referential traversal

A self-referencing FK traverses like any other — the join target is the same table, reached by a distinct path:

=== "Assignment"

    ```python
    --8<-- "docs/examples/traversal.py:self-fk-model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/traversal_annotated.py:self-fk-model"
    ```

```python
--8<-- "docs/examples/traversal.py:self-fk-query"
```

### Traversing from a many-to-many

An association query (`post.tags`) composes with forward-FK traversal on the target model: the association join and the traversal join coexist in one statement.

=== "Assignment"

    ```python
    --8<-- "docs/examples/traversal.py:m2m-model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/traversal_annotated.py:m2m-model"
    ```

```python
--8<-- "docs/examples/traversal.py:m2m-query"
```

### Filtering reverse relations

Reverse relations (`BackRef`) are chainable queries in their own right — filter, order, and slice them before executing, and their predicates can traverse too:

```python
published = await author.posts.where(lambda post: post.published == True).all()  # noqa: E712
latest = await author.posts.order_by(lambda post: post.created_at, "desc").limit(5).all()
n = await author.posts.count()
```

That is a query *from* an instance. To filter the **root query** on membership in a reverse relation — "authors who have at least one published post" — use an [existence test](#existence-tests-on-reverse-many-to-many-relations): `Author.where(lambda a: a.posts.exists(lambda post: post.published == True))`.

### Results are plain root instances

A traversed query is **shape-preserving**: filtering `Transaction` through `transaction.account.ledger_id` still returns `Transaction` instances, no matter how deep the predicate reaches. Traversal does *not* pre-load the related rows onto the results — `await transaction.account` still issues its own query, exactly as it does without any traversal. Attaching related data is a separate, explicit request: [`include()`](#populating-relations-with-include).

### `update()` and `delete()` cannot traverse

Portable SQL has no `UPDATE … JOIN` / `DELETE … JOIN`, so a traversed predicate on `update()`/`delete()` is rejected **before any SQL runs**:

```text
>>> await Transaction.where(lambda transaction: transaction.account.label == "a1").delete()
ValueError: delete() does not support relation traversal: portable SQL has no DELETE ... JOIN. Fetch primary keys via the joined query first, then delete by primary-key set. (A join-free relation filter like `t.account == instance` is allowed.)
```

Do the two-step: fetch the primary keys with the joined query, then mutate by that key set (a join-free `relation == instance` or `== None` filter *is* allowed on mutations):

```python
--8<-- "docs/examples/traversal.py:mutate-limitation"
```

### Guardrails

- A predicate lambda that returns a **bare relation** (`lambda transaction: transaction.account`) is meaningless as a filter and raises `TypeError` pointing you at `== None`, `== an instance`, or a column comparison. The same bare relation in `order_by()` is likewise rejected.
- Combining predicates with Python's `and`/`or`/`not` (instead of `&`/`|`/`~`) coerces a node to `bool` and raises `TypeError: QueryNode cannot be used in a boolean context; use & / | to combine predicates and ~ to negate them`. Always parenthesize and use the bitwise operators.

See [Relationships](relationships.md) for the schema-declaration side of foreign keys and reverse relations.

## Existence Tests on Reverse & Many-to-Many Relations

Traversal reaches *forward* along a foreign key. The reverse question — "which transactions have at least one split line?", "which transactions appear in any transfer?" — is a different shape: the related rows live on the **other** table, keyed back at you. In a predicate, a reverse (`BackRef`) or many-to-many relation supports exactly one verb for that question, the **existence test**:

```python
matches = await Transaction.where(lambda t: t.lines.exists()).all()
```

`.exists()` renders as a correlated `EXISTS` subquery — never a join — so the result stays **root-shaped**: each matching row comes back exactly once (a transaction with three lines is one result, no `DISTINCT` bookkeeping), rows are never multiplied, and the test composes with every other predicate, ordering, and paging. One verb covers every cardinality: a one-to-one `BackRef`, a to-many `BackRef`, and an M2M edge all spell the same, so a schema cardinality change never breaks a call site.

The examples below use this schema — a transfer links two transactions through unique FKs (one-to-one BackRefs), split lines hang off a transaction (to-many), and a category is referenced by both layers:

=== "Assignment"

    ```python
    --8<-- "docs/examples/existence_tests.py:schema"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/existence_tests_annotated.py:schema"
    ```

### Bare tests and negation

A bare `.exists()` asks "is any related row there?". Negation is the uniform `~` — NOT EXISTS is not a separate spelling:

```python
--8<-- "docs/examples/existence_tests.py:bare"
```

The first query ships to the database as:

```sql
SELECT ... FROM transaction t
WHERE EXISTS (SELECT 1 FROM transfer WHERE transfer.outflow_transaction_id = t.id)
   OR EXISTS (SELECT 1 FROM transfer WHERE transfer.inflow_transaction_id = t.id)
```

### Scoping with an inner predicate

Pass a lambda to filter *which* related rows count. The inner lambda is a **full ferro predicate over the related model** — every operator, `&`/`|`/`~`, forward traversal, even nested existence tests — not a sub-language:

```python
--8<-- "docs/examples/existence_tests.py:scoped"
```

Rendered SQL — the root branch keeps line-less transactions, the `EXISTS` branch finds categories on any line:

```sql
SELECT ... FROM transaction t
WHERE t.category_id IN (...)
   OR EXISTS (SELECT 1 FROM split_line l
              WHERE l.txn_id = t.id AND l.category_id IN (...))
```

Because the result is root-shaped, keyset ordering and paging compose unchanged:

```python
--8<-- "docs/examples/existence_tests.py:composes"
```

### Grouping is explicit

With conditions on the *same* relation, there are two different questions: does **one** related row match all conditions, or does **some** related row match each? The lambda scope makes the choice visible — one test with a compound inner predicate, or two tests combined outside:

```python
--8<-- "docs/examples/existence_tests.py:grouping"
```

This is why reverse relations have an explicit combinator rather than implicit path traversal (`t.lines.category_id == x` raises): with implicit traversal, nothing on the page says which of those two questions `(t.lines.a == 1) & (t.lines.b == 2)` asks (ADR-0007).

### Traversal inside the test, and nesting

Forward-FK traversal works inside the inner lambda — its joins render *inside* the `EXISTS` subquery with the same [INNER semantics](#every-traversed-hop-is-an-inner-join) as everywhere else — and existence tests nest to any depth:

```python
--8<-- "docs/examples/existence_tests.py:traversal-inside"
```

### Many-to-many

An M2M relation spells identically, from either side — the test correlates through the association table and the inner lambda scopes over the target model:

=== "Assignment"

    ```python
    --8<-- "docs/examples/existence_tests.py:m2m-schema"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/existence_tests_annotated.py:m2m-schema"
    ```

```python
--8<-- "docs/examples/existence_tests.py:m2m"
```

### One verb, loud dead ends

Reverse relations are **tested, never traversed** (ADR-0007). Every other way of naming a reverse or M2M relation in a query fails at build time with the supported spelling in the message:

- **Column access** (`t.lines.category_id`) raises `AttributeError` — scope the columns with the inner lambda instead: `t.lines.exists(lambda line: line.category_id == ...)`.
- **Comparisons**, including the tempting `t.transfer_out != None`, raise `TypeError` — a reverse relation has no root-side column to be `NULL`; the spelling is `t.transfer_out.exists()` (and `~t.transfer_out.exists()` for absence).
- **`in_()` with a query** (`t.id.in_(subquery)`) raises `TypeError` — the workloads an `IN (subquery)` serves are existence-test workloads, without hand-correlating on id columns.
- **`join()` / `left_join()` on a reverse edge** raise `TypeError` — a join there would multiply root rows; membership is the existence test's job.
- **The inner lambda sees only its own parameter.** Referencing the outer lambda's parameter (or comparing column-to-column) is a build-time error — cross-scope correlation is a tracked future capability ([#309](https://github.com/syn54x/ferro-orm/issues/309)), never a silent misrender.

Existence tests answer **membership** only. *Populating* a reverse collection onto results (the data axis) is a separate future mechanism — see [Not Yet Supported](#not-yet-supported); until it lands, fetch collections through the relation itself (`await txn.lines.all()`).

!!! note "Two `exists`, two levels"
    `t.lines.exists(...)` inside a predicate is the existence *test* on a relation. `await query.exists()` is the query *terminal* asking whether the whole query matches any row. Same word, deliberately — both ask "is there at least one?" — at different levels.

## Populating Relations with include()

Every relation access is a query. A list view that renders 100 transactions with their account labels awaits `transaction.account` 100 times — 101 statements for one screen. `include()` cures that N+1: ask the query to bring the related rows along, and each result's relation arrives **populated**:

```python
--8<-- "docs/examples/populated_relations.py:basic"
```

One SQL statement. The query still returns the same `list[Transaction]` it always did — and `transaction.account` is now a plain attribute holding the complete `Account` instance, exactly as the field's annotation (`account: Account`) always claimed. No await, no query.

The examples in this section use this schema (both foreign keys nullable, so you can see what happens when a relation is absent):

=== "Assignment"

    ```python
    --8<-- "docs/examples/populated_relations.py:schema"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/populated_relations_annotated.py:schema"
    ```

### The population contract

A relation is in exactly one of two states on any given instance:

- **Populated** (the query included it): access is a **plain attribute** returning the complete related instance. A nullable FK with no target populates as `None` — truthful to the declared `Account | None`.
- **Unpopulated** (everything else): access keeps today's **awaitable** contract, unchanged — a coroutine that runs its own query:

```python
--8<-- "docs/examples/populated_relations.py:awaitable"
```

!!! warning "Awaiting a populated relation is a hard break"
    `await transaction.account` on a *populated* instance raises `TypeError: 'Account' object can't be awaited` — the attribute is the instance itself, not a coroutine. Code that must handle both states cheaply can check `inspect.isawaitable(...)`, but the better pattern is to decide at the query: if you are going to read the relation, include it.

There is no separate "loaded" model type: `.all()` on an included query still returns `list[Transaction]`, and a populated instance is an ordinary instance of the target model. (Why no `Loaded[Transaction]`? See [Typed Query Predicates](../concepts/query-typing.md#included-queries).)

### Include never changes membership

Joins decide **membership**, projection decides **shape**, include decides **attached data** — three orthogonal axes. Adding `.include(...)` to any query returns exactly the rows that query returned without it:

```python
--8<-- "docs/examples/populated_relations.py:membership"
```

Under the hood, an edge only the include touches renders a LEFT join, and an edge any other clause references keeps that clause's join type. So a `where()` traversal on the same path keeps its INNER narrowing — include attaches data to the surviving rows, it never rewrites what a filter matches:

```python
--8<-- "docs/examples/populated_relations.py:interplay"
```

`count()` and `exists()` are likewise unaffected — they measure the same rows with or without the include:

```python
--8<-- "docs/examples/populated_relations.py:count"
```

### Multi-hop paths populate every hop

`include(lambda t: t.account.owner)` populates the whole path — a populated graph is never missing its intermediate nodes. A `NULL` somewhere along the chain ends the chain as a populated `None`, with the root row retained:

```python
--8<-- "docs/examples/populated_relations.py:multi-hop"
```

Includes are cumulative, order-free, and idempotent: `.include(lambda t: t.account)` plus `.include(lambda t: t.account.owner)` — in either order — is the same query as `.include(lambda t: t.account.owner)` alone.

### Populated instances are session instances

In a session, population runs through the identity map like every other fetch: same row, same object. A populated `Account` **is** the `Account` a direct fetch returns — deduped across result rows, and attached onto instances the session already holds:

```python
--8<-- "docs/examples/populated_relations.py:identity"
```

Populations **accumulate** across a session's queries — an `account` include and a later `attachment` include both stick, so shared objects get richer, never forked:

```python
--8<-- "docs/examples/populated_relations.py:accumulate"
```

Outside a session there is no identity map, so an included query returns fresh instances per row — including a fresh related instance per row, with no query-local dedup. Sessionless Ferro keeps exactly one identity story: the session.

### Refreshes drop populations that stopped being true

Every fetch refreshes instances the session already holds. A refresh keeps each population **iff the row's foreign key still points at it**; if the FK changed (or went `NULL`) underneath you, the now-lying population is dropped and access reverts to the awaitable — a populated relation never points at the wrong row:

```python
--8<-- "docs/examples/populated_relations.py:refresh-rule"
```

### The loud limits

Misuse raises at build time, before any SQL:

- **Forward foreign keys only.** Including a `BackRef` or `ManyToMany` relation raises `TypeError`: reverse and M2M population will be a separate mechanism (a batched second query stitched onto the results), not `include()`. Until it lands, fetch collections through the relation itself (`await author.posts.all()`). *Filtering* on reverse membership is a different axis and already works — the [existence test](#existence-tests-on-reverse-many-to-many-relations).
- **Lambda selectors only.** `include("account")` raises pointing at the lambda form — strings never traverse. Selecting a *column* (`include(lambda t: t.account.label)`) raises too: every populated hop is a complete row, so there is nothing to select per column.
- **No include × projection.** A query carries exactly one materialization plan — populated instances or projected records, never both — and record results are flat, permanently. Either order raises `ValueError` pointing at [traversed projection](#reaching-across-a-relation), the record-shaped way across a relation.
- **No mutations.** `update()`/`delete()` on an included query raise — a mutation returns no instances to populate.

```python
--8<-- "docs/examples/populated_relations.py:limits"
```

Include composes with the M2M association context (`post.tags.include(lambda tag: tag.created_by)` populates each tag's forward FK) and with everything else a query does: `where()` (traversal included), `order_by()`, `limit()`/`offset()`, `first()`, and query branching.

## Selecting a Column Subset

Every query so far loads complete rows into complete model instances. When a list view only reads two columns of a wide table, ask for less: pass `select()` a lambda naming the columns, and the query becomes a **projection**:

```python
--8<-- "docs/examples/partial_selects.py:basic"
```

The examples in this section use this schema:

=== "Assignment"

    ```python
    --8<-- "docs/examples/partial_selects.py:schema"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/partial_selects_annotated.py:schema"
    ```

### What you get back — and why it isn't a `Transaction`

A projected query does **not** return `Transaction` instances. In Ferro, **a model instance always carries a complete row** — there is no such thing as a partial or deferred-field model instance, anywhere. If projection handed you a two-column `Transaction`, every `save()`, every refresh, and every helper that takes a model would have to wonder which query produced its argument, and touching an unselected field would blow up far from the query that caused it.

So anything narrower than a full row comes back honestly typed as what it is: a **projected record** — a `Row`, delivered in the list-like `Rows` container:

```python
--8<-- "docs/examples/partial_selects.py:not-a-model"
```

A `Row` is read-only in the persistence sense: it has no `save()`, no refresh, and never enters the identity map — a record can never masquerade as a row in the database. It carries exactly the columns you selected, in selection order, decoded by the same machinery as full hydration — a projected `datetime`, `UUID`, enum, or `Decimal` column has the same Python type and value it would have on the model, on both backends. Under the hood the query declares this result shape explicitly (a *materialization plan* travels with the query), which is also why asking for less is never slower per row than asking for everything: records are built on the same zero-validation path as models.

Selecting a single column needs no tuple ceremony:

```python
--8<-- "docs/examples/partial_selects.py:single"
```

### `Rows` is a list you can ship

`Rows` behaves like a list — index, slice, iterate, `len()` — and both `Rows` and `Row` are pydantic-shaped: `model_dump()` yields `list[dict]`, and `Rows[Row]` drops straight into a FastAPI `response_model`:

```python
--8<-- "docs/examples/partial_selects.py:container"
```

### Column-name strings

Quick scripts can pass column names as strings — the same string contract as `order_by()`: root columns only (shadow `{fk}_id` columns included), validated at build time. The lambda form remains the documented style:

```python
--8<-- "docs/examples/partial_selects.py:strings"
```

### Reaching across a relation

A selected field may traverse a forward-FK relation, at any depth — the same attribute chaining as a `where()` predicate. Unaliased, a traversed field takes the **bare leaf column name**:

```python
--8<-- "docs/examples/partial_selects.py:traversed"
```

Strings never traverse (`select("account.label")` is rejected permanently) — traversed projection is lambda-only.

### Naming output fields

Return a **dict** from the selector and the keys name the record's fields — an *output alias*. Aliases name output fields only, never joins or tables, and the dict's insertion order is the record's field order:

```python
--8<-- "docs/examples/partial_selects.py:aliases"
```

The dict form is also how you resolve a name collision: `t.id` and `t.account.id` both want to be called `id`, so selecting them unaliased is a build-time error naming this fix:

```python
--8<-- "docs/examples/partial_selects.py:collision"
```

### Traversal narrows; `left_join()` opts out

Projection traversal is ordinary traversal (ADR-0006): it renders an INNER join per relation path — **shared** with any `where()`/`order_by()` traversal of the same path — so rows without the relation drop out. `left_join()` keeps them, and their traversed fields decode to `None`, *even when the source column is non-nullable* — a projected record describes the row you got, not the related model:

```python
--8<-- "docs/examples/partial_selects.py:traversal-narrows"
```

### Projections compose like any other query

`where()` (relation traversal included), `order_by()` (even by columns the projection does not select), `limit()`/`offset()`, and `first()` all work unchanged; on a plain projection `count()` and `exists()` are unaffected — they measure the same matching rows a full query would. (On an *aggregate* projection they raise with guidance instead — see [Aggregations & Grouped Queries](aggregations.md#the-loud-limits).)

```python
--8<-- "docs/examples/partial_selects.py:compose"
```

```python
--8<-- "docs/examples/partial_selects.py:count"
```

### Build-time validation and the loud limits

A misspelled column fails when you build the query, with a did-you-mean — exactly like `where()` and `order_by()`, and a traversed field validates every hop against that hop's model:

```text
>>> Transaction.select(lambda t: (t.id, t.amonut))
AttributeError: Transaction has no queryable column 'amonut'. Did you mean 'amount'? Valid columns: account_id, amount, id, memo.
```

Misuse is loud, never silently ignored:

- **No output-name collisions.** Two selected fields resolving to the same name raise `ValueError` naming the dict form (see above).
- **One selector, one shape.** A dict nested in a tuple, a tuple (or dict) as a dict value, and non-string dict keys all raise `TypeError` — a projected record is flat.
- **No mutations through a projection.** `update()` / `delete()` on a projected query raise `ValueError` at the call, before any SQL — a projection is a read shape, and silently ignoring it would make `select(...)` a no-op on mutations. Mutate through an unprojected query instead.
- **No double `select()`.** Replacing a projection mid-chain would change the result type; name every field in one call, or start a new query from the model.
- **No mixing forms.** Strings and a lambda in one `select()` call raise `TypeError`.
- **No `include()` with a projection**, in either chain order: a query carries exactly one materialization plan, and record results are flat — permanently. Reach across the relation with traversed projection, or populate instances on an unprojected query.

Static typing follows the same shape promise as predicates: `select(...)` flips the query's type so `.all()` checks as `Rows[Row]` and `.first()` as `Row | None` — passing a `Row` where a model instance is expected fails the type checker. See [Typed Query Predicates](../concepts/query-typing.md#projected-queries).

Aggregation builds directly on this machinery — `select(lambda t: {"total": t.amount.sum()})` — and mixing aggregate and plain fields turns the projection into a **grouped query**. That is its own chapter: [Aggregations & Grouped Queries](aggregations.md).

## Not Yet Supported

!!! note "On the roadmap"
    The following query features are **not yet implemented** — see the [Roadmap](../roadmap.md):

    - `having()` — post-aggregation filtering; `where()` rejects aggregate predicates pointing at it ([#291](https://github.com/syn54x/ferro-orm/issues/291))
    - Reverse (`BackRef`) and many-to-many **population** — [`include()`](#populating-relations-with-include) covers forward FKs; collection population is a separate future mechanism. (*Filtering* on reverse/M2M membership is supported — that's the [existence test](#existence-tests-on-reverse-many-to-many-relations).)
    - Cross-scope correlation inside an existence test — comparing an inner-lambda column to an outer column ([#309](https://github.com/syn54x/ferro-orm/issues/309)); rejected loudly at build time today
    - Case-insensitive `ilike()`

## See Also

- [Aggregations & Grouped Queries](aggregations.md) — `count`/`sum`/`avg`/`min`/`max` and derived grouping
- [Mutations](mutations.md) — creating, updating, and deleting records
- [Relationships](relationships.md) — forward and reverse relations
- [Typed Query Predicates](../concepts/query-typing.md) — why three predicate styles exist
- [Raw SQL](raw-sql.md) — the escape hatch for queries the ORM can't express
- [Pagination](../howto/pagination.md) — efficient pagination patterns
