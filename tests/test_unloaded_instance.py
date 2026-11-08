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


@pytest.mark.asyncio
async def test_without_a_session_two_reads_return_one_object_cached_on_the_player(
    db_url,
):
    m = await _seed(db_url)
    player = await m.Player.using("default").get(1)

    first = player.team

    assert isinstance(first, m.Team) and first.id == 1
    assert player.team is first


@pytest.mark.asyncio
async def test_a_null_key_reads_as_none_and_builds_nothing(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)

        assert player.mentor is None
        assert "mentor" not in vars(player)


@pytest.mark.asyncio
async def test_load_on_a_complete_instance_sends_no_statement(
    db_url, statement_records
):
    m = await _seed(db_url)
    async with engines.session():
        team = await m.Team.get(1)
        statement_records.clear()

        assert await team.load() is team
        assert _statements(statement_records) == []


@pytest.mark.asyncio
async def test_an_unloaded_instance_reprs_as_its_key_and_unloaded(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)

        assert repr(player.team) == "Team(id=1, unloaded)"


@pytest.mark.asyncio
async def test_getattr_with_a_default_and_hasattr_see_an_unloaded_column_as_absent(
    db_url,
):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)

        assert getattr(player.team, "name", None) is None
        assert hasattr(player.team, "name") is False


@pytest.mark.asyncio
async def test_delete_on_an_unloaded_instance_deletes_its_row(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)

        await player.team.delete()

        assert await m.Team.get_or_none(1) is None


@pytest.mark.asyncio
async def test_position_of_treats_an_unloaded_relation_as_not_included(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        assert player.team.id == 1  # the unloaded instance is now cached

        query = (
            m.Player.select()
            .order_by(lambda player: player.team.name)
            .order_by(lambda player: player.id)
        )
        with pytest.raises(ValueError, match="requires relation path"):
            query.position_of(player)


_NOT_LOADED = (
    "Team(id=1) is not loaded. Read it after "
    "include(lambda player: player.team) or await player.team.load()"
)


def _refusals(team) -> dict[str, object]:
    """Every use an unloaded instance refuses, each as a zero-argument call."""

    def assign():
        team.name = "Ferrous"

    return {
        "read a column": lambda: team.name,
        "assign a column": assign,
        "model_dump()": team.model_dump,
        "model_dump_json()": team.model_dump_json,
    }


@pytest.mark.parametrize("form", ["forward", "foreign_key"])
@pytest.mark.asyncio
async def test_an_unloaded_instance_refuses_every_use_but_its_key_naming_the_fix(
    db_url, form
):
    m = await _seed(db_url, form)
    async with engines.session():
        player = await m.Player.get(1)
        team = player.team
        assert repr(team) == "Team(id=1, unloaded)"

        for use, call in _refusals(team).items():
            with pytest.raises(InstanceNotLoaded) as refusal:
                call()
            assert str(refusal.value) == _NOT_LOADED, use
            assert isinstance(refusal.value, AttributeError)
            assert isinstance(refusal.value, FerroError)
        with pytest.raises(InstanceNotLoaded, match=re.escape(_NOT_LOADED)):
            await team.save()

        # Nothing was written to the instance or the row.
        assert "name" not in vars(team)
        assert (await m.Team.get(1)).name == "Rustaceans"


def _printed_fixes(message: str) -> tuple[str, str]:
    """The include() lambda and the load line a refusal prints."""
    found = re.search(r"include\((lambda .+?)\) or (await .+\.load\(\))$", message)
    assert found, message
    return found.group(1), found.group(2)


@pytest.mark.parametrize("form", ["forward", "foreign_key"])
@pytest.mark.asyncio
async def test_each_fix_the_refusal_prints_loads_the_relation_when_pasted_back(
    db_url, form
):
    m = await _seed(db_url, form)
    async with engines.session():
        player = await m.Player.get(1)
        with pytest.raises(InstanceNotLoaded) as refusal:
            player.team.name
        selector, load_line = _printed_fixes(str(refusal.value))

        loaded = await eval(load_line.removeprefix("await "), {"player": player})
        assert loaded.name == "Rustaceans"

    async with engines.session():
        included = await (
            m.Player.select()
            .include(eval(selector))
            .where(lambda player: player.id == 2)
            .first()
        )
        assert included.team.name == "Rustaceans"


@pytest.mark.asyncio
async def test_a_two_word_model_names_its_lambda_parameter_in_snake_case(db_url):
    class SquadLeague(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str

    class SquadMember(Model):
        id: int | None = Field(default=None, primary_key=True)
        squad_league_id: int = Field(foreign_key=SquadLeague)
        squad_league: Relation[SquadLeague] = Forward("squad_league_id")

    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await SquadLeague.create(id=1, title="Premier")
        await SquadMember.create(id=1, squad_league_id=1)
    async with engines.session():
        squad_member = await SquadMember.get(1)
        with pytest.raises(InstanceNotLoaded) as refusal:
            squad_member.squad_league.title
        assert str(refusal.value) == (
            "SquadLeague(id=1) is not loaded. Read it after "
            "include(lambda squad_member: squad_member.squad_league) or "
            "await squad_member.squad_league.load()"
        )
        selector, load_line = _printed_fixes(str(refusal.value))
        loaded = await eval(
            load_line.removeprefix("await "), {"squad_member": squad_member}
        )
        assert loaded.title == "Premier"

    async with engines.session():
        included = await SquadMember.select().include(eval(selector)).first()
        assert included.squad_league.title == "Premier"
