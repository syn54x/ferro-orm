from shapes import ModelA as Base, RelationA as Relation  # the only line that differs

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
