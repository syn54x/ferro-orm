"""
Ferro: A High-Performance Rust-Backed Python ORM.

Ferro combines the speed of a Rust engine with the ergonomics of Pydantic models
to provide a seamless, high-performance database experience.
"""

import logging

from . import _deprecations as _deprecations  # noqa: F401 — enable deprecation visibility

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from ._core import (
    clear_registry as _core_clear_registry,
    create_tables as _core_create_tables,
    migrate as _core_migrate,
    reset_engine,
    set_default_connection,
    version,
)
from ._core import (
    _set_schema_ir_modelset,
    connect as _core_connect,
)
from .base import DbType, DbTypeToken, FerroField, FerroNullable, ForeignKey, varchar
from .exceptions import (
    CheckViolationError,
    DataError,
    FerroError,
    ForeignKeyViolationError,
    IntegrityError,
    InterfaceError,
    ModelDoesNotExist,
    NotNullViolationError,
    OperationalError,
    UniqueViolationError,
)
from .fields import BackRef, Field, ManyToMany
from .models import Model, evict_instance, transaction
from .query import Relation
from .raw import Transaction, execute, fetch_all, fetch_one
from .session import Session, engines

# Set up the Ferro logger
_logger = logging.getLogger("ferro")
# Only add a handler if none exists (to avoid duplicate logs)
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(name)s: %(levelname)s: %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    # Prevent propagation to root logger to avoid duplicate messages
    _logger.propagate = False


def clear_registry() -> None:
    """Reset the compiled/registered schema state.

    Delegates to the Rust core (which clears the Rust model registry and the
    pushed SchemaIR modelset) and additionally clears the Python **join-table
    registry**. That registry must be reset here because
    ``connect``/``create_tables``/``migrate`` compile the *full* registry via
    ``compile_registry_schema_ir()``: a join table left behind by a prior run
    would be re-created with foreign keys to tables that no longer exist —
    tolerated by SQLite but rejected by Postgres (``relation ... does not
    exist``). (#153)

    The Python model registry (``_MODEL_REGISTRY_PY``) is intentionally **not**
    cleared here: clearing the Rust registry while keeping the declared Python
    models is what allows cold re-hydration after ``reset_engine`` (see
    ``tests/test_enum_cold_hydration.py``). Callers that want a full Python-side
    reset clear ``_MODEL_REGISTRY_PY`` / ``_PENDING_RELATIONS`` themselves.

    Each purged join table's compiled SchemaIR envelope is also evicted from the
    per-model envelope cache (via ``evict_model_envelope`` — envelope-only, so the
    model registry stays intact per the promise above), keeping the two stores the
    #153 guard depends on in agreement: a lingering join envelope would let a
    future assemble step resurrect the stale join table.
    """
    _core_clear_registry()
    from .state import _JOIN_TABLE_REGISTRY, evict_model_envelope

    for join_table in list(_JOIN_TABLE_REGISTRY):
        evict_model_envelope(join_table)
    _JOIN_TABLE_REGISTRY.clear()


class PoolConfig(BaseModel):
    """Connection pool settings for a named Ferro connection."""

    model_config = ConfigDict(frozen=True)

    max_connections: int = PydanticField(default=5, ge=1)
    min_connections: int = PydanticField(default=0, ge=0)

    @model_validator(mode="after")
    def validate(self) -> "PoolConfig":
        if self.min_connections > self.max_connections:
            raise ValueError("min_connections cannot exceed max_connections")
        return self


