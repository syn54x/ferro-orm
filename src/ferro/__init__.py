"""
Ferro: A High-Performance Rust-Backed Python ORM.

Ferro combines the speed of a Rust engine with the ergonomics of Pydantic models
to provide a seamless, high-performance database experience.
"""

import logging
import threading
from typing import Any, Literal

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
    _install_registration,
    connect as _core_connect,
)
from .base import DbType, DbTypeToken, FerroField, FerroNullable, ForeignKey, varchar
from .checks import Check
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
from .query import Relation, Row, Rows, now
from .raw import Transaction, execute, fetch_all, fetch_one
from .rowsecurity import RowPolicy, RowSecurity
from .session import Session, current_session, engines

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


_RESOLVED_MODELSET_LOCK = threading.Lock()
_OPERATION_REGISTRATION_SYNC_LOCK = threading.Lock()


async def _ensure_rust_registration_synced_for_operation() -> None:
    """Registration-only sync before ORM operations touch Rust (#247).

    Clean path (O(1)): in-process generation comparison only — no FFI.
    Dirty path: resolve + bulk install under a threading single-flight guard
    (``_OPERATION_REGISTRATION_SYNC_LOCK``), matching ``_RESOLVED_MODELSET_LOCK``
    two functions up. The guarded section is fully synchronous — no ``await``
    inside — so a ``threading.Lock`` gives real cross-thread single-flight
    without the cross-loop hang of a module-global ``asyncio.Lock``. Under
    contention the event loop blocks for one resolve+install per generation; do
    not offload this to a thread pool — that would reintroduce interleaving.
    """
    from .registry import REGISTRY

    if not REGISTRY.is_dirty():
        return

    with _OPERATION_REGISTRATION_SYNC_LOCK:
        if REGISTRY.is_dirty():
            _ensure_rust_registration_synced()


def ensure_resolved_modelset() -> dict[str, Any]:
    """Ensure the SchemaIR modelset reflects the current registry.

    Clean path (O(1)): one in-process generation comparison, then assemble
    already-compiled envelopes without recompiling. Dirty path: run
    ``resolve_relationships()`` (which clears the generation counter on success),
    then assemble (#245).

    Defining model classes concurrently with ``connect``/``create_tables``/``migrate``
    is unsupported: a registry entry can become visible before its envelope is
    persisted, producing a transient missing-envelope error on the unlocked fast
    path.
    """
    from .ir.compiler import compile_registry_schema_ir
    from .registry import REGISTRY
    from .relations import resolve_relationships

    if not REGISTRY.is_dirty():
        return compile_registry_schema_ir()

    with _RESOLVED_MODELSET_LOCK:
        if REGISTRY.is_dirty():
            resolve_relationships()
        return compile_registry_schema_ir()


