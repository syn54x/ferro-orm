"""``settings_delivery="connection"``: one pinned connection per session.

``tests/test_session_settings.py`` and ``tests/test_session_settings_operations.py``
cover the default ``transaction`` delivery, where every operation wraps itself in
a transaction and re-sends ``SET LOCAL``. This file covers the opt-in mode, where
Ferro instead takes one pool connection at the session's first operation, sets the
values on it once — for the whole database session, not one transaction — and
sends every later statement bare.

What that has to be true for it to be safe, and what each test below pins:

* every statement of the session really does go to that one connection
  (``pg_backend_pid()``), transactions included;
* the values are readable with no transaction anywhere in sight, and the
  operation opens none (``CREATE INDEX CONCURRENTLY``, which Postgres refuses
  inside a transaction block, runs fine);
* closing the session resets **exactly the keys it set** and nothing else — the
  pool's ``search_path`` has to survive, which is why it is never ``RESET ALL``;
* a cancelled operation's connection is discarded, never recycled with a live
  tenant value on it;
* two pinned sessions never see each other, and settings-*less* sessions never
  pin at all.
"""

import asyncio
import uuid
from typing import Annotated
from urllib.parse import quote, urlparse, urlunparse

import pytest

import ferro
from ferro import PoolConfig, connect, engines
from ferro.raw import execute, fetch_all, fetch_one

TENANT_KEY = "myapp.tenant_id"
ROLE_KEY = "myapp.role"

ACME = "acme"
OTHER = "other"

TENANT_PASSWORD = "ferro-tenant-pw"


class DeliveryRow(ferro.Model):
    """One row of tenant data, used for pinning, atomicity and RLS checks."""

    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    ledger: str
    label: Annotated[str, ferro.FerroField(unique=True)]


# ---------------------------------------------------------------------------
# Reading the connection back: who am I talking to, and what does it know?
# ---------------------------------------------------------------------------


async def _pid() -> int:
    """The Postgres backend serving this statement."""
    return (await fetch_one("SELECT pg_backend_pid() AS pid"))["pid"]


async def _setting(key: str) -> str:
    """A session setting as the server sees it; ``''`` once it is reset."""
    row = await fetch_one("SELECT current_setting($1, true) AS v", key)
    return row["v"] or ""


async def _open_pinned_pool(db_url: str, *, max_connections: int = 2) -> None:
    """Connect the default connection in ``connection`` delivery."""
    await connect(
        db_url,
        auto_migrate=True,
        pool=PoolConfig(
            max_connections=max_connections, settings_delivery="connection"
        ),
    )


# ---------------------------------------------------------------------------
# The mode is opt-in, spelled one of two ways, and never guessed
# ---------------------------------------------------------------------------


def test_delivery_defaults_to_transaction():
    """Nobody gets pinned connections by accident."""
    assert PoolConfig().settings_delivery == "transaction"
    assert PoolConfig(settings_delivery="connection").settings_delivery == "connection"


def test_an_unknown_delivery_is_rejected_at_the_call_site():
    with pytest.raises(ValueError):
        PoolConfig(settings_delivery="session")
    with pytest.raises(ValueError):
        PoolConfig(settings_delivery="pgbouncer")


def test_the_core_rejects_an_unknown_delivery_too():
    """`PoolConfig` is the front door, but the FFI keeps its own gate."""
    from ferro._core import connect as _core_connect

    with pytest.raises(ValueError, match="Unknown settings_delivery"):
        _core_connect("sqlite::memory:", settings_delivery="whatever")


