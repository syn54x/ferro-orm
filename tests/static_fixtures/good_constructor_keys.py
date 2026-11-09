"""Type-checked (never executed): a construction `Model.__init__` accepts PASSES `ty check` (#894).

test_static_contracts.py asserts this file produces no diagnostics. Each
call passes every required `Field(...)` column and leaves out what the
constructor does not take or does not need: a generated column, a retired
field, relations, a `default=` field, a `default_factory=` field, a
`primary_key=True, default=None` key, a `pydantic.Field(default=...)`
field and a `PrivateAttr`.
"""

from datetime import datetime
from typing import assert_type

import pydantic
from pydantic import PrivateAttr

from ferro import Field, Forward, ManyToMany, Model, Relation, Reverse, fn


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=40)
    players: Relation[list["Player"]] = Reverse("team_id")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    handle: str = Field(unique=True)
    team_id: int = Field(foreign_key=Team, index=True)
    team: Relation[Team] = Forward("team_id")
    rating: int = Field(default=1000, index=True)
    joined_at: datetime = Field(default_factory=datetime.now)
    nickname: str = pydantic.Field(default="")
    score: int = Field(generated=lambda player: fn.abs(player.rating))
    bio: str | None = Field(default=None, retired=True)
    _session_note: str = PrivateAttr(default="")


class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    posts: Relation[list["Post"]] = Reverse("tags")


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    tags: Relation[list[Tag]] = ManyToMany(through="post_tags")


def _constructions() -> None:
    team = Team(name="Rustaceans")
    player = Player(handle="ferris", team_id=1)
    Tag(label="rust")
    Post(title="Ownership")
    assert_type(player.team, Team)
    assert_type(team.players, Relation[list[Player]])


def _many_to_many_takes_its_documented_options() -> None:
    ManyToMany(through="post_tags", reverse_index=False)
