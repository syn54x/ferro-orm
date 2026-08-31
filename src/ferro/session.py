"""Session-scoped runtime state for Ferro operations."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ._core import close_session as _core_close_session
from ._core import open_session as _core_open_session
from ._core import set_session_config as _core_set_session_config
from .state import _CURRENT_SESSION

_SESSION_CLOSE_AMBIENT_MISMATCH = (
    "Session close failed: ambient session does not match the closing session. "
    "This usually indicates session lifecycle misuse in the current asyncio context."
)

_SET_CONFIG_NOT_OPEN_MESSAGE = (
    "Cannot set_config on a session that is not open. `set_config` mutates a "
    "live session's settings, so the session has to exist first: enter it with "
    "`async with` before calling `set_config` — including from inside that "
    "block, via `ferro.current_session()`."
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

    A value not known until partway through the session's life — resolved
    from an auth chain, say — is set with `set_config` rather than at open;
    see there for the mid-transaction and identity-map-eviction contract.
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

        Under ``settings_delivery="connection"`` this is also where the
        connection the session pinned goes back to the pool — after Ferro
        resets **exactly the settings keys this session set on it**, and
        nothing else. (Never ``RESET ALL``: that would also wipe the
        ``search_path`` the pool installs on every connection.) A session
        under the default ``transaction`` delivery has nothing to release and
        this costs no round trip.

        Safe to call from a different asyncio context than ``__aenter__``.
        Repeated calls are no-ops.

        Raises:
            RuntimeError: If the ambient session in this asyncio context does not
                match this handle (same-context lifecycle misuse), or if
                session-scoped transactions are still open, or if resetting a
                pinned connection failed — in which case the session is closed
                regardless and the connection was discarded rather than
                returned to the pool.
        """
        if self._close_lock is None:
            self._close_lock = asyncio.Lock()

        async with self._close_lock:
            if self.session_id is None and self._token is None:
                return

            self._assert_close_allowed()

            releasing = None
            if self.session_id is not None:
                # Rust takes the session out of its registry *here*, on the
                # call itself, and rejects the close outright — before any
                # awaitable exists — when transactions are still open. So an
                # exception from this line leaves the handle exactly as it
                # was: still open, still usable.
                releasing = _core_close_session(self.session_id)
                self.session_id = None

            try:
                if releasing is not None:
                    # Awaits a round trip only under `connection` settings
                    # delivery, where the pinned connection's settings are
                    # reset before it goes back to the pool.
                    await releasing
            finally:
                # Past that point the session is gone from the runtime
                # whatever happens, so the ambient session is restored
                # whatever happens: a failed reset must not leave the rest of
                # this context scoped to a session that no longer exists.
                self._detach_ambient()

    def _detach_ambient(self) -> None:
        """Stop being the ambient session for this asyncio context."""
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

    async def set_config(self, key: str, value: str) -> None:
        """Mutate this session's settings while it is open.

        Everything you can `settings=` at open, you can also set later, for the
        case where the value isn't known until partway through a request — an
        auth chain that resolves the tenant only after checking a token, say:

            async with engines.session() as session:      # no settings yet
                tenant = await resolve_tenant_from_auth_header(request)
                await session.set_config("myapp.tenant_id", tenant)
                # every query from here on is scoped to `tenant`
                invoices = await Invoice.where(lambda invoice: invoice.paid).all()

        Deep in a call stack, reach the session through `ferro.current_session()`
        rather than threading it through every function signature:

            async def resolve_tenant_and_scope(request) -> None:
                tenant = await resolve_tenant_from_auth_header(request)
                await ferro.current_session().set_config("myapp.tenant_id", tenant)

        **When the new value takes effect, precisely.** Any operation or
        `transaction()` STARTED after `set_config` returns sees the new value
        — this holds for any task, not just the one that called `set_config`,
        because the change is committed before `set_config`'s `await`
        returns. An operation or transaction already in flight *when*
        `set_config` runs keeps running under the scope it started with —
        that is the existing per-operation-atomicity guarantee, working as
        intended, not something `set_config` reaches back into. The one place
        that needs (and gets) special handling is a `transaction()` block
        already open in the SAME task that calls `set_config` — there, the
        very next statement in that same transaction sees the new value
        immediately, rather than waiting for the transaction to end and a new
        one to begin (there is no race to resolve here: the two are
        sequential code in one task, by construction):

            async with engines.session() as session:
                async with transaction():
                    await session.set_config("myapp.tenant_id", "acme")
                    # this SELECT, in the SAME transaction, already sees it
                    rows = await Invoice.where(lambda invoice: invoice.paid).all()

        `set_config` also evicts this session's identity map on a real change:
        an instance `get()`-ed under the old scope is never handed back under
        the new one — the next `get()` for that primary key refetches instead.
        A call that doesn't actually change the value (the key already holds
        it) is a true no-op: no database round trip, no identity-map eviction.

        Validation matches `engines.session(settings=...)` exactly (see there
        for the full rationale): `key` and `value` must be `str`, and `key` must
        be a dotted custom setting name.

        Args:
            key: Dotted custom Postgres setting name (e.g. `"myapp.tenant_id"`).
            value: The setting's new value.

        Raises:
            RuntimeError: This session is not open (`set_config` mutates a live
                session, so there has to be one), or this session's connection
                is not Postgres — `set_config` is a declaration, exactly like
                `settings=` at open, so it has to be honourable rather than a
                silent no-op on a backend with no GUCs. On the rare failure
                while delivering the change into an already-open transaction,
                this session's recorded settings and identity map have
                already committed the change regardless (see the Ferro source
                for the exact sequencing) — the affected transaction cannot go
                on to leak an old-scope read, because Postgres aborts a
                transaction after a failed statement until it is rolled back.
            TypeError: `key` or `value` is not a `str`.
            ValueError: `key` is not a dotted custom setting name.
        """
        if self.session_id is None:
            raise RuntimeError(_SET_CONFIG_NOT_OPEN_MESSAGE)
        if self.connection_name is None:
            # `connection_name` is resolved by `__aenter__` alongside
            # `session_id` (see there), so a live `session_id` should make
            # this unreachable. Raised rather than asserted (`assert` strips
            # under `-O`) because this is a Ferro lifecycle invariant, not an
            # ordinary user mistake.
            raise RuntimeError(_SET_CONFIG_NOT_OPEN_MESSAGE)

        validated = _validated_settings({key: value})
        ((validated_key, validated_value),) = validated.items()

        # Only the one validated pair is sent — Rust merges it against the
        # session's own last-committed settings, inside a lock that
        # serializes the read and the write together, so two sibling tasks
        # setting different keys concurrently can never lose one to a
        # stale-mirror race (see `operations::set_session_config`). The
        # mirror below is always assigned from what Rust actually committed,
        # never computed here — both when this call changed something and
        # when it was a no-op.
        committed = await _core_set_session_config(
            self.session_id,
            self.connection_name,
            validated_key,
            validated_value,
        )
        self.effective_settings = dict(committed)

    def query(self, model_cls):
        from .query import Query

        return Query(model_cls, session=self)


