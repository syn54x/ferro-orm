# Upgrade Guide Consolidation — Requirements

**Date:** 2026-07-06
**Status:** Design approved; ready for implementation plan
**Scope:** Documentation only (no library code changes)

## Problem

The `docs/pages/howto/` directory carries two per-version upgrade pages:

- `migrating-to-v0-12-0.md` — the IR-first release; announced three deprecations
  ("planned removal in v0.14.0"): operator/`col()` predicates → lambdas, implicit
  connection routing → sessions, private Alembic helpers → `get_metadata()`.
- `migrating-to-v0-14-0.md` — the mutation-strictness release; silent behavioral
  fixes (`create`/`save` no longer upsert, `limit`/`offset` on mutations raise),
  the typed exception hierarchy, and eight schema-emission default changes.

They are entangled: v0.14 is exactly where the v0.12 deprecations are removed, so
each page reaches into the other. Worse, the v0.12 page has **rotted** — it is a
frozen snapshot that reality drifted past, so it accreted correction callouts:

- `migrating-to-v0-12-0.md:42` — `!!! warning "v0.14.0 update: this removal already landed"`
- `migrating-to-v0-12-0.md:122` — `!!! warning "v0.13 update: this removal already landed"`
  (the routing removal shipped a version *early*, in 0.13)

The user asked to combine the two version-upgrade guides into a single v0.14+
guide.

## Decision

Do **not** move this content into the changelog. `docs/pages/changelog.md` is
just `--8<-- "CHANGELOG.md"`, and `CHANGELOG.md` is the commitizen /
conventional-commits machine output (terse, grouped by Bug Fixes / Features /
Refactoring per version). It cannot carry tabbed before/after code, SQL
conversion recipes, an 8-row decision table, or a cumulative multi-version jump —
and hand-written prose would not survive the next `cz bump`.

Dedicated migration documentation is the right vehicle. The real defect is
**per-version snapshot pages rot**. Fix it by consolidating into **one living,
cumulative Upgrade Guide**, newest boundary first, each section written as
settled past-tense fact. The auto-generated changelog remains the exhaustive
per-version index and links into the guide for breaking boundaries. This is the
standard model (Django "Upgrading" + release notes, SQLAlchemy migration notes,
Pydantic migration guide) and it prevents the next `migrating-to-v0-16-0.md` from
ever being created.

## File & wiring changes

- **New page:** `docs/pages/howto/upgrade-guide.md`, titled `# Upgrade Guide`.
  Canonical URL: `https://syn54x.github.io/ferro-orm/howto/upgrade-guide/`.
- **Delete:** `docs/pages/howto/migrating-to-v0-12-0.md` and
  `docs/pages/howto/migrating-to-v0-14-0.md`.
- **Nav (`zensical.toml` lines 33–34):** replace the two `Migrating to v0.xx`
  entries with a single `{ "Upgrade Guide" = "howto/upgrade-guide.md" }`, kept at
  the top of the How-To group.
- **Inbound links repointed** to `../howto/upgrade-guide.md` + a section anchor:
  - `docs/pages/guide/connections.md:151` → routing section; drop the now-false
    "during the compatibility window" wording.
  - `docs/pages/api/migrations.md:5` → `get_metadata` section; **also fix the
    accuracy bug** — it currently says the private helpers are "scheduled for
    removal in `v0.14.0`", but they were *not* removed in 0.14 (still deprecated).
    Reword to "still deprecated; migrate to `get_metadata()`".
- **Maintainer note:** an HTML comment at the top of the new page reminding
  maintainers that future breaking releases append a new `## Upgrading to X.Y`
  section at the top — never a new per-version page.

## Structure — cumulative, newest boundary first

