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

> 2026-11-09, checked on Python 3.14.5: this story is history. The
> `ForeignKey` annotation it was found with was removed by #874 Explicit
> foreign-key columns, with relations as typed views, and its successor does
> not swallow the error. `org_id: Annotated[int, Field(foreign_key=Org,
> bogus=True)]` fails at class definition, with or without
> `from __future__ import annotations`, and the traceback carries
> `TypeError: Field() got an unknown keyword 'bogus'`. Keep the lesson for
> any other constructor called inside a deferred annotation.

## Problem

Python 3.14 evaluates annotations lazily. A relation written as the
`ForeignKey` annotation, `org: Annotated[Org, ForeignKey ...]` with a keyword
the constructor did not accept yet (`index=` before it existed), had its
`ForeignKey` call _deferred_. Pydantic / Ferro reached into
`__annotations__` to evaluate it later, and at that point the `TypeError`
raised by the constructor ("unexpected keyword argument 'index'") was
**swallowed** by the deferred-annotation machinery and converted into a
"this field has no metadata" outcome.

The downstream symptom was **not** the `TypeError` you'd expect. The
foreign-key column never appeared and you saw something like:

```
KeyError: 'org_id'
# or
RuntimeError: Model 'Project' defines a relationship to 'int'
```

…which suggested the relationship machinery itself was broken, when in
reality the constructor in the annotation never finished running.

## Takeaway

When debugging "the foreign-key column / relationship resolution is broken"
on Python 3.14:

1. Don't trust the surface error. Add a `print(...)` inside the constructor
   the annotation calls and see if it ever runs.
2. Call that constructor outside of an annotation, with the same
   arguments. This forces eager evaluation, and the real `TypeError`
   surfaces here.
3. Once you see the real error, fix it and the deferred-annotation symptom
   disappears.

## How to recognize

- Tests fail with `KeyError: '<column>'` or `RuntimeError: Model 'X'
  defines a relationship to 'int'` after you added a new kwarg to a
  constructor called inside an annotation.
- The same code works on Python 3.13 but not 3.14.
- Adding the kwarg to `__init__` makes the symptom go away — that's because
  the deferred annotation now succeeds, not because the original error was
  meaningful.

## Why this matters for TDD

This trips the TDD red→green→refactor loop. The "red" test fails, but for the
wrong reason, which makes "green" hard to recognize. Always sanity-check the
red phase by reading the actual exception type and asking "is this what I
predicted?" If not, dig before implementing.
