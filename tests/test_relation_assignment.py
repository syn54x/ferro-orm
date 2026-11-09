"""Assigning a forward relation sets the foreign-key column behind it.

```python
player.team = crabs      # team_id becomes crabs.id; player.team is crabs, no query
player.mentor = None     # mentor_id becomes None; player.mentor reads None
player.team_id = 9       # the loaded team is dropped; player.team reads Team(id=9, unloaded)
```

Each test declares its own models in an empty registry, so the names in the
refusals (`Player.team`, `team_id`) read as a user's would.
"""

import importlib
import itertools
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import pytest

import ferro
from ferro import (
    Field,
    Forward,
    Model,
    Relation,
    Reverse,
    clear_registry,
    engines,
    reset_engine,
)
from ferro.registry import REGISTRY
from ferro.relations.fixes import DeclarationRefusal

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
    """Team, ProTeam (a model of its own, with its own table), Coach and
    Player (a required team and a nullable mentor)."""

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        players: Relation[list["Player"]] = Reverse("team_id")

    class ProTeam(Team):
        pass

    class Coach(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")
        mentor_id: int | None = Field(
            default=None, foreign_key="Player", on_delete="SET NULL"
        )
        mentor: Relation["Player | None"] = Forward("mentor_id")

    return SimpleNamespace(Team=Team, ProTeam=ProTeam, Coach=Coach, Player=Player)


async def _seed(db_url: str) -> SimpleNamespace:
    """Teams "Rustaceans" (1) and "Crabs" (2), pro team "Krakens" (1), coach
    Pat (1), Ferris (player 1, team 1) and Corro (player 2, team 1, mentored
    by Ferris)."""
    m = _declare()
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        # No explicit ids, so each table's sequence hands out the next one.
        await m.Team.create(name="Rustaceans")
        await m.Team.create(name="Crabs")
        await m.ProTeam.create(name="Krakens")
        await m.Coach.create(name="Pat")
        await m.Player.create(name="Ferris", team_id=1)
        await m.Player.create(name="Corro", team_id=1, mentor_id=1)
    return m


@pytest.mark.asyncio
async def test_assigning_a_team_sets_its_key_and_loads_the_relation_without_a_query(
    db_url, statement_records
):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        crabs = await m.Team.get(2)
        statement_records.clear()

        player.team = crabs

        assert player.team_id == 2
        assert player.team is crabs
        assert statement_records == []


@pytest.mark.asyncio
async def test_an_assigned_team_is_saved_as_the_key_of_the_row(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        player.team = await m.Team.get(2)
        await player.save()

    async with engines.session():
        assert (await m.Player.get(1)).team_id == 2


@pytest.mark.asyncio
async def test_assigning_none_clears_a_nullable_key(db_url):
    m = await _seed(db_url)
    async with engines.session():
        corro = await m.Player.get(2)
        assert corro.mentor.id == 1

        corro.mentor = None

        assert corro.mentor_id is None
        assert corro.mentor is None
        await corro.save()

    async with engines.session():
        assert (await m.Player.get(2)).mentor_id is None


@pytest.mark.asyncio
async def test_an_unloaded_instance_is_assigned_as_it_is(db_url):
    m = await _seed(db_url)
    async with engines.session():
        ferris = await m.Player.get(1)
        other = m.Player(id=3, name="Sly", team_id=2)
        assert repr(ferris.team) == "Team(id=1, unloaded)"

        other.team = ferris.team

        assert other.team_id == ferris.team_id == 1
        assert other.team is ferris.team


@pytest.mark.asyncio
async def test_another_models_instance_is_refused_and_the_fix_it_names_works(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        loaded = player.team
        coach = await m.Coach.get(1)

        with pytest.raises(TypeError) as refused:
            player.team = coach

        assert str(refused.value) == (
            "Player.team: expected a Team, got Coach. Assign a saved Team "
            "instance, or set team_id directly"
        )
        assert player.team_id == 1
        assert player.team is loaded

        # A subclass model has a table of its own, so its key names no Team.
        krakens = await m.ProTeam.get(1)
        with pytest.raises(TypeError) as refused:
            player.team = krakens

        assert str(refused.value) == (
            "Player.team: expected a Team, got ProTeam. Assign a saved Team "
            "instance, or set team_id directly"
        )
        assert player.team_id == 1
        assert player.team is loaded

        # The fix, both ways it reads.
        crabs = await m.Team.get(2)
        player.team = crabs
        assert player.team is crabs
        player.team_id = 1
        assert player.team_id == 1
        assert player.team.id == 1


@pytest.mark.asyncio
async def test_a_value_that_is_no_instance_is_refused(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        loaded = player.team

        with pytest.raises(TypeError) as refused:
            player.team = 5

        assert str(refused.value) == (
            "Player.team: expected a Team, got int. Assign a saved Team "
            "instance, or set team_id directly"
        )
        assert player.team_id == 1
        assert player.team is loaded

        player.team_id = 2
        assert player.team.id == 2


@pytest.mark.asyncio
async def test_an_instance_with_no_key_is_refused_until_it_is_saved(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        loaded = player.team
        new = m.Team(name="new")

        with pytest.raises(ValueError) as refused:
            player.team = new

        assert str(refused.value) == (
            "Player.team: the Team has no id yet. Save it first, then assign it"
        )
        assert player.team_id == 1
        assert player.team is loaded

        await new.save()
        player.team = new
        assert player.team_id == new.id
        assert player.team is new


@pytest.mark.asyncio
async def test_none_on_a_required_relation_is_refused(db_url):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        loaded = player.team

        with pytest.raises(TypeError) as refused:
            player.team = None

        assert str(refused.value) == (
            "Player.team: team_id is not nullable, so the relation cannot be "
            "None. Assign a Team, or make both nullable. Fix it by hand: "
            "(1) on Player, replace team_id with "
            "`team_id: int | None = Field(default=None, foreign_key=Team)`; "
            "(2) on Player, replace team with "
            '`team: Relation[Team | None] = Forward("team_id")`'
        )
        assert player.team_id == 1
        assert player.team is loaded


def _team() -> type[Model]:
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    return Team


def _refused_none(model: type[Model], relation: str, **keys) -> str:
    """The `None` refusal's message for `relation` on a new `model`."""
    with pytest.raises(TypeError) as refused:
        setattr(model(**keys), relation, None)
    return str(refused.value)


def _assigns_none(model: type[Model], relation: str, column: str, **keys) -> None:
    instance = model(**keys)
    setattr(instance, relation, None)
    assert getattr(instance, column) is None
    assert getattr(instance, relation) is None


def test_the_none_refusal_writes_lines_that_make_none_assignable():
    Team = _team()

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team, index=True)
        team: Relation[Team] = Forward("team_id")

    assert _refused_none(Player, "team", team_id=1).endswith(
        "(1) on Player, replace team_id with `team_id: int | None = "
        "Field(default=None, foreign_key=Team, index=True)`; (2) on Player, "
        'replace team with `team: Relation[Team | None] = Forward("team_id")`'
    )

    # The two lines, pasted in place of the two refused ones.
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int | None = Field(default=None, foreign_key=Team, index=True)
        team: Relation[Team | None] = Forward("team_id")

    _assigns_none(Player, "team", "team_id", team_id=1)


def test_the_none_refusal_writes_lines_for_an_annotated_key_column():
    Team = _team()

    class Player(Model):
        id: Annotated[int | None, Field(primary_key=True)] = None
        team_id: Annotated[int, Field(foreign_key=Team, index=True)]
        team: Relation[Team] = Forward("team_id")

    assert _refused_none(Player, "team", team_id=1).endswith(
        "(1) on Player, replace team_id with `team_id: int | None = "
        "Field(default=None, foreign_key=Team, index=True)`; (2) on Player, "
        'replace team with `team: Relation[Team | None] = Forward("team_id")`'
    )

    class Player(Model):
        id: Annotated[int | None, Field(primary_key=True)] = None
        team_id: int | None = Field(default=None, foreign_key=Team, index=True)
        team: Relation[Team | None] = Forward("team_id")

    _assigns_none(Player, "team", "team_id", team_id=1)


def test_the_none_refusal_quotes_a_self_reference():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        mentor_id: int = Field(foreign_key="Player")
        mentor: Relation["Player"] = Forward("mentor_id")

    assert _refused_none(Player, "mentor", mentor_id=1).endswith(
        "(1) on Player, replace mentor_id with "
        '`mentor_id: int | None = Field(default=None, foreign_key="Player")`; '
        "(2) on Player, replace mentor with "
        '`mentor: Relation["Player | None"] = Forward("mentor_id")`'
    )

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        mentor_id: int | None = Field(default=None, foreign_key="Player")
        mentor: Relation["Player | None"] = Forward("mentor_id")

    _assigns_none(Player, "mentor", "mentor_id", mentor_id=1)


def test_assigning_another_key_reads_as_an_unloaded_instance_with_that_key():
    m = _declare()
    crabs = m.Team(id=2, name="Crabs")
    rustaceans = m.Team(id=1, name="Rustaceans")
    player = m.Player(name="Ferris", team_id=1)
    player.team = crabs

    player.team_id = rustaceans.id

    assert repr(player.team) == f"Team(id={rustaceans.id}, unloaded)"


def test_assigning_the_same_key_keeps_the_loaded_relation():
    m = _declare()
    crabs = m.Team(id=2, name="Crabs")
    player = m.Player(name="Ferris", team_id=1)
    player.team = crabs

    player.team_id = crabs.id

    assert player.team is crabs


def test_assignment_needs_no_database():
    m = _declare()
    player = m.Player(name="Ferris", team_id=2)

    player.team = m.Team(id=1, name="x")
    # A relation named by a string resolves on its first assignment.
    player.mentor = m.Player(id=7, name="Corro", team_id=1)
    player.mentor_id = 8

    assert player.team_id == 1
    assert repr(player.mentor) == "Player(id=8, unloaded)"


def test_a_refusal_writes_the_article_the_target_name_takes():
    class User(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Upload(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Note(Model):
        id: int | None = Field(default=None, primary_key=True)
        user_id: int = Field(foreign_key=User)
        user: Relation[User] = Forward("user_id")
        upload_id: int = Field(foreign_key=Upload)
        upload: Relation[Upload] = Forward("upload_id")

    note = Note(user_id=1, upload_id=1)
    with pytest.raises(TypeError, match="^Note.user: expected a User, got int"):
        note.user = 5
    with pytest.raises(TypeError, match="^Note.upload: expected an Upload, got"):
        note.upload = 5


_MODULES = itertools.count()


def _import_models(tmp_path: Path, body: str, *, future: bool):
    """Write `body` as a model module, with or without the __future__ import,
    and import it."""
    name = f"relation_assignment_mod_{next(_MODULES)}"
    header = "from __future__ import annotations\n" if future else ""
    header += "from ferro import Field, Forward, Model, Relation\n"
    (tmp_path / f"{name}.py").write_text(header + textwrap.dedent(body))
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(name, None)


_TEAM = """
class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
"""


@pytest.mark.parametrize("future", [False, True], ids=["eager", "postponed"])
def test_the_none_refusal_drops_an_explicit_nullable_false(tmp_path, future):
    refused_models = _import_models(
        tmp_path,
        _TEAM
        + """
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team, nullable=False)
    team: Relation[Team] = Forward("team_id")
""",
        future=future,
    )
    with pytest.raises(DeclarationRefusal) as refused:
        refused_models.Player(team_id=1).team = None
    (column,), (relation,) = (edit.lines for edit in refused.value.edits)
    assert column == "team_id: int | None = Field(default=None, foreign_key=Team)"

    clear_registry()
    REGISTRY.reset_for_test()
    pasted = _import_models(
        tmp_path,
        _TEAM
        + f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    {column}
    {relation}
""",
        future=future,
    )
    _assigns_none(pasted.Player, "team", "team_id", team_id=1)
