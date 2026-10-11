# Exceptions

Every database failure Ferro raises is catchable by type. The tree is
DBAPI-shaped, rooted at `FerroError`:

```
FerroError
├── InterfaceError          the database interface was misused
├── OperationalError        the database or its environment failed
├── DataError               a value could not be decoded or converted
├── IntegrityError          a constraint rejected the statement
│   ├── UniqueViolationError
│   ├── ForeignKeyViolationError
│   ├── NotNullViolationError
│   └── CheckViolationError
├── ModelDoesNotExist       (also a LookupError)
├── SettingsError           the project configuration is missing a key, malformed, or contradicts itself
└── MigrationRefused        a migration call refused; .report says how far it got
    ├── PendingMigrationsError
    ├── DatabaseAheadError
    ├── AlreadyTrackedError
    └── AppliedAboveBaselineError
```

`ForeignKeyViolationError` covers every foreign-key rejection: a dangling
insert, and deleting a row still referenced by `ForeignKey(on_delete="RESTRICT")`.
Catch the type, not a SQLSTATE. On PostgreSQL 17 that RESTRICT delete
reports `23503`; PostgreSQL 18 reports `23001` (`restrict_violation`).
Both are this class. `exc.sqlstate` is the raw driver code — it is not
rewritten.

Catch a duplicate insert without matching driver text:

```python
from ferro import UniqueViolationError

try:
    await User(email="taylor@example.com").save()
except UniqueViolationError as exc:
    print(exc.sqlstate)      # SQLSTATE "23505" on Postgres, result code "2067" on SQLite
    print(exc.constraint)    # violated constraint name (Postgres only)
    print(exc.driver_message)  # original driver text, for logs
```

The same pattern applies to table checks declared in `__ferro_checks__` — a row that violates the predicate raises `CheckViolationError`:

```python
from ferro import CheckViolationError

try:
    await Pair.create(left="both", right="set")
except CheckViolationError as exc:
    print(exc.constraint)  # e.g. "ck_pair_at_most_one_side" on Postgres
```

`ModelDoesNotExist` is raised by primary-key lookups like `Model.get(pk)` when
no row matches — use `Model.get_or_none(pk)` if you prefer `None` over an
exception — and by `save()` on a persisted instance whose row no longer exists
(see [Saving: INSERT or UPDATE](../guide/mutations.md#saving-insert-or-update)).
It remains a `LookupError`, so pre-existing `except LookupError` handlers keep
working.

`SettingsError` is raised by `FerroSettings` and every call that reads the
project configuration; its message names the key to add or move, the line to
write, or the variable to set.

A refused migration call raises
[`MigrationRefused`](migrations.md#ferro.migrations.MigrationRefused), or one
of its subclasses for the states an application branches on:
[`PendingMigrationsError`](migrations.md#ferro.migrations.PendingMigrationsError)
when `require_applied()` finds the database behind its migrations,
[`DatabaseAheadError`](migrations.md#ferro.migrations.DatabaseAheadError) when
the database has applied migrations this checkout does not have,
[`AlreadyTrackedError`](migrations.md#ferro.migrations.AlreadyTrackedError)
when `baseline()` finds the database already tracked (another pre-deploy
adopted it first; `.applied` names the recorded migrations, `.head` the
newest), and
[`AppliedAboveBaselineError`](migrations.md#ferro.migrations.AppliedAboveBaselineError)
when `remove_baseline()` finds a run applied migrations above the baseline
(`.above` names them). Its `.report` is the report of the call that refused,
when it has one: the run's report (what `up()` applied before it stopped, the
refusal itself as `report.refused`), the `check()` report whose
`raise_for_problems()` raised it, or the status report of the database a
baseline refusal is about. A refusal is raised, never returned: an application
that called `up()` must not go on serving a database the run did not reach.

```python
import ferro.migrations
from ferro.migrations import MigrationRefused

try:
    await ferro.migrations.up()
except MigrationRefused as exc:
    print(exc)          # what refused, and the command that fixes it
    print(exc.report)   # what the run applied before it stopped
```

An auto-migrate pass (`connect(auto_migrate=True)`, `ferro.migrate()`,
`ferro.create_tables()`) that fails partway raises its usual error, also with
`.report` set: the [`PassReport`](connection.md#ferro.PassReport) of what
committed before the failure.

::: ferro.FerroError

::: ferro.InterfaceError

::: ferro.OperationalError

::: ferro.DataError

::: ferro.IntegrityError

::: ferro.UniqueViolationError

::: ferro.ForeignKeyViolationError

::: ferro.NotNullViolationError

::: ferro.CheckViolationError

::: ferro.ModelDoesNotExist

::: ferro.SettingsError
