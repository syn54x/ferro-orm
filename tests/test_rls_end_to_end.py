"""End-to-end proof of the Postgres RLS pair (#415, PRD #406).

Every other suite in this feature proves one half in isolation:

* ``test_session_settings*.py`` proves session settings reach every operation
  a settings-bearing session runs, against hand-written policies.
* ``test_row_security_*.py`` proves ``__ferro_rls__`` declarations emit and
  reconcile the right catalog objects, enforced through ``SET LOCAL ROLE`` on
  the matrix's own connection.

This file is the pair, proven together, exactly the way an application uses
them: a model declares ``__ferro_rls__``, ``auto_migrate`` creates it policed,
and a **separate** NOSUPERUSER login-role connection scopes its queries with
nothing but ``engines.session(settings=...)`` — no ``transaction()``, no
``SET LOCAL ROLE``, no raw policy SQL. If this file is green, the two halves
that ship as one PRD actually compose.

Every enforcement assertion below runs against that second, real NOSUPERUSER
connection: the matrix itself connects as a superuser, and superusers bypass
row-level security unconditionally (``FORCE`` included), so a green test as
the matrix role would prove nothing.
"""

import uuid
from typing import Annotated, ClassVar
from urllib.parse import quote, urlparse, urlunparse

import pytest

import ferro
from ferro import PoolConfig, RowPolicy, RowSecurity, connect, engines
from ferro.raw import execute, fetch_one

LEDGER_KEY = "pinch.ledger_id"
MEMBER_KEY = "pinch.member"

LEDGER_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
LEDGER_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

TENANT_PASSWORD = "ferro-e2e-tenant-pw"


def _rewind_registry() -> None:
    """Drop every registered model so each test declares exactly the models
    it needs — a policed model's policy can reference a table (``membership``)
    that only one test creates, so models must not leak across tests."""
    from ferro.registry import REGISTRY

    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()


@pytest.fixture(autouse=True)
def cleanup_registry():
    _rewind_registry()
    yield
    _rewind_registry()


# ---------------------------------------------------------------------------
# The policed model: exactly the shorthand declaration from the docs.
# ---------------------------------------------------------------------------


def _define_tenant_invoice() -> type[ferro.Model]:
    class TenantInvoice(ferro.Model):
        """A model that declares its own row security — no hand-written SQL."""

        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        ledger_id: uuid.UUID
        amount: int

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting=LEDGER_KEY)
        )

    return TenantInvoice


# ---------------------------------------------------------------------------
# The shareability shape: owner ALL + restrictive tenant fence + invitee read.
# ---------------------------------------------------------------------------


def _define_shared_doc() -> type[ferro.Model]:
    class SharedDoc(ferro.Model):
        """PRD #406's shareability shape: an owner writes, a member reads,
        and neither can see across ledgers no matter what their other
        settings say."""

        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        ledger_id: uuid.UUID
        owner: str
        title: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(
                name="tenant",
                column="ledger_id",
                setting=LEDGER_KEY,
                restrictive=True,
            ),
            RowPolicy(
                name="owner_all",
                using=(
                    f"\"owner\" = NULLIF(current_setting('{MEMBER_KEY}', true), '')"
                ),
                with_check=(
                    f"\"owner\" = NULLIF(current_setting('{MEMBER_KEY}', true), '')"
                ),
            ),
            RowPolicy(
                name="invitee_read",
                command="select",
                using=(
                    '"id" IN (SELECT doc_id FROM membership WHERE member = '
                    f"NULLIF(current_setting('{MEMBER_KEY}', true), ''))"
                ),
            ),
        )

    return SharedDoc


# ---------------------------------------------------------------------------
# A real NOSUPERUSER login role on its own connection — the public API's
# delivery surface, not a same-connection `SET LOCAL ROLE` shortcut.
# ---------------------------------------------------------------------------


def _tenant_url(db_url: str, role: str) -> str:
    parsed = urlparse(db_url)
    netloc = (
        f"{quote(role, safe='')}:{quote(TENANT_PASSWORD, safe='')}@{parsed.hostname}"
    )
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _grant_tenant_role(role: str, tables: tuple[str, ...]) -> None:
    """Create the NOSUPERUSER login role and grant exactly what it needs.

    Called INSIDE the ``try`` whose ``finally`` drops the role: roles are
    cluster-global while the test schema is not, so a GRANT that fails midway
    must still leave a role the teardown knows to remove.
    """
    schema = (await fetch_one("SELECT current_schema() AS s"))["s"]
    await execute(
        f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER PASSWORD '{TENANT_PASSWORD}'"
    )
    await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    for table in tables:
        await execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO "{role}"')
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
    return f"ferro_e2e_{uuid.uuid4().hex[:12]}"


