"""Type-checked (never executed): relation assignment PASSES `ty check` (#880).

test_static_contracts.py asserts this file produces no diagnostics: a
forward relation takes its target, an unloaded relation read from another
instance, and `None` where the foreign-key column is nullable.
"""

from ferro import Field, Forward, Model, Relation


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)
    team: Relation[Team] = Forward("team_id")
    mentor_id: int | None = Field(
        default=None, foreign_key="Player", on_delete="SET NULL"
    )
    mentor: Relation["Player | None"] = Forward("mentor_id")


def _assign_relations(player: Player, other: Player, team: Team) -> None:
    player.team = team
    player.team = other.team
    player.mentor = other
    player.mentor = None
