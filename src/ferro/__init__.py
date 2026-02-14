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
)
from .models import Model

__all__ = [
    "connect",
    "Model",
    "version",
    "create_tables",
    "reset_engine",
    "clear_registry",
]
