---
title: Historical models go through the metaclass, into a registry of their own
type: pattern
tags: [python, bridge, migrations, pydantic, gotcha, invariant]
related_files:
  - src/ferro/migrations/historical.py
  - src/ferro/migrations/context.py
  - src/ferro/registry.py
  - src/ferro/migrations/runner.py
related_issues: [530, 471]
captured: 2026-10-07
---

# Historical models go through the metaclass, into a registry of their own

A data step queries `ctx.models.Author`, a class ferro builds from two
`ir.json` snapshots, never the `Author` in the codebase today:

```python
@atomic
async def up(ctx):
    for author in await ctx.models.Author.where(lambda author: author.slug == None).all():
        author.slug = author.name.lower()
        await author.save()
```

## How a historical class is made

`historical.build(parent_ir, own_ir, rev=...)` unions the two snapshots
(ADR-0025) and builds one class per table with `ModelMetaclass` itself, from
a synthesized namespace: an annotation per column (the reverse of
`ferro.columns._logical_type`), a `Field(primary_key=, autoincrement=,
db_type=)` per column, `__ferro_table__`, and `__module__ =
"ferro.migrations.historical.<rev>"`. Going through the metaclass is what
keeps I-2 true: hydration, the identity key and the `__pydantic_*__` slots
work exactly as for a declared model, and nothing constructs instances by
hand.

Every class is then compiled back and checked against its union
(`_check_compiled`): a column whose logical type, `db_type`, enum type,
primary key or nullability does not round-trip is a ferro bug and raises.

## Gotchas

- **`model_config` must be overridden.** `Model` sets
  `use_attribute_docstrings=True`, and Pydantic then calls
  `inspect.getsource` on the class. A class built from a namespace has no
  source, so class creation dies with `TypeError: ... is a built-in class`.
  Historical classes set `use_attribute_docstrings=False`.
- **The metaclass registers on class creation.** Built against today's
  registry, historical classes would collide with today's tables (two models
  claiming `author`). `Registry.capture(define)` runs `define` against an
  empty registry and returns the state it left; today's is restored in a
  `finally`. That captured `RegistrySnapshot` is what `Registry.swap`
  installs.
- **A JSON column is `dict[str, Any] | list[Any]`.** One annotation has to
  compile back to logical type `json` and accept both shapes, with or
  without `db_type="jsonb"`. `_is_json_family` therefore accepts a union
  whose every member is json-family. An `Any` column is `Any` with its
  `db_type` (ADR-0035), so `db_type_is_compatible` accepts `Any` for every
  token.
- **The enum is the union of both snapshots' labels**, built on
  `_HistoricalEnum`, whose `_missing_` names the label and the type. A live
  label neither snapshot declares fails hydration (Rust calls
  `enum_cls(value)`) and is never rebuilt from the catalog.

## The swap

`with REGISTRY.swap(models):` installs the historical registry state in
Python and pushes its modelset to Rust (`_install_registration`, behind the
fingerprint gate). It restores today's state on exit, error and
cancellation: re-installed when today's registry has an assembled modelset,
cleared otherwise. While the swap is open, every public entry point
`ferro.models.Model` defines (`where`, `select`, `all`, `get`, `create`,
`save`, ...) is shadowed on each of today's classes by a descriptor that
raises `SwappedOutModelError` naming `ctx.models.<Name>`. Without it, today's
`Author` would hit a Rust registry that no longer knows it and fail with a
bare "model not found" (or, worse, query a table shape the step never saw).
The runner swaps once per migration that has data steps and holds the swap
until the run leaves that migration.

Store-write guard: `tests/test_registry_entrypoints.py` flags any attribute
named `_models`, `_envelopes` or `_fingerprints` assigned outside
`registry.py`, so data-step code names its fields otherwise
(`_historical`, `_installed`).
