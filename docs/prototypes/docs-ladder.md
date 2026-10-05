# PROTOTYPE: the schema-management docs rewritten around two doors

Throwaway. Answers the wayfinder ticket [Docs ladder: the schema-management guide rewritten around two doors](https://github.com/syn54x/ferro-orm/issues/498). Nothing here is published docs; it is a shape to react to. Every behavior it describes was decided on another ticket of [the map](https://github.com/syn54x/ferro-orm/issues/452); the only new decisions are how the docs are cut up and worded. Those are collected in [Choices to react to](#choices-to-react-to) at the end.

Real pages will follow I-7 (both declaration styles as tabs) and I-8 (lambda predicates). The samples below show one style to stay short.

---

## 1. The shape in one picture

Today: one page, one three-rung table, "Alembic for production".

Proposed: the single page becomes a **Schema Management** group in the Guide nav. The overview keeps the ladder and sends the reader to one of two doors.

```text
Guide
  …
  Schema Management
    Overview                    guide/schema/index.md          the ladder, the "one door per database" rule, choosing
    Auto-migrate                guide/schema/auto-migrate.md   door 1: today's Auto-Migration section, moved
    Migrations                  guide/schema/migrations.md     door 2: init, new, review, up, check, status, down
    Data steps and backfills    guide/schema/data-steps.md     ctx, @atomic/@chunked, backfill versus guard
    Deploying migrations        guide/schema/deploying.md      CI gate, deploy step, rolling deploys, local-first start-up
    Alembic                     guide/schema/alembic.md        the peer: today's Alembic section, updated
How-To
    Adopting migrations on an existing database    howto/adopt-migrations.md
    Testing                                        (existing page gains a migrations section; content is still fog)
API Reference
    Migrations                  api/migrations.md              ferro.migrations.up/require_applied/status/baseline, get_metadata, ferro_options
    Configuration               api/configuration.md           FerroSettings, every [tool.ferro] key
    CLI                         api/cli.md                     every `ferro migrate` verb, flags, exit codes
```

`guide/migrations.md` keeps working as a redirect to the overview, and its `#alembic-for-production` anchor redirects to the Alembic page.

---

## 2. The overview page, drafted

> # Schema Management
>
> Your models are the source of truth for your schema. Ferro gives you two doors for getting a database to match them.
>
> **Auto-migrate** changes the database straight from the models, at `connect()`. No files, nothing to review.
>
> **Migrations** write each change down as a numbered directory of SQL and Python steps that you review, commit and apply. Ferro writes them for you from the difference between your models and the last migration.
>
> ## The ladder
>
> | Rung | How you ask for it | What it does | Reach for it when |
> | :--- | :--- | :--- | :--- |
> | 1. Auto-create | `connect(url, auto_migrate=True)` | Creates missing tables. Never touches an existing one. | Tests, scripts, a first prototype |
> | 2. Auto-update | `connect(url, migrate_updates=True)`, optionally `migrate_destructive=True` | Also alters existing tables to match the models, as far as one in-place statement can. | Development while the schema is still moving |
> | 3. Migrations | `ferro migrate new`, then `ferro migrate up` (`ferro-orm[cli]`) | Reviewed, numbered files: schema steps, data steps, a way back down. Covers every change the models can express, on Postgres and SQLite. | Any database whose data you would mind losing. **Recommended for production.** |
>
> Rungs 1 and 2 are the first door; each flag implies the ones before it. Rung 3 is the second door.
>
> **Already on Alembic, or need it for SQLAlchemy tables in the same project?** Ferro's [Alembic bridge](alembic.md) is a supported alternative to rung 3: it generates revisions from the same decisions. It does not write backfills or SQLite table rebuilds; see [what each door covers](#what-each-door-covers).
>
> ## One door per database
>
> A database is managed by auto-migrate **or** by migrations, never both. Once a database has run a migration, `connect()` refuses the auto-migrate flags on it:
>
> ```text
> ferro.connect: this database is managed by ferro migrations
>   (tracking table "public"."_ferro_migrations").
> auto_migrate, migrate_updates and migrate_destructive do not run on a database that has
> migrations: what they changed would be drift, and the next migration would fail on it.
> Remove the flag and generate the change with `ferro migrate new`. Nothing was changed.
> ```
>
> The rule is per database, not per project. A throwaway test database built with `auto_migrate=True` still works in a project that has migrations, because that database has never run one.
>
> | You have | And you try | What happens |
> | :--- | :--- | :--- |
> | A database with migrations applied | `connect(..., auto_migrate=True)` (or either stronger flag) | Refused before any DDL, text above |
> | A database with migrations applied | `alembic revision --autogenerate` over Ferro models | Refused, names `ferro migrate new` |
> | A database auto-migrate or Alembic built | `ferro migrate up` | Refused, names `ferro migrate baseline` ([adoption guide](../../howto/adopt-migrations.md)) |
> | A database auto-migrate built | `alembic revision --autogenerate` | Works, as today: the first revision is a clean baseline |
>
> ## What each door covers
>
> | Change | Auto-update | Migrations | Alembic bridge |
> | :--- | :--- | :--- | :--- |
> | Add a table, a nullable column, an index, an enum label | ✅ | ✅ | ✅ |
> | Add a check, FK constraint, type or nullability change on Postgres | ✅ | ✅ | ✅ |
> | The same on SQLite (needs a table rebuild) | ⚠️ warns, no DDL | ✅ generated rebuild | ❌ refused at autogenerate |
> | Rename a column, table or enum label | ❌ | ✅ declared with `renamed_from` | ✅ same hint |
> | Drop a table or column | columns only, `migrate_destructive` | ✅ marked `-- ferro: destructive` | ✅ marked `# ferro: destructive` |
> | A change existing rows must be given a value for (required column, nullable → required, label removal) | literal default only | ✅ expand, backfill, contract generated | ⚠️ the plain op, marked data-dependent; you write the backfill |
> | Python data steps against the models as they were | ❌ | ✅ | hand-written `op.execute` |
> | Change a primary key | ❌ | refused with a recipe until the restructure scaffold ships | ❌ refused |
>
> ## Choosing
>
> - **Starting out, or writing tests:** `auto_migrate=True`.
> - **Developing, schema still moving, data disposable:** `migrate_updates=True`.
> - **The first time you would mind losing the data:** `ferro migrate init`, then `ferro migrate new initial`. If the database already exists, follow the [adoption guide](../../howto/adopt-migrations.md); it takes four steps and runs no DDL.
> - **A local-first app that ships a SQLite file to users:** migrations, applied at start-up with `await ferro.migrations.up()` ([Deploying](deploying.md#local-first-apps)).

---

## 3. Pages that hang off it, outlined

### Auto-migrate (`guide/schema/auto-migrate.md`)

Today's "Auto-Migration" section moved almost verbatim: the capability table, the rules, label addition, destructive drops, on-demand `migrate()`, safety guidance. Edits:

- Opens with the "one door per database" rule in one sentence and a link back.
- Every "Alembic territory" and "use Alembic" phrase is reworded per [section 4](#4-where-every-alembic-territory-row-lands).
- SQLite warn-skip rows keep their ⚠️ and gain "generated as a table rebuild by migrations".
- New row: auto-migrate takes the run lock, and is refused behind a transaction-mode pooler on Postgres.
- The enum danger box ("this gap is invisible to your tests") stays; its escape hatch becomes migrations first, Alembic second.

### Migrations (`guide/schema/migrations.md`)

The day-to-day page. Sections:

1. **Install and set up.** `pip install "ferro-orm[cli]"`, then the `ferro migrate init` walkthrough as an annotated terminal transcript: one database or several (with the "several databases is not several dialects" wording), models, dialects, URL variable, directory, `pyproject.toml` or `ferro.toml`, the `.gitattributes` line, the pre-commit offer.
2. **Where configuration lives.** `[tool.ferro]` and `ferro.toml` shown as tabs with the same keys; both in one directory is refused. The URL is never in the file (`--url` or the variable `url_env` names). One paragraph on `FerroSettings` as the single reader, linking to the reference page.
3. **The loop.** Edit a model → `ferro migrate new author_slug` → read the directory → `ferro migrate up` → commit. Anchored on one model edit with the generated tree and the SQL shown:

   ```text
   migrations/0008_author_slug/
     01_schema.up.postgres.sql
     01_schema.down.postgres.sql
     ir.json
   ```

4. **Reading a migration.** Step numbers, one rendering per declared dialect, `-- ferro: not-applicable`, the `-- ferro: destructive` and `-- ferro: data-dependent` markers, what `ir.json` is and that it is never edited.
5. **Renames.** `renamed_from` on a field, `__ferro_renamed_from__` on a model, `__ferro_renamed_labels__` on an enum; the "drops nickname, adds handle" note `new` prints when there is no hint.
6. **Hand-written steps.** `new --sql-step`, `new --data-only`, portable unsuffixed `.sql`, `-- ferro: no-transaction`.
7. **Checking.** `status`, `check` (offline, for CI and the pre-commit hook, both the shipped hook id and the `repo: local` form), `drift` (online).
8. **Going back.** `down`, `--to`, the prompt, `@irreversible` refusing before anything is reverted, "restores schema, never data".
9. **Editing an applied step.** The refusal and `rerecord`.
10. **SQLite.** What a table rebuild looks like and the three things the runner refuses on a live table (undeclared column, foreign index, trigger).

### Data steps and backfills (`guide/schema/data-steps.md`)

1. **A data step.** `async def up(ctx)`, the four declarations, `ctx.models` as the models were, `ctx.execute`, `todo("…")`.
2. **Atomic or chunked**, and when to pick which.
3. **When the generator writes one for you.** A required column on a table with rows splits into expand, backfill, contract.
4. **Backfill or guard**, the two generated directories side by side:

   ```text
   ferro migrate new author_slug            ferro migrate new author_slug --no-backfill author.slug

   0008_author_slug/                        0008_author_slug/
     01_expand.up.postgres.sql                01_expand.up.postgres.sql
     02_backfill_author.py     ← you write    02_guard_author_slug.up.postgres.sql   ← generated, complete
     03_contract.up.postgres.sql              03_contract.up.postgres.sql
     ir.json                                  ir.json
   ```

   Left: for tables that have rows needing a value; `up` refuses until the `todo` is replaced. Right: for tables you know need none; the guard fails the run if a row turns out to need one. Then both middle files shown in full, and the rule "never delete the backfill file; regenerate with `--no-backfill`". (The guard file's name and exact body are illustrative here.)
5. **Label removal** as the second worked example.
6. **Templates.** `<directory>/_templates/data_step.py`.

### Deploying migrations (`guide/schema/deploying.md`)

1. **CI.** `ferro migrate check` needs no credentials.
2. **A server.** The deploy step runs `ferro migrate up`; the app calls `await ferro.migrations.require_applied()` at start-up and raises `PendingMigrationsError` otherwise.
3. **Rolling deploys.** The section handed over from the expand/contract ticket. A migration is applied whole, so a required column with no downtime is two releases:
   - Release 1 declares `slug: str | None`. Its migration is expand-only. Old code still running sees a database ahead of it and needs `allow_ahead=True`.
   - Release 2 tightens to `slug: str`. Its migration is the backfill plus the contract.
   - The one-migration shape is "for deploys where writers stop, or every writer already supplies the value".
   - What a late row looks like (the contract fails loudly) and the recovery: `ferro migrate down --to 0008:01`, then `ferro migrate up`.
4. **Local-first apps** {#local-first-apps}. `await ferro.migrations.up()` at start-up; the run lock; `DatabaseAheadError` when a user opens a newer file with an older build.
5. **Several databases.** `--database`, one lineage each; links to the existing Multiple Databases how-to.

### Alembic (`guide/schema/alembic.md`)

Today's "Alembic for Production" section, retitled "Alembic". Edits:

- Opening states its role: a supported peer, the right choice when the project already runs Alembic or has SQLAlchemy tables beside Ferro models; migrations are what Ferro recommends otherwise.
- `alembic init alembic` (not `migrations`, which is now Ferro's default directory and is refused when it holds an Alembic environment).
- `env.py` becomes the one wired line, `**ferro_options()`, and `get_metadata()` imports the configured models itself when `[tool.ferro]` exists.
- The seven enum bullets shrink: the reader no longer needs the per-family history, only "revisions come from the same decisions auto-migrate makes; an empty autogenerate means no drift".
- New "What the bridge does not do" box: no backfill scaffold (the op is marked data-dependent), no SQLite table rebuild (refused at autogenerate), refused on a database that has migrations.
- Ends with a link to the adoption guide for moving off it.

### Adopting migrations on an existing database (`howto/adopt-migrations.md`)

1. **The four steps**, once, for a database built by either auto-migrate or Alembic:
   1. Bring the database to the old door's head (one last `migrate_updates` run or `alembic upgrade head`).
   2. `ferro migrate init`, then `ferro migrate new initial`. `0001` is generated from the models, never from the database.
   3. `ferro migrate baseline` on each existing database. It runs nothing; it records `0001` as applied only if the database already matches, and prints what differs if not.
   4. Stop using the old door on that database: remove the auto-migrate flags (now refused), or stop generating Alembic revisions.
2. **When baseline says no.** The mismatch report as a to-do list; no override exists.
3. **Coming from Alembic.** Drop `alembic_version` once every database is cut over; move or keep the Alembic folder; the mixed project that keeps Alembic for its SQLAlchemy tables.
4. **Databases at different points.** Baseline a restored dump at `0002`, then `up`.
5. **A local-first app.** Users' files cannot be baselined by hand, so the app does it in process at start-up: `status()`; when unadopted, `baseline()` (still verified); then `up()`. Shown as one start-up function.
6. **Undoing it.** `baseline --remove`.

### Other pages that change

Seventeen pages and the README mention Alembic today. Beyond the group above, the ones whose message changes (rather than a link target):

- `getting-started/installation.md`: the extras table gains `[cli]`; "for production use Alembic" becomes migrations.
- `getting-started/quickstart.md`, `next-steps.md`: the "what next for schema" pointer goes to the overview.
- `why-ferro.md`, `README.md`, `faq.md`: "Ferro doesn't reinvent migrations" is no longer true and is rewritten.
- `concepts/backends.md`: the SQLite limitations paragraph points at the table rebuild.
- `howto/testing.md`: a migrations section, content still fog on the map.
- `howto/upgrade-guide.md`: an entry for the release that ships migrations, and one for the bridge release (`ferro_options()` required in `env.py`).
- `howto/migrate-from-sqlalchemy.md`: keeps recommending the bridge for a project in transition.

---

## 4. Where every "Alembic territory" row lands

| Today's text | Where | Becomes |
| :--- | :--- | :--- |
| Remove or rename an enum label: "⚠️ no DDL — Alembic territory" | capability table | "⚠️ no DDL here. Migrations: a rename is declared with `__ferro_renamed_labels__`; a removal generates its data step." |
| Inline `UNIQUE` on an existing column, index option changes: "❌ never — Alembic territory" | capability table | "❌ never here. Generated by migrations." |
| Rename column/table, change primary key, drop table: "❌ never — Alembic territory" | capability table | Split in three rows. Rename: "declare `renamed_from`; generated by migrations". Drop table: "generated by migrations, marked destructive". Primary key: "refused by every door today; migrations print the recipe". |
| Nine SQLite "⚠️ `UserWarning`, no DDL" rows | capability table | Unchanged behavior, plus "generated as a table rebuild by migrations". The warning text itself points there. |
| "NOT NULL additions need a literal default … or use Alembic" | rules list | "… or generate a migration, which splits the change into expand, backfill and contract." |
| "Remove or rename labels in a reviewed Alembic migration" | label addition contract | "… in a migration." |
| Dropping a PK, constrained or FK-referenced column "abort with a clear error pointing at Alembic" | destructive drops | "… pointing at `ferro migrate new`." |
| "For production, use Alembic — renames, primary-key changes, and data transforms are deliberately out of auto-migrate's scope" | safety box | "For production, use migrations. Renames and data transforms live there." |
| "Production: Alembic, exclusively" | Choosing a Workflow | The overview's Choosing list. |
| "you can develop with auto-migration and switch to Alembic when the schema stabilizes" | Choosing a Workflow | Still true, moves to the Alembic page. Its twin for migrations is the adoption guide. |
| "use Alembic" | `getting-started/installation.md` | "use migrations", linking to the overview. |

The runtime's own error and warning strings that name Alembic (in `src/migrate.rs`, `crates/ferro-migrate`, `src/ferro/base.py` and others) change with the build, not with this page; the spec should list them as a work item.

---

## Choices to react to

1. **One page becomes a nav group of six.** The alternative is to keep `guide/migrations.md` as one long page with the Alembic section at the bottom. The group is proposed because the day-to-day migrations page alone will be longer than today's whole page.
2. **Alembic is not a rung.** The ladder has three rungs and Alembic is a callout under it plus a column in the coverage table. The alternative is a fourth row. Proposed because a ladder implies climbing, and nobody climbs from rung 3 to Alembic; it is a sideways choice at rung 3.
3. **"Auto-migrate" and "Migrations" are the two door names** used in prose. "In-house" never appears in user docs (it only means something beside Alembic). "Built-in door" and "migration door" from the connect() ticket stay internal.
4. **The exclusivity rule sits on the overview**, directly under the ladder, with the full refusal text and the four-row "what happens if" table. Each door page repeats it in one sentence with a link, not the text.
5. **Rolling deploys live on a Deploying page**, not on the data-steps page, together with CI, `require_applied()` and local-first start-up.
6. **Adoption is a How-To**, not a Guide page, with the local-first in-process cutover as its own section.
7. **Backfill versus guard is a section of the data-steps page**, not its own page. The connect/CLI tickets said "page"; a section with its own anchor is proposed because the comparison is one screen.
8. **Reference pages for the CLI and configuration are new**; the guide pages show only the common path.
9. **Open fact, not a docs choice:** does the auto-update rung honour `renamed_from`? The Alembic bridge does (hint live while the database holds the old name). The coverage table above says ❌ for auto-update because no ticket says otherwise.
