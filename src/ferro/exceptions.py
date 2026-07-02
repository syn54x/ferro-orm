"""ORM-specific exceptions.

The tree is DBAPI-shaped so callers can catch database failures by type
instead of string-matching driver messages:

- :class:`FerroError` — root of everything Ferro raises for database work.
    - :class:`InterfaceError` — the database interface was misused
      (unknown connection name, protocol/configuration problems).
    - :class:`OperationalError` — the database or its environment failed
      (connection refused, pool timeout, I/O or TLS errors).
    - :class:`DataError` — a value could not be decoded or converted.
    - :class:`IntegrityError` — a constraint rejected the statement.
        - :class:`UniqueViolationError`
        - :class:`ForeignKeyViolationError`
        - :class:`NotNullViolationError`
        - :class:`CheckViolationError`
    - :class:`ModelDoesNotExist` — lookup by primary key found no row
      (also a :class:`LookupError` for backwards compatibility).

Exceptions raised from the Rust core preserve the original driver
message and, where the backend provides them, the SQLSTATE code and the
violated constraint name as structured attributes.
"""

from typing import Any


class FerroError(Exception):
    """Root of Ferro's database exception hierarchy.

    Attributes:
        driver_message: Original message from the database driver, when the
            error originated in the database. ``None`` otherwise.
        sqlstate: Five-character SQLSTATE code (e.g. ``"23505"``) when the
            backend reports one. ``None`` otherwise (SQLite reports extended
            result codes for some errors, Postgres reports SQLSTATE).
        constraint: Name of the violated constraint when the backend reports
            it (Postgres does; SQLite does not). ``None`` otherwise.
    """

    driver_message: str | None
    sqlstate: str | None
    constraint: str | None

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
    """A UNIQUE or PRIMARY KEY constraint rejected the statement."""


class ForeignKeyViolationError(IntegrityError):
    """A FOREIGN KEY constraint rejected the statement."""


class NotNullViolationError(IntegrityError):
    """A NOT NULL constraint rejected the statement."""


class CheckViolationError(IntegrityError):
    """A CHECK constraint rejected the statement."""


class ModelDoesNotExist(FerroError, LookupError):
    """Raised when :meth:`~ferro.models.Model.get` finds no row for the primary key."""

    model: type
    pk: Any

    def __init__(self, model_cls: type, pk: Any) -> None:
        self.model = model_cls
        self.pk = pk
        super().__init__(
            f"No {model_cls.__name__} record found for primary key {pk!r}"
        )

    def __reduce__(self):
        return (self.__class__, (self.model, self.pk))
