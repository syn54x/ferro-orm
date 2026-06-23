"""Session-scoped runtime state for Ferro operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._core import close_session as _core_close_session
from ._core import open_session as _core_open_session
from .state import _CURRENT_SESSION


@dataclass(slots=True)
class Session:
    connection_name: str | None = None
    session_id: str | None = None
    _token: Any = None

    async def __aenter__(self) -> "Session":
        self.session_id, resolved_name = _core_open_session(self.connection_name)
        if self.connection_name is None:
            self.connection_name = resolved_name
        self._token = _CURRENT_SESSION.set(self)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _CURRENT_SESSION.reset(self._token)
            self._token = None
        if self.session_id is not None:
            _core_close_session(self.session_id)
            self.session_id = None

    def query(self, model_cls):
        from .query import Query

        return Query(model_cls, session=self)


class EngineManager:
    def session(self, name: str | None = None) -> Session:
        return Session(connection_name=name)


engines = EngineManager()