```
# Upgrade Guide
  <intro: the curated upgrade path; the changelog is the exhaustive per-version
   record and links here. Find your version, work top-down through each boundary
   you cross.>

## Which sections apply to you
  <tiny wayfinding table — on 0.13.x → 0.14: read one section; on ≤0.11 → 0.14:
   read both, top to bottom. Multi-boundary upgraders run the agent prompts
   newest-first, one at a time, verifying between.>

## Before you start
  <shared verification harness: `pytest -W error::DeprecationWarning`, with the
   explicit caveat that 0.14's silent behavioral changes are NOT catchable this
   way and must be read.>

## Upgrading to 0.14
  ??? example "Automate this: copy this prompt to your coding agent"   ← collapsed, near top (see Agent prompts below)
  ### Predicates: operator style and col() are gone   (was a 0.12 deprecation; removed in 0.14 — canonical before/after lives HERE)
  ### Connection routing requires a session           (accurately noted as landed in 0.13)
  ### create() no longer overwrites on PK conflict
  ### save() no longer upserts
  ### limit()/offset() on mutating queries raise
  ### Catch typed exceptions, not RuntimeError
  ### Schema-emission default changes                 (8 items: timestamptz, native enum, time, FK names, named uniques, 63-char truncation, SQLite spellings)
  ### Changed surfaces at a glance                     (the v0.14 table)

## Upgrading to 0.12
  ??? example "Automate this: copy this prompt to your coding agent"   ← collapsed, near top
  ### What IR-first changed for you                    ("why this matters" narrative, past tense)
  ### Alembic metadata: build from get_metadata()      (STILL deprecated as of 0.14, not removed — canonical before/after lives HERE)
  ### Deprecations introduced in 0.12                  (predicates + routing began warning here; both enforced by 0.14 — links up; keeps the corrected "at a glance" table)
```

## Content-handling principles

1. **Reframe to settled past tense.** Delete every "planned for v0.14 / this
   removal already landed" retrofit callout. State end-states as fact.
2. **Preserve the working code verbatim.** Before/after tabs, SQL recipes, and
   the decision table move over unchanged — only prose and headings are
   re-homed. Keeps AGENTS.md I-8 (both declaration styles) / I-9 (lambda
   predicates official) compliance intact and avoids reintroducing tested-shape
   drift.
3. **De-dup by "where the change is forced."** Each concrete migration appears
   once, at the boundary that enforces it; other boundaries cross-link to it. The
   predicate before/after lives in the 0.14 section (removed there); the
   `get_metadata` before/after lives in the 0.12 section (still only deprecated).
4. **Carry two accuracy corrections:** routing removal landed in **0.13** (not
   0.14); Alembic private helpers are **still deprecated, not removed** as of
   0.14 — reflected in the guide *and* in `api/migrations.md`.
5. **Keep both "at a glance" tables**, one per boundary, with corrected
   "landed in" columns.

## Agent prompts (per boundary)

Each `## Upgrading to X.Y` section includes a **collapsed** admonition
(`??? example "Automate this: copy this prompt to your coding agent"`) near the
top, containing a fenced ` ```text ` block that copies as one clean unit. The
prompt sits adjacent to the prose it mirrors, with an HTML comment reminding
maintainers to keep the two in sync.

**Design guardrail:** the three 0.14 behavioral changes are semantic, not
syntactic — no agent can grep its way to "did you *intend* `save()` to upsert
here?" The prompt therefore splits work into a mechanical Pass 1 (safe to apply)
and a semantic Pass 2 (list each `file:line` and ask the human; do **not**
auto-rewrite). The prompt also states that `-W error::DeprecationWarning` catches
the deprecation-based changes only, never the silent ones.

### Drafted prompt — "Upgrading to 0.14"

```text
You are helping upgrade a Python codebase from Ferro ORM 0.13.x to 0.14.
Reference: https://syn54x.github.io/ferro-orm/howto/upgrade-guide/#upgrading-to-014

Work in two passes. Do NOT open a PR until I have reviewed Pass 2.

