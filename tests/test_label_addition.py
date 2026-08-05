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
        await execute('INSERT INTO "feed" ("provider") VALUES (\'plaid\')')
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
    enum_warnings = [str(w.message) for w in record if "provider" in str(w.message)]
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


@pytest.mark.asyncio
async def test_shared_type_reconciles_exactly_once(db_url, clean_registry):
    """A StrEnum shared by two models is one type and reconciles once: one
    warning for its drift, and both tables accept the appended label."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid', 'legacy')")
        await execute(
            'CREATE TABLE "feed" ("id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
        await execute(
            'CREATE TABLE "payout" ("id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
    reset_engine()

    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    class Payout(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    with pytest.warns(UserWarning) as record:
        await connect(db_url, migrate_updates=True)
    per_type = [w for w in record if "'legacy'" in str(w.message)]
    assert len(per_type) == 1, "one warning per drifted type, not per table"

    async with engines.session():
        assert (await Feed.create(provider=Provider.MX)).provider is Provider.MX
        assert (await Payout.create(provider=Provider.MX)).provider is Provider.MX


@pytest.mark.asyncio
async def test_new_label_as_default_of_new_column_in_same_run(db_url, clean_registry):
    """The single-deploy shape: add a member AND a new column defaulting to it.
    The label commits (autocommit pre-pass) before the table plan references
    it — the trap Prisma #8424 documents."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"state\" AS ENUM ('open')")
        await execute('CREATE TABLE "ticket" ("id" serial PRIMARY KEY)')
        await execute('INSERT INTO "ticket" DEFAULT VALUES')
    reset_engine()

    class State(StrEnum):
        OPEN = "open"
        CLOSED = "closed"

    class Ticket(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        state: State = ferro.Field(default=State.CLOSED)

    await connect(db_url, migrate_updates=True)
    async with engines.session():
        rows = await fetch_all('SELECT "state" FROM "ticket"')
        assert [r["state"] for r in rows] == ["closed"], (
            "existing row backfilled with the appended label"
        )
        assert (await Ticket.create()).state is State.CLOSED


@pytest.mark.asyncio
async def test_new_table_defaulting_to_new_label_of_existing_stale_type(
    db_url, clean_registry
):
    """A brand new table whose enum column defaults to a new member of an
    existing stale type creates cleanly in one migrate_updates run: fresh
    CREATE TABLE never renders server-side defaults (they are client-side),
    so no create-pass statement can reference a label before label addition
    lands it."""
    await connect(db_url)
    async with engines.session():
        # The type exists (older model used it); the new table does not.
        await execute("CREATE TYPE \"state\" AS ENUM ('open')")
    reset_engine()

    class State(StrEnum):
        OPEN = "open"
        CLOSED = "closed"

    class Audit(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        state: State = ferro.Field(default=State.CLOSED)

    await connect(db_url, migrate_updates=True)
    async with engines.session():
        assert (await Audit.create()).state is State.CLOSED
        assert await _live_labels("state") == ["open", "closed"]


@pytest.mark.asyncio
async def test_appended_labels_sort_last_regardless_of_declaration_order(
    db_url, clean_registry
):
    """Documented caveat: ADD VALUE appends, so enum ORDER BY follows database
    order, not Python declaration order."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid')")
        await execute(
            'CREATE TABLE "feed" ("id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
    reset_engine()

    class Provider(StrEnum):
        MX = "mx"  # declared first in Python...
        PLAID = "plaid"

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    await connect(db_url, migrate_updates=True)
    async with engines.session():
        # ...but appended last in the database ordering.
        assert await _live_labels("provider") == ["plaid", "mx"]
        await Feed.create(provider=Provider.MX)
        await Feed.create(provider=Provider.PLAID)
        rows = await fetch_all('SELECT "provider" FROM "feed" ORDER BY "provider"')
        assert [r["provider"] for r in rows] == ["plaid", "mx"]


@pytest.mark.asyncio
async def test_second_boot_is_a_noop(db_url, clean_registry, recwarn):
    """Label addition is idempotent: a reconciled schema replans to nothing —
    no statements, no warnings, labels and order untouched."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid')")
        await execute(
            'CREATE TABLE "feed" ("id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
    reset_engine()

    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    await connect(db_url, migrate_updates=True)
    reset_engine()
    recwarn.clear()

    await connect(db_url, migrate_updates=True)
    assert not [w for w in recwarn if "provider" in str(w.message)]
    async with engines.session():
        assert await _live_labels("provider") == ["plaid", "mx"]


# ---------------------------------------------------------------------------
# Alembic comparator (#333): the second consumer of the label-addition
# decision (AGENTS.md § I-1) — autogenerate sees the same drift.
# ---------------------------------------------------------------------------


def _autogen_upgrade_code(postgres_base_url, db_schema_name):
    """Run real autogenerate against the live per-test schema and render the
    upgrade code, mirroring test_cross_emitter_parity.py's connection dance."""
    import sqlalchemy as sa
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata

    metadata = get_metadata()
    for scheme in ("postgresql://", "postgres://"):
        if postgres_base_url.startswith(scheme):
            sync_url = "postgresql+psycopg://" + postgres_base_url[len(scheme) :]
            break
    else:
        sync_url = postgres_base_url
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            script = produce_migrations(ctx, metadata)
        return render_python_code(script.upgrade_ops)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_autogenerate_emits_label_additions_in_autocommit_block(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """The reviewed-migration door sees the drift: the generated revision
    carries the addition inside an autocommit block (runnable on every
    supported PG version) and a comment for the warn-never-act direction."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid', 'legacy')")
        await execute(
            'CREATE TABLE "feed" ("id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
    reset_engine()

    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "autocommit_block" in code
    # The op.execute payload is the Rust-rendered statement (repr-quoted by
    # the renderer); assert on its stable substrings.
    assert "ADD VALUE IF NOT EXISTS" in code
    assert '"provider"' in code
    assert "mx" in code
    assert "legacy" in code, "extra live label surfaces as a revision comment"
    assert "reviewed" in code, "comment names the reviewed-migration exit"


@pytest.mark.asyncio
async def test_autogenerate_emits_nothing_for_enums_in_sync(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """No phantom diffs (AGENTS.md § I-1): model and database agree → the
    comparator stays silent."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"provider\" AS ENUM ('plaid', 'mx')")
        await execute(
            'CREATE TABLE "feed" ("id" serial PRIMARY KEY, "provider" "provider" NOT NULL)'
        )
    reset_engine()

    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"

    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "ADD VALUE" not in code
    assert "autocommit_block" not in code


# The cross-language statement parity pin lives in the canonical parity seam:
# tests/test_cross_emitter_parity.py::test_label_addition_statement_parity_pin.
