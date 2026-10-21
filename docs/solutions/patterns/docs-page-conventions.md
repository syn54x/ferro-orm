---
title: Docs page conventions (kind, running example, results, printed output, session, SQL, glossary, Annotated twins)
type: pattern
tags: [docs, convention, zensical, pydantic]
related_files:
  - AGENTS.md
  - docs/examples/
  - docs/includes/abbreviations.md
  - tests/_docs_harness.py
  - tests/_docs_sql.py
  - tests/_docs_twins.py
  - tests/test_docs_examples.py
  - tests/test_docs_output.py
  - tests/test_docs_sql.py
  - tests/test_docs_twins.py
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

### Printed output is regenerated, never typed

A ```text block shows what something printed, and says where that came from
in an HTML comment on the line directly above it, at the fence's indentation:

````markdown
<!-- prints: predicates.py:first-query -->
```text
Rent -1450
To savings -500
```
````

| Note | The block is | Checked by |
| --- | --- | --- |
| `<!-- prints: <example>.py:<snippet> -->` | What that snippet region prints | `tests/test_docs_output.py`, byte for byte |
| `<!-- prints: <example>.py:<snippet> (postgres) -->` | What the region prints on PostgreSQL; the script runs as `<example>.py <database-url> <role>` | `tests/test_docs_output.py` under `postgres_only`, byte for byte |
| `<!-- prints: above -->` | What the nearest Python fence above prints; that fence is marked `{test=run prelude=<name>}` | `tests/test_docs_output.py`, byte for byte |
| `<!-- output: <id> -->`, `<!-- refusal: <id> -->` | What `ferro migrate` prints | `tests/test_docs_migrations.py` |
| `<!-- shows: <what it is> -->` | Text Ferro does not print: a sketch, a file tree, another tool's shell session, a prompt to paste | Nothing; the note says what it is |

`tests/test_docs_output.py` refuses a ```text block with no note. To add one
under an embedded example, put the `print` inside the snippet region (a
region that catches an error prints `f"{type(exc).__name__}: {exc}"`), write
the note and an empty fence, and fill the fence from the failure message. A
printed line belongs to the innermost line of the example inside a region on
the call stack, so a helper defined outside the region can do the printing.
Every error, warning and message Ferro prints is a `prints:` block: write the
scenario that raises it as an example region, however small (a model, a
connect, a held lock, a migrations project from `_migrations_project.py`).
`docs/examples/auto_migrate_errors.py` and `migration_errors.py` are the
pattern; a region not shown on any page is fine, since the page shows the
text. A `shows:` block that reads like Ferro's output (`ferro.`,
`ferro auto-migrate:`, `SomethingError:`, `N validation error for`,
`connect(`) fails the test. The one exception, in `VARIES`, is the statement
log on the Connections guide, whose lines carry timings that change on every run.

### The session wrapper

