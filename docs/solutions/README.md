# docs/solutions/

Institutional memory for Ferro. Future contributors (human and AI) search this
directory before starting non-trivial work to learn from what's already been
solved, debugged, or decided.

## Layout

- `patterns/` — design patterns, architectural decisions, conventions that span
  more than one file.
- `issues/` — debugging stories. "I hit X, the actual cause was Y, here's how to
  recognize it next time."

## Frontmatter

Every doc starts with YAML frontmatter so agents can filter:

```yaml
---
title: Short human title
type: pattern | issue
tags: [list, of, tags]
related_files:
  - src/path/to/file.py
  - src/other/path.rs
related_issues: [32, 41]
related_prs: [36]
captured: 2026-04-28
---
```

Tags should be terse and reusable. Common ones:

- Layer: `python`, `rust`, `bridge`, `ffi`
- Domain: `schema`, `migrations`, `relationships`, `validation`
- Tooling: `pydantic`, `sqlalchemy`, `alembic`, `pyo3`, `sea-query`, `pytest`,
  `maturin`
- Class: `convention`, `gotcha`, `invariant`, `performance`

## Voice

Write for the agent who hits this in 6 months and has 30 seconds.

- Lead with the problem in one sentence.
- Then the takeaway in one sentence.
- Then the explanation, with code links where it lives in the codebase.
- End with a "How to recognize" or "When to apply" section.

## When to add

- You hit a footgun that cost you more than 15 minutes.
- You made a non-obvious architectural decision and want it sticky.
- You discovered a convention by reading the codebase and want to lock it in.
- A code review surfaced a pattern that should be reused.

## When NOT to add

- One-off fixes with no general lesson.
- Style preferences (those go in `.cursorrules`).
- API documentation (that goes in `docs/guide/` or docstrings).
