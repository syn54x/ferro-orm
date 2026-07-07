# Consolidated Upgrade Guide — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two per-version migration pages with one living, cumulative `upgrade-guide.md`, rewire nav and inbound links, and give each version boundary a copy/paste agent prompt.

**Architecture:** Merge `howto/migrating-to-v0-12-0.md` + `howto/migrating-to-v0-14-0.md` into `howto/upgrade-guide.md`, newest boundary first, each section written as settled past-tense fact. Working code examples (before/after tabs, SQL recipes, the decision table) are ported verbatim; only prose and headings are re-homed. Each boundary section carries a collapsed admonition with a copy/paste coding-agent prompt.

**Tech Stack:** Markdown docs rendered by Zensical (MkDocs-family; `zensical.toml` nav, PyMdown tabbed `=== "..."` blocks and `??? example` collapsible admonitions).

**Spec:** `docs/brainstorms/2026-07-06-upgrade-guide-consolidation-requirements.md`

## Global Constraints

- **Docs-only change.** No `src/` or library code is modified. Current version is 0.13.0; 0.14 is the next release.
- **Preserve tested-shape code verbatim.** Move before/after tabs, SQL recipes, and the decision table unchanged. Do not re-author example code. (AGENTS.md I-8 both-declaration-styles / I-9 lambda-predicates compliance is inherited by preserving existing examples; introduce no new field-declaration or predicate examples.)
- **Settled past tense.** Delete every "planned for v0.14 / this removal already landed" retrofit callout. State end-states as fact.
- **De-dup by "where the change is forced":** predicate before/after lives in the 0.14 section (removed there); `get_metadata` before/after lives in the 0.12 section (still only deprecated). Cross-link, never duplicate.
- **Two accuracy corrections:** connection-routing removal landed in **0.13** (not 0.14); the private Alembic helpers (`_build_sa_table`, `_map_to_sa_type`) are **still deprecated, not removed** as of 0.14.
- **Conventional-commits** message types only (repo uses commitizen/CI). No AI attribution in commits.
- **Canonical site URL:** `https://syn54x.github.io/ferro-orm/`; the new page is `.../howto/upgrade-guide/`.
- **Source line references** below point at the current (pre-change) files.

## File Structure

- Create: `docs/pages/howto/upgrade-guide.md` — the single cumulative guide.
- Delete: `docs/pages/howto/migrating-to-v0-12-0.md`, `docs/pages/howto/migrating-to-v0-14-0.md`.
- Modify: `zensical.toml` (nav), `docs/pages/guide/connections.md` (inbound link), `docs/pages/api/migrations.md` (inbound link + accuracy fix).

---

### Task 1: Scaffold `upgrade-guide.md` (front matter, intro, wayfinding, shared verification)

**Files:**
- Create: `docs/pages/howto/upgrade-guide.md`

**Interfaces:**
- Produces: the page skeleton and the anchor `#which-sections-apply-to-you`, `#before-you-start`. Later tasks append `## Upgrading to 0.14` and `## Upgrading to 0.12` sections below "Before you start".

- [ ] **Step 1: Create the file with the header block, maintainer note, intro, wayfinding table, and shared verification section**

Write exactly this content to `docs/pages/howto/upgrade-guide.md`:

````markdown
<!--
  MAINTAINERS: This is the single, cumulative upgrade guide. When a release
  introduces breaking changes, ADD a new "## Upgrading to X.Y" section at the
  TOP of the boundary list (below "Before you start") — do NOT create a new
  per-version page. Keep each section's copy/paste agent prompt in sync with the
  prose directly beneath it.
-->

# Upgrade Guide

This is the curated path for upgrading Ferro across releases that changed
behavior. The [changelog](../changelog.md) is the exhaustive per-version record;
this guide is the task-oriented "here is the work" companion for the boundaries
that need it.

Find your current version below, then work **top to bottom** through every
boundary section you cross. Each section describes the settled end state — what
the API does now, and what you change to get there.

## Which sections apply to you

