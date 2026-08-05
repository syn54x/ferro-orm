"""Label addition (#329/#330): the reconciliation pass appends model-declared
labels missing from a live ferro-owned enum type. See ADR-0011 and CONTEXT.md
(*enum label*, *label addition*)."""

from enum import StrEnum

import pytest

import ferro
from ferro import Model, connect, engines, reset_engine
from ferro.raw import execute, fetch_all

pytestmark = [pytest.mark.backend_matrix, pytest.mark.postgres_only]


@pytest.mark.asyncio
async def test_migrate_updates_appends_missing_label_and_member_round_trips(
    db_url, clean_registry
):
    """#328's repro, fixed at the correct gate: a StrEnum grown after the type
    was created becomes usable on the next migrate_updates=True boot."""
    # An old deployment: the type was created when the StrEnum had one member.
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid')")
        await execute(
            'CREATE TABLE "feed" ('
            '"id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
        await execute("INSERT INTO \"feed\" (\"provider\") VALUES ('plaid')")
    reset_engine()

    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    await connect(db_url, migrate_updates=True)
    async with engines.session():
        created = await Feed.create(provider=Provider.MX)
        assert created.provider is Provider.MX

        fetched = await Feed.where(lambda f: f.provider == Provider.MX).all()
        assert len(fetched) == 1
        assert fetched[0].provider is Provider.MX

        # The old deployment's row is untouched.
        rows = await fetch_all('SELECT "provider" FROM "feed" ORDER BY "id"')
        assert [r["provider"] for r in rows] == ["plaid", "mx"]