# ---------------------------------------------------------------------------
# Pinning: one session, one connection, for everything it sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_every_statement_of_a_settings_session_hits_one_backend(db_url):
    """The headline. Plain operations, a ``transaction()`` block, and the
    operations after it all run on the same Postgres backend — and the settings
    are readable throughout, including with no transaction open."""
    await _open_pinned_pool(db_url, max_connections=3)

    async with engines.session(settings={TENANT_KEY: ACME}):
        first = await _pid()
        assert await _setting(TENANT_KEY) == ACME

        second = await _pid()
        await DeliveryRow.create(ledger=ACME, label=f"a-{uuid.uuid4().hex[:8]}")
        after_write = await _pid()

        async with ferro.transaction():
            inside = await _pid()
            # The transaction opened on the pinned connection and re-applied
            # nothing: the values were already there, at session scope.
            assert await _setting(TENANT_KEY) == ACME

        after_transaction = await _pid()

    assert {second, after_write, inside, after_transaction} == {first}, (
        "a settings-bearing session must never spread its statements across "
        "connections in this mode"
    )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_pinned_operation_opens_no_transaction_of_its_own(db_url):
    """The mode's whole point, made observable.

    ``CREATE INDEX CONCURRENTLY`` is the discriminator, because Postgres
    simply refuses to run it inside a transaction block. Under the default
    ``transaction`` delivery a settings-bearing session wraps every operation,
    so the statement fails; under ``connection`` delivery there is no wrap for
    it to fail against — the settings are already on the connection, and the
    operation sends nothing but its own statement.
    """
    await _open_pinned_pool(db_url, max_connections=2)
    await connect(
        db_url,
        name="wrapped",
        pool=PoolConfig(max_connections=2, settings_delivery="transaction"),
    )
    index = f"idx_deliveryrow_unwrapped_{uuid.uuid4().hex[:8]}"

    async with engines.session(settings={TENANT_KEY: ACME}):
        await execute(f"CREATE INDEX CONCURRENTLY {index} ON deliveryrow (ledger)")

    async with engines.session("wrapped", settings={TENANT_KEY: ACME}):
        with pytest.raises(Exception) as excinfo:
            await execute(
                f"CREATE INDEX CONCURRENTLY {index}_wrapped ON deliveryrow (ledger)"
            )
        assert "transaction" in str(excinfo.value).lower(), (
            "the discriminator itself is broken if the default mode also runs "
            "this unwrapped"
        )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_settings_less_session_never_pins(db_url):
    """Nothing changes for the sessions that are not using this feature.

    A one-connection pool would deadlock the second of two concurrent sessions
    if settings-less sessions pinned. They don't, so both finish.
    """
    await _open_pinned_pool(db_url, max_connections=1)

    async def one_op() -> int:
        async with engines.session():
            await asyncio.sleep(0)
            return await _pid()

    pids = await asyncio.wait_for(
        asyncio.gather(one_op(), one_op(), one_op()), timeout=10
    )
    assert len(pids) == 3, "settings-less sessions share the pool as they always did"


# ---------------------------------------------------------------------------
# Closing: reset exactly what this session set, and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_closing_resets_only_this_sessions_keys(db_url, db_schema_name):
    """A one-connection pool, so the next session provably gets the *same*
    backend back — the one carrying whatever the first session left on it.

    ``search_path`` is the trap this test exists for: it is pool-level state
    Ferro installs on every connection, so a `RESET ALL` at close would leave
    every later query resolving tables in the wrong schema.
    """
    await _open_pinned_pool(db_url, max_connections=1)

    async with engines.session(settings={TENANT_KEY: ACME, ROLE_KEY: "owner"}):
        pinned_pid = await _pid()
        assert await _setting(TENANT_KEY) == ACME
        assert await _setting("search_path") == db_schema_name

    async with engines.session():
        assert await _pid() == pinned_pid, (
            "the pool handed the connection back, which is what makes the "
            "assertions below meaningful"
        )
        assert await _setting(TENANT_KEY) == "", "a tenant value survived the close"
        assert await _setting(ROLE_KEY) == ""
        assert await _setting("search_path") == db_schema_name, (
            "close reset the pool's search_path — that is RESET ALL, not a "
            "targeted reset"
        )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_lands_on_the_pinned_connection(db_url):
    """A value resolved mid-session applies immediately, with no transaction
    to carry it — and joins the reset list, so it is gone at close too."""
    await _open_pinned_pool(db_url, max_connections=1)

    async with engines.session(settings={TENANT_KEY: ACME}) as session:
        pinned_pid = await _pid()

        await session.set_config(ROLE_KEY, "auditor")
        assert await _setting(ROLE_KEY) == "auditor", (
            "the change has to be live for the very next statement, wrap or no wrap"
        )
        assert await _setting(TENANT_KEY) == ACME, "the other key was left alone"

        await session.set_config(TENANT_KEY, OTHER)
        assert await _setting(TENANT_KEY) == OTHER
        assert await _pid() == pinned_pid, "set_config must not re-pin"

    async with engines.session():
        assert await _pid() == pinned_pid
        assert await _setting(TENANT_KEY) == ""
        assert await _setting(ROLE_KEY) == "", (
            "a key added by set_config after the pin must still be reset at close"
        )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_set_config_evicts_the_identity_map(db_url):
    """An instance loaded under one scope is never handed back under another."""
    await _open_pinned_pool(db_url, max_connections=1)

    async with engines.session():
        row = await DeliveryRow.create(ledger=ACME, label=f"m-{uuid.uuid4().hex[:8]}")
        row_id = row.id

    async with engines.session(settings={TENANT_KEY: ACME}) as session:
        before = await DeliveryRow.get(row_id)
        again = await DeliveryRow.get(row_id)
        assert again is before, "same scope, same instance"

        await session.set_config(TENANT_KEY, OTHER)
        after = await DeliveryRow.get(row_id)
        assert after is not before, (
            "the scope changed, so the cached instance must not be served again"
        )


