"""Type-checked (never executed): a keyword `Model.__init__` refuses FAILS `ty check` (#894).

A model's constructor parameters, to `ty`, are exactly the keywords the
constructor accepts. test_static_contracts.py asserts a `missing-argument`
naming `team_id` on each `Player` call, and an `unknown-argument` for a
forward relation, a reverse relation, a many-to-many, a generated column
and a retired field.
"""

from ferro import Field, Forward, ManyToMany, Model, Relation, Reverse, fn


class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list["Player"]] = Reverse("team_id")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    handle: str
    team_id: int = Field(foreign_key=Team)
    team: Relation[Team] = Forward("team_id")


class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str = ""


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    tags: Relation[list[Tag]] = ManyToMany(through="post_tags")


class Order(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount_minor: int = 0
    total: int = Field(generated=lambda order: fn.abs(order.amount_minor))


class Profile(Model):
    id: int | None = Field(default=None, primary_key=True)
    bio: str | None = Field(default=None, retired=True)


def _relation_keyword(team: Team) -> Player:
    return Player(handle="ferris", team=team)


def _missing_key() -> Player:
    return Player(handle="ferris")


def _reverse_relation_keyword() -> Team:
    return Team(name="Rustaceans", players=[])


def _many_to_many_keyword() -> Post:
    return Post(title="x", tags=[])


def _generated_column_keyword() -> Order:
    return Order(total=3)


def _retired_field_keyword() -> Profile:
    return Profile(bio="x")
