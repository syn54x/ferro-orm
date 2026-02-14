"""
Ferro: A High-Performance Rust-Backed Python ORM.

Ferro combines the speed of a Rust engine with the ergonomics of Pydantic models
to provide a seamless, high-performance database experience.
"""

import logging

from ._core import (
    clear_registry,
    create_tables,
    evict_instance,
    reset_engine,
    version,
)
from ._core import (
    connect as _core_connect,
)
from .base import FerroField, ForeignKey, ManyToManyField
from .models import Model, transaction
from .query import BackRelationship

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
