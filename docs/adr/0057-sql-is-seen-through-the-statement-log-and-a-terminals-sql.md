# SQL is seen through the statement log and a terminal's `.sql()`, both rendered by the function that sends it

A developer wants to know what this query sends:

```python
adults = User.where(lambda user: user.age >= 18).order_by(lambda user: user.name)

print(await adults.all.sql())
# SELECT "user"."id", "user"."name", "user"."age" FROM "user" WHERE "user"."age" >= $1 ORDER BY "user"."name" ASC

logging.getLogger("ferro.sql").setLevel(logging.DEBUG)
await adults.all()
# ferro.sql DEBUG SELECT ... WHERE "user"."age" >= $1 ORDER BY ... params=(18,) 0.4ms
```

Schema changes already had this (`PassReport`, ADR-0049); queries and mutations had nothing. We decided four things (grilled with #700).

## The statement log is a logger, and the logger is the only switch

Every statement ferro sends is one record on `ferro.sql` at `DEBUG`, written as the statement finishes so the record carries its duration (a failure gets a record with its error): queries, mutations, raw SQL, `BEGIN`/`COMMIT`/savepoints, `SET LOCAL`, the catalog probe and migration DDL alike. The record is a one-line message plus structured attributes (`sql`, `params`, `dialect`, `duration_ms`, `operation`, `connection`, `rows`), so a structured-logging handler reads fields and a plain one reads a line. Multi-statement operations write one record per statement under the same `operation`; nothing groups them.

There is no `echo=` flag. Python checks `isEnabledFor(DEBUG)` once per operation, holding the GIL it already has, and passes the answer down; when the log is off Rust collects nothing, and when it is on Rust takes the GIL once per statement to write the record. One switch cannot disagree with another, and a disabled log costs nothing. The free-text `ferro migrate: <ddl>` debug lines become these records; `PassReport` is unchanged. An ad hoc `capture_sql()` context manager was considered and dropped: a handler on `ferro.sql` captures the same records, and a Python context variable does not follow work onto the tokio threads that send the SQL.

For the records to reach anyone, the `ferro` logger stops installing its own `StreamHandler` with `propagate = False` at import and installs a `NullHandler` that propagates, the standard posture for a library; the `ferro migrate` CLI, an application, installs its own handler so a data step's `ctx.log` lines still print. A programmatic caller of the migration API sees those lines once it configures logging, as with any library.

## Every `Query` terminal carries `.sql()`, with the terminal's own signature

`all`, `first`, `count`, `exists`, `update` and `delete` each gain a `.sql()` that renders what the call would send, through a decorator that leaves the method a method: `query.all.sql()`, `query.update.sql(age=19)`, `query.delete.sql()`. It returns a `RenderedSql(sql, params, dialect)` whose `str()` is the SQL alone, so it pastes into `psql`. The alternatives were one `query.sql()` that renders the `SELECT` (a `Query` does not know its terminal, so `count` and `delete` would be unreachable) and a string kind, `query.sql("update", age=19)`, which restates every terminal's signature a second time. Instance `save`/`delete`, `bulk_create` and the many-to-many operations send more than one statement in some cases; they are seen through the log, and the same decorator extends to them later.

## Rendering resolves a database and may read the catalog, never the statement

On Postgres the SQL text depends on the live table: ferro casts bound values (`$1::mood`, `::uuid`, `::timestamp`) by what the catalog says the column is, not what the model declares, because the two differ on a database ferro did not create and on a drifted one, and a typed text parameter gets no assignment coercion (the C2 probe, `docs/plans/2026-07-02-004-ff-c-c2-catalog-cache-design.md`). The probe is cached per table per schema epoch, so a warm session renders for free. `.sql()` therefore routes like the call it mirrors (`await query.all.sql()`), raises the same "no database route" error outside a session, and sends nothing but that cached probe: no transaction, no identity-map write. An offline renderer from the model alone would be a second renderer that disagrees with what runs on exactly those databases, the phantom-diff shape I-1 exists to prevent. The rendered statement is the exact bytes sent, placeholders included (`?` on SQLite, `$1` on Postgres), never reformatted.

## A parameter is redacted by its value or by its column, decided once in Python

A bound value shows as `***`, a sentinel whose `repr` is `***` so a test can tell it from the string, when it is a `SecretStr`/`SecretBytes` or when it is bound to a column whose field is declared one. Either rule alone leaks: a plain `"hunter2"` compared against a secret column, or a `SecretStr` passed to `raw.execute` where no column is known. The mask is decided in Python, from the model's declared secret fields and the value's type, and travels to Rust beside the binds; Rust never re-derives it, the one-decision pattern I-1 uses. `.sql()`'s `params` are redacted the same way: one rule, applied in one place, and `print(rendered)` in a bug report is the common use. `SET LOCAL` takes no bind parameters on Postgres, so a session setting's value appears inline, unredacted; settings are tenancy scope, not secrets by declaration. Depends on #760, which makes a secret field store its raw value and hydrate as a secret type.

A pin holds the two doors together: for every terminal on both dialects, `.sql()` equals the record the executed call wrote.

## Amendment (2026-10-21): pool hooks are not in the statement log

A Postgres pool sends two statements of its own, outside any operation: the `after_connect` hook's `SET search_path TO …` on each new connection, and, with `settings_delivery="connection"`, the `after_release` guard's `SELECT coalesce(current_setting($1, true), '')` on each returned connection while a session holds a pinned one. They are not in the statement log. A record needs an operation's scope, and these belong to no operation: sqlx runs the release hook on a task it spawns, and opens connections for `min_connections` in its background maintenance, so a scope could reach at most the connects an operation happens to wait on. Logging them would mean asking the logger once per connection opened and once per connection returned, a GIL take on each, which is the per-pool-event cost the one switch exists to avoid. The run lock's statements are not pool hooks: the run sends them on its own session inside its `migrate` scope, so they are records like the DDL around them.
