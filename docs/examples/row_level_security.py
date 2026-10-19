"""Runnable companion for the row-level security pages.

Embedded by docs/pages/guide/row-level-security.md,
docs/pages/concepts/row-level-security.md and
docs/pages/reference/row-level-security.md.

Row-level security is a Postgres feature, so the pages' demo needs Postgres:

    python docs/examples/row_level_security.py <postgres-url> <app-role>

connects to ``<postgres-url>`` as the migrating role (a superuser, or a role
with BYPASSRLS), creates the tables, creates ``<app-role>`` as the
application's NOSUPERUSER login role, and checks what each session sees.
``tests/test_docs_examples.py`` runs it that way when a Postgres server is
configured. With no arguments it runs on SQLite, where Ferro creates the
tables without their policies and warns.
"""

import asyncio
import sys
import uuid
import warnings
from typing import ClassVar
from urllib.parse import quote, urlparse, urlunparse

import ferro
from ferro import Field, Model, RowPolicy, RowSecurity, connect, engines
from ferro.exceptions import OperationalError
from ferro.raw import execute

ACME = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
GLOBEX = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
APP_PASSWORD = "app-password"


# --8<-- [start:models]
class Invoice(Model):
    id: int | None = Field(default=None, primary_key=True)
    tenant_id: uuid.UUID
    customer: str
    total: int

    __ferro_rls__: ClassVar = RowSecurity(
        RowPolicy(column="tenant_id", setting="app.tenant_id")
    )


# --8<-- [end:models]


# --8<-- [start:composed]
class Document(Model):
    id: int | None = Field(default=None, primary_key=True)
    tenant_id: uuid.UUID
    owner: str
    title: str
    published: bool = False

    __ferro_rls__: ClassVar = RowSecurity(
        # Restrictive: every other policy applies only inside the tenant.
        RowPolicy(
            name="tenant",
            column="tenant_id",
            setting="app.tenant_id",
            restrictive=True,
        ),
        # The owner reads and writes their own documents.
        RowPolicy(
            name="owner_all",
            using="\"owner\" = NULLIF(current_setting('app.user', true), '')",
            with_check="\"owner\" = NULLIF(current_setting('app.user', true), '')",
        ),
        # Everyone in the tenant reads a published document.
        RowPolicy(name="published_read", command="select", using='"published"'),
    )


# --8<-- [end:composed]


def app_url_for(migrator_url: str, role: str) -> str:
    """The same database as ``migrator_url``, logged in as ``role``."""
    parsed = urlparse(migrator_url)
    netloc = f"{quote(role, safe='')}:{quote(APP_PASSWORD, safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


async def create_app_role(role: str) -> None:
    """Create the application's login role, which row security applies to."""
    async with engines.session():
        schema = (await ferro.raw.fetch_one("SELECT current_schema() AS name"))["name"]
        await execute(
            f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER PASSWORD '{APP_PASSWORD}'"
        )
        await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        for table in ("invoice", "document"):
            await execute(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO "{role}"'
            )
        await execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"')