def current_session() -> "Session | None":
    """Return the session open in the current asyncio task, or `None`.

    A `Session` is ambient: once you `async with engines.session(...)`, code
    called from inside that block doesn't need the session passed down through
    every function signature — it can just ask for it. This is what makes the
    deferred-resolution pattern possible: open a session before you know the
    tenant, resolve it partway through the request, and hand it to the session
    from wherever that resolution happens:

        async with engines.session():             # tenant not known yet
            await handle_request(request)          # ... called deep inside ...

        async def handle_request(request) -> None:
            tenant = await resolve_tenant_from_auth_header(request)
            await ferro.current_session().set_config("myapp.tenant_id", tenant)
            # every query for the rest of this request is scoped to `tenant`

    Nested sessions each become the ambient one for the code inside their own
    `async with` block; `current_session()` always returns the innermost one,
    and `None` outside every session.

    Returns:
        Session | None: The ambient session, or `None` if none is open.
    """
    return _CURRENT_SESSION.get()  # type: ignore[return-value]  # ty: ignore[invalid-return-type]


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

        The cost, stated plainly: about two extra round-trips per *operation*
        that is not already inside a transaction — where one operation is one
        call into Ferro's core, not one line of your code. A flow that saves a
        row and then links it is two operations and pays twice; put such a flow
        in a `transaction()` block and it pays once, for the block.
        `ferro.raw.execute(..., autocommit=True)` opts a statement out of the
        wrap entirely, for the few Postgres statements that cannot run inside a
        transaction — and out of the scoping with it.

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
