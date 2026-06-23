"""Session-scoped runtime state for Ferro operations."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Any

from ._core import close_session as _core_close_session
from ._core import open_session as _core_open_session
from .state import _CURRENT_SESSION

_SESSION_CLOSE_AMBIENT_MISMATCH = (
    "Session close failed: ambient session does not match the closing session. "
    "This usually indicates session lifecycle misuse in the current asyncio context."
)


@dataclass(slots=True)
class Session:
    connection_name: str | None = None
    session_id: str | None = None
    _token: Any = field(default=None, repr=False, compare=False)
    _enter_context: contextvars.Context | None = field(
        default=None, repr=False, compare=False
    )
    _enter_task: asyncio.Task[Any] | None = field(
        default=None, repr=False, compare=False
    )

    async def __aenter__(self) -> "Session":
        self.session_id, resolved_name = _core_open_session(self.connection_name)
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
            if exc_type is not None:
                raise close_exc from exc
            raise

    async def close(self) -> None:
        """Close this session and release its runtime state.

        Safe to call from a different asyncio context than ``__aenter__``.
        Repeated calls are no-ops.

        Raises:
            RuntimeError: If the ambient session in this asyncio context does not
                match this handle (same-context lifecycle misuse).
        """
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

    def query(self, model_cls):
        from .query import Query

        return Query(model_cls, session=self)


class EngineManager:
    def session(self, name: str | None = None) -> Session:
        return Session(connection_name=name)


engines = EngineManager()