| You are on | Read |
| --- | --- |
| `0.13.x`, upgrading to `0.14` | [Upgrading to 0.14](#upgrading-to-014) |
| `0.11.x` or earlier, upgrading to `0.14` | Both sections, top to bottom |
| `0.11.x` or earlier, stopping at `0.12`/`0.13` | [Upgrading to 0.12](#upgrading-to-012) |

Each section includes a copy/paste prompt for a coding agent. If you are crossing
more than one boundary, run the prompts **newest first, one at a time**, and
verify your suite between them.

## Before you start

Turn deprecation warnings into failures on a throwaway branch so nothing slips
through:

```bash
uv run pytest -W error::DeprecationWarning
```

This catches every **deprecation-based** change. It does **not** catch the silent
behavioral changes in `0.14` (`create()`/`save()` no longer upsert;
`limit()`/`offset()` on a mutation now raise) — those emit no warning and must be
reviewed by reading the relevant section below.
````

- [ ] **Step 2: Verify the file renders the expected headings and anchors**

Run: `grep -nE "^# Upgrade Guide|^## Which sections|^## Before you start" docs/pages/howto/upgrade-guide.md`
Expected: three matching lines, in order.

- [ ] **Step 3: Commit**

```bash
git add docs/pages/howto/upgrade-guide.md
git commit -m "docs: scaffold consolidated upgrade guide"
```

---

### Task 2: Author the "Upgrading to 0.14" section

**Files:**
- Modify: `docs/pages/howto/upgrade-guide.md` (append below "Before you start")
- Reference (read, do not modify yet): `docs/pages/howto/migrating-to-v0-14-0.md`, `docs/pages/howto/migrating-to-v0-12-0.md`

**Interfaces:**
- Consumes: the skeleton from Task 1.
- Produces: anchor `#upgrading-to-014` and the subsection anchors `#predicates-operator-style-and-col-are-gone`, `#connection-routing-requires-a-session`, `#catch-typed-exceptions-not-runtimeerror` (Task 4 and Task 3 link to these).

- [ ] **Step 1: Append the section heading and the collapsed agent prompt**

Append to `docs/pages/howto/upgrade-guide.md`:

````markdown
## Upgrading to 0.14

`0.14` makes the mutation surface strict — no write silently does more (or
different) work than you asked — routes every database failure through a typed
exception hierarchy, unifies how the Rust runtime emitter and the Alembic bridge
derive column types and names, and **removes** the operator-style predicates and
`col()` bridge that `0.12` deprecated. Your model definitions do not change.

<!-- MAINTAINERS: keep this prompt in sync with the subsections below it. -->
??? example "Automate this: copy this prompt to your coding agent"

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
````

- [ ] **Step 2: Port the removed-deprecation subsections (predicates, routing)**

Add two `###` subsections directly after the prompt. Source the predicate
before/after tabs from `migrating-to-v0-12-0.md:57-77` and the session tabs from
`migrating-to-v0-12-0.md:90-117`, reframed to past tense. Write:

````markdown
### Predicates: operator style and `col()` are gone

`where()` accepts only lambda predicates. Operator-style predicates
(`Model.field == value`) and the `col()` bridge — both deprecated in `0.12` —
were removed. Class attributes are no longer replaced with `FieldProxy`, so
`Model.field` at class scope raises `AttributeError` under normal Pydantic
semantics. `order_by()` takes a lambda (`order_by(lambda t: t.created_at, "desc")`)
or a validated column-name string (`order_by("created_at", "desc")`). See
[Typed Query Predicates](../concepts/query-typing.md).

=== "Before (removed)"

    ```python
    adults = await User.where(User.age >= 18).all()
    admins = await User.where(User.role == "admin").all()
    ```

=== "After"

    ```python
    adults = await User.where(lambda t: t.age >= 18).all()
    admins = await User.where(lambda t: t.role == "admin").all()
    ```

### Connection routing requires a session

Unqualified operations (`User.all()`, `ferro.execute(...)`) once fell back to
implicit default-connection routing. That fallback is gone (it was removed in
`0.13`, one release ahead of the other deprecations, as part of the identity-map
and routing redesign). Wrap request- or task-scoped work in a session so routing,
the identity map, and transactions are explicit and isolated under concurrency. A
call with no active session and no `using=` now raises `RuntimeError`; an explicit
`using=` that names a different connection than the ambient session raises
`ValueError`. See [Identity Map](../concepts/identity-map.md).

=== "Before (removed)"

    ```python
    import ferro

    users = await User.where(lambda t: t.active == True).all()  # noqa: E712
    await ferro.execute("UPDATE users SET active = TRUE")
    ```

=== "After (ambient session)"

    ```python
    import ferro

    async with ferro.engines.session("app"):
        users = await User.where(lambda t: t.active == True).all()  # noqa: E712
        await ferro.execute("UPDATE users SET active = TRUE")
    ```
````

- [ ] **Step 3: Port the mutation + exception subsections verbatim**

Copy these four subsections from `migrating-to-v0-14-0.md` into the guide,
renumbering to `###` headings without the leading ordinal, keeping all tabbed
code blocks byte-for-byte:
- `create()` overwrite → `migrating-to-v0-14-0.md:21-50` → heading `### create() no longer overwrites on primary-key conflict`
- `save()` upsert → `migrating-to-v0-14-0.md:52-83` → heading `### save() no longer upserts`
- `limit()`/`offset()` → `migrating-to-v0-14-0.md:85-105` → heading `### limit()/offset() on a mutating query raise`
- typed exceptions → `migrating-to-v0-14-0.md:107-137` → heading `### Catch typed exceptions, not RuntimeError`

Preserve the existing relative links (`../api/exceptions.md`, `../guide/mutations.md#upsert`) unchanged.

- [ ] **Step 4: Port the schema-emission subsections and the "at a glance" table verbatim**

Copy `migrating-to-v0-14-0.md:139-242` (the "Schema-emission changes" block,
items 5–8, and the "Changed surfaces at a glance" table) into the guide under a
`### Schema-emission default changes` heading, keeping the SQL recipes, the
`=== "Keep..." / === "Convert..."` tabs, and the decision-table markdown
unchanged. Drop the standalone `#5`–`#8` ordinals from the sub-headings (use
descriptive `####` or bold labels matching the source titles). Do **not** copy
the "What about the deprecated v0.12 surfaces?" block (`:244-262`) — that
content is now expressed by the predicates/routing subsections above and the 0.12
section (Task 3).

- [ ] **Step 5: Verify no retrofit callouts and the prompt block are present**

Run: `grep -nE "already landed|Planned removal|planned for|scheduled for removal" docs/pages/howto/upgrade-guide.md`
Expected: no matches.
Run: `grep -c "copy this prompt to your coding agent" docs/pages/howto/upgrade-guide.md`
Expected: `1`.

- [ ] **Step 6: Commit**

```bash
git add docs/pages/howto/upgrade-guide.md
git commit -m "docs: add 'Upgrading to 0.14' section to upgrade guide"
```

---

### Task 3: Author the "Upgrading to 0.12" section

**Files:**
- Modify: `docs/pages/howto/upgrade-guide.md` (append below the 0.14 section)
- Reference: `docs/pages/howto/migrating-to-v0-12-0.md`

**Interfaces:**
- Consumes: anchors produced by Task 2 (links up to `#predicates-operator-style-and-col-are-gone`, `#connection-routing-requires-a-session`).
- Produces: anchor `#upgrading-to-012` and `#alembic-metadata-build-from-get_metadata` (Task 4's `api/migrations.md` link targets this).

- [ ] **Step 1: Append the section heading, IR-first rationale, and the collapsed 0.12 agent prompt**

Append to `docs/pages/howto/upgrade-guide.md`:

````markdown
## Upgrading to 0.12

`0.12` is the first release built on Ferro's IR-first architecture: query
execution, schema/migration planning, codecs, hydration, and connection routing
all flow through one shared intermediate representation instead of several
independent code paths. A single source of truth removes whole classes of drift
bugs — predictable schema diffs, identical bind/null semantics across SQLite and
PostgreSQL, and explicit session-scoped runtime state. Your model definitions do
not change; this is about how a few APIs are called.

`0.12` introduced these as **deprecation warnings**; the operator-predicate and
routing removals later landed (see [Upgrading to 0.14](#upgrading-to-014) and the
`0.13` routing note there). The one surface below still deprecated as of `0.14`
is the private Alembic helper import — migrate it now.

<!-- MAINTAINERS: keep this prompt in sync with the subsections below it. -->
??? example "Automate this: copy this prompt to your coding agent"

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
````

- [ ] **Step 2: Port the `get_metadata` migration (canonical home) and the deprecation summary**

Source the before/after from `migrating-to-v0-12-0.md:146-165`. Append:

````markdown
### Alembic metadata: build from `get_metadata()`

The private JSON-derivation helpers `ferro.migrations.alembic._build_sa_table`
and `ferro.migrations.alembic._map_to_sa_type` are deprecated — still present as
of `0.14`, but slated for removal. Schema metadata now derives from the IR through
the public `get_metadata()` entry point; use it directly in your Alembic `env.py`.

=== "Before (deprecated)"

    ```python
    from ferro.migrations.alembic import _build_sa_table, _map_to_sa_type
    ```

=== "After (recommended)"

    ```python
    from ferro.migrations import get_metadata

    target_metadata = get_metadata()
    ```

### Deprecations introduced in 0.12, at a glance

| Deprecated in 0.12 | Replacement | Status |
| --- | --- | --- |
| `Model.where(Model.field OP value)` / `col()` | `where(lambda t: ...)` | Removed in `0.14` — see [Predicates](#predicates-operator-style-and-col-are-gone) |
| Unqualified ops outside an active session | `async with ferro.engines.session("name")` | Removed in `0.13` — see [Connection routing](#connection-routing-requires-a-session) |
| `ferro.migrations.alembic._build_sa_table` | `ferro.migrations.get_metadata()` | Still deprecated as of `0.14` |
| `ferro.migrations.alembic._map_to_sa_type` | `ferro.migrations.get_metadata()` | Still deprecated as of `0.14` |
````

- [ ] **Step 3: Verify cross-links resolve to real anchors**

Run: `grep -nE "#predicates-operator-style-and-col-are-gone|#connection-routing-requires-a-session" docs/pages/howto/upgrade-guide.md`
Expected: at least the two definition headings (Task 2) plus the two reference links (this task) match — i.e. each anchor slug appears as both a heading and a link.

- [ ] **Step 4: Commit**

```bash
git add docs/pages/howto/upgrade-guide.md
git commit -m "docs: add 'Upgrading to 0.12' section to upgrade guide"
```

---

### Task 4: Rewire nav and inbound links, delete the old pages

**Files:**
- Modify: `zensical.toml:33-34`
- Modify: `docs/pages/guide/connections.md:151`
- Modify: `docs/pages/api/migrations.md:5`
- Delete: `docs/pages/howto/migrating-to-v0-12-0.md`, `docs/pages/howto/migrating-to-v0-14-0.md`

**Interfaces:**
- Consumes: anchors `#connection-routing-requires-a-session` (Task 2) and `#alembic-metadata-build-from-get_metadata` (Task 3).

- [ ] **Step 1: Replace the two nav entries with one**

In `zensical.toml`, replace lines 33–34:

```toml
    { "Migrating to v0.12.0" = "howto/migrating-to-v0-12-0.md" },
    { "Migrating to v0.14.0" = "howto/migrating-to-v0-14-0.md" },
```

with:

```toml
    { "Upgrade Guide" = "howto/upgrade-guide.md" },
```

- [ ] **Step 2: Repoint the `connections.md` inbound link (drop the stale "compatibility window" wording)**

In `docs/pages/guide/connections.md:151`, replace:

```markdown
Follow [Migrating to v0.12.0](../howto/migrating-to-v0-12-0.md) to remove these legacy call sites during the compatibility window.
```

with:

```markdown
See [Upgrade Guide: Connection routing requires a session](../howto/upgrade-guide.md#connection-routing-requires-a-session) to remove these legacy call sites.
```

- [ ] **Step 3: Repoint the `api/migrations.md` inbound link and fix the removal-version error**

In `docs/pages/api/migrations.md:5`, replace:

```markdown
Internal JSON-derivation helpers (`_build_sa_table`, `_map_to_sa_type`) are deprecated and scheduled for removal in `v0.14.0`. Replace internal usages with `get_metadata()`; see [Migrating to v0.12.0](../howto/migrating-to-v0-12-0.md).
```

with:

```markdown
Internal JSON-derivation helpers (`_build_sa_table`, `_map_to_sa_type`) remain deprecated (not yet removed as of `v0.14.0`). Replace internal usages with `get_metadata()`; see [Upgrade Guide: Alembic metadata](../howto/upgrade-guide.md#alembic-metadata-build-from-get_metadata).
```

- [ ] **Step 4: Delete the two old pages**

```bash
git rm docs/pages/howto/migrating-to-v0-12-0.md docs/pages/howto/migrating-to-v0-14-0.md
```

- [ ] **Step 5: Verify zero live references to the old slugs remain**

Run: `grep -rn "migrating-to-v0-12-0\|migrating-to-v0-14-0" docs/pages zensical.toml`
Expected: no matches.

- [ ] **Step 6: Commit**

```bash
git add zensical.toml docs/pages/guide/connections.md docs/pages/api/migrations.md
git commit -m "docs: rewire nav and inbound links to consolidated upgrade guide"
```

---

### Task 5: Final link/anchor audit and site build

**Files:** none (verification only)

- [ ] **Step 1: Confirm every intra-page anchor link has a matching heading**

Run:
```bash
python - <<'PY'
import re, pathlib
p = pathlib.Path("docs/pages/howto/upgrade-guide.md")
text = p.read_text()
def slug(h):
    h = h.replace("`","")
    v = re.sub(r"[^\w\s-]", "", h).strip().lower()
    return re.sub(r"[-\s]+", "-", v)  # emulate python-markdown: strip punctuation (incl "."), keep "_"
headings = {slug(m.group(2)) for m in re.finditer(r'^(#{1,6})\s+(.*)$', text, re.M)}
anchors = set(re.findall(r'\]\(#([^)]+)\)', text))
missing = anchors - headings
print("MISSING ANCHORS:", missing or "none")
PY
```
Expected: `MISSING ANCHORS: none`.

- [ ] **Step 2: Confirm the guide's outbound relative links point at real files**

Run:
```bash
grep -oE "\]\(\.\./[^)#]+\.md" docs/pages/howto/upgrade-guide.md | sed -E 's/\]\(//' | sort -u | while read rel; do
  f="docs/pages/howto/$rel"; [ -f "$f" ] && echo "OK  $rel" || echo "MISSING  $rel"
done
```
Expected: every line starts with `OK`.

- [ ] **Step 3: Build the site if Zensical is available (optional gate)**

Run: `uv run zensical build 2>&1 | tail -20 || echo "zensical unavailable — skipping build; anchor/link checks above are the gate"`
Expected: a successful build, or the skip message. Investigate any broken-link warnings that name `upgrade-guide`.

- [ ] **Step 4: No commit needed** (verification-only task). If Step 3 surfaced a fix, commit it with `docs: fix upgrade guide link`.

---

## Self-Review

**Spec coverage:**
- Evergreen consolidated page + delete old pair → Task 1 (create), Task 4 (delete). ✓
- Cumulative newest-first structure with wayfinding + shared verification → Task 1. ✓
- "Upgrading to 0.14" with all mutation/exception/schema subsections + table → Task 2. ✓
- "Upgrading to 0.12" IR-first rationale + `get_metadata` + corrected summary table → Task 3. ✓
- De-dup rule (predicates in 0.14, get_metadata in 0.12, cross-links) → Task 2 Step 2 / Task 3 Step 2. ✓
- Reframe to settled past tense; drop retrofit callouts → Task 2 Step 5 grep gate. ✓
- Per-boundary collapsed agent prompts with Pass 1/Pass 2 guardrail → Task 2 Step 1, Task 3 Step 1. ✓
- Nav rewire → Task 4 Step 1. ✓
- Two inbound links repointed → Task 4 Steps 2–3. ✓
- Accuracy corrections (routing landed 0.13; helpers still deprecated) → Task 2 Step 2 prose, Task 3 Step 2 table, Task 4 Step 3. ✓
- Maintainer notes → Task 1 Step 1, Task 2/3 prompt comments. ✓
- Out of scope (schema-migrations feature guide, sqlalchemy porting, changelog) → untouched by all tasks. ✓

**Placeholder scan:** New content (intro, wayfinding, both prompts, both cross-link tables, the reframed predicate/routing subsections) is written in full. Bulk verbatim moves cite exact source line ranges rather than re-pasting, per the "preserve verbatim" constraint — the source files exist in-repo until Task 4. No "TBD"/"handle appropriately" steps. ✓

**Type/anchor consistency:** Anchor slugs referenced by links match the heading text that generates them — `#connection-routing-requires-a-session`, `#predicates-operator-style-and-col-are-gone`, `#alembic-metadata-build-from-get_metadata`, `#upgrading-to-014`, `#upgrading-to-012` — cross-checked between the defining task and the linking task, and gated by Task 5 Step 1. ✓
