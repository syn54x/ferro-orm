---
title: A docstring example sees a loaded relation because the reference prelude holds its rows
type: issue
tags: [python, relationships, pytest, gotcha]
related_files:
  - docs/examples/_preludes/reference.py
  - src/ferro/exceptions.py
  - src/ferro/models.py
related_issues: [878]
related_prs: []
captured: 2026-11-08
---

# A docstring example sees a loaded relation because the reference prelude holds its rows

**Problem:** an example meant to show an unloaded instance (`post.author.name`
raising `InstanceNotLoaded`) runs green in your head and fails under
`tests/test_docstring_examples.py`: `post.author` is already the complete
`Author`.

**Takeaway:** the `reference` prelude creates `ada` and `grace` inside the
session it holds open around the block, and keeps them alive, so the session's
identity map hands a relation read the complete instance. Open a session of
your own inside the example to start from an empty map.

## Explanation

`opened()` in `docs/examples/_preludes/reference.py` runs
`ada = await Author.create(...)` and then `yield`s with `ada` still a local of
the suspended generator. The identity map holds instances weakly, so `ada`
stays registered for the whole block. A forward relation's first read looks
its key up in the map (ADR-0063) and returns that complete `ada`: no
unloaded instance, no refusal, and `.load()` sends nothing.

A nested `async with engines.session():` gets its own map:

```python
async with engines.session():  # a session that holds no author yet
    post = await Post.get(1)
    post.author.name           # InstanceNotLoaded
```

`InstanceNotLoaded`'s and `Model.load()`'s examples do this.

## How to recognize

An example that reads a relation expecting a query or a refusal sees neither,
or a `sql` block expecting a `SELECT` for `.load()` finds none. Ask whether the
code before the region (a prelude, or an earlier `create()` whose result is
still in a variable) holds the target row in the same session.
