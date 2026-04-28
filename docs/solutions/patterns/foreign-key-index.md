---
title: ForeignKey(index=True) for join performance
type: pattern
tags: [schema, relationships, performance]
related_files:
  - src/ferro/base.py
  - docs/guide/relationships.md
related_issues: [32]
related_prs: [36]
captured: 2026-04-28
---

## Problem

Foreign-key columns are not indexed by default in any major SQL database
(Postgres, MySQL InnoDB, SQLite). Joins and filters against an unindexed FK
fall back to sequential scans, which is fine for ten rows and catastrophic for
ten million.

The classic case: a multi-tenant table with `tenant_id` as a ForeignKey, and
every query in the application filters on `tenant_id`. Without an index, every
query scans the whole table and rejects 99% of rows.

## Takeaway

Use `ForeignKey(index=True)` on FK columns that are used for filtering, joining,
or ordering — which in practice is most of them in a well-normalized schema.

```python
class Project(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    org: Annotated[Org, ForeignKey(related_name="projects", index=True)]
```

This emits `idx_project_org_id` in both Alembic autogen and Rust runtime DDL.

## When to use

- Tenant-scoped tables (`tenant_id`, `organization_id`, `user_id`).
- Hot lookup paths (`session.user_id`, `comment.post_id`).
- Anything you'll join, filter, or sort on.

## When NOT to use

- The FK is also marked `unique=True` — uniqueness already creates an index.
  Ferro warns and strips `index=True` in this case
  (see `index-unique-redundancy.md`).
- The FK is to a tiny lookup table that you never filter on (e.g. a `country`
  FK on a row that's only ever fetched by primary key).
- You expect heavy write traffic and few reads — index maintenance has cost.

## Migration considerations

- Adding an index to an existing large Postgres table without `CREATE INDEX
  CONCURRENTLY` will lock the table. Wrap large rollouts in a manually-edited
  Alembic migration that uses `op.create_index(..., postgresql_concurrently=True)`
  inside `with op.get_context().autocommit_block():`.
- SQLite and MySQL are generally fine because their lock-on-create is cheap or
  bounded.
