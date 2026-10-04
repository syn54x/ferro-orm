# A data step declares its shape, and an unwritten one is a `todo()` read at load

Adding a required `slug` to `Author` scaffolds this data step between the expand and the contract:

```python
# migrations/0008_author_slug/02_backfill_author.py
from ferro.migrations import chunked, nothing_to_reverse, todo

@chunked(
    lambda models: models.Author.where(lambda author: author.slug == None)
                                .order_by(lambda author: author.id),
    batch_size=1000,
)
async def up(ctx, batch):
    for author in batch:
        author.slug = todo("the slug for an existing author; 03_contract makes it NOT NULL")
        await author.save()

@nothing_to_reverse("01_expand.down.sql drops the column")
def down(ctx): ...
```

`migrate up` refuses the migration before running anything:

```text
0008/02_backfill_author.py:11: not written yet: the slug for an existing author; 03_contract makes it NOT NULL
```

Three decisions are in that file.

**Every `up` and `down` carries exactly one declaration**: `@atomic`, `@chunked(query, batch_size=…)`, or, on a `down` only, `@irreversible("…")` or `@nothing_to_reverse("…")` (ADR-0033). An undecorated function and a function with two are refused at load. `batch_size` is required. The two directions are independent: a chunked `up` may have an atomic `down`, and a chunked `down` declares its own query. A function the runner calls is `async def`; `@atomic` takes `(ctx)` and `@chunked` takes `(ctx, batch)`, a non-empty list of historical instances. All names import from `ferro.migrations`.

**The part only a person can supply is `todo("…")`, in the position of the missing value.** The loader reads the step file's syntax tree, resolves the name through its import from `ferro.migrations`, and refuses the migration with the file, line and message. The message is required. If a call is reached at run time anyway, it raises. A hand-requested data step (`migrate new --data`) starts as `@atomic` `up` and `down`, each holding `todo("write this step")` as a bare statement.

**`ctx`, the step context, is small and closed.** It carries:

- `ctx.models.Author`: historical models by class name; `ctx.models.table("post_tags")` reaches any table in the union by table name, which is how a join table is reached. A missing name raises and lists what the union holds. The `@chunked` lambda receives this same namespace.
- `ctx.execute` / `ctx.fetch_all` / `ctx.fetch_one`: ferro's raw API without `using`, `session` or `autocommit`, on the step's transaction.
- `ctx.dialect`: the target dialect token, for raw SQL that must branch.
- `ctx.log`: a stdlib logger named for the step.

It has no `transaction()`, no batch counters, no database name, migration number or direction, and no path to today's models. A nested `ferro.transaction()` inside a step is a savepoint, as in application code. Querying a class imported from the codebase raises and names `ctx.models.<Name>`.

## Considered options

- **Undecorated means atomic** (the default ADR-0024 named). Rejected: a reader of an undecorated `up` has to know the default to know what happens on a crash. With the decorator required, the file says it.
- **`not_written()`** (the name the casebook used). Rejected for the name: `author.slug = not_written()` reads as a sentinel value being assigned. `todo("…")` cannot be read as a value, and the required message says what is missing. Rust's `todo!()`, Kotlin's `TODO("…")` and Scala's `???` are the same shape.
- **A bare marker statement with the body commented out.** Rejected: the scaffold stops being code an editor or type checker reads.
- **A `@not_written` decorator.** Rejected: it stacks on the shape decorator, and removing it while forgetting the body runs a step that saves nothing or `...`.
- **`raise NotImplementedError`.** Rejected for ADR-0033's reason: a raise exists only once the function runs.
- **A `# ferro: not-written` header.** Rejected: Python steps declare through decorators; a comment vocabulary would be a second one and cannot point at the hole.
- **Deliberately invalid Python.** Rejected: it breaks formatters and editors across the migrations directory and the refusal is a `SyntaxError`.
- **`ctx.transaction()`.** Rejected: the runner owns the step's transaction (ADR-0024), and the verb suggests the step can commit it.
- **An escape hatch to today's models.** Rejected: a data step that imports today's class breaks when the class changes, the failure Rails and Django both document. A helper the step needs is copied into the step file.
- **Building a historical enum from the live catalog** when the live type holds a label neither snapshot declares. Rejected: that label is drift, and the step would run against a schema nobody reviewed. Hydration fails and names the label and type.

## Consequences

- The runner reports chunked progress from the cursor and row count it already commits. The cursor advances over every row handed to the function, saved or not; a skipped row is caught by the contract step that follows.
- A join table has no single-column key, so it cannot be the driving query of a chunked step; `@chunked` refuses it at load and the step drives from a parent model. Lifting that waits on keyset paging over a composite key (#492).
- An `Any`-typed column always carries an explicit `db_type` (the emitters refuse it otherwise). Its historical annotation is `Any` with the same `db_type`, and its value is the wire-close primitive the raw API returns. Every explicit `db_type` token is pinned end to end through a historical model.
- The registry gains a first-class scoped swap that installs a historical modelset and restores today's on exit, error and cancellation. It swaps once per migration that has data steps, since the union is a property of the migration: two installs through the fingerprint gate, none for a DDL-only migration.
- A project overrides the hand-requested skeleton by file: a `data_step.py` in a templates directory beside the migrations, used verbatim with a fixed set of `{placeholders}`. Generator scaffolds are derived, never templated.
- ADR-0033's examples predate this ADR: a declared `down` needs no second decorator, and a `down` that does work is `async def` under `@atomic` or `@chunked`.
