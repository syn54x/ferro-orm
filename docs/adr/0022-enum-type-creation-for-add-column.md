# A generated upgrade creates the enum types only its add_column introduces

> **Superseded in part by ADR-0041**: the bridge no longer decides type creation from the revision. On Postgres every native enum type a change needs is the planner's guarded `CREATE TYPE` (`render_pg_enum_create_type`), ahead of every table op, and every enum column renders `postgresql.ENUM(..., create_type=False)`. A type is dropped (`render_pg_enum_drop_type`, after the table ops) when the plan removes every column and table that declares it, decided by the one planner against the live database. The inline-versus-statement distinction, the *Introduced/Reused* verdicts made from the revision alone, and ownership by provenance are historical. The problems this ADR records (#438, #439, #443) stay solved, by that rule.

An existing table gains a field of a brand-new `StrEnum`:

```python
class CategoryColor(StrEnum):
    RUST = "rust"
    AMBER = "amber"

class Card(Model):                 # migrated earlier, without `color`
    id: int | None = Field(default=None, primary_key=True)
    title: str | None = None
    color: CategoryColor | None = None   # new this revision
```

Autogenerate rendered a bare `op.add_column('card', sa.Column('color',
sa.Enum('rust', 'amber', name='categorycolor'), nullable=True))`, and the
upgrade failed with `type "categorycolor" does not exist` (#439).
SQLAlchemy creates a named enum type inline with `create_table` and nowhere
else; Alembic's `add_column` runs against a type nothing created. ADR-0021
recorded this shape as a known gap. The downgrade half was already right —
the type is the revision's by provenance (ADR-0020), so `DROP TYPE` was
rendered after the `drop_column` — which left a revision that drops a type
it never created.

Ferro's bridge now renders the missing half. The generated `upgrade()`
opens with an `op.execute` of the Rust core's guarded `CREATE TYPE` — the
statement the auto-migrate create pass executes for the same model — ahead
of every table operation:

```python
def upgrade() -> None:
    op.execute('DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typname = \'categorycolor\' AND n.nspname = current_schema()) THEN CREATE TYPE "categorycolor" AS ENUM (\'rust\', \'amber\'); END IF; END $$')
    op.add_column('card', sa.Column('color', sa.Enum('rust', 'amber', name='categorycolor'), nullable=True))

def downgrade() -> None:
    op.drop_column('card', 'color')
    op.execute('DROP TYPE "categorycolor"')
```

**Which types**: exactly the introduced types (every column declaring the
type is one the revision adds, ADR-0020) that no `create_table` of the
revision carries. The provenance decision gains a second axis for an
introduced type — *creation*: `Inline` when at least one of its columns is
on a table the revision creates (SQLAlchemy issues the `CREATE TYPE` with
that table, and rendering another would fail with `DuplicateObject`),
`Statement` when every column of it is an `add_column`. Both axes come from
one function (`ferro_ddl_lowering::enum_type_provenance`), and both
rendered statements from its FFI carrier (`_core._plan_enum_type_provenance`:
`create_statement` from `render_pg_enum_create_type`, `drop_statement` from
`render_pg_enum_drop_type`), so the upgrade's creation and the downgrade's
drop can never disagree about whose type it is or what it is called. A
reused type (ADR-0021) renders neither.

**One renderer.** The statement is `render_pg_enum_create_type` — the
guarded `DO $$ ... IF NOT EXISTS ... CREATE TYPE ... END $$` block — not a
bare `CREATE TYPE` and not `sa.Enum(...).create(...)`. AGENTS.md I-1 item
17 forbids a second renderer for the same artifact, and this is the only
form the runtime executes. The guard is a consequence, not a goal. On a
database that already carries the type (the #438 orphan residue from a
downgrade on an older ferro) the statement is a no-op, where the
`create_table` shape (ADR-0020) fails with `DuplicateObject` until the
orphan is removed by hand. Both are the same rule — the type is the
revision's by provenance, decided from the revision alone — rendered
through the one statement each side has. What the `add_column` then
binds to depends on where the orphan is. On the **generating** database
the orphan is live, so label addition (ADR-0011) sees it like any other
live type and the same revision renders `ALTER TYPE ... ADD VALUE` for
every declared label it lacks, in its autocommit block beside the no-op
creation, ahead of the table ops. On a **different** database that
carries an orphan the generating one did not, nothing in the revision
knows, and the `add_column` adopts the live label set as it stands; that
is the revision-only rule's trade-off, and the next autogenerate against
that database renders the missing labels.

**Slot (I-12).** The creation is a before-tables statement: it must precede
every `add_column`, and its reverse must follow every `drop_column`. The
comparator still runs at `priority=LAST` (it reads the revision's table and
column ops) and still inserts at the front of the ops list, so the op that
renders nothing for an inline-created type and the op that renders the
creation are one carrier (`FerroEnumTypeIntroducedOp`) in one place, and
`UpgradeOps.reverse()` puts the drop after the last `drop_table` /
`drop_column` as before. Label additions share the front of the revision.
A type absent from the generating database has no label diff, so the two
families name the same type only when the generating database carries an
orphan of it (above), and then both statements are correct in either
order: the creation is a no-op against the live orphan, and the labels
are appended to it.

**Decided from the revision alone**, never from the live catalog, for the
reason ADR-0020 gives. Postgres-only: SQLite stores enums as text and
renders nothing. Alembic-only by construction: the auto-migrate create
pass already executes this statement itself and has no downgrade door.

Decision by owner (2026-09-28), triage of #439 after #441 / #443.

Rejected alternatives:

- **Leave it as Alembic's gap** (the ADR-0021 posture): every revision
  that adds an enum-typed field to an existing table needs a hand edit,
  and the generated file is internally inconsistent — its downgrade drops
  a type its upgrade never made. ADR-0021 rejected ferro creating *every*
  type because that changed the common case and required `render_item`
  everywhere; this creates only the type nothing else creates, changes no
  revision that ran before, and needs no render hook.
- **A bare `CREATE TYPE`** (or `sa.Enum(name=...).create(op.get_bind(),
  checkfirst=False)`): a second renderer for the same artifact (I-1), and
  the runtime and the revision would run different SQL for the same model.
- **Letting SQLAlchemy create it from the column type**: Alembic's
  `add_column` compiles a bare `ALTER TABLE ... ADD COLUMN` and creates no
  type whatever the column's `sa.Enum` / `postgresql.ENUM` flags say
  (alembic#278); plain Alembic users write the `CREATE TYPE` by hand, and
  a rendering through `render_item` would tie this case to the hook for
  no gain.
- **Reading the catalog to skip the creation when the type is live**: the
  ADR-0020 F4 argument; the committed file must mean the same thing on
  every database. The guard in the one renderer already makes the
  statement safe to run against a live type without consulting anything.

## Consequences

- The revision that adds an enum field to an existing table runs end to
  end: upgrade, downgrade, upgrade again.
- The FFI verdict carries `create_statement` (non-null only for an
  introduced type created by statement) beside `drop_statement`; the
  declaring input gains the inline-created column subset and the declared
  labels per type. The comparator passes both mechanically.
- Two `add_column`s of one new type in one revision create it once, ahead
  of both; a type on a `create_table` column and an `add_column` in the
  same revision is created inline with the table, and the `add_column`
  follows the `create_table` (Alembic orders created tables first).
- A generated file still does not import ferro to run.
- `render_item` is not involved: the `add_column` keeps its plain
  `sa.Enum`, which never creates a type.
