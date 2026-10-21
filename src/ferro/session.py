"""Sessions: `Session`, `engines` and `current_session`."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._core import close_session as _core_close_session
from ._core import open_session as _core_open_session
from ._core import set_session_config as _core_set_session_config
from .state import _CURRENT_SESSION

if TYPE_CHECKING:
    from .models import Model
    from .query.builder import Query

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
    """A unit of work on one connection, with its own identity map and settings.

    Open one with `async with engines.session():`. Every ORM call and raw
    query inside the block runs in it, and code called from the block finds
    it with [`current_session`][ferro.current_session]. Leaving the block
    closes the session. See [Sessions](../guide/connections.md#sessions).

    `settings` are Postgres settings that every operation in the session
    sends, which is what row-level security policies read; see
    [Session settings](../concepts/row-level-security.md#session-settings).
    A session opened inside another starts from the outer one's settings.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import engines

        async with engines.session() as session:
            authors = await Author.where(lambda author: author.id <= 2).all()
        assert [author.name for author in authors] == ["Ada Lovelace", "Grace Hopper"]
        assert session.connection_name == "default"
        ```
    """

    connection_name: str | None = None
    """The connection the session runs on, or `None` for the default until it opens."""
    session_id: str | None = None
    """The open session's id, or `None` before it opens and after it closes."""
    settings: Mapping[str, str] | None = None
    """The settings this session declares, as given."""
    effective_settings: dict[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    """The settings the session runs with: its own over the ones it inherits."""
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
        """Close the session and release what it holds.

        Leaving the `async with` block calls it; call it yourself for a
        session you entered by hand. Under `settings_delivery="connection"`,
        the session first resets exactly the setting keys it set on its
        connection, then returns the connection to the pool. A second call
        does nothing.

        Raises:
            RuntimeError: A `transaction()` block of the session is still
                open, another session became current in this task without
                closing first, or resetting the held connection failed. In
                the last case the session is closed anyway and the
                connection is discarded.
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
                assert self.connection_name is not None  # resolved at __aenter__
                releasing = _core_close_session(self.session_id, self.connection_name)
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
        """Set one session setting while the session is open.

        Use it when the value is known only partway through the session,
        such as a tenant resolved from an auth token. An operation or
        `transaction()` block that starts after it returns sees the new
        value, and so does a `transaction()` block of this session already
        open in any task. It also empties the session's identity map. Setting
        a key to the value it holds sends nothing. See
        [Visibility of a new value](../concepts/row-level-security.md#visibility-of-a-new-value).

        Args:
            key: A dotted custom Postgres setting name, such as
                `"myapp.tenant_id"`.
            value: The setting's new value.

        Raises:
            RuntimeError: The session is not open, its connection is not
                Postgres, or delivering the value to an open transaction
                failed. The new value is recorded even then.
            TypeError: `key` or `value` is not a `str`.
            ValueError: `key` is not a dotted custom setting name.

        Examples:
            ```python {test=run prelude=invoices backend=postgres}
            async with engines.session() as session:
                tenant = await resolve_tenant(request)
                await session.set_config("myapp.tenant_id", tenant)
                invoices = await Invoice.where(lambda invoice: invoice.total > 100).all()
            ```
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

    def query[M: Model](self, model_cls: type[M]) -> Query[M]:
        """Start a query of a model's records in this session.

        Args:
            model_cls: The model to query.

        Returns:
            A [`Query`][ferro.query.builder.Query] over every row of the
                model's table, run in this session.

        Examples:
            ```python {test=run prelude=reference}
            from ferro import current_session

            posts = await current_session().query(Post).where(lambda post: post.views > 1000).all()
            assert [post.title for post in posts] == ["Notes on the Analytical Engine"]
            ```
        """
        from .query import Query

        return Query(model_cls, session=self)


def current_session() -> "Session | None":
    """Return the session open in the current task, or `None`.

    Code called from inside `async with engines.session():` reaches the
    session through it instead of taking it as an argument. Inside nested
    sessions it returns the innermost one.

    Returns:
        The current session, or `None` outside every session.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import current_session, engines

        async with engines.session() as session:
            assert current_session() is session
        ```
    """
    return _CURRENT_SESSION.get()  # type: ignore[return-value]  # ty: ignore[invalid-return-type]


class EngineManager:
    """The registry of connections that sessions open on; use the `engines` instance."""

    def session(
        self,
        name: str | None = None,
        *,
        settings: Mapping[str, str] | None = None,
    ) -> Session:
        """Open a session on a connection, for use with `async with`.

        With `settings`, every operation in the session sends those Postgres
        settings first, so row-level security policies see them. Inside a
        `transaction()` block they follow its `BEGIN` once; outside one,
        each operation wraps itself in a transaction to carry them, which
        costs three extra round trips. A session without settings sends
        nothing extra. See
        [Round trips per operation](../concepts/row-level-security.md#round-trips-per-operation).

        Args:
            name: The connection to open on. Defaults to `None`, the default
                connection.
            settings: Postgres settings for every operation, as dotted
                custom names mapped to `str` values. Defaults to `None`, no
                settings beyond the ones an enclosing session passes on.

        Returns:
            A [`Session`][ferro.Session], not yet open: enter it with
                `async with`.

        Raises:
            TypeError: A settings key or value is not a `str`.
            ValueError: A settings key is not a dotted custom setting name.
            RuntimeError: On entering, a session that declares settings is
                on a connection that is not Postgres.

        Examples:
            ```python {test=run prelude=reference}
            from ferro import engines

            async with engines.session():
                post = await Post.where(lambda post: post.views > 1000).first()
            assert post.title == "Notes on the Analytical Engine"
            ```

            With settings, on Postgres:

            ```python {test=run prelude=invoices backend=postgres}
            async with engines.session(settings={"myapp.tenant_id": "acme"}):
                invoices = await Invoice.where(lambda invoice: invoice.total > 100).all()
            ```
        """
        return Session(connection_name=name, settings=settings)


engines = EngineManager()
"""The connections [`connect`][ferro.connect] registered: `engines.session(name)` opens a session on one."""
