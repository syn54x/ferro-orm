"""The Postgres fixture of the testing how-to (docs/pages/howto/testing.md).

``tests/test_docs_testing_fixtures.py`` runs it against a Postgres server
when one is available (``FERRO_POSTGRES_URL`` or local server binaries).
"""

# --8<-- [start:postgres]
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from ferro import connect, engines, execute, reset_engine

POSTGRES_URL = os.environ.get(
    "TEST_POSTGRES_URL", "postgresql://localhost:5432/app_test"
)


def with_search_path(url: str, schema: str) -> str:
    parts = urlsplit(url)
    query = urlencode([*parse_qsl(parts.query), ("ferro_search_path", schema)])
    return urlunsplit(parts._replace(query=query))


@pytest.fixture
async def pg_db():
    """A Postgres schema of its own for each test, dropped when it ends."""
    schema = f"test_{uuid.uuid4().hex[:8]}"

    await connect(POSTGRES_URL)
    async with engines.session():
        await execute(f'CREATE SCHEMA "{schema}"')
    reset_engine()

    await connect(with_search_path(POSTGRES_URL, schema), auto_migrate=True)
    async with engines.session():
        yield
        await execute(f'DROP SCHEMA "{schema}" CASCADE')
    reset_engine()


# --8<-- [end:postgres]
