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
