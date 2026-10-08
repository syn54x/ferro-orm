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
    execution order. Each report is ``{"kind", "subject", "text", "recurs"}``:
    ``kind`` is its name (``"PrimaryKeyKept"``) or ``{name: fields}``
    (``{"HintRefused": {...}}``), ``subject`` is ``{"scope": "table",
    "table": ...}`` (or ``column``, ``enum_type``, ``modelset``), ``text`` the
    sentence printed, ``recurs`` whether it holds on every run until someone
    acts. With ``render`` each op also carries its ``statements`` and
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

# -- the migration runner (#519) -------------------------------------------------

def _run_plan(
    directory: str,
    records_json: str,
    dialect: str,
    direction_json: str,
    allow_ahead: bool,
    live_tables_json: str | None = None,
    order_keys_json: str | None = None,
) -> str:
    """Plan a run: the JSON ``RunPlan`` (``{"steps": [...], "ahead": [...]}``).

    Raises ``RunRefused`` with the refusal's text (an edited applied file, a
    snapshot mismatch, a broken chain, out of order, applied but missing,
    the database's tables built without migrations when ``live_tables_json``
    is given and no record exists, ...).
    """
    ...

def _run_status(
    directory: str,
    records_json: str,
    dialect: str,
    lock_held: bool,
    order_keys_json: str | None = None,
) -> str:
    """``ferro migrate status`` read-only, as the JSON ``RunStatus``."""
    ...

async def _acquire_run_lock(
    using: str | None,
    governed_schema: str | None = None,
    timeout_s: float = 30.0,
    on_wait: Callable[[str], object] | None = None,
) -> int:
    """Take the run lock (waiting up to ``timeout_s``; ``on_wait(text)`` once
    when another run holds it); returns a handle. Raises ``RunRefused`` on
    timeout or behind a transaction-mode pooler."""
    ...

async def _verify_run_lock(handle: int) -> None:
    """Raise ``RunRefused`` unless the lock behind ``handle`` is still held."""
    ...

async def _release_run_lock(handle: int) -> None:
    """Release the lock behind ``handle``. Refused (``RuntimeError``), with
    the handle kept for a retry, while another call holds the lock."""
    ...

async def _run_lock_is_held(
    using: str | None, governed_schema: str | None = None
) -> bool:
    """Whether any run holds the run lock, without taking it."""
    ...

async def _close_run_lock_connection_for_test(handle: int) -> None:
    """Close the Postgres lock connection without releasing it (tests only)."""
    ...

async def _unacquired_run_lock_for_test(using: str | None = None) -> int:
    """Register a never-acquired, never-verified Postgres run lock, as a
    transaction-mode pooler would hand back; its first ``_verify_run_lock``
    raises the pooler refusal (tests only)."""
    ...

async def _ensure_tracking_tables(
    using: str | None, tracking_schema: str | None = None
) -> None:
    """Create ``_ferro_migrations`` and ``_ferro_migrations_format`` where
    missing. Raises ``RunRefused`` naming ``CREATE SCHEMA`` for a missing
    ``tracking_schema``."""
    ...

async def _read_records(using: str | None, tracking_schema: str | None = None) -> str:
    """JSON ``{"table", "exists", "format", "governed_schema", "records",
    "refusal"}``; creates nothing."""
    ...

async def _write_record(
    using: str | None,
    record_json: str,
    tracking_schema: str | None = None,
    route: RouteHandle | None = None,
) -> None:
    """Upsert one step record; with ``route`` (an open ``transaction()``
    block's), on that transaction's connection, committing with it."""
    ...

async def _remove_record(
    route: RouteHandle,
    migration: int,
    step: int,
    tracking_schema: str | None = None,
) -> None:
    """Delete the record of ``(migration, step)`` inside the transaction
    ``route`` names (a data step's down)."""
    ...

