# Migrations

The Alembic bridge. `get_metadata()` builds a SQLAlchemy `MetaData` describing all registered Ferro models, so Alembic's `--autogenerate` can diff your models against the live database and emit migration scripts. Assign it to `target_metadata` in your Alembic `env.py` (requires the `ferro-orm[alembic]` extra). See the [Schema Migrations guide](../guide/migrations.md) for the full workflow.

::: ferro.migrations.alembic.get_metadata
