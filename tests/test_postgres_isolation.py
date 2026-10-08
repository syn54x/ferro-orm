"""Tenants of one Postgres database do not trip over each other.

Every schema of a database is its own tenant: ``connect(url, auto_migrate=True)``
on ``tenant_a`` must succeed whatever ``tenant_b`` is doing meanwhile, including
being deprovisioned. The guard that refuses auto-migrate on a tracked schema
(ADR-0038) reads every ``_ferro_migrations_format`` table in the database to
find the ones governing this schema, so a neighbour schema dropped between
that listing and its read used to fail the pass::

    OperationalError: reading a tracking table's format: relation
    "tenant_b._ferro_migrations_format" does not exist

The same race failed two concurrent runs of this suite against one server.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from typing import Annotated, ClassVar

import psycopg
import pytest

import ferro
from ferro import Field, Model, RowPolicy, RowSecurity
from ferro.base import FerroField
from ferro.raw import execute, fetch_all
from tests._pass_harness import auto_migrate
from tests.db_backends import build_postgres_test_url

pytestmark = [
    pytest.mark.backend_matrix,
    pytest.mark.postgres_only,
    pytest.mark.usefixtures("clean_registry"),
]

FORMAT_TABLE_DDL = (
    "CREATE TABLE {schema}._ferro_migrations_format "
    "(format INTEGER NOT NULL, governed_schema TEXT NOT NULL)"
)


class _Churn:
    """A neighbour tenant that keeps being provisioned under migrations and
    deprovisioned: a schema holding a format table, created and dropped in a
    loop on a connection of its own until :meth:`stop`."""

    def __init__(self, base_url: str, prefix: str) -> None:
        self._base_url = base_url
        self._prefix = prefix
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.cycles = 0
        self.error: BaseException | None = None

    def _run(self) -> None:
        try:
            with psycopg.connect(self._base_url, autocommit=True) as conn:
                while not self._stop.is_set():
                    schema = f'"{self._prefix}_{uuid.uuid4().hex[:8]}"'
                    conn.execute(f"CREATE SCHEMA {schema}")
                    conn.execute(FORMAT_TABLE_DDL.format(schema=schema))
                    conn.execute(f"DROP SCHEMA {schema} CASCADE")
                    self.cycles += 1
        except BaseException as exc:  # noqa: BLE001 - surfaced by stop()
            self.error = exc

    def __enter__(self) -> _Churn:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()
        if self.error is not None:
            raise self.error


@pytest.fixture
def churn(postgres_base_url: str, db_schema_name: str) -> Iterator[_Churn]:
    with _Churn(postgres_base_url, f"{db_schema_name}_nb") as running:
        yield running


@pytest.fixture
def second_tenant(postgres_base_url: str, db_schema_name: str) -> Iterator[str]:
    """A second schema of the test's database: the URL of ``tenant_b``."""
    schema = f"{db_schema_name}_b"
    with psycopg.connect(postgres_base_url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield build_postgres_test_url(postgres_base_url, schema)
    finally:
        ferro.reset_engine()
        with psycopg.connect(postgres_base_url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _define_writer() -> None:
    class IsoWriter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]


def _define_author_and_ledger() -> None:
    class IsoAuthor(Model):
        __ferro_renamed_from__ = "isowriter"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]

    class IsoLedger(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting="iso.ledger_id"), force=True
        )


def _rewind() -> None:
    from ferro import clear_registry
    from ferro.registry import REGISTRY

    ferro.reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


@pytest.mark.asyncio
async def test_auto_migrate_survives_a_neighbour_dropped_while_the_guard_reads_it(
    db_url, churn
):
    """Each pass's guard lists the format tables, then reads each one; the
    neighbour's is dropped in between often enough that a hundred passes
    reliably hit it."""
    _define_writer()
    for _ in range(100):
        await auto_migrate(db_url)
        ferro.reset_engine()
    assert churn.cycles > 0


@pytest.mark.asyncio
async def test_two_tenants_migrate_side_by_side_while_a_third_comes_and_goes(
    db_url, second_tenant, db_schema_name, pg_role, churn
):
    """Two schemas take the same models through the same passes at the same
    time — a create, then a declared table rename plus a forced row-security
    table — while a third, tracked tenant is provisioned and dropped
    meanwhile. Both succeed, and each tenant's role is bound by its own
    tenant's policy alone."""
    tenants = {"a": db_url, "b": second_tenant}

    _define_writer()
    await asyncio.gather(
        *(auto_migrate(url, name=name) for name, url in tenants.items())
    )
    _rewind()

    _define_author_and_ledger()
    await asyncio.gather(
        *(auto_migrate(url, name=name, updates=True) for name, url in tenants.items())
    )

    for name in tenants:
        role = pg_role(f"tenant_{name}")
        schema = (await fetch_all("SELECT current_schema() AS s", using=name))[0]["s"]
        assert schema == (db_schema_name if name == "a" else f"{db_schema_name}_b")
        tables = await fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
            "ORDER BY tablename",
            using=name,
        )
        assert [row["tablename"] for row in tables] == ["isoauthor", "isoledger"]
        await execute(f'CREATE ROLE "{role}" NOSUPERUSER', using=name)
        await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"', using=name)
        await execute(f'GRANT SELECT ON "isoledger" TO "{role}"', using=name)
        await execute(
            "INSERT INTO isoledger (ledger_id, label) VALUES "
            f"('{uuid.uuid4()}', 'hidden')",
            using=name,
        )
        async with ferro.transaction(using=name) as tx:
            await tx.execute(f'SET LOCAL ROLE "{role}"')
            assert await tx.fetch_all("SELECT label FROM isoledger") == []