async def _seed_invoice_ledgers(
    db_url: str,
    role: str,
    invoice_model: type[ferro.Model],
    *,
    delivery: str,
    max_connections: int = 3,
) -> None:
    """Declare, migrate (policed table created by the create pass), seed both
    ledgers as the superuser matrix connection, and register the tenant
    NOSUPERUSER connection with the requested settings delivery."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await invoice_model.create(ledger_id=LEDGER_A, amount=100)
        await invoice_model.create(ledger_id=LEDGER_A, amount=200)
        await invoice_model.create(ledger_id=LEDGER_B, amount=999)
        await _grant_tenant_role(role, ("tenantinvoice",))

    await connect(
        _tenant_url(db_url, role),
        name="tenant",
        pool=PoolConfig(max_connections=max_connections, settings_delivery=delivery),
    )


async def _amounts(invoice_model: type[ferro.Model]) -> list[int]:
    """Every amount visible to the current ambient (tenant) session, through
    a plain ORM query with no `transaction()` in sight."""
    rows = await invoice_model.where(lambda invoice: invoice.amount > 0).all()
    return sorted(row.amount for row in rows)


# ---------------------------------------------------------------------------
# 1. The pair, end to end, for both settings-delivery modes.
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["transaction", "connection"])
async def test_declared_policy_scopes_plain_queries_and_rejects_cross_tenant_writes(
    db_url, tenant_role, delivery
):
    """Declaration -> migration -> scoped queries -> WITH CHECK, as a real
    NOSUPERUSER connection, under both settings-delivery modes.

    Nothing here opens a `transaction()`: the point is that the declared
    policy and the session's settings compose through the plain ORM surface
    the way an application actually uses them.
    """
    TenantInvoice = _define_tenant_invoice()
    await _seed_invoice_ledgers(db_url, tenant_role, TenantInvoice, delivery=delivery)
    try:
        async with engines.session("tenant", settings={LEDGER_KEY: str(LEDGER_A)}):
            assert await _amounts(TenantInvoice) == [100, 200]

            # WITH CHECK: a write outside the session's own ledger is rejected
            # by the very same declaration, not by application code.
            with pytest.raises(Exception) as excinfo:
                await TenantInvoice.create(ledger_id=LEDGER_B, amount=1)
            assert "policy" in str(excinfo.value).lower()

            # The session recovers and keeps working after the failed write.
            await TenantInvoice.create(ledger_id=LEDGER_A, amount=300)
            assert await _amounts(TenantInvoice) == [100, 200, 300]

        async with engines.session("tenant", settings={LEDGER_KEY: str(LEDGER_B)}):
            assert await _amounts(TenantInvoice) == [999]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


# ---------------------------------------------------------------------------
# 2. Fail-closed: no scope at all, and scope that was set then RESET.
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_fail_closed_session_returns_zero_rows_with_no_error(
    db_url, tenant_role
):
    """The NULLIF contract, both halves.

    A session that never declared settings reads the GUC as `NULL`. A session
    that pinned a connection (`connection` delivery), set the value, and
    closed leaves that connection's GUC as `''` (Postgres' `RESET` behaviour,
    not `NULL`) for whichever session the pool hands the connection to next.
    Both must mean "zero rows", never a cast error — a one-connection pool
    makes the second case a provable connection reuse rather than a
    coincidence.
    """
    TenantInvoice = _define_tenant_invoice()
    # A one-connection pool makes the reuse below provable rather than
    # coincidental: there is only one backend to hand back.
    await _seed_invoice_ledgers(
        db_url, tenant_role, TenantInvoice, delivery="connection", max_connections=1
    )
    try:
        # Never set: current_setting reads NULL.
        async with engines.session("tenant"):
            assert await _amounts(TenantInvoice) == []

        async with engines.session("tenant", settings={LEDGER_KEY: str(LEDGER_A)}):
            assert await _amounts(TenantInvoice) == [100, 200]
            pinned_pid = (await fetch_one("SELECT pg_backend_pid() AS pid"))["pid"]

        # Set, then RESET (session close's targeted reset): '' on the wire,
        # not NULL — and still zero rows, never `''::uuid` erroring out.
        async with engines.session("tenant"):
            reused_pid = (await fetch_one("SELECT pg_backend_pid() AS pid"))["pid"]
            assert reused_pid == pinned_pid, (
                "the one-connection pool must have handed back the same backend"
            )
            stale = await fetch_one("SELECT current_setting($1, true) AS v", LEDGER_KEY)
            assert stale["v"] == "", "close must RESET, not leave NULL, on this GUC"
            assert await _amounts(TenantInvoice) == []
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


# ---------------------------------------------------------------------------
# 3. Deferred resolution: no settings at open, scoped mid-request.
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_deferred_set_config_scopes_subsequent_plain_queries(
    db_url, tenant_role
):
    """The auth-chain shape: the tenant isn't known until partway through the
    request, so the session opens bare and `current_session().set_config`
    supplies the scope once it resolves."""
    TenantInvoice = _define_tenant_invoice()
    await _seed_invoice_ledgers(
        db_url, tenant_role, TenantInvoice, delivery="transaction"
    )

    async def resolve_tenant_and_scope(ledger: uuid.UUID) -> None:
        await ferro.current_session().set_config(LEDGER_KEY, str(ledger))

    try:
        async with engines.session("tenant") as session:
            assert session.effective_settings == {}
            assert await _amounts(TenantInvoice) == [], "no scope yet: nothing visible"

            await resolve_tenant_and_scope(LEDGER_A)

            assert await _amounts(TenantInvoice) == [100, 200]
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)


# ---------------------------------------------------------------------------
# 4. Multi-policy composition: owner ALL + restrictive tenant fence.
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_owner_and_restrictive_tenant_fence_compose_end_to_end(
    db_url, tenant_role
):
    """PRD #406's shareability shape, declared and enforced end to end:

    * ``tenant`` is RESTRICTIVE, so it AND-composes with everything else — no
      ledger scope, no rows, whoever you are.
    * ``owner_all`` is permissive and unscoped by command: the owner reads
      and writes their own doc.
    * ``invitee_read`` is permissive and SELECT-only: an invited member reads
      the doc and nothing more.
    """
    SharedDoc = _define_shared_doc()
    await connect(db_url)
    async with engines.session():
        # The membership subquery reads a table ferro does not own; it must
        # exist before the CREATE POLICY that references it.
        await execute("CREATE TABLE membership (doc_id INTEGER, member TEXT)")
        await ferro.create_tables()

        doc = await SharedDoc.create(ledger_id=LEDGER_A, owner="alice", title="plan")
        other_ledger_doc = await SharedDoc.create(
            ledger_id=LEDGER_B, owner="alice", title="other-ledger-plan"
        )
        await execute(
            "INSERT INTO membership (doc_id, member) VALUES ($1, $2)", doc.id, "bob"
        )
        await _grant_tenant_role(tenant_role, ("shareddoc",))
        await execute(f'GRANT SELECT ON membership TO "{tenant_role}"')

    await connect(
        _tenant_url(db_url, tenant_role),
        name="tenant",
        pool=PoolConfig(max_connections=3),
    )

    async def titles() -> list[str]:
        rows = await SharedDoc.where(lambda d: d.title != "").all()
        return sorted(row.title for row in rows)

    try:
        # Owner, in scope: reads and writes through the plain ORM surface.
        async with engines.session(
            "tenant", settings={LEDGER_KEY: str(LEDGER_A), MEMBER_KEY: "alice"}
        ):
            assert await titles() == ["plan"]
            found = await SharedDoc.get(doc.id)
            found.title = "renamed"
            await found.save()
            assert await titles() == ["renamed"]

        # Invitee: SELECT only. No permissive policy covers UPDATE, so the
        # row is invisible to the update — zero rows affected, which ferro's
        # own `save()` surfaces as "no such record", not a silent no-op.
        async with engines.session(
            "tenant", settings={LEDGER_KEY: str(LEDGER_A), MEMBER_KEY: "bob"}
        ):
            assert await titles() == ["renamed"]
            hijacked = await SharedDoc.where(lambda d: d.title == "renamed").all()
            for row in hijacked:
                row.title = "hijacked"
                with pytest.raises(ferro.exceptions.ModelDoesNotExist):
                    await row.save()

        async with engines.session():
            row = await fetch_one("SELECT title FROM shareddoc WHERE id = $1", doc.id)
            assert row["title"] == "renamed", (
                "an invitee's write must not have touched the row"
            )

        # Wrong ledger: the RESTRICTIVE tenant fence AND-composes, so even the
        # owner's permissive policy cannot bring the row back — even though
        # `owner` still matches.
        async with engines.session(
            "tenant", settings={LEDGER_KEY: str(LEDGER_A), MEMBER_KEY: "alice"}
        ):
            assert other_ledger_doc.id not in {row.id for row in await SharedDoc.all()}
    finally:
        async with engines.session():
            await _drop_tenant_role(tenant_role)
