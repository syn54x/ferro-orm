"""ORM-specific exceptions.

The tree is DBAPI-shaped so callers can catch database failures by type
instead of string-matching driver messages:

- `FerroError`: the root of everything Ferro raises for database work.
    - `InterfaceError`: the database interface was misused (unknown
      connection name, protocol or configuration problems).
    - `OperationalError`: the database or its environment failed
      (connection refused, pool timeout, I/O or TLS errors).
    - `DataError`: a value could not be decoded or converted.
    - `IntegrityError`: a constraint rejected the statement.
        - `UniqueViolationError`
        - `ForeignKeyViolationError`
        - `NotNullViolationError`
        - `CheckViolationError`
    - `ModelDoesNotExist`: no row matched the primary key (also a
      `LookupError`).

Exceptions raised from the Rust core keep the driver's message and, where
the backend reports them, the SQLSTATE code and the violated constraint's
name as attributes.
"""

from typing import Any


class FerroError(Exception):
    """The root of every exception Ferro raises for database work.

    Catch it to handle any database failure, or a subclass to handle one
    kind.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import UniqueViolationError

        try:
            await Author.create(name="Ada King", email="ada@example.com")
        except UniqueViolationError as error:
            assert error.sqlstate == "2067"
        else:
            raise AssertionError("expected UniqueViolationError")
        ```
    """

    driver_message: str | None
    """The database driver's own message, or `None` when the error did not come from the database."""
    sqlstate: str | None
    """The backend's error code, or `None` when the driver reports none.

    On Postgres it is the five-character SQLSTATE, such as `"23505"`; on
    SQLite the extended result code, such as `"2067"`.
    """
    constraint: str | None
    """The violated constraint's name on Postgres, or `None` (SQLite does not report it)."""

    def __init__(
        self,
        message: str,
        *,
        driver_message: str | None = None,
        sqlstate: str | None = None,
        constraint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.driver_message = driver_message
        self.sqlstate = sqlstate
        self.constraint = constraint

    def __reduce__(self):
        return (
            self.__class__,
            (str(self),),
            {
                "driver_message": self.driver_message,
                "sqlstate": self.sqlstate,
                "constraint": self.constraint,
            },
        )


class InterfaceError(FerroError):
    """The database interface was misused.

    Raised for problems on the client side of the conversation: operating on
    an unknown or uninitialized connection name, driver protocol errors, or
    invalid connection configuration.
    """


class OperationalError(FerroError):
    """The database or its environment failed.

    Raised for failures outside the program's control: the server is
    unreachable, a connection pool timed out or is closed, or an I/O or TLS
    error interrupted the conversation.
    """


class DataError(FerroError):
    """A value could not be decoded or converted.

    Raised when a fetched value cannot be decoded into the expected Python
    type or a bound value cannot be represented for the column type.
    """


class IntegrityError(FerroError):
    """A database constraint rejected the statement."""


class UniqueViolationError(IntegrityError):
    """A `UNIQUE` or `PRIMARY KEY` constraint rejected the statement."""


class ForeignKeyViolationError(IntegrityError):
    """A `FOREIGN KEY` constraint rejected the statement.

    Includes a parent delete blocked by `ForeignKey(on_delete="RESTRICT")`.
    PostgreSQL 18 reports SQLSTATE `23001` for that case; PostgreSQL 17
    reports `23503`. `sqlstate` is the raw driver code.
    """


class NotNullViolationError(IntegrityError):
    """A `NOT NULL` constraint rejected the statement."""


class CheckViolationError(IntegrityError):
    """A `CHECK` constraint rejected the statement."""


class ModelDoesNotExist(FerroError, LookupError):
    """No row matched the primary key.

    [`Model.get`][ferro.Model.get] raises it when no row has the key, and
    [`save`][ferro.Model.save] on a saved instance raises it when its row is
    gone (deleted, or its primary key changed before saving). It is also a
    `LookupError`. Use [`get_or_none`][ferro.Model.get_or_none] to get `None`
    instead.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import ModelDoesNotExist

        try:
            await Author.get(99)
        except ModelDoesNotExist as error:
            assert (error.model, error.pk) == (Author, 99)
        else:
            raise AssertionError("expected ModelDoesNotExist")
        ```
    """

    model: type
    """The model class that was looked up."""
    pk: Any
    """The primary key that matched no row."""

    def __init__(self, model_cls: type, pk: Any) -> None:
        self.model = model_cls
        self.pk = pk
        super().__init__(f"No {model_cls.__name__} record found for primary key {pk!r}")

    def __reduce__(self):
        return (self.__class__, (self.model, self.pk))
