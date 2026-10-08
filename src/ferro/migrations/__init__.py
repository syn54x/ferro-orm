"""Ferro migrations: the in-process calls and the Alembic bridge.

Each call here is the CLI verb's own function: ``up()`` is ``ferro migrate
up``, ``status()`` is ``ferro migrate status``, ``check()`` is ``ferro
migrate check``. Each takes ``settings=None, database=None`` (the project's
configuration and which of its databases) and, when it works on a live
database, ``using=`` (an open connection) or ``url=`` (a private one); the
default connection otherwise (:class:`~ferro.migrations.target.Target`).
``down`` and ``rerecord`` stay CLI verbs: they are operator decisions with
prompts. A refusal is raised, never returned: an application that called
``up()`` must not go on serving a database the run did not reach.

```python
await ferro.connect(url)
await ferro.migrations.up()                # a desktop app with no deploy step
await ferro.migrations.require_applied()   # a server that migrates in its deploy step
```

A data step declares its shape with ``atomic`` / ``chunked`` (and its down
with ``irreversible`` / ``nothing_to_reverse``), and marks what only a
person can supply with ``todo("…")`` (ADR-0035).

``get_metadata``, ``ferro_options`` and ``render_item`` (the Alembic bridge) are loaded on
first use, so the migration calls never import Alembic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .baseline import BaselineReport, baseline, remove_baseline
from .drift import DriftReport, drift, render_op
from .errors import DatabaseAheadError, MigrationRefused, PendingMigrationsError
from .generate import check
from .runner import require_applied, status, up
from .steps import atomic, chunked, irreversible, nothing_to_reverse, todo

if TYPE_CHECKING:
    from .alembic import ferro_options, get_metadata, render_item

__all__ = [
    "BaselineReport",
    "DatabaseAheadError",
    "DriftReport",
    "MigrationRefused",
    "PendingMigrationsError",
    "atomic",
    "baseline",
    "check",
    "chunked",
    "drift",
    "ferro_options",
    "get_metadata",
    "irreversible",
    "nothing_to_reverse",
    "remove_baseline",
    "render_item",
    "render_op",
    "require_applied",
    "status",
    "todo",
    "up",
]

_ALEMBIC_NAMES = ("ferro_options", "get_metadata", "render_item")


def __getattr__(name: str) -> Any:
    if name in _ALEMBIC_NAMES:
        from . import alembic

        return getattr(alembic, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
