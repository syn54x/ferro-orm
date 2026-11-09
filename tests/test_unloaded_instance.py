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
from contextlib import closing
from enum import Enum
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import ClassVar
from uuid import UUID

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    model_serializer,
)
from pydantic_core import PydanticSerializationError

import ferro
from ferro import (
    Field,
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


def _declare() -> SimpleNamespace:
    """Team, Player (a required team and a nullable mentor) and a one-to-one
    Profile."""

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

    return SimpleNamespace(Team=Team, Player=Player, Profile=Profile)


async def _seed(db_url: str) -> SimpleNamespace:
    """Team 1 "Rustaceans" with Ferris (id 1) and Corro (id 2, no mentor)."""
    m = _declare()
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


@pytest.mark.asyncio
async def test_a_one_to_one_reverse_relation_reads_with_first(db_url):
    m = await _seed(db_url)
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
async def test_getattr_with_a_default_and_hasattr_refuse_an_unloaded_column(db_url):
    """`InstanceNotLoaded` is no `AttributeError`, so neither swallows it as a
    missing attribute; a name that is no column is still a plain one."""
    m = await _seed(db_url)
    async with engines.session():
        team = (await m.Player.get(1)).team

        with pytest.raises(InstanceNotLoaded, match=re.escape(_NOT_LOADED)):
            getattr(team, "name", None)
        with pytest.raises(InstanceNotLoaded, match=re.escape(_NOT_LOADED)):
            hasattr(team, "name")
        assert not issubclass(InstanceNotLoaded, AttributeError)

        assert getattr(team, "_repr_html_", None) is None
        assert hasattr(team, "nickname") is False
        with pytest.raises(AttributeError) as missing:
            team.nickname
        assert not isinstance(missing.value, InstanceNotLoaded)


@pytest.mark.parametrize("default", [True, False], ids=["defaulted", "required"])
@pytest.mark.asyncio
async def test_a_from_attributes_model_refuses_an_unloaded_instance(db_url, default):
    """Pydantic reads each field with `getattr` and treats an `AttributeError`
    as a missing attribute; the refusal is none, so it surfaces with its fix."""
    if default:

        class TeamOut(BaseModel):
            model_config = ConfigDict(from_attributes=True)
            id: int
            name: str = "unknown"

    else:

        class TeamOut(BaseModel):
            model_config = ConfigDict(from_attributes=True)
            id: int
            name: str

    m = await _seed(db_url)
    async with engines.session():
        team = (await m.Player.get(1)).team

        with pytest.raises(ValidationError) as refused:
            TeamOut.model_validate(team)

        assert [error["type"] for error in refused.value.errors()] == [
            "get_attribute_error"
        ]
        assert f"InstanceNotLoaded: {_NOT_LOADED}" in str(refused.value)
        await team.load()
        assert TeamOut.model_validate(team) == TeamOut(id=1, name="Rustaceans")


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
        "iter()": lambda: iter(team),
        "dict()": lambda: dict(team),
    }


@pytest.mark.asyncio
async def test_an_unloaded_instance_refuses_every_use_but_its_key_naming_the_fix(
    db_url,
):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        team = player.team
        assert repr(team) == "Team(id=1, unloaded)"

        for use, call in _refusals(team).items():
            with pytest.raises(InstanceNotLoaded) as refusal:
                call()
            assert str(refusal.value) == _NOT_LOADED, use
            assert not isinstance(refusal.value, AttributeError)
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


@pytest.mark.asyncio
async def test_each_fix_the_refusal_prints_loads_the_relation_when_pasted_back(
    db_url,
):
    m = await _seed(db_url)
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


