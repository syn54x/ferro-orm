# A generated upgrade reuses the enum types it does not introduce

Two models share one `StrEnum`; the first was migrated earlier, the second
is added in a later revision:

```python
class CategoryColor(StrEnum):
    RUST = "rust"
    AMBER = "amber"

class Category(Model):            # migrated earlier; type "categorycolor" is live
    id: int | None = Field(default=None, primary_key=True)
    color: CategoryColor | None = None

class Card(Model):                # new this revision, reuses the same StrEnum
    id: int | None = Field(default=None, primary_key=True)
    color: CategoryColor | None = None
```

Autogenerate rendered `card.color` as `sa.Enum('rust', 'amber',
name='categorycolor')`, and Alembic's `create_table` let SQLAlchemy issue
`CREATE TYPE categorycolor` for it unconditionally (`checkfirst=False`).
Postgres refused: `type "categorycolor" already exists` (#443). Plain
Alembic users hand-edit the column to
`postgresql.ENUM('rust', 'amber', name='categorycolor', create_type=False)`
(alembic#278). Ferro's bridge now renders exactly that, for exactly the
columns that need it:

```python
def upgrade() -> None:
    op.create_table('card',
    sa.Column('color', postgresql.ENUM('rust', 'amber', name='categorycolor', create_type=False), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
```

**Which columns**: the mirror image of ADR-0020. A type is the revision's
by provenance when every column declaring it is one the revision adds; the
revision creates it (inline, with `create_table`) and its downgrade drops
it. A type with an added column *and* a column the downgrade leaves
standing or puts back — a pre-existing column on a surviving table,
including one `include_object` hides from the revision, or a column the
revision drops — is **reused**: it already lives on every database the
revision can run against, because the revision that added the surviving
column created it. The revision's `create_table` columns of a reused type
render `create_type=False`, and no `DROP TYPE` is rendered. Both verdicts
come from one decision over the same inputs
(`ferro_ddl_lowering::enum_type_provenance`, one `Introduced | Reused`
per touched type, consumed over FFI as `_core._plan_enum_type_provenance`),
so the upgrade and the downgrade can never disagree about whose type it
is. `add_column` is left
alone: Alembic's `add_column` never creates a type, so there is nothing to
suppress.

**Decided from the revision alone**, never from the live catalog, for the
reason ADR-0020 gives: a generated revision is a file that runs against
many databases, and what it creates must follow from what it adds, not from
what one developer's database held on the afternoon it was generated. The
issue's own suggestion — treat a type as not the revision's when it is live
at autogenerate time — was rejected in the #441 review panel (F4) for the
drop and is rejected here for the same reason: it would render
`create_type=False` for the #438 orphan residue, and the committed file
would fail with `type "categorycolor" does not exist` on every other
database.

**How the flag reaches the file**: SQLAlchemy's `repr()` of a
`postgresql.ENUM` omits `create_type`, and Alembic's default type renderer
is that `repr`, so swapping the column's type is not enough — Alembic would
still write a type-creating column. Alembic's sanctioned seam for rendering
a type is the `render_item` hook configured in `env.py`; ferro exports one
(`ferro.migrations.render_item`) that renders a `create_type=False` enum
with its `from sqlalchemy.dialects import postgresql` import and declines
everything else, so a project's own hook composes it (call ferro's first,
fall through on `False`). The comparator does not assume the hook is
there: when a revision needs the rendering, it calls the context's
`render_item` on the rewritten type and refuses — before any file is
written — unless the result carries the flag. The check is behavioural, so
a delegating hook passes and an unwired `env.py` gets one actionable error
instead of a revision whose upgrade fails later (AGENTS.md I-6).

Decision by owner's invariants (2026-09-28), diagnosing #443 after #441.

Rejected alternatives:

- **Catalog-based exclusion** (render `create_type=False` for whatever is
  live when generating): see above; ADR-0020 F4.
- **Ferro creates every type itself** (an `op.execute` of the runtime's
  guarded `CREATE TYPE` for each introduced type, and `create_type=False`
  on every enum column): the purest single-renderer shape, and it would
  also cover a type introduced by `add_column` alone, which Alembic never
  creates. Rejected for now because it needs `render_item` wired for
  *every* enum-bearing revision — every existing project would fail at its
  next enum revision until `env.py` changed — and changes the rendered
  upgrade for the common case that already works. The reuse rule only
  touches the case that fails today. The `add_column`-only introduction
  remains Alembic's gap.
- **Injecting `render_item` from the comparator** (wrapping
  `autogen_context.opts["render_item"]` at comparison time): the seam
  exists, but it rewrites the project's configuration behind its back, and
  a double `produce_migrations` on one context would double-wrap.
- **Overriding Alembic's `create_table` renderer** on the global
  dispatcher, or registering a ferro `PostgresqlImpl`: both replace Alembic
  machinery for every user of the process to fix one type's `repr`.
- **A private `_create_events=False` kwarg** (which `repr` *does* print
  and which also suppresses the creation): a SQLAlchemy-internal flag in
  every generated file.
- **Mutating the metadata's own column** instead of a copy: the metadata
  is reused across revisions and comparators; the column would render
  `create_type=False` forever.

## Consequences

- The issue's loop runs end to end: the generated upgrade creates `card`
  without touching `categorycolor`, the downgrade drops `card` and
  `cardsize` only, and the upgrade runs again.
- Moving an enum column to a new table (`drop_column` + `create_table`)
  now renders a runnable upgrade too: the restored column makes the type
  reused.
- `env.py` gains one line: `render_item=render_item` in
  `context.configure(...)`. A project without it keeps working until a
  revision reuses a type, and then autogenerate says what to add.
- The generated file imports `from sqlalchemy.dialects import postgresql`
  only when it renders a reused type; introduced types keep `sa.Enum`.
- Nothing changes for SQLite (enums store as text; the comparator does not
  run) or for auto-migrate (its create pass guards `CREATE TYPE` with a
  catalog check and has no downgrade door).
- The orphan case of ADR-0020 is unchanged: an orphan nothing declares is
  the adopting revision's by provenance, its column creates inline, and on
  that one database the orphan is removed by hand.
