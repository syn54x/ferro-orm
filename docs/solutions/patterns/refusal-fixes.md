---
title: Adding a declaration refusal whose fix is a list of edits
type: pattern
tags: [python, relationships, validation, convention, pytest]
related_files:
  - src/ferro/relations/fixes.py
  - src/ferro/relations/__init__.py
  - src/ferro/metaclass.py
  - tests/test_declaration_fixes.py
  - docs/adr/0064-a-refusals-fix-is-a-list-of-edits.md
related_issues: [874, 893, 898]
captured: 2026-11-08
---

## Problem

A refusal that formats its own fix text drifts from the fix its test proves:
five gate rounds on #893 each found a printed fix that did not work when
followed.

## Takeaway

Compute the fix as `Edit`s, raise `DeclarationRefusal`, and let
`render_fix` print it. Never write `Write:` or numbered steps by hand.

## Recipe

Say a new check refuses `Player.team_id` and the fix rewrites that line and
the `Forward` reading it:

```python
from .fixes import DeclarationRefusal, Edit

line = key_column_line(model_cls, name, target, nullable=True, meta=meta)
raise DeclarationRefusal(
    model,
    name,
    'on_delete="SET NULL" needs a nullable column',  # the reason, no full stop
    [
        Edit(model, name, "replace", (line,)),
        Edit(model, "team", "replace", (relation_line(...),)),
    ],
)
```

The message comes out as numbered steps because the second edit is not on
the refused field. With only the first edit it would print `Write: <line>`.

1. **Build each line with the builders** (`key_column_line`,
   `relation_line`, `field_line`), passing `bound` for the class the line is
   pasted in: the relation's own `bound` for its class, `ON_ANOTHER_MODEL`
   for any other model.
2. **Choose the action from what the edit does to that class**: `replace`
   an existing field, `add` a field it lacks (or a whole model, with
   `field=""` and the class body as `lines`), `remove` a field.
3. **A fix that gives a model a primary key** uses
   `_primary_key_edits(model_cls, needing)`, which takes the needing
   column's type and storage and retypes every known key column the new key
   would not back.
4. **A refusal that cannot name one fix** (the reader picks between models,
   or nothing names the model) passes no edits and says what to do in the
   reason. I-6 still applies: the words name the fix.
5. **Add a case to `_cases()` in `tests/test_declaration_fixes.py`** if no
   existing case reaches the new path. Say whether it is `local` (a
   `Write:` line) and what `kind` of relation the fixed models must give.
   The sweep applies the edits over every storage, order and `__future__`
   mode.

## How to recognize

- A string containing `"Write: "` or `Fix it by hand` outside
  `src/ferro/relations/fixes.py`.
- A test that parses a refusal message to find the line to paste, instead
  of reading `refusal.edits`.
- A refusal raised as a plain `TypeError` with a declaration line in its
  text.
