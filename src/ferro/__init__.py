"""
Ferro: A High-Performance Rust-Backed Python ORM.

Ferro combines the speed of a Rust engine with the ergonomics of Pydantic models
to provide a seamless, high-performance database experience.
"""

from ._core import (
    clear_registry,
    connect,
    create_tables,
    reset_engine,
    version,
    evict_instance,
    begin_transaction,
    commit_transaction,
    rollback_transaction,
)
from .models import FerroField, Model, transaction

__all__ = [
    "connect",
    "Model",
    "FerroField",
    "version",
    "create_tables",
    "reset_engine",
    "clear_registry",
    "evict_instance",
    "transaction",
]
