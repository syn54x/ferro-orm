"""Session settings declared on a session and delivered in explicit transactions.

A session opened with ``settings={"myapp.tenant_id": "acme"}`` makes that
Postgres setting (GUC) visible to every statement inside every ``transaction()``
it opens — the scope a row-level-security policy reads back with
``current_setting()``. Delivery is one parameter-bound ``set_config`` batch
issued right after ``BEGIN``.
"""

from typing import Annotated

import pytest

import ferro
from ferro import connect, engines, transaction

TENANT_KEY = "myapp.tenant_id"
ROLE_KEY = "myapp.role"


async def _current_setting(tx, key: str) -> str:
    row = await tx.fetch_one("select current_setting($1, true) as v", key)
    assert row is not None
    return row["v"] or ""


# --------------------------------------------------------------------------
# Eager validation — raises at the call site, before any query runs.
# --------------------------------------------------------------------------


def test_non_str_value_raises_type_error():
    """A value must be a str: Postgres settings are text."""
    with pytest.raises(TypeError, match="must be a str"):
        engines.session(settings={TENANT_KEY: 42})


def test_non_str_key_raises_type_error():
    with pytest.raises(TypeError, match="keys must be str"):
        engines.session(settings={42: "acme"})


def test_undotted_key_raises_value_error():
    """Only custom (dotted) settings; built-ins would change how Ferro talks
    to the database."""
    with pytest.raises(ValueError, match="dotted custom setting name"):
        engines.session(settings={"timezone": "UTC"})


def test_non_mapping_settings_raises_type_error():
    with pytest.raises(TypeError, match="mapping of str to str"):
        engines.session(settings=[(TENANT_KEY, "acme")])


def test_validation_happens_before_any_connection_exists():
    """No engine, no connection, no query — the error is still raised."""
    with pytest.raises(ValueError):
        engines.session(settings={"nodots": "acme"})


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_declared_settings_on_non_postgres_connection_raise_at_open(db_url):
    """SQLite has no GUCs, so a session that *declares* settings fails loudly at
    enter rather than silently scoping nothing."""
    await connect(db_url)

    session = engines.session(settings={TENANT_KEY: "acme"})
    with pytest.raises(RuntimeError, match="require a Postgres connection"):
        await session.__aenter__()

    assert session.session_id is None


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_settings_less_session_still_works_on_sqlite(db_url):
    """The Postgres-only gate is about declared settings, not about sessions."""
    await connect(db_url)
    async with engines.session() as session:
        assert session.session_id is not None
        assert session.effective_settings == {}


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_settings_reassigned_after_construction_are_revalidated(db_url):
    """What gets applied is what got checked: the mapping is read at enter, so
    it is validated at enter too."""
    await connect(db_url)

    session = engines.session()
    session.settings = {"nodots": "acme"}

    with pytest.raises(ValueError, match="dotted custom setting name"):
        await session.__aenter__()

    assert session.session_id is None