@pytest.mark.asyncio
async def test_load_on_a_gone_row_raises_model_does_not_exist(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        team = player.team
        await m.Team.where(lambda team: team.id == 1).delete()

        with pytest.raises(ModelDoesNotExist) as missing:
            await team.load()

        assert (missing.value.model, missing.value.pk) == (m.Team, 1)


@pytest.mark.asyncio
async def test_refresh_on_a_gone_row_raises_model_does_not_exist(db_url):
    m = await _seed(db_url)
    async with engines.session():
        team = await m.Team.get(1)
        await m.Team.where(lambda team: team.id == 1).delete()

        with pytest.raises(ModelDoesNotExist):
            await team.refresh()


@pytest.mark.asyncio
async def test_awaiting_a_relation_raises_type_error(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)

        with pytest.raises(TypeError):
            await player.team


_FOUND_NO_ROW = (
    "Team(id=99) is not loaded: the query that included player.team found no "
    "row with that key"
)


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_an_include_that_finds_no_row_leaves_the_relation_unloaded(db_url):
    m = await _seed(db_url)
    # Foreign keys are unenforced on a plain sqlite3 connection, so the key can
    # point at no row, as it can after a delete with enforcement off.
    database = db_url.removeprefix("sqlite:").split("?")[0]
    with closing(sqlite3.connect(database)) as raw, raw:
        raw.execute("INSERT INTO player (id, name, team_id) VALUES (3, 'Gordon', 99)")
    async with engines.session():
        players = await (
            m.Player.select()
            .include(lambda player: player.team)
            .order_by(lambda player: player.id)
            .all()
        )

        assert [player.id for player in players] == [1, 2, 3]
        gordon = players[2]
        assert gordon.team.id == 99
        assert repr(gordon.team) == "Team(id=99, unloaded)"
        with pytest.raises(InstanceNotLoaded) as refusal:
            gordon.team.name
        assert str(refusal.value).startswith(_FOUND_NO_ROW)
        team = gordon.team
        with pytest.raises(ModelDoesNotExist):
            await team.load()

        # The failed load leaves the instance the session's object for its key.
        again = await (
            m.Player.select()
            .include(lambda player: player.team)
            .where(lambda player: player.id == 3)
            .first()
        )
        assert again.team is team

        # The refusal's step works once the row is back.
        found = re.search(r"then (await \S+\.load\(\))$", str(refusal.value))
        assert found, str(refusal.value)
        _raw_sqlite(db_url, "INSERT INTO team (id, name) VALUES (99, 'Gophers')")
        loaded = await eval(found.group(1).removeprefix("await "), {"player": gordon})
        assert loaded is team and team.name == "Gophers"


def _raw_sqlite(db_url: str, statement: str) -> None:
    """Run one statement on a plain sqlite3 connection, where foreign keys are
    unenforced and the session's identity map is never told."""
    database = db_url.removeprefix("sqlite:").split("?")[0]
    with closing(sqlite3.connect(database)) as raw, raw:
        raw.execute(statement)


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_an_include_that_misses_a_row_the_session_holds_keeps_one_object(
    db_url,
):
    m = await _seed(db_url)
    async with engines.session():
        held = await m.Team.create(id=99, name="Gophers")
        await m.Player.create(id=3, name="Gordon", team_id=99)
        _raw_sqlite(db_url, "DELETE FROM team WHERE id = 99")

        gordon = await (
            m.Player.select()
            .include(lambda player: player.team)
            .where(lambda player: player.id == 3)
            .first()
        )
        assert gordon.team is held

        _raw_sqlite(db_url, "INSERT INTO team (id, name) VALUES (99, 'Gophers II')")
        assert await m.Team.get(99) is held is gordon.team
        assert held.name == "Gophers II"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_an_include_whose_row_row_security_hides_leaves_the_relation_unloaded(
    db_url, pg_role
):
    from ferro import PoolConfig, RowPolicy, RowSecurity

    from tests.test_rls_end_to_end import (
        LEDGER_A,
        LEDGER_B,
        LEDGER_KEY,
        _drop_tenant_role,
        _grant_tenant_role,
        _tenant_url,
    )

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        ledger_id: UUID

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting=LEDGER_KEY)
        )

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")

    role = pg_role("unloaded")
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await Team.create(id=1, name="Rustaceans", ledger_id=LEDGER_A)
        await Team.create(id=99, name="Gophers", ledger_id=LEDGER_B)
        await Player.create(id=1, name="Ferris", team_id=1)
        await Player.create(id=2, name="Gordon", team_id=99)
    try:
        async with engines.session():
            await _grant_tenant_role(role, ("team", "player"))
        await ferro.connect(
            _tenant_url(db_url, role),
            name="tenant",
            pool=PoolConfig(max_connections=1, settings_delivery="transaction"),
        )
        async with engines.session("tenant", settings={LEDGER_KEY: str(LEDGER_A)}):
            players = await (
                Player.select()
                .include(lambda player: player.team)
                .order_by(lambda player: player.id)
                .all()
            )

            assert [player.id for player in players] == [1, 2]
            assert players[0].team.name == "Rustaceans"
            gordon = players[1]
            assert gordon.team.id == 99
            with pytest.raises(InstanceNotLoaded) as refusal:
                gordon.team.name
            assert str(refusal.value).startswith(_FOUND_NO_ROW)
            team = gordon.team
            with pytest.raises(ModelDoesNotExist):
                await team.load()
            again = await (
                Player.select()
                .include(lambda player: player.team)
                .where(lambda player: player.id == 2)
                .first()
            )
            assert again.team is team

            # Restore this connection's access, then paste the refusal's step.
            async with engines.session():
                await Team.where(lambda team: team.id == 99).update(ledger_id=LEDGER_A)
            found = re.search(r"then (await \S+\.load\(\))$", str(refusal.value))
            assert found, str(refusal.value)
            loaded = await eval(
                found.group(1).removeprefix("await "), {"player": gordon}
            )
            assert loaded is team and team.name == "Gophers"
    finally:
        async with engines.session():
            await _drop_tenant_role(role)


