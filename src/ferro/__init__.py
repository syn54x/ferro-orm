"""
Ferro: A High-Performance Rust-Backed Python ORM.

Ferro combines the speed of a Rust engine with the ergonomics of Pydantic models
to provide a seamless, high-performance database experience.
"""

import logging

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from ._core import (
    clear_registry,
    create_tables,
    evict_instance,
    reset_engine,
    set_default_connection,
    version,
)
from ._core import (
    connect as _core_connect,
)
from .base import FerroField, FerroNullable, ForeignKey
from .fields import BackRef, Field, ManyToMany
from .models import Model, transaction
from .query import Relation
from .raw import Transaction, execute, fetch_all, fetch_one

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
) -> None:
    """
    Establish a connection to the database.

    Args:
        url: The database connection string (e.g., "sqlite:example.db?mode=rwc").
        auto_migrate: If True, automatically create tables for all registered models.
        name: Optional connection name. Omitted connections register as "default".
        default: If True, make this named connection the default for unqualified operations.
        pool: Optional per-connection pool configuration.
    """
    from .relations import resolve_relationships

    resolve_relationships()

    pool_config = pool or PoolConfig()
    await _core_connect(
        url,
        auto_migrate=auto_migrate,
        name=name,
        default=default,
        max_connections=pool_config.max_connections,
        min_connections=pool_config.min_connections,
    )


__all__ = [
    "connect",
    "PoolConfig",
    "Model",
    "FerroField",
    "FerroNullable",
    "Field",
    "ForeignKey",
    "BackRef",
    "ManyToMany",
    "Relation",
    "version",
    "create_tables",
    "reset_engine",
    "set_default_connection",
    "clear_registry",
    "evict_instance",
    "transaction",
    "execute",
    "fetch_all",
    "fetch_one",
    "Transaction",
]
