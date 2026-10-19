"""Operation routing: sessions, transactions, and RouteHandle construction.

The single resolution site for "which connection does this operation hit"
and the only module allowed to construct ``RouteHandle``.
Registration state lives in `ferro.registry`.
"""

from contextvars import ContextVar

from typing import Protocol

from ._core import RouteHandle

# Context variable to store the active transaction ID for the current task
_CURRENT_TRANSACTION: ContextVar[str | None] = ContextVar(
    "current_transaction", default=None
)

_CURRENT_TRANSACTION_CONNECTION: ContextVar[str | None] = ContextVar(
    "current_transaction_connection", default=None
)


class SessionLike(Protocol):
    """What routing reads from a session: its id, connection and settings."""

    session_id: str
    """The open session's id."""
    connection_name: str
    """The connection the session runs on."""
    effective_settings: dict[str, str]
    """The settings the session runs with, snapshotted when it opened.

    Empty for a session that declares none; a nested session inherits from
    it.
    """


_CURRENT_SESSION: ContextVar[SessionLike | None] = ContextVar(
    "current_session", default=None
)

_SESSION_CLOSED_MESSAGE = (
    "Session is closed. Open a new session with "
    "`async with ferro.engines.session(...)` or pass an active session handle."
)


def _ensure_active_session(session: SessionLike | None) -> None:
    if session is not None and session.session_id is None:
        raise RuntimeError(_SESSION_CLOSED_MESSAGE)


_NO_ROUTE_MESSAGE = (
    "No database route for this operation. Open a session "
    "(`async with ferro.engines.session(...)`) or pass `using=`/`session=` "
    "explicitly. Implicit default-connection routing was removed in v0.14 "
    "(deprecated since v0.12); see the upgrade guide."
)


def _using_conflict_message(using: str, session_connection: str) -> str:
    return (
        f"Explicit `using={using!r}` conflicts with the ambient session bound "
        f"to connection {session_connection!r}. Pass an explicit `session=` "
        f"for that connection, or open a session on {using!r}."
    )


def resolve_operation_scope(
    *,
    using: str | None,
    session: SessionLike | None,
) -> RouteHandle:
    """Resolve the route of an ORM or raw SQL operation.

    The one resolution site per operation: a session (current or explicit),
    an explicit `using=`, or an active transaction. The returned
    `RouteHandle` is resolved once and passed by value through every core
    operation; the Rust core never derives it again.

    Args:
        using: An explicit connection name, or `None`.
        session: An explicit session, or `None` for the current one.

    Returns:
        The route the operation runs on.

    Raises:
        ValueError: `using` names another connection than the session's or
            the transaction's, or an explicit session and `using` disagree.
        RuntimeError: There is no route (no session, no `using`), or the
            session is closed.
    """
    tx_id = _CURRENT_TRANSACTION.get()
    tx_connection = _CURRENT_TRANSACTION_CONNECTION.get()

    ambient_session = _CURRENT_SESSION.get()
    explicit_session = session
    effective_session = explicit_session or ambient_session

    if effective_session is not None and using is not None:
        if using != effective_session.connection_name:
            if explicit_session is not None:
                raise ValueError(
                    "Explicit `using` conflicts with explicit `session` connection"
                )
            raise ValueError(  # FF-D D4
                _using_conflict_message(using, effective_session.connection_name)
            )

    if explicit_session is None and ambient_session is not None:
        _ensure_active_session(ambient_session)
    _ensure_active_session(effective_session)

    session_id = effective_session.session_id if effective_session is not None else None

    if tx_id is not None:
        if using is not None and using != tx_connection:
            raise ValueError(
                "Operations inside a transaction inherit the transaction connection"
            )
        if effective_session is not None and tx_connection is not None:
            if effective_session.connection_name != tx_connection:
                raise ValueError(
                    "Active transaction is bound to a different connection than session"
                )
        return RouteHandle(
            connection_name=tx_connection, tx_id=tx_id, session_id=session_id
        )

    effective_using = using or (
        effective_session.connection_name if effective_session is not None else None
    )
    if effective_using is None:
        raise RuntimeError(_NO_ROUTE_MESSAGE)
    return RouteHandle(connection_name=effective_using, session_id=session_id)


def resolve_compile_connection_name(
    *,
    using: str | None,
    session: SessionLike | None,
) -> str | None:
    """Return the connection an operation would run on, or `None` when there is no route.

    Compile-time dialect gates (`.merge()`) must inspect the same connection
    as `resolve_operation_scope`, not only `Query._using`. Schema goldens
    compile without an engine; that case stays `None`.

    Args:
        using: An explicit connection name, or `None`.
        session: An explicit session, or `None` for the current one.

    Returns:
        The connection name, or `None` when there is no route.

    Raises:
        ValueError: `using` conflicts with the session or the transaction.
    """
    try:
        return resolve_operation_scope(using=using, session=session).connection_name
    except RuntimeError as exc:
        if exc.args and exc.args[0] == _NO_ROUTE_MESSAGE:
            return None
        raise


def resolve_transaction_scope(
    *,
    using: str | None,
    session: SessionLike | None,
) -> RouteHandle:
    """Resolve the route of a `transaction()` block.

    The rules are `resolve_operation_scope`'s, except that a nested
    transaction always inherits its parent's route.

    Args:
        using: An explicit connection name, or `None`.
        session: An explicit session, or `None` for the current one.

    Returns:
        The route, whose `tx_id` is the parent transaction's, or `None` for
            a root transaction.

    Raises:
        ValueError: `using` conflicts with the session's connection.
        RuntimeError: There is no route, or the session is closed.
    """
    parent_tx_id = _CURRENT_TRANSACTION.get()
    tx_connection = _CURRENT_TRANSACTION_CONNECTION.get()
    ambient_session = _CURRENT_SESSION.get()
    explicit_session = session
    effective_session = explicit_session or ambient_session

    if effective_session is not None and using is not None:
        if using != effective_session.connection_name:
            if explicit_session is not None:
                raise ValueError(
                    "Explicit `using` conflicts with explicit `session` connection"
                )
            raise ValueError(  # FF-D D4
                _using_conflict_message(using, effective_session.connection_name)
            )

    if explicit_session is None and ambient_session is not None:
        _ensure_active_session(ambient_session)
    _ensure_active_session(effective_session)

    session_id = effective_session.session_id if effective_session is not None else None

    if parent_tx_id is not None:
        # Nested tx route is always inherited from parent.
        return RouteHandle(
            connection_name=tx_connection, tx_id=parent_tx_id, session_id=session_id
        )

    effective_using = using or (
        effective_session.connection_name if effective_session is not None else None
    )
    if effective_using is None:
        raise RuntimeError(_NO_ROUTE_MESSAGE)
    return RouteHandle(connection_name=effective_using, session_id=session_id)


def route_for_transaction(
    connection_name: str, tx_id: str, session_id: str | None
) -> RouteHandle:
    """Build the route of the `Transaction` handle a `transaction()` block yields.

    It lives here, in the only module allowed to construct `RouteHandle`,
    so that invariant holds for every route, including the one raw SQL
    uses inside a transaction block.

    Args:
        connection_name: The transaction's connection.
        tx_id: The transaction's id.
        session_id: The session's id, or `None` outside a session.

    Returns:
        The route.
    """
    return RouteHandle(
        connection_name=connection_name, tx_id=tx_id, session_id=session_id
    )
