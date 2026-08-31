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

**Ferro-owned artifact**:
A schema object ferro may reconcile to match the declared model. Indexes and constraints are ferro-owned by naming (`idx_`, `uq_`, `fk_`, `ck_`); native enum types are ferro-owned by derivation — the type's name matches the name ferro derives from the model. Artifacts owned neither way belong to the user and are never altered or dropped.
_Avoid_: Managed index, system constraint, internal index

**Enum label**:
One storable value of a native Postgres enum type, mirrored from a Python `StrEnum` member's value. Members are the Python-side declaration; labels are what the database accepts and stores.
_Avoid_: Enum value, variant, choice

**Label addition**:
The reconciliation-pass operation appending model-declared labels missing from a live ferro-owned enum type. Append-only and metadata-only: rows are never touched, and labels the database has but the model lacks are warned about loudly and never removed — removal and rename are reviewed-migration territory.
_Avoid_: Enum sync, label reconciliation, enum evolution

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
