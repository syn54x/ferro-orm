"""A forward relation that reads as a query, like a reverse or many-to-many one does today.

Uses Ferro's real `Relation` class, unchanged.
"""

from __future__ import annotations

from typing import assert_type

from ferro import Field, Model, Relation
from shapes import Forward, Key, Reverse


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list[Player]] = Reverse("team_id")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    team_id: int = Key(Team, index=True)
    team: Relation[Team] = Forward("team_id")
    mentor_id: int | None = Key("Player", default=None, on_delete="SET NULL")
    mentor: Relation[Player | None] = Forward("mentor_id")
    profile: Relation[Profile | None] = Reverse("player_id")


class Profile(Model):
    id: int | None = Field(default=None, primary_key=True)
    bio: str
    player_id: int = Key(Player, unique=True)


async def cases(player: Player, team: Team) -> None:
    # Every relation is a query, so the read is a terminal you await.
    assert_type(await player.team.first(), Team | None)
    reveal_type(await player.mentor.first())
    reveal_type(await player.profile.first())
    assert_type(await team.players.all(), list[Player])

    # Query methods a single row has no use for are all there.
    reveal_type(await player.team.all())
    await player.team.where(lambda team: team.name == "Rustaceans").count()
    await player.team.delete()

    # The included read has nowhere to live.
    included = await Player.select().include(lambda player: player.team).all()
    for row in included:
        row.team.name
