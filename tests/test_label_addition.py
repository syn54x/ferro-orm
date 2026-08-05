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


async def _live_labels(type_name: str) -> list[str]:
    rows = await fetch_all(
        "SELECT e.enumlabel AS label FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "JOIN pg_enum e ON e.enumtypid = t.oid "
        f"WHERE n.nspname = current_schema() AND t.typname = '{type_name}' "
        "ORDER BY e.enumsortorder"
    )
    return [r["label"] for r in rows]


@pytest.mark.asyncio
async def test_extra_live_labels_warn_and_are_never_removed(db_url, clean_registry):
    """Warn-never-act (ADR-0011): a live label the model no longer declares is
    named in a UserWarning — with the reviewed-migration exit — and survives."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid', 'legacy')")
        await execute(
            'CREATE TABLE "feed" ('
            '"id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
    reset_engine()

    class Provider(StrEnum):
        PLAID = "plaid"

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    with pytest.warns(UserWarning, match=r"legacy") as record:
        await connect(db_url, migrate_updates=True)
    enum_warnings = [
        str(w.message) for w in record if "provider" in str(w.message)
    ]
    assert len(enum_warnings) == 1, "warning fires per drifted type, exactly once"
    assert "'legacy'" in enum_warnings[0]
    assert "Alembic" in enum_warnings[0], "warning names the reviewed-migration exit"

    async with engines.session():
        assert await _live_labels("provider") == ["plaid", "legacy"]


@pytest.mark.asyncio
async def test_plain_auto_migrate_stays_silent_and_inert_with_drift(
    db_url, clean_registry, recwarn
):
    """The create pass neither acts nor warns on enum drift in either
    direction (ADR-0011): drift handling of every kind is migrate_updates'."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid', 'legacy')")
        await execute(
            'CREATE TABLE "feed" ('
            '"id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
    reset_engine()

    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"  # missing live; 'legacy' is extra live: drift both ways

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    await connect(db_url, auto_migrate=True)
    assert not [w for w in recwarn if "provider" in str(w.message)]
    async with engines.session():
        assert await _live_labels("provider") == ["plaid", "legacy"]
