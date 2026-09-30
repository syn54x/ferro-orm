# PROTOTYPE — Model-change casebook

**Throwaway. Not part of ferro's docs. Delete freely.**

Answers wayfinder ticket [#481](https://github.com/syn54x/ferro-orm/issues/481)
(map [#452](https://github.com/syn54x/ferro-orm/issues/452)): for every kind of
model edit a developer can make, what exact migration does the in-house
generator produce — the directory, the steps and their transaction class,
whether a data step is scaffolded, the SQLite shape, and what `down` does.

The document is a **checklist**, not a design. Where a case is already
decided on the map it shows the decided shape and cites the ticket. Where it is
not, it shows the shape the decided rules *imply* and names the open ticket that
owns the call. Every case that forced a rule the map had not stated is marked
**⚑ surfaced**; those are collected at the end, and they are the reason this
casebook exists (#464 found four of them in four cases).

Shape: a document, not the prototype skill's HTML demo. The question is "what
does each edit generate", and the artifact a human reacts to is the rendered
directory and SQL, which reads best as text.

---

## 0. Ground rules the casebook is written against

Decided on the map (cited, not re-argued):

- **A migration is a directory** holding ordered steps and `ir.json`, its schema
  snapshot: the declared models at generation time, linked to the parent
  snapshot by stored-file checksum. Numbers count migrations. (#453, #454, #463)
- **The generator diffs declared IR against the head snapshot** and never opens
  a database. Coverage is the whole IR. Ops the reconciliation pass also
  performs render **byte-identical** to it (I-1: the DDL door is a third
  emitter); ops the pass refuses are the DDL door's own and are #468's. (#463)
- **Steps group by transaction class, not by model**: one transactional DDL
  step for every table's transactional DDL (FK order), one step per
  `CONCURRENTLY`-class statement, one data step per driving model, one
  contract step. (#464)
- **A data step is atomic (default) or chunked**; a DDL step that must run
  outside a transaction opens with `-- ferro: no-transaction` and must be
  re-runnable from its first statement. A scaffolded data step's body is
  `not_written()`, refused at load. A data step is scaffolded **only when the
  schema demands values**. (#464, ADR-0024)
- **Historical models are built from the union** of the previous and the
  migration's own snapshot, columns only. (#455, #462, ADR-0025)

Provisional spellings, used so the directories can be shown at all. Each is
owned by an open ticket and the casebook does not decide it:

| Provisional spelling | Owner |
| :--- | :--- |
| Step file names `NN_<name>.up.sql` / `NN_<name>.down.sql` / `NN_<name>.py` | #473 |
| Data-step declaration `@atomic` / `@chunked(query=…, batch_size=1000)` with `up(ctx, …)` / `down(ctx, …)` | #471 |
| Rename hints `Field(renamed_from="name")`, `__ferro_renamed_from__ = "author"` | #468 |
| SQLite table rebuild rendered as one no-transaction step (pragma outside, own `BEGIN`/`COMMIT`) | #470 |
| **How one migration serves two dialects** — see ⚑ S1; the casebook writes SQLite shapes as if a per-dialect step file existed | **unowned** |

Anchor models throughout:

```python
class Author(Model):
    id: int = Field(primary_key=True)
    name: str
    email: str = Field(unique=True)
    status: Status = Status.active          # StrEnum → Postgres type "status"

class Post(Model):
    id: int = Field(primary_key=True)
    title: str = Field(index=True)
    author: Annotated[Author, ForeignKey()]  # shadow column author_id, fk_post_author_id_author
```

Column types in the SQL below are illustrative (`TEXT`, `INTEGER`); the real
token comes from `resolve_column_storage` and is not this casebook's concern.

---

## 1. Summary matrix

| # | Edit | Steps generated | Data step | SQLite shape | `down` | Owner of the open part |
| :- | :--- | :--- | :--- | :--- | :--- | :--- |
| A1 | Add optional column | 1 atomic DDL | no | native `ADD COLUMN` | `DROP COLUMN` | — |
| A2 | Add required column, literal default | 1 atomic DDL | no | native, default lingers (pass parity) | `DROP COLUMN` | — |
| A2b | Add required column, `default_factory` | as A3 | **yes** ⚑ | as A3 | as A3 | #475 ⚑ S3 |
| A3 | Add required column, no default | expand · backfill · contract | **chunked** | 01 native; 03 rebuild | 03 `DROP NOT NULL`; 02 no-op ⚑; 01 `DROP COLUMN` | #470, #469 ⚑ S4, S5 |
| A4 | Drop column | 1 atomic DDL (destructive) | no | native unless PK/UNIQUE-autoindex/CHECK/FK → rebuild | `ADD COLUMN`, nullable; NOT NULL unrestorable ⚑ | #468, #469, #470 |
| A5 | Rename column | 1 atomic DDL, hint required | no | native `RENAME COLUMN` | reverse rename | #468 ⚑ S6 (union) |
| A6 | Change column type | 1 atomic DDL | no (hand-added when cast can't express it) | rebuild with cast in the copy, or nothing (affinity) | reverse cast; may fail on data | #470, #469 |
| A7a | Nullable → NOT NULL | backfill · contract | **chunked** over `IS NULL` | 02 rebuild | 02 `DROP NOT NULL`; 01 no-op | #470, #473 ⚑ S7 |
| A7b | NOT NULL → nullable | 1 atomic DDL | no | rebuild | `SET NOT NULL` (may fail on data) | #470, #469 |
| A8 | Change Python default | **no DDL, no migration** ⚑ | no | — | — | ⚑ S2 |
| A9 | Add / drop unique or index | 1 step per index | no | native `CREATE [UNIQUE] INDEX` | `DROP INDEX` | online-safety fog (`CONCURRENTLY`) |
| A10 | Add / drop / change check | 1 atomic DDL | no | rebuild | symmetric | #470; `NOT VALID` staging is fog |
| A11 | `db_check` on a *new* column | folded into A1/A2 | no | inline CHECK is legal; pass elides it today | — | #470 ⚑ S8 |
| B1 | New model | 1 atomic DDL (type + table + indexes + RLS) | no | same minus type/RLS | `DROP TABLE` (+ `DROP TYPE` if introduced) | — |
| B2 | Drop model | 1 atomic DDL (destructive), children first | no | `DROP TABLE` | `CREATE TABLE` from previous snapshot, empty | #468, #469 |
| B3 | Rename table | rename + rename every owned artifact ⚑ | no | `RENAME TO` + index drop/create; constraints → rebuild | reverse | #468, #470 ⚑ S9 |
| B4 | Change primary key | refuse, or expand · backfill(parent + each child) · contract | **yes**, several | rebuild | rebuild | #468 ⚑ S10 |
| C1 | Add FK field | as A1/A3 + `ADD CONSTRAINT` | if required | `ADD COLUMN … REFERENCES` legal (pass elides); down needs rebuild ⚑ | `DROP COLUMN` | #470 ⚑ S8, S11 |
| C2 | Drop FK field | 1 atomic DDL (destructive) | no | rebuild (FK column can't be dropped natively) | `ADD COLUMN`, nullable | #468, #470 |
| C3 | FK retarget / `on_delete` | 1 atomic DDL: `DROP` + `ADD CONSTRAINT` | no; hand-inserted if ids need remapping | rebuild | reverse | #470 |
| C4 | Many-to-many add / drop | as B1 / B2 for the join table | no | same | same | — |
| C5 | BackRef add / drop | **no DDL, no migration** | no | — | — | ⚑ S2 |
| D1 | Enum label add | 1 atomic DDL, first in the migration | no | nothing (text) | **no-op** (Postgres cannot drop a value) ⚑ | #469 ⚑ S12 |
| D2 | Enum label remove | (data step) · swap-type recipe | **yes** if rows carry the label | nothing | swap back | #468 |
| D3 | Enum label rename | 1 atomic `RENAME VALUE`, hint required | no | nothing | reverse | #468 |
| D4 | Enum type rename (class renamed) | swap-type recipe, no hint needed; or `RENAME TO` with hint | no | nothing | reverse | #468 |
| E1 | Row policy add | 1 atomic DDL | no | nothing (RLS is Postgres-only) | `DROP POLICY` (+ flags?) | #469 |
| E2 | Row policy change | 1 atomic DDL (`DROP` + `CREATE`) | no | nothing | rebuild with previous body | — ⚑ S13 |
| E3 | Row policy drop / RowSecurity removed | 1 atomic DDL (+ teardown) | no | nothing | `CREATE POLICY` from previous snapshot | #469 |
| F1 | Several models, one edit | grouped by transaction class | per driving model | per row above | reverse order | — |
| F2 | Expand/contract in one vs. two migrations | developer's editing cadence, not a flag | — | — | — | #475 |
| F3 | `--data`, no schema change | 1 data step; `ir.json` = head | **yes**, by request | same | optional `down` | #469, #473 |
| F4 | Whole-table invariant | hand-written atomic step replaces generated files | **atomic** | same | hand-written | — |

---

## 2. Cases

Each case: the edit, the generated directory, the steps with their transaction
class, the data step (if any), the SQLite shape, `down`, and who owns what is
still open.

### A. Columns

#### A1 · Add an optional column

```python
class Author(Model):
    nickname: str | None = None      # new
```

```
migrations/0008_author_nickname/
  01_schema.up.sql        ALTER TABLE "author" ADD COLUMN "nickname" TEXT;
  01_schema.down.sql      ALTER TABLE "author" DROP COLUMN "nickname";
  ir.json
```

- **Steps**: one atomic DDL step (Postgres DDL is transactional; the pass runs a
  table's plan in one transaction, so this is its shape byte-for-byte).
- **Data step**: none. An optional column demands no values (#464). Asking for
  one anyway is a `migrate new` spelling (#473).
- **SQLite**: native `ADD COLUMN`. `down` is native `DROP COLUMN` (3.35+).
  If the column is indexed the index is dropped first, as the pass already does.
- **`down`**: `DROP COLUMN`. Loses whatever was written into the column after
  `up`; that is the data-never-restored posture (#469) and needs no comment.

#### A2 · Add a required column with a literal default

```python
class Author(Model):
    tier: str = "free"                # new
```

```
migrations/0008_author_tier/
  01_schema.up.sql        ALTER TABLE "author" ADD COLUMN "tier" TEXT NOT NULL DEFAULT 'free';
                          ALTER TABLE "author" ALTER COLUMN "tier" DROP DEFAULT;
  01_schema.down.sql      ALTER TABLE "author" DROP COLUMN "tier";
  ir.json
```

- **Steps**: one atomic DDL step. Ferro persists no server-side defaults; the
  `DEFAULT` exists to backfill existing rows and Postgres drops it in the same
  transaction, exactly as the pass does today.
- **Data step**: none. A literal default is a value the schema supplies.
- **SQLite**: `ADD COLUMN "tier" TEXT NOT NULL DEFAULT 'free'`. The default
  **lingers** (SQLite has no `DROP DEFAULT`). The pass accepts that today, so
  the door does too (I-1: same shape as the pass). A rebuild to remove a
  cosmetic default would be O(rows) for nothing; the casebook does not propose
  one.
- **`down`**: `DROP COLUMN`.

#### A2b · Add a required column with `default_factory` or a callable default

```python
class Author(Model):
    joined_at: datetime = Field(default_factory=utcnow)   # new
```

The pass freezes **one** value at compile time onto every existing row (the
docs call this out). In a reviewed migration file that frozen literal would be
visible, but it is still one timestamp for every author.

**⚑ S3 (surfaced).** The generator should treat a callable default as *no
literal default* and generate the A3 split — expand nullable, chunked backfill
with the factory pre-filled as the transform's suggestion, contract — rather
than freezing one value. The reconciliation pass has no data step, so freezing
is its only option; the door has one. Owner: #475 (the split), with #473 for
the "I know one value is fine" override.

#### A3 · Add a required column with no default

```python
class Author(Model):
    slug: str                        # new, required, no default
```

```
migrations/0008_author_slug/
  01_expand.up.sql          ALTER TABLE "author" ADD COLUMN "slug" TEXT;
  01_expand.down.sql        ALTER TABLE "author" DROP COLUMN "slug";
  02_backfill_author.py     @chunked(query=lambda m: m.Author.where(lambda a: a.slug == None)
                                                          .order_by(lambda a: a.id),
                                     batch_size=1000)
                            async def up(ctx, batch):
                                for author in batch:
                                    author.slug = not_written()     # refused at load
                                    await author.save()

                            # down: nothing to un-backfill — 01_expand.down.sql drops the column
  03_contract.up.sql        ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL;
  03_contract.down.sql      ALTER TABLE "author" ALTER COLUMN "slug" DROP NOT NULL;
  ir.json                   # declares slug NOT NULL — the state after 03
```

- **Steps**: expand (atomic DDL) → backfill (chunked) → contract (atomic DDL).
  Decided in #464; the split's trigger and refusals are #475's.
- **Data step**: chunked, cursor on `id`, body refused until written.
- **Historical model** for step 02: union of head (no `slug`) and own (`slug`
  NOT NULL). **⚑ S4 (surfaced):** the union must relax a column that exists
  only in the *own* snapshot to **nullable**, because that is what the expand
  step created and what the live rows hold before the backfill. Taking the own
  snapshot's `NOT NULL` literally gives a historical class whose `slug: str`
  hydrates `None` on every row. Owner: #471 / ADR-0025 amendment.
- **SQLite**: 01 native `ADD COLUMN`; 03 has no native `SET NOT NULL` on the
  bundled 3.46.0, so it is a **rebuild** (12-step, no-transaction step) or a
  stated boundary — #470. With a rebuild, the reverse of 03 is a rebuild
  without the constraint.
- **`down`**: 03 `DROP NOT NULL`, 02 nothing, 01 `DROP COLUMN`.
  **⚑ S5 (surfaced):** a scaffolded backfill has a *trivially empty* reverse
  because the expand's `down` drops the column. Under #464 a missing `down`
  makes the migration irreversible at that step, which would make **every
  required-column migration irreversible by default**. The template must
  declare the empty reverse explicitly (spelling is #471's; `down` semantics
  are #469's) rather than omit it.
- **Online safety**: staging `SET NOT NULL` as `CHECK … NOT VALID` →
  `VALIDATE` → `SET NOT NULL` is the map's online-safety fog, not this row's.

#### A4 · Drop a column

```python
class Author(Model):
    # name: str                      # removed
```

```
migrations/0008_drop_author_name/
  01_schema.up.sql        -- destructive: rows lose "author"."name"      (#468's spelling)
                          ALTER TABLE "author" DROP COLUMN "name";
  01_schema.down.sql      ALTER TABLE "author" ADD COLUMN "name" TEXT;   -- nullable: see below
  ir.json
```

- **Steps**: one atomic DDL step. Whether it is written with a warning comment,
  behind a flag, or as a `TODO` is #468's.
- **Data step**: none.
- **SQLite**: native `DROP COLUMN` after dropping the column's own `idx_*` /
  `uq_*` index (the pass does this). Native `DROP COLUMN` refuses when the
  column is a PK, UNIQUE-autoindexed, referenced by a CHECK, a FK, a generated
  column, a trigger or a view — those shapes are a **rebuild** (#470).
- **`down`**: the column was `NOT NULL` with no default. `down` can put the
  column back but cannot restore `NOT NULL` without values, and it restores no
  data. Candidates: re-add nullable with a comment (shown), or re-add `NOT NULL
  DEFAULT ''`-style placeholder (wrong data, silently), or refuse to render a
  `down` and mark the step irreversible. Owner: #469. The casebook's lean is
  the first: schema restored as far as it can be, honestly marked.

#### A5 · Rename a column

```python
class Author(Model):
    full_name: str = Field(renamed_from="name")     # hint spelling is #468's
```

Without a hint the diff is `DROP COLUMN "name"` + `ADD COLUMN "full_name"`
(A4 + A3, with a backfill that cannot see the dropped data). With a hint:

```
migrations/0008_author_full_name/
  01_schema.up.sql        ALTER TABLE "author" RENAME COLUMN "name" TO "full_name";
  01_schema.down.sql      ALTER TABLE "author" RENAME COLUMN "full_name" TO "name";
  ir.json
```

- **Steps**: one atomic DDL step. Owned artifacts named after the column are
  renamed with it: `idx_author_name` → `idx_author_full_name`
  (`ALTER INDEX … RENAME TO`), `ck_author_name` likewise
  (`ALTER TABLE … RENAME CONSTRAINT`). The FK shadow column of a renamed FK
  *field* renames the same way and drags `fk_<table>_<col>_<to>` with it.
- **SQLite**: `RENAME COLUMN` is native (3.25+) and rewrites index and trigger
  references. Named constraints inside `CREATE TABLE` cannot be renamed
  natively; a `ck_author_name` needs a rebuild, or keeps its stale name. #470.
- **`down`**: the reverse rename.
- **⚑ S6 (surfaced): the union breaks on renames.** ADR-0025 builds the
  historical model from the union of the two snapshots; for this migration
  that is `name` *and* `full_name`, but the live table between the steps has
  only one of them. Either the union applies the rename hints (a rename is one
  column under two names), or a rename migration refuses data steps. Owner:
  #468 for the hint, #471 / ADR-0025 for the builder.
- **Hint lifetime**: once head's snapshot carries `full_name`, the hint is
  inert; the diff never sees `name` again. No cleanup is required, and the
  hint can be removed at leisure.

#### A6 · Change a column's type

```python
class Post(Model):
    views: int                       # was str
```

```
migrations/0008_post_views_int/
  01_schema.up.sql        ALTER TABLE "post" ALTER COLUMN "views" TYPE INTEGER USING "views"::INTEGER;
  01_schema.down.sql      ALTER TABLE "post" ALTER COLUMN "views" TYPE TEXT USING "views"::TEXT;
  ir.json
```

- **Steps**: one atomic DDL step, the pass's `AlterColumnType` shape. Exclusive
  lock; fails at apply if a value does not cast. Both facts belong in the
  file as a comment (#468's spelling of warnings).
- **Data step**: none scaffolded. When the cast cannot express the transform,
  the developer inserts an A3-shaped split by hand: add `views_new`, chunked
  transform, drop + rename. The generator does not guess.
- **SQLite**: the pass plans a type change only when the *affinity class*
  changes; the door does the same (I-1). A real affinity change is a
  **rebuild** with the cast in the copy statement
  (`INSERT … SELECT CAST("views" AS INTEGER)`), or a boundary. #470.
- **`down`**: the reverse cast. It can fail on data written after `up`
  (`int` → `str` always succeeds; the reverse does not). #469's posture.

#### A7a · Nullable → NOT NULL

```python
class Author(Model):
    nickname: str                    # was str | None
```

```
migrations/0008_author_nickname_required/
  01_backfill_author.py   @chunked(query=lambda m: m.Author.where(lambda a: a.nickname == None)
                                                        .order_by(lambda a: a.id), batch_size=1000)
                          async def up(ctx, batch): … not_written() …
                          # down: nothing — rows that were NULL cannot be told apart afterwards
  02_contract.up.sql      ALTER TABLE "author" ALTER COLUMN "nickname" SET NOT NULL;
  02_contract.down.sql    ALTER TABLE "author" ALTER COLUMN "nickname" DROP NOT NULL;
  ir.json
```

- **Steps**: the schema demands values for the rows that are `NULL`, so this
  is the A3 split **without the expand**: backfill then contract.
- **⚑ S7 (surfaced)**: the developer often knows there are no `NULL`s. A
  `not_written()` body that is refused at load blocks them. `migrate new`
  needs a spelling to skip the backfill (or the template body offers a
  one-line "assert none are NULL" alternative that fails the step loudly if
  wrong). Owner: #473. Never a stub that runs and does nothing (#464).
- **SQLite**: `SET NOT NULL` is a rebuild on 3.46.0 (native on 3.53+). #470.
- **`down`**: `DROP NOT NULL`; the backfill's reverse is empty by nature (S5's
  explicit-empty spelling).

#### A7b · NOT NULL → nullable

One atomic step, `ALTER COLUMN … DROP NOT NULL`. No data step. SQLite: rebuild
(#470). `down` is `SET NOT NULL` and fails if `NULL`s were written meanwhile
(#469).

#### A8 · Change a Python default

```python
class Author(Model):
    tier: str = "pro"                # was "free"
```

Ferro persists no server-side `DEFAULT`; the only `DEFAULT` it ever writes is
the transient backfill default on `ADD COLUMN` (A2), dropped on Postgres in the
same transaction and lingering on SQLite. A default change therefore renders
**no DDL**.

**⚑ S2 (surfaced): what is a "schema change"?** #463 says generator coverage
is the whole IR and lists defaults among the things "diffable from the
snapshot". But the IR carries facts that produce no DDL: Python defaults,
back-references, join-table declarations that already exist, field ordering.
If those differences produced a migration, every cosmetic model edit would
generate an empty directory whose only content is a new `ir.json`. The
casebook's rule: **the generator diffs the DDL-bearing projection of the IR.
A change outside it generates nothing and `migrate new` says so**; the next
real migration's snapshot carries the newer metadata. Under this rule A8 and
C5 are not migrations. The SQLite lingering default is a consequence the docs
already accept for the pass. Owner: this is a spec rule; the casebook proposes
it and asks the maintainer to confirm.

#### A9 · Add or drop a unique or an index

```python
class Post(Model):
    slug: str = Field(unique=True)   # index added on an existing column
```

```
migrations/0008_post_slug_unique/
  01_uq_post_slug.up.sql     CREATE UNIQUE INDEX "uq_post_slug" ON "post" ("slug");
  01_uq_post_slug.down.sql   DROP INDEX "uq_post_slug";
  ir.json
```

or, if the online-safety fog decides the generator emits `CONCURRENTLY`:

```
  01_uq_post_slug.up.sql     -- ferro: no-transaction
                             DROP INDEX CONCURRENTLY IF EXISTS "uq_post_slug";
                             CREATE UNIQUE INDEX CONCURRENTLY "uq_post_slug" ON "post" ("slug");
  01_uq_post_slug.down.sql   -- ferro: no-transaction
                             DROP INDEX CONCURRENTLY IF EXISTS "uq_post_slug";
```

- **Steps**: one step per index either way (#464: one step per
  `CONCURRENTLY` statement). Plain `CREATE INDEX` is the pass's shape and the
  I-1 pin; `CONCURRENTLY` cannot be the pass's shape (it runs inside a
  transaction) and is the door's own. The fog decides the default; the
  casebook only notes that the *no-transaction* form is already fully
  specified by #464.
- **Composite** indexes and uniques: same row, one step per index.
- **Duplicates**: `CREATE UNIQUE INDEX` fails at apply if the data has them.
  No data step is scaffolded; that is the developer's `--data` step to write
  first, by hand.
- **SQLite**: `CREATE [UNIQUE] INDEX` in place (no `CONCURRENTLY`; the
  directive line is honored but there is nothing for it to do — see S1 for
  whether SQLite gets its own file).
- **`down`**: `DROP INDEX`.
- **Inline single-column `UNIQUE`** on an existing column ("never — Alembic
  territory" in the ladder) is not a Ferro shape: Ferro's canonical unique is
  the standalone `uq_*` index on both dialects, so this row already covers it.

#### A10 · Add, drop or change a check

```python
class Post(Model):
    __ferro_checks__ = (Check("views_nonneg", lambda post: post.views >= 0),)
```

```
migrations/0008_post_views_nonneg/
  01_schema.up.sql        ALTER TABLE "post" ADD CONSTRAINT "ck_post_views_nonneg" CHECK ("views" >= 0);
  01_schema.down.sql      ALTER TABLE "post" DROP CONSTRAINT "ck_post_views_nonneg";
  ir.json
```

- **Steps**: one atomic DDL step, the pass's `render_check_addition`; a body
  change is the pass's `render_check_rebuild` (`DROP` + `ADD`); a drop is
  `render_check_drop` (destructive gate is #468's). The generator's old side
  is the head snapshot's *declaration*, rendered and normalized through the
  same `normalize_check_definition`, so drift detection is declaration vs
  declaration — never catalog text.
- **SQLite**: every one of these is a **rebuild** (no `ADD`/`DROP CONSTRAINT`).
  #470. This is the single biggest SQLite bucket (research §4.2: nine of
  fifteen warn-skips).
- **`down`**: symmetric. Existing rows that violate a re-added check make the
  `down` fail loudly, as the pass's `ADD CONSTRAINT` does.
- `NOT VALID` → `VALIDATE` staging is the online-safety fog.

#### A11 · `db_check=True` on a new column

Folded into A1/A2: the inline `CHECK` rides on `ADD COLUMN`. **⚑ S8
(surfaced)**: SQLite accepts an inline column `CHECK` on `ADD COLUMN`
(3.37+ validates existing rows) and `ADD COLUMN … REFERENCES` with a `NULL`
default, but the pass elides both today (research §4.2 items 4 and 6). These
are emitter gaps, not rebuild questions, and I-1 says the door inherits the
pass's shape — so the fix belongs in the pass first, and the door follows.
Owner: #470 (it has the SQLite facts in hand), as a prerequisite it can hand
to implementation.

### B. Models

#### B1 · New model

```python
class Comment(Model):
    id: int = Field(primary_key=True)
    body: str
    kind: CommentKind = CommentKind.note          # new StrEnum → new type
    post: Annotated[Post, ForeignKey(on_delete="cascade")]
    __ferro_rls__ = RowSecurity(RowPolicy("post_id"), force=True)
```

```
migrations/0008_comment/
  01_schema.up.sql        DO $$ … CREATE TYPE "commentkind" AS ENUM (…) … $$;     -- create pass's guarded statement
                          CREATE TABLE "comment" ( … CONSTRAINT "fk_comment_post_id_post" … );
                          CREATE INDEX …;                                          -- every idx_/uq_ the create pass emits
                          ALTER TABLE "comment" ENABLE ROW LEVEL SECURITY;
                          ALTER TABLE "comment" FORCE ROW LEVEL SECURITY;
                          CREATE POLICY "rls_comment_post_id" ON "comment" …;
  01_schema.down.sql      DROP TABLE "comment";
                          DROP TYPE "commentkind";
  ir.json
```

- **Steps**: one atomic DDL step: exactly the create pass's `render_create_table`
  output (I-1 items 1–8, 15, 17), in dependency order after any parent table
  created in the same migration.
- **Enum type provenance**: decided from the two snapshots — the type is
  *introduced* if no column in the head snapshot declares it, *reused*
  otherwise. Trivial here, where I-1 item 17 needed three ADRs on the Alembic
  side, because the generator sees whole modelsets rather than a revision's
  ops. `down` drops the type only when this migration introduced it.
- **SQLite**: `CREATE TABLE` with enums as text, no RLS (one warning, ADR-0014
  posture — or nothing at all if SQLite gets its own file, S1).
- **`down`**: `DROP TABLE`, then `DROP TYPE` for introduced types.

#### B2 · Drop a model

```
migrations/0008_drop_comment/
  01_schema.up.sql        -- destructive: table "comment" and its rows are dropped     (#468)
                          DROP TABLE "comment";
                          DROP TYPE "commentkind";                                    -- no other column uses it
  01_schema.down.sql      <the B1 up, verbatim, from the previous snapshot>            -- schema only, empty table
  ir.json
```

- **Order**: child tables (and FK columns pointing at the dropped table) go
  first, in one atomic step; the generator refuses a diff that drops a parent
  while a declared child still references it (the models would not compile
  anyway).
- **SQLite**: `DROP TABLE` runs an implicit `DELETE` that fires `ON DELETE
  CASCADE` into children while enforcement is on. Because children are dropped
  or detached first, nothing is left to cascade into; the ordering rule is
  what makes this safe, and the casebook records it as a required rule, not a
  nicety.
- **`down`**: recreates the table from the previous snapshot, empty.
  Irreversible for data (#469).

#### B3 · Rename a table

```python
class Writer(Model):                     # was Author
    __ferro_renamed_from__ = "author"    # hint spelling is #468's
```

**⚑ S9 (surfaced): a table rename is never one statement in Ferro.** I-1
derives every owned artifact name from the table name, so the rename drags
all of them, in this table and in every table whose FK points at it:

```
migrations/0008_writer/
  01_schema.up.sql        ALTER TABLE "author" RENAME TO "writer";
                          ALTER INDEX "uq_author_email" RENAME TO "uq_writer_email";
                          ALTER TABLE "writer" RENAME CONSTRAINT "ck_author_…" TO "ck_writer_…";
                          ALTER POLICY "rls_author_…" ON "writer" RENAME TO "rls_writer_…";
                          ALTER TABLE "post" RENAME CONSTRAINT "fk_post_author_id_author" TO "fk_post_author_id_writer";
  01_schema.down.sql      <each rename reversed>
  ir.json
```

- **Steps**: one atomic step (all are metadata-only on Postgres). The shadow
  column `post.author_id` is named after the *field*, not the target table, so
  it stays unless the field is renamed too (A5).
- **Without a hint** the diff is B2 + B1: drop `author`, create `writer`, and
  every FK column pointing at it drops and re-adds. That is a data-losing
  migration the developer did not intend, and it is the strongest argument on
  this casebook for a declaration-side hint that the generator honours (#468).
- **SQLite**: `RENAME TO` is native and rewrites FK references (3.26+).
  Indexes cannot be renamed natively → `DROP INDEX` + `CREATE INDEX` under the
  new name. Named constraints (`ck_*`, `fk_*`) live inside `CREATE TABLE` and
  need a **rebuild** to rename, or keep stale names that the drift check would
  then report forever. #470.
- **`down`**: every rename reversed.

#### B4 · Change the primary key

```python
class Author(Model):
    id: UUID = Field(primary_key=True, default_factory=uuid4)   # was int
```

The pass refuses PK changes (hard error on `DropColumn` of a PK). The honest
shape is a full expand/backfill/contract over the parent **and every child**:
add `new_id UUID`, backfill it, add `post.new_author_id`, backfill from the
parent's mapping, swap constraints, drop old columns, rename. Two data steps
minimum, `not_written()` in both, an FK-ordered chain of DDL steps.

**⚑ S10 (surfaced)**: does the generator emit that whole scaffold (many steps,
every transform blank), or refuse with a message that names the recipe and
points at `--empty`? Emitting it is honest to "generator coverage is the whole
IR"; refusing is honest to "the generator does not guess". Owner: #468. SQLite
is a rebuild of every table involved (#470).

### C. Relations

#### C1 · Add a foreign-key field

```python
class Post(Model):
    editor: Annotated[Author | None, ForeignKey(on_delete="set_null")] = None   # new, optional
```

```
migrations/0008_post_editor/
  01_schema.up.sql        ALTER TABLE "post" ADD COLUMN "editor_id" INTEGER;
                          ALTER TABLE "post" ADD CONSTRAINT "fk_post_editor_id_author"
                              FOREIGN KEY ("editor_id") REFERENCES "author" ("id") ON DELETE SET NULL;
  01_schema.down.sql      ALTER TABLE "post" DROP COLUMN "editor_id";         -- the constraint goes with it
  ir.json
```

- **Steps**: A1 plus the pass's `AddForeignKey`, one atomic step. A
  **required** FK field is A3: expand nullable, chunked backfill (the template
  names both models; the historical `Post` has `editor_id` as a plain column),
  contract `SET NOT NULL`, with the constraint added in the expand (it
  validates `NULL`s trivially) so the backfill is checked as it writes.
- **SQLite**: `ADD COLUMN … REFERENCES` is legal when the default is `NULL`;
  the pass elides the constraint today (S8). **⚑ S11 (surfaced)**: SQLite's
  native `DROP COLUMN` refuses a column "used in a foreign key constraint", so
  **the `down` of an FK-column add is a rebuild on SQLite** even though the
  `up` is native. Whichever way #470 goes, its boundary statement has to cover
  the `down` direction, not just `up`.
- **`down`**: `DROP COLUMN`.

#### C2 · Drop a foreign-key field

A4 with the constraint: `DROP CONSTRAINT` + `DROP COLUMN` in one atomic step,
destructive (#468). SQLite: rebuild, for S11's reason (#470). `down`: re-add
the column nullable plus the constraint, data never restored (#469).

#### C3 · Retarget a foreign key or change `on_delete`

```python
class Post(Model):
    author: Annotated[Author, ForeignKey(on_delete="restrict")]   # was cascade
```

```
migrations/0008_post_author_restrict/
  01_schema.up.sql        ALTER TABLE "post" DROP CONSTRAINT "fk_post_author_id_author";
                          ALTER TABLE "post" ADD CONSTRAINT "fk_post_author_id_author"
                              FOREIGN KEY ("author_id") REFERENCES "author" ("id") ON DELETE RESTRICT;
  01_schema.down.sql      <reverse>
  ir.json
```

- **Steps**: the pass's `RebuildForeignKey`, one atomic step. A **retarget**
  (to `writer`) changes the constraint name (`fk_post_author_id_writer`) and
  `ADD CONSTRAINT` validates every existing `author_id` against the new
  target. If the ids need remapping first, the developer splits the step by
  hand and inserts a data step between `DROP` and `ADD`; the generator does
  not guess a mapping.
- **SQLite**: rebuild (#470).
- **`down`**: the reverse rebuild.

#### C4 · Many-to-many add or drop

Adding `tags: list[Tag] = ManyToMany()` creates the join table the create pass
names, with its two FK columns and constraints: **B1 for the join table**, in
the same atomic step as any other table created that migration, after both
sides exist. Dropping it is **B2 for the join table**, destructive. Both
dialects handle both natively. The snapshot carries the join table's envelope,
so a data step in the same migration can reach it (how it is surfaced on
`ctx.models` is #471's).

#### C5 · Back-reference add or drop

`posts: list[Post] = BackRef()` is the reverse side of an existing FK. No
DDL; the IR may record it. Under S2, **no migration**.

### D. Enums (Postgres native types; text on SQLite)

#### D1 · Label added

```python
class Status(StrEnum):
    active = "active"; banned = "banned"; suspended = "suspended"   # new
```

```
migrations/0008_status_suspended/
  01_enum_status.up.sql   ALTER TYPE "status" ADD VALUE IF NOT EXISTS 'suspended';   -- render_pg_enum_add_value
  01_enum_status.down.sql -- ferro: no-op — Postgres cannot drop an enum value; the label stays (harmless)
  ir.json
```

- **Steps**: one atomic step, **first in the migration** — the I-12
  before-tables slot, for the same reason it exists there: a later step that
  writes the new label must see it committed. Steps are separate transactions,
  so the pre-Postgres-12 "cannot use a value added in this transaction" rule
  is satisfied by construction.
- **SQLite**: nothing. Enums store as text.
- **⚑ S12 (surfaced): the `down` of a label add is a no-op**, and it needs a
  spelling. The three candidates: an empty `.down.sql` (ambiguous with "not
  written"), a comment-only file with a directive (shown), or no file plus a
  rule that a label-add step is reversible-as-no-op. Owner: #469 (it also
  answers "what does an empty `down` file mean").

#### D2 · Label removed

Postgres has no `DROP VALUE`. The recipe is the swap: create `status__new`
without the label, `ALTER COLUMN … TYPE status__new USING status::text::status__new`,
drop `status`, rename. Rows still carrying the removed label make the cast
fail, so the schema demands values:

```
migrations/0008_status_drop_banned/
  01_migrate_author_status.py   @chunked(query=… .where(lambda a: a.status == "banned") …)   # not_written()
  02_schema.up.sql              CREATE TYPE "status__new" AS ENUM ('active', 'suspended');
                                ALTER TABLE "author" ALTER COLUMN "status" TYPE "status__new" USING "status"::text::"status__new";
                                DROP TYPE "status";
                                ALTER TYPE "status__new" RENAME TO "status";
  02_schema.down.sql            <swap back, adding 'banned'>
  ir.json
```

The pass refuses this (ladder: "Alembic territory"); it is the DDL door's and
#468's, including whether the data step is scaffolded unconditionally or
only offered. Every column of the type is altered in the one step.

#### D3 · Label renamed

Undetectable without a hint (the diff sees D2 + D1). With a hint (#468's
family, alongside column and table renames): `ALTER TYPE "status" RENAME
VALUE 'banned' TO 'blocked'` (Postgres 10+), one atomic step, reverse is the
reverse rename. The historical enum in a data step of the same migration hits
S6's union problem for labels.

#### D4 · Enum class renamed (type name changes)

The type name is the class name lowercased, so `Status` → `AuthorStatus`
changes the type. **No hint is needed**: the swap recipe (D2 without the data
step, labels identical) is data-preserving and the diff produces it directly
— create `authorstatus`, alter every column `USING …::text::authorstatus`,
drop `status`. With a hint, it collapses to `ALTER TYPE "status" RENAME TO
"authorstatus"`. #468 decides whether the hint family covers types; the
casebook notes the no-hint shape is correct, just heavier.

### E. Row security (Postgres only; SQLite plans nothing)

#### E1 · Policy added on an existing table

One atomic step from `plan_row_security_reconcile` over two declarations:
`ENABLE` / `FORCE` if the head snapshot had no row security, then `CREATE
POLICY "rls_<table>_<name>"`. `down`: `DROP POLICY`, and — unlike the pass,
whose flags are one-way (ADR-0019) — the door's `down` has to decide whether
it also runs the teardown (`NO FORCE` / `DISABLE`) when this migration
enabled them. The pass has teardown statements under `migrate_destructive`,
so the rendering exists; whether `down` uses them is #469's.

#### E2 · Policy body changed

One atomic step: `DROP POLICY` + `CREATE POLICY` (`row_policy_rebuild_statements`).
`down`: the same pair with the previous snapshot's body.

**⚑ S13 (surfaced, a simplification, not a gap)**: the pass's *unverifiable
raw body* category (ADR-0019: ferro cannot tell an edit from Postgres's
re-spelling of a raw body, so it only warns) **does not exist for the
generator**. Both sides are declarations, so a raw-body edit is an exact
string difference and rebuilds like any other. Worth a line in the spec and
in #479's cost accounting for the bridge.

#### E3 · Policy dropped, or `RowSecurity` removed

`DROP POLICY` for each removed declaration; when the model drops row security
entirely, the teardown (`NO FORCE`, `DISABLE`) follows in the same atomic
step, gated as destructive or not (#468's ladder). `down`: recreate from the
previous snapshot.

### F. Combinations

#### F1 · Several models in one edit

`Author` gains required `slug` (A3) and `Post` gains an index (A9,
`CONCURRENTLY` variant) and drops `subtitle` (A4):

```
migrations/0008_author_slug_post_cleanup/
  01_expand.up.sql              ALTER TABLE "author" ADD COLUMN "slug" TEXT;          -- every table's transactional expand, FK order
  01_expand.down.sql
  02_idx_post_title.up.sql      -- ferro: no-transaction …CREATE INDEX CONCURRENTLY…  -- one step per CONCURRENTLY statement
  02_idx_post_title.down.sql
  03_backfill_author.py                                                                -- one data step per driving model
  04_contract.up.sql            ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL; -- every table's contract, incl. the destructive drop
                                ALTER TABLE "post" DROP COLUMN "subtitle";
  04_contract.down.sql
  ir.json
```

Decided by #464's grouping rule; the casebook only shows it end to end.
`down` runs the steps in reverse order.

#### F2 · Expand/backfill/contract in one migration versus across two

One migration is the generated default and the reason the union exists. The
two-migration shape (deploy the expand, let the app dual-write, contract in a
later release) is **the developer's editing cadence, not a generator flag**:
declare the new column beside the old one and generate (expand-only
migration); later remove the old one and generate again (contract-only). Each
snapshot is a real declared state. Rows written behind the moving cursor
during the one-migration shape are #475's stated boundary.

#### F3 · A data step with no schema change (`--data`)

```
migrations/0008_normalize_emails/
  01_normalize_emails.py     @chunked(query=lambda m: m.Author.order_by(lambda a: a.id), batch_size=1000)
                             async def up(ctx, batch): … not_written() …
                             # down optional; missing → irreversible at this step (#469)
  ir.json                    # identical content to head's; parent checksum links it
```

The union of head and own is head itself, so the historical models are
exactly today's declared columns. Whether `ir.json` is copied or the directory
records "same as parent" is #473's; the runner's fingerprint check needs a
file to check.

#### F4 · A whole-table invariant

`Treasury.total == sum(Account.balance)` must hold at every commit: the
developer deletes the generated per-model data steps and writes **one
`@atomic` step over both historical models**, accepting the lock window
(#464). Nothing is generated differently; this is a documented pattern, not
a generator mode.

---

## 3. Surfaced rules and questions

Collected from the cases, in the order a spec author would need them. Each
either proposes a rule for the spec or hands a sharp question to the ticket
that owns it.

| ⚑ | Finding | Proposed disposition |
| :- | :--- | :--- |
| **S1** | **One migration, two dialects.** Every `.sql` step is dialect-bound: types differ, uniques and enums and RLS differ, and SQLite's rebuilds have no Postgres twin. Nothing on the map says how a project that tests on SQLite and ships on Postgres (the map's "both first-class") applies one chain to both. Options: (a) dialect-suffixed step files written by the generator from the one planner (`01_schema.up.postgres.sql`, `01_schema.up.sqlite.sql`), runner picks by connection; (b) one chain per configured dialect (two `migrations/` roots); (c) steps stored as planner ops and rendered at apply time (Django's shape; loses "review the SQL"). The casebook leans (a). | **New ticket** (grilling); blocks #473 and #470's rendering question. |
| **S2** | **What counts as a schema change.** Python defaults, back-refs and other non-DDL IR facts must not generate migrations. Rule: the generator diffs the DDL-bearing projection of the IR; anything else is "no schema change". Amends #463's "defaults are diffable" line, which is true but produces no op. | Spec rule; confirm with maintainer. |
| **S3** | A required column with a **callable default** should generate the A3 split, not freeze one value as the pass must. | #475 (the split) / #473 (override). |
| **S4** | In the **union**, a column present only in the migration's own snapshot is **nullable** in the historical model, because the expand created it that way and rows hold `NULL` until the backfill. | #471; ADR-0025 amendment. |
| **S5** | A scaffolded backfill's reverse is **empty by nature** (the expand's `down` drops the column). Omitting `down` would make every required-column migration irreversible. The template must declare the empty reverse explicitly. | #471 (spelling), #469 (semantics). |
| **S6** | The **union breaks on renames**: it yields both the old and the new column. Either the union applies rename hints or a rename migration refuses data steps. Same for renamed enum labels. | #468 (hint), #471 / ADR-0025 (builder). |
| **S7** | **Nullable → NOT NULL** when the developer knows there are no `NULL`s: `migrate new` needs a way to skip the backfill that is not a stub. | #473. |
| **S8** | SQLite **emitter gaps** (inline `CHECK` on `ADD COLUMN`; `ADD COLUMN … REFERENCES` with `NULL` default) belong in the pass first; the door inherits by I-1. | #470, as a prerequisite it hands to implementation. |
| **S9** | **A table rename is never one statement**: every owned `idx_`/`uq_`/`ck_`/`rls_`/`fk_` name in this table and in referencing tables renames with it. On SQLite, indexes drop/create and named constraints need a rebuild. Without a hint the diff is drop + create with data loss. | #468 (hint; the ladder row), #470. |
| **S10** | **PK change**: emit the full multi-step scaffold with blank transforms, or refuse and name the recipe. | #468. |
| **S11** | SQLite native `DROP COLUMN` refuses FK-bearing columns, so **the `down` of an FK-column add is a rebuild** even though `up` is native. #470's boundary must be stated for both directions. | #470. |
| **S12** | **`down` of an enum label add is a no-op** and needs a spelling (comment-only file with a directive, or a rule). Also answers "what does an empty `down` file mean". | #469. |
| **S13** | The **unverifiable raw policy body** category vanishes for the generator (declaration vs declaration). A simplification to record. | Spec; #479's accounting. |

Rules the casebook relies on that the map already states, restated because
every case leaned on them: destructive ops always render (the gate is a
comment, flag or `TODO`, #468); the door's `down` restores schema, never data
(#469); one data step per driving model; the generator never guesses a
transform or an id mapping.

## 4. What the casebook did not cover

- Partial indexes, exclusion constraints, generated columns: not Ferro
  features today; no row.
- Views, triggers, sequences: user-owned objects the pass cannot see; the
  drift check's stated bound (#463). A SQLite rebuild has to carry or refuse
  them (research §5.5); that is inside #470.
- The exact `ctx` surface, template text, and CLI flags: #471, #473.
- Dialect-specific type token differences per column: `resolve_column_storage`
  decides them and is already pinned (I-1 item 3).
