---
title: pytest-examples ignores bare fence settings like {test=run}
type: issue
tags: [pytest, docs, gotcha]
related_files:
  - tests/_docs_harness.py
  - tests/test_docs_examples.py
  - docs/examples/_preludes/__init__.py
related_issues: [704]
captured: 2026-10-19
---

A docs fence marked ` ```python {test=run} ` never ran, because
`CodeExample.prefix_settings()` from pytest-examples only reads quoted values
(`test="run"`).

Read fence settings with `fence_settings()` in `tests/_docs_harness.py`
instead. It accepts both forms:

```python
fence_settings(example)  # 'python {test=run prelude=reference}'
# {'test': 'run', 'prelude': 'reference'}
```

The docs harness used `prefix_settings()` from the start, so the
`{test=run}` and `{lint=check}` opt-ins it documented did nothing for the bare
spelling. No page used them, so nothing failed. Every other check in the
harness reads settings through `fence_settings()`, which also refuses a
`prelude=` without `test=run`.

## How to recognize

A fence carries a setting and its test passes, but deliberately breaking the
block also passes. Print `example.prefix_settings()`: an empty dict for a
prefix that clearly has `key=value` pairs is this bug.
