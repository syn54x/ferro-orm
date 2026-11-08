"""A forward relation reads as the related instance: complete or unloaded.

```python
player = await Player.get(1)
player.team.id                     # 1, read from player.team_id, no query
player.team.name                   # InstanceNotLoaded: Team(id=1) is not loaded. Read it after
                                   # include(lambda player: player.team) or await player.team.load()
team = await player.team.load()    # fills the same object in place and returns it
```

Each test declares its own models in an empty registry, so the names in the
refusals (`Team`, `lambda player: player.team`) read as a user's would.
"""

import re
from types import SimpleNamespace
from typing import Annotated

import pytest

import ferro
from ferro import (
    BackRef,
    FerroField,
    Field,
    ForeignKey,
    Forward,
    InstanceNotLoaded,
    Model,
    ModelDoesNotExist,
    Relation,
    Reverse,
    clear_registry,
    engines,
    reset_engine,
)
from ferro.exceptions import FerroError
from ferro.registry import REGISTRY

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def _empty_registry():
    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()
    yield
    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


def _declare(form: str = "forward") -> SimpleNamespace:
    """Team, Player (a required team and a nullable mentor) and a one-to-one
    Profile, in the `Forward("team_id")` form or the old `ForeignKey` form."""
    if form == "forward":

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str
            players: Relation[list["Player"]] = Reverse("team_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str
            team_id: int = Field(foreign_key=Team)
            team: Relation[Team] = Forward("team_id")
            mentor_id: int | None = Field(
                default=None, foreign_key="Player", on_delete="SET NULL"
            )
            mentor: Relation["Player | None"] = Forward("mentor_id")
            profile: Relation[list["Profile"]] = Reverse("player_id")

        class Profile(Model):
            id: int | None = Field(default=None, primary_key=True)
            bio: str
            player_id: int = Field(foreign_key=Player, unique=True)
            player: Relation[Player] = Forward("player_id")

    else:

        class Team(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str
            players: Relation[list["Player"]] = BackRef()

        class Player(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str
            team: Annotated[Team, ForeignKey(related_name="players")]
            mentor: Annotated[
                "Player",
                ForeignKey(related_name="mentees", nullable=True, on_delete="SET NULL"),
            ] = None
            mentees: Relation[list["Player"]] = BackRef()
            profile: "Profile" = BackRef()

        class Profile(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            bio: str
            player: Annotated[Player, ForeignKey(related_name="profile", unique=True)]

    return SimpleNamespace(Team=Team, Player=Player, Profile=Profile)


async def _seed(db_url: str, form: str = "forward") -> SimpleNamespace:
    """Team 1 "Rustaceans" with Ferris (id 1) and Corro (id 2, no mentor)."""
    m = _declare(form)
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await m.Team.create(id=1, name="Rustaceans")
        await m.Player.create(id=1, name="Ferris", team_id=1)
        await m.Player.create(id=2, name="Corro", team_id=1)
    return m


def _statements(records) -> list[str]:
    return [record.sql for record in records]


@pytest.mark.asyncio
async def test_a_relation_nothing_included_reads_as_an_unloaded_instance_without_a_query(
    db_url, statement_records
):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        statement_records.clear()

        team = player.team

        assert isinstance(team, m.Team)
        assert team.id == player.team_id == 1
        assert _statements(statement_records) == []


@pytest.mark.asyncio
async def test_a_later_fetch_of_the_row_fills_the_unloaded_instance_in_place(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        team = player.team

        fetched = await m.Team.get(1)

        assert fetched is team is player.team
        assert team.name == "Rustaceans"


@pytest.mark.asyncio
async def test_an_include_over_the_same_row_fills_the_instance_read_before(db_url):
    m = await _seed(db_url)
    async with engines.session():
        ferris = await m.Player.get(1)
        team = ferris.team

        corro = await (
            m.Player.select()
            .include(lambda player: player.team)
            .where(lambda player: player.id == 2)
            .first()
        )

        assert corro.team is team
        assert team.name == "Rustaceans"


@pytest.mark.asyncio
async def test_load_fills_the_unloaded_instance_in_place_with_one_select(
    db_url, statement_records
):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        team = player.team
        statement_records.clear()

        loaded = await player.team.load()

        assert loaded is team is player.team
        assert loaded.name == "Rustaceans"
        statements = _statements(statement_records)
        assert len(statements) == 1 and statements[0].startswith("SELECT")


@pytest.mark.asyncio
async def test_a_team_created_in_the_session_is_the_relation_with_no_statement(
    db_url, statement_records
):
    m = _declare()
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        team = await m.Team.create(name="Rustaceans")
        player = await m.Player.create(name="Ferris", team_id=team.id)
        statement_records.clear()

        assert player.team is team
        assert player.team.name == "Rustaceans"
        assert _statements(statement_records) == []


@pytest.mark.asyncio
async def test_an_included_relation_reads_with_no_statement(db_url, statement_records):
    m = await _seed(db_url)
    async with engines.session():
        player = await (
            m.Player.select()
            .include(lambda player: player.team)
            .where(lambda player: player.id == 1)
            .first()
        )
        statement_records.clear()

        assert player.team.name == "Rustaceans"
        assert _statements(statement_records) == []


@pytest.mark.parametrize("form", ["forward", "foreign_key"])
@pytest.mark.asyncio
async def test_a_one_to_one_reverse_relation_reads_with_first(db_url, form):
    m = await _seed(db_url, form)
    async with engines.session():
        ferris = await m.Player.get(1)
        corro = await m.Player.get(2)
        await m.Profile.create(bio="Rust's mascot", player_id=1)

        profile = await ferris.profile.first()

        assert isinstance(profile, m.Profile) and profile.bio == "Rust's mascot"
        assert await corro.profile.first() is None
