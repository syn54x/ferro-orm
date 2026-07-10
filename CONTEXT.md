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

**Provisional registration**:
The per-model state installed when a class body finishes executing — enough for runtime codec and PK metadata, but relationships may still be pending and the modelset is not yet authoritative for DDL.
_Avoid_: Import-time registration, partial registry

**Resolved registration**:
The registry epoch after relationship resolution completes — join tables exist, shadow FK columns are wired, and the SchemaIR modelset is authoritative for DDL and auto-migrate.
_Avoid_: Final registration, committed registry