# ---------------------------------------------------------------------------
# Multi-statement atomicity still holds on the pinned connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_failed_multi_statement_operation_rolls_back_and_keeps_the_pin(db_url):
    """The operation-atomicity invariant does not lapse in this mode.

    A ``bulk_create`` that trips a unique constraint partway must leave nothing
    behind — so the operation still opens a transaction, just a bare one (the
    settings are already on the connection; there is no batch to re-send). And
    the rollback must leave the pin usable: this is one connection the session
    cannot afford to lose to a routine constraint violation.
    """
    await _open_pinned_pool(db_url, max_connections=1)
    clash = f"clash-{uuid.uuid4().hex[:8]}"

    async with engines.session(settings={TENANT_KEY: ACME}) as session:
        pinned_pid = await _pid()

        with pytest.raises(Exception):
            await DeliveryRow.bulk_create(
                [
                    DeliveryRow(ledger=ACME, label=clash),
                    DeliveryRow(ledger=ACME, label=clash),
                ]
            )

        assert await _pid() == pinned_pid, "the rollback cost the session its pin"
        assert await DeliveryRow.where(lambda r: r.label == clash).count() == 0
        assert await _setting(TENANT_KEY) == ACME, (
            "and the settings survived the rolled-back wrap"
        )

        await session.set_config(ROLE_KEY, "owner")
        assert await _setting(ROLE_KEY) == "owner"


# ---------------------------------------------------------------------------
# One session, one connection, therefore one operation at a time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_sibling_atomic_operations_stay_atomic(db_url):
    """Per-operation atomicity survives many tasks sharing one pinned session.

    This is the bug a per-*statement* lock would leave open. Concurrent
    ``bulk_create`` calls each open a transaction on the one connection the
    session owns; interleaved, the second ``BEGIN`` is a no-op Postgres merely
    warns about, the first ``COMMIT`` commits the other operation's
    half-written rows, and the other's rollback rolls back nothing. The pin is
    therefore claimed for the whole operation, not each statement.

    The unique index makes half the batches fail partway, so "atomic" has
    something to prove: a failed batch's rows must be entirely absent while
    every successful batch's are entirely present. Many pairs at once, because
    the window a per-statement lock would leave open is narrow — the
    deterministic proof of the claim is
    ``test_a_sibling_operation_waits_for_an_open_transaction`` below; this one
    is the end-to-end scenario it protects.
    """
    await _open_pinned_pool(db_url, max_connections=2)
    tag = uuid.uuid4().hex[:8]
    pairs = 8

    async def good(n: int) -> None:
        await DeliveryRow.bulk_create(
            [
                DeliveryRow(ledger=ACME, label=f"good-{n}-a-{tag}"),
                DeliveryRow(ledger=ACME, label=f"good-{n}-b-{tag}"),
            ]
        )

    async def doomed(n: int) -> None:
        clash = f"clash-{n}-{tag}"
        with pytest.raises(Exception):
            await DeliveryRow.bulk_create(
                [
                    DeliveryRow(ledger=ACME, label=f"doomed-{n}-{tag}"),
                    DeliveryRow(ledger=ACME, label=clash),
                    DeliveryRow(ledger=ACME, label=clash),
                ]
            )

    async with engines.session(settings={TENANT_KEY: ACME}):
        pinned_pid = await _pid()
        # The timeout is the regression detector: a pin that deadlocks fails
        # this test instead of hanging the suite.
        await asyncio.wait_for(
            asyncio.gather(
                *(good(n) for n in range(pairs)),
                *(doomed(n) for n in range(pairs)),
            ),
            timeout=30,
        )

        labels = {
            row.label
            for row in await DeliveryRow.where(lambda row: row.ledger == ACME).all()
        }
        for n in range(pairs):
            assert f"good-{n}-a-{tag}" in labels and f"good-{n}-b-{tag}" in labels, (
                "an operation that succeeded lost rows to a sibling's rollback"
            )
            assert f"doomed-{n}-{tag}" not in labels, (
                "an operation that failed left rows behind — a sibling's COMMIT "
                "committed them"
            )
            assert f"clash-{n}-{tag}" not in labels
        assert await _pid() == pinned_pid, "every operation ran on the one pin"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_sibling_operation_waits_for_an_open_transaction(db_url):
    """A sibling task's operation runs after the block, never inside it.

    Without the claim, task B's insert would land inside task A's open
    transaction and vanish with A's rollback. Rolling A back is exactly how
    the test can tell: B's row has to survive it.
    """
    await _open_pinned_pool(db_url, max_connections=2)
    tag = uuid.uuid4().hex[:8]
    holding = asyncio.Event()
    release = asyncio.Event()

    class Abandon(Exception):
        pass

    async with engines.session(settings={TENANT_KEY: ACME}):

        async def holder() -> None:
            try:
                async with ferro.transaction():
                    await DeliveryRow.create(ledger=ACME, label=f"rolled-{tag}")
                    holding.set()
                    await release.wait()
                    raise Abandon
            except Abandon:
                pass

        holding_task = asyncio.create_task(holder())
        await asyncio.wait_for(holding.wait(), timeout=10)

        # Created outside the transaction's context, so this task routes as an
        # ordinary operation on the session — not as part of the block.
        sibling = asyncio.create_task(
            DeliveryRow.create(ledger=ACME, label=f"kept-{tag}")
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(sibling), timeout=0.5)
        assert not sibling.done(), (
            "a sibling task's operation ran while a transaction() block held "
            "the pinned connection"
        )

        release.set()
        await asyncio.wait_for(holding_task, timeout=10)
        await asyncio.wait_for(sibling, timeout=10)

        labels = {
            row.label
            for row in await DeliveryRow.where(lambda row: row.ledger == ACME).all()
        }
        assert f"kept-{tag}" in labels, (
            "the sibling's row was inside the rolled-back transaction"
        )
        assert f"rolled-{tag}" not in labels


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_an_operation_inside_its_own_transaction_still_runs(db_url):
    """The reentrancy case: same task, inside the block, takes no new claim.

    If an operation inside ``transaction()`` tried to claim the pin the block
    already holds, every transactional write in this mode would deadlock. It
    doesn't — the block owns the span, and its operations ride it.
    """
    await _open_pinned_pool(db_url, max_connections=1)
    tag = uuid.uuid4().hex[:8]

    async def body() -> None:
        async with engines.session(settings={TENANT_KEY: ACME}):
            async with ferro.transaction():
                await DeliveryRow.create(ledger=ACME, label=f"inner-1-{tag}")
                await DeliveryRow.create(ledger=ACME, label=f"inner-2-{tag}")
                assert await _setting(TENANT_KEY) == ACME
            assert await DeliveryRow.where(lambda row: row.ledger == ACME).count() >= 2

    await asyncio.wait_for(body(), timeout=20)


