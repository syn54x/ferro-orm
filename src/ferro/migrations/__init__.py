"""Ferro migrations: the in-process calls and the Alembic bridge.

```python
await ferro.connect(url)
await ferro.migrations.up()                # a desktop app with no deploy step
await ferro.migrations.require_applied()   # a server that migrates in its deploy step
```

``get_metadata`` and ``render_item`` (the Alembic bridge) are loaded on
first use, so the migration calls never import Alembic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .api import check, require_applied, status, up
from .drift import DriftReport, drift, render_op
from .errors import DatabaseAheadError, MigrationRefused, PendingMigrationsError

if TYPE_CHECKING:
    from .alembic import get_metadata, render_item

__all__ = [
    "DatabaseAheadError",
    "DriftReport",
    "MigrationRefused",
    "PendingMigrationsError",
    "check",
    "drift",
    "get_metadata",
    "render_item",
    "render_op",
    "require_applied",
    "status",
    "up",
]

_ALEMBIC_NAMES = ("get_metadata", "render_item")


def __getattr__(name: str) -> Any:
    if name in _ALEMBIC_NAMES:
        from . import alembic

        return getattr(alembic, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
