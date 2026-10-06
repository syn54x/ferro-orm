# Ferro ORM

A Python ORM with a Rust core. Models are Pydantic subclasses; schema compiles to SchemaIR and fans out to runtime DDL and the Alembic bridge.

## Language

**Materialized View**:
A PostgreSQL database object that stores the result of a query and is refreshed on demand. In Ferro it is a read-only `MaterializedView` subclass — queryable through the normal ORM, not writable.
_Avoid_: Snapshot table, cache table, denormalized table

**Refresh**:
The explicit operation that repopulates a materialized view from its defining SELECT. Ferro never refreshes at connect time; the user calls `refresh()` when they want updated data.
_Avoid_: Auto-refresh, sync, rebuild

**Postgres-only schema object**:
A model artifact that Ferro emits only on PostgreSQL. On SQLite the class still registers for imports and typing, but DDL is skipped and querying raises a clear error.
_Avoid_: Dialect-specific model, PG-only table

**Redefine**:
Replacing an existing materialized view by dropping and recreating it when its defining SELECT changes. Authorized by `migrate_materialized_redefine=True`; otherwise connect fails loudly on drift.
_Avoid_: Alter, migrate, update

**Materialized view column**:
A flat, typed field on a `MaterializedView` — same declarations as `Model` fields, but no `ForeignKey`, `BackRef`, or `ManyToMany`. Reference related entities by scalar columns (`order_id: int`), not relations.
_Avoid_: Relation column, FK field

**Materialized query**:
The `ClassVar` SQL string (`__materialized_query__`) that defines what rows a materialized view stores. Declared alongside typed fields; Ferro validates that SELECT output matches the field contract.
_Avoid_: Select SQL, view definition, query body

**Read-only view**:
A `MaterializedView` that can be queried but never mutated. `save()`, `delete()`, and `create()` raise a clear error.
_Avoid_: Immutable model, snapshot model

**Storage token**:
A word in the canonical `db_type` vocabulary (`text`, `bigint`, `timestamptz`, `jsonb`, …) naming how a column is stored. One shared vocabulary feeds every emitter; a token never means different things to different emitters.
_Avoid_: SQL type string, dialect type, column type name

**Storage lowering**:
The dialect-side degrade of a storage token to the nearest type a backend supports (e.g. `jsonb` stores as plain JSON on SQLite). Lowering is silent and documented; it never changes value semantics — only the on-disk representation.
_Avoid_: Fallback type, emulation, downgrade

**Json-family field**:
A field whose values Ferro stores as JSON documents: `dict`, `list` (any element type, including nested models), or a nested Pydantic model. Only json-family fields may opt into JSON storage tokens such as `jsonb`.
_Avoid_: Object field, blob field, document column

**Column spec**:
The single authoritative record of one column's facts — identity, type, and constraints — derived exactly once from the field declaration. Provisional at class-body time; authoritative once relationship resolution completes.
_Avoid_: Column metadata, enriched schema property, field dict

**Relation traversal**:
Attribute access on a declared forward-FK field inside a query lambda (`lambda t: t.account.ledger_id`), reaching a related model's columns from the root. Traversal narrows the result to rows where the relation exists; keeping rows without the relation requires an explicit left join.
_Avoid_: Join inference, nested filter, path lookup, string path

**Relation path**:
The ordered sequence of forward-FK hops a traversal walks (`account`, or `account → owner`). A path is the identity of a join: the same path referenced anywhere in a query is one join, and distinct paths to the same model are distinct joins. Left-join requests apply to a whole path.
_Avoid_: Join alias, lookup chain, dotted path string

**Existence test**:
The only predicate form on a reverse or many-to-many relation — `t.lines.exists(...)` — asking whether at least one related row exists, optionally scoped by a full inner predicate over the related model. Renders as a correlated EXISTS; the result stays root-shaped (shape-preserving query), so it composes with any other predicate, ordering, and paging. Reverse relations are *tested*, never *traversed*: traversal remains a forward-FK concept.
_Avoid_: membership filter, semi-join filter, reverse traversal, subquery filter, `.any()`

