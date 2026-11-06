from shapes import ModelD as Base, RelationD as Relation  # the only line that differs

from typing import assert_type

from ferro import Field
from shapes import Forward, Key, Reverse


class Team(Base):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list["Player"]] = Reverse("team_id")


class Player(Base):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    team_id: int = Key(Team, index=True)
    team: Relation[Team] = Forward("team_id")
    mentor_id: int | None = Key("Player", default=None, on_delete="SET NULL")
    mentor: Relation["Player | None"] = Forward("mentor_id")
    profile: Relation["Profile | None"] = Reverse("player_id")


class Profile(Base):
    id: int | None = Field(default=None, primary_key=True)
    bio: str
    player_id: int = Key(Player, unique=True)
    player: Relation[Player] = Forward("player_id")


async def cases(player: Player, team: Team) -> None:
    # 1. Key columns are plain typed fields.
    assert_type(player.team_id, int)
    assert_type(player.mentor_id, int | None)

    # 2. Loading a forward relation.
    assert_type(await player.team, Team)
    assert_type(await player.mentor, Player | None)

    # 3. Reverse relations: a collection, and a one-to-one.
    assert_type(await team.players.all(), list[Player])
    await team.players.where(lambda player: player.name == "Ferris").all()
    assert_type(await player.profile, Profile | None)

    # 4. Lambdas, through QueryProxy.
    await Player.where(lambda player: player.team.name == "Rustaceans").all()
    await Player.where(lambda player: player.team_id == team.id).count()

    # 5. Reading a relation that include() filled in, with no await.
    included = await Player.select().include(lambda player: player.team).all()
    for row in included:
        assert_type(row.team.name, str)
        mentor = row.mentor
        if mentor is not None:
            assert_type(mentor.name, str)

    # 6. Loading by the key column, which needs no relation at all.
    assert_type(await Team.get(player.team_id), Team)

    # 2d. Variant D's own spelling of the load.
    assert_type(await player.load(lambda player: player.team), Team)
    assert_type(await player.load(lambda player: player.mentor), Player | None)
    assert_type(await player.load(lambda player: player.profile), Profile | None)

    # 2d. The same load through the class attribute, which needs no lambda inference.
    assert_type(await Player.team.of(player), Team)
    assert_type(await Player.mentor.of(player), Player | None)
    assert_type(await Player.profile.of(player), Profile | None)

    # 2d. Masonite's split: the attribute is the value, `related()` is the query or the load.
    assert_type(await player.related(Player.team), Team)
    assert_type(await player.related(Player.mentor), Player | None)
    assert_type(await player.related(Player.profile), Profile | None)
    assert_type(await team.related(Team.players).where(lambda player: player.name == "Ferris").all(), list[Player])

    # 2d. Filling relations in after the fact, on one instance or on many.
    await player.populate(lambda player: player.team)
    assert_type(player.team.name, str)
    await Player.populate_all(included, lambda player: player.team, lambda player: player.mentor.team)
    await player.populate(lambda player: player.name == "x")
