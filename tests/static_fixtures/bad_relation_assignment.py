"""Type-checked (never executed): relation assignment misuse FAILS `ty check` (#880).

test_static_contracts.py asserts exactly three `invalid-assignment`
diagnostics: an `int` is no `Team`, `None` is refused where the foreign-key
column is required, and a reverse relation is a query, never assigned.
"""

from ferro import Field, Forward, Model, Relation, Reverse


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list["Player"]] = Reverse("team_id")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)
    team: Relation[Team] = Forward("team_id")


def _misassign_relations(player: Player, team: Team) -> None:
    player.team = 5
    player.team = None
    team.players = [player]