# ---------------------------------------------------------------------------
# Cancellation: a discarded connection, never a recycled dirty one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_cancelled_operation_discards_the_pinned_connection(db_url):
    """The leak this mode could have introduced, and does not.

    Cancel an operation mid-statement and its connection is one Ferro can no
    longer describe — and in this mode it carries a live tenant value at
    *database-session* scope, with no transaction end to clear it. So it is
    discarded, the pool opens a replacement, and the next session gets a clean
    backend.

    A one-connection pool is the point: the next session has no choice but to
    use whatever came back.
    """
    await _open_pinned_pool(db_url, max_connections=1)

    async with engines.session(settings={TENANT_KEY: ACME}):
        poisoned_pid = await _pid()
        with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
            await asyncio.wait_for(fetch_all("SELECT pg_sleep(5)"), timeout=0.2)

    async with engines.session(settings={TENANT_KEY: OTHER}) as session:
        fresh_pid = await asyncio.wait_for(_pid(), timeout=10)
        assert fresh_pid != poisoned_pid, (
            "the cancelled operation's connection was handed to the next session"
        )
        assert await _setting(TENANT_KEY) == OTHER
        assert session is ferro.current_session()

    # And the pool is not down a connection it can never replace.
    async with engines.session():
        assert await _setting(TENANT_KEY) == ""


