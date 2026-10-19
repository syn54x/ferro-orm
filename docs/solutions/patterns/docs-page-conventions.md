---
title: Docs page conventions (kind, running example, results, session, SQL, glossary, Annotated twins)
type: pattern
tags: [docs, convention, zensical, pydantic]
related_files:
  - AGENTS.md
  - docs/examples/
  - docs/includes/abbreviations.md
  - tests/test_docs_examples.py
  - zensical.toml
related_issues: [698, 700, 701, 703, 704, 725]
related_prs: []
captured: 2026-10-19
---

## Problem

The 2026 docs audit found pages a reader could not copy from: snippets that
stop just above the answer, three to fourteen unrelated models per page, no
session wrapper anywhere, no SQL, and Annotated tabs that differ from their
Assignment twins in more than declaration style.

## Takeaway

A page is one kind, uses one running example, shows every result, shows the
session it runs in, and shows the SQL Ferro sends. Annotated twins use one
idiom: `name: Annotated[T, Field(...)] = default`.

## The conventions

### One kind per page

Each page is a tutorial, a how-to, a reference or an explanation, and opens
with one sentence saying what it covers, then an example on the first screen.
Material of another kind moves to its own page and is linked. Option tables,
exit codes and other reference tables live once under Reference; guides link
to them.

### One running example per page

A page declares one model set near the top and reuses it to the end; each
later section shows only what changed. Example data is about four rows that
read like real records, with no tied values, so the reader can check a
result by eye. Docstrings and the API reference share one running example,
the `reference` prelude (`docs/examples/_preludes/reference.py`, `Author` and
`Post`).

### Snippet `end:` markers go below the result

The embedded range ends after the `assert` or `print` that holds the answer,
so the page shows what the call returns.

Before (the page shows a query and never its result):

```python
# --8<-- [start:filtering]
adults = await User.where(lambda user: user.age >= 18).all()
# --8<-- [end:filtering]
assert [user.name for user in adults] == ["Ada", "Grace"]
```

After:

```python
# --8<-- [start:filtering]
adults = await User.where(lambda user: user.age >= 18).all()
assert [user.name for user in adults] == ["Ada", "Grace"]
# --8<-- [end:filtering]
```

### The session wrapper

Every ORM call runs inside `async with engines.session():` and raises
`No database route` outside it. The first code block on a page shows the
wrapper. Every later block either shows it too or sits under a sentence
saying it runs inside one, linking to Connections › Sessions. An inline block
that does real work runs under the docs harness with a prelude that opens the
session (`{test=run prelude=<name>}`, from #704).

### Generated SQL beside each query

Each query and each schema feature shows the SQL Ferro sends, for each
dialect where it differs. The SQL is captured by the example's own test run,
never typed by hand; a hand-simplified block drifts (unquoted reserved
words, invented aliases). This lands once #700 ships a public way to read a
query's SQL (#725 applies it to the guides).

### Quoted migration files are regenerated, never pasted

A block whose first line is `-- 0002_author_slug/04_contract.up.sqlite.sql`
(or, for a generated data step, `# migrations/0002_author_slug/02_backfill_author.py`)
quotes a file `ferro migrate new` writes. `tests/test_docs_migrations.py`
regenerates it from the page's example models (`STEP_FILE_SOURCES` names the
example or scenario) and compares byte for byte, so a quoted step file cannot
go stale the way the hand-pasted `0001` `CREATE TABLE` and SQLite rebuild did
(they lost a column, a unique index, the `sqlite_sequence` copy and a
recreated index). A data step the page shows written by hand is listed with
`None`. Alembic revisions on the bridge page are pinned the same way, under
`<!-- revision: <id> -->`.

### Compile-only blocks say why

An inline Python block that does not run under the harness says why in its
fence: `{test=compile reason="needs a Postgres database"}`. Generated files
and `env.py` excerpts are the usual cases; a block that can run, runs.

Every Python fence on a page carries one of three markers, and
`tests/test_docs_examples.py` fails on a fence with none:

| Marker | What the harness does |
| --- | --- |
| `{test=run prelude=<name>}` | Runs the block after `docs/examples/_preludes/<name>.py`. |
| `{test=compile reason="..."}` | Compiles the block; the reason says why it cannot run. |
| `{test=pytest path="tests/<file>.py"}` | Writes the block at that path in a scratch project and runs pytest there (`tests/test_docs_testing_fixtures.py`, for the testing how-to). |

`lint=check` can be added to any of them. Any other setting is refused with
the marker to write instead, so `{compile-only="..."}` or a bare
`{test=run}` fails the test rather than being ignored.

### The glossary file

Acronyms in prose (DDL, RLS, FK, PK, GUC, FFI, IR, ...) get a hover
definition from one glossary, `docs/includes/abbreviations.md`, appended to
every page by `pymdownx.snippets.auto_append` (#701). Its wording follows
`CONTEXT.md` where `CONTEXT.md` defines the term. A new term goes into that
file, never into a page's own abbreviation list.

### One Annotated idiom for I-7 twins

AGENTS.md I-7 shows every field-declaring example as an Assignment tab and an
Annotated tab. The two tabs differ only in where the field options sit: the
Annotated tab moves the same `Field(...)` call inside `Annotated[...]` and
keeps the default as the assigned value. A field with no default has no
assignment in either tab.

```python
name: Annotated[T, Field(...)] = default
```

Before (two idioms in today's twins, neither matching the Assignment tab
line for line):

```python
id: Annotated[int | None, Field(default=None, primary_key=True)]
id: Annotated[int | None, FerroField(primary_key=True)] = None
```

After (the Assignment tab, then its Annotated twin):

```python
id: int | None = Field(default=None, primary_key=True)
email: str = Field(unique=True)
nickname: str | None = Field(default=None, index=True)
```

```python
id: Annotated[int | None, Field(primary_key=True)] = None
email: Annotated[str, Field(unique=True)]
nickname: Annotated[str | None, Field(index=True)] = None
```

Pydantic accepts `Field(default=...)` inside `Annotated`, but type checkers
do not see a default there (`ty` reports `User(email="ada@example.com")` as
missing `id`); the
assigned default is the one both read, and it keeps the two tabs line for
line. Use `Field`, the name the Assignment tab imports;
`FerroField` stays out of twins. Forward foreign keys
(`Annotated[Target, ForeignKey(...)]`) and `BackRef()` / `ManyToMany()`
assignments have one form and appear the same in both tabs.

Link the tabs with the labels `Assignment` and `Annotated`, exactly; the
`content.tabs.link` feature only syncs tabs whose labels match.

## When to apply

Every new or edited page under `docs/pages/`, every script under
`docs/examples/`, and every twin pair. Prose rules (lead-in and result
sentences, sentence-case headings, no minimising words) come from the
writing-for-humans skill and are checked in review; lambda naming (I-8) and
error checks that can fail are checked by `python -m tests._docs_checks`.
