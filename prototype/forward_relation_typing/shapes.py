"""PROTOTYPE for #845. Never merges, has no runtime, exists to be checked by ty.

Question: can a forward relation that names its key column be typed under ty
with no ignore comments, and what does `player.team` have to be for that?

Three candidate types for what `player.team` reads as, one per variant:

  A  an awaitable of the target             (`await player.team`)
  B  the target itself, and models can be awaited (Tortoise's approach)
  C  a handle with two doors                (`await player.team`, `player.team.loaded`)

`cases_a.py`, `cases_b.py` and `cases_c.py` hold the same models and the same
call sites; only their first import line differs. Run:

    uv run --no-sync ty check prototype/forward_relation_typing

Every name here (`Key`, `Forward`, `Reverse`, `loaded`) is a placeholder.
"""

from __future__ import annotations

from collections.abc import Awaitable, Generator
from typing import Any, Generic, Literal, Self, TypeVar, overload

from ferro import Model
from ferro.query import Query

T = TypeVar("T")
E = TypeVar("E")


def Key(
    to: Any, *, default: Any = ..., index: bool = False, unique: bool = False, on_delete: str = "CASCADE"
) -> Any:
    """The explicit foreign-key column: `team_id: int = Key(Team, index=True)`."""


def Forward(column: str) -> Any:
    """A forward relation over a key column on this model."""


def Reverse(column: str) -> Any:
    """A reverse relation over a key column on the other model."""


# --- Variant A: a forward relation reads as an awaitable -------------------


class RelationA(Generic[T]):
    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self: RelationA[list[E]], instance: object, owner: type) -> Query[E]: ...
    @overload
    def __get__(self: RelationA[E], instance: object, owner: type) -> Awaitable[E]: ...
    def __get__(self, instance: Any, owner: Any) -> Any: ...


ModelA = Model


# --- Variant B: a forward relation reads as the target, which is awaitable --


class RelationB(Generic[T]):
    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self: RelationB[list[E]], instance: object, owner: type) -> Query[E]: ...
    @overload
    def __get__(self: RelationB[E], instance: object, owner: type) -> E: ...
    def __get__(self, instance: Any, owner: Any) -> Any: ...


class ModelB(Model):
    def __await__(self) -> Generator[Any, None, Self]:
        raise NotImplementedError


# --- Variant C: a forward relation reads as a handle with two doors ---------


class Ref(Generic[T]):
    """What `player.team` is: await it to load, or read `.loaded` after include()."""

    def __await__(self) -> Generator[Any, None, T]:
        raise NotImplementedError

    @property
    def loaded(self) -> T:
        """The included value. Raises, naming `include()`, when it was not included."""
        raise NotImplementedError


class RelationC(Generic[T]):
    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self: RelationC[list[E]], instance: object, owner: type) -> Query[E]: ...
    @overload
    def __get__(self: RelationC[E], instance: object, owner: type) -> Ref[E]: ...
    def __get__(self, instance: Any, owner: Any) -> Any: ...


ModelC = Model


# --- Variant B2: B, with a stand-in for "no row" so a nullable one can be awaited


class Missing:
    """What a nullable relation would read as instead of `None`: falsy, awaits to `None`."""

    def __await__(self) -> Generator[Any, None, None]:
        raise NotImplementedError

    def __bool__(self) -> Literal[False]:
        raise NotImplementedError


class RelationB2(Generic[T]):
    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self: RelationB2[list[E]], instance: object, owner: type) -> Query[E]: ...
    @overload
    def __get__(self: RelationB2[E | None], instance: object, owner: type) -> E | Missing: ...
    @overload
    def __get__(self: RelationB2[E], instance: object, owner: type) -> E: ...
    def __get__(self, instance: Any, owner: Any) -> Any: ...
