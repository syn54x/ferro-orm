# Migrations

The Alembic bridge. `get_metadata()` builds a SQLAlchemy `MetaData` describing all registered Ferro models from the compiled SchemaIR modelset, so Alembic's `--autogenerate` can diff your models against the live database and emit migration scripts. Assign it to `target_metadata` in your Alembic `env.py` (requires the `ferro-orm[alembic]` extra). See the [Schema Migrations guide](../guide/migrations.md) for the full workflow.

Internal JSON-derivation helpers (`_build_sa_table`, `_map_to_sa_type`) are deprecated and scheduled for removal in `v0.13.0`. Replace internal usages with `get_metadata()`; see [Migrating to v0.12.0](../howto/migrating-to-v0-12-0.md).

::: ferro.migrations.alembic.get_metadata
