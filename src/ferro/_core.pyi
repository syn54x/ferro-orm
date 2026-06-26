from typing import Any, Optional

def register_model_schema(name: str, schema: str) -> None: ...
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
) -> tuple[str, list[str]]:
    """Test-only: render CREATE TABLE SQL + post-create fragments without executing.

    Used by the cross-emitter parity test (U5). ``dialect`` is ``"postgres"`` or
    ``"sqlite"``.
    """
    ...

def _render_migration_sql_for_test(
    name: str,
    schema_json: str,
    live_columns_json: str,
    dialect: str,
    updates: bool = True,
    destructive: bool = False,
) -> tuple[list[str], list[str]]:
    """Test-only: render the auto-migrate diff for one table without a database.

    ``live_columns_json`` is a JSON array of objects with the LiveColumn shape
    (``name``, ``declared_type``, ``is_nullable``, ``is_primary_key``,
    ``char_max_len``, ``is_enum_udt``). Returns ``(statements, warnings)``.
    """
    ...

def _shadow_compare_migration_plan_for_test(
    name: str,
    schema_json: str,
    live_columns_json: str,
    dialect: str,
    updates: bool = True,
    destructive: bool = False,
) -> str:
    """Test-only: compare IR-primary vs legacy migration planners."""
    ...

def _shadow_compare_query_plan_for_test(
    query_payload_json: str, dialect: str, operation: str = "select"
) -> str:
    """Test-only: compare query payload planning semantics."""
    ...

async def fetch_all(
    cls: object,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> list[Any]: ...
async def fetch_filtered(
    cls: object,
    query_ir_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> list[Any]: ...
async def count_filtered(
    name: str,
    query_ir_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int: ...
async def fetch_one(
    cls: object,
    pk_val: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Any | None: ...
async def save_record(
    name: str,
    data: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int | None: ...
async def save_bulk_records(
    name: str,
    data_list_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int: ...
async def delete_record(
    name: str,
    pk_val: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool: ...
async def delete_filtered(
    name: str,
    query_ir_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int: ...
async def update_filtered(
    name: str,
    query_ir_json: str,
    update_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int: ...
async def add_m2m_links(
    join_table: str,
    source_col: str,
    target_col: str,
    source_id: Any,
    target_ids: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None: ...
async def remove_m2m_links(
    join_table: str,
    source_col: str,
    target_col: str,
    source_id: Any,
    target_ids: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None: ...
async def clear_m2m_links(
    join_table: str,
    source_col: str,
    source_id: Any,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None: ...
async def begin_transaction(
    parent_tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str: ...
async def commit_transaction(tx_id: str, session_id: Optional[str] = None) -> None: ...
def transaction_connection_name(tx_id: str, session_id: Optional[str] = None) -> str: ...
async def rollback_transaction(tx_id: str, session_id: Optional[str] = None) -> None: ...
def open_session(using: Optional[str] = None) -> tuple[str, str]: ...
def close_session(session_id: str) -> None: ...
async def raw_execute(
    sql: str,
    args: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int: ...
async def raw_fetch_all(
    sql: str,
    args: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> list[dict[str, Any]]: ...
async def raw_fetch_one(
    sql: str,
    args: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any] | None: ...
def register_instance(
    name: str,
    pk: str,
    obj: object,
    using: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None: ...
def evict_instance(
    name: str, pk: str, using: Optional[str] = None, session_id: Optional[str] = None
) -> None: ...
def reset_engine() -> None: ...
def set_default_connection(name: str) -> None: ...
def clear_registry() -> None: ...
def version() -> str: ...