# ---------------------------------------------------------------------------
# Concurrency: the cap, and the isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_settings_sessions_are_capped_by_pool_size(db_url):
    """The mode's headline cost, made concrete.

    A settings-bearing session holds a connection from its first operation
    until it closes, so on a one-connection pool the second such session's
    first operation *waits* — and then completes, the moment the first session
    lets go. That is an ordinary pool checkout, not a deadlock and not an
    error.
    """
    await _open_pinned_pool(db_url, max_connections=1)

    pinned = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> int:
        async with engines.session(settings={TENANT_KEY: ACME}):
            pid = await _pid()
            pinned.set()
            await release.wait()
            return pid

    async def waiter() -> int:
        async with engines.session(settings={TENANT_KEY: OTHER}):
            return await _pid()

    holding = asyncio.create_task(holder())
    await asyncio.wait_for(pinned.wait(), timeout=10)

    waiting = asyncio.create_task(waiter())
    with pytest.raises(asyncio.TimeoutError):
        # `shield` so the timeout cancels the wait, not the waiting session.
        await asyncio.wait_for(asyncio.shield(waiting), timeout=0.5)
    assert not waiting.done(), (
        "session two's first operation must wait for a connection"
    )

    release.set()
    held_pid = await asyncio.wait_for(holding, timeout=10)
    waited_pid = await asyncio.wait_for(waiting, timeout=10)
    assert waited_pid == held_pid, "the released connection is the one it got"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_two_pinned_sessions_never_see_each_other(db_url):
    """Two connections, two sessions, two tenants — and no bleed either way."""
    await _open_pinned_pool(db_url, max_connections=2)

    both_pinned = asyncio.Barrier(2) if hasattr(asyncio, "Barrier") else None
    seen: dict[str, tuple[int, str]] = {}

    async def scoped(tenant: str) -> None:
        async with engines.session(settings={TENANT_KEY: tenant}):
            pid = await _pid()
            if both_pinned is not None:
                await both_pinned.wait()
            # Read after both sessions have pinned: if either had landed on
            # the other's connection, this is where it would show.
            seen[tenant] = (pid, await _setting(TENANT_KEY))

    await asyncio.wait_for(asyncio.gather(scoped(ACME), scoped(OTHER)), timeout=15)

    assert seen[ACME][1] == ACME
    assert seen[OTHER][1] == OTHER
    assert seen[ACME][0] != seen[OTHER][0], "two pinned sessions shared a connection"


# ---------------------------------------------------------------------------
# End to end: a real policy, a non-superuser role, and rows that stay fenced
# ---------------------------------------------------------------------------

_POLICY_EXPR = f"ledger = NULLIF(current_setting('{TENANT_KEY}', true), '')"


def _tenant_url(db_url: str, role: str) -> str:
    """The matrix's Postgres URL, re-pointed at the tenant login role."""
    parsed = urlparse(db_url)
    netloc = (
        f"{quote(role, safe='')}:{quote(TENANT_PASSWORD, safe='')}@{parsed.hostname}"
    )
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


@pytest.fixture
def tenant_role() -> str:
    """A cluster-unique role name; the test drops it in its own ``finally``."""
    return f"ferro_conn_delivery_{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_policy_fences_rows_under_connection_delivery(db_url, tenant_role):
    """The reason the feature exists, in the mode this issue adds.

    Superusers bypass row-level security unconditionally, so the enforcement
    has to run as a created ``NOSUPERUSER`` role — otherwise a green test
    proves nothing.
    """
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        schema = (await fetch_one("SELECT current_schema() AS s"))["s"]
        await execute("ALTER TABLE deliveryrow ENABLE ROW LEVEL SECURITY")
        await execute(
            "CREATE POLICY rls_deliveryrow_ledger ON deliveryrow FOR ALL "
            f"USING ({_POLICY_EXPR}) WITH CHECK ({_POLICY_EXPR})"
        )
        for ledger, label in ((ACME, "e2e-a1"), (ACME, "e2e-a2"), (OTHER, "e2e-b1")):
            await DeliveryRow.create(ledger=ledger, label=label)

        await execute(
            f'CREATE ROLE "{tenant_role}" LOGIN NOSUPERUSER '
            f"PASSWORD '{TENANT_PASSWORD}'"
        )

    try:
        async with engines.session():
            await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{tenant_role}"')
            await execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON deliveryrow "
                f'TO "{tenant_role}"'
            )
            await execute(
                f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{tenant_role}"'
            )

        await connect(
            _tenant_url(db_url, tenant_role),
            name="tenant",
            pool=PoolConfig(max_connections=2, settings_delivery="connection"),
        )

        async def labels() -> list[str]:
            rows = await DeliveryRow.where(lambda row: row.label != "").all()
            return sorted(row.label for row in rows)

        # No transaction() anywhere: the policy reads a value that is simply
        # on the connection.
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            assert await labels() == ["e2e-a1", "e2e-a2"]

        async with engines.session("tenant", settings={TENANT_KEY: OTHER}):
            assert await labels() == ["e2e-b1"]

        # Fail closed: no scope, no rows, no error (the NULLIF contract).
        async with engines.session("tenant"):
            assert await labels() == []
    finally:
        async with engines.session():
            await execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = $1 AND pid <> pg_backend_pid()",
                tenant_role,
            )
            await execute(f'DROP OWNED BY "{tenant_role}"')
            await execute(f'DROP ROLE IF EXISTS "{tenant_role}"')