**Shape-preserving query**:
The invariant that filtering and ordering never change what a query returns — a query over Transaction yields Transaction instances regardless of which relations its predicates traverse. Only an explicit projection operation may change the result shape.
_Avoid_: Implicit projection, row narrowing

**Complete-instance invariant**:
A model instance always carries a complete row — there is no such thing as a partial or deferred-field model instance, anywhere. Anything narrower than a full row (a column subset, an aggregate) comes back as a projected record, never as the model type.
_Avoid_: Partial instance, deferred field, lightweight model, .only()

**Projected record**:
The result of an explicit projection — a typed record of named values that is not a model instance and cannot be saved, refreshed, or identity-mapped. Column subsets and aggregation results are projected records; complete model rows are not. Realized as `Row`, delivered in the list-like `Rows` container.
_Avoid_: Partial model, row dict, value tuple

**Include**:
The explicit request (`.include(lambda t: t.account.owner)`) that a query populate a forward-FK relation path — the data axis of a query, distinct from joins (membership) and projection (shape). Including a path populates every hop along it and never changes which rows come back.
_Avoid_: Eager load, select_related, prefetch, join-fetch

**Populated relation**:
A forward-FK field carrying its complete related instance, attached by an explicit include on the query. Attribute access returns the instance directly — no await, no query — matching the field's declared type. An unpopulated relation keeps the awaitable contract; population changes cost and attached data, never the result type (there is no separate "loaded" model type).
_Avoid_: Eager-loaded field, select_related, prefetched attribute, joined attribute

**Materialization plan**:
A query's declaration of what its result columns become: complete root instances (every query today), a projected record of named fields, or — in the future — a populated instance graph. Every query carries exactly one plan; the plan travels with the query rather than being inferred from its column list.
_Avoid_: Select list, projection spec, hydration mode flag

**QueryIR payload**:
The single typed wire artifact a query ships to the Rust runtime — model identity, predicates, ordering, paging, joins, and exactly one materialization plan, inside a versioned envelope. Compiled only by `compile_query`; no other code assembles query wire shape.
_Avoid_: Query dict, query def, payload dict

**Paging**:
The QueryIR window over matching rows: a size (`limit`) and a start. A start is either an offset or one position bound (`after` or `before`, never both). Both bounds are exclusive of the position. A limited `before` is the adjacent previous page; an unbounded `before` is every earlier row in declared order. Paging is not a predicate — it does not change which rows match — and `count()` drops it.
_Avoid_: pagination, cursor, page filter

**Position**:
The ordered tuple of a query's order-key values that marks one row's place in that order. Two rows never share a position: the order keys include the model's primary key. A non-PK slot may be empty (`None`); the PK slot may not. `after`/`before` start the page from a position; `position_of` reads one off a model instance, or off a projected record that carries every order key. Traversed order keys require those relations populated. Not a cursor — encoding is the caller's.
_Avoid_: cursor, bookmark, page token, keyset

**Order key**:
One term in a query's `order_by`: the column (root or traversed), its direction, and its null placement. A position holds one value per order key, in declaration order.
_Avoid_: sort field, sort column, order term