async def connect(
    url: str,
    auto_migrate: bool = False,
    name: str | None = None,
    default: bool = False,
    pool: PoolConfig | None = None,
    *,
    identity_map: bool = True,
    migrate_updates: bool = False,
    migrate_destructive: bool = False,
) -> None:
    """
    Establish a connection to the database.

    Args:
        url: The database connection string (e.g., "sqlite:example.db?mode=rwc").
        auto_migrate: If True, automatically create tables for all registered models.
            Existing tables are left untouched unless ``migrate_updates`` /
            ``migrate_destructive`` are also set.
        name: Optional connection name. Omitted connections register as "default".
        default: If True, make this named connection the default for unqualified operations.
        pool: Optional per-connection pool configuration.
        identity_map: If True (default), sessions opened on this connection keep an identity
            map so the same primary key maps to a single Python instance within a session.
            Identity maps are session-scoped: operations outside a session never cache or
            dedup instances. If False, loads on this connection return fresh instances even
            inside a session (lower memory use; no ``a is b`` guarantees across loads).
        migrate_updates: If True, additionally update existing tables to match the
            registered models. Implies ``auto_migrate``. What this covers is
            capability-relative per backend:

            - **Both backends**: ``ALTER TABLE ... ADD COLUMN`` for model fields
              missing from the live table, using the same column DDL ``CREATE TABLE``
              would emit (including single-column indexes and, on Postgres, CHECK
              constraints and foreign keys). NOT NULL fields need a literal default
              to backfill existing rows — connecting fails with a clear error
              otherwise.
            - **Postgres only**: column type changes
              (``ALTER COLUMN ... TYPE ... USING`` cast) and nullability changes
              (``SET/DROP NOT NULL``) when the live column disagrees with the model.
            - **SQLite**: type/nullability drift cannot be changed in place; ferro
              emits a ``UserWarning`` naming the column and pointing at Alembic.
              (SQLite's type affinity makes declared-type drift mostly cosmetic.)
            - **Transactionality**: on Postgres each table's migration plan
              runs inside a single transaction — a mid-plan failure rolls the
              table back to exactly its pre-migration state. On SQLite,
              statements apply one at a time; a mid-run failure can leave
              earlier statements of that table applied (SQLite ALTERs are
              single-statement operations). For transactional multi-step
              SQLite migrations, use the Alembic bridge.

            After any schema change, the connection pool is refreshed so no cached
            statement can observe the pre-migration schema.
        migrate_destructive: If True, additionally **drop** live columns that no
            longer exist on the model (never whole tables). Implies
            ``migrate_updates``. Dropping is dependency-aware: explicit indexes
            covering the column are dropped first; columns that are primary keys or
            enforced by table constraints fail with a clear error instead.

    Raises:
        ValueError: A connection with this name (or a default connection,
            when ``name`` is omitted) is already registered. Use ``name=...``
            for additional connections or ``reset_engine()`` to tear down.

    For schema changes beyond these (renames, primary-key changes, complex
    transforms), use the Alembic bridge — see ``docs/guide/migrations.md``.
    """
    import json as _json
    from .ir.compiler import compile_registry_schema_ir
    from .relations import resolve_relationships

    resolve_relationships()
    _set_schema_ir_modelset(_json.dumps(compile_registry_schema_ir()))

    pool_config = pool or PoolConfig()
    await _core_connect(
        url,
        auto_migrate=auto_migrate,
        name=name,
        default=default,
        max_connections=pool_config.max_connections,
        min_connections=pool_config.min_connections,
        identity_map=identity_map,
        migrate_updates=migrate_updates,
        migrate_destructive=migrate_destructive,
    )


async def create_tables(using=None):
    """
    Manually create tables for all registered models on a connected engine.

    Compiles and pushes the current registry SchemaIR to the Rust runtime
    before delegating to the Rust create entrypoint, so a model defined after
    ``connect()`` (and thus absent from the connect-time snapshot) is still
    created. The runtime emits each ``CREATE TABLE`` from this SchemaIR via the
    shared emitter.

    Args:
        using: Named connection to create tables on, or None for the default.
    """
    import json as _json
    from .ir.compiler import compile_registry_schema_ir
    from .relations import resolve_relationships

    resolve_relationships()
    _set_schema_ir_modelset(_json.dumps(compile_registry_schema_ir()))
    return await _core_create_tables(using=using)


async def migrate(using=None, updates=True, destructive=False):
    """
    Manually run the auto-migrate pass against a connected engine.

    Compiles and pushes the current registry SchemaIR to the Rust runtime,
    then delegates to the Rust migrate entrypoint.

    Args:
        using: Named connection to migrate, or None for the default.
        updates: If True (default), add missing columns and reconcile type/nullability drift.
        destructive: If True, also drop live columns absent from the model. Implies ``updates``.
    """
    import json as _json
    from .ir.compiler import compile_registry_schema_ir
    from .relations import resolve_relationships

    resolve_relationships()
    _set_schema_ir_modelset(_json.dumps(compile_registry_schema_ir()))
    return await _core_migrate(using=using, updates=updates, destructive=destructive)


__all__ = [
    "connect",
    "PoolConfig",
    "Model",
    "FerroError",
    "InterfaceError",
    "OperationalError",
    "DataError",
    "IntegrityError",
    "UniqueViolationError",
    "ForeignKeyViolationError",
    "NotNullViolationError",
    "CheckViolationError",
    "ModelDoesNotExist",
    "DbType",
    "DbTypeToken",
    "FerroField",
    "FerroNullable",
    "varchar",
    "Field",
    "ForeignKey",
    "BackRef",
    "ManyToMany",
    "Relation",
    "version",
    "create_tables",
    "migrate",
    "reset_engine",
    "set_default_connection",
    "clear_registry",
    "evict_instance",
    "transaction",
    "execute",
    "fetch_all",
    "fetch_one",
    "Transaction",
    "Session",
    "engines",
]
