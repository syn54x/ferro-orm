"""Session settings delivered to operations that run outside ``transaction()``.

``tests/test_session_settings.py`` covers the values themselves and the
explicit-transaction delivery. This file covers the other half: a plain
``Model.where(...).all()``, ``create()``, or raw fetch, with no ``transaction()``
anywhere in sight, still runs tenant-scoped — because the operation wraps itself
in one (``BEGIN``, the ``set_config`` batch, its statements, ``COMMIT``).

The assertions run through a real row-security policy rather than through
``current_setting()``: a policy is what the feature exists for, and it is the
only assertion that fails if the setting arrives one statement too late. Which
means they also run as a created ``NOSUPERUSER`` login role connected as a
second Ferro connection — the matrix connects as a superuser, and superusers
bypass RLS unconditionally, so a green test as the matrix role would prove
nothing.
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
TAG_KEY = "myapp.session_tag"

ACME = "acme"
OTHER = "other"

TENANT_PASSWORD = "ferro-tenant-pw"


class ScopedLedgerRow(ferro.Model):
    """One row of tenant data, fenced by a policy on ``ledger``."""

    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    ledger: str
    label: str


# ---------------------------------------------------------------------------
# The policed table and the role that is actually subject to it
# ---------------------------------------------------------------------------

#: What a `RowSecurity(RowPolicy(column="ledger", setting=TENANT_KEY))`
#: declaration will emit once #409 lands. Hand-written here so this suite tests
#: delivery against a real policy without depending on that branch. `NULLIF` is
#: the fail-closed half: an unset GUC reads back as `''`, which must mean "no
#: rows", never an error.
_POLICY_EXPR = f"ledger = NULLIF(current_setting('{TENANT_KEY}', true), '')"


async def _create_policed_table() -> None:
    """Turn the model's table into a policed one (superuser connection)."""
    await execute("ALTER TABLE scopedledgerrow ENABLE ROW LEVEL SECURITY")
    await execute(
        "CREATE POLICY rls_scopedledgerrow_ledger ON scopedledgerrow FOR ALL "
        f"USING ({_POLICY_EXPR}) WITH CHECK ({_POLICY_EXPR})"
    )


def _tenant_url(db_url: str, role: str) -> str:
    """The matrix's Postgres URL, re-pointed at the tenant login role."""
    parsed = urlparse(db_url)
    netloc = (
        f"{quote(role, safe='')}:{quote(TENANT_PASSWORD, safe='')}@{parsed.hostname}"
    )
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _grant_tenant_role(role: str) -> None:
    """Create the NOSUPERUSER login role and give it exactly what it needs.

    Called INSIDE the ``try`` whose ``finally`` drops the role: roles are
    cluster-global while the test schema is not, so a GRANT that fails midway
    must still leave a role the teardown knows to remove.
    """
    schema = (await fetch_one("SELECT current_schema() AS s"))["s"]
    await execute(
        f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER PASSWORD '{TENANT_PASSWORD}'"
    )
    await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    await execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON scopedledgerrow TO "{role}"'
    )
    await execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"')


async def _drop_tenant_role(role: str) -> None:
    await execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE usename = $1 AND pid <> pg_backend_pid()",
        role,
    )
    await execute(f'DROP OWNED BY "{role}"')
    await execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.fixture
def tenant_role() -> str:
    """A cluster-unique role name; the tests drop it in their own ``finally``."""
    return f"ferro_op_scope_{uuid.uuid4().hex[:12]}"


async def _seed_and_open_tenant_connection(
    db_url: str, role: str, *, max_connections: int = 5
) -> None:
    """Create the policed table, seed both tenants, and register the
    ``"tenant"`` connection that runs as the policed role.

    Seeding happens on the matrix's superuser connection, which RLS does not
    apply to — so the rows exist regardless of scope, and every later assertion
    is about visibility rather than about what got written.
    """
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _create_policed_table()
        for ledger, label in ((ACME, "a1"), (ACME, "a2"), (OTHER, "b1")):
            await ScopedLedgerRow.create(ledger=ledger, label=label)
        await _grant_tenant_role(role)

    await connect(
        _tenant_url(db_url, role),
        name="tenant",
        pool=PoolConfig(max_connections=max_connections),
    )


async def _labels() -> list[str]:
    """Every label the current route can see, through the ORM."""
    rows = await ScopedLedgerRow.where(lambda row: row.label != "").all()
    return sorted(row.label for row in rows)


