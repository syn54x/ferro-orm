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

## Problem

Python 3.14 evaluates annotations lazily. When you write:

```python
class Project(Model):
    org: Annotated[Org, ForeignKey(related_name="projects", index=True)]
```

…the `ForeignKey(...)` call is _deferred_. Pydantic / Ferro reaches into
`__annotations__` to evaluate it later, and at that point any `TypeError`
raised by the constructor (e.g. "unexpected keyword argument 'index'") gets
**swallowed** by the deferred-annotation machinery and converted into a
"this field has no metadata" outcome.

The downstream symptom is **not** the `TypeError` you'd expect. Instead the
shadow column never gets injected and you see something like:

```
KeyError: 'org_id'
# or
RuntimeError: Model 'Project' defines a relationship to 'int' with related_name='projects'
```

…which suggests the relationship machinery itself is broken, when in reality
the `ForeignKey` constructor never finished running.

## Takeaway

When debugging "the FK shadow column / relationship resolution is broken" on
Python 3.14:

1. Don't trust the surface error. Add a `print(...)` inside `ForeignKey.__init__`
   and see if it ever runs.
2. Manually construct the `ForeignKey(...)` outside of an annotation:
   ```python
   _ = ForeignKey(related_name="projects", index=True)
   ```
   This forces eager evaluation. The real `TypeError` will surface here.
3. Once you see the real error, fix it and the deferred-annotation symptom
   disappears.

## How to recognize

- Tests fail with `KeyError: '<field>_id'` or `RuntimeError: Model 'X'
  defines a relationship to 'int'` after you added a new kwarg to
  `ForeignKey` or `FerroField`.
- The same code works on Python 3.13 but not 3.14.
- Adding the kwarg to `__init__` makes the symptom go away — that's because
  the deferred annotation now succeeds, not because the original error was
  meaningful.

## Why this matters for TDD

This trips the TDD red→green→refactor loop. The "red" test fails, but for the
wrong reason, which makes "green" hard to recognize. Always sanity-check the
red phase by reading the actual exception type and asking "is this what I
predicted?" If not, dig before implementing.
