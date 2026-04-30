from typing import Any, Optional

def register_model_schema(name: str, schema: str) -> None: ...
async def connect(
    url: str,
    auto_migrate: bool = False,
    name: Optional[str] = None,
    default: bool = False,
    max_connections: int = 5,
    min_connections: int = 0,
) -> None: ...
async def create_tables(using: Optional[str] = None) -> None: ...
async def fetch_all(
    cls: object, tx_id: Optional[str] = None, using: Optional[str] = None
) -> list[Any]: ...
async def fetch_filtered(
    cls: object,
    query_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> list[Any]: ...
async def count_filtered(
    name: str,
    query_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> int: ...
async def fetch_one(
    cls: object,
    pk_val: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> Any | None: ...
async def save_record(
    name: str,
    data: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> int | None: ...
async def save_bulk_records(
    name: str,
    data_list_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> int: ...
async def delete_record(
    name: str,
    pk_val: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> bool: ...
async def delete_filtered(
    name: str,
    query_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> int: ...
async def update_filtered(
    name: str,
    query_json: str,
    update_json: str,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> int: ...
async def add_m2m_links(
    join_table: str,
    source_col: str,
    target_col: str,
    source_id: Any,
    target_ids: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> None: ...
async def remove_m2m_links(
    join_table: str,
    source_col: str,
    target_col: str,
    source_id: Any,
    target_ids: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> None: ...
async def clear_m2m_links(
    join_table: str,
    source_col: str,
    source_id: Any,
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> None: ...
async def begin_transaction(
    parent_tx_id: Optional[str] = None, using: Optional[str] = None
) -> str: ...
async def commit_transaction(tx_id: str) -> None: ...
async def rollback_transaction(tx_id: str) -> None: ...
async def raw_execute(
    sql: str,
    args: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> int: ...
async def raw_fetch_all(
    sql: str,
    args: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> list[dict[str, Any]]: ...
async def raw_fetch_one(
    sql: str,
    args: list[Any],
    tx_id: Optional[str] = None,
    using: Optional[str] = None,
) -> dict[str, Any] | None: ...
def register_instance(
    name: str, pk: str, obj: object, using: Optional[str] = None
) -> None: ...
def evict_instance(name: str, pk: str, using: Optional[str] = None) -> None: ...
def reset_engine() -> None: ...
def set_default_connection(name: str) -> None: ...
def clear_registry() -> None: ...
def version() -> str: ...