# ---------------------------------------------------------------------------
# The headline: no transaction() anywhere, and the query is still scoped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_plain_query_outside_transaction_sees_the_session_scope(
    db_url, tenant_role
):
    """User story 2, end to end.

    ``Model.where(...).all()`` with no ``transaction()`` around it. Before this
    slice the query ran on a bare pool connection where the GUC had never been
    set, and the policy returned zero rows.
    """
    await _seed_and_open_tenant_connection(db_url, tenant_role)
    try:
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            assert await _labels() == ["a1", "a2"]

        async with engines.session("tenant", settings={TENANT_KEY: OTHER}):
            assert await _labels() == ["b1"]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_every_operation_shape_is_scoped(db_url, tenant_role):
    """Not just `where().all()`: the wrap is opened by every operation, so
    `all()`, `get()`, `count()` and raw SQL are scoped alike."""
    await _seed_and_open_tenant_connection(db_url, tenant_role)
    try:
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            mine = await ScopedLedgerRow.where(lambda row: row.ledger == ACME).all()
            assert sorted(row.label for row in mine) == ["a1", "a2"]

            assert len(await ScopedLedgerRow.all()) == 2
            assert await ScopedLedgerRow.where(lambda row: row.label != "").count() == 2

            theirs = await ScopedLedgerRow.where(lambda row: row.label == "b1").first()
            assert theirs is None, "another tenant's row is invisible, not an error"

            raw = await fetch_all("SELECT label FROM scopedledgerrow ORDER BY label")
            assert [row["label"] for row in raw] == ["a1", "a2"]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_session_without_settings_fails_closed(db_url, tenant_role):
    """The other half of fail-closed: no scope, no rows — and no error.

    This is the `NULLIF` contract. An unwrapped query on a policed table reads
    the GUC as `''`, and `'' = ledger` matches nothing.
    """
    await _seed_and_open_tenant_connection(db_url, tenant_role)
    try:
        async with engines.session("tenant"):
            assert await _labels() == []
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


# ---------------------------------------------------------------------------
# Writes: the wrap commits on success and rolls back on failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_write_outside_transaction_is_scoped_and_committed(db_url, tenant_role):
    """A `create()` outside `transaction()` passes the policy's WITH CHECK and
    stays written after the operation's own COMMIT."""
    await _seed_and_open_tenant_connection(db_url, tenant_role)
    try:
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            await ScopedLedgerRow.create(ledger=ACME, label="a3")

        # A fresh session, a fresh connection: the row survived the wrap.
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            assert await _labels() == ["a1", "a2", "a3"]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_failed_operation_leaves_nothing_behind(db_url, tenant_role):
    """An out-of-scope insert trips the policy's WITH CHECK, and the wrap that
    delivered the scope is what rolls the failure back.

    The session stays usable afterwards, which is the part that would break if
    the wrap's connection went back to the pool still in a transaction.
    """
    await _seed_and_open_tenant_connection(db_url, tenant_role)
    try:
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            with pytest.raises(Exception) as excinfo:
                await ScopedLedgerRow.create(ledger=OTHER, label="stolen")
            assert "policy" in str(excinfo.value).lower()

            assert await _labels() == ["a1", "a2"]
            await ScopedLedgerRow.create(ledger=ACME, label="a3")
            assert await _labels() == ["a1", "a2", "a3"]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


# ---------------------------------------------------------------------------
# Two sessions, one small pool, no bleed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_interleaved_sessions_never_observe_each_others_values(
    db_url, tenant_role
):
    """Two tasks, two tenants, a two-connection pool — so their operations are
    guaranteed to share connections — stepping strictly alternately.

    Each round asserts through the policy (rows) and through the GUC (a second,
    per-session key), so neither a stale value nor a missing one can hide.
    """
    await _seed_and_open_tenant_connection(db_url, tenant_role, max_connections=2)

    rounds = 8

    async def tenant_task(
        ledger: str, tag: str, mine: asyncio.Event, theirs: asyncio.Event
    ):
        async with engines.session(
            "tenant", settings={TENANT_KEY: ledger, TAG_KEY: tag}
        ):
            for _ in range(rounds):
                await mine.wait()
                mine.clear()
                try:
                    rows = await ScopedLedgerRow.all()
                    assert {row.ledger for row in rows} == {ledger}
                    seen = await fetch_one(
                        "SELECT current_setting($1, true) AS v", TAG_KEY
                    )
                    assert seen["v"] == tag
                finally:
                    theirs.set()

    first, second = asyncio.Event(), asyncio.Event()
    first.set()
    try:
        await asyncio.wait_for(
            asyncio.gather(
                tenant_task(ACME, "task-a", first, second),
                tenant_task(OTHER, "task-b", second, first),
            ),
            timeout=30,
        )
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_recycled_connection_carries_no_stale_scope(db_url, tenant_role):
    """`SET LOCAL` makes this trivial, and this is the test that proves it.

    A scoped session runs enough operations to have touched every connection in
    a one-connection pool, closes, and the next settings-less session gets those
    same connections back: zero rows through the policy, not somebody else's.
    """
    await _seed_and_open_tenant_connection(db_url, tenant_role, max_connections=1)
    try:
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            for _ in range(3):
                assert await _labels() == ["a1", "a2"]

        async with engines.session("tenant"):
            for _ in range(3):
                assert await _labels() == [], "the pooled connection kept the scope"
            stale = await fetch_one("SELECT current_setting($1, true) AS v", TENANT_KEY)
            assert (stale["v"] or "") == ""

        # And a *different* tenant on the same recycled connection sees only
        # its own rows.
        async with engines.session("tenant", settings={TENANT_KEY: OTHER}):
            assert await _labels() == ["b1"]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


