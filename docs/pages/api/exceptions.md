---
description: "API reference for FerroError and the subclasses Ferro raises, each catchable by type."
---

# FerroError and its subclasses

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
    └── RunRefused          a migration run refused before it ran anything; .kind names why
        ├── AlreadyTrackedError
        └── AppliedAboveBaselineError
```

`ForeignKeyViolationError` covers every foreign-key rejection: a dangling
insert, and deleting a row still referenced by `ForeignKey(on_delete="RESTRICT")`.
Catch the type, not a SQLSTATE. On PostgreSQL 17 that RESTRICT delete
reports `23503`; PostgreSQL 18 reports `23001` (`restrict_violation`).
Both are this class. `exc.sqlstate` is the raw driver code; Ferro does not
rewrite it.

Catch a duplicate insert without matching driver text:

```python {test=compile reason="uses an application's User model with a unique email"}
from ferro import UniqueViolationError

try:
    await User(email="taylor@example.com").save()
except UniqueViolationError as exc:
    print(exc.sqlstate)      # SQLSTATE "23505" on Postgres, result code "2067" on SQLite
    print(exc.constraint)    # violated constraint name (Postgres only)
    print(exc.driver_message)  # original driver text, for logs
```

The same pattern applies to table checks declared in `__ferro_checks__`. A row that violates the predicate raises `CheckViolationError`:

```python {test=compile reason="uses the Pair model of the table checks guide"}
from ferro import CheckViolationError

try:
    await Pair.create(left="both", right="set")
except CheckViolationError as exc:
    print(exc.constraint)  # "ck_pair_at_most_one_side" on Postgres
```

`ModelDoesNotExist` is raised by primary-key lookups such as `Model.get(pk)`
when no row matches; use `Model.get_or_none(pk)` to get `None` instead. It is
also raised by `save()` on a persisted instance whose row no longer exists
(see [Saving: INSERT or UPDATE](../guide/mutations.md#saving-insert-or-update)).
It is a `LookupError` too, so an `except LookupError` handler still catches it.

`SettingsError` is raised by `FerroSettings` and every call that reads the
project configuration; its message names the key to add or move, the line to
write, or the variable to set.

A refused migration call raises
[`MigrationRefused`](migrations.md#ferro.migrations.MigrationRefused) or one of
its subclasses; see [Errors](migrations.md#errors) in the migrations reference.
Its `.report` says how far the call got:

```python {test=compile reason="needs a project with a migrations directory"}
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
