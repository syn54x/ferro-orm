"""Relations declared as decorated methods, Masonite style, over variant D's read."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Generic, Self, TypeVar, assert_type, overload

from ferro import Field, Model
from ferro.query import Query
from shapes import Key

T = TypeVar("T")


class One(Generic[T]):
    """What `@belongs_to` makes: reads as the target itself."""

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: object, owner: type) -> T: ...
    def __get__(self, instance: Any, owner: Any) -> Any:
        raise NotImplementedError


class MaybeOne(Generic[T]):
    """What a nullable `@belongs_to` or a `@has_one` makes: the target or `None`."""

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: object, owner: type) -> T | None: ...
    def __get__(self, instance: Any, owner: Any) -> Any:
        raise NotImplementedError


class Many(Generic[T]):
    """What `@has_many` makes: reads as a query."""

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: object, owner: type) -> Query[T]: ...
    def __get__(self, instance: Any, owner: Any) -> Any:
        raise NotImplementedError


def belongs_to(column: str) -> Callable[[Callable[[Any], type[T]]], One[T]]:
    raise NotImplementedError


def belongs_to_optional(column: str) -> Callable[[Callable[[Any], type[T]]], MaybeOne[T]]:
    raise NotImplementedError


def has_one(column: str) -> Callable[[Callable[[Any], type[T]]], MaybeOne[T]]:
    raise NotImplementedError


def has_many(column: str) -> Callable[[Callable[[Any], type[T]]], Many[T]]:
    raise NotImplementedError


class Base(Model):
    @overload
    def related(self, relation: Many[T]) -> Query[T]: ...
    @overload
    def related(self, relation: MaybeOne[T]) -> Awaitable[T | None]: ...
    @overload
    def related(self, relation: One[T]) -> Awaitable[T]: ...
    def related(self, relation: Any) -> Any:
        raise NotImplementedError


class Team(Base):
    id: int | None = Field(default=None, primary_key=True)
    name: str

    @has_many("team_id")
    def players(self) -> type[Player]:
        return Player


class Player(Base):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    team_id: int = Key(Team, index=True)
    mentor_id: int | None = Key("Player", default=None, on_delete="SET NULL")

    @belongs_to("team_id")
    def team(self) -> type[Team]:
        return Team

    @belongs_to_optional("mentor_id")
    def mentor(self) -> type[Player]:
        return Player

    @has_one("player_id")
    def profile(self) -> type[Profile]:
        return Profile

    # The same declaration with no return annotation and with an empty body.
    @belongs_to("team_id")
    def team_unannotated(self):
        return Team

    @belongs_to("team_id")
    def team_empty(self) -> type[Team]: ...


class Profile(Base):
    id: int | None = Field(default=None, primary_key=True)
    bio: str
    player_id: int = Key(Player, unique=True)


async def cases(player: Player, team: Team) -> None:
    # The included read.
    assert_type(player.team.name, str)
    mentor = player.mentor
    if mentor is not None:
        assert_type(mentor.name, str)

    # The load, Masonite's related().
    assert_type(await player.related(Player.team), Team)
    assert_type(await player.related(Player.mentor), Player | None)
    assert_type(await player.related(Player.profile), Profile | None)
    assert_type(await team.players.all(), list[Player])

    # Lambdas are unchanged.
    await Player.where(lambda player: player.team.name == "Rustaceans").all()

    # What ty makes of the unannotated one, and of the old spelling.
    reveal_type(player.team_unannotated)
    await player.team
