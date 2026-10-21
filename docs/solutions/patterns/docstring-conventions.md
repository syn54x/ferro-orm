---
title: Docstring conventions for the rendered API reference (I-14)
type: pattern
tags: [docs, python, convention, invariant, mkdocstrings, griffe]
related_files:
  - AGENTS.md
  - src/ferro/
  - docs/examples/_preludes/reference.py
  - tests/test_docstring_examples.py
  - zensical.toml
related_issues: [698, 703, 704, 719, 720, 721]
related_prs: []
captured: 2026-10-19
---

## Problem

The API reference is rendered from docstrings, and the 2026 audit found it
showing about 280 literal `:class:` roles, a `Returns:` section split into
five broken table rows, about 180 members with no docstring, a `#294` line
rendered as a heading, and 90-line essays opening `connect` and `PoolConfig`.

## Takeaway

A docstring is a short Google-style reference entry: a one-line summary,
every parameter in `Args:`, Markdown autorefs, references the reader can
follow, a runnable lambda-style example, and a link for anything longer than
a paragraph. AGENTS.md I-14 holds the rules; this note shows each as a
before/after pair.

## The rules, before and after

### Google style as griffe parses it

griffe recognises a section by its title and a colon, with entries indented
under it. A `Returns:` written as prose lines becomes table rows, one per
line.

Before:

```python
"""...

Returns:
    PassReport with:
    - statements: what ran
    - warnings: what was skipped
"""
```

After:

```python
"""...

Returns:
    The statements the pass ran and the warnings it raised, as a
    [`PassReport`][ferro.PassReport].
"""
```

### A one-line summary in the infinitive, ending with a period

A function or method summary starts with a verb in the infinitive. A class
summary is a noun phrase.

Before:

```python
"""Returns the first matching record or None if the query has no rows"""
"""This class represents a lazily-evaluated query that ..."""
```

After:

```python
"""Return the first matching record, or `None` when there is none."""
"""A lazy query over one model's rows."""
```

### Every parameter in `Args:`

Keyword-only parameters such as `session`, `using` and `nulls` belong in the
table too, with the default when the signature does not make it obvious.

Before:

```python
async def get(cls, pk: Any, *, session: Session | None = None) -> Self:
    """Fetch one record by primary key.

    Args:
        pk: Primary key value.
    """
```

After:

```python
async def get(cls, pk: Any, *, session: Session | None = None) -> Self:
    """Fetch one record by primary key.

    Args:
        pk: The primary key value to look up.
        session: The session to run in. Defaults to the one opened by the
            enclosing `async with engines.session():`.
    """
```

### A one-line docstring on every public dataclass or Pydantic field

griffe reads a string literal on the line below a field as its docstring.
Without one, the field renders as a bare name.

Before:

```python
@dataclass(frozen=True)
class PassReport:
    statements: tuple[ExecutedStatement, ...]
    warnings: tuple[Report, ...]
```

After:

```python
@dataclass(frozen=True)
class PassReport:
    """What one reconciliation pass ran and reported."""

    statements: tuple[ExecutedStatement, ...]
    """Every statement the pass sent, in execution order."""
    warnings: tuple[Report, ...]
    """Every warning the pass raised, in the order raised."""
```

### Markdown autorefs, never Sphinx roles

Sphinx roles render as literal text in mkdocstrings.

Before:

```python
"""Use :meth:`get_or_none` to get ``None`` instead. See :class:`~ferro.Model`."""
```

After:

```python
"""Use [`get_or_none`][ferro.Model.get_or_none] to get `None` instead.
See [Model][ferro.Model]."""
```

### No ADR numbers, issue numbers, codenames or Rust internals

The reader cannot follow `ADR-0007`, `#412` or a Rust function name.
Say what the behaviour is, and link the docs page that explains why.

Before:

```python
"""Test membership with ``t.posts.exists()``; negate it with
``~t.posts.exists()`` (ADR-0007, #412), lowered by ``render_exists_subquery``."""
```

After:

```python
"""Test membership with `author.posts.exists()`; negate it with
`~author.posts.exists()`."""
```

### Examples in the lambda style, against the reference prelude

Examples use lambda predicates named after the model (I-8) and the shared
`reference` prelude (`Author` and `Post`, connected, inside a session). The
fence marker `{test=run prelude=reference}` makes the docs harness run it.

Before:

```python
"""Examples:
    >>> users = await User.where(lambda u: u.age >= 18).all()
"""
```

After:

````python
"""Examples:
    ```python {test=run prelude=reference}
    authors = await Author.where(lambda author: author.id <= 2).all()
    print([author.id for author in authors])
    ```
"""
````

### Explanations longer than a paragraph live in a guide

A docstring states what the object does and how to call it. The reasoning,
trade-offs and walk-throughs go on a guide or concepts page, and the
docstring links there.

Before: `connect()` opening with 90 lines on pools, routing and migration
modes.

After:

```python
"""Open a connection pool and register it as a database route.

See [Connections](../guide/connections.md) for routing and pool sizing.

Args:
    ...
"""
```

### A docstring link to a guide heading is checked by the site test, not the build

`zensical build --strict` reports a dead anchor in a docs page, but not in a
link that mkdocstrings renders from a docstring. Three docstrings linked guide
headings that had been renamed (`#composite-constraints`,
`#overriding-the-column-type-with-db_type`,
`#every-traversed-hop-is-an-inner-join`) and the strict build stayed green.
`test_links_from_api_pages_land_on_existing_anchors` in
`tests/test_docs_site.py` reads every link on the built API pages and fails on
one whose anchor does not exist. Before renaming a guide heading, grep
`src/ferro` as well as `docs/pages` for its anchor.

### Every export has one entry, and nothing renders empty

`test_every_export_has_an_api_entry` checks that each name in `ferro.__all__`,
`ferro.query.__all__`, `ferro.migrations.__all__` and
`ferro.migrations.testing.__all__` has exactly one entry on the API pages,
under its public path (`::: ferro.query.Query`, never
`::: ferro.query.builder.Query`). A type a public call returns but no
`__all__` exports (`RunReport`, `ModelConnection`) is listed in
`RETURNED_TYPES` there and documented under its defining module.
`show_if_no_docstring` is off, so a public member without a docstring would
vanish from the reference without a warning;
`test_every_public_object_and_member_has_a_docstring` reads the package with
griffe and fails naming it.

## How to recognize

- Literal `:class:` or `:meth:` text on a rendered API page.
- A `Returns:` or `Attributes:` table whose rows are sentence fragments.
- A field listed with no description, or a parameter in the signature missing
  from the table.
- A summary that wraps, starts with "Returns" or "This", or has no period.
- `lambda t:` or a doctest `>>>` in an `Examples:` section.