**Null placement**:
Where NULL sort keys land for one order key: `last`, `first`, or `native` (that backend's own default — Postgres and SQLite are opposites). Omitted means `last`, so the same order on every backend. `native` is never implied.
_Avoid_: dialect default, omitted nulls

**Compiled query**:
The single artifact `compile_query` returns: the QueryIR payload, its wire JSON, and the plan-scoped hop-class map, all views of one compile. The map is collected from the hop facts the payload itself carries, so wire and hop classes can never disagree; it is `None` unless the materialization plan decodes or hydrates through a hop model's class (mirroring the Rust `needs_hop_classes` guard — a both-sides double-check). No other code assembles hop classes for the FFI.
_Avoid_: payload + kwargs, hop-class side-channel, wire tuple

**Golden vector**:
A hand-authored JSON fixture pinning one wire shape — the independent authority both the Python emitter and the Rust decoder assert against, never regenerated from either side's output. Updating one by hand is the contract-review moment for a wire change.
_Avoid_: Snapshot, fixture dump, example payload

**Aggregate projection**:
A projection containing at least one aggregate field. Each group collapses to exactly one projected record: every non-aggregate field is a group key, so grouping is derived from the projection and never declared separately. With no non-aggregate fields, the whole result collapses to a single record. Grouping collapses rows — bucketing complete instances by a key ("partitioning") is a different, client-side operation and is not grouping.
_Avoid_: Group-by query, summary query, rollup, partition

**Traversed projection**:
A projected record field whose source column lives across a forward-FK relation path (`select(lambda t: t.account.name)`). Projection traversal narrows exactly like predicate traversal (ADR-0006). Unaliased, the field takes the bare leaf column name; two selected fields sharing an output name is a build-time error, resolved with an output alias.
_Avoid_: Nested select, join column, related-field pull

**Output alias**:
A user-chosen name for one field of a projected record, given as the key in a dict-returning selector (`select(lambda t: {"account_name": t.account.name})`). Aliases name output fields only — never joins or tables; the relation path remains the sole join identity.
_Avoid_: Column alias, AS label, join alias

**Primary key fact**:
The single cached answer to "which column is this model's primary key" — derived once at the compile choke point alongside column specs, stored as `__ferro_pk__` (`None` for a PK-less model). At most one `primary_key=True` column may be declared; a violation raises at class definition time. Operations that need a PK read the cached fact and raise clearly when it is `None` — never guess.
_Avoid_: PK lookup, PK scan, first primary-key column

**Registry**:
The single owner of Python-side registration state (`ferro.registry.REGISTRY`): model classes, per-model SchemaIR envelopes + fingerprints, join-table bundles, pending relations, the modelset artifact, and the generation counters. Its stores are private; the agreement invariants (fingerprint pairs its envelope, join-table eviction clears envelopes, one-call test reset) live inside the interface, not in caller convention. Routing/session state is not the Registry — that is `ferro.state`.
_Avoid_: state module, global dicts, model cache

**Provisional registration**:
The per-model state installed when a class body finishes executing — enough for runtime codec and PK metadata, but relationships may still be pending and the modelset is not yet authoritative for DDL.
_Avoid_: Import-time registration, partial registry

**Resolved registration**:
The registry epoch after relationship resolution completes — join tables exist, shadow FK columns are wired, and the SchemaIR modelset is authoritative for DDL and auto-migrate.
_Avoid_: Final registration, committed registry

**Create pass**:
The auto-migrate step that brings missing tables into existence — the table, its columns, its indexes and constraints, together. A table that already exists is left completely untouched by this pass, whatever its shape.
_Avoid_: Bootstrap, ensure-tables, table sync

**Reconciliation pass**:
The `migrate_updates` step that alters existing schema objects — tables and ferro-owned enum types — to match the registered models; the only authority for DDL against an object that already exists. Within one table, column changes land before the indexes and constraints that reference them; label additions land before any table's changes.
_Avoid_: Update pass, schema sync, drift repair

**Migration** (in-house):
The numbered unit of schema-and-data change in ferro's own migration system: what a developer reviews, applies, and reverts as one thing. It is a directory holding one or more ordered *steps* and its *schema snapshot*. Numbered sequentially, so numbers count migrations, never steps. Distinct from a *generated revision*, which is Alembic's unit.
_Avoid_: Change, change set, revision, version

**Step**:
One file inside a migration, applied and recorded on its own so a failure resumes where it stopped. A DDL step is SQL rendered by the same functions the reconciliation pass runs, once per *target dialect*; a *data step* is Python and dialect-neutral.
_Avoid_: Operation, phase, sub-migration

**Down** (of a step):
The reverse of a *step*, kept beside it: what a *run* executes to revert that step. Every step has one. A DDL step's down returns the schema to what the previous migration's *schema snapshot* declares, never the rows a drop removed. A down either does work, declares with a reason that there is nothing to reverse, or declares the step an *irreversible step*.
_Avoid_: Rollback (a transaction rolls back), downgrade (Alembic's word), undo

**Irreversible step**:
A step whose *down* is a declaration, with a stated reason, that it cannot be reversed. A *run* that would have to revert it refuses before reverting anything. Only a person declares one; a generated step is never irreversible.
_Avoid_: One-way step, forward-only step, missing down

**Data step**:
A Python step that moves or transforms rows and never changes schema. It sees *historical models*, never the models in the codebase today, so it keeps working after the codebase moves on. Every data step is either an *atomic data step* or a *chunked data step*; there is no third shape, and the step says which it is: neither is assumed.
_Avoid_: Data migration script, RunPython, backfill file

**Historical model**:
A throwaway class a data step queries and saves through, built from a *schema snapshot* rather than from code: the columns present in either the previous migration's snapshot or the migration's own, which is the table as it stands between that migration's expand and contract steps: a column only the migration adds is nullable there, and a renamed column appears once, under its new name. It carries columns only — no relations, methods or validators. Every table in that union has one, a many-to-many join table included, though no class was ever written for it.
_Avoid_: Frozen model, snapshot model, old model

**Atomic data step**:
A data step that runs as one transaction with its step record committed inside it, so the whole step lands or none of it does. The shape for work small enough to hold in one transaction.
_Avoid_: Transactional step, small data step

**Chunked data step**:
A data step the runner drives in batches over one declared query, one transaction per batch with the *cursor* committed in it, so an interrupted step resumes at its last completed batch and never replays a row.
_Avoid_: Batched migration, non-transactional step, manual loop

**Cursor**:
The position of the last row of a chunked data step's last committed batch — its order-key values, primary key included — from which the next batch, or the next run, continues.
_Avoid_: Offset, progress marker, checkpoint

**Step context**:
What a *data step* is handed to do its work: the *historical models*, raw SQL on the step's own transaction, the *target dialect*, and a log. It offers no way to open, commit or leave that transaction, and no way to the models in the codebase today.
_Avoid_: Migration context, environment, connection

**Unwritten step**:
A scaffolded step that still lacks what only a person can supply: a *data step* holding the marker where a value belongs, or a hand-requested SQL step with no statement. A migration holding one is refused when it is loaded, so it never runs and never writes a placeholder.
_Avoid_: Stub, placeholder step, empty step

**Guard step**:
A generated, complete *data step* that stands where a scaffolded backfill would, written when the developer states that no row needs a value. It changes nothing and fails the migration if such a row exists.
_Avoid_: Assertion step, skipped backfill, no-op step

**Backfill**:
A scaffolded *data step* that supplies the values a *schema change* demands of rows that already exist: a new required column, a column that stops accepting `NULL`, an enum label rows still carry. It works only on the rows that still need a value, so running it again is exact. It is an *unwritten step* until a person supplies what the generator cannot know.
_Avoid_: Data migration, populate step, seed step

**Expand step**:
The generated DDL step that runs before a migration's data steps. It holds only what existing rows already satisfy: a new column created nullable, with its foreign key and check; on a table that already exists the column's index and unique follow the data steps as *index steps*. A migration with no data step has no expand step; its DDL is one schema step.
_Avoid_: Pre-step, additive step, phase one

**Contract step**:
The generated DDL step that runs after a migration's data steps. It holds what the rows had to be prepared for (`NOT NULL`, the removal of an enum label) and every *destructive step* statement, so a *backfill* can still read a column the same migration drops.
_Avoid_: Post-step, cleanup step, tighten step

**Staged `NOT NULL`**:
How a generated Postgres migration makes an existing column required without scanning its table under a lock that blocks writers: an *add-constraint step* installs a temporary check that refuses `NULL` on every new write, and the *contract step* validates it, sets `NOT NULL` and removes it. Always generated on Postgres for a table that already exists; a table the same migration creates needs none, and SQLite has no equivalent (a *table rebuild*).
_Avoid_: Write-side guard, NOT VALID trick, bouncer

**Add-constraint step**:
The generated Postgres DDL step between a migration's data steps and its *contract step* that installs the temporary check of a *staged `NOT NULL`* and commits, so the contract's validation never runs under the lock the installation takes. Its down removes the check. Not a *guard step*, which is a data step.
_Avoid_: Stage step, guard, pre-contract

**Staged constraint**:
How a generated Postgres migration adds a foreign key or a check to a table that already exists without scanning it under a lock that blocks writers: the constraint is installed under its own name as not yet validated, refusing every new write that violates it, and a later step validates it. Always generated on Postgres for a table that already exists, whether or not the column is new; a table the same migration creates needs none; a unique cannot be staged and is an *index step* instead; SQLite has no equivalent (a *table rebuild*). A declared constraint found live but not validated is *drift*.
_Avoid_: NOT VALID trick, deferred constraint, lazy constraint

**Validate step**:
The generated Postgres DDL step that validates a migration's *staged constraints* when the migration has no *contract step* to do it in. It is the *data-dependent step* of that migration; its down returns each constraint to not yet validated. Never holds a *table rebuild*.
_Avoid_: Verify step, check step, post-schema step

**Index step**:
The generated DDL step that builds or drops one index on a table that already exists, after the migration's data steps. On Postgres it is a *no-transaction step* that builds without blocking writers, and its first statement removes whatever an earlier failed build left behind under that name, so it is exact from the top; on SQLite it holds the plain index inside a transaction. The step for a unique is the migration's *data-dependent step*: a duplicate fails it, and nothing is scaffolded to resolve one. An index on a table the same migration creates needs none; a unique is never a constraint, so this is the whole of its online form.
_Avoid_: Concurrent index step, CONCURRENTLY step, index migration

**DDL lock timeout**:
How long a DDL statement that ferro executes on Postgres, in a *run* or in the *reconciliation pass*, waits for a table lock before giving up, so a statement queued behind one long query never queues every other query behind itself. A statement that gives up is retried, its step from the first statement, a fixed number of times before the step fails. Set per *database* in *project configuration*; distinct from the wait for the *run lock*, and not applied to a *data step*.
_Avoid_: Lock timeout (the run-lock wait), statement timeout, lock retry

**Restructure scaffold**:
The generated expand, backfills and contract for a change that replaces a key and everything that references it, spanning the parent table and each child. A primary-key change is the one case today.
_Avoid_: PK migration, key swap, multi-table split

**No-transaction step**:
A DDL step that declares, in its file, that the runner opens no transaction around it, so statements that refuse to run inside one (`CREATE INDEX CONCURRENTLY`) can. Because its record can no longer commit with its DDL, it must be safe to re-run from its first statement. A *table rebuild* is not one.
_Avoid_: No-tx step, autocommit step, unsafe step

**Table rebuild**:
The SQLite rendering of a schema change SQLite cannot make in place: the table is created again in its new shape under a temporary name, its rows are copied across, and it replaces the old table. It sits inside the DDL step whose change needs it (the *expand step*, a schema step, or the *contract step*; never a *validate step*, *add-constraint step* or *index step*), folds every change that step makes to the table, and recreates the table and its indexes as they stand after the step, so one table may be rebuilt once per such step. A step that rebuilds several tables is still one atomic step. It needs foreign-key enforcement off while it runs, which its file declares and the runner arranges. It refuses a live table holding anything the *schema snapshot* does not declare, since the copy would discard it. Distinct from a *constraint rebuild*, which touches no rows.
_Avoid_: Batch operation, table recreate, copy-and-move, 12-step

**Run**:
One invocation of the in-house migration runner against one database, from taking the *run lock* to releasing it. It goes one way: it applies zero or more pending *steps* in order, or reverts applied steps in reverse order through their *downs*, and stops at the first that fails.
_Avoid_: Deploy, session, migration (a run applies migrations; it is not one)

**Run lock**:
The lock that admits one *run* per database at a time. It is released by the database or the operating system when the running process dies, never by a timeout or an operator, so a crashed run can always be told from a live one.
_Avoid_: Migration lock, lock row, mutex

**Tracking table**:
The table inside a database where the in-house migration system keeps one *step record* per step it has started there. It says where that database stands now, not what was ever done to it: reverting a step removes its record.
_Avoid_: Ledger, history table, version table, migration log

**Step record**:
One row of the *tracking table*: a *step* that was started on this database, the checksum of the file that was run and of its migration's *schema snapshot*, and whether it finished. A record that is started and not finished marks where the next *run* resumes; a chunked data step's record also carries its *cursor*. A chunked step being reverted keeps its record, marked as reverting and carrying the down's own cursor, until its last batch.
_Avoid_: Migration row, version row, applied migration

**Baseline**:
Recording migrations as applied on a database that already has their schema, built by auto-migrate or Alembic before the project had migrations. Nothing is executed: the database is checked against the *schema snapshot* of the last migration being recorded, and the *step records* are written only when it shows no *drift*. A baselined migration is never reverted by running its down steps, since it created nothing there; undoing a baseline removes the records.
_Avoid_: Fake, stamp, mark-applied

**Schema snapshot**:
The declared modelset as it was when a migration was generated, stored inside the migration and linked to the snapshot before it. It is the previous state the generator diffs against and one of the two states *historical models* are built from — what the models said, never what the migration's SQL would produce.
_Avoid_: IR dump, state file, history, replayed state

**Database** (configured):
A named set of models whose tables live together, declared in project configuration. It has one migration lineage. Many connections may reach one database, and one database's migrations may be run against many servers: a tenant per server, SQLite locally and Postgres in production. A model belongs to a database by the module that defines it, never by a declaration on the class.
_Avoid_: Alias, connection name, app

**Target dialect**:
A database dialect a project's migrations are generated for, declared in its configuration per *database*. A migration carries one rendering of each DDL step per target dialect and nothing for any other.
_Avoid_: Backend, supported database, flavor

**Rendering** (of a DDL step):
One *target dialect*'s SQL for a DDL step. Every DDL step has a rendering for every target dialect under the same step number; where a dialect has no work the rendering says so explicitly.
_Avoid_: Variant, dialect file, translation

**Schema change**:
A difference between two *schema snapshots* that renders DDL. A difference that renders none (a Python default, a back-reference) is not a schema change and generates no migration.
_Avoid_: Model change, diff, IR change

**Rename hint**:
A declaration on a model or enum saying what a column, a table or an enum label was called before. Without one, a rename is indistinguishable from a drop and an add, and generates exactly that. A hint is live only while the previous *schema snapshot* still holds the old name and lacks the new one; after its migration is generated it is inert and may be deleted. An enum type's rename needs no hint: it is read off the columns that moved to it.
_Avoid_: Rename marker, rename directive, migration hint

**Destructive step**:
A generated DDL step that discards data when it runs: it drops a column, a table or an enum type. It is always generated and says so in its file; review is the gate, never a flag or a refusal.
_Avoid_: Dangerous step, unsafe step, data-loss migration

**Data-dependent step**:
A generated DDL step that discards nothing but fails on a database whose rows do not satisfy it: a type change whose cast fails, a `NOT NULL`, a unique over existing rows, the validation of a *staged constraint*, the removal of an enum label rows still carry. It says so in its file.
_Avoid_: Risky step, may-fail step, conditional step

**Drift**:
A live database whose *ferro-owned artifacts* disagree with the *schema snapshot* of the last migration applied to it. A database behind the newest migration is pending, not drifted. A live table the snapshot does not declare is not drift: it was never ferro's. Drift is reported, never repaired by the migration system and never a generator input.
_Avoid_: Schema mismatch, out-of-sync, dirty database

**Pending**:
A *migration* or *step* the project's migrations directory holds and a database's *tracking table* has no finished *step record* for. A database with anything pending is behind, which is not *drift*; an application can refuse to start on it or apply it.
_Avoid_: Unapplied, outstanding, out of date

**Ahead**:
A database whose *tracking table* holds *step records* for migrations the running code's migrations directory does not have: an older build of the application meeting a newer schema. Refused by default, and allowed only when the application says so.
_Avoid_: Newer database, future migration, unknown migration

**Migration test harness**:
What a project's test suite uses to stand a throwaway database at any migration by name, seed it through that migration's *historical models*, apply or revert one named migration, and walk the whole lineage. It carries the targets and the promptless revert the application's own calls refuse, because a test is about one migration and is deleted with it. It never makes a database fresh, never skips a *target dialect*, and names no state between two steps of one migration.
_Avoid_: Test runner, fixtures, migration sandbox

**Round trip**:
The test that applies every *migration* in order, reverts every one, and applies them again, checking after each stop that the database shows no *drift* against the *schema snapshot* it should stand at. It proves every *down* reaches its parent. An *irreversible step* ends the downward walk at its migration, reported rather than failed: everything above it round-trips, everything below is applied only.
_Avoid_: Up-down test, reversibility check, migration smoke test

**Project configuration**:
The committed file that declares a project's *databases* to ferro's tooling: which modules hold the models, the *target dialects*, where the migrations live. It is one file, never two merged, and never overridden from the environment. Distinct from *session settings*, which are Postgres values on a `Session`.
_Avoid_: Settings (alone), env config, config layers

**Generated revision**:
Alembic's unit of schema change, written by `alembic revision --autogenerate` through ferro's bridge. It holds what the *reconciliation pass* would do to the database it was generated against, with destructive changes included, since it is reviewed before it runs: nothing more, so an empty one means no *drift*. It carries schema changes only; a change that demands values of existing rows is written plain and marked, and its *backfill* belongs to a *migration*.
_Avoid_: Migration (ferro's own unit), autogenerated migration, Alembic migration

**Ferro-owned artifact**:
A schema object ferro may reconcile to match the declared model. Indexes and constraints are ferro-owned by naming (`idx_`, `uq_`, `fk_`, `ck_`); native enum types are ferro-owned by derivation — the type's name matches the name ferro derives from the model. A generated revision owns an enum type a third way, by provenance: it introduces every column of the type, so its downgrade drops the type (see *Type drop*); a type it adds a column of but does not introduce is one it reuses, never creates (see *Type reuse*). Artifacts owned none of these ways belong to the user and are never altered or dropped.
_Avoid_: Managed index, system constraint, internal index

**Enum label**:
One storable value of a native Postgres enum type, mirrored from a Python `StrEnum` member's value. Members are the Python-side declaration; labels are what the database accepts and stores.
_Avoid_: Enum value, variant, choice

**Label addition**:
The reconciliation-pass operation appending model-declared labels missing from a live ferro-owned enum type. Append-only and metadata-only: rows are never touched, and labels the database has but the model lacks are warned about loudly and never removed — removal and rename are reviewed-migration territory.
_Avoid_: Enum sync, label reconciliation, enum evolution

**Type drop**:
The reverse of a generated revision's creation of a native enum type — inline with `create_table`, or by *Type creation* when only `add_column`s carry it: the `DROP TYPE` the revision's `downgrade()` emits, after its last `drop_table` and `drop_column`, for each native enum type the revision introduces — every column declaring it is one the revision adds, and none is one the downgrade puts back. Decided from the revision alone, never from the live catalog; the type is the revision's by provenance, not derivation (ADR-0020). Alembic core has no operation for either direction; ferro's bridge supplies the drop, and the creation for the `add_column`-only shape, so a downgrade leaves no type behind and an upgrade finds every type it needs.
_Avoid_: Enum cleanup, type teardown, cascade drop

**Type creation**:
The `CREATE TYPE` a generated revision's `upgrade()` executes, ahead of its table operations, for a native enum type it introduces (see *Type drop*) that no `create_table` of the revision carries — every column of it is an `add_column`, and SQLAlchemy creates a named enum type inline with `create_table` only. It is the runtime create pass's guarded statement, rendered by the same function, so both migration doors run the same SQL for the same model; a type a `create_table` carries is created inline by SQLAlchemy and gets no statement. The second axis of the one type-provenance decision (ADR-0022).
_Avoid_: Explicit enum create, add_column type fix, pre-create

**Type reuse**:
A generated revision's use of a native enum type it does not introduce — a column the revision adds declares it, but so does a column the downgrade leaves standing or puts back — so the type already lives on every database the revision can run against. The revision's `create_table` columns of that type render as `postgresql.ENUM(..., create_type=False)` (through the bridge's `render_item` hook, since SQLAlchemy's `repr` omits the flag) and no `DROP TYPE` is emitted. The other verdict of the one type-provenance decision that also decides the type drop, made from the revision alone (ADR-0021).
_Avoid_: Shared type, existing type, live-type exclusion

**Constraint rebuild**:
Drop-and-recreate of a ferro-owned constraint whose live definition no longer matches the declared model — a foreign key's `on_delete`, its target, its columns, or a table check's predicate. Metadata-only: rows are never touched. On a backend that cannot alter constraints, ferro warns loudly and skips; it never diverges silently.
_Avoid_: Constraint alter, FK patch, in-place constraint update

**Column check**:
A single-column CHECK that restricts a closed-domain field to its declared labels (`Field(db_check=True)` → `col IN (...)`). Named `ck_<table>_<col>`; not a table check.
_Avoid_: enum check, db_check constraint, value check

**Table check**:
A named boolean invariant over one row of one table, declared on the model as a lambda and enforced by the database as a CHECK constraint. The live name is `ck_<table>_<suffix>`; the suffix is what the model declares.
_Avoid_: table-level check, multi-column check, composite check, row check

**Check predicate**:
The lambda body of a table check — a ferro predicate over that model's own columns (or a forward-FK null test on the relation or its shadow `*_id`), with literal values only. Relation traversal, existence tests, and aggregates are not check predicates.
_Avoid_: check SQL, constraint expression, check body

**Session settings**:
Key/value Postgres settings (GUCs) belonging to a ferro `Session`, applied by ferro to whichever database connection runs each of the session's statements. The unit of tenancy scope for Row-Level Security.
_Avoid_: Session variables, Postgres session state, connection settings

**Settings delivery**:
How session settings reach the database: `transaction` (default — `SET LOCAL` inside every transaction, implicit ones included; safe behind any pooler) or `connection` (opt-in — `SET` once on a pinned connection, reset on close; direct-Postgres only). Delivery is the mechanism; session settings are the values.
_Avoid_: Pooler mode, GUC mode, SET mode

**Operation atomicity**:
Every ferro operation is atomic: it issues one statement, or — when it must issue several — runs them inside the ambient transaction when one exists and self-wraps in its own transaction when none does (the `bulk_create` chunking contract, #298). Settings delivery rides this invariant; it never creates a new atomicity boundary.
_Avoid_: Implicit transaction, auto-commit batching, per-statement autocommit

**Row policy**:
One named row-visibility rule on a model, enforced by Postgres as a `rls_<table>_<name>` policy. Declared as a column/setting shorthand (rendered with `NULLIF` and the column spec's cast) or a raw expression; scoped to a command; permissive (OR) or restrictive (AND).
_Avoid_: RLS rule, tenant filter, row filter

**Row security declaration**:
The table-level `RowSecurity(*policies, force=True)` ClassVar (`__ferro_rls__`) — the single owner of a model's row-security facts: its policies plus the table flags (`ENABLE`, `FORCE`). Ferro reconciles it one-way: flags and ferro-owned policies are created and rebuilt to match the model, never disabled or dropped outside `migrate_destructive`.
_Avoid_: Policy tuple, RLS config, security metadata

**Policy rebuild**:
Drop-and-recreate of a ferro-owned row policy whose live catalog entry no longer matches the declaration — its command, its permissive/restrictive composition, which clauses it carries, or a body ferro itself rendered. Metadata-only: no row is read, validated, or rewritten. A body the *author* wrote (the raw `using=`/`with_check=` form) is never rebuilt on a textual difference — Postgres stores its own rewriting of raw SQL, so ferro reports the difference with both texts instead (ADR-0019).
_Avoid_: Policy alter, policy sync, RLS drift repair
