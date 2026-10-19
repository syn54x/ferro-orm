---
description: "Set up Postgres row-level security so each request reads and writes only its own tenant's rows."
---

# Scope rows by tenant with Postgres row-level security

This page sets up Postgres row-level security so that each request reads and writes only its own tenant's rows, with no `where` filter in your code.

Once it is in place, a session that carries a tenant id sees that tenant's invoices and no others:

```python
--8<-- "docs/examples/row_level_security.py:scoped"
```

`ACME` and `GLOBEX` are the two tenants' ids, and each tenant has two invoices. The query has no `where` clause: Postgres applied the table's policy to every row, and Globex's invoices never left the database.

The steps below build that example: declare the policy on the model, give the application a role the policy applies to, open each session with the tenant's id, and deploy the two halves in the right order. The Python examples, apart from the web framework recipes, run against Postgres in the docs test suite. Row-level security is a Postgres feature, and on SQLite Ferro creates the tables without policies and warns (see [SQLite](../reference/row-level-security.md#sqlite)).

## The tenant policy

Add a `RowSecurity` to the model, on the `__ferro_rls__` class variable. `RowPolicy(column=..., setting=...)` compares a column with a session setting:

=== "Assignment"

    ```python
    --8<-- "docs/examples/row_level_security.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/row_level_security_annotated.py:models"
    ```

The policy takes its name from the column, `rls_invoice_tenant_id`, and its cast from the column's type, `uuid`.

`auto_migrate`, `ferro migrate new` and the Alembic bridge all create the table with row-level security switched on and the policy in place:

```sql
ALTER TABLE "invoice" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "invoice" FORCE ROW LEVEL SECURITY;

CREATE POLICY "rls_invoice_tenant_id" ON "invoice" FOR ALL USING ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
```

`USING` decides which rows a query sees and `WITH CHECK` decides which rows it may write; both compare `tenant_id` with the connection's `app.tenant_id` setting. [Policy expressions](../concepts/row-level-security.md#policy-expressions) explains the `NULLIF`, and the [reference](../reference/row-level-security.md#rowpolicy) lists every `RowPolicy` option, including policies you write in SQL.

## The application's role

Postgres never applies a policy to a superuser or to a role with `BYPASSRLS`, so the application must connect as a role that has neither. The migrating role is the opposite: it needs to see every row, so it keeps `BYPASSRLS` (or is the superuser). Create the application's role once, as an administrator:

```sql
CREATE ROLE invoice_app LOGIN NOSUPERUSER PASSWORD 'change-me';
GRANT USAGE ON SCHEMA public TO invoice_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON invoice TO invoice_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO invoice_app;
ALTER ROLE migrator BYPASSRLS;
```

`invoice_app` can read and write `invoice`, and every statement it sends passes through the policy. The docs example runs the first four statements with its own role and schema names; it migrates as a superuser, which needs no `BYPASSRLS`.

Then connect twice. The migrating role creates the tables on the default connection, and the application's role gets a [named connection](connections.md#named-connections), `"app"`:

```python
--8<-- "docs/examples/row_level_security.py:connect_migrator"
--8<-- "docs/examples/row_level_security.py:connect_app"
```

Every session opened on `"app"` is filtered by the policy, and sessions on the default connection are not. `migrator_url` and `app_url` are the two roles' database URLs.

## Tenant-scoped sessions

Pass the tenant's id to `engines.session(settings=...)`, as the first example on this page does. The examples below run on the four invoices the migrating role added, two for each tenant:

```python
--8<-- "docs/examples/row_level_security.py:seed"
```

The migrating role bypasses the policy, so it can write rows for any tenant.

A session on `"app"` that carries no setting sees nothing:

```python
--8<-- "docs/examples/row_level_security.py:unscoped"
```

The policy compares `tenant_id` with an unset setting, which matches no row. A request that forgets to scope its session gets an empty result, never another tenant's rows.

The migrating role's session, on the default connection, sees all four:

```python
--8<-- "docs/examples/row_level_security.py:migrator"
```

Its role bypasses the policy, so the count covers both tenants.

The same policy refuses a write into another tenant:

```python
--8<-- "docs/examples/row_level_security.py:write_check"
```

Postgres rejects the insert with SQLSTATE `42501`, which Ferro raises as `OperationalError`, and the session keeps working afterwards.

