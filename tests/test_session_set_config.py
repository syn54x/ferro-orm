"""Live session settings mutation: ``Session.set_config`` and
``ferro.current_session()`` (#411).

``tests/test_session_settings.py`` covers settings declared at open;
``tests/test_session_settings_operations.py`` covers the deferred-resolution
flow through a real row-security policy. This file covers the mechanics of
mutating a *live* session: validation parity with declaration, the
un-entered-session guard, mid-transaction visibility, the no-redundant-
reapplication property, identity-map eviction, and nested-session snapshot
semantics.
"""

from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model, connect, engines, transaction

TENANT_KEY = "myapp.tenant_id"
ROLE_KEY = "myapp.role"


async def _current_setting(tx, key: str) -> str:
    row = await tx.fetch_one("select current_setting($1, true) as v", key)
    assert row is not None
    return row["v"] or ""


class SetConfigMarkerRow(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str


# ---------------------------------------------------------------------------
# `ferro.current_session()`
# ---------------------------------------------------------------------------


def test_current_session_returns_none_outside_any_session():
    assert ferro.current_session() is None


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_current_session_returns_the_open_session(db_url):
    await connect(db_url)
    async with engines.session() as session:
        assert ferro.current_session() is session
    assert ferro.current_session() is None


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_current_session_returns_the_innermost_nested_session(db_url):
    await connect(db_url)
    async with engines.session() as outer:
        assert ferro.current_session() is outer
        async with engines.session() as inner:
            assert ferro.current_session() is inner
        assert ferro.current_session() is outer


# ---------------------------------------------------------------------------
# `set_config` validation parity with declaration, and the un-entered-session
# guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_set_config_on_an_unentered_session_raises_runtime_error(db_url):
    """`set_config` mutates a *live* session — one that was never entered has
    nothing to mutate, and must say so rather than silently no-opping."""
    await connect(db_url)
    session = engines.session()
    assert session.session_id is None
    with pytest.raises(RuntimeError, match="not open"):
        await session.set_config(TENANT_KEY, "acme")


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_set_config_after_close_raises_runtime_error(db_url):
    await connect(db_url)
    session = engines.session()
    async with session:
        pass
    with pytest.raises(RuntimeError, match="not open"):
        await session.set_config(TENANT_KEY, "acme")


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_set_config_non_str_value_raises_type_error(db_url):
    await connect(db_url)
    async with engines.session() as session:
        with pytest.raises(TypeError, match="must be a str"):
            await session.set_config(TENANT_KEY, 42)


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_set_config_non_str_key_raises_type_error(db_url):
    await connect(db_url)
    async with engines.session() as session:
        with pytest.raises(TypeError, match="keys must be str"):
            await session.set_config(42, "acme")


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_set_config_undotted_key_raises_value_error(db_url):
    await connect(db_url)
    async with engines.session() as session:
        with pytest.raises(ValueError, match="dotted custom setting name"):
            await session.set_config("timezone", "UTC")


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_set_config_on_a_non_postgres_session_raises_runtime_error(db_url):
    """`set_config` is a declaration-equivalent act: the same Postgres-only
    gate `settings=` applies at open, regardless of what the session declared
    (or didn't, as here) when it was entered."""
    await connect(db_url)
    async with engines.session() as session:
        assert session.effective_settings == {}
        with pytest.raises(RuntimeError, match="require a Postgres connection"):
            await session.set_config(TENANT_KEY, "acme")
        # The rejected mutation must not have partially applied.
        assert session.effective_settings == {}


# ---------------------------------------------------------------------------
# Mid-transaction: the very next statement in an already-open transaction()
# sees the new value.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_mid_transaction_is_visible_to_the_next_statement(db_url):
    await connect(db_url)
    async with engines.session() as session:
        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == ""
            await session.set_config(TENANT_KEY, "acme")
            assert await _current_setting(tx, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_mid_transaction_reaches_every_open_savepoint(db_url):
    """`SET LOCAL` is transaction-scoped, not savepoint-scoped: a `set_config`
    made while nested `transaction()` blocks are open reaches all of them —
    they all share the root's connection."""
    await connect(db_url)
    async with engines.session() as session:
        async with transaction() as outer_tx:
            async with transaction() as inner_tx:
                await session.set_config(TENANT_KEY, "acme")
                assert await _current_setting(inner_tx, TENANT_KEY) == "acme"
            assert await _current_setting(outer_tx, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_after_a_transaction_closes_does_not_leak_backward(db_url):
    """Eager reapplication targets the transactions open *at the time of the
    call* — it must not somehow affect a transaction that already ended."""
    await connect(db_url)
    async with engines.session() as session:
        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == ""
        await session.set_config(TENANT_KEY, "acme")
        async with transaction() as tx2:
            assert await _current_setting(tx2, TENANT_KEY) == "acme"


# ---------------------------------------------------------------------------
# No redundant reapplication: an unchanged value is a true no-op.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_with_the_same_value_does_not_evict_the_identity_map(
    db_url,
):
    """The observable half of "no redundant reapplication": if `set_config`
    actually re-ran the mutation for a value that did not change, it would
    evict the identity map right along with it. It must not."""
    await connect(db_url, auto_migrate=True)

    async with engines.session(settings={TENANT_KEY: "acme"}) as session:
        created = await SetConfigMarkerRow.create(label="one")
        cached = await SetConfigMarkerRow.get(created.id)
        assert cached is created

        await session.set_config(TENANT_KEY, "acme")  # unchanged

        still_cached = await SetConfigMarkerRow.get(created.id)
        assert still_cached is created, (
            "an unchanged set_config must not touch the identity map"
        )

        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == "acme"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_with_the_same_value_is_not_an_error(db_url):
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme"}) as session:
        await session.set_config(TENANT_KEY, "acme")
        await session.set_config(TENANT_KEY, "acme")
        assert session.effective_settings == {TENANT_KEY: "acme"}


# ---------------------------------------------------------------------------
# Identity map eviction: `get()` after a scope change refetches.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_evicts_the_identity_map(db_url):
    """`get()` after a scope change must refetch rather than serve the
    instance loaded under the old scope (pattern per `tests/test_include.py`'s
    identity checks)."""
    await connect(db_url, auto_migrate=True)

    async with engines.session(settings={TENANT_KEY: "acme"}) as session:
        created = await SetConfigMarkerRow.create(label="one")
        first = await SetConfigMarkerRow.get(created.id)
        assert first is created

        await session.set_config(TENANT_KEY, "globex")

        second = await SetConfigMarkerRow.get(created.id)
        assert second is not first, "a scope change must evict the identity map"
        assert second.label == "one"


# ---------------------------------------------------------------------------
# Nested-session snapshot semantics (PRD #406 Q4 / #408's amendments): a
# child snapshotted before the parent's set_config keeps its own snapshot; a
# child entered after inherits the new value.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_child_session_entered_before_set_config_keeps_its_snapshot(db_url):
    await connect(db_url)
    async with engines.session(settings={TENANT_KEY: "acme"}) as outer:
        async with engines.session() as child_before:
            assert child_before.effective_settings == {TENANT_KEY: "acme"}

            await outer.set_config(TENANT_KEY, "globex")

            assert child_before.effective_settings == {TENANT_KEY: "acme"}, (
                "a child snapshotted before the parent's set_config keeps its "
                "own snapshot — nothing propagates live between sessions"
            )
            async with transaction() as tx:
                assert await _current_setting(tx, TENANT_KEY) == "acme"

        async with engines.session() as child_after:
            assert child_after.effective_settings == {TENANT_KEY: "globex"}, (
                "a child entered after the parent's set_config inherits the new value"
            )
            async with transaction() as tx:
                assert await _current_setting(tx, TENANT_KEY) == "globex"

    async with engines.session() as sibling:
        assert sibling.effective_settings == {}, (
            "the mutation must not have leaked past the session it was made on"
        )
