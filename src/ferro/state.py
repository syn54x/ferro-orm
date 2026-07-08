import hashlib
import json
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

class FerroModelClass(Protocol):
    """Structural type for a registrable Ferro model class.

    The registry keys by ``__ferro_identity__`` (what ``register_model``
    derives its key from) and resolves bare references against ``__name__`` /
    ``__qualname__``. Typed as a ``Protocol`` rather than ``type[Model]`` so
    ``state`` — which ``models`` and ``metaclass`` import — does not import back
    into them (circular import); a non-Ferro class handed to ``register_model``
    is then a type error at the call site instead of a raw ``AttributeError``
    at runtime.
    """

    __ferro_identity__: str
    __name__: str
    __qualname__: str


# Global registry for models (Python side)
_MODEL_REGISTRY_PY: dict[str, FerroModelClass] = {}

_UNSET = object()


def resolve_model_reference(ref: str, *, default: Any = _UNSET) -> Any:
    """Resolve a model reference to a registered model class (FF-E E1).

    Accepts a qualified identity (``module.QualName``) or a bare class name.
    The registry keys by ``__ferro_identity__``, so a qualified reference is a
    direct dict hit. A bare name matching exactly one registered model resolves
    to it; several matches raise with the qualified candidates listed; no match
    raises (or returns ``default`` when given). Ambiguity always raises.
    """
    model = _MODEL_REGISTRY_PY.get(ref)
    if model is not None:
        return model
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

# Monotonic generation counter bumped by register/deregister; cleared (matched to
# resolved) at the end of relationship resolution (#245).
_REGISTRATION_GENERATION: int = 0
_RESOLVED_GENERATION: int = 0


def registration_generation() -> int:
    """Return the current registration generation counter (test/diagnostic)."""
    return _REGISTRATION_GENERATION


def resolved_generation() -> int:
    """Return the generation counter last cleared by relationship resolution."""
    return _RESOLVED_GENERATION


def is_modelset_dirty() -> bool:
    """True when the registry changed since last resolve or relations are pending."""
    return _REGISTRATION_GENERATION != _RESOLVED_GENERATION or bool(_PENDING_RELATIONS)


def _bump_registration_generation() -> None:
    global _REGISTRATION_GENERATION
    _REGISTRATION_GENERATION += 1


def mark_modelset_resolved() -> None:
    """Record that the current registry generation has been resolved."""
    global _RESOLVED_GENERATION
    _RESOLVED_GENERATION = _REGISTRATION_GENERATION


def reset_registration_generations_for_test() -> None:
    """Reset generation counters (``clean_registry`` fixture)."""
    global _REGISTRATION_GENERATION, _RESOLVED_GENERATION
    _REGISTRATION_GENERATION = 0
    _RESOLVED_GENERATION = 0


def canonical_ir_json(value: dict[str, Any]) -> str:
    """Serialize an IR artifact with deterministic key ordering."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def ir_fingerprint(value: dict[str, Any]) -> str:
    """Return the canonical SHA-256 fingerprint for an IR artifact.

    Single source of the fingerprint function so a stored fingerprint is always
    a pure function of the envelope it accompanies (the ADR fingerprint gate
    reads these; a mismatched pair would serve stale schema silently).
    """
    return hashlib.sha256(canonical_ir_json(value).encode("utf-8")).hexdigest()


def register_model(cls: FerroModelClass) -> None:
    """Record a model class in the Python registry (provisional registration).

    Single entrypoint for every per-model ``_MODEL_REGISTRY_PY`` write. Today the
    writers are the metaclass (class-body registration) and a handful of test
    fixtures that re-add models after a registry wipe. Centralizing the write is
    the prefactor (#243) that lets a later slice (#242) hook one site — e.g. a
    generation counter — instead of chasing scattered dict assignments.
    Idempotent by construction.

    The key is the model's canonical identity (``__ferro_identity__``), derived
    from ``cls`` here rather than caller-supplied (#249). Deriving it in one
    place makes the registry key a pure function of the model: callers can no
    longer register a model under a divergent bare ``__name__`` that a lookup by
    canonical identity would miss (the "Model 'X' not found" footgun). Every
    other emitter — the Rust runtime registry, the raw-FFI operations — keys the
    same model by this identity, so there is exactly one name for a model.
    """
    _MODEL_REGISTRY_PY[cls.__ferro_identity__] = cls
    _bump_registration_generation()


def persist_model_envelope(name: str, envelope: dict[str, Any]) -> None:
    """Store one model's (or join table's) compiled SchemaIR envelope + fingerprint.

    The envelope-cache half of registration. The fingerprint is computed here
    from the envelope — it is a pure function of it, so no caller can persist a
    stale envelope/fingerprint pair. Keyed by the same ``name`` the IR compiler
    uses (a model's ``__ferro_identity__`` or a join-table name).
    """
    _SCHEMA_IR_BY_MODEL[name] = envelope
    _SCHEMA_IR_FINGERPRINT_BY_MODEL[name] = ir_fingerprint(envelope)


def evict_model_envelope(name: str) -> None:
    """Evict one entry from the SchemaIR envelope + fingerprint caches only.

    The envelope-only counterpart of :func:`persist_model_envelope`. Used by the
    join-table cleanup path (``clear_registry``), where the model registry must
    stay untouched — join tables never live there, and ``clear_registry`` promises
    declared models survive so they can cold-rehydrate.
    """
    _SCHEMA_IR_BY_MODEL.pop(name, None)
    _SCHEMA_IR_FINGERPRINT_BY_MODEL.pop(name, None)


def deregister_model(name: str) -> None:
    """Evict one model from every per-model store (registry + envelope caches).

    Single deregistration entrypoint: removes the class-registry entry plus the
    compiled SchemaIR envelope and its fingerprint. Idempotent — absent keys are
    ignored — so it is safe for teardown paths and re-runs.

    Takes a ``name`` (the canonical identity) rather than a class, unlike
    :func:`register_model` which derives the key from ``cls``. The asymmetry is
    deliberate: teardown paths iterate registry keys (e.g. evicting every
    function-local model added during a test) and hold the identity string, not
    the class object.
    """
    removed = name in _MODEL_REGISTRY_PY
    _MODEL_REGISTRY_PY.pop(name, None)
    evict_model_envelope(name)
    if removed:
        _bump_registration_generation()


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
