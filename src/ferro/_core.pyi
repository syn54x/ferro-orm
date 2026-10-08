from collections.abc import Callable
from typing import Any, Optional

class RouteHandle:
    """Opaque, immutable route for one operation (FF-D D3).

    Resolved exactly once by ``ferro.state.resolve_operation_scope`` /
    ``resolve_transaction_scope`` and threaded by value through every FFI
    operation. ``connection_name`` is never ``None`` — a routeless handle is
    unrepresentable, not an error branch.
    """

    def __init__(
        self,
        connection_name: str,
        tx_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None: ...
    @property
    def connection_name(self) -> str: ...
    @property
    def tx_id(self) -> Optional[str]: ...
    @property
    def session_id(self) -> Optional[str]: ...

def register_model_schema(name: str, schema: str, table_name: str) -> None: ...
async def connect(
    url: str,
    auto_migrate: bool = False,
    name: Optional[str] = None,
    default: bool = False,
    max_connections: int = 5,
    min_connections: int = 0,
    *,
    identity_map: bool = True,
    migrate_updates: bool = False,
    migrate_destructive: bool = False,
    settings_delivery: str = "transaction",
    tracking_schemas: list[str] = ...,
    ddl_lock_timeout_s: float = 5.0,
) -> None: ...
async def create_tables(
    using: Optional[str] = None,
    tracking_schemas: list[str] = ...,
    ddl_lock_timeout_s: float = 5.0,
) -> None: ...
async def migrate(
    using: Optional[str] = None,
    updates: bool = True,
    destructive: bool = False,
    tracking_schemas: list[str] = ...,
    ddl_lock_timeout_s: float = 5.0,
) -> None:
    """Run the auto-migrate pass against a connected engine.

    Creates missing tables, then (with ``updates``, the default) adds missing
    model columns to existing tables and reconciles type/nullability drift on
    Postgres; with ``destructive`` it also drops live columns no longer on the
    model. ``destructive`` implies ``updates``. The pool is refreshed after any
    DDL so no cached statement observes the pre-migration schema. On Postgres
    each reconciliation statement waits for a table lock under
    ``ddl_lock_timeout_s`` seconds (``0`` disables; ADR-0044).
    """
    ...

def _render_create_table_sql_for_test(
    name: str, schema_json: str, dialect: str
) -> tuple[str, list[str], list[str]]:
    """Test-only: render CREATE TABLE SQL + post-create + pre-create fragments
    (``(create_sql, post_create_sqls, pre_create_sqls)``) without executing.
    Pre-create carries the idempotent native-enum ``CREATE TYPE`` guards.

    ``schema_json`` is a SchemaIR *payload* JSON string of the shape
    ``{"dialect_agnostic": bool, "models": [<SchemaModel>...]}`` produced by
    ``ferro.ir.compiler.compile_schema_ir_payload``; the model matching ``name``
    (or the first) is rendered through the shared ``render_create_table`` emitter
    the runtime uses. Used by the cross-emitter parity test (U5). ``dialect`` is
    ``"postgres"`` or ``"sqlite"``.
    """
    ...

def _render_migration_sql_for_test(
    name: str,
    schema_ir_json: str,
    live_columns_json: str,
    dialect: str,
    updates: bool = True,
    destructive: bool = False,
    live_indexes_json: str = "",
    live_foreign_keys_json: str = "",
    live_checks_json: str = "",
    live_row_security_json: str = "",
) -> tuple[list[str], list[str]]:
    """Test-only: render the auto-migrate diff for one table without a database.

    ``schema_ir_json`` is a compiled SchemaIR envelope (``IrEnvelope<SchemaIrPayload>``
    serialized as JSON). ``live_columns_json`` is a JSON array of objects with the
    LiveColumn shape (``name``, ``declared_type``, ``is_nullable``, ``is_primary_key``,
    ``char_max_len``, ``is_enum_udt``). ``live_indexes_json`` is a JSON array of objects
    with the LiveIndex shape (``name``, ``columns``, ``unique``);
    ``live_foreign_keys_json`` the LiveForeignKey shape (``name``, ``column``,
    ``to_table``, ``to_column``, ``on_delete``); ``live_checks_json`` the LiveCheck
    shape (``name``, ``definition``, ``ferro_owned``); ``live_row_security_json``
    the LiveRowSecurity shape (``enabled``, ``forced``, ``policies``, each with
    ``name``, ``command``, ``restrictive``, ``using``, ``with_check``,
    ``roles``, ``ferro_owned``).
    Returns ``(statements, warnings)``.
    """
    ...

def _plan_reverse_from_ir(
    live_json: str,
    declared_json: str,
    dialect: str,
    options_json: str,
    facts_json: str,
    render: bool = True,
    unrendered: list[int] | None = None,
) -> str:
    """The reverse of the live-origin plan (``_plan_from_ir(live_json,
    declared_json, ..., facts_json)``): what turns the database it leaves
    back into the live one (ADR-0041). Same JSON shape as ``_plan_from_ir``
    plus ``before`` (the live envelope under the forward plan's renames); a
    step nothing undoes carries ``irreversible: {"reason": ...}``, a check or
    policy put back from the catalog is a ``RestoreCheck`` /
    ``RestoreRowPolicy``, a foreign key the forward plan added comes off as
    ``DropForeignKey``. With ``render``, the ops at the ``unrendered``
    indexes carry no statement: the ones the bridge writes itself (a
    re-added column that demands values of existing rows)."""
    ...

def _plan_from_ir(
    old_ir_json: str,
    new_ir_json: str,
    dialect: str,
    options_json: str,
    render: bool = False,
    facts_json: str | None = None,
    unrendered: list[int] | None = None,
) -> str:
    """The one planner: every change that turns one SchemaIR snapshot into another.

    ``old_ir_json`` / ``new_ir_json`` are ``schema`` IR envelopes (as
    ``ferro.ir.compile_registry_schema_ir()`` or ``_live_schema_ir`` return
    them); ``dialect`` is ``"postgres"`` or ``"sqlite"``; ``options_json`` is
    ``{"destructive": bool}``. ``facts_json`` is the live side-table
    ``_live_schema_ir`` returns beside a live envelope; omitted, the old
    snapshot reads as declared. Returns JSON
    ``{"operations": [{"kind": ..., <op fields>}], "reports": [...]}``, ops in
    execution order. Each report is ``{"kind", "subject", "text", "recurs",
    "blocks"}``:
    ``kind`` is its name (``"PrimaryKeyKept"``) or ``{name: fields}``
    (``{"HintRefused": {...}}``), ``subject`` is ``{"scope": "table",
    "table": ...}`` (or ``column``, ``enum_type``, ``modelset``), ``text`` the
    sentence printed, ``recurs`` whether it holds on every run until someone
    acts, ``blocks`` whether it stands in for a statement its op does not
    render (the op is left out, so a reviewed file refuses it). With ``render`` each op also carries its ``statements`` and
    ``reports`` for ``dialect`` — the byte-identical statements the
    reconciliation pass executes (I-1) — and an ``AddTable`` its
    ``row_security_statements``; the ops at the ``unrendered`` indexes carry
    none (the ones a caller writes its own way), and the rest render as that
    subset alone.
    """
    ...

def _generate_migration(
    parent_ir_json: str | None,
    target_ir_json: str,
    dialects: list[str],
    options_json: str | None = None,
) -> str | None:
    """Generate the migration that turns the head snapshot into the declared modelset.

    ``parent_ir_json`` is the head migration's ``ir.json`` text exactly as
    stored, or ``None`` before the first migration (generated against the
    empty modelset); ``target_ir_json`` is the declared ``schema`` IR
    envelope; ``dialects`` are the target dialects. Returns the JSON of the
    generated migration — ``{"steps": [{"ordinal", "name", "kind",
    "renderings": {<dialect>: {"up", "down", "headers", "down_headers"}}}],
    "snapshot": {...}, "snapshot_json", "summary", "warnings"}`` — or ``None``
    when nothing renders DDL (no schema change). Raises ``ValueError`` naming
    the refusal (``not generated yet: <op> on <table> (ticket #N)``).
    """
    ...

def _check_migrations(directory: str, target_ir_json: str, dialects: list[str]) -> str:
    """Check a migrations directory against the declared modelset, reading files only.

    Returns JSON ``{"ok": bool, "head": str | None, "problems": [{"kind",
    "message"}]}``: a malformed directory or broken snapshot chain, a DDL step
    missing a target dialect's rendering, a model change no migration records.
    """
    ...

def _read_migrations_dir(directory: str) -> str:
    """Read and verify a migrations directory; returns the JSON of its migrations.

    ``{"path", "migrations": [{"number", "name", "dir", "steps": [{"ordinal",
    "name", "kind" ("ddl" | "data" | "portable_sql"), "files": {<step
    dialect>: {"up", "down", "up_checksum", "headers"}}}], "snapshot":
    {"checksum", "parent_checksum", "ir"}}]}``. A step dialect is
    ``"postgres"``, ``"sqlite"`` or ``"portable"`` (an unsuffixed file, or a
    data step, serving every dialect); ``headers`` has ``no_transaction``,
    ``foreign_keys_off``, ``destructive``, ``data_dependent``,
    ``not_applicable`` and ``nothing_to_reverse`` / ``irreversible`` (a
    reason or ``None``). Checksums are SHA-384 in lowercase hex.

    Raises ``ValueError`` naming the problem and its fix (a duplicate or
    missing number, a broken chain, an unparseable header, ...). A directory
    that does not exist holds no migrations.
    """
    ...

def _load_snapshot(ir_json: str) -> str:
    """Load one ``ir.json`` of any shipped ``ir_version``.

    Returns JSON ``{"checksum", "parent_checksum", "ir"}`` (checksums in hex).
    """
    ...

def _store_snapshot(parent_ir_json: str) -> str:
    """The ``ir.json`` text of a migration that changes no schema: a full copy
    of the parent's modelset whose ``parent_checksum`` is the parent's."""
    ...

# -- the migration runner (ADR-0048) --------------------------------------------

async def _open_tracked(
    using: str | None,
    tracking_schema: str | None,
    directory: str,
    ddl_lock_timeout_s: float = 5.0,
) -> TrackedDatabase:
    """Open connection ``using``'s tracking tables (in ``tracking_schema``
    when set) and the migrations directory at ``directory``: one read of
    the records and one read of the directory, held. Creates nothing and
    takes no lock. Every SQL step a locked run executes waits for table
    locks under ``ddl_lock_timeout_s`` seconds (``0`` disables; ADR-0044)."""
    ...

class StepHandle:
    """One planned step, opaque: what Python may read of it, and the handle
    the locked run executes or records it by."""

    @property
    def migration(self) -> int: ...
    @property
    def migration_name(self) -> str: ...
    @property
    def step(self) -> int: ...
    @property
    def file(self) -> str:
        """The up file going up, the down file going down (a data step's own
        file either way)."""
        ...
    @property
    def path(self) -> str: ...
    @property
    def checksum(self) -> str:
        """SHA-384 of the file's bytes (a data step's loaded source is
        checked against it)."""
        ...
    @property
    def data(self) -> bool: ...
    @property
    def nothing_to_reverse(self) -> str | None: ...
    @property
    def edited(self) -> dict[str, Any] | None:
        """``{"recorded", "recorded_file"}`` for an accepted edit of an
        unfinished step (ADR-0030)."""
        ...
    @property
    def resumes(self) -> bool: ...
    @property
    def resume_cursor(self) -> str | None:
        """The chunked cursor this walk resumes from."""
        ...
    @property
    def rows_done(self) -> int | None: ...
    @property
    def standing(self) -> dict[str, Any] | None:
        """The step's record as it stands: going up its unfinished record,
        going down the record its down removes."""
        ...

class Plan:
    """A run's plan."""

    @property
    def steps(self) -> list[StepHandle]: ...
    @property
    def ahead(self) -> list[str]: ...
    @property
    def direction(self) -> str:
        """``"up"`` or ``"down"``."""
        ...

class BaselinePlan:
    """What ``ferro migrate baseline`` would record."""

    @property
    def target(self) -> str: ...
    @property
    def snapshot(self) -> dict[str, Any]: ...
    @property
    def recorded(self) -> list[str]: ...
    @property
    def data_steps(self) -> list[str]: ...
    @property
    def data_step_files(self) -> list[tuple[int, int, str]]: ...
    @property
    def steps(self) -> int: ...

class RerecordPlan:
    """What ``ferro migrate rerecord`` would change."""

    @property
    def migration(self) -> int: ...
    @property
    def step(self) -> int: ...
    @property
    def migration_name(self) -> str: ...
    @property
    def file(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def old_checksum(self) -> str: ...
    @property
    def new_checksum(self) -> str: ...
    @property
    def data(self) -> bool: ...

class TrackedDatabase:
    """A database's tracking tables and its migrations directory, each read
    once. Read-only; ``locked(...)`` is the one door to a run's writes."""

    @property
    def dialect(self) -> str: ...
    @property
    def tracking_table(self) -> str: ...
    @property
    def records(self) -> list[dict[str, Any]]: ...
    @property
    def refusal(self) -> str | None:
        """The newer-format refusal every verb stops on."""
        ...
    @property
    def migrations(self) -> dict[str, Any]:
        """The held directory read; raises ``RunRefused`` when unreadable."""
        ...
    async def lock_held(self) -> bool: ...
    def status(
        self, order_keys: Any = None, *, lock_held: bool = False
    ) -> dict[str, Any]: ...
    async def plan(
        self,
        direction: dict[str, Any],
        *,
        allow_ahead: bool = False,
        order_keys: Any = None,
    ) -> Plan:
        """A preview: its steps cannot be executed."""
        ...
    def locked(
        self, timeout_s: float, on_wait: Callable[[str], object] | None = None
    ) -> LockScope:
        """The run lock, held for an ``async with`` block."""
        ...
    def _locked_unacquired_for_test(self) -> LockScope: ...

class LockScope:
    async def __aenter__(self) -> LockedDatabase: ...
    async def __aexit__(self, *exc: object) -> bool: ...

class LockedDatabase:
    """A run holding the run lock: every write verifies the lock first."""

    @property
    def dialect(self) -> str: ...
    @property
    def tracking_table(self) -> str: ...
    @property
    def records(self) -> list[dict[str, Any]]: ...
    @property
    def refusal(self) -> str | None: ...
    @property
    def migrations(self) -> dict[str, Any]: ...
    async def lock_held(self) -> bool: ...
    def status(
        self, order_keys: Any = None, *, lock_held: bool = False
    ) -> dict[str, Any]: ...
    async def plan(
        self,
        direction: dict[str, Any],
        *,
        allow_ahead: bool = False,
        order_keys: Any = None,
    ) -> Plan: ...
    async def execute(
        self, step: StepHandle, on_attempt: Callable[[str], object] | None = None
    ) -> dict[str, Any]:
        """Run a SQL step from the held bytes; ``{"ok", "ms", "error",
        "message"}``."""
        ...
    async def start(self, step: StepHandle, kind: str) -> None: ...
    async def advance(
        self, step: StepHandle, cursor: str | None, rows_done: int, tx: RouteHandle
    ) -> None: ...
    async def finish(
        self,
        step: StepHandle,
        ms: int,
        tx: RouteHandle | None = None,
        *,
        cursor: str | None = None,
        rows_done: int | None = None,
    ) -> None: ...
    async def fail(
        self,
        step: StepHandle,
        ms: int,
        error: str,
        cursor: str | None = None,
        rows_done: int | None = None,
    ) -> None: ...
    async def advance_revert(
        self, step: StepHandle, cursor: str | None, rows_done: int, tx: RouteHandle
    ) -> None: ...
    async def fail_revert(
        self, step: StepHandle, error: str, cursor: str | None, rows_done: int
    ) -> None: ...
    async def remove(self, step: StepHandle, tx: RouteHandle) -> None: ...
    def plan_baseline(self, target: str | None = None) -> BaselinePlan: ...
    async def write_baseline(
        self,
        plan: BaselinePlan,
        data_kinds: dict[tuple[int, int], str] | None = None,
    ) -> None: ...
    async def remove_baseline(self) -> list[tuple[int, int]]: ...
    def plan_rerecord(
        self, target: str, mode: str, order_keys: Any = None
    ) -> RerecordPlan: ...
    async def rerecord(self, action: RerecordPlan, kind: str | None = None) -> None: ...
    async def _close_lock_connection_for_test(self) -> None: ...

async def _tracking_tables_for(using: str | None, schema: str | None = None) -> str:
    """JSON list of the tracking tables governing ``schema``."""
    ...

async def _disconnect(name: str) -> None:
    """Close and forget one named, non-default connection."""
    ...

async def _live_schema_ir(
    using: str | None,
    declared_json: str,
    extra_tables_json: str | None = None,
) -> tuple[str, str]:
    """Read the database behind connection ``using``, planned against
    ``declared_json`` (a schema IR envelope), into the planner's input.

    The tables read are the reconciliation pass's (ADR-0047): every declared
    table that is live, the old table of every live
    ``__ferro_renamed_from__`` hint (ADR-0032), and every live table in
    ``extra_tables_json`` (a JSON list of names; the Alembic bridge's
    dropped tables). A live table is a base table — never a view, never
    SQLite's own ``sqlite_*`` tables.

    Returns ``(ir_json, facts_json)``: a ``schema`` IR envelope with one model
    per table read and the live facts the IR cannot carry — CHECK and policy
    bodies as the catalog prints them, validity flags, enum labels.
    """
    ...

async def _live_table_checks_for_test(
    table: str, using: str | None = None
) -> list[dict[str, object]]:
    """Test-only: read live CHECK constraints on ``table`` from the connected engine.

    Each dict has keys ``name``, ``definition``, and ``ferro_owned``.
    """
    ...

async def _live_row_security_for_test(
    table: str, using: str | None = None
) -> dict[str, object]:
    """Test-only: read ``table``'s live row-security state from the engine.

    Returns ``{"enabled": bool, "forced": bool, "policies": [...]}``; each
    policy dict has ``name``, ``command``, ``restrictive``, ``using``,
    ``with_check`` (the catalog's own ``pg_get_expr`` text), ``roles``
    (``pg_policy.polroles`` resolved to names, ``["public"]`` for the default)
    and ``ferro_owned``.
    """
    ...

async def fetch_all(
    cls: object,
    route: RouteHandle,
) -> list[Any]: ...
async def fetch_filtered(
    cls: object,
    query_ir_json: str,
    route: RouteHandle,
    record_cls: type | None = None,
    hop_classes: dict[str, type] | None = None,
) -> list[Any]: ...
async def count_filtered(
    name: str,
    query_ir_json: str,
    route: RouteHandle,
) -> int: ...
async def fetch_one(
    cls: object,
    pk_val: str,
    route: RouteHandle,
) -> Any | None: ...
async def save_record(
    name: str,
    data: dict[str, Any],
    route: RouteHandle,
    mode: str = "insert",
) -> int | None: ...
async def update_record(
    name: str,
    data: dict[str, Any],
    route: RouteHandle,
) -> int: ...
async def save_bulk_records(
    name: str,
    rows: list[dict[str, Any]],
    route: RouteHandle,
) -> int: ...
async def delete_record(
    name: str,
    pk_val: str,
    route: RouteHandle,
) -> bool: ...
async def delete_filtered(
    name: str,
    query_ir_json: str,
    route: RouteHandle,
) -> int: ...
async def update_filtered(
    name: str,
    query_ir_json: str,
    route: RouteHandle,
) -> int: ...
async def add_m2m_links(
    join_table: str,
    source_col: str,
    target_col: str,
    source_id: Any,
    target_ids: list[Any],
    route: RouteHandle,
) -> None: ...
async def remove_m2m_links(
    join_table: str,
    source_col: str,
    target_col: str,
    source_id: Any,
    target_ids: list[Any],
    route: RouteHandle,
) -> None: ...
async def clear_m2m_links(
    join_table: str,
    source_col: str,
    source_id: Any,
    route: RouteHandle,
) -> None: ...
async def begin_transaction(route: RouteHandle, immediate: bool = False) -> str: ...
async def commit_transaction(tx_id: str, session_id: Optional[str] = None) -> None: ...
def transaction_connection_name(
    tx_id: str, session_id: Optional[str] = None
) -> str: ...
async def rollback_transaction(
    tx_id: str, session_id: Optional[str] = None
) -> None: ...
def open_session(
    using: Optional[str] = None,
    settings: Optional[list[tuple[str, str]]] = None,
    declared: Optional[list[tuple[str, str]]] = None,
) -> tuple[str, str]: ...
async def close_session(session_id: str) -> None: ...
async def set_session_config(
    session_id: str,
    connection_name: str,
    key: str,
    value: str,
) -> list[tuple[str, str]]: ...
async def raw_execute(
    sql: str,
    args: list[Any],
    route: RouteHandle,
    autocommit: bool = False,
) -> int: ...
async def raw_fetch_all(
    sql: str,
    args: list[Any],
    route: RouteHandle,
) -> list[dict[str, Any]]: ...
async def raw_fetch_one(
    sql: str,
    args: list[Any],
    route: RouteHandle,
) -> dict[str, Any] | None: ...
def register_instance(
    name: str,
    pk: str,
    obj: object,
    route: RouteHandle,
) -> None: ...
def evict_instance(name: str, pk: str, route: RouteHandle) -> None: ...
def reset_engine() -> None: ...
def set_default_connection(name: str) -> None: ...
def connection_backend(using: str | None = None) -> str | None: ...
def clear_registry() -> None: ...
def version() -> str: ...
def _install_registration(payload_json: str, fingerprint: str) -> bool: ...
def _bulk_install_count_for_test() -> int: ...
def _rust_model_registry_count_for_test() -> int: ...
def _clear_schema_ir_modelset_for_test() -> None: ...
def _verify_hydration_abi_for_test(cls: type) -> None: ...

# Single-sourced DDL artifact-name builders (ferro-ddl-lowering; AGENTS.md § I-1).
# All apply the 63-char truncation guards; Python must not re-implement them.
def _ddl_single_index_name(table: str, column: str) -> str: ...
def _ddl_single_unique_name(table: str, column: str) -> str: ...
def _ddl_composite_index_name(table: str, columns: list[str]) -> str: ...
def _ddl_composite_unique_name(table: str, columns: list[str]) -> str: ...
def _ddl_check_constraint_name(table: str, column: str) -> str: ...
def _ddl_table_check_constraint_name(table: str, suffix: str) -> str: ...
def _ddl_fk_name(table: str, column: str, to_table: str) -> str: ...
def _resolve_storage_type(column_ir_json: str, dialect: str) -> str:
    """Resolve one SchemaIR column's storage decision via ferro-ddl-lowering.

    Returns JSON: ``{"kind": "scalar", "token": "<db_type token>"}`` or
    ``{"kind": "pg_enum", "name": "<type name>", "labels": [...]}``.
    Unknown logical types raise ``RuntimeError`` (never a silent varchar).
    """
    ...

def _render_check_body(column: str, values: list[str]) -> str:
    """The shared db_check CHECK body, byte-identical to the Rust emitters."""
    ...

def _render_table_check_body(predicate_json: str) -> str:
    """The shared table-check CHECK body, byte-identical to the Rust emitters."""
    ...

def _plan_step_verdicts(
    before_json: str,
    after_json: str,
    dialect: str,
    direction: str,
    operations_json: str,
) -> str:
    """What each planner op needs on ``dialect`` in a file turning
    ``before_json`` into ``after_json`` (``direction`` ``"up"`` / ``"down"``):
    the generator's step-assignment verdict. Returns a JSON list, one
    ``{"needs": "native" | "rebuild" | "backfill" | "refused", "refusal":
    str | None, "primary_key": bool, "drops_data": bool, "demands_values":
    bool}`` per op; ``demands_values`` marks an op asking existing rows for a
    value no statement supplies (a backfill going up, a re-added required
    column going down)."""
    ...

def _tracking_table_names() -> tuple[str, str]:
    """The tracking tables' names: ``("_ferro_migrations",
    "_ferro_migrations_format")``."""
    ...

def _ddl_row_policy_name(table: str, name: str) -> str:
    """Canonical row-policy name (``rls_<table>_<name>``), 63-char guarded."""
    ...

def _rls_command_matrix() -> str:
    """Every row-policy command and the clauses Postgres accepts for it.

    Returns JSON: ``[{"command": "all", "using": true, "with_check": true}, …]``.
    ``ferro.rowsecurity`` reads this at import instead of keeping its own copy,
    so the command allowlist and the USING / WITH CHECK rules are decided in
    ``ferro-ddl-lowering`` alone (I-1) — the same decision the renderer filters
    clauses with.
    """
    ...

def _rls_shorthand_cast(column_ir_json: str) -> str:
    """The column/setting shorthand's cast decision for one IR column.

    Returns JSON: ``{"supported": true, "cast": "uuid" | null}`` for a column
    the shorthand can render (``null`` = the column already stores text, so no
    cast), or ``{"supported": false, "reason": "..."}``. The emitters render
    with the same decision, so class definition fails for exactly the columns
    DDL would fail for (I-1).
    """
    ...

def _normalize_row_policy_expr(expr: str) -> str:
    """One row-policy expression through ferro's canonical normalizer (#413).

    Both ferro's rendered body and the catalog's ``pg_get_expr`` text go
    through this one function; equal output means the same predicate.
    """
    ...

def _row_policy_command_from_catalog_code(code: str) -> str | None:
    """Decode one ``pg_policy.polcmd`` code into ferro's command vocabulary.

    ``None`` for a code ferro does not recognize. The Alembic autogenerate
    comparator's own ``pg_policy`` introspection uses this to build the
    ``LiveRowPolicy`` payload the planner's live facts carry —
    the same decode ``src/introspect.rs``'s ``live_table_row_security`` uses
    (AGENTS.md § I-1).
    """
    ...

def _is_ferro_row_policy_name(name: str) -> bool:
    """Whether a live policy name follows ferro's ``rls_`` ownership prefix."""
    ...

# --- #521: connect() guard ---

def _default_connection_name() -> str | None:
    """The default connection's name, or ``None`` when there is none."""
    ...
