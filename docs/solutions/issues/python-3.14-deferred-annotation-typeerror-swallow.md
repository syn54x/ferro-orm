---
title: Python 3.14 swallows TypeError from invalid kwargs in annotated metadata
type: issue
tags: [gotcha, python, pydantic, validation, ffi]
related_files:
  - src/ferro/base.py
related_issues: [32]
related_prs: [36]
captured: 2026-04-28
---

> 2026-11-09: the `ForeignKey` annotation this story was found with was
> removed by #874 Explicit foreign-key columns, with relations as typed views.
> Its successor, `Field(foreign_key=...)` inside `Annotated[...]`, is
> deferred the same way, so the lesson stands.

## Problem

Python 3.14 evaluates annotations lazily. When you write option metadata
inside an annotation:

```python
class Project(Model):
    org_id: Annotated[int, Field(foreign_key=Org, index=True)]
```

…the `Field(...)` call is _deferred_. Pydantic / Ferro reaches into
`__annotations__` to evaluate it later, and at that point any `TypeError`
raised by the constructor (e.g. "unexpected keyword argument 'index'") gets
**swallowed** by the deferred-annotation machinery and converted into a
"this field has no metadata" outcome.

The downstream symptom is **not** the `TypeError` you'd expect. With the
`ForeignKey` annotation, the foreign-key column never appeared and you saw
something like:

```
KeyError: 'org_id'
# or
RuntimeError: Model 'Project' defines a relationship to 'int'
```

…which suggests the relationship machinery itself is broken, when in reality
the constructor in the annotation never finished running.

## Takeaway

When debugging "the foreign-key column / relationship resolution is broken"
on Python 3.14:

1. Don't trust the surface error. Add a `print(...)` inside the constructor
   the annotation calls (`FerroField.__init__`) and see if it ever runs.
2. Manually construct the metadata outside of an annotation:
   ```python
   _ = Field(foreign_key=Org, index=True)
   ```
   This forces eager evaluation. The real `TypeError` will surface here.
3. Once you see the real error, fix it and the deferred-annotation symptom
   disappears.

## How to recognize

- Tests fail with `KeyError: '<column>'` or `RuntimeError: Model 'X'
  defines a relationship to 'int'` after you added a new kwarg to
  `Field` or `FerroField`.
- The same code works on Python 3.13 but not 3.14.
- Adding the kwarg to `__init__` makes the symptom go away — that's because
  the deferred annotation now succeeds, not because the original error was
  meaningful.

## Why this matters for TDD

This trips the TDD red→green→refactor loop. The "red" test fails, but for the
wrong reason, which makes "green" hard to recognize. Always sanity-check the
red phase by reading the actual exception type and asking "is this what I
predicted?" If not, dig before implementing.