# --------------------------------------------------------------------------
# Delivery inside explicit transactions.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_setting_visible_inside_transaction(db_url):
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme"}):
        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_every_key_applied_in_one_batch(db_url):
    """Several keys, one statement — all of them are live in the transaction."""
    await connect(db_url)
    settings = {TENANT_KEY: "acme", ROLE_KEY: "auditor", "myapp.request_id": "r-1"}
    async with engines.session(settings=settings):
        async with transaction() as tx:
            for key, value in settings.items():
                assert await _current_setting(tx, key) == value


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_setting_visible_in_every_transaction_of_the_session(db_url):
    """Each BEGIN re-applies: SET LOCAL dies with the previous transaction."""
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme"}):
        async with transaction() as first:
            assert await _current_setting(first, TENANT_KEY) == "acme"
        async with transaction() as second:
            assert await _current_setting(second, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_setting_visible_inside_nested_savepoint(db_url):
    """Nested transactions are savepoints on the same connection, and
    SET LOCAL is transaction-scoped — so they inherit with nothing emitted."""
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme"}):
        async with transaction():
            async with transaction() as inner:
                assert await _current_setting(inner, TENANT_KEY) == "acme"
                async with transaction() as innermost:
                    assert await _current_setting(innermost, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_setting_applies_on_a_second_postgres_connection(db_url):
    """Settings follow the session, not the routing: a nested session on
    another Postgres connection carries the outer scope with it."""
    await connect(db_url, name="primary", default=True)
    await connect(db_url, name="secondary")

    async with engines.session("primary", settings={TENANT_KEY: "acme"}):
        async with engines.session("secondary"):
            async with transaction(using="secondary") as tx:
                assert await _current_setting(tx, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_values_are_bound_not_interpolated(db_url):
    """A value full of SQL punctuation round-trips verbatim."""
    hostile = "'); drop table nope; --"
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: hostile}):
        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == hostile


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_setting_does_not_survive_the_transaction(db_url):
    """`is_local=true` is what makes pooling safe: the value dies at COMMIT,
    so the connection goes back to the pool clean."""
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme"}) as session:
        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == "acme"

        from ferro.raw import fetch_one

        row = await fetch_one(
            "select current_setting($1, true) as v",
            TENANT_KEY,
            session=session,
        )
        assert (row["v"] or "") == ""


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_session_without_settings_emits_nothing(db_url):
    """Zero settings, zero statements: nothing is set inside the transaction."""
    await connect(db_url)
    async with engines.session() as session:
        assert session.effective_settings == {}
        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == ""


# --------------------------------------------------------------------------
# Nested sessions: snapshot merge, child wins per key.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_nested_session_inherits_and_overrides_per_key(db_url):
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme", ROLE_KEY: "owner"}):
        async with engines.session(settings={ROLE_KEY: "auditor"}):
            async with transaction() as tx:
                assert await _current_setting(tx, TENANT_KEY) == "acme"
                assert await _current_setting(tx, ROLE_KEY) == "auditor"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_outer_session_unchanged_after_inner_closes(db_url):
    await connect(db_url)
    async with engines.session(
        settings={TENANT_KEY: "acme", ROLE_KEY: "owner"}
    ) as outer:
        async with engines.session(settings={ROLE_KEY: "auditor"}):
            pass

        assert outer.effective_settings == {TENANT_KEY: "acme", ROLE_KEY: "owner"}
        async with transaction() as tx:
            assert await _current_setting(tx, ROLE_KEY) == "owner"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_settings_less_nested_session_inherits_everything(db_url):
    """Helper code that opens a plain session stays scoped."""
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme"}):
        async with engines.session() as inner:
            assert inner.effective_settings == {TENANT_KEY: "acme"}
            async with transaction() as tx:
                assert await _current_setting(tx, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_mapping_mutated_before_enter_is_what_gets_applied(db_url):
    """The mapping is read at enter, so a change made before then is honoured —
    and validated — rather than a stale construction-time copy being applied."""
    await connect(db_url)

    declared = {TENANT_KEY: "acme"}
    session = engines.session(settings=declared)
    declared[TENANT_KEY] = "globex"

    async with session:
        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == "globex"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_declared_settings_are_not_mutated_by_the_merge(db_url):
    """The merge produces a snapshot; the caller's dict is left alone."""
    await connect(db_url)
    declared = {ROLE_KEY: "auditor"}
    async with engines.session(settings={TENANT_KEY: "acme"}):
        async with engines.session(settings=declared) as inner:
            assert inner.effective_settings == {
                TENANT_KEY: "acme",
                ROLE_KEY: "auditor",
            }
    assert declared == {ROLE_KEY: "auditor"}


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_orm_queries_inside_the_transaction_see_the_setting(db_url):
    """The point of the feature: ordinary model queries run scoped, with no
    per-query filter and no manual set_config."""

    class SettingsScopedRow(ferro.Model):
        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        label: str

    await connect(db_url)
    await ferro.create_tables()

    async with engines.session(settings={TENANT_KEY: "acme"}):
        async with transaction() as tx:
            await SettingsScopedRow.create(id=1, label="acme")
            rows = await SettingsScopedRow.where(lambda row: row.label == "acme").all()
            assert [row.id for row in rows] == [1]
            assert await _current_setting(tx, TENANT_KEY) == "acme"


# --------------------------------------------------------------------------
# The self-wrapped transaction: a chunked bulk_create outside transaction().
# --------------------------------------------------------------------------

# A chunked bulk_create opens its own transaction so the call stays
# all-or-nothing (the operation-atomicity invariant, #298). That BEGIN is a
# BEGIN like any other, so it carries the session's settings too. Bind budget:
# 5 rendered columns (the unset autoincrement pk is skipped), so 13,200 rows
# bind 66,000 parameters — past Postgres' 65,535 limit, which is what forces
# the chunking that opens the transaction.
STRADDLE_ROWS = 13_200


class ScopedChunkedItem(ferro.Model):
    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    label: str
    quantity: int
    price: float
    is_active: bool
    note: str


def _scoped_items(n: int) -> list[ScopedChunkedItem]:
    return [
        ScopedChunkedItem(
            label=f"item-{i}",
            quantity=i,
            price=i * 0.5,
            is_active=i % 2 == 0,
            note=f"note-{i}",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_chunked_bulk_create_outside_transaction_carries_the_setting(db_url):
    """A bulk_create big enough to chunk wraps itself in a transaction — and
    that transaction gets the session's settings, like any other.

    Delivery is observable without any policy: the table carries a column whose
    DEFAULT is `current_setting('myapp.tenant_id', true)`, so every row the
    batch inserts records what Postgres could see at insert time.
    """
    from ferro.raw import execute, fetch_all

    await connect(db_url, auto_migrate=True)

    async with engines.session():
        await execute(
            "ALTER TABLE scopedchunkeditem ADD COLUMN tenant text "
            "DEFAULT current_setting('myapp.tenant_id', true)"
        )

    async with engines.session(settings={TENANT_KEY: "acme"}):
        # No transaction() here on purpose: the wrap is the operation's own.
        inserted = await ScopedChunkedItem.bulk_create(_scoped_items(STRADDLE_ROWS))
        assert inserted == STRADDLE_ROWS

        rows = await fetch_all("select distinct tenant from scopedchunkeditem")
        assert [row["tenant"] for row in rows] == ["acme"]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_small_bulk_create_outside_transaction_carries_the_setting(db_url):
    """Tenancy must not depend on input size.

    Three rows fit in one INSERT, so this batch never chunks — and before the
    settings wrap it would have run on a raw pool connection with no scope,
    while the same call at 13,200 rows ran scoped. A settings-bearing session
    wraps either way.
    """
    from ferro.raw import execute, fetch_all

    await connect(db_url, auto_migrate=True)

    async with engines.session():
        await execute(
            "ALTER TABLE scopedchunkeditem ADD COLUMN tenant text "
            "DEFAULT current_setting('myapp.tenant_id', true)"
        )

    async with engines.session(settings={TENANT_KEY: "acme"}):
        inserted = await ScopedChunkedItem.bulk_create(_scoped_items(3))
        assert inserted == 3

        rows = await fetch_all("select distinct tenant from scopedchunkeditem")
        assert [row["tenant"] for row in rows] == ["acme"]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_settings_less_session_bulk_create_opens_no_extra_transaction(db_url):
    """The wrap is for settings only: a session without them keeps the old
    boundary, so a small batch stays a bare single statement."""
    from ferro.raw import execute, fetch_all

    await connect(db_url, auto_migrate=True)

    async with engines.session():
        await execute(
            "ALTER TABLE scopedchunkeditem ADD COLUMN tenant text "
            "DEFAULT current_setting('myapp.tenant_id', true)"
        )
        inserted = await ScopedChunkedItem.bulk_create(_scoped_items(3))
        assert inserted == 3

        rows = await fetch_all("select distinct tenant from scopedchunkeditem")
        assert [row["tenant"] for row in rows] == [None]


# --------------------------------------------------------------------------
# Multi-database nesting: inherited settings are inert off Postgres.
# --------------------------------------------------------------------------


async def _connect_pg_and_sqlite(db_url, tmp_path) -> None:
    await connect(db_url, name="pg", default=True)
    await connect(f"sqlite:{tmp_path / 'aux.db'}?mode=rwc", name="aux")


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_inherited_settings_are_inert_on_a_non_postgres_connection(
    db_url, tmp_path
):
    """A tenant-scoped request can still reach a side database.

    The inner session inherits the scope (a Postgres session nested deeper
    needs it) but SQLite has no GUCs to apply it to, so it simply lies inert —
    opening the session and running transactions there both work.
    """
    from ferro.raw import execute, fetch_all

    await _connect_pg_and_sqlite(db_url, tmp_path)

    async with engines.session("pg", settings={TENANT_KEY: "acme"}):
        async with engines.session("aux") as aux:
            assert aux.effective_settings == {TENANT_KEY: "acme"}

            async with transaction():
                await execute("CREATE TABLE aux_marker (id INTEGER PRIMARY KEY)")
                await execute("INSERT INTO aux_marker (id) VALUES (1)")

            rows = await fetch_all("SELECT id FROM aux_marker")
            assert rows == [{"id": 1}]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_postgres_grandchild_under_a_sqlite_session_still_inherits(
    db_url, tmp_path
):
    """Inherited settings ride through the SQLite session rather than being
    dropped there — the Postgres session below still applies them."""
    await _connect_pg_and_sqlite(db_url, tmp_path)

    async with engines.session("pg", settings={TENANT_KEY: "acme"}):
        async with engines.session("aux"):
            async with engines.session("pg") as grandchild:
                assert grandchild.effective_settings == {TENANT_KEY: "acme"}
                async with transaction() as tx:
                    assert await _current_setting(tx, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_declared_settings_on_a_nested_sqlite_session_still_raise(
    db_url, tmp_path
):
    """Inheriting is inert; *asking* for scope the backend cannot give is not."""
    await _connect_pg_and_sqlite(db_url, tmp_path)

    async with engines.session("pg", settings={TENANT_KEY: "acme"}):
        nested = engines.session("aux", settings={ROLE_KEY: "auditor"})
        with pytest.raises(RuntimeError, match="require a Postgres connection"):
            await nested.__aenter__()
        assert nested.session_id is None
