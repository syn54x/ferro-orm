"""A many-to-many declared with `through=`, and its other side as a `Reverse` (#877).

```python
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    tournaments: Relation[list["Tournament"]] = ManyToMany(through="player_tournaments")

class Tournament(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation[list["Player"]] = Reverse("tournaments")
```

The join table is today's: the name passed to `through=`, one `<table>_id`
column per side and the same two indexes, so a model converted from
`ManyToMany(related_name=...)` plus `BackRef()` plans nothing against the
snapshot `tests/fixtures/many_to_many_pre_conversion/` recorded from the old
form.
"""

import shutil
from pathlib import Path

import pytest

import ferro
from ferro import Field, ManyToMany, Model, Relation, Reverse
from ferro.ir.compiler import compile_registry_schema_ir
from tests._pass_harness import auto_migrate
from tests.test_migrate_new import listing, run, write_config, write_models
from tests.test_postponed_annotations import _import, _wipe

PRE_CONVERSION = (
    Path(__file__).parent
    / "fixtures"
    / "many_to_many_pre_conversion"
    / "0001_tournaments"
)


def _tournament_models(reverse: bool = True):
    _wipe()

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        tournaments: Relation[list["Tournament"]] = ManyToMany(
            through="player_tournaments"
        )

    if reverse:

        class Tournament(Model):
            id: int | None = Field(default=None, primary_key=True)
            title: str
            players: Relation[list["Player"]] = Reverse("tournaments")

    else:

        class Tournament(Model):
            id: int | None = Field(default=None, primary_key=True)
            title: str

    return Player, Tournament


@pytest.fixture
def tournament_models():
    return _tournament_models()


# -- linking rows from both sides ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_add_links_rows_read_back_from_both_sides(db_url, tournament_models):
    Player, Tournament = tournament_models
    await auto_migrate(db_url)
    async with ferro.engines.session():
        ada = await Player.create(name="Ada")
        cup = await Tournament.create(title="Cup")

        await ada.tournaments.add(cup)

        assert [player.id for player in await cup.players.all()] == [ada.id]
        assert [tournament.id for tournament in await ada.tournaments.all()] == [cup.id]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_exists_finds_the_tournaments_with_a_player(db_url, tournament_models):
    Player, Tournament = tournament_models
    await auto_migrate(db_url)
    async with ferro.engines.session():
        ada = await Player.create(name="Ada")
        cup = await Tournament.create(title="Cup")
        await Tournament.create(title="Empty")
        await ada.tournaments.add(cup)

        linked = await Tournament.where(
            lambda tournament: tournament.players.exists()
        ).all()

        assert [tournament.title for tournament in linked] == ["Cup"]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_remove_from_the_reverse_side_unlinks(db_url, tournament_models):
    Player, Tournament = tournament_models
    await auto_migrate(db_url)
    async with ferro.engines.session():
        ada = await Player.create(name="Ada")
        cup = await Tournament.create(title="Cup")
        await ada.tournaments.add(cup)

        await cup.players.remove(ada)

        assert await ada.tournaments.all() == []


# -- the join table is today's -------------------------------------------------------

_NEW_FORM = """
from ferro import Field, ManyToMany, Model, Relation, Reverse

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    {owner}: Relation[list["Tournament"]] = ManyToMany(through="player_tournaments")

class Tournament(Model):
    id: int | None = Field(default=None, primary_key=True)
    {reverse}: Relation[list["Player"]] = Reverse("{owner}")
"""


@pytest.mark.usefixtures("isolated_imports", "clean_registry")
@pytest.mark.parametrize(
    ("owner", "reverse"), [("tournaments", "players"), ("events", "entrants")]
)
def test_the_new_form_plans_nothing_against_the_old_forms_snapshot(
    tmp_path, monkeypatch, capsys, owner, reverse
):
    monkeypatch.delenv("FERRO_CONFIG", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    pkg = f"ferro_m2m_through_{owner}"
    write_config(project, pkg)
    shutil.copytree(PRE_CONVERSION, project / "migrations" / "0001_tournaments")
    write_models(project, pkg, _NEW_FORM.format(owner=owner, reverse=reverse))
    before = listing(project / "migrations")

    assert run("migrate", "new", "nothing") == 0

    assert capsys.readouterr().out.endswith("no schema change: nothing written\n")
    assert listing(project / "migrations") == before


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_many_to_many_needs_no_reverse_side(db_url):
    Player, Tournament = _tournament_models(reverse=False)
    await auto_migrate(db_url)
    async with ferro.engines.session():
        ada = await Player.create(name="Ada")
        cup = await Tournament.create(title="Cup")

        await ada.tournaments.add(cup)

        assert [tournament.id for tournament in await ada.tournaments.all()] == [cup.id]


def test_reverse_index_false_keeps_the_unique_index_only():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        tournaments: Relation[list["Tournament"]] = ManyToMany(
            through="player_tournaments", reverse_index=False
        )

    class Tournament(Model):
        id: int | None = Field(default=None, primary_key=True)
        players: Relation[list["Player"]] = Reverse("tournaments")

    ferro.ensure_resolved_modelset()
    (join,) = [
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "player_tournaments"
    ]

    assert [unique["name"] for unique in join["uniques"]] == [
        "uq_player_tournaments_player_id_tournament_id"
    ]
    assert join["indexes"] == []


# -- refusals ------------------------------------------------------------------------

_NEEDS_THROUGH = (
    'Write: tournaments: Relation[list["Tournament"]] = '
    'ManyToMany(through="player_tournaments")'
)


@pytest.mark.parametrize("marker", ["ManyToMany()", 'ManyToMany(through="")'])
def test_a_many_to_many_without_through_is_refused(tmp_path, marker):
    with pytest.raises(TypeError) as refused:
        _import(
            tmp_path,
            f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    tournaments: Relation[list["Tournament"]] = {marker}
""",
            future=False,
        )

    message = str(refused.value)
    assert message.startswith("Player.tournaments: ")
    assert message.endswith(_NEEDS_THROUGH)


def test_a_reverse_naming_a_many_to_many_aimed_elsewhere_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        players: Relation[list["Player"]] = Reverse("tournaments")

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        tournaments: Relation[list["Tournament"]] = ManyToMany(
            through="player_tournaments"
        )

    class Tournament(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(TypeError) as refused:
        ferro.ensure_resolved_modelset()

    assert str(refused.value) == (
        "Team.players: Player.tournaments points at Tournament, not Team. "
        'Write: players: Relation[list["Player"]] = '
        'ManyToMany(through="team_players")'
    )