async def _execute_sql_step(
    using: str | None,
    planned_step_json: str,
    sql: str,
    record_json: str,
    tracking_schema: str | None = None,
    lock: int | None = None,
    direction_json: str | None = None,
    ddl_lock_timeout_s: float = 5.0,
    on_attempt: Callable[[str], object] | None = None,
) -> str:
    """Run one planned SQL step and settle its record; JSON ``{"ok", "ms",
    "error", "message"}``.

    ``direction_json`` is the plan's direction (``_run_plan``'s; up when
    omitted). Going up ``sql`` is the up file and the record is written when
    the step finishes; going down ``sql`` is the down file and the standing
    record is removed in the down's transaction (kept, with ``failed_at`` and
    ``error``, when the down fails).

    On Postgres the step waits for table locks under ``ddl_lock_timeout_s``
    seconds (``0`` disables; ADR-0044); a step that times out is re-run from
    its first statement, up to ten attempts, and ``on_attempt`` hears each
    one as ``waiting for a lock on "author" (attempt 1 of 10, retry in
    1s)``."""
    ...

async def _tracking_tables_for(using: str | None, schema: str | None = None) -> str:
    """JSON list of the tracking tables governing ``schema``."""
    ...

async def _live_tables(using: str | None = None) -> str:
    """JSON list of the governed schema's tables, sorted: base tables only,
    never a view or SQLite's own ``sqlite_*`` tables."""
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

# --- #537: rerecord ---

def _rerecord_plan(
    directory: str,
    records_json: str,
    target: str,
    mode: str,
    dialect: str,
    order_keys_json: str | None = None,
) -> str:
    """Plan ``ferro migrate rerecord <target>`` (``mode`` ``"record"``,
    ``"continue"`` or ``"restart"``): the JSON ``RerecordAction``
    (``{"migration", "step", "migration_name", "recorded_file", "file",
    "path", "old_checksum", "new_checksum", "kind", "data", "finished",
    "clear_cursor"}``). Raises a structured ``RunRefused`` for every
    refusal."""
    ...

async def _rerecord(
    using: str | None,
    action_json: str,
    tracking_schema: str | None = None,
    lock: int | None = None,
) -> None:
    """Write one planned re-record (the record's file, checksum and kind;
    with ``clear_cursor`` its cursor and rows_done cleared) in one
    statement, running nothing of the step. Raises ``RunRefused`` when the
    lock was lost or the record changed since it was planned."""
    ...

# --- #525: baseline ---

def _plan_baseline(
    directory: str,
    records_json: str,
    dialect: str,
    target: str | None = None,
    ferro_version: str = "",
) -> str:
    """Plan ``ferro migrate baseline`` (ADR-0031). ``target`` is ``None``
    (the head), a number (``"0006"``) or a full name (``"0006_add_teams"``).
    Returns JSON ``{"target", "snapshot", "records", "recorded",
    "data_steps"}``: the target's snapshot to check the database against, and
    one finished ``origin='baseline'`` record per step through the target.
    Raises ``RunRefused`` when records exist, the target is not in the
    directory, or a step has no rendering for ``dialect``."""
    ...

async def _write_baseline_records(
    using: str | None,
    records_json: str,
    tracking_schema: str | None = None,
    lock: int | None = None,
) -> None:
    """Write ``_plan_baseline``'s records in one transaction, creating the
    tracking tables where missing; verifies the run lock behind ``lock``."""
    ...

async def _remove_baseline_records(
    using: str | None,
    tracking_schema: str | None = None,
    lock: int | None = None,
) -> str:
    """Delete every baseline-origin record; JSON ``[[migration, step], ...]``
    of those removed. Raises ``RunRefused`` naming a run-origin migration
    applied above the baseline."""
    ...

# --- #532: chunked ---

async def _write_cursor(
    using: str | None,
    migration: int,
    step: int,
    cursor_json: str | None,
    rows_done: int,
    reverting: bool,
    tracking_schema: str | None = None,
    route: RouteHandle | None = None,
) -> None:
    """Commit one batch of a chunked step on its record: ``cursor_json``
    (``{"keys": [...], "rows_done": N}``, ``None`` before any row) into
    ``resume_cursor``, or with ``reverting`` into ``revert_cursor`` with the
    record marked reverting, plus ``rows_done``; clears the last failure.
    With ``route`` (the batch's ``transaction()`` block) it commits with the
    batch. Raises ``RunRefused`` when the step has no record."""
    ...