Every ORM call runs inside `async with engines.session():` and raises
`No database route` outside it. The first code block on a page shows the
wrapper. Every later block either shows it too or sits under a sentence
saying it runs inside one, linking to Connections › Sessions. An inline block
that does real work runs under the docs harness with a prelude that opens the
session (`{test=run prelude=<name>}`, from #704).

### Generated SQL beside each query

Each query and each mutation an example runs shows the SQL Ferro sends, in a
`sql` fence below the example's result sentence. A page never carries SQL a
person typed: a hand-simplified block drifts (unquoted reserved words,
invented aliases), and the four such blocks the audit found on the Queries
guide had. The block's first line is a marker naming where its SQL comes from:

```sql
-- predicates.py:first-query (sqlite)
SELECT "transaction"."account_id", ...
FROM "transaction"
WHERE "transaction"."amount" <= ?
ORDER BY "transaction"."amount" ASC NULLS LAST;
```

`<example>.py` is a script in `docs/examples/`, `first-query` one of its
snippet regions (`# --8<-- [start:first-query]`), and `(sqlite)` or
`(postgres)` the dialect. The block holds every statement the region sends,
in order: a query terminal's statement as its `.sql()` renders it, and any
other statement (`save()`, `create()`, `bulk_create()`, an instance's
`delete()`, a many-to-many `add()`, a raw `execute()`) as the statement log
records it. Schema statements, the Postgres catalog probe, session settings
and a `transaction()` block's own `BEGIN` / `COMMIT` are left out. A region
that sends nothing but prints statements (a `create_tables()` report's
`statement.sql`) shows what it prints.

`tests/test_docs_sql.py` regenerates every `sql` fence on the pages it lists
(`PAGES`: Queries, Aggregations, Relationships, Mutations) and compares byte
for byte, failing with the page, the line and the example. The capture and
the checks live in `tests/_docs_sql.py` (`captured(example, dialect, url)`,
`check_block`, `check_printed`), which a new gate reuses; one run of an
example records both the SQL and the printed output of every region. To add
a block:

1. Write the query in a snippet region of an example script; the region is
   what the block shows, so keep one example per region.
2. Below the example's result sentence, write a lead-in naming the call and
   saying whether only the placeholders differ on PostgreSQL ("The query
   sends this; on PostgreSQL only the placeholders differ:"), then a `sql`
   fence holding just the marker line.
3. Fill the fence from the test's failure message, which prints what the
   region sends, then write the sentence after it: what in the SQL the reader
   should notice.
4. Where PostgreSQL differs in more than placeholders (casts, `RETURNING`,
   `serial`), show both as `=== "SQLite"` / `=== "PostgreSQL"` tabs, the
   second marked `(postgres)`. Under `--db-backends=sqlite,postgres` the test
   checks a `(postgres)` block exactly and a lone SQLite block once both
   sides read each placeholder as `?`, so "only the placeholders differ"
   stays true.

Each statement is laid out one clause per line (`FROM`, `JOIN`, `WHERE`,
`GROUP BY`, `ORDER BY`, `LIMIT` and the rest, at the top level only), a
`CREATE TABLE` one column per line, and ends with `;`. Unindenting the lines
and joining them with single spaces gives back the statement Ferro sent. The
example runs under the interpreter's own hash seed, so output that varies from
run to run fails the gate. An example that a `(postgres)` block
or a lone SQLite block names must run on PostgreSQL too: `ferro.connect` is
pointed at the test database, so raw SQL in it uses `$1` placeholders, which
SQLite also accepts.

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

Every Python fence on a page, in Markdown a page pulls in with `--8<--`
(`CONTRIBUTING.md`, `README.md` sections, `docs/includes`) and in a
docstring carries one of three markers; `tests/test_docs_examples.py` and
`tests/test_docstring_examples.py` fail on a fence with none:

| Marker | What the harness does |
| --- | --- |
| `{test=run prelude=<name>}` | Runs the block after `docs/examples/_preludes/<name>.py`. |
| `{test=run prelude=<name> backend=postgres}` | Runs it on PostgreSQL under the `postgres_only` marker, every `connect()` in the prelude and the block pointed at the test database whatever URL it names; without Postgres the test is skipped and the block only compiles. |
| `{test=compile reason="..."}` | Compiles the block; the reason says why it cannot run. |
| `{test=pytest path="tests/<file>.py"}` | Writes the block at that path in a scratch project and runs pytest there (`tests/test_docs_testing_fixtures.py`, for the testing how-to). |

A block that needs Postgres runs with `backend=postgres`; `test=compile` is for a block that cannot run anywhere the suite can (a web framework the test groups do not install, a generated file).

`lint=check` can be added to any of them. Any other setting is refused with
the marker to write instead, so `{compile-only="..."}` or a bare
`{test=run}` fails the test rather than being ignored.

### The glossary file

Acronyms in prose (DDL, RLS, FK, PK, GUC, FFI, IR, ...) get a hover
definition from one glossary, `docs/includes/abbreviations.md`, appended to
every page by `pymdownx.snippets.auto_append` (#701). Its wording follows
`GLOSSARY.md` where `GLOSSARY.md` defines the term. A new term goes into that
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

A `default_factory` is a default too, and type checkers do not see it
inside `Annotated` either (`ty` reports `missing-argument` for
`id: Annotated[UUID, Field(primary_key=True, default_factory=uuid4)]`). It
stays the assigned value, as a `Field` of its own; only the other options
move:

```python
id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

```python
id: Annotated[uuid.UUID, Field(primary_key=True)] = Field(default_factory=uuid.uuid4)
created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

The twin is generated, never written by hand. `tests/_docs_twins.py`
rewrites the Assignment script into the Annotated idiom and leaves every
other line alone, so both tabs run the same data, queries and prints. After
editing `<name>.py`, run `python -m tests._docs_twins docs/examples/<name>.py`
to rewrite `<name>_annotated.py`. `tests/test_docs_twins.py` fails when a
twin is not its script rewritten, or when the two declare a different schema
or print different output.

Link the tabs with the labels `Assignment` and `Annotated`, exactly; the
`content.tabs.link` feature only syncs tabs whose labels match.

## When to apply

Every new or edited page under `docs/pages/`, every script under
`docs/examples/`, and every twin pair. Prose rules (lead-in and result
sentences, sentence-case headings, no minimising words) come from the
writing-for-humans skill and are checked in review; lambda naming (I-8) and
error checks that can fail are checked by `python -m tests._docs_checks`.
