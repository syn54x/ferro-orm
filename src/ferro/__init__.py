"""
Ferro: A High-Performance Rust-Backed Python ORM.

Ferro combines the speed of a Rust engine with the ergonomics of Pydantic models
to provide a seamless, high-performance database experience.
"""

from ._core import (
    clear_registry,
    connect as _core_connect,
    create_tables,
    reset_engine,
    version,
    evict_instance,
)
from .models import Model, transaction
from .base import FerroField, ForeignKey, ManyToManyField
from .query import BackRelationship


async def connect(url: str, auto_migrate: bool = False) -> None:
    """
    Establish a connection to the database.

    Args:
        url: The database connection string (e.g., "sqlite::memory:").
        auto_migrate: If True, automatically create tables for all registered models.
    """
    from .relations import resolve_relationships

    resolve_relationships()

    await _core_connect(url)
    if auto_migrate:
        await create_tables()


__all__ = [
    "connect",
    "Model",
    "FerroField",
    "ForeignKey",
    "ManyToManyField",
    "BackRelationship",
    "version",
    "create_tables",
    "reset_engine",
    "clear_registry",
    "evict_instance",
    "transaction",
]
