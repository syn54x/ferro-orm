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

**Aggregate projection**:
A projection containing at least one aggregate field. Each group collapses to exactly one projected record: every non-aggregate field is a group key, so grouping is derived from the projection and never declared separately. With no non-aggregate fields, the whole result collapses to a single record. Grouping collapses rows — bucketing complete instances by a key ("partitioning") is a different, client-side operation and is not grouping.
_Avoid_: Group-by query, summary query, rollup, partition

**Traversed projection**:
A projected record field whose source column lives across a forward-FK relation path (`select(lambda t: t.account.name)`). Projection traversal narrows exactly like predicate traversal (ADR-0006). Unaliased, the field takes the bare leaf column name; two selected fields sharing an output name is a build-time error, resolved with an output alias.
_Avoid_: Nested select, join column, related-field pull

**Output alias**:
A user-chosen name for one field of a projected record, given as the key in a dict-returning selector (`select(lambda t: {"account_name": t.account.name})`). Aliases name output fields only — never joins or tables; the relation path remains the sole join identity.
_Avoid_: Column alias, AS label, join alias

**Provisional registration**:
The per-model state installed when a class body finishes executing — enough for runtime codec and PK metadata, but relationships may still be pending and the modelset is not yet authoritative for DDL.
_Avoid_: Import-time registration, partial registry

**Resolved registration**:
The registry epoch after relationship resolution completes — join tables exist, shadow FK columns are wired, and the SchemaIR modelset is authoritative for DDL and auto-migrate.
_Avoid_: Final registration, committed registry
