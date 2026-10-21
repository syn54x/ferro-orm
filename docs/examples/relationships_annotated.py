"""Runnable companion to the Relationships guide (docs/pages/guide/relationships.md).

One roster runs through the whole page: teams have players, a player may
have a profile and a mentor, players enter tournaments, and a tournament
may have a winning team. Every section's block is a range of ``main()``,
in page order, so later sections build on the rows earlier ones added.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation


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


def table_ddl(report, table: str) -> list[str]:
    """The statements ``create_tables()`` ran for one table, in order."""
    return [
        statement.sql for statement in report.statements if statement.subject == table
    ]


async def main() -> None:
    # --8<-- [start:first]
    from ferro import connect, create_tables, engines

    await connect("sqlite::memory:")
    report = await create_tables()  # creates the tables and lists what it ran

    async with engines.session():
        rustaceans = await Team.create(name="Rustaceans")
        ferris = await Player.create(name="Ferris", team=rustaceans)

        team = await ferris.team  # forward: runs one query
        roster = await rustaceans.players.all()  # reverse: a query you run
        print(team.name, [player.name for player in roster])
        # --8<-- [end:first]
        assert (team.name, [player.name for player in roster]) == (
            "Rustaceans",
            ["Ferris"],
        )

        # --8<-- [start:one-to-many]
        corro = await Player.create(name="Corro", team=rustaceans)
        gophers = await Team.create(name="Gophers")
        gordon = await Player.create(name="Gordon", team=gophers)

        roster = await rustaceans.players.order_by(lambda player: player.name).all()
        assert [player.name for player in roster] == ["Corro", "Ferris"]
        # --8<-- [end:one-to-many]

        # --8<-- [start:shadow-column]
        assert ferris.team_id == rustaceans.id  # read without a query

        on_rustaceans = await Player.where(
            lambda player: player.team_id == rustaceans.id
        ).count()
        assert on_rustaceans == 2
        # --8<-- [end:shadow-column]

        # --8<-- [start:player-ddl]
        for statement in report.statements:
            if statement.subject == "player":
                print(statement.sql)
        # --8<-- [end:player-ddl]
        # The page shows these statements whole, on both databases
        # (tests/test_docs_sql.py); here, the names each relation gives them.
        player_ddl = "\n".join(table_ddl(report, "player"))
        assert 'CONSTRAINT "fk_player_mentor_id_player"' in player_ddl
        assert 'CONSTRAINT "fk_player_team_id_team"' in player_ddl
        assert '"idx_player_team_id" ON "player" ("team_id")' in player_ddl

        # --8<-- [start:populated]
        players = (
            await Player.select()
            .include(lambda player: player.team)
            .order_by(lambda player: player.name)
            .all()
        )
        for player in players:
            print(player.name, "|", player.team.name)  # no await
        # --8<-- [end:populated]
        assert [(player.name, player.team.name) for player in players] == [
            ("Corro", "Rustaceans"),
            ("Ferris", "Rustaceans"),
            ("Gordon", "Gophers"),
        ]

        # --8<-- [start:one-to-one]
        await Profile.create(bio="Plays in the front row", player=ferris)

        profile = await ferris.profile  # one instance, not a list
        assert profile.bio == "Plays in the front row"
        assert await corro.profile is None  # Corro has no profile
        # --8<-- [end:one-to-one]
        # unique=True gives the shadow column a unique index.
        assert (
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_profile_player_id" ON "profile" '
            '("player_id")'
        ) in table_ddl(report, "profile")

        # --8<-- [start:many-to-many]
        spring = await Tournament.create(name="Spring Open")
        autumn = await Tournament.create(name="Autumn Cup")

        await ferris.tournaments.add(spring, autumn)
        await gordon.tournaments.add(spring)

        entered = await ferris.tournaments.order_by(
            lambda tournament: tournament.name
        ).all()
        assert [tournament.name for tournament in entered] == [
            "Autumn Cup",
            "Spring Open",
        ]

        field = await spring.players.order_by(lambda player: player.name).all()
        assert [player.name for player in field] == ["Ferris", "Gordon"]
        # --8<-- [end:many-to-many]

        # --8<-- [start:m2m-remove]
        await ferris.tournaments.remove(autumn)
        entered = await ferris.tournaments.all()
        assert [tournament.name for tournament in entered] == ["Spring Open"]

        await ferris.tournaments.clear()
        assert await ferris.tournaments.count() == 0
        assert await spring.players.count() == 1  # Gordon is still entered
        # --8<-- [end:m2m-remove]

        # --8<-- [start:join-table-ddl]
        for statement in report.statements:
            if statement.subject == "player_tournaments":
                print(statement.sql)
        # --8<-- [end:join-table-ddl]
        link_ddl = "\n".join(table_ddl(report, "player_tournaments"))
        assert 'CONSTRAINT "fk_player_tournaments_player_id_player"' in link_ddl
        assert '"idx_player_tournaments_tournament_id_player_id"' in link_ddl
        assert '"uq_player_tournaments_player_id_tournament_id"' in link_ddl

        # --8<-- [start:self-referential]
        crabby = await Player.create(name="Crabby", team=rustaceans, mentor=ferris)

        assert (await crabby.mentor).name == "Ferris"
        mentees = await ferris.mentees.all()
        assert [player.name for player in mentees] == ["Crabby"]
        assert ferris.mentor_id is None  # Ferris has no mentor
        # --8<-- [end:self-referential]

        # --8<-- [start:nullable]
        assert autumn.winner_id is None  # no winner yet

        spring.winner_id = rustaceans.id  # set the shadow column, then save
        await spring.save()
        titles = await rustaceans.titles.all()
        assert [tournament.name for tournament in titles] == ["Spring Open"]
        # --8<-- [end:nullable]

        # --8<-- [start:on-delete]
        await rustaceans.delete()

        assert [player.name for player in await Player.select().all()] == ["Gordon"]
        assert await Profile.select().count() == 0
        assert (await Tournament.get(spring.id)).winner_id is None
        # --8<-- [end:on-delete]

    print("relationships example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
