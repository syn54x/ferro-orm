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
) -> None: ...
async def create_tables(using: Optional[str] = None) -> None: ...
async def migrate(
    using: Optional[str] = None,
    updates: bool = True,
    destructive: bool = False,
) -> None:
    """Run the auto-migrate pass against a connected engine.

    Creates missing tables, then (with ``updates``, the default) adds missing
    model columns to existing tables and reconciles type/nullability drift on
    Postgres; with ``destructive`` it also drops live columns no longer on the
    model. ``destructive`` implies ``updates``. The pool is refreshed after any
    DDL so no cached statement observes the pre-migration schema.
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
) -> tuple[list[str], list[str]]:
    """Test-only: render the auto-migrate diff for one table without a database.

    ``schema_ir_json`` is a compiled SchemaIR envelope (``IrEnvelope<SchemaIrPayload>``
    serialized as JSON). ``live_columns_json`` is a JSON array of objects with the
    LiveColumn shape (``name``, ``declared_type``, ``is_nullable``, ``is_primary_key``,
    ``char_max_len``, ``is_enum_udt``). ``live_indexes_json`` is a JSON array of objects
    with the LiveIndex shape (``name``, ``columns``, ``unique``).
    Returns ``(statements, warnings)``.
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
    updates: dict[str, Any],
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
async def begin_transaction(route: RouteHandle) -> str: ...
async def commit_transaction(tx_id: str, session_id: Optional[str] = None) -> None: ...
def transaction_connection_name(tx_id: str, session_id: Optional[str] = None) -> str: ...
async def rollback_transaction(tx_id: str, session_id: Optional[str] = None) -> None: ...
def open_session(using: Optional[str] = None) -> tuple[str, str]: ...
def close_session(session_id: str) -> None: ...
async def raw_execute(
    sql: str,
    args: list[Any],
    route: RouteHandle,
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

def _plan_enum_label_addition(
    type_name: str, declared: list[str], live: list[str]
) -> str:
    """The label-addition decision (ADR-0011) for one enum type.

    Returns JSON: ``{"statements": [...], "extra_labels": [...]}`` — the
    Rust-rendered ``ADD VALUE IF NOT EXISTS`` statements for model-declared
    labels the live type is missing, and the live labels the model no longer
    declares (warn-never-act).
    """
    ...
