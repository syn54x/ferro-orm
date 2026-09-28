# A generated downgrade drops the enum types its create_table ops created

A generated revision that creates `category(color: CategoryColor)` also
creates the native Postgres type `categorycolor`: SQLAlchemy emits
`CREATE TYPE` inline with `create_table`, and Alembic has no op for it. The
rendered `downgrade()` is a bare `op.drop_table('category')`, so the type
outlives the downgrade and the next upgrade fails with `type "categorycolor"
already exists` (#438). Ferro's Alembic bridge adds the missing half: the
generated `downgrade()` ends with `op.execute('DROP TYPE "categorycolor"')`,
after the last `drop_table`, for each native enum type the revision's
`create_table` ops created.

A type is this revision's by **provenance**, not derivation: it is not live
when the revision is generated, and at least one table declaring it is one
the revision creates, so this revision's `create_table` is what brings it
into being. A type that is not live has no existing column using it. Its
only users outside the created tables are columns the same revision adds
(`add_column` on an existing table), and the downgrade drops those before
it reaches the `DROP TYPE`. After the downgrade nothing uses the type.
ADR-0011's derivation ownership (the type's name matches the one ferro
derives from the model) is not what decides this, and ADR-0011's
additive-only argument ("the worst misattribution appends a label") does
not stretch to cover a drop. ADR-0011 stands as written; it governs label
addition only.

No destructive gate applies. The drop is the exact reverse of a creation
the same revision makes, the same posture ADR-0013 takes for autogenerate:
a generated revision is reviewed before it runs, so running autogenerate is
itself the request for the full diff.

The exclusion: a type already live at generation time is not this
revision's, and no drop is proposed for it, even when every table declaring
it is created by the revision. That exclusion is also what keeps a type a
table outside the revision still uses (one `include_object` hides, or one
in an earlier revision), because such a table can only be using a live
type. The live-type exclusion is under review (panel finding F4 on #441:
a type left live by an earlier broken downgrade makes the committed
revision's downgrade incomplete everywhere else) and may be revisited
together with #443.

The decision (`ferro_ddl_lowering::enum_types_created_with_tables`) and the
rendered statement (`render_pg_enum_drop_type`) live in the Rust core; the
Alembic comparator consumes both over FFI (`_core._plan_enum_type_drop`)
and executes the statement verbatim, and a generated revision does not
import ferro to run. There is no runtime counterpart: auto-migrate has no
downgrade door. `render_pg_enum_drop_type` is single-sourced so a future
runtime consumer (a runtime teardown, a SQL dump) shares it rather than
growing a second renderer. SQLite is unaffected: enums store as text and
there is no type to drop.

Decision by owner (2026-09-28), review panel on #441.

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

## Consequences

- The downgrade of a revision that creates a type is a true inverse: after
  it runs, the same upgrade runs again cleanly.
- `DROP TYPE` has no `CASCADE` and no `IF EXISTS`. If a hand-added column
  still depends on the type, the downgrade fails with a dependency error and,
  on Postgres, rolls back as one transaction.
- The comparator runs at `priority=LAST` (it reads the revision's
  `CreateTableOp`s) and inserts at the front of the ops list, so
  `UpgradeOps.reverse()` places the drop after every `drop_table`
  (AGENTS.md I-12).
- The type-drop decision reads its own live input (every enum type name in
  the schema). Label addition's catalog read is unchanged and stays
  byte-parallel with the runtime pass.