def _ensure_rust_registration_synced() -> None:
    """Resolve the modelset if needed, then push it to the Rust runtime."""
    from .registry import REGISTRY

    envelope = ensure_resolved_modelset()
    cached = REGISTRY.modelset()
    _push_registration_to_rust(
        envelope, fingerprint=cached[1] if cached is not None else None
    )


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

    The Python model registry is intentionally **not** cleared here: clearing
    the Rust registry while keeping the declared Python models is what allows
    cold re-hydration after ``reset_engine`` (see
    ``tests/test_enum_cold_hydration.py``). Callers that want a full
    Python-side reset use ``REGISTRY.reset_for_test()``.

    Each purged join table's compiled SchemaIR envelope is evicted with it —
    ``Registry.clear_join_tables`` owns that agreement (#153): a lingering
    join envelope would let a future assemble step resurrect the stale join
    table.
    """
    _core_clear_registry()
    from .registry import REGISTRY

    REGISTRY.clear_join_tables()


def _push_registration_to_rust(
    envelope: dict[str, Any] | None = None,
    *,
    fingerprint: str | None = None,
) -> None:
    """Install an assembled modelset in the Rust runtime.

    The single Rust registration sync seam (#244): hands the assembled modelset
    plus its fingerprint to the atomic bulk-install FFI. Rust builds the column
    registry, swaps the registry + modelset + fingerprint under one lock
    (build-then-swap, retained-last-good on failure), and skips the swap
    entirely when the fingerprint already matches — so a clean reconnect installs
    nothing.

    When called from ``_ensure_rust_registration_synced``, pass the envelope and
    cached fingerprint returned by ``ensure_resolved_modelset()`` so the clean
    path performs one assemble and one fingerprint. With no arguments, assembles
    (or re-assembles) the current registry for direct callers such as tests.
    """
    import json as _json

    from .ir.compiler import compile_registry_schema_ir, schema_ir_fingerprint
    from .registry import REGISTRY

    if envelope is None:
        envelope = compile_registry_schema_ir()
    if fingerprint is None:
        cached = REGISTRY.modelset()
        if cached is not None and cached[0] is envelope:
            fingerprint = cached[1]
        else:
            fingerprint = schema_ir_fingerprint(envelope)
    _install_registration(_json.dumps(envelope), fingerprint)


class PoolConfig(BaseModel):
    """Connection pool settings for a named Ferro connection.

    ``settings_delivery`` chooses how sessions on this connection get their
    session settings (the Postgres GUCs row-level-security policies read) onto
    the database. The two modes send different SQL for the very same session:

        async with engines.session(settings={"myapp.tenant_id": "acme"}):
            invoices = await Invoice.where(lambda invoice: invoice.paid).all()

    ``"transaction"`` (the default) wraps the query in a transaction of its own
    and scopes the value to it::

        BEGIN
        SELECT set_config($1, $2, true)    -- 'myapp.tenant_id', 'acme'
        SELECT "invoice".* FROM "invoice" WHERE "paid"
        COMMIT

    ``"connection"`` sets the value once, on a connection it keeps for the
    session, and then sends your statements bare::

        SELECT set_config($1, $2, false), set_config($3, $4, false)
        -- 'myapp.tenant_id', 'acme', 'ferro.pinned_keys', 'myapp.tenant_id'
        SELECT "invoice".* FROM "invoice" WHERE "paid"
        ...                                -- every later query, no wrap
        SELECT set_config($1, NULL, false), set_config($2, NULL, false)
        -- at session close: exactly the keys above, reset

    The difference that matters is ``true`` vs ``false`` — Postgres'
    ``is_local`` flag. ``true`` makes the value die with the transaction, which
    is why the default is safe even behind a transaction pooler like PgBouncer,
    where consecutive statements can land on different backends. ``false``
    makes it live for the whole database session, which is only safe if that
    session belongs to one Ferro session and nobody else — hence the pinned
    connection.

    So ``settings_delivery="connection"`` is a promise about your deployment:
    **this pool talks to Postgres directly.** Ferro never guesses it. A pooler
    is invisible to its clients, and guessing wrong means one tenant's value
    answering another tenant's query.

    What you get for the promise: no per-operation wrap at all, so a
    settings-bearing session costs one extra round trip for its whole life
    instead of about two per operation outside ``transaction()``.

    What it costs, and there are four costs worth knowing before you opt in.

    **One connection per scoped session.** A settings-bearing session holds a
    pool connection from its first operation until it closes, so **no more
    settings-bearing sessions can run at once than the pool has
    connections**. Number ``max_connections`` + 1's first query waits for a
    connection, exactly like any other pool checkout. Size the pool for peak
    concurrent scoped sessions.

    **One session, one connection, therefore one thing at a time.** Everything
    a pinned session does is serialized: two sibling tasks sharing the session
    run one after the other, and while a ``transaction()`` block is open a
    sibling task's operation waits for it rather than running inside it.
    That is the honest meaning of a session that owns a single connection —
    the same fact as the cap above, seen from inside one session. Operations
    in the same task *inside* a ``transaction()`` block are unaffected; they
    already own the block.

    **A marker check on release while anyone is pinned.** The pool verifies
    that a connection coming back is not still carrying a session's settings.
    While no session is pinned this costs nothing at all. While *any* session
    on the pool is pinned, every connection release on that pool performs one
    marker check — including releases by settings-less sessions and by
    sessionless operations.

    **Schema changes wait for scoped sessions.** ``connect(migrate_updates=…)``
    and ``migrate()`` refresh the pool afterwards, and a refresh cannot finish
    until every connection comes back — including the pinned ones. Migrating
    while long-lived tenant sessions are open therefore blocks until they
    close; Ferro warns, naming how many it is waiting on. Run schema changes
    before opening tenant-scoped sessions.

    Sessions *without* settings never pin: same statements, same connections,
    no wrap, no serialization.

    Two smaller behaviours worth knowing:

    * A session that opens with no ``settings`` and gains them later via
      ``set_config`` has nothing pinned yet, so it pins on its **next
      operation** and applies the values then.
    * Closing a session resets its keys to the value the connection *started*
      with. For an ordinary custom setting that is the empty string, which is
      what fail-closed policies rely on. If an operator has set a default with
      ``ALTER ROLE ... SET myapp.tenant_id = ...`` or ``ALTER DATABASE ... SET
      ...``, resetting brings that value *back* rather than clearing it — so
      never configure a tenancy key as a role or database default.
    """

    model_config = ConfigDict(frozen=True)

    max_connections: int = PydanticField(default=5, ge=1)
    min_connections: int = PydanticField(default=0, ge=0)
    settings_delivery: Literal["transaction", "connection"] = "transaction"

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
            Existing tables are left completely untouched — whatever their shape —
            unless ``migrate_updates`` / ``migrate_destructive`` are also set.
        name: Optional connection name. Omitted connections register as "default".
        default: If True, make this named connection the default for unqualified operations.
        pool: Optional per-connection pool configuration, including
            ``settings_delivery`` — how sessions on this connection deliver
            their session settings (see ``PoolConfig``).
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
              to backfill existing rows — a json-family object/array (`Field(default={})`,
              `default_factory=dict`) counts; connecting fails with a clear error
              otherwise.
            - **Postgres only**: column type changes
              (``ALTER COLUMN ... TYPE ... USING`` cast) and nullability changes
              (``SET/DROP NOT NULL``) when the live column disagrees with the model.
              Also foreign-key reconciliation: a ferro-owned (``fk_``-named)
              constraint whose definition drifts from the declared FK
              (``on_delete``, target) is rebuilt (``DROP CONSTRAINT`` +
              ``ADD CONSTRAINT``), and a declared FK missing entirely from an
              existing column is added. A drifting constraint ferro does not
              own is warned about, never altered.
            - **SQLite**: type/nullability drift cannot be changed in place; ferro
              emits a ``UserWarning`` naming the column and pointing at Alembic.
              (SQLite's type affinity makes declared-type drift mostly cosmetic.)
              FK constraints likewise cannot be added or altered on an existing
              table; any foreign-key drift warns loudly instead of diverging
              silently.
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
    _ensure_rust_registration_synced()

    pool_config = pool or PoolConfig()
    await _core_connect(
        url,
        auto_migrate=auto_migrate,
        name=name,
        default=default,
        max_connections=pool_config.max_connections,
        min_connections=pool_config.min_connections,
        settings_delivery=pool_config.settings_delivery,
        identity_map=identity_map,
        migrate_updates=migrate_updates,
        migrate_destructive=migrate_destructive,
    )


async def create_tables(using=None):
    """
    Manually create the *missing* tables for registered models on a connected
    engine. A table that already exists is left completely untouched; altering
    existing tables belongs to ``migrate(updates=True)``.

    Compiles and pushes the current registry SchemaIR to the Rust runtime
    before delegating to the Rust create entrypoint, so a model defined after
    ``connect()`` (and thus absent from the connect-time snapshot) is still
    created. The runtime emits each ``CREATE TABLE`` from this SchemaIR via the
    shared emitter.

    Args:
        using: Named connection to create tables on, or None for the default.
    """
    _ensure_rust_registration_synced()
    return await _core_create_tables(using=using)


async def migrate(using=None, updates=True, destructive=False):
    """
    Manually run the auto-migrate pass against a connected engine.

    Compiles and pushes the current registry SchemaIR to the Rust runtime,
    then delegates to the Rust migrate entrypoint.

    Args:
        using: Named connection to migrate, or None for the default.
        updates: If True (default), add missing columns and reconcile
            type, nullability, and foreign-key definition drift (see ``connect``).
        destructive: If True, also drop live columns absent from the model. Implies ``updates``.
    """
    _ensure_rust_registration_synced()
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
    "Check",
    "RowSecurity",
    "RowPolicy",
    "ForeignKey",
    "BackRef",
    "ManyToMany",
    "Relation",
    "Row",
    "Rows",
    "version",
    "create_tables",
    "migrate",
    "reset_engine",
    "set_default_connection",
    "clear_registry",
    "ensure_resolved_modelset",
    "evict_instance",
    "transaction",
    "execute",
    "fetch_all",
    "fetch_one",
    "Transaction",
    "Session",
    "engines",
    "current_session",
    "now",
]