# ---------------------------------------------------------------------------
# The control: a session with no settings sends nothing extra.
# ---------------------------------------------------------------------------


class ScopeControlRow(ferro.Model):
    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    label: str


async def _add_tenant_default_column() -> None:
    """A column whose DEFAULT records what Postgres could see at write time.

    Any operation that ran inside a settings wrap writes the tenant; any
    operation that ran unwrapped writes NULL. That is the difference between
    the two paths, made visible without reading a single internal.
    """
    await execute(
        "ALTER TABLE scopecontrolrow ADD COLUMN tenant text "
        f"DEFAULT current_setting('{TENANT_KEY}', true)"
    )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_settings_bearing_session_wraps_a_single_statement_write(db_url):
    """The single-statement case, which nothing wrapped before this slice."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _add_tenant_default_column()

    async with engines.session(settings={TENANT_KEY: ACME}):
        await ScopeControlRow.create(label="one")

    async with engines.session():
        rows = await fetch_all("SELECT tenant FROM scopecontrolrow")
        assert [row["tenant"] for row in rows] == [ACME]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_settings_less_session_opens_no_wrap(db_url):
    """Zero settings, zero machinery: the same write runs exactly as it did
    before — one statement, no transaction, nothing to apply."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _add_tenant_default_column()
        await ScopeControlRow.create(label="one")

        rows = await fetch_all("SELECT tenant FROM scopecontrolrow")
        assert [row["tenant"] for row in rows] == [None]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_sessionless_operation_opens_no_wrap(db_url):
    """No session at all — a raw operation routed by `using=` — is untouched.

    There is no session to read settings from, so the gate is false before it
    ever looks at a backend.
    """
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _add_tenant_default_column()

    await execute("INSERT INTO scopecontrolrow (label) VALUES ('one')", using="default")

    rows = await fetch_all("SELECT tenant FROM scopecontrolrow", using="default")
    assert [row["tenant"] for row in rows] == [None]


# ---------------------------------------------------------------------------
# Multi-statement atomicity inside one wrap.
# ---------------------------------------------------------------------------

# `bulk_create` is the only operation that issues several *data* statements
# today (it chunks so no statement exceeds the backend's bind budget, #298), so
# it is where a wrap that covered one statement instead of the whole operation
# would show. 5 rendered columns × 13,200 rows = 66,000 binds, past Postgres'
# 65,535 ceiling, which is what forces the second chunk.
STRADDLE_ROWS = 13_200


class ScopedBatchItem(ferro.Model):
    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    label: str
    quantity: int
    price: float
    is_active: bool
    note: str


def _batch(n: int, *, duplicate_label_at: int | None = None) -> list[ScopedBatchItem]:
    items = [
        ScopedBatchItem(
            label=f"item-{i}",
            quantity=i,
            price=i * 0.5,
            is_active=i % 2 == 0,
            note=f"note-{i}",
        )
        for i in range(n)
    ]
    if duplicate_label_at is not None:
        items[duplicate_label_at].label = items[0].label
    return items


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_chunked_batch_that_fails_late_writes_nothing(db_url):
    """The wrap covers the whole operation, not one statement of it.

    The first chunk inserts cleanly; a duplicate label in the second chunk
    trips a unique index. If each chunk had been wrapped on its own, the first
    13,107 rows would have survived the failure.
    """
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute(
            "CREATE UNIQUE INDEX uq_scopedbatchitem_label ON scopedbatchitem (label)"
        )

    async with engines.session(settings={TENANT_KEY: ACME}):
        with pytest.raises(Exception) as excinfo:
            await ScopedBatchItem.bulk_create(
                _batch(STRADDLE_ROWS, duplicate_label_at=STRADDLE_ROWS - 1)
            )
        assert "bulk save failed" in str(excinfo.value).lower()

        assert await ScopedBatchItem.where(lambda item: item.quantity >= 0).count() == 0


