# A generated downgrade drops the enum types its create_table ops created

A generated revision that creates `category(color: CategoryColor)` also
creates the native Postgres type `categorycolor`: SQLAlchemy emits
`CREATE TYPE` inline with `create_table`, and Alembic has no op for it. The
rendered `downgrade()` is a bare `op.drop_table('category')`, so the type
outlives the downgrade and the next upgrade fails with `type "categorycolor"
already exists` (#438). Ferro's Alembic bridge adds the missing half: the
generated `downgrade()` ends with `op.execute('DROP TYPE "categorycolor"')`,
after the last `drop_table` and `drop_column`, for each native enum type
the revision introduces.

A type is this revision's by **provenance**, not derivation: every column
declaring it is one the revision adds — a column of a table its
`create_table` creates, or a column its `add_column` adds — so this
revision is what brings the type into being, and the downgrade, which
drops those columns and tables first, is what removes its last use. A type
with any column the downgrade leaves standing or puts back (a pre-existing
column on a surviving table, including a table `include_object` hides from
the revision, or a column the revision drops) is kept. ADR-0011's derivation ownership (the type's name matches
the one ferro derives from the model) is not what decides this, and
ADR-0011's additive-only argument ("the worst misattribution appends a
label") does not stretch to cover a drop. ADR-0011 stands as written; it
governs label addition only.

The decision is made **from the revision alone**, never from the live
catalog. A generated revision is a file that runs against many databases;
what it does on downgrade must follow from what it does on upgrade, not
from what one developer's database held on the afternoon it was generated.
Alembic's `create_table` issues `CREATE TYPE` unconditionally, so on every
database the upgrade can run against, the upgrade is what created the
type.

No destructive gate applies. The drop is the exact reverse of a creation
the same revision makes, the same posture ADR-0013 takes for autogenerate:
a generated revision is reviewed before it runs, so running autogenerate is
itself the request for the full diff.

The decision (`ferro_ddl_lowering::enum_types_introduced_by_revision`) and the
rendered statement (`render_pg_enum_drop_type`) live in the Rust core; the
Alembic comparator consumes both over FFI (`_core._plan_enum_type_drop`)
and executes the statement verbatim, and a generated revision does not
import ferro to run. There is no runtime counterpart: auto-migrate has no
downgrade door. `render_pg_enum_drop_type` is single-sourced so a future
runtime consumer (a runtime teardown, a SQL dump) shares it rather than
growing a second renderer. SQLite is unaffected: enums store as text and
there is no type to drop.

Decision by owner (2026-09-28), review panel on #441 (finding F4 chose the
revision-only rule over a live-catalog exclusion).

Rejected alternatives:

- **Render the columns into `drop_table`** so SQLAlchemy's `after_drop`
  hook emits `DROP TYPE` itself: Alembic's renderer never writes columns
  into a `drop_table`, and even with them the hook fires per table, so a
  type shared by two new tables would drop after the first table while the
  second still uses it.
- **Render `sa.Enum(name=...).drop(op.get_bind(), checkfirst=False)`**
  instead of the Rust-rendered statement: a second renderer for the same
  artifact, which I-1 forbids. The quoting and the statement would stop
  being pinned by one function.
- **A destructive gate** (propose the drop only under an opt-in flag): the
  flag would guard the reverse of something the same revision creates, and
  a generated revision is reviewed before it runs. The gate would only
  reintroduce #438 for everyone who did not set it.
- **Exclude a type that is already live when the revision is generated**
  ("the upgrade did not create it, so the downgrade must not drop it"):
  the most common way a type is live with no user is the #438 residue
  itself. A developer downgrades on an older ferro, the type is orphaned,
  they regenerate the revision, no drop is rendered, their upgrade fails
  once (#443), they drop the orphan by hand, and the committed file has no
  `DROP TYPE` for every other database. The exclusion only ever protected
  a hand-edited revision, and whoever edits the upgrade can edit the
  downgrade. Rejected in the #441 review panel (F4).

## Consequences

- The downgrade of a revision that creates a type is a true inverse: after
  it runs, the same upgrade runs again cleanly.
- `DROP TYPE` has no `CASCADE` and no `IF EXISTS`. If a hand-added column
  still depends on the type, the downgrade fails with a dependency error and,
  on Postgres, rolls back as one transaction.
- The comparator runs at `priority=LAST` (it reads the revision's
  `CreateTableOp`s and `AddColumnOp`s) and inserts at the front of the ops
  list, so `UpgradeOps.reverse()` places the drop after every `drop_table`
  and `drop_column` (AGENTS.md I-12).
- The type-drop decision reads nothing from the database. Label addition's
  catalog read is unchanged and stays byte-parallel with the runtime pass.
- A pre-existing orphan type that this revision's `create_table` adopts is
  dropped by the downgrade. Nothing used it, and on that database the
  upgrade cannot run until the orphan is removed anyway (#443).
- A type a table outside ferro's metadata still uses (a hand-made table
  sharing a ferro type name) makes the downgrade fail with a dependency
  error rather than silently keeping the type; the revision is then edited
  by hand, in the open.
- The comparator reads `CreateTableOp`s and `AddColumnOp`s (inside
  `ModifyTableOps`); an `add_column`-only revision that introduces a type
  drops it on downgrade too.
- A column the revision drops is one the downgrade restores, so it counts
  as a surviving user: the comparator reads the reverse of each
  `DropColumnOp` / `DropTableOp` (Alembic builds them from the reflected
  live column, enum type included) and adds those columns to the declaring
  set. Moving an enum column from one table to a new one keeps the type.
