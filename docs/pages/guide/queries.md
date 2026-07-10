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

## Ordering, Limit & Offset

Sort with `.order_by(field, direction)` (direction defaults to ascending; pass `"desc"` to reverse) and slice with `.limit()` / `.offset()`. `field` is a lambda naming the column (`order_by(lambda u: u.created_at, "desc")`, matching the `where()` predicate style) or a column-name string (`order_by("created_at", "desc")`). Both forms are validated against the model's queryable columns at build time:

```python
--8<-- "docs/examples/predicates.py:ordering-slicing"
```

Chain `.order_by()` multiple times for multi-column sorts. For robust pagination patterns, see [Pagination](../howto/pagination.md).

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

### Keeping rows that have no relation

When you want the relation-less rows *kept* rather than filtered out, opt into a `left_join`. It marks **every edge of its path** LEFT (the whole-path rule), so a left-marked two-hop path retains rows missing the relation at either hop:

```python
--8<-- "docs/examples/traversal.py:left-join"
```

A bare `left_join` on a path also traversed by `where()` lifts the shared edge to LEFT — an explicit LEFT always beats an implicit INNER on the same edge (declaring `join` **and** `left_join` on one edge is a build-time `ValueError`).

!!! note "NULL ordering diverges by dialect under `left_join`"
    Once relation-less rows survive into an `order_by` on a related column, their `NULL` sort key lands in a dialect-specific spot: **PostgreSQL sorts `NULL`s last on an ascending sort; SQLite sorts them first.** This divergence only appears because you opted into `left_join` — plain INNER traversal drops those rows, so it never surfaces (ADR-0006).

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

### Results are plain root instances

A traversed query is **shape-preserving**: filtering `Transaction` through `transaction.account.ledger_id` still returns `Transaction` instances, no matter how deep the predicate reaches. Traversal does *not* pre-load the related rows onto the results — `await transaction.account` still issues its own query, exactly as it does without any traversal. (Eager loading is separate future work; see [Not Yet Supported](#not-yet-supported).)

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
- Combining predicates with Python's `and`/`or` (instead of `&`/`|`) coerces a node to `bool` and raises `TypeError: QueryNode cannot be used in a boolean context; use & / |`. Always parenthesize and use the bitwise operators.

See [Relationships](relationships.md) for the schema-declaration side of foreign keys and reverse relations.

## Not Yet Supported

!!! note "On the roadmap"
    The following query features are **not yet implemented** — see the [Roadmap](../roadmap.md):

    - Aggregations beyond `count()` / `exists()` (`sum`, `avg`, `min`, `max`, `GROUP BY`)
    - Partial selects (selecting specific columns; queries always load all model fields)
    - Eager loading (`prefetch_related` / `select_related`) — be mindful of N+1 patterns when looping over relations
    - Case-insensitive `ilike()`
    - `not_in()` (negate with `!=` conditions combined with `&` in the meantime)

## See Also

- [Mutations](mutations.md) — creating, updating, and deleting records
- [Relationships](relationships.md) — forward and reverse relations
- [Typed Query Predicates](../concepts/query-typing.md) — why three predicate styles exist
- [Raw SQL](raw-sql.md) — the escape hatch for queries the ORM can't express
- [Pagination](../howto/pagination.md) — efficient pagination patterns