# ---------------------------------------------------------------------------
# Multi-database nesting: inherited settings are inert, never an error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_operations_on_an_inherited_sqlite_session_run_unwrapped(
    db_url, tmp_path
):
    """A tenant-scoped request reaching a SQLite side database.

    The nested session inherits the scope (a Postgres session below it still
    needs it) but SQLite has no GUCs, so its operations run on the ordinary
    unwrapped path rather than raising or wrapping.
    """
    await connect(db_url, name="pg", default=True, auto_migrate=True)
    await connect(f"sqlite:{tmp_path / 'aux.db'}?mode=rwc", name="aux")

    async with engines.session("pg", settings={TENANT_KEY: ACME}):
        async with engines.session("aux") as aux:
            assert aux.effective_settings == {TENANT_KEY: ACME}

            # No transaction(): these are exactly the operations #410 wraps on
            # Postgres, and they must stay unwrapped here.
            await execute(
                "CREATE TABLE aux_marker (id INTEGER PRIMARY KEY, label TEXT)"
            )
            await execute("INSERT INTO aux_marker (id, label) VALUES (1, 'ok')")
            assert await fetch_all("SELECT label FROM aux_marker") == [{"label": "ok"}]


# ---------------------------------------------------------------------------
# Cancellation: the wrap's connection is never handed to the next request.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_cancelled_operation_does_not_poison_the_pool(db_url, tenant_role):
    """The leak this feature could have introduced, and does not.

    Cancel a wrapped operation mid-statement — a client disconnecting, a
    `wait_for` timing out — and its connection is still inside `BEGIN` with the
    tenant's `SET LOCAL` scope live on it. Handed back to the pool, the next
    request would run inside that transaction and under that scope. So the
    connection is discarded instead and the pool opens a fresh one.

    A one-connection pool is the whole point: the next session has no choice
    but to use whatever came back.
    """
    await _seed_and_open_tenant_connection(db_url, tenant_role, max_connections=1)
    try:
        async with engines.session("tenant", settings={TENANT_KEY: ACME}):
            with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
                await asyncio.wait_for(fetch_all("SELECT pg_sleep(5)"), timeout=0.2)

        # The next request gets a connection that is not mid-transaction, not
        # scoped to the cancelled tenant, and answers normally.
        async with engines.session("tenant"):
            stale = await fetch_one("SELECT current_setting($1, true) AS v", TENANT_KEY)
            assert (stale["v"] or "") == "", "the cancelled request's scope survived"
            assert await _labels() == []

        async with engines.session("tenant", settings={TENANT_KEY: OTHER}):
            assert await _labels() == ["b1"]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


# ---------------------------------------------------------------------------
# autocommit: the statements Postgres refuses to run inside a transaction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_autocommit_runs_a_statement_that_cannot_be_wrapped(db_url):
    """`CREATE INDEX CONCURRENTLY` is illegal inside a transaction block.

    Without the opt-out, adding settings to a session would break this call
    with a 25001 — code that works today, for exactly this feature's users.
    """
    await connect(db_url, auto_migrate=True)

    async with engines.session(settings={TENANT_KEY: ACME}):
        # The wrap is what it cannot survive.
        with pytest.raises(Exception) as excinfo:
            await execute(
                "CREATE INDEX CONCURRENTLY idx_scopecontrolrow_wrapped "
                "ON scopecontrolrow (label)"
            )
        assert "transaction" in str(excinfo.value).lower()

        # Opted out, it runs.
        await execute(
            "CREATE INDEX CONCURRENTLY idx_scopecontrolrow_label "
            "ON scopecontrolrow (label)",
            autocommit=True,
        )

    async with engines.session():
        indexes = await fetch_all(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'scopecontrolrow'"
        )
        assert "idx_scopecontrolrow_label" in {row["indexname"] for row in indexes}


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_an_autocommit_statement_is_not_tenant_scoped(db_url):
    """Pinning the docstring's claim rather than asking anyone to trust it.

    `autocommit=True` skips the wrap, and the wrap is what carries the
    settings — so the row this writes records no tenant. That is the trade, and
    it is why the opt-out is documented for maintenance DDL and not for data.
    """
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _add_tenant_default_column()

    async with engines.session(settings={TENANT_KEY: ACME}):
        await execute("INSERT INTO scopecontrolrow (label) VALUES ('wrapped')")
        await execute(
            "INSERT INTO scopecontrolrow (label) VALUES ('autocommit')",
            autocommit=True,
        )

        rows = await fetch_all(
            "SELECT label, tenant FROM scopecontrolrow ORDER BY label"
        )
        assert {row["label"]: row["tenant"] for row in rows} == {
            "autocommit": None,
            "wrapped": ACME,
        }
