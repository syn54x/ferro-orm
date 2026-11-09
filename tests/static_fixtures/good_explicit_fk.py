"""Type-checked (never executed): explicit foreign-key reads PASS `ty check` (#876).

test_static_contracts.py asserts this file produces no diagnostics: a
foreign-key column reads as its annotation, a forward relation as its
target, and a reverse relation as the query over the rows pointing back.
"""

from typing import assert_type

from ferro import Field, Forward, Model, Relation, Reverse


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list["Player"]] = Reverse("team_id")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    team_id: int = Field(foreign_key=Team, index=True)
    team: Relation[Team] = Forward("team_id")
    mentor_id: int | None = Field(
        default=None, foreign_key="Player", on_delete="SET NULL"
    )
    mentor: Relation["Player | None"] = Forward("mentor_id")


async def _explicit_foreign_key_reads(player: Player, team: Team) -> None:
    assert_type(player.team_id, int)
    assert_type(player.mentor_id, int | None)
    assert_type(player.team, Team)
    assert_type(player.mentor, Player | None)
    assert_type(await team.players.all(), list[Player])
    assert_type(Player.team, Relation[Team])
