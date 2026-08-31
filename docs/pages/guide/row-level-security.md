# Using with Postgres RLS

PostgreSQL's row-level security (RLS) lets the database itself refuse to
return or write rows a connection isn't scoped to see — the boundary that
would otherwise live entirely in application discipline (a `where` clause
nobody forgets, forever). Ferro ships two features as one pair to make that
boundary real:

- **Session settings** — a `Session` carries Postgres settings (GUCs) and
  delivers them to every connection its operations touch.
- **Row security declarations** — a model declares its policies once
  (`__ferro_rls__`), and Ferro creates and reconciles them like any other
  schema artifact.

Read this whole page before you turn `FORCE ROW LEVEL SECURITY` on in
production — the first two sections cover the two ways this feature bites
teams that skip them.

## The pooler constraint

A GUC set with plain `SET` lives on the **server-side** Postgres connection.
Behind a connection pooler running in *transaction* mode — PgBouncer's default
`pool_mode`, and what most managed Postgres front doors use — that server
connection is handed to a different client between transactions. Two of your
requests can share one server backend seconds apart:

```
tenant A → pgbouncer → server conn 7: SET pinch.ledger_id = 'A'
tenant B → pgbouncer → server conn 7: SELECT ... FROM invoice
                                       -- sees tenant A's policy scope
```

`SET` there isn't a correctness gap — it's a cross-tenant data leak, and
because the pooler is invisible to its clients (`SHOW pool_mode` needs
superuser access to the admin database), Ferro cannot detect it and refuse.
So Ferro doesn't guess:

**Default delivery is `transaction`**: every setting is applied with
parameter-bound `set_config($1, $2, true)` — the `true` is Postgres' `is_local`
flag, so the value dies at `COMMIT` and can never survive onto a connection's
next user. It rides `BEGIN` for every explicit `transaction()` block, and a
plain operation outside one (`Model.where(...).all()`, `create()`, a raw
query) wraps itself in an implicit transaction to carry it — see
[Session settings](#session-settings) below. This is safe on direct Postgres,
PgBouncer transaction mode, RDS Proxy, and the Supabase pooler alike.

**`connection` delivery is opt-in and never automatic** — `PoolConfig(pool=...,
settings_delivery="connection")`. It pins one pool connection per
settings-bearing session and sets values with plain `SET` once, for the whole
session. That is **an explicit operator promise that this pool talks to
Postgres directly.** Reaching for it against a transaction-mode pooler is the
exact leak the paragraph above describes — see
[Connection delivery mode](#connection-delivery-mode) for the full trade-off
before opting in.

## Deploy ordering

Ship the pair in this order:

1. **Settings delivery first.** Get your app opening a settings-bearing
   session around every request (see [Framework integration](#framework-integration))
   and prove it in a staging environment *before* any table enforces
   anything.
2. **Row security declarations second.** Add `__ferro_rls__` to your models
   and run `migrate_updates` (or an Alembic revision) once delivery is live.

Reversed, the failure is silent and total: the moment `FORCE ROW LEVEL
SECURITY` is live on a table and no connection anywhere is delivering the
setting the policy reads, every query on that table returns zero rows — a
blank dashboard, not an error, not a warning your monitoring will catch by
itself.

There is deliberately no adoption-gate flag (no `migrate_rls=False` to hold
the line back). Declaring `RowSecurity` and running `migrate_updates` enforces
it immediately, the same as every other schema artifact Ferro manages. The one
automatic guardrail is at connect time: if the reconciliation pass is about to
turn `FORCE` on for a table and the role running the migration is neither a
superuser nor `BYPASSRLS`, Ferro warns —

> The connected role is neither a superuser nor BYPASSRLS, and this migration
> touches table(s) 'invoice' with FORCE ROW LEVEL SECURITY. Row policies apply
> to the migrating role too, so a backfill or data step can silently see and
> update zero rows. Migrate as a role with BYPASSRLS if this pass moves data.

— which catches the same-shaped mistake for your migrator before it reaches
your users: if a role this unprivileged is about to be filtered by its own
migration, ordinary application traffic on an unscoped connection is in
exactly as much trouble. Give your migrator `BYPASSRLS` (see
[BYPASSRLS](#bypassrls) below) and treat the warning as a deploy-ordering
smoke test, not just a migration-safety one.

## Declaring row security

A model declares its own row security once, as a `RowSecurity` container on
the `__ferro_rls__` `ClassVar` — the single owner of that table's policies and
flags:

=== "Assignment"

    ```python
    --8<-- "docs/examples/row_level_security.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/row_level_security_annotated.py:models"
    ```

`auto_migrate` (or `migrate_updates` on an existing table) creates the table
with row-level security switched on and the policy in place:

```sql
ALTER TABLE "invoice" ENABLE ROW LEVEL SECURITY
ALTER TABLE "invoice" FORCE ROW LEVEL SECURITY
CREATE POLICY "rls_invoice_ledger_id" ON "invoice" FOR ALL
  USING      ("ledger_id" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid)
  WITH CHECK ("ledger_id" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid)
```

From here the database decides which rows a query can see — a connection
whose `pinch.ledger_id` setting is unset sees no rows, and one that carries a
ledger id sees only that ledger's rows. A forgotten `where` filter is no
longer a data leak.

### The shorthand

`RowPolicy(column=..., setting=...)` compares one column to one session
setting and renders the `NULLIF(current_setting(...), '')::<cast>` expression
above for both `USING` and `WITH CHECK`. The cast is derived from the
column's own storage type — `uuid`, `text`/`varchar`, and the integer
families are supported; anything else (`timestamptz`, `jsonb`, ...) is a
class-definition-time error naming the raw form as the way out. The policy's
live name defaults to the column name (`rls_invoice_ledger_id` above); pass
`name=` to choose your own.

### Multiple policies: composing permissive and restrictive

`RowSecurity(*policies, force=True)` takes any number of `RowPolicy` values,
the same way Postgres composes them: **permissive** policies (the default)
OR-compose with each other, and **restrictive** (`restrictive=True`) policies
AND-compose with everything. That is what lets a model express Postgres'
full "owner has full access, an invited member can read" shape declaratively —
own scope-fence plus shared-read:

```python
class Doc(Model):
    id: int | None = Field(default=None, primary_key=True)
    ledger_id: uuid.UUID
    owner: str
    title: str

    __ferro_rls__: ClassVar = RowSecurity(
        # RESTRICTIVE: AND-composes with everything below. No ledger scope,
        # no rows — whoever you are.
        RowPolicy(
            name="tenant", column="ledger_id", setting="pinch.ledger_id",
            restrictive=True,
        ),
        # Permissive, unscoped by command: the owner reads and writes.
        RowPolicy(
            name="owner_all",
            using="\"owner\" = NULLIF(current_setting('pinch.member', true), '')",
            with_check="\"owner\" = NULLIF(current_setting('pinch.member', true), '')",
        ),
        # Permissive, SELECT-only: an invited member reads and nothing more.
        RowPolicy(
            name="invitee_read",
            command="select",
            using=(
                '"id" IN (SELECT doc_id FROM membership WHERE member = '
                "NULLIF(current_setting('pinch.member', true), ''))"
            ),
        ),
    )
```

Every policy needs a unique `name` per model (duplicates are a
class-definition-time error); `command=` scopes a policy to `"all"` (the
default), `"select"`, `"insert"`, `"update"`, or `"delete"`.

### The raw escape hatch

When the shorthand's single column/setting comparison isn't enough — a
membership subquery, a function call, boolean composition — pass `using=`
and/or `with_check=` directly, as `owner_all` and `invitee_read` do above.
The raw form requires `name=` (there's no column to derive it from), and
Postgres' own command rules apply: `using=` only on commands that read
existing rows, `with_check=` only on commands that write, and a `FOR INSERT`
policy needs `with_check=` since there's no existing row to read.

## Session settings

### Opening a session with settings=

```python
async with ferro.engines.session(settings={"pinch.ledger_id": "acme"}):
    open_invoices = await Invoice.where(
        lambda invoice: invoice.status == "open"
    ).all()
```

Every operation in the session is scoped, whether or not you opened a
`transaction()` — a plain `where().all()`, `create()`, `save()`, or raw query
outside one wraps itself in an implicit transaction to carry the value, at a
cost of roughly two extra round-trips per operation not already inside a
`transaction()` block. Put a multi-operation flow in `transaction()` and it
pays once, for the whole block, instead of once per operation.

Settings are validated eagerly, before any connection is touched: values must
be `str` (Postgres settings are text), keys must contain a dot
(`"pinch.ledger_id"`, never `"timezone"` or another built-in — this is a
tenancy API, not a general connection-mutation one), and values are always
bound parameters, never interpolated into SQL.

### Deferred resolution: `current_session().set_config`

The tenant isn't always known when the session opens — an auth chain that
resolves it from a token partway through the request is the common shape.
Open the session bare and supply the scope once you have it:

```python
async with ferro.engines.session() as session:      # tenant not known yet
    await handle_request(request)                    # ...called deep inside...


async def handle_request(request) -> None:
    tenant = await resolve_tenant_from_auth_header(request)
    await ferro.current_session().set_config("pinch.ledger_id", tenant)
    # every query for the rest of this request is scoped to `tenant`
```

`ferro.current_session()` returns the session ambient in the current asyncio
task (or `None` outside every session), so deeply nested helper code never
needs the session threaded through its signature.

### Nesting and inheritance

A nested session's effective settings are the parent's, shallow-merged with
its own (the child wins per key), snapshotted the moment it opens — there is
no live propagation back to the parent, and a settings-less nested session
simply inherits everything:

```python
async with ferro.engines.session(settings={"pinch.ledger_id": "acme", "pinch.role": "owner"}):
    async with ferro.engines.session(settings={"pinch.role": "auditor"}):
        ...  # sees ledger_id=acme (inherited), role=auditor (overridden)
```

Settings follow the **session**, not the connection route — a nested session
opened against a different named connection (`transaction(using="reporting")`
included) still carries the outer scope. A settings-bearing session (or a
settings-bearing operation routed with `using=`) against a non-Postgres
connection raises loudly at open/at the operation, rather than silently
scoping nothing; **inherited** settings on a non-Postgres nested session are
carried but sit inert — opening the session and running its operations both
work, because there is nothing there to apply them to.

### Scope stability mid-operation

`set_config` takes effect for any operation or `transaction()` **started**
after it returns — from any task, since the change commits before
`set_config`'s `await` returns. An operation already in flight when
`set_config` runs keeps running under the scope it started with; this is the
pre-existing operation-atomicity guarantee at work, not a race `set_config`
has to resolve. The one place it gets special handling is a `transaction()`
block already open in the *same* task that calls `set_config`: there, the very
next statement in that same transaction sees the new value immediately.

## The NULLIF contract

The shorthand's `NULLIF(current_setting(key, true), '')::<cast>` exists for
exactly one reason: a bare `current_setting(...)::uuid` throws on almost every
policed query that should instead see zero rows. Two states, both fail
closed, neither ever an error:

| GUC state on the connection | `current_setting(key, true)` | With `NULLIF(..., '')::uuid` |
| :--- | :--- | :--- |
| never set | `NULL` | `NULL` → policy matches nothing → zero rows |
| set, then `RESET` | `''` | `NULL` (via `NULLIF`) → zero rows, not `''::uuid` erroring |

The second row is the one worth internalizing: a custom GUC that was ever
`SET` and later `RESET` on that connection — which is exactly what closing a
`connection`-delivery session does to its touched keys — reads back as an
**empty string**, not `NULL`. Without `NULLIF`, that state is
`invalid input syntax for type uuid: ""` on every query against the table,
turning a fail-closed design into a hard outage the moment a connection is
reused. Write raw policies (the [escape hatch](#the-raw-escape-hatch) above)
with the same wrapper if you hand-write the comparison yourself.

## BYPASSRLS

`force=True` (the default on `RowSecurity`) binds the table's **owner** too —
without it, a deployment that connects as the table owner (the common
single-role setup) gets policies that are simply never consulted, because
Postgres exempts owners from `RLS` unless `FORCE` is set.

Once `FORCE` is on, any connection that legitimately needs to see every row —
your migrator, a bypass connection for admin jobs — needs the database-level
escape hatch, which is a **role property**, never a settings omission:

```sql
ALTER ROLE migrator_role BYPASSRLS;
```

Superusers already bypass RLS unconditionally (`FORCE` included), which is
also why every enforcement test in this feature runs as a created
`NOSUPERUSER` role — a green test connected as a superuser proves nothing.

For an application-side admin job that must see every tenant's rows, open a
named connection whose role carries `BYPASSRLS` and route to it explicitly
(`transaction(using="admin")`, or a session on that named connection) —
`BYPASSRLS` is what makes the bypass auditable in the database itself, rather
than an implicit consequence of "forgot to set a GUC".

## The JSON-claims recipe

The Supabase pattern: instead of one GUC per claim, deliver a single JSON blob
as one session setting and unpack it in SQL. Ferro delivers the setting; it
does not manage the unpacking function's DDL (`CREATE FUNCTION` is not a
schema object Ferro owns or reconciles) — create it yourself, once, in a
migration:

```sql
CREATE OR REPLACE FUNCTION auth_uid() RETURNS uuid
LANGUAGE sql STABLE
AS $$
  SELECT NULLIF(current_setting('request.jwt.claims', true), '')::json ->> 'sub'
$$;
```

Deliver the claims blob as one setting, the same way as any other:

```python
import json

claims = {"sub": str(user.id), "role": user.role}
async with ferro.engines.session(settings={"request.jwt.claims": json.dumps(claims)}):
    ...
```

and reference the function from a raw policy, wrapped in a scalar subquery:

```python
RowPolicy(
    name="owner",
    using="owner_id = (SELECT auth_uid())",
    with_check="owner_id = (SELECT auth_uid())",
)
```

The `(SELECT auth_uid())` wrapper matters for performance, not just style:
combined with `STABLE`, Postgres evaluates it once per statement rather than
once per row, because a `STABLE` function called through a scalar subquery is
eligible to be hoisted into an initPlan. Calling `auth_uid()` directly in the
expression loses that hoist on some query shapes and re-evaluates the claims
parse per row.

## Framework integration

Every recipe below shares one shape: open (or scope) a settings-bearing
session **before** any route handler runs, so tenancy scoping is automatic at
the app level and an unscoped route is the explicit exception, never the
default.

### Litestar (recommended)

Litestar's `AbstractMiddleware`, placed **after** authentication in the stack,
reads the tenant from the auth-populated `scope["user"]` and wraps the
downstream call in a settings-bearing session:

```python
from litestar.middleware import AbstractMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

import ferro


class TenantScopingMiddleware(AbstractMiddleware):
    scopes = {"http"}
    # Per-route opt-out for the rare unscoped endpoint (health checks, ...).
    exclude = ["/health", "/metrics"]
    exclude_opt_key = "skip_tenant_scope"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        user = scope["user"]  # populated by an earlier auth middleware/guard
        async with ferro.engines.session(
            settings={"pinch.ledger_id": str(user.ledger_id)}
        ):
            await self.app(scope, receive, send)
```

Register it app-wide, and opt individual routes out explicitly:

```python
from litestar import Litestar, get

app = Litestar(route_handlers=[...], middleware=[TenantScopingMiddleware])


@get("/health", opt={"skip_tenant_scope": True})
async def health() -> dict:
    return {"status": "ok"}
```

!!! warning "`Provide` dependency injection cannot do run-always scoping"
    Litestar's `Provide` only evaluates a dependency when a handler's own
    signature requests it by name. A route that forgets to declare the
    dependency parameter runs completely unscoped — silently, since nothing
    about the request looks wrong. Middleware runs for every matched route
    regardless of its signature, which is the only way to make "every request
    is scoped by default" actually true. Use `Provide` for values a handler
    wants to *read*, never for something that must apply unconditionally.

If the tenant isn't resolvable until deeper in the request (a guard or a
dependency that itself needs to run first), open the session bare in the
middleware and resolve mid-request with the deferred pattern:

```python
class TenantScopingMiddleware(AbstractMiddleware):
    scopes = {"http"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with ferro.engines.session():
            await self.app(scope, receive, send)


# in a guard or dependency further down the stack:
async def resolve_tenant(request) -> None:
    tenant = await resolve_tenant_from_auth_header(request)
    await ferro.current_session().set_config("pinch.ledger_id", tenant)
```

### FastAPI

An app-wide `yield` dependency, applied globally rather than per-route:

```python
from fastapi import Depends, FastAPI, Request

import ferro


async def tenant_session(request: Request):
    async with ferro.engines.session(
        settings={"pinch.ledger_id": str(request.state.user.ledger_id)}
    ):
        yield


app = FastAPI(dependencies=[Depends(tenant_session)])
```

`dependencies=` at the `FastAPI(...)` level runs for every route the app
serves, with no per-handler signature to remember — the same "run always"
property the Litestar middleware recipe relies on. Opt a router out by
mounting it separately, without the dependency.

### Generic ASGI

Any other ASGI framework: the middleware shape underneath both recipes above,
with no framework-specific pieces:

```python
class TenantScopingASGIMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        user = scope.get("user")
        settings = {"pinch.ledger_id": str(user.ledger_id)} if user else {}
        async with ferro.engines.session(settings=settings):
            await self.app(scope, receive, send)
```

### Outside the request context

Session scope propagates through `contextvars`, which covers a request's
entire transitive call graph — including synchronous handlers a framework
runs in a threadpool. It does **not** cover work that starts outside that
graph: a background task scheduled after the response is sent, a job handed
to a task queue (Celery, arq, ...), a cron job. That code runs in its own
context with no ambient session, so it needs to open its own settings-bearing
session — and until it does, every operation it runs against a policed table
fails closed (zero rows, never an error), exactly as an unscoped request
would.

## Connection delivery mode

`PoolConfig(settings_delivery="connection")` is the opt-in alternative to the
default `transaction` delivery: a settings-bearing session pins one pool
connection at its first operation, applies its settings once with plain `SET`,
and sends every later statement bare — no per-operation wrap.

**When to use it.** Only when you know the pool is a **direct** connection to
Postgres — no PgBouncer transaction mode, no transaction-mode proxy in front
of it. It trades roughly two round-trips per non-transactional operation for
one round-trip for the session's entire life, which matters for
request-heavy, short-transaction workloads on a direct pool.

**The concurrency cap.** A settings-bearing session holds its pinned
connection from first use until it closes, so no more settings-bearing
sessions can run at once than the pool has connections — size
`max_connections` for peak concurrent scoped sessions, not average.

**Operation serialization.** One session, one connection: everything the
session does happens one thing at a time. Two sibling tasks sharing the
session run sequentially, and a sibling operation waits out an open
`transaction()` block rather than running inside it. Operations inside that
`transaction()` block itself are unaffected — they already own the
connection.

**The release-probe cost.** While no session on a pool is pinned, this mode
costs nothing at all. While *any* session on that pool is pinned, every
connection release on that pool performs one marker check — including
releases from settings-less sessions and sessionless operations sharing the
same pool.

**`autocommit=True` is tenant-scoped here — the reverse of `transaction`
delivery.** Under the default mode, `autocommit=True` opts a statement out of
the implicit-transaction wrap that carries the setting, so that statement runs
unscoped (documented, and useful for maintenance DDL that must skip the
scope). Under `connection` delivery there's no wrap to opt out of — the
setting already lives on the connection itself — so an `autocommit=True`
statement on a pinned session still sees it.

**The `ALTER ROLE`/`ALTER DATABASE ... SET` caveat.** Closing a session resets
exactly the keys it touched to whatever value the connection *started* with.
For an ordinary custom setting with no server-side default, that's the empty
string — which is what the [NULLIF contract](#the-nullif-contract) relies on.
If an operator has configured a startup value with `ALTER ROLE ... SET
pinch.ledger_id = ...` or `ALTER DATABASE ... SET ...`, closing the session
restores *that* value instead of clearing it. Never configure a tenancy key as
a role or database default.

**Schema changes wait for pinned sessions.** `connect(migrate_updates=...)`
and `migrate()` refresh the connection pool afterward, and a refresh cannot
complete until every connection — pinned ones included — comes back. Running
a migration while long-lived tenant sessions are open blocks until they
close; Ferro warns, naming how many connections it's waiting on. Run schema
changes before opening tenant-scoped sessions, not concurrently with them.

Sessions **without** settings never pin under this mode: same statements,
same connections, no wrap, no serialization — the mode only changes anything
for the sessions actually using it.

## Raw policies and drift

Reconciliation on a live table (`migrate_updates`) follows the same pattern
Ferro already uses for table checks: a **shorthand** policy that drifts from
its declaration is rebuilt (`DROP POLICY` + `CREATE POLICY`, metadata-only —
no row is read or validated), because Ferro rendered the body and knows
exactly what the catalog should store for it.

A **raw** (`using=`/`with_check=`) policy that drifts is different: Postgres
rewrites author SQL when it stores it (`BETWEEN a AND b` comes back as
`(x >= a) AND (x <= b)`), so a textual difference can't be trusted to mean
"this needs rebuilding" — rebuilding on every cosmetic rewrite would take an
exclusive table lock on every single connect, forever. Instead, Ferro reports
the difference — both the declared text and the live catalog's — and leaves
the policy alone. Two ways out: express the policy so it fits the
[shorthand](#the-shorthand) (which Ferro can always compare exactly), or drop
the policy so the next `migrate_updates` recreates it from the declaration.

**The flags are one-way.** `migrate_updates` can only turn `ENABLE`/`FORCE`
**on**. A model that drops its `__ferro_rls__` declaration, or drops
`force=`, keeps its live flags and warns on *every* connect — not just the
first — until the declaration is restored or `migrate_destructive` tears it
down explicitly. A policy Ferro doesn't recognize (any name that isn't
`rls_*`) is reported and never altered, on any flag; an orphaned `rls_*`
policy (declared once, no longer) warns on `migrate_updates` and drops only
under `migrate_destructive`. See [ADR-0019](https://github.com/syn54x/ferro-orm/blob/main/docs/adr/0019-row-policy-drift-and-the-one-way-flags.md)
for the full reconciliation contract, including why a table a DBA enabled RLS
on by hand — with only their own policies — is left completely alone by every
one of these mechanisms.

## Alembic

With the [Alembic bridge](migrations.md#alembic-for-production) installed,
`alembic revision --autogenerate` emits the same policy DDL the runtime
reconciliation pass would, from the same drift comparison — add, rebuild, and
drop operations, plus the `ENABLE`/`FORCE` flags, as a custom autogenerate
operation (row security has no `SQLAlchemy` metadata construct to carry it,
the same reason table checks use one).

Downgrade semantics are deliberately asymmetric, matching the one-way flags
above:

- A **new declaration**'s downgrade drops the policy it added — a clean
  reverse of an add.
- A **removed declaration**'s downgrade is an intentional no-op: recreating a
  dropped policy's exact live body from a downgrade path is a reviewed edit,
  not something autogenerate should reconstruct.
- A **flag-only** change (`force=True` → `force=False`) downgrades to nothing:
  restoring `FORCE ROW LEVEL SECURITY` is a decision for a human, not an
  automatic reverse.
- Flags and policies that predate Ferro's declaration (a DBA's own fence) are
  never touched by either the upgrade or the downgrade — the same ownership
  gate that protects them at runtime protects them here.

## See Also

- [Session settings](#session-settings) — `engines.session(settings=...)`,
  `current_session()`
- [Schema Migrations](migrations.md) — `migrate_updates`, `migrate_destructive`,
  and the Alembic bridge in general
- [Connections & Databases](connections.md) — `PoolConfig` and named
  connections
- [Transactions](transactions.md) — connection affinity and `using=` routing
- [Queries](queries.md) — the lambda predicate style used throughout this page
