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

Every `ForeignKey` field gets a shadow `*_id` column you can filter on like any scalar:

```python
posts = await Post.where(lambda post: post.author_id == user.id).all()
```

Reverse relations (`BackRef`) are chainable queries themselves — filter, order, and slice them before executing:

```python
published = await author.posts.where(lambda post: post.published == True).all()  # noqa: E712
latest = await author.posts.order_by(lambda post: post.created_at, "desc").limit(5).all()
n = await author.posts.count()
```

Joins across relations inside a single `where()` are not supported — filter on shadow FK columns or use the reverse-relation query. See [Relationships](relationships.md) for the full picture.

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
