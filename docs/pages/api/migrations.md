# Migrations

The Alembic bridge. `get_metadata()` builds a SQLAlchemy `MetaData` describing all registered Ferro models from the compiled SchemaIR modelset, so Alembic's `--autogenerate` can diff your models against the live database and emit migration scripts. Assign it to `target_metadata` in your Alembic `env.py` (requires the `ferro-orm[alembic]` extra). See the [Schema Migrations guide](../guide/migrations.md) for the full workflow.

Internal JSON-derivation helpers (`_build_sa_table`, `_map_to_sa_type`) remain deprecated (not yet removed as of `v0.14.0`). Replace internal usages with `get_metadata()`; see [Upgrade Guide: Alembic metadata](../howto/upgrade-guide.md#alembic-metadata-build-from-get_metadata).

::: ferro.migrations.alembic.get_metadata