Setting keys are dotted names such as `app.tenant_id`, and values are strings, so convert a UUID with `str()`. Ferro sends them as bound parameters, never as SQL text. [Session settings](../concepts/row-level-security.md#session-settings) explains when a setting reaches the database and what each operation costs.

## A tenant known after the session opens

When the tenant is known only partway through a request (an auth step that reads a token, say), open the session without settings and set the value once you have it, through `ferro.current_session()`:

```python
--8<-- "docs/examples/row_level_security.py:deferred"
```

The first query sees no rows, and every query after `set_config` sees Globex's two invoices. `current_session()` returns the session open in the current task, so helper code deep in a call stack does not need the session passed in.

## Web framework middleware

Open the settings-bearing session in middleware, before any route handler runs, so every route is scoped and an unscoped route is the exception you opt into. Each recipe below assumes an authentication middleware that runs first and puts the user on `scope["user"]` (Litestar, generic ASGI) or `request.state.user` (FastAPI), with the tenant's id on `user.tenant_id`. Change those two lines to wherever your authentication puts the user.

### Litestar

Place the middleware after authentication. `ASGIMiddleware` needs Litestar 2.15 or later, and `AbstractMiddleware` works on every 2.x release:

=== "ASGIMiddleware"

    ```python {test=compile reason="needs Litestar and a Postgres connection"}
    from litestar import Litestar, get
    from litestar.enums import ScopeType
    from litestar.middleware import ASGIMiddleware
    from litestar.types import ASGIApp, Receive, Scope, Send

    import ferro


    class TenantScopingMiddleware(ASGIMiddleware):
        scopes = (ScopeType.HTTP,)
        # Routes that run unscoped: health checks and the like.
        exclude_path_pattern = ("/health", "/metrics")
        exclude_opt_key = "skip_tenant_scope"

        async def handle(
            self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
        ) -> None:
            user = scope["user"]  # set by your authentication middleware
            async with ferro.engines.session(
                "app", settings={"app.tenant_id": str(user.tenant_id)}
            ):
                await next_app(scope, receive, send)


    @get("/health", opt={"skip_tenant_scope": True})
    async def health() -> dict[str, str]:
        return {"status": "ok"}


    app = Litestar(route_handlers=[health], middleware=[TenantScopingMiddleware()])
    ```

=== "AbstractMiddleware"

    ```python {test=compile reason="needs Litestar and a Postgres connection"}
    from litestar import Litestar, get
    from litestar.middleware import AbstractMiddleware
    from litestar.types import Receive, Scope, Send

    import ferro


    class TenantScopingMiddleware(AbstractMiddleware):
        scopes = {"http"}
        # Routes that run unscoped: health checks and the like.
        exclude = ["/health", "/metrics"]
        exclude_opt_key = "skip_tenant_scope"

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            user = scope["user"]  # set by your authentication middleware
            async with ferro.engines.session(
                "app", settings={"app.tenant_id": str(user.tenant_id)}
            ):
                await self.app(scope, receive, send)


    @get("/health", opt={"skip_tenant_scope": True})
    async def health() -> dict[str, str]:
        return {"status": "ok"}


    app = Litestar(route_handlers=[health], middleware=[TenantScopingMiddleware])
    ```

Both register the middleware for every route, and the path pattern and `skip_tenant_scope` opt let individual routes out.

!!! note "Litestar's `Provide` cannot scope every request"
    `Provide` runs a dependency only when the handler's signature asks for it by name. A route that forgets the parameter runs unscoped, and nothing in the request shows it. Middleware runs for every matched route, so use middleware for the session and `Provide` for values a handler reads.

When the tenant is known only after a guard or dependency has run, open the session without settings in the middleware and set it later, as in [A tenant known after the session opens](#a-tenant-known-after-the-session-opens). The middleware's `handle` (or `__call__`) then opens a bare session:

```python {test=compile reason="a fragment of the ASGIMiddleware class above"}
async def handle(
    self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
) -> None:
    async with ferro.engines.session("app"):
        await next_app(scope, receive, send)
```

The guard or dependency then calls `ferro.current_session().set_config("app.tenant_id", ...)` once it knows the tenant.

### FastAPI

Add a `yield` dependency to the application itself, so it runs for every route:

```python {test=compile reason="needs FastAPI and a Postgres connection"}
from fastapi import Depends, FastAPI, Request

import ferro


async def tenant_session(request: Request):
    user = request.state.user  # set by your authentication middleware
    async with ferro.engines.session(
        "app", settings={"app.tenant_id": str(user.tenant_id)}
    ):
        yield


app = FastAPI(dependencies=[Depends(tenant_session)])
```

`dependencies=` on `FastAPI(...)` applies to every route the application serves, routers added with `app.include_router(...)` included. FastAPI runs a `yield` dependency's code before the handler in the same context, so the session is open while the handler runs. To serve some routes unscoped, put them in a separate ASGI application that you `app.mount(...)` beside this one; a mounted application has its own dependencies.

### Generic ASGI

Any other ASGI framework takes the same shape as plain ASGI middleware:

```python {test=compile reason="needs an ASGI server and a Postgres connection"}
import ferro


class TenantScopingASGIMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        user = scope.get("user")  # set by your authentication middleware
        settings = {"app.tenant_id": str(user.tenant_id)} if user else {}
        async with ferro.engines.session("app", settings=settings):
            await self.app(scope, receive, send)
```

A request without a user opens a session with no settings, which sees no rows of a policed table.

### Background jobs

The session reaches everything a request calls, including synchronous handlers a framework runs in a thread pool. It does not reach work that starts outside the request: a background task that runs after the response, a job on a task queue (Celery, arq), a cron job. Open a session with the tenant's settings at the start of each job; until it does, every query it runs on a policed table returns no rows.

## Admin jobs across every tenant

A job that must see every tenant's rows runs on a connection whose role has `BYPASSRLS`, as the migrating role's default connection does in this example. Route the job there by name (`engines.session("admin")`, or `transaction(using="admin")`) rather than opening an `"app"` session without settings, which sees nothing. The `BYPASSRLS` role property records in the database itself which role may see every tenant.

## Deploy order

!!! warning "Deploy settings delivery before policies"
    Once `FORCE ROW LEVEL SECURITY` is live on a table, a connection that does not send the setting sees no rows of it, and nothing raises an error. If the policies ship before the middleware, every page that reads the table goes blank.

Ship the two halves in this order:

1. **Settings delivery.** Deploy the middleware that opens a settings-bearing session around every request, and check in staging that requests carry the tenant's id.
2. **The policies.** Add `__ferro_rls__` to the models, then run `ferro migrate new` (or `connect(migrate_updates=True)`, or an Alembic revision).

For a table that already exists, `ferro migrate new` writes the declaration as one schema step, and its down step removes it:

```sql
-- 0002_invoice_rls/01_schema.up.postgres.sql
ALTER TABLE "invoice" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "invoice" FORCE ROW LEVEL SECURITY;

CREATE POLICY "rls_invoice_tenant_id" ON "invoice" FOR ALL USING ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- 0002_invoice_rls/01_schema.down.postgres.sql
DROP POLICY "rls_invoice_tenant_id" ON "invoice";

ALTER TABLE "invoice" NO FORCE ROW LEVEL SECURITY;

ALTER TABLE "invoice" DISABLE ROW LEVEL SECURITY;
```

The SQLite step files say `-- ferro: not-applicable`, and a project that targets only SQLite gets no step at all. See [Migrations](schema/migrations.md) for the rest of the workflow.

Ferro has no switch to hold the policies back: declaring `RowSecurity` and migrating enforces it at once, as with every other schema change Ferro manages. Migrate as a role with `BYPASSRLS`, so a data step in the same migration sees every row; `connect(migrate_updates=True)` warns when the connecting role has neither superuser nor `BYPASSRLS` and an existing table it migrates has `FORCE` declared (the text is in the [reference](../reference/row-level-security.md#warnings)).

## JSON claims

Some platforms (Supabase among them) pass the whole set of authentication claims as one JSON setting and read it in SQL. Ferro sends the setting; the function that unpacks it is yours to create, once, in a migration:

```sql
CREATE OR REPLACE FUNCTION auth_uid() RETURNS uuid
LANGUAGE sql STABLE
AS $$
  SELECT (NULLIF(current_setting('request.jwt.claims', true), '')::json ->> 'sub')::uuid
$$;
```

`auth_uid()` returns the token's `sub` claim as a `uuid`, or `NULL` when the session sent no claims.

Send the claims as one setting:

```python {test=compile reason="needs a Postgres connection and the auth_uid() function above"}
import json

claims = {"sub": "5f0c1a4e-2b7d-4c1e-9a3f-6d8e2b1c0a94", "role": "editor"}
async with ferro.engines.session(
    "app", settings={"request.jwt.claims": json.dumps(claims)}
):
    ...
```

Every query in that session can read the claims through `auth_uid()`.

Then call the function from a policy written in SQL, inside a scalar subquery:

```python {test=compile reason="a RowPolicy argument, not a statement"}
RowPolicy(
    name="owner",
    using="owner_id = (SELECT auth_uid())",
    with_check="owner_id = (SELECT auth_uid())",
)
```

Wrapped in `(SELECT ...)`, a `STABLE` function runs once per statement instead of once per row, because Postgres can evaluate the subquery once and reuse its result.

## See also

- [Row-level security and session settings](../concepts/row-level-security.md): how settings reach the database, what each operation costs, connection delivery, and drift.
- [Row-level security reference](../reference/row-level-security.md): `RowSecurity`, `RowPolicy`, the command table, the shorthand's casts, and composing several policies.
- [Connections & Databases](connections.md): named connections and `PoolConfig`.
- [Transactions](transactions.md): one transaction for several operations.
