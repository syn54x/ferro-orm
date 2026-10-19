---
title: A runnable docs block that declares a model inherits the runner's context
type: issue
tags: [docs, python, pydantic, pytest, gotcha]
related_files:
  - docs/examples/_preludes/__init__.py
  - tests/test_docstring_examples.py
  - src/ferro/metaclass.py
related_issues: [704, 719]
related_prs: []
captured: 2026-10-19
---

## Problem

A `{test=run prelude=...}` block that declared its own model failed in two
ways that had nothing to do with the block. Declaring any model raised
`SyntaxError` from pydantic, and a model with a `ForeignKey` got a `JSON`
column named after the relation instead of a `<field>_id` foreign key.

## Takeaway

The runner compiles and runs each block, and anything the runner's own
module carries leaks into the block unless it is cut off. Run the block as a
module of its own whose `__file__` is the page or docstring, and compile it
with `dont_inherit=True`.

## What happened

- **`SyntaxError`**: `Model` sets `use_attribute_docstrings=True`, so pydantic
  reads each model's source with `inspect.getsourcelines(cls)`. The block ran
  with `__name__ == "__main__"`, so the source lookup found the runner file
  and parsed its docstring as the class body.
- **`JSON` column**: `compile()` inherits the caller's `__future__` flags.
  The runner has `from __future__ import annotations`, so every annotation in
  the block became a string, and the metaclass never saw the `ForeignKey`
  inside `Annotated[...]`.

Ferro has the same gap outside the harness: a module with
`from __future__ import annotations` that declares
`author: Annotated[Author, ForeignKey(related_name="posts")]` gets a `JSON`
column `author` and no error. Until the metaclass resolves string
annotations, keep that import out of model modules.

## How to recognize

- `SyntaxError` with a line of English prose from
  `pydantic/_internal/_docs_extraction.py`.
- `CREATE TABLE ... "<relation>" JSON` where a `"<relation>_id"` column and a
  `FOREIGN KEY` constraint belong.