async def on_postgres(migrator_url: str, role: str) -> None:
    # --8<-- [start:connect_migrator]
    await connect(migrator_url, auto_migrate=True)
    # --8<-- [end:connect_migrator]
    await create_app_role(role)
    app_url = app_url_for(migrator_url, role)

    # --8<-- [start:connect_app]
    await connect(app_url, name="app")
    # --8<-- [end:connect_app]

    # --8<-- [start:seed]
    async with engines.session():
        await Invoice.create(tenant_id=ACME, customer="Initech", total=450)
        await Invoice.create(tenant_id=ACME, customer="Hooli", total=120)
        await Invoice.create(tenant_id=GLOBEX, customer="Vandelay", total=300)
        await Invoice.create(tenant_id=GLOBEX, customer="Pied Piper", total=980)
    # --8<-- [end:seed]

    # --8<-- [start:scoped]
    async with engines.session("app", settings={"app.tenant_id": str(ACME)}):
        invoices = await Invoice.select().order_by(lambda invoice: invoice.total).all()
        assert [(invoice.customer, invoice.total) for invoice in invoices] == [
            ("Hooli", 120),
            ("Initech", 450),
        ]
    # --8<-- [end:scoped]

    # --8<-- [start:unscoped]
    async with engines.session("app"):
        assert await Invoice.all() == []
    # --8<-- [end:unscoped]

    # --8<-- [start:migrator]
    async with engines.session():
        assert await Invoice.select().count() == 4
    # --8<-- [end:migrator]

    # --8<-- [start:write_check]
    async with engines.session("app", settings={"app.tenant_id": str(ACME)}):
        try:
            await Invoice.create(tenant_id=GLOBEX, customer="Initech", total=75)
        except OperationalError as error:
            assert error.sqlstate == "42501"
        else:
            raise AssertionError("a write into another tenant must be refused")
    # --8<-- [end:write_check]

    # --8<-- [start:deferred]
    async def scope_to_tenant(tenant_id: uuid.UUID) -> None:
        session = ferro.current_session()
        assert session is not None
        await session.set_config("app.tenant_id", str(tenant_id))

    async with engines.session("app"):
        assert await Invoice.all() == []
        await scope_to_tenant(GLOBEX)
        invoices = await Invoice.select().order_by(lambda invoice: invoice.total).all()
        assert [(invoice.customer, invoice.total) for invoice in invoices] == [
            ("Vandelay", 300),
            ("Pied Piper", 980),
        ]
    # --8<-- [end:deferred]

    # --8<-- [start:nested]
    acme = str(ACME)
    async with engines.session(
        "app", settings={"app.tenant_id": acme, "app.user": "ada"}
    ) as outer:
        async with engines.session("app", settings={"app.user": "grace"}) as inner:
            assert inner.effective_settings == {"app.tenant_id": acme, "app.user": "grace"}
        assert outer.effective_settings == {"app.tenant_id": acme, "app.user": "ada"}
    # --8<-- [end:nested]

    async with engines.session():
        await Document.create(tenant_id=ACME, owner="ada", title="Q3 plan")
        await Document.create(
            tenant_id=ACME, owner="grace", title="Handbook", published=True
        )
        await Document.create(tenant_id=ACME, owner="grace", title="Grace's draft")
        await Document.create(
            tenant_id=GLOBEX, owner="ada", title="Globex roadmap", published=True
        )

    # --8<-- [start:composed_query]
    async with engines.session(
        "app", settings={"app.tenant_id": str(ACME), "app.user": "ada"}
    ):
        documents = await Document.select().order_by(
            lambda document: document.title
        ).all()
        assert [document.title for document in documents] == ["Handbook", "Q3 plan"]
    # --8<-- [end:composed_query]


async def on_sqlite() -> None:
    # --8<-- [start:sqlite]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await connect("sqlite::memory:", auto_migrate=True)
    print(next(str(w.message) for w in caught if "'invoice'" in str(w.message)))
    # ferro auto-migrate: Table 'invoice' declares __ferro_rls__, but row-level
    # security is a PostgreSQL-only feature: the table is created without its
    # policies and rows are NOT filtered on SQLite. Run against PostgreSQL for
    # enforcement.
    # --8<-- [end:sqlite]
    skipped = [str(w.message) for w in caught if "PostgreSQL-only" in str(w.message)]
    assert len(skipped) == 2, skipped

    # --8<-- [start:sqlite_settings]
    try:
        async with engines.session(settings={"app.tenant_id": str(ACME)}):
            pass
    except RuntimeError as error:
        assert "Session settings require a Postgres connection" in str(error)
    else:
        raise AssertionError("settings= on SQLite must be refused")
    # --8<-- [end:sqlite_settings]

    async with engines.session():
        await Invoice.create(tenant_id=ACME, customer="Initech", total=450)
        await Invoice.create(tenant_id=GLOBEX, customer="Vandelay", total=300)
        assert await Invoice.select().count() == 2


async def main(argv: list[str]) -> None:
    if argv:
        migrator_url, role = argv
        await on_postgres(migrator_url, role)
    else:
        await on_sqlite()
    print("row_level_security example ran successfully")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