PASS 1 — mechanical replacements (safe to apply directly):
1. Query predicates: replace operator-style `where(Model.field == value)` and any
   `col(...)` bridges with lambda predicates, e.g. `where(lambda m: m.field == value)`.
   `order_by` takes a lambda or a validated column-name string. Operator style and
   `col()` were removed in 0.14.
2. Connection routing: any ORM or raw call that ran outside a session
   (`User.all()`, `ferro.execute(...)` with no active session) must run inside
   `async with ferro.engines.session("name"):`. Unsessioned calls now raise.
3. Exceptions: replace `except RuntimeError` / `except ConnectionError` handlers
   around Ferro calls with the typed hierarchy from `ferro` (e.g.
   `UniqueViolationError`, `OperationalError`, `InterfaceError`). Match on type,
   not on driver-message text.

PASS 2 — semantic changes (DO NOT rewrite; list each site as file:line and ask me):
4. `Model.create(...)` and `instance.save()` no longer upsert. For each call site,
   tell me whether the code RELIED on the old overwrite-on-conflict behavior. If it
   did, I decide between `Model.upsert(...)` / `save(on_conflict="update")` and
   catching `UniqueViolationError`. Do not guess.
5. `.limit()` / `.offset()` chained into `.update()` / `.delete()` now raise. List
   each site — bounding a mutation requires fetching primary keys first, and only I
   know whether the bound was intentional.
6. Schema-emission defaults changed for derived types (plain `datetime` ->
   timestamptz, Enum -> native enum, `datetime.time` -> time). Auto-migrate refuses
   to rewrite existing columns and warns. For each affected model, list it and ask
   me to choose keep (`db_type=...`) vs a reviewed ALTER migration.

VERIFY after Pass 1:
- Run the test suite.
- Run `pytest -W error::DeprecationWarning`. This catches the deprecation-based
  changes ONLY — items 4, 5, and 6 are silent and will not appear here.
- Grep helper: grep -rn "\.limit(\|\.offset(" --include="*.py" src/ | grep "delete()\|update("

Report a summary: what you changed in Pass 1, and the Pass 2 sites awaiting my decision.
```

### Drafted prompt — "Upgrading to 0.12"

```text
You are helping upgrade a Python codebase from Ferro ORM 0.11.x to 0.12.
Reference: https://syn54x.github.io/ferro-orm/howto/upgrade-guide/#upgrading-to-012

All 0.12 changes are deprecation-based and safe to apply mechanically. Apply them,
then verify.

1. Query predicates: replace operator-style `where(Model.field == value)` with
   lambda predicates `where(lambda m: m.field == value)`. (These were removed
   outright in 0.14 — if you are upgrading past 0.12, prefer lambdas now.)
2. Connection routing: wrap request/task-scoped ORM and raw operations in
   `async with ferro.engines.session("name"):` instead of relying on implicit
   default-connection routing.
3. Alembic metadata: replace
   `from ferro.migrations.alembic import _build_sa_table, _map_to_sa_type`
   with `from ferro.migrations import get_metadata; target_metadata = get_metadata()`
   in your Alembic env.py.

VERIFY:
- Run the test suite.
- Run `pytest -W error::DeprecationWarning`; a clean run means no deprecated
  0.12-era surfaces remain on the paths you exercise.
```

## Out of scope

- `docs/pages/guide/migrations.md` (the schema-migrations *feature* guide) —
  untouched.
- `docs/pages/howto/migrate-from-sqlalchemy.md` (porting from another ORM) —
  untouched.
- The auto-generated changelog — untouched.

## Verification

- No test consumes these markdown files by path: `tests/test_docs_examples.py`
  globs `docs/examples/*.py` only; `tests/test_documentation_features.py`
  hand-mirrors `guide/` and `getting-started/` docs. Merging/renaming is safe.
- After the change, grep the whole `docs/pages/` tree for `migrating-to-v0-12-0`
  and `migrating-to-v0-14-0` — zero live references should remain.
- Build the site (zensical) and confirm the single "Upgrade Guide" nav entry and
  both repointed inbound links resolve.
