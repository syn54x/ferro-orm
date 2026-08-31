"""Session-scoped runtime state for Ferro operations."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ._core import close_session as _core_close_session
from ._core import open_session as _core_open_session
from .state import _CURRENT_SESSION

_SESSION_CLOSE_AMBIENT_MISMATCH = (
    "Session close failed: ambient session does not match the closing session. "
    "This usually indicates session lifecycle misuse in the current asyncio context."
)


def _validated_settings(settings: Mapping[str, str] | None) -> dict[str, str]:
    """Check a settings mapping at the call site, before any query runs.

    Session settings are Postgres settings (GUCs), so they are text-valued and
    live in a custom, dotted namespace. Both rules are checked here rather than
    at the database, so a typo surfaces where it was written.

    Raises:
        TypeError: A key or value is not a `str`.
        ValueError: A key has no dot — built-in settings (`timezone`, `role`,
            `work_mem`, ...) are not settable through session settings.
    """
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        raise TypeError(
            "Session settings must be a mapping of str to str, got "
            f"{type(settings).__name__}."
        )

    validated: dict[str, str] = {}
    for key, value in settings.items():
        if not isinstance(key, str):
            raise TypeError(
                f"Session setting keys must be str, got {type(key).__name__} ({key!r})."
            )
        if not isinstance(value, str):
            raise TypeError(
                f"Session setting {key!r} must be a str, got "
                f"{type(value).__name__} ({value!r}). Postgres settings are text; "
                "convert the value at the call site (e.g. str(ledger.id)) so the "
                "conversion is visible where it happens."
            )
        if "." not in key:
            raise ValueError(
                f"Session setting key {key!r} must be a dotted custom setting name, "
                "such as 'myapp.tenant_id'. Built-in Postgres settings (timezone, "
                "role, work_mem, ...) are deliberately not settable through session "
                "settings — they change how Ferro itself talks to the database."
            )
        validated[key] = value
    return validated


@dataclass(slots=True)
class Session:
    """A unit of work: one connection scope, one identity map, one settings set.

    `settings` are *session settings* — Postgres settings (GUCs) that Ferro
    applies to every operation this session runs, so queries are scoped without
    a per-query `where`:

        async with engines.session(settings={"myapp.tenant_id": "acme"}):
            # Postgres sees myapp.tenant_id = 'acme' for every statement this
            # session sends, which is what a row-level-security policy reads
            # back — inside a transaction() block or, as here, outside one.
            invoices = await Invoice.where(lambda invoice: invoice.paid).all()

    They are validated when the `Session` is constructed and again when it is
    entered, which is when the effective set — this session's settings merged
    over anything it inherits from an enclosing session — is snapshotted. See
    `EngineManager.session` for the full contract, including what an operation
    outside a transaction sends and how inherited settings behave off Postgres.
    """

    connection_name: str | None = None
    session_id: str | None = None
    settings: Mapping[str, str] | None = None
    effective_settings: dict[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _token: Any = field(default=None, repr=False, compare=False)
    _enter_context: contextvars.Context | None = field(
        default=None, repr=False, compare=False
    )
    _enter_task: asyncio.Task[Any] | None = field(
        default=None, repr=False, compare=False
    )
    _close_lock: asyncio.Lock | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Eager validation: a bad key or value raises where it was written, not
        # on the first query that would have carried it. `settings` is left
        # exactly as passed and checked again at enter, which is the moment its
        # values are actually read.
        _validated_settings(self.settings)

    async def __aenter__(self) -> "Session":
        # Re-validate rather than trusting the construction-time check: the
        # caller may have reassigned `settings` or mutated their mapping since,
        # and what gets applied has to be what was checked.
        declared = _validated_settings(self.settings)
        self.effective_settings = self._merge_ambient_settings(declared)
        self.session_id, resolved_name = _core_open_session(
            self.connection_name,
            list(self.effective_settings.items()),
            list(declared.items()),
        )
        if self.connection_name is None:
            self.connection_name = resolved_name
        self._token = _CURRENT_SESSION.set(self)
        self._enter_context = contextvars.copy_context()
        self._enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self.close()
        except Exception as close_exc:
            if exc_type is None:
                raise
            if exc is not None:
                raise exc from close_exc
            raise close_exc

    async def close(self) -> None:
        """Close this session and release its runtime state.

        Safe to call from a different asyncio context than ``__aenter__``.
        Repeated calls are no-ops.

        Raises:
            RuntimeError: If the ambient session in this asyncio context does not
                match this handle (same-context lifecycle misuse), or if
                session-scoped transactions are still open.
        """
        if self._close_lock is None:
            self._close_lock = asyncio.Lock()

        async with self._close_lock:
            if self.session_id is None and self._token is None:
                return

            self._assert_close_allowed()

            if self.session_id is not None:
                session_id = self.session_id
                _core_close_session(session_id)
                self.session_id = None

            if self._token is None:
                self._enter_context = None
                self._enter_task = None
                return

            token = self._token
            self._token = None
            self._enter_context = None
            self._enter_task = None
            self._restore_ambient_session(token)

    def _assert_close_allowed(self) -> None:
        if self._token is None:
            return
        if asyncio.current_task() is not self._enter_task:
            return
        ambient = _CURRENT_SESSION.get()
        if ambient is self:
            return
        entered_ambient = (
            self._enter_context.get(_CURRENT_SESSION)
            if self._enter_context is not None
            else None
        )
        if entered_ambient is self and ambient is not None:
            raise RuntimeError(_SESSION_CLOSE_AMBIENT_MISMATCH)

    def _restore_ambient_session(self, token: Any) -> None:
        ambient = _CURRENT_SESSION.get()
        if ambient is not self:
            try:
                _CURRENT_SESSION.reset(token)
            except ValueError:
                return
            raise RuntimeError(_SESSION_CLOSE_AMBIENT_MISMATCH)
        try:
            _CURRENT_SESSION.reset(token)
        except ValueError:
            return

    def _merge_ambient_settings(self, declared: dict[str, str]) -> dict[str, str]:
        """Snapshot the settings this session runs with, at enter.

        A session opened inside another one starts from the outer session's
        effective settings and overrides them key by key, so helper code that
        opens its own session stays scoped:

            async with engines.session(settings={"myapp.tenant_id": "acme"}):
                async with engines.session(settings={"myapp.role": "auditor"}):
                    # myapp.tenant_id == 'acme' and myapp.role == 'auditor'
                    ...

        The merge is a snapshot: nothing propagates between the two sessions
        afterwards, and closing the inner one leaves the outer exactly as it was.

        Args:
            declared: This session's own validated settings.
        """
        declared = dict(declared)
        ambient = _CURRENT_SESSION.get()
        inherited = getattr(ambient, "effective_settings", None) if ambient else None
        if not inherited:
            return declared
        return {**inherited, **declared}

    def query(self, model_cls):
        from .query import Query

        return Query(model_cls, session=self)


class EngineManager:
    def session(
        self,
        name: str | None = None,
        *,
        settings: Mapping[str, str] | None = None,
    ) -> Session:
        """Open a session on `name` (or the default connection).

        Pass `settings` to give every operation in the session a set of Postgres
        settings (GUCs) — the tenancy scope row-level-security policies read:

            async with engines.session(settings={"myapp.tenant_id": "acme"}):
                open_invoices = await Invoice.where(
                    lambda invoice: invoice.status == "open"
                ).all()

        Ferro sends them as one parameter-bound statement right after `BEGIN`,
        before any statement of yours:

            BEGIN
            SELECT set_config($1, $2, true)   -- 'myapp.tenant_id', 'acme'
            SELECT "invoice".* FROM "invoice" WHERE "status" = $1
            COMMIT

        `true` is Postgres' `is_local` flag, so the value dies with the
        transaction and a recycled pool connection can never leak one request's
        scope into the next. Nested savepoints inherit it; keys and values are
        always bound parameters, never pasted into SQL.

        Every operation in the session is scoped, whether or not you opened a
        transaction. Inside a `transaction()` block the settings ride that
        block's `BEGIN`. Outside one, the operation opens an implicit
        transaction of its own — as above — so a plain `where().all()`,
        `save()`, `get()` or raw query is scoped too. The wrap is per
        *operation*, not per statement, so an operation that issues several
        statements (a chunked `bulk_create`) sends all of them inside one
        transaction and stays all-or-nothing, exactly as it already did.
        The cost, stated plainly: about two extra round-trips on an operation
        that is not already inside a transaction.

        A session without `settings` sends nothing extra at all — same
        statements, same connections, no wrap.

        Settings you declare here have to be honourable, so opening this session
        on a non-Postgres connection raises. Settings it merely *inherits* from
        an enclosing session are different: they ride along, so a Postgres
        session nested deeper still gets them, but they lie inert on a
        non-Postgres connection — which has no row security to scope anyway.
        A tenant-scoped request can therefore still reach an auxiliary SQLite
        database without ceremony:

            async with engines.session("pg", settings={"myapp.tenant_id": "acme"}):
                async with engines.session("local_cache"):   # SQLite: fine
                    stale = await Lookup.where(lambda row: row.stale).all()

        Args:
            name: Registered connection name; defaults to the default connection.
            settings: Session settings as `{dotted.key: str value}`.

        Returns:
            Session: An unopened session handle; `async with` it.

        Raises:
            TypeError: A settings key or value is not a `str`.
            ValueError: A settings key is not dotted (built-in Postgres settings
                are deliberately not settable this way).
            RuntimeError: On enter, when a session that *declares* settings
                resolves to a non-Postgres connection — settings are Postgres
                GUCs, so scoping fails loudly rather than silently doing nothing.
        """
        return Session(connection_name=name, settings=settings)


engines = EngineManager()
