"""The Row-level security guide's JSON claims: a Postgres connection named
``"app"`` and the ``auth_uid()`` function the guide creates.

A block marked ``{test=run prelude=jwt_claims backend=postgres}`` runs on
PostgreSQL. ``opened()`` connects under the name ``"app"`` and creates
``auth_uid()`` with the guide's own statement; the block opens its own
session, with the claims it sends.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import ferro
from ferro import engines, execute

AUTH_UID = """\
CREATE OR REPLACE FUNCTION auth_uid() RETURNS uuid
LANGUAGE sql STABLE
AS $$
  SELECT (NULLIF(current_setting('request.jwt.claims', true), '')::json ->> 'sub')::uuid
$$"""
"""The function the guide creates in a migration, as the guide shows it."""


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect as ``"app"`` and create ``auth_uid()``."""
    await ferro.connect("postgresql://localhost/app", name="app")
    async with engines.session("app"):
        await execute(AUTH_UID)
    try:
        yield
    finally:
        ferro.reset_engine()
