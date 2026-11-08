"""Type-checked (never executed): relation assignment misuse FAILS `ty check` (#880).

test_static_contracts.py asserts exactly two `invalid-assignment`
diagnostics: an `int` is no `Team`, and `None` is refused where the
foreign-key column is required.
"""

from ferro import Field, Forward, Model, Relation


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)
    team: Relation[Team] = Forward("team_id")


def _misassign_relations(player: Player) -> None:
    player.team = 5
    player.team = None