def test_one_module_owns_the_unloaded_marker():
    """Every Python site reads the unloaded state through `ferro._unloaded`, so
    the marker's name and the layout the hydrator writes change in one place."""
    package = Path(ferro.__file__).parent
    marker = re.compile(r"__ferro_unloaded|UNLOADED_ATTR|_unloaded\.MARKER")
    naming = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if marker.search(path.read_text())
    )

    assert naming == ["_unloaded.py"]


@pytest.mark.asyncio
async def test_every_pydantic_serializer_refuses_an_unloaded_instance(db_url):
    """A nested model, a `TypeAdapter` and a response model serialize through
    pydantic-core, never through the instance's own `model_dump()`.
    pydantic-core wraps whatever a serializer raises in its own error, so the
    refusal arrives as a `PydanticSerializationError` carrying its message."""
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        team = player.team

        class PlayerOut(BaseModel):
            team: m.Team

        nested = PlayerOut(team=team)
        for use, call in {
            "nested model_dump()": nested.model_dump,
            "nested model_dump_json()": nested.model_dump_json,
            "TypeAdapter.dump_python": lambda: TypeAdapter(m.Team).dump_python(team),
            "TypeAdapter.dump_json": lambda: TypeAdapter(list[m.Team]).dump_json(
                [team]
            ),
        }.items():
            with pytest.raises(PydanticSerializationError) as refusal:
                call()
            assert f"InstanceNotLoaded: {_NOT_LOADED}" in str(refusal.value), use

        await team.load()
        assert PlayerOut(team=team).model_dump() == {
            "team": {"id": 1, "name": "Rustaceans"}
        }


@pytest.mark.parametrize("mode", ["wrap", "plain"])
@pytest.mark.asyncio
async def test_a_model_serializer_of_its_own_still_refuses_an_unloaded_instance(
    db_url, mode
):
    """A model may declare its own `model_serializer`; Pydantic keeps one per
    class, and the refusal does not depend on it being Ferro's."""
    if mode == "wrap":

        class Badge(Model):
            id: int | None = Field(default=None, primary_key=True)
            label: str

            @model_serializer(mode="wrap")
            def _shouted(self, handler):
                return {
                    name: value.upper() if isinstance(value, str) else value
                    for name, value in handler(self).items()
                }

    else:

        class Badge(Model):
            id: int | None = Field(default=None, primary_key=True)
            label: str

            @model_serializer(mode="plain")
            def _shouted(self):
                values = {
                    name: self.__dict__.get(name) for name in type(self).model_fields
                }
                return {
                    name: value.upper() if isinstance(value, str) else value
                    for name, value in values.items()
                }

    class Holder(Model):
        id: int | None = Field(default=None, primary_key=True)
        badge_id: int = Field(foreign_key=Badge)
        badge: Relation[Badge] = Forward("badge_id")

    class HolderOut(BaseModel):
        badge: Badge

    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await Badge.create(id=1, label="Founder")
        await Holder.create(id=1, badge_id=1)
    async with engines.session():
        badge = (await Holder.get(1)).badge

        for use, call in {
            "nested model_dump()": lambda: HolderOut(badge=badge).model_dump(),
            "TypeAdapter.dump_json": lambda: TypeAdapter(Badge).dump_json(badge),
        }.items():
            with pytest.raises(PydanticSerializationError) as refusal:
                call()
            assert "InstanceNotLoaded: Badge(id=1) is not loaded" in str(
                refusal.value
            ), use

        await badge.load()
        assert HolderOut(badge=badge).model_dump() == {
            "badge": {"id": 1, "label": "FOUNDER"}
        }


