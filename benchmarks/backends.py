"""Backend selection and per-case isolation for the benchmark suite.

Reuses ``tests/db_backends.py`` (``get_postgres_url`` / ``build_postgres_test_url``)
rather than inventing a second backend-selection path, mirroring how the pytest
matrix decides whether Postgres is available. SQLite is always present; Postgres
is included only when a URL is configured and skipped cleanly otherwise.

Each benchmark case runs against a *fresh* database so timings never depend on
leftover state:

- SQLite: a unique temp file (not ``:memory:`` — a pooled connection opens a
  distinct in-memory database per connection, which would break multi-connection
  pools).
- Postgres: a random ``ferro_<hex>`` schema created up front and dropped after,
  addressed through the ``ferro_search_path`` query param the Rust engine reads.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Repo root is on sys.path when run as ``python -m benchmarks.run`` from the
# project directory, so the pytest-free helpers import cleanly.
from tests.db_backends import build_postgres_test_url, get_postgres_url

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


def postgres_base_url() -> str | None:
    """The externally managed Postgres URL, or ``None`` when unconfigured."""
    return get_postgres_url(dict(os.environ), _ENV_FILE)


def available_backends() -> list[str]:
    """``["sqlite"]`` plus ``"postgres"`` when a Postgres URL is configured."""
    backends = ["sqlite"]
    if postgres_base_url():
        backends.append("postgres")
    return backends


def _connect_admin(base_url: str):
    import psycopg

    return psycopg.connect(base_url, autocommit=True)


def _create_schema(base_url: str, schema: str) -> None:
    with _connect_admin(base_url) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema}"')


def _drop_schema(base_url: str, schema: str) -> None:
    with _connect_admin(base_url) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@contextmanager
def fresh_target(backend: str) -> Iterator[str]:
    """Yield a connect URL for a fresh, isolated database of ``backend``.

    Cleans up (temp dir / dropped schema) on exit regardless of outcome.
    """
    if backend == "sqlite":
        with tempfile.TemporaryDirectory(prefix="ferro-bench-") as tmp:
            yield f"sqlite:{Path(tmp) / 'bench.db'}?mode=rwc"
        return

    if backend == "postgres":
        base_url = postgres_base_url()
        if not base_url:  # pragma: no cover - guarded by available_backends()
            raise RuntimeError("Postgres benchmark requested but no URL configured")
        schema = f"ferro_{uuid.uuid4().hex[:16]}"
        _create_schema(base_url, schema)
        try:
            yield build_postgres_test_url(base_url, schema)
        finally:
            _drop_schema(base_url, schema)
        return

    raise ValueError(f"Unknown backend: {backend!r}")
