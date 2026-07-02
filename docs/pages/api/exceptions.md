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
└── ModelDoesNotExist       (also a LookupError)
```

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

`ModelDoesNotExist` is raised by primary-key lookups like `Model.get(pk)` when
no row matches — use `Model.get_or_none(pk)` if you prefer `None` over an
exception — and by `save()` on a persisted instance whose row no longer exists
(see [Saving: INSERT or UPDATE](../guide/mutations.md#saving-insert-or-update)).
It remains a `LookupError`, so pre-existing `except LookupError` handlers keep
working.

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