@pytest.mark.asyncio
async def test_model_copy_refuses_a_relation_in_update_as_the_constructor_does(
    db_url,
):
    m = await _seed(db_url)
    async with engines.session():
        gophers = await m.Team.create(id=2, name="Gophers")
        player = await m.Player.get(1)

        with pytest.raises(TypeError) as refused:
            player.model_copy(update={"team": gophers})

        assert str(refused.value) == (
            "Player.team is a relation. Pass the foreign-key column's value, "
            "team_id=team.id"
        )


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_fill_that_fails_to_convert_a_value_leaves_the_instance_unloaded(
    db_url,
):
    class Tier(str, Enum):
        GOLD = "gold"

    class Club(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        tier: Tier

    class Member(Model):
        id: int | None = Field(default=None, primary_key=True)
        club_id: int = Field(foreign_key=Club)
        club: Relation[Club] = Forward("club_id")

    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await Club.create(id=1, name="Rust", tier=Tier.GOLD)
        await Member.create(id=1, club_id=1)
    database = db_url.removeprefix("sqlite:").split("?")[0]
    with closing(sqlite3.connect(database)) as raw, raw:
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute("UPDATE club SET tier = 'tin' WHERE id = 1")

    async with engines.session():
        club = (await Member.get(1)).club
        with pytest.raises(ValueError, match="tier"):
            await Club.get(1)  # the fetch fills the session's unloaded club

        assert repr(club) == "Club(id=1, unloaded)"
        with pytest.raises(InstanceNotLoaded):
            club.name


@pytest.mark.asyncio
async def test_model_copy_with_a_new_key_reads_the_relation_the_new_key_names(db_url):
    m = await _seed(db_url)
    async with engines.session():
        gophers = await m.Team.create(id=2, name="Gophers")
        player = await m.Player.get(1)
        assert player.team.id == 1  # cached on the player

        moved = player.model_copy(update={"team_id": 2})

        assert moved.team is gophers
        assert player.team.id == 1


@pytest.mark.asyncio
async def test_model_copy_refuses_to_write_a_column_into_an_unloaded_instance(db_url):
    m = await _seed(db_url)
    async with engines.session():
        team = (await m.Player.get(1)).team

        with pytest.raises(InstanceNotLoaded, match=re.escape(_NOT_LOADED)):
            team.model_copy(update={"name": "Ferrous"})


def _declare_key_last() -> SimpleNamespace:
    """An Account whose primary key is declared after its name."""

    class Account(Model):
        name: str
        code: str = Field(primary_key=True)

    class Note(Model):
        id: int | None = Field(default=None, primary_key=True)
        account_code: str = Field(foreign_key=Account)
        account: Relation[Account] = Forward("account_code")

    return SimpleNamespace(Account=Account, Note=Note)


@pytest.mark.parametrize("in_session", [True, False], ids=["session", "no-session"])
@pytest.mark.asyncio
async def test_a_filled_instance_lists_its_fields_in_declaration_order(
    db_url, in_session
):
    m = _declare_key_last()
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await m.Account.create(name="Ada", code="A-1")
        await m.Note.create(id=1, account_code="A-1")

    async def filled():
        note = await m.Note.using("default").get(1)
        return await note.account.load()

    if in_session:
        async with engines.session():
            account = await filled()
    else:
        account = await filled()

    fetched = await m.Account.using("default").get("A-1")
    assert list(account.model_dump()) == list(fetched.model_dump()) == ["name", "code"]
    assert account.model_dump_json() == fetched.model_dump_json()
    assert repr(account) == repr(fetched)


@pytest.mark.asyncio
async def test_a_sessionless_read_needs_no_registered_connection(db_url):
    m = await _seed(db_url)
    player = await m.Player.using("default").get(1)
    reset_engine()

    assert player.team.id == 1
    assert repr(player.team) == "Team(id=1, unloaded)"


@pytest.mark.asyncio
async def test_a_read_in_a_session_on_another_connection_leaves_the_conflict_to_load(
    db_url,
):
    m = await _seed(db_url)
    await ferro.connect(db_url, name="replica")
    player = await m.Player.using("replica").get(1)

    async with engines.session():
        team = player.team

        assert hasattr(player, "team")
        assert repr(team) == "Team(id=1, unloaded)"
        with pytest.raises(ValueError, match="replica"):
            await team.load()
        # Built on the player's connection, it is not the session's object.
        assert await m.Team.get(1) is not team


@pytest.mark.asyncio
async def test_a_refresh_that_finds_the_row_gone_leaves_the_relation_unloaded(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        team = await m.Team.get(1)
        await m.Player.where(lambda player: player.team_id == 1).delete()
        await m.Team.where(lambda team: team.id == 1).delete()

        with pytest.raises(ModelDoesNotExist):
            await team.refresh()

        assert player.team is not team
        with pytest.raises(InstanceNotLoaded, match=re.escape(_NOT_LOADED)):
            player.team.name
