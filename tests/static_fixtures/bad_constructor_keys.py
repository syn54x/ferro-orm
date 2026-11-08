"""Type-checked (never executed): a relation keyword in a constructor FAILS `ty check` (#881).

`team_id` is a required field, so `ty` reports the missing key whatever the
relation keyword carries. test_static_contracts.py asserts a
`missing-argument` diagnostic naming `team_id`.
"""

from ferro import Field, Forward, Model, Relation, Reverse


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list["Player"]] = Reverse("team_id")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    handle: str
    team_id: int = Field(foreign_key=Team)
    team: Relation[Team] = Forward("team_id")


def _construct_with_the_relation(team: Team) -> Player:
    return Player(handle="ferris", team=team)
