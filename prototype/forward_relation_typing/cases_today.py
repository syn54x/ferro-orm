"""The baseline: today's declarations, with the two idioms #290 reports."""

from typing import Annotated, assert_type

from ferro import BackRef, Field, ForeignKey, Model, Relation


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list["Player"]] = BackRef()


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    team: Annotated[Team, ForeignKey(related_name="players", index=True)]


async def cases(player: Player) -> None:
    assert_type(player.team_id, int)
    assert_type(await player.team, Team)
    assert_type(player.team.name, str)
