from contextvars import ContextVar

from typing import Any, Protocol

from ._deprecations import (
    IR_FIRST_DEPRECATION_REMOVE_IN,
    IR_FIRST_DEPRECATION_SINCE,
    IR_FIRST_MIGRATION_GUIDE_SESSIONS,
    warn_deprecated,
)

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

_LEGACY_DEFAULT_CONNECTION_REASON = (
    "Implicit default-connection routing without an active session is deprecated; "
    "use `async with ferro.engines.session(\"name\")` (or pass `session=...`) for "
    "ORM/raw operations."
)

# Global registry for models (Python side)
_MODEL_REGISTRY_PY = {}

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


def resolve_operation_scope(
    *,
    using: str | None,
    session: SessionLike | None,
    allow_legacy_default: bool,
) -> tuple[str | None, str | None, str | None]:
    """Resolve `(tx_id, using, session_id)` for ORM/raw operations."""
    tx_id = _CURRENT_TRANSACTION.get()
    tx_connection = _CURRENT_TRANSACTION_CONNECTION.get()

    ambient_session = _CURRENT_SESSION.get()
    explicit_session = session
    effective_session = explicit_session or ambient_session

    # Explicit `using` can override ambient sessions for compatibility, but not
    # explicitly supplied `session=...`.
    if effective_session is not None and using is not None:
        if explicit_session is not None and using != effective_session.connection_name:
            raise ValueError(
                "Explicit `using` conflicts with explicit `session` connection"
            )
        if explicit_session is None and using != effective_session.connection_name:
            effective_session = None

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
        return tx_id, None, session_id

    effective_using = using or (
        effective_session.connection_name if effective_session is not None else None
    )
    if effective_using is None and allow_legacy_default:
        warn_deprecated(
            reason=_LEGACY_DEFAULT_CONNECTION_REASON,
            since=IR_FIRST_DEPRECATION_SINCE,
            remove_in=IR_FIRST_DEPRECATION_REMOVE_IN,
            reference=IR_FIRST_MIGRATION_GUIDE_SESSIONS,
            stacklevel=2,
        )
    return None, effective_using, session_id


def resolve_transaction_scope(
    *,
    using: str | None,
    session: SessionLike | None,
    allow_legacy_default: bool,
) -> tuple[str | None, str | None, str | None]:
    parent_tx_id = _CURRENT_TRANSACTION.get()
    tx_connection = _CURRENT_TRANSACTION_CONNECTION.get()
    ambient_session = _CURRENT_SESSION.get()
    explicit_session = session
    effective_session = explicit_session or ambient_session

    if effective_session is not None and using is not None:
        if explicit_session is not None and using != effective_session.connection_name:
            raise ValueError(
                "Explicit `using` conflicts with explicit `session` connection"
            )
        if explicit_session is None and using != effective_session.connection_name:
            effective_session = None

    if parent_tx_id is not None:
        # Nested tx route is always inherited from parent.
        return parent_tx_id, None, (
            effective_session.session_id if effective_session is not None else None
        )

    effective_using = using or (
        effective_session.connection_name if effective_session is not None else None
    )
    if effective_using is None and allow_legacy_default:
        warn_deprecated(
            reason=_LEGACY_DEFAULT_CONNECTION_REASON,
            since=IR_FIRST_DEPRECATION_SINCE,
            remove_in=IR_FIRST_DEPRECATION_REMOVE_IN,
            reference=IR_FIRST_MIGRATION_GUIDE_SESSIONS,
            stacklevel=2,
        )
    return None, effective_using, (
        effective_session.session_id if effective_session is not None else None
    )
