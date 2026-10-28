# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""Ferro connects to PostgreSQL 17 or newer.

``connect()`` reads ``server_version_num`` once and refuses an older server
before anything else happens on the connection::

    await ferro.connect("postgresql://app:secret@db/app")
    # InterfaceError: Ferro needs PostgreSQL 17 or newer;
    # postgresql://app:***@db/app runs PostgreSQL 16.4
    # (server_version_num 160004). Upgrade the server to 17 or later.

The refusal is driven through ``_core._report_server_version_for_test``,
which reports a version in place of the server's; ``None`` clears it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from urllib.parse import urlsplit

import psycopg
import pytest

import ferro
from ferro import Model, _core
from ferro.base import FerroField
from ferro.migrations import MigrationRefused
from ferro.migrations.testing import harness
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
)
from tests.test_migrations_testing_harness import build_chain

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def reported_version() -> Iterator[None]:
    """Clear the reported version around every test, so none leaks."""
    _core._report_server_version_for_test(None)
    yield
    _core._report_server_version_for_test(None)


def refusal(version: str, version_num: int) -> str:
    return (
        f"runs PostgreSQL {version} (server_version_num {version_num}). "
        "Upgrade the server to 17 or later."
    )


async def test_a_real_17_or_later_server_connects(db_url):
    await ferro.connect(db_url, name="floor")

    assert _core.connection_backend("floor") == "postgres"


async def test_a_server_reporting_170000_connects(db_url):
    _core._report_server_version_for_test(170000)

    await ferro.connect(db_url, name="floor")

    assert _core.connection_backend("floor") == "postgres"


async def test_sqlite_ignores_the_reported_version(tmp_path):
    _core._report_server_version_for_test(160004)

    await ferro.connect(f"sqlite:{tmp_path / 'floor.db'}?mode=rwc", name="floor")

    assert _core.connection_backend("floor") == "sqlite"


async def test_a_server_reporting_16_is_refused_and_not_registered(db_url):
    _core._report_server_version_for_test(160004)

    with pytest.raises(ferro.InterfaceError) as excinfo:
        await ferro.connect(db_url, name="floor")

    message = str(excinfo.value)
    assert message.startswith("Ferro needs PostgreSQL 17 or newer; postgresql://")
    assert refusal("16.4", 160004) in message
    password = urlsplit(db_url).password
    assert password and f":{password}@" not in message
    assert ":***@" in message, "the URL in the refusal is the redacted one"
    assert _core.connection_backend("floor") is None


async def test_a_refused_server_gets_no_tables_from_auto_migrate(
    db_url, postgres_base_url, db_schema_name
):
    class FloorAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    _core._report_server_version_for_test(160004)

    with pytest.raises(ferro.InterfaceError, match="needs PostgreSQL 17"):
        await ferro.connect(db_url, auto_migrate=True)

    with psycopg.connect(postgres_base_url) as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (db_schema_name,),
        ).fetchall()
    assert tables == []


@pytest.mark.usefixtures("isolated_imports", "clean_registry")
async def test_the_harness_meets_the_same_refusal(db_url, project, pkg):
    build_chain(project, pkg, "postgres")
    _core._report_server_version_for_test(160004)

    with pytest.raises(ferro.InterfaceError) as excinfo:
        await ferro.connect(db_url)
    assert refusal("16.4", 160004) in str(excinfo.value)

    # The refused connection was never registered, so the harness has none.
    with pytest.raises(MigrationRefused, match="no default connection"):
        await harness().apply_through("0001")
