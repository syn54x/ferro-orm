---
description: "Declare a table as a Ferro model and add keys, constraints, indexes, defaults and column types with Field()."
---

# Models and fields

A model is a Python class that declares one database table: each annotated attribute is a column, and [`Field()`](../api/fields.md#ferro.Field) adds the database options. This page builds one product catalog and adds a feature in each section. Here is the whole `Product` model, created in an in-memory SQLite database and saved once:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:program"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:program"
    ```

Run it with `asyncio.run(main())`. The database assigns `id`, and `status` takes its default. `create_tables()` returns the statements it ran; this is the table it created:

```sql
--8<-- "docs/examples/models_and_fields.py:product-table"
```

Columns are written in name order, and every annotation that does not allow `None` is `NOT NULL`. The rest of this page takes the model apart one feature at a time. Each later example runs inside the session the first one opens; see [Sessions](connections.md#sessions).

Every Ferro model is also a [Pydantic](https://docs.pydantic.dev/latest/) `BaseModel`, so `model_dump()`, `model_dump_json()`, validators and `model_config` work as they do in Pydantic.

## Declaration styles

`Field()` takes the database options (`primary_key`, `unique`, `index`, `db_type`) and Pydantic's validation options (`max_length`, `ge`, `pattern`) in one call. The two tabs above attach it in the two ways Ferro accepts, and they create the same schema:

- **Assignment** puts `Field(...)` on the right of `=`, the classic Pydantic style.
- **Annotated** puts it inside `typing.Annotated[...]` and keeps the default as the assigned value, so a type checker sees the default.

Pick one style per project. Every example in these docs shows both, in tabs. A forward foreign key is always written `Annotated[Author, ForeignKey(...)]`, in either style; see [Relationships](relationships.md).

## Field types

Ferro chooses each column's type from the annotation. The table lists what the create pass writes for each annotation, on each database:

--8<-- "docs/includes/field-types.md:annotations"

The table is generated from the code that chooses the type, so it matches the schema Ferro creates. A few rows need a sentence more:

- An annotation that allows `None`, such as `int | None`, drops `NOT NULL` and keeps the type.
- `datetime` is `timestamp with time zone` on PostgreSQL. For a column without a time zone, declare `db_type="timestamp"`.
- A `UUID` column is declared `CHAR(32)` on SQLite, and the value Ferro stores in it is the 36-character hyphenated string. SQLite does not enforce a declared length.
- A JSON-family field, `dict`, `list`, `tuple`, `set` or a nested Pydantic model, is `jsonb` on PostgreSQL and `JSON` on SQLite. See [JSON storage](#json-storage-json-and-jsonb).

### Column type overrides with `db_type`

When you want a different column type for a field, for example a length limit of `varchar(200)` instead of unbounded `varchar`, pass a `db_type` token:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:db-type"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:db-type"
    ```

The column becomes:

```sql
--8<-- "docs/examples/models_and_fields.py:name-column"
```

`max_length=200` is the Pydantic check that rejects a longer name before it reaches the database; `db_type` sets only the column. Build `varchar(n)` with [`ferro.varchar`](../api/fields.md#ferro.varchar) rather than the string `"varchar(200)"`, so a type checker sees a token it knows. These are the tokens and what each writes:

--8<-- "docs/includes/field-types.md:db-type-tokens"

Ferro checks the token against the annotation when the class is defined, so `x: int = Field(db_type="uuid")` raises `TypeError` when the class is defined, not at the first query.

### Enums

An `enum.Enum` field stores the enum's values. `Product.status` is a `StrEnum` with a default:

```python
--8<-- "docs/examples/models_and_fields.py:enum"
```

On SQLite the column is `varchar(n)`, sized to the longest value (`retired`, 7 characters):

```sql
--8<-- "docs/examples/models_and_fields.py:status-column"
```

On PostgreSQL the column uses a native enum type named after the class, `status`, which every migration strategy creates before the table: [auto-migrate](schema/auto-migrate.md), [migrations](schema/migrations.md) and the [Alembic bridge](schema/alembic.md). Either way the database holds the value, not the member name:

```python
--8<-- "docs/examples/models_and_fields.py:enum-value"
```

The row reads back `"active"`, the value of `Status.ACTIVE`. To keep a closed set of values in a plain column with a `CHECK` constraint instead, declare `Field(db_type="text", db_check=True)`; the constraint is named `ck_<table>_<column>`.

### JSON storage: `json` and `jsonb`

A `dict`, a `list` or a nested Pydantic model is stored as one JSON document. `Product` has two:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:json"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:json"
    ```

Both columns are `jsonb` on PostgreSQL and `JSON` on SQLite, and a value reads back equal to the one saved:

```python
--8<-- "docs/examples/models_and_fields.py:json-round-trip"
```

`jsonb` is binary storage that PostgreSQL can index and compare, and it is the type PostgreSQL recommends. It does not keep the text you wrote: keys come back sorted, a duplicate key keeps its last value, and whitespace is dropped. Python's `==` ignores dict key order, so the assertions above hold. If you iterate the keys and need their insertion order, or need the stored text byte for byte, declare `db_type="json"` on that field. `db_type="jsonb"` on a field outside the JSON family, such as an `int`, raises `TypeError` when the class is defined.

On SQLite both tokens store plain JSON text, so switching between them changes nothing there. On PostgreSQL the switch is a declaration edit: with [`migrate_updates`](schema/auto-migrate.md) on, the pass runs one `ALTER TABLE ... TYPE ... USING ...` and existing values survive the cast. A [migration](schema/migrations.md) and the [Alembic bridge](schema/alembic.md) write the same statement.

A filter compares a JSON column as a whole value. The query builder has no containment or path operators (`@>`, `->`, `#>>`) and declares no GIN index. To change some keys of a stored object on PostgreSQL, use `.merge()` in a [recipe update](mutations.md#expression-updates).

!!! note "Changed in 0.15"
    Before 0.15, a `dict` or `list` field without `db_type` created a plain `json` column on PostgreSQL. Read [Upgrading to 0.15](../upgrading.md#upgrading-to-015) before you connect such a database with `migrate_updates`.

## Primary keys

### Auto-increment

An integer primary key is generated by the database. Declare it `int | None` with a default of `None`, so an instance can exist before it is saved:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:primary-key"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:primary-key"
    ```

It is `integer PRIMARY KEY AUTOINCREMENT` on SQLite and `serial PRIMARY KEY` on PostgreSQL. `save()` writes the generated value back onto the instance:

```python
--8<-- "docs/examples/models_and_fields.py:save"
```

The second product gets `id` 2. For an integer key that you assign yourself, pass `Field(primary_key=True, autoincrement=False)`.

### Natural keys

A column that already identifies the row, such as a category code, can be the primary key. `Category` uses its `code`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:category"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:category"
    ```

The key is a plain `varchar` primary key, and you pass it on create: `Category.create(code="lighting", title="Lighting")`. `__ferro_table__` names the table; see [Custom table names](#custom-table-names).

### UUID primary keys

A `UUID` key is generated in Python, by a `default_factory`, before the row is written. `Order` uses `uuid.uuid4`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:order"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:order"
    ```

On SQLite the column is declared `CHAR(32)`:

```sql
--8<-- "docs/examples/models_and_fields.py:order-ddl"
```

The value in it is the 36-character hyphenated string, as a raw query shows:

```python
--8<-- "docs/examples/models_and_fields.py:uuid-value"
```

On PostgreSQL the column is a native `uuid`.

## Uniques and indexes

### Single-column uniques

`unique=True` on a field adds a unique index named `uq_<table>_<column>`. `Product.sku` has one:

```sql
--8<-- "docs/examples/models_and_fields.py:sku-unique"
```

`index=True` adds a plain index the same way, named `idx_<table>_<column>`.

### Composite uniques

When a row must be unique across several columns together, declare the `ClassVar` `__ferro_composite_uniques__`. A product name must be unique within its category:

```python
--8<-- "docs/examples/models_and_fields.py:composite-uniques"
```

The index is named after the table and its columns, in order:

```sql
--8<-- "docs/examples/models_and_fields.py:composite-unique-ddl"
```

A second desk lamp in the lighting category fails on that index:

```python
--8<-- "docs/examples/models_and_fields.py:composite-unique-violation"
```

The error is a `UniqueViolationError`; SQLite's message names the two columns.

- Each inner tuple lists column names. For a `ForeignKey` field named `author`, use its column, `author_id`.
- Declare several tuples for several independent composite uniques.
- An unknown column name raises when the class is defined.

SQL `UNIQUE` treats `NULL` values as distinct, so a nullable column in a composite unique lets through rows that differ only by `NULL`. Declare the columns without `None` when you need at most one row per combination.

### Composite indexes

For a non-unique index over several columns, such as a common filter on `status` sorted by `created_at`, declare `__ferro_composite_indexes__` in the same shape:

```python
--8<-- "docs/examples/models_and_fields.py:composite-indexes"
```

It creates:

```sql
--8<-- "docs/examples/models_and_fields.py:composite-index-ddl"
```

Each tuple names at least two columns, in the order the index uses them, which matters because a query can use the index for any leading run of its columns. For one column, use `Field(index=True)`. If the same ordered tuple is also a composite unique, Ferro emits a `UserWarning` and drops the index, since the unique index already covers it.

Both `ClassVar`s reach [migrations](schema/migrations.md) and [Alembic autogenerate](schema/alembic.md) as the same `UniqueConstraint` and `Index` names.

### Table checks

When a rule spans several columns of one row, declare a table check: a named predicate in the `ClassVar` `__ferro_checks__`. A payment carries a card token or a bank account, never both:

=== "Assignment"

    ```python
    --8<-- "docs/examples/table_checks.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/table_checks_annotated.py:models"
    ```

Each entry is `Check(suffix, predicate)`. Ferro names the constraint `ck_<table>_<suffix>` and writes it inside `CREATE TABLE`, on both databases:

```sql
--8<-- "docs/examples/table_checks.py:ddl"
```

The predicate is a lambda written like a [`where()`](queries.md) filter, with its parameter named after the model in lowercase (`payment` for `Payment`). It accepts comparisons, `== None` and `!= None` (on a forward foreign key or its `*_id` column too), `&`, `|`, `~`, `.in_()`, `.like()`, and column-to-column comparisons. Values are written into the `CHECK` as literals, not bound as parameters. Relation traversal, `.exists()`, aggregates and variables from the enclosing scope raise when the class is defined.

A row that breaks the check raises `CheckViolationError`, whose message names the constraint:

```python
--8<-- "docs/examples/table_checks.py:violation"
```

On PostgreSQL the error also sets `exc.constraint` to `ck_payment_at_most_one_method`.

- A suffix is a lowercase identifier (`[a-z][a-z0-9_]*`), unique within the model, and must not collide with a column check's name (`ck_<table>_<column>`).
- An unknown column, a repeated suffix or an unsupported predicate raises when the class is defined.
- [Auto-migrate](schema/auto-migrate.md) explains how checks reconcile on a table that already exists, and [SQLite table rebuilds](schema/migrations.md#sqlite-table-rebuilds) how a migration changes one on SQLite.

For a closed set of values in a single column, use `Field(db_check=True)` instead; see [Enums](#enums).

#### Functions in a check

Either side of a comparison can call a function from [`ferro.fn`](../api/fields.md#ferro.fn), the same functions a [generated column](#generated-columns) uses. A refund is held to 500.00 in either direction, with its amount in cents:

=== "Assignment"

    ```python
    --8<-- "docs/examples/generated_columns.py:check-model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/generated_columns_annotated.py:check-model"
    ```

`fn.abs` becomes the database's `abs()`. On PostgreSQL the column is cast to `bigint` first, as integer arithmetic in a [generated column](#generated-columns) is. On SQLite the comparison sits inside a guard, because `abs` of the smallest `bigint` overflows (see [value parity](#value-parity)):

=== "SQLite"

    ```sql
    -- generated_columns.py:check-ddl (sqlite)
    CREATE TABLE IF NOT EXISTS "refund" (
      "amount_minor" integer NOT NULL,
      "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
      CONSTRAINT "ck_refund_within_limit" CHECK (CASE WHEN NOT (typeof(abs("amount_minor")) IN ('integer', 'null')) THEN abs(-9223372036854775807 - 1) ELSE (abs("amount_minor") <= 50000) END)
    );
    ```

=== "PostgreSQL"

    ```sql
    -- generated_columns.py:check-ddl (postgres)
    CREATE TABLE IF NOT EXISTS "refund" (
      "amount_minor" integer NOT NULL,
      "id" serial PRIMARY KEY NOT NULL,
      CONSTRAINT "ck_refund_within_limit" CHECK ((abs(CAST("amount_minor" AS bigint)) <= 50000))
    );
    ```

Python's own `abs()` inside a predicate raises when the class is defined; write `fn.abs`. A function call in a check follows the [value parity](#value-parity) rules of a generated column.

## Generated columns

A generated column is one the database computes from other columns of the same row, on every write. Whether you want one, or Pydantic's [`computed_field`](https://docs.pydantic.dev/latest/concepts/fields/#the-computed_field-decorator), depends on who needs the value:

- **Only your Python code reads it**, say a label you print: use `computed_field`. It is a property, computed on each access, and it never reaches the table.
- **The database needs it too**, to filter on, to index, or for a report or another service that reads the table: use a generated column. The value is stored in the row, so SQL sees it like any other column.

A bank transaction stores its amount in minor units (cents), negative for money out. Its direction, `-1` or `1`, is the sign of that amount, and the database works it out:

=== "Assignment"

    ```python
    --8<-- "docs/examples/generated_columns.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/generated_columns_annotated.py:models"
    ```

`generated=` takes a lambda written like a [`where()`](queries.md) filter, with its parameter named after the model, that returns a value instead of a condition. `fn.sign` is one of the database functions in `ferro.fn`. Ferro writes the expression into `CREATE TABLE`:

=== "SQLite"

    ```sql
    --8<-- "docs/examples/generated_columns.py:ddl-sqlite"
    ```

=== "PostgreSQL"

    ```sql
    --8<-- "docs/examples/generated_columns.py:ddl-postgres"
    ```

The column is always `STORED`: the database computes it when the row is written, not when it is read, so it can carry an index (`index=True` and `unique=True` work as on any column). The annotation decides nullability, as for every field: `int` is `NOT NULL`, `int | None` allows `NULL`. A generated column has no `DEFAULT`, and it cannot also take `primary_key=`, `autoincrement=`, `default=` or `db_check=`.

The two dialects differ in how they keep an overflow from storing different values. PostgreSQL computes integer arithmetic at 64 bits, so a column narrower than `bigint` is cast up first. SQLite carries Ferro's own guard, `_ferro_range_direction`, which holds the value to PostgreSQL's `integer` range. A value that does not fit raises `DataError` naming the column, on both databases.

### Reading the value

The database is the only place the value is computed, so an instance holds what the database last stored, or `None` while that is unknown:

```python
--8<-- "docs/examples/generated_columns.py:lifecycle"
```

It prints:

<!-- prints: generated_columns.py:lifecycle -->
```text
None
-1
None
1
```

Before the first `save()` there is no row, so `direction` is `None`. `save()` asks for the value back with `RETURNING` and fills it in. Assigning `amount_minor` sets `direction` back to `None`, because the stored `-1` no longer matches the row about to be written; the next `save()` reads the new value. The two saves send these statements; on PostgreSQL only the placeholders differ:

```sql
-- generated_columns.py:lifecycle (sqlite)
INSERT INTO "transaction" ("amount_minor")
VALUES (?)
RETURNING "id", "direction";

UPDATE "transaction"
SET "amount_minor" = ?
WHERE "id" = ?
RETURNING "direction";
```

Neither statement lists `direction` among the columns it writes. A fetched instance carries the stored value, and `model_dump()` includes it. [`bulk_create()`](mutations.md) reads nothing back, so its instances keep `None`, as they keep `None` for an auto-assigned `id`.

A generated column filters, orders and projects like any other column. The money that went out:

```python
--8<-- "docs/examples/generated_columns.py:filter"
```

It prints:

<!-- prints: generated_columns.py:filter -->
```text
[-1200]
```

The query compares the stored column, which `index=True` covers; on PostgreSQL only the placeholder differs:

```sql
-- generated_columns.py:filter (sqlite)
SELECT "transaction"."amount_minor", "transaction"."direction", "transaction"."id"
FROM "transaction"
WHERE "transaction"."direction" = ?;
```

### Writing to a generated column

Ferro refuses any write to the column, naming what to do instead:

```python
--8<-- "docs/examples/generated_columns.py:refusals"
```

It prints:

<!-- prints: generated_columns.py:refusals -->
```text
TypeError: Transaction.direction is a generated column: the database computes it from amount_minor on every write; drop direction= from the call
TypeError: Transaction.direction is a generated column: the database computes it from amount_minor on every write; assign those columns and save() instead
```

An [`update()`](mutations.md) that sets `direction` and a `save(only={"direction"})` are refused too, when the call is built. Change `amount_minor` and the database follows.

### The functions in `fn`

An expression is built from the model's own columns, literals, `+`, `-`, `*`, `/`, unary `-` and these functions:

| Function | Takes | Returns |
| --- | --- | --- |
| `fn.sign(x)` | an integer, float or decimal | `-1`, `0` or `1` |
| `fn.abs(x)` | an integer, float or decimal | the number without its sign |
| `fn.least(a, b, ...)` | non-nullable numbers or dates | the smallest |
| `fn.greatest(a, b, ...)` | non-nullable numbers or dates | the largest |
| `fn.coalesce(a, b, ...)` | values of one type | the first that is not `NULL` |
| `fn.length(s)` | text | its number of characters |
| `fn.year(d)`, `fn.month(d)`, `fn.day(d)` | a `date` | that part, as an integer |

Each one has a [reference entry](../api/fields.md#ferro.fn). A name that is not in the table, such as `fn.round`, raises listing the ones that are.

### Value parity

Ferro admits an expression only when SQLite and PostgreSQL store the same value for it on every row, and it checks that when the class is defined. A float product is one they disagree on:

=== "Assignment"

    ```python
    --8<-- "docs/examples/generated_columns.py:parity"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/generated_columns_annotated.py:parity"
    ```

It prints:

<!-- prints: generated_columns.py:parity -->
```text
TypeError: __main__.Rate.doubled '*' over a float: Postgres raises when a float product or quotient underflows to zero where SQLite stores 0.0, so the databases would disagree; keep the arithmetic in integers (an amount in minor units, such as amount_minor) or compute the value in the application
```

Each refusal names the fix. The rules, each from a case where the two databases would store different values:

- **The result type matches the field's.** `fn.sign` over an `int` fills an `int` field; it cannot fill a `str`. `db_type=` within the same family (`bigint` for an `int`) is fine.
- **`fn.least` and `fn.greatest` take numbers or dates, never `NULL`.** PostgreSQL skips a `NULL` argument where SQLite returns `NULL`, so a nullable column goes inside `fn.coalesce(column, fallback)`. Text is refused too, because the two order it differently.
- **`fn.year`, `fn.month` and `fn.day` take a `date`.** The day of a `datetime` depends on the session's time zone, which PostgreSQL refuses in a generated column.
- **`/` divides by a non-zero literal only**, such as `/ 100`. PostgreSQL raises on division by zero where SQLite stores `NULL`.
- **No `*` or `/` over a float, and no arithmetic over a `Decimal`.** PostgreSQL raises when a float product underflows where SQLite stores `0.0`, and SQLite computes decimals in floating point. Keep amounts in integer minor units, or compute the value in your application.
- **Only the model's own stored columns.** An expression cannot read another generated column, a relation, or a variable from the enclosing scope; inline a constant as a literal. A misspelled column raises with the closest name.
- **A value, not a condition.** A comparison or `&` / `|` body raises; a condition belongs in a [table check](#table-checks).
- **An operation that overflows fails the row on both databases, in a generated column or a check.** PostgreSQL raises when an integer operation (a sum, difference, product or `fn.abs`) passes 64 bits, or a float one passes the largest double. SQLite would turn the value into a real or `inf`, so Ferro adds a guard there that fails the same row. On both databases the failure is a `DataError` naming the generated column or the check.

[ADR 0059](https://github.com/syn54x/ferro-orm/blob/main/docs/adr/0059-value-expressions-are-lambdas-over-a-closed-function-table-under-value-parity.md) records why the function table is closed, and [ADR 0058](https://github.com/syn54x/ferro-orm/blob/main/docs/adr/0058-a-generated-columns-value-comes-only-from-the-database.md) why the value is never computed in Python.

### Changing a generated column

Adding a generated column, changing its expression, or turning it into a plain column are schema changes like any other. [Auto-migrate](schema/auto-migrate.md) and [migrations](schema/migrations.md) say which ones each database runs in place and which rebuild the table on SQLite. A generated column needs no backfill: the database computes it for every existing row. `ferro migrate drift` reports an expression edited by hand as `transaction.direction generated expression differs`.

## Custom table names

A model's table is its class name in lowercase: `class Product` is `product`. Set `__ferro_table__` in the class body to choose the name yourself, as [`Category`](#natural-keys) does:

```python
--8<-- "docs/examples/models_and_fields.py:table-name"
```

Every statement Ferro writes for the model uses the name you set:

```sql
--8<-- "docs/examples/models_and_fields.py:category-ddl"
```

A `ForeignKey` to `Category` renders `REFERENCES "categories" ("code")`. A default many-to-many join table is named from the source model's table and the relation's field: a `tags` relation on a model stored in `wiki_pages` gets the join table `wiki_pages_tags`.

- `__ferro_table__` applies only to the class that declares it; a subclass gets the default name unless it sets its own.
- The name is an identifier of 1 to 63 characters matching `[A-Za-z_][A-Za-z0-9_]*`.
- Two models cannot share one table. Defining a second model that resolves to a table name already in use raises at class definition and names both models. Redefining the same class, in a REPL or a re-imported module, is allowed.

## Validation

A model validates its fields whenever an instance is built, including inside `create()` and before `bulk_create()` sends anything. `Product.price` must not be negative:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:validation-fields"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:validation-fields"
    ```

A negative price raises Pydantic's `ValidationError` before Ferro writes the row:

```python
--8<-- "docs/examples/models_and_fields.py:validation"
```

The error names the field and the rule it broke. `@field_validator`, `@model_validator` and `model_config` settings such as `validate_assignment=True` work as they do in Pydantic.

## Fields on a model base class

A class that subclasses `Model` is a model, with a table of its own, even when you meant it only as a base for others. Here `Timestamped` declares `created_at` for `Review` to inherit:

=== "Assignment"

    ```python
    --8<-- "docs/examples/models_and_fields.py:base-class"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/models_and_fields_annotated.py:base-class"
    ```

`create_tables()` creates two tables. `review` has the inherited column, and `timestamped` is a second table with only `created_at`:

```sql
--8<-- "docs/examples/models_and_fields.py:base-class-ddl"
```

The subclass works: its rows go to `review`, and the inherited default fills `created_at`. Nothing is written to `timestamped`:

```python
--8<-- "docs/examples/models_and_fields.py:base-class-rows"
```

The `timestamped` table is the cost. Every migration strategy treats `Timestamped` as a model, so auto-migrate, a migration and the Alembic bridge all create its table, though no code stores a row there. To share fields without that table, declare the fields on each model and share behaviour through a plain class that does not subclass `Model`; [Timestamps](../howto/timestamps.md) and [Soft deletes](../howto/soft-deletes.md) show the pattern.

## Field options

`Field()` takes these database options, beside every Pydantic option (`min_length`, `max_length`, `pattern`, `gt`, `ge`, `lt`, `le`, `decimal_places`, `description` and the rest):

| Option | Default | What it does |
| :--- | :--- | :--- |
| `primary_key` | `False` | Makes the column the primary key; see [Primary keys](#primary-keys). |
| `autoincrement` | `None` | `None` turns it on for an integer primary key; `False` turns it off. |
| `unique` | `False` | Adds a single-column unique index; see [Uniques and indexes](#uniques-and-indexes). |
| `index` | `False` | Adds a single-column index. |
| `nullable` | `"infer"` | `"infer"` follows whether the annotation allows `None`; `True` or `False` forces it. |
| `default`, `default_factory` | none | Pydantic's default. [Auto-migrate](schema/auto-migrate.md) also uses a literal default to fill existing rows when it adds a `NOT NULL` column. |
| `db_type` | `None` | Overrides the column type; see [Column type overrides](#column-type-overrides-with-db_type). |
| `db_check` | `False` | Adds a `CHECK` for a closed set of values; needs `db_type`. |
| `renamed_from` | `None` | The column's previous name; see [Rename hints](schema/renames-and-retirements.md#rename-hints). |
| `retired` | `False` | Keeps the column in the schema after the field leaves the model; see [Retiring a column](schema/renames-and-retirements.md#retiring-a-column). |

The full signature is in the reference: [`Field`](../api/fields.md#ferro.Field), [`FerroField`](../api/fields.md#ferro.FerroField) and [`DbType`](../api/fields.md#ferro.DbType).

## See also

- [Relationships](relationships.md): foreign keys, one-to-many and many-to-many.
- [Queries](queries.md): fetching and filtering rows.
- [Mutations](mutations.md): creating, updating and deleting rows.
- [Schema management](schema/overview.md): how fields become DDL, and how a change reaches the database.
- [`Model`](../api/model.md) in the API reference.
