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

import asyncio
import uuid
from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model, connect, engines, transaction
from ferro.raw import execute, fetch_one

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


# ---------------------------------------------------------------------------
# Concurrency contract (gate review of PR #421): the swap+merge happens
# server-side, inside one lock, so two sibling tasks calling `set_config`
# with different keys can never lose one to a stale-mirror race; and any
# task's operation that STARTS after `set_config`'s `await` returns sees the
# new value, whether or not it is the task that called `set_config`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_concurrent_set_config_on_different_keys_never_loses_one(db_url):
    """Before the fix: both calls merged against the SAME Python-side mirror
    and each replaced it wholesale, so whichever call's assignment landed
    second silently dropped the other's key. The merge is now server-side,
    against the session's own last-committed settings, inside the lock that
    serializes the read and the write together — so this always keeps both."""
    await connect(db_url)
    rounds = 50

    async with engines.session() as session:

        async def setter(key: str, values: list[str]) -> None:
            for value in values:
                await session.set_config(key, value)
                await asyncio.sleep(0)

        await asyncio.gather(
            setter(TENANT_KEY, [f"acme-{i}" for i in range(rounds)]),
            setter(ROLE_KEY, [f"owner-{i}" for i in range(rounds)]),
        )

        assert set(session.effective_settings) == {TENANT_KEY, ROLE_KEY}
        assert session.effective_settings[TENANT_KEY] == f"acme-{rounds - 1}"
        assert session.effective_settings[ROLE_KEY] == f"owner-{rounds - 1}"

        async with transaction() as tx:
            assert await _current_setting(tx, TENANT_KEY) == f"acme-{rounds - 1}"
            assert await _current_setting(tx, ROLE_KEY) == f"owner-{rounds - 1}"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_scopes_an_operation_a_different_task_starts_afterward(
    db_url,
):
    """The cross-task half of the concurrency contract, pinned directly:
    `set_config` need not be called by the same task that runs the next
    operation — any task's operation that STARTS after `set_config`'s `await`
    returns sees the new value, because the settings swap happens-before that
    return."""
    await connect(db_url)
    async with engines.session() as session:
        await session.set_config(TENANT_KEY, "acme")

        async def read_from_another_task() -> str:
            async with transaction() as tx:
                return await _current_setting(tx, TENANT_KEY)

        seen = await asyncio.create_task(read_from_another_task())
        assert seen == "acme"


# ---------------------------------------------------------------------------
# Reapply failure (gate review of PR #421): a mid-transaction `set_config`
# whose reapply statement fails must still leave the session's recorded
# settings and its identity map consistent with EACH OTHER (both already
# reflect the change — the swap and the eviction commit before the
# reapply is even attempted), and a later call must not have lost the key the
# failed call installed.
#
# The failure is forced by revoking EXECUTE on `set_config` from a created
# NOSUPERUSER role — not by killing the connection: a superuser bypasses the
# revoke (the matrix connects as one), and killing the connection would also
# break the surrounding `transaction()` block's own COMMIT/ROLLBACK, which is
# a separate concern this test is not about.
# ---------------------------------------------------------------------------


class ReapplyFailureRow(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str


@pytest.fixture
def reapply_failure_role() -> str:
    return f"ferro_reapply_fail_{uuid.uuid4().hex[:12]}"


async def _revoke_set_config(role: str) -> None:
    schema = (await fetch_one("SELECT current_schema() AS s"))["s"]
    await execute(f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER PASSWORD 'ferro-pw'")
    await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    await execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON reapplyfailurerow TO "{role}"'
    )
    await execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"')
    # PUBLIC, not the role specifically: a role-specific REVOKE would still
    # leave PUBLIC's grant in effect for that role.
    await execute(
        "REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC"
    )


async def _restore_set_config() -> None:
    await execute(
        "GRANT EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) TO PUBLIC"
    )


async def _drop_role(role: str) -> None:
    await execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE usename = $1 AND pid <> pg_backend_pid()",
        role,
    )
    await execute(f'DROP OWNED BY "{role}"')
    await execute(f'DROP ROLE IF EXISTS "{role}"')


def _role_url(db_url: str, role: str) -> str:
    from urllib.parse import quote, urlparse, urlunparse

    parsed = urlparse(db_url)
    netloc = f"{quote(role, safe='')}:{quote('ferro-pw', safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_reapply_failure_still_commits_the_swap_and_the_eviction(
    db_url, reapply_failure_role
):
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _revoke_set_config(reapply_failure_role)

    await connect(_role_url(db_url, reapply_failure_role), name="limited")
    try:
        async with engines.session("limited") as session:
            # No settings yet, so these run unwrapped — no permission needed.
            created = await ReapplyFailureRow.create(label="one")
            first = await ReapplyFailureRow.get(created.id)
            assert first is created

            async with transaction(using="limited"):
                with pytest.raises(Exception, match="permission denied"):
                    await session.set_config(TENANT_KEY, "acme")
            # The transaction rolled back cleanly (the connection itself was
            # never touched, only a statement inside it failed) — the session
            # is still usable, per the operation-atomicity precedent. Rust's
            # own settings are already `{TENANT_KEY: "acme"}` at this point
            # (the swap committed before the reapply was even attempted), so
            # EVERY operation on this session — not just the one that failed
            # — now tries to deliver that scope and would hit the same
            # permission error, until it is restored below.

            # The failed call's key was NOT lost: a second call with the
            # SAME value is a true no-op (nothing changed from what the first
            # call already committed server-side), so it succeeds with NO
            # reapply attempt and NO new `BEGIN` at all — proving the value
            # survived despite the exception, and despite the still-revoked
            # permission (this assertion runs before restoring it, on
            # purpose, so a false pass from restored permissions is
            # impossible).
            await session.set_config(TENANT_KEY, "acme")
            assert session.effective_settings == {TENANT_KEY: "acme"}, (
                "a repeat call after the failure must see the first call's "
                "key, not silently start over from an empty mirror"
            )

            # Restore permission (as the superuser matrix connection, which
            # the revoke never touched) so an operation on this now
            # tenant-scoped session can run at all again.
            async with engines.session():
                await _restore_set_config()

            # NOW the identity-map eviction is checkable: this `get()` must
            # refetch rather than serve `first`, because the eviction
            # committed back at the failed `set_config` call — well before
            # permission was restored, and unconditionally on the reapply
            # outcome.
            second = await ReapplyFailureRow.get(created.id)
            assert second is not first, (
                "the identity-map eviction commits before the reapply is "
                "even attempted, so a reapply failure must not leave it stale"
            )
            assert second.label == "one"
    finally:
        async with engines.session():
            await _restore_set_config()
            await _drop_role(reapply_failure_role)
