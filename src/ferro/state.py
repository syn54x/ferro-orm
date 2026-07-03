from contextvars import ContextVar

from typing import Any, Protocol

from ._core import RouteHandle

# Context variable to store the active transaction ID for the current task
_CURRENT_TRANSACTION: ContextVar[str | None] = ContextVar(
    "current_transaction", default=None
)

_CURRENT_TRANSACTION_CONNECTION: ContextVar[str | None] = ContextVar(
    "current_transaction_connection", default=None
)


class SessionLike(Protocol):
    session_id: str
    connection_name: str


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

# Global registry for models (Python side)
_MODEL_REGISTRY_PY = {}

_UNSET = object()


def resolve_model_reference(ref: str, *, default: Any = _UNSET) -> Any:
    """Resolve a model reference to a registered model class (FF-E E1).

    Accepts a qualified identity (``module.QualName``) or a bare class name.
    A bare name matching exactly one registered model resolves to it; several
    matches raise with the qualified candidates listed; no match raises
    (or returns ``default`` when given). Ambiguity always raises.

    The registry is still keyed by bare class name in this task (FF-E task
    2), so a qualified identity is matched by scanning registered classes'
    ``__ferro_identity__`` stamp rather than by a direct dict lookup — that
    dict-key change lands in a later FF-E task.
    """
    model = _MODEL_REGISTRY_PY.get(ref)
    if model is not None:
        return model
    for cls in _MODEL_REGISTRY_PY.values():
        if getattr(cls, "__ferro_identity__", None) == ref:
            return cls
    candidates = [cls for cls in _MODEL_REGISTRY_PY.values() if cls.__name__ == ref]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        listed = ", ".join(
            sorted(getattr(c, "__ferro_identity__", c.__qualname__) for c in candidates)
        )
        raise RuntimeError(
            f"Model reference '{ref}' is ambiguous: {listed}. "
            "Use the qualified 'module.QualName' form to disambiguate."
        )
    if default is not _UNSET:
        return default
    raise RuntimeError(f"Model '{ref}' not found in registry")


# Global registry for relationships that need deferred resolution
_PENDING_RELATIONS = []

# Global registry for automatically generated join tables
_JOIN_TABLE_REGISTRY = {}

# Latest compiled SchemaIR model-set artifact and fingerprint.
_SCHEMA_IR_MODELSET: dict[str, Any] | None = None
_SCHEMA_IR_MODELSET_FINGERPRINT: str | None = None

# Per-model compiled SchemaIR artifacts and fingerprints.
_SCHEMA_IR_BY_MODEL: dict[str, dict[str, Any]] = {}
_SCHEMA_IR_FINGERPRINT_BY_MODEL: dict[str, str] = {}


_NO_ROUTE_MESSAGE = (
    "No database route for this operation. Open a session "
    "(`async with ferro.engines.session(...)`) or pass `using=`/`session=` "
    "explicitly. Implicit default-connection routing was removed in v0.13 "
    "(deprecated since v0.12); see the sessions migration guide."
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
    """Resolve the route for ORM/raw operations.

    Exactly one resolution site per operation (FF-D D3/D4): a session
    (ambient or explicit), an explicit `using=`, or an active transaction.
    No route is an error; `using=` conflicting with the ambient session is
    an error (the silent sessionless bypass was removed in v0.13). The
    returned `RouteHandle` is resolved once and threaded by value through
    every FFI operation — Rust never re-derives it.
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


def resolve_transaction_scope(
    *,
    using: str | None,
    session: SessionLike | None,
) -> RouteHandle:
    """Resolve the route for `transaction()` — same rules as
    `resolve_operation_scope`, but nested transactions always inherit the
    parent transaction's route. The returned handle's `tx_id` is the
    *parent* transaction (or `None` for a root transaction).
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
    """Build the child route for the `Transaction` handle yielded by
    `transaction()` (FF-D D3).

    Kept in `state.py` — the only module allowed to construct
    `RouteHandle` — rather than inline in `models.py`, so the single
    construction-site invariant holds for every route, including the one
    handed to raw SQL inside a transaction block.
    """
    return RouteHandle(connection_name=connection_name, tx_id=tx_id, session_id=session_id)
