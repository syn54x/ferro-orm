# Relationships

Ferro connects models with foreign keys, zero-boilerplate reverse lookups, and automatically managed join tables.

## Overview

Relationships are **lazy** — nothing is fetched until you ask for it:

- **Forward relations** (a `ForeignKey` field): `await post.author` performs one query and returns the related instance.
- **Reverse relations** (a `BackRef` field): `author.posts` is a chainable query — filter, order, and slice it before awaiting a terminal.

A forward `ForeignKey(related_name="x")` always pairs with a reverse field named `x` on the target model. The pairing is **required and checked at `connect()`** — a `ForeignKey` whose `related_name` has no matching `BackRef()` on the target raises at connect time.

## One-to-Many

The most common shape: a `ForeignKey` on the "child" model, declared as `Annotated` metadata, plus a `Relation[list[...]] = BackRef()` on the "parent":

=== "Assignment"

    ```python
    --8<-- "docs/examples/relationships.py:one-to-many"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/relationships_annotated.py:one-to-many"
    ```

```python
--8<-- "docs/examples/relationships.py:one-to-many-usage"
```

### Shadow FK columns

For every `ForeignKey` field (e.g. `team`), Ferro creates a shadow scalar column and matching Pydantic field named `{field}_id` (e.g. `team_id`) holding the related row's primary key. Its Python type follows the target model's primary-key annotation. Read it or filter on it like any other column — `Player.where(lambda t: t.team_id == team.id)` — with no extra query.

## One-to-One

Add `unique=True` to the `ForeignKey` to enforce at most one child per parent. The reverse side is a plain (non-list) `BackRef` that resolves to a single instance:

=== "Assignment"

    ```python
    --8<-- "docs/examples/relationships.py:one-to-one"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/relationships_annotated.py:one-to-one"
    ```

```python
--8<-- "docs/examples/relationships.py:one-to-one-usage"
```

`unique=True` implies an index on the shadow column (combining it with `index=True` is redundant and emits a `UserWarning`).

## Many-to-Many

Declare `ManyToMany(related_name=...)` on one side and a `BackRef()` collection on the other. Ferro synthesizes the join table automatically:

=== "Assignment"

    ```python
    --8<-- "docs/examples/relationships.py:many-to-many"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/relationships_annotated.py:many-to-many"
    ```

```python
--8<-- "docs/examples/relationships.py:m2m-usage"
```

Both sides expose the chainable query API plus the link mutators `add(*instances)`, `remove(*instances)`, and `clear()`.

`ManyToMany` accepts:

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `related_name` | required | Name of the reverse field on the related model. |
| `through` | `None` | Explicit join-table name; auto-generated when omitted. |
| `reverse_index` | `True` | Add a non-unique composite index on `(target_col, source_col)` in the join table so reverse queries also hit an index. Pass `False` to opt out on write-heavy join tables. |

The default join table gets a **composite unique** on its two foreign-key columns, so the same link can never be stored twice. `reverse_index` lives on the forward `ManyToMany(...)` declaration — passing it to `BackRef()` raises `TypeError`.

## Self-Referential

A model can reference itself — org charts, threaded comments, category trees. The forward reference must be the **quoted class name**, and because a string annotation cannot carry `| None`, nullability must be declared explicitly with `nullable=True`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/relationships.py:self-referential"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/relationships_annotated.py:self-referential"
    ```

```python
--8<-- "docs/examples/relationships.py:self-referential-usage"
```

Without `nullable=True` the root of the tree (an `Employee` with no manager) could never be stored.

## Nullable Relationships

For an optional relation to *another* model, put the union **inside** `Annotated` and default the field to `None`:

=== "Assignment"

    ```python
    from typing import Annotated

    from ferro import BackRef, Field, ForeignKey, Model, Relation


    class Category(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        products: Relation[list["Product"]] = BackRef()


    class Product(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        category: Annotated[Category | None, ForeignKey(related_name="products")] = None
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import BackRef, Field, ForeignKey, Model, Relation


    class Category(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        name: str
        products: Relation[list["Product"]] = BackRef()


    class Product(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        name: str
        category: Annotated[Category | None, ForeignKey(related_name="products")] = None
    ```

!!! warning "Union placement matters"
    Write `Annotated[Category | None, ForeignKey(...)] = None` — the union goes **inside** `Annotated`. The form `Annotated[Category, ForeignKey(...)] | None` is not supported and will not produce a nullable foreign key.

With the default `nullable="infer"`, Ferro derives column nullability from whether the relation annotation allows `None`. `on_delete="SET NULL"` also implies a nullable column (and explicitly combining it with `nullable=False` raises).

## Delete Behavior

`ForeignKey(on_delete=...)` controls what happens to child rows when their parent is deleted:

| `on_delete` | Effect when the parent row is deleted |
| :--- | :--- |
| `"CASCADE"` (default) | Child rows are deleted too. |
| `"RESTRICT"` | Deletion fails while child rows exist. |
| `"SET NULL"` | The shadow FK column is set to `NULL` (requires a nullable relation). |
| `"SET DEFAULT"` | The shadow FK column is reset to its column default. |
| `"NO ACTION"` | The constraint is not enforced at delete time (backend semantics apply). |

=== "Assignment"

    ```python
    --8<-- "docs/examples/relationships.py:on-delete"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/relationships_annotated.py:on-delete"
    ```

Because `CASCADE` is the default, deleting a parent silently removes its children unless you choose otherwise — pick `RESTRICT` when orphan deletion would be a bug you want surfaced.

## Indexing Foreign Keys

PostgreSQL does not automatically index foreign-key columns. For FKs that appear in hot query paths (tenant IDs on every list endpoint, for instance), request a non-unique index on the shadow column with `index=True`:

=== "Assignment"

    ```python
    from typing import Annotated

    from ferro import BackRef, Field, ForeignKey, Model, Relation


    class Org(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        projects: Relation[list["Project"]] = BackRef()


    class Project(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        org: Annotated[Org, ForeignKey(related_name="projects", index=True)]
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import BackRef, Field, ForeignKey, Model, Relation


    class Org(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        name: str
        projects: Relation[list["Project"]] = BackRef()


    class Project(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        name: str
        org: Annotated[Org, ForeignKey(related_name="projects", index=True)]
    ```

One-to-one relations (`unique=True`) already get an index; for multi-column indexes that start with the FK column, use [composite indexes](models-and-fields.md#composite-indexes).

## See Also

- [Models & Fields](models-and-fields.md) — field declaration styles and constraints
- [Queries](queries.md) — filtering on shadow FK columns and reverse relations
- [Mutations](mutations.md) — creating related records, cascade implications
- [Schema Migrations](migrations.md) — how relationships appear in Alembic metadata
