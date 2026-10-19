"""Ferro, an async Python ORM on Pydantic models with a Rust core."""

import logging
import threading
from collections.abc import Callable
from typing import Any, Literal

from . import _deprecations as _deprecations  # noqa: F401 — enable deprecation visibility

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from ._core import (
    clear_registry as _core_clear_registry,
    create_tables as _core_create_tables,
    migrate as _core_migrate,
    reset_engine as _core_reset_engine,
    set_default_connection as _core_set_default_connection,
    version as _core_version,
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
from .pass_report import (
    ExecutedStatement,
    PassReport,
    Report,
    _carrying_report,
    _run_pass,
)
from .query import Relation, Row, Rows, now
from .raw import Transaction, execute, fetch_all, fetch_one
from .rowsecurity import RowPolicy, RowSecurity
from .session import Session, current_session, engines
from .settings import (
    DatabaseSettings,
    FerroSettings,
    SettingsError,
    lock_timeout_setting_seconds,
    parse_ddl_lock_timeout,
)

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
    """Return the schema of every registered model, relationships resolved.

    It is the schema [`connect`][ferro.connect],
    [`create_tables`][ferro.create_tables] and `ferro migrate new` read,
    as a JSON-ready dict. Relationships between models are resolved first,
    so a model declared since the last call is included. Declaring models
    in another thread while it runs is unsupported and can raise.

    Returns:
        The schema as a dict, ready for `json.dumps`.
    """
    # Clean registry: assemble the already-compiled envelopes. Dirty:
    # resolve relationships (which clears the generation counter), then
    # assemble, under the lock so two threads resolve once.
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
    """Drop the schema the engine holds for the registered models.

    Call it in a test fixture, after [`reset_engine`][ferro.reset_engine],
    when the next test declares its own models: the next `connect` then
    builds the schema only from what is registered at that point, and
    leaves out the many-to-many join tables of earlier tests. Model classes
    you have declared stay declared and keep working after the next
    `connect`.

    Examples:
        ```python {test=run prelude=connections}
        from ferro import clear_registry, connect, reset_engine

        await connect("sqlite::memory:", auto_migrate=True)
        reset_engine()
        clear_registry()
        await connect("sqlite::memory:", auto_migrate=True)
        ```
    """
    # The Rust core forgets its model registry and pushed modelset; the
    # Python join-table registry (and each join table's compiled envelope)
    # is cleared here, since a join table left behind would be recreated
    # with foreign keys to tables that may no longer exist. The Python model
    # registry is kept on purpose, so models re-hydrate after reset_engine();
    # tests that need a full reset use REGISTRY.reset_for_test().
    _core_clear_registry()
    from .registry import REGISTRY

    REGISTRY.clear_join_tables()


def version() -> str:
    """Return the version of the installed Ferro.

    Examples:
        ```python {test=run prelude=reference}
        from importlib.metadata import version as installed

        from ferro import version

        assert version() == installed("ferro-orm")
        ```
    """
    return _core_version()


def reset_engine() -> None:
    """Close every connection and drop every open session and transaction.

    Call it between tests to start each one from a clean engine. The next
    [`connect`][ferro.connect] without a name registers as the default
    again. Declared models stay registered; see
    [`clear_registry`][ferro.clear_registry] to drop their schema as well.

    Raises:
        RuntimeError: The engine's state could not be locked.

    Examples:
        ```python {test=run prelude=connections}
        from ferro import connect, reset_engine

        await connect("sqlite::memory:", auto_migrate=True)
        reset_engine()
        await connect("sqlite::memory:", auto_migrate=True)
        ```
    """
    _core_reset_engine()


def set_default_connection(name: str) -> None:
    """Set the connection `engines.session()` opens when called without a name.

    See [The default connection](../guide/connections.md#the-default-connection)
    for the other two ways to set it.

    Args:
        name: A connection registered with `connect(url, name=...)`.

    Raises:
        ValueError: No connection is registered under `name`, or `name` holds
            a character other than an ASCII letter, digit or underscore.
        RuntimeError: The engine's state could not be locked.

    Examples:
        ```python {test=run prelude=connections}
        from ferro import connect, engines, set_default_connection

        await connect("sqlite::memory:", name="app", default=True)
        await connect("sqlite::memory:", name="archive")
        set_default_connection("archive")
        async with engines.session() as session:
            assert session.connection_name == "archive"
        ```
    """
    _core_set_default_connection(name)


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
    """Pool size and settings delivery for one connection.

    Pass it to [`connect`][ferro.connect] as `pool=`. `settings_delivery`
    chooses how a session sends its
    [session settings](../concepts/row-level-security.md#session-settings)
    to Postgres; read
    [Connection delivery](../concepts/row-level-security.md#connection-delivery)
    for what `"connection"` saves and what it costs before you opt in.

    Raises:
        pydantic.ValidationError: A size is out of range, or
            `min_connections` exceeds `max_connections`.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import PoolConfig, connect

        pool = PoolConfig(max_connections=10, min_connections=1)
        await connect("sqlite::memory:", name="pooled", pool=pool)
        assert pool.settings_delivery == "transaction"
        ```
    """

    model_config = ConfigDict(frozen=True)

    max_connections: int = PydanticField(default=5, ge=1)
    """The most connections the pool opens at once. Defaults to 5; at least 1."""
    min_connections: int = PydanticField(default=0, ge=0)
    """The connections the pool keeps open while idle. Defaults to 0."""
    settings_delivery: Literal["transaction", "connection"] = "transaction"
    """How sessions send their settings to Postgres. Defaults to `"transaction"`.

    `"transaction"` sets them inside each transaction, which is safe behind a
    transaction-mode pooler. `"connection"` sets them once on a connection the
    session holds until it closes; use it only when the pool reaches Postgres
    directly.
    """

    @model_validator(mode="after")
    def _check_bounds(self) -> "PoolConfig":
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
    """Open a connection pool and register it under a name.

    Call it once at startup. ORM calls then run inside a session opened on
    the connection; see [Sessions](../guide/connections.md#sessions).

    The three migrate flags make the database match the registered models
    as the connection opens, each one going further than the last. They run
    under the run lock `ferro migrate up` takes and read the project's
    `lock_timeout` and `ddl_lock_timeout` settings.
    [Auto-migrate](../guide/schema/auto-migrate.md) lists what each flag
    changes on each backend and how the lock waits. A change the pass
    refuses, such as a primary-key change, is a migration's job: see
    [Migrations](../guide/schema/migrations.md).

    Args:
        url: The database URL, such as `"sqlite:app.db?mode=rwc"`,
            `"sqlite::memory:"` or `"postgresql://user:password@localhost/app"`.
        auto_migrate: Create the missing tables of every registered model.
            Existing tables are left as they are. Defaults to `False`.
        name: The connection's name, made of ASCII letters, digits and
            underscores. Defaults to `None`, which registers it as
            `"default"`.
        default: Make this named connection the default, the one
            `engines.session()` opens when given no name. Defaults to `False`.
        pool: The pool's size and how sessions send their settings. Defaults
            to `None`, which means `PoolConfig()`.
        identity_map: Keep an identity map in each session on this
            connection, so one primary key loads as one instance within the
            session. Defaults to `True`.
        migrate_updates: Also change existing tables to match the models:
            add columns and indexes, and on Postgres change types,
            nullability and foreign keys. Implies `auto_migrate`. Defaults to
            `False`.
        migrate_destructive: Also drop columns the models no longer declare,
            never whole tables. Implies `migrate_updates`. Defaults to
            `False`.

    Raises:
        ValueError: A connection with this name is already registered (or a
            default one, when `name` is omitted), or `name` holds a
            character other than an ASCII letter, digit or underscore. Pass
            another `name`, or call [`reset_engine`][ferro.reset_engine]
            first.
        ferro.migrations.MigrationRefused: A migrate flag was given for a
            database that migrations govern (run `ferro migrate up` instead),
            the run lock stayed held longer than `lock_timeout`, or the
            Postgres URL goes through a transaction-mode pooler.
        OperationalError: On Postgres, a statement of the pass waited longer
            than `ddl_lock_timeout` for its table lock on ten attempts. Its
            `.report` lists what committed before the failure.
        SettingsError: The configured databases set different `lock_timeout`
            or `ddl_lock_timeout` values.

    Examples:
        ```python {test=run prelude=connections}
        from ferro import connect, engines

        await connect("sqlite::memory:", auto_migrate=True)
        async with engines.session():
            await Author.create(name="Ada Lovelace", email="ada@example.com")
            assert len(await Author.all()) == 1
        ```
    """
    _ensure_rust_registration_synced()

    # Only an auto-migrate flag reads the config: a plain connect() is
    # exactly what it was.
    guard = (
        _auto_migrate_settings()
        if auto_migrate or migrate_updates or migrate_destructive
        else {}
    )
    pool_config = pool or PoolConfig()
    await _carrying_report(
        _core_connect(
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
            **guard,
        )
    )


def _auto_migrate_settings() -> dict[str, Any]:
    """What the auto-migrate passes read from ``FerroSettings()``, as the
    Rust entry points' keyword arguments: every configured
    ``tracking_schema`` for the guard (ADR-0038), the ``ddl_lock_timeout``
    the create and reconciliation passes wait for table locks under
    (ADR-0044), and the ``lock_timeout`` they wait for the run lock under
    (ADR-0038 as amended). No config file is not an error: the guard still
    reads the catalog, and each timeout is its default.

    Raises:
        SettingsError: the configured databases set different
            ``ddl_lock_timeout`` or ``lock_timeout`` values."""
    settings = FerroSettings()
    return {
        "tracking_schemas": sorted(
            {
                database.tracking_schema
                for database in settings.databases.values()
                if database.tracking_schema is not None
            }
        ),
        "ddl_lock_timeout_s": _agreed_seconds(
            settings,
            "ddl_lock_timeout",
            lambda value: parse_ddl_lock_timeout(value).total_seconds(),
        ),
        "lock_timeout_s": _agreed_seconds(
            settings, "lock_timeout", lock_timeout_setting_seconds
        ),
    }


def _agreed_seconds(
    settings: FerroSettings, key: str, seconds: Callable[[str], float]
) -> float:
    """The duration setting ``key`` an auto-migrate pass runs under, read by
    ``seconds``: the value every configured database sets, or the default
    with no config.

    ``connect()`` names no database (ADR-0036), so databases that set
    different values leave no single answer: that is refused, naming each
    database's value and the fix, never settled by a silent default."""
    databases = settings.databases
    if not databases:
        return seconds(DatabaseSettings.model_fields[key].default)
    values = {seconds(getattr(database, key)) for database in databases.values()}
    if len(values) == 1:
        return values.pop()
    each = ", ".join(
        f'`{name}` = "{getattr(database, key)}"'
        for name, database in sorted(databases.items())
    )
    raise SettingsError(
        f"{settings.config_path} sets different {key} values ({each}), "
        f"and an auto-migrate pass does not name a database to take one from; set "
        f"the same {key} on every database, or configure one database"
    )


async def create_tables(using: str | None = None) -> PassReport:
    """Create the missing tables of every registered model.

    A table that already exists is left as it is; changing one is
    [`migrate`][ferro.migrate]'s job. A model defined after
    [`connect`][ferro.connect] is created too. Like `connect`'s migrate
    flags, it runs under the run lock and refuses a database that
    migrations govern.

    Args:
        using: The connection to create the tables on. Defaults to `None`,
            the default connection.

    Returns:
        Every statement the pass ran and every warning it raised, as a
            [`PassReport`][ferro.PassReport].

    Raises:
        ferro.migrations.MigrationRefused: Migrations govern the database,
            or the run lock stayed held longer than `lock_timeout`.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import Field, Model, create_tables


        class Note(Model):
            id: int | None = Field(default=None, primary_key=True)
            body: str


        report = await create_tables()
        assert [statement.sql for statement in report.statements] == [
            'CREATE TABLE IF NOT EXISTS "note" ( "body" varchar NOT NULL, '
            '"id" integer NOT NULL PRIMARY KEY AUTOINCREMENT )'
        ]
        ```
    """
    _ensure_rust_registration_synced()
    return await _run_pass(_core_create_tables(using=using, **_auto_migrate_settings()))


async def migrate(
    using: str | None = None, updates: bool = True, destructive: bool = False
) -> PassReport:
    """Run the auto-migrate pass on an open connection.

    It runs the same pass as `connect(migrate_updates=True)`: it creates the
    missing tables, then changes existing ones to match the models. See
    [On-demand `migrate()`](../guide/schema/auto-migrate.md#on-demand-migrate)
    for what it changes on each backend. Like `connect`'s migrate flags, it
    runs under the run lock and refuses a database that migrations govern.

    Args:
        using: The connection to migrate. Defaults to `None`, the default
            connection.
        updates: Change existing tables as well as creating missing ones.
            Defaults to `True`; `False` runs only the create pass, as
            [`create_tables`][ferro.create_tables] does.
        destructive: Also drop columns the models no longer declare. Implies
            `updates`. Defaults to `False`.

    Returns:
        Every statement the pass sent, in order, and every warning it raised,
            as a [`PassReport`][ferro.PassReport]. Each warning is also raised
            as a `UserWarning`.

    Raises:
        ferro.migrations.MigrationRefused: Migrations govern the database,
            or the run lock stayed held longer than `lock_timeout`.
        OperationalError: A statement failed partway through the pass. Its
            `.report` lists what committed before the failure.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import Field, Model, execute, migrate

        await execute('CREATE TABLE "tag" ("id" integer PRIMARY KEY, "label" varchar NOT NULL)')


        class Tag(Model):
            id: int | None = Field(default=None, primary_key=True)
            label: str
            color: str | None = None


        report = await migrate()
        assert [statement.sql for statement in report.statements] == [
            'ALTER TABLE "tag" ADD COLUMN "color" varchar'
        ]
        ```
    """
    _ensure_rust_registration_synced()
    return await _run_pass(
        _core_migrate(
            using=using,
            updates=updates,
            destructive=destructive,
            **_auto_migrate_settings(),
        )
    )


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
    "PassReport",
    "ExecutedStatement",
    "Report",
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
    "FerroSettings",
    "DatabaseSettings",
    "SettingsError",
]
