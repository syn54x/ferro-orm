"""Annotated-style companion to relationships.py (AGENTS.md I-7).

The roster's field options move into ``Annotated[...]``. The relationship
declarations are the same in both styles: forward foreign keys are always
``Annotated[Target, ForeignKey(...)]`` and ``BackRef()``/``ManyToMany()``
are always assignments. ``main()`` checks this declaration creates the same
tables as the assignment one.
"""

import asyncio
from typing import Annotated

from ferro import (
    BackRef,
    Field,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
    connect,
    create_tables,
    engines,
)


# --8<-- [start:models]
class Team(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    players: Relation[list["Player"]] = BackRef()
    titles: Relation[list["Tournament"]] = BackRef()


class Player(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    team: Annotated[Team, ForeignKey(related_name="players", index=True)]
    mentor: Annotated[
        "Player",
        ForeignKey(related_name="mentees", nullable=True, on_delete="SET NULL"),
    ] = None
    mentees: Relation[list["Player"]] = BackRef()
    profile: "Profile" = BackRef()
    tournaments: Relation[list["Tournament"]] = ManyToMany(related_name="players")


class Profile(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    bio: str
    player: Annotated[Player, ForeignKey(related_name="profile", unique=True)]


class Tournament(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    winner: Annotated[
        Team | None, ForeignKey(related_name="titles", on_delete="SET NULL")
    ] = None
    players: Relation[list[Player]] = BackRef()


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:")
    report = await create_tables()
    assert [statement.subject for statement in report.statements] == [
        "team",
        "tournament",
        "player",
        "player",
        "profile",
        "profile",
        "player_tournaments",
        "player_tournaments",
        "player_tournaments",
    ]

    async with engines.session():
        rustaceans = await Team.create(name="Rustaceans")
        ferris = await Player.create(name="Ferris", team=rustaceans)
        crabby = await Player.create(name="Crabby", team=rustaceans, mentor=ferris)
        await Profile.create(bio="Plays in the front row", player=ferris)
        spring = await Tournament.create(name="Spring Open")
        await ferris.tournaments.add(spring)

        assert (await crabby.mentor).name == "Ferris"
        assert (await ferris.profile).bio == "Plays in the front row"
        assert [player.name for player in await spring.players.all()] == ["Ferris"]

    print("relationships_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
