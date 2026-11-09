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
from ferro.relations.fixes import DeclarationRefusal
from tests._pass_harness import auto_migrate
from tests.test_migrate_new import listing, run, write_config, write_models
from tests.test_postponed_annotations import (
    _ASSIGNED,
    _SPELLINGS,
    _import,
    _refusal,
    _wipe,
)

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


@pytest.mark.usefixtures("clean_registry")
def test_a_model_redefined_in_one_process_keeps_its_join_table():
    """A notebook or REPL redefines a model: its many-to-many, renamed over the
    same join table, takes the table over from the old definition."""

    class Tournament(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        tournaments: Relation[list["Tournament"]] = ManyToMany(
            through="player_tournaments"
        )

    ferro.ensure_resolved_modelset()

    class Player(Model):  # noqa: F811, the same model redefined
        id: int | None = Field(default=None, primary_key=True)
        events: Relation[list["Tournament"]] = ManyToMany(through="player_tournaments")

    ferro.ensure_resolved_modelset()

    join = ferro.registry.REGISTRY.join_tables()["player_tournaments"]
    assert join["declared_by"] == (Player.__ferro_identity__, "events")
    assert Player.__ferro_reverse_specs__["events"].join_table == "player_tournaments"


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


# -- every refusal's fix works ---------------------------------------------------------
#
# Each spelling of a `ManyToMany(through=...)` field, and of a `Reverse` naming
# one, with and without the __future__ import, its target quoted or not and
# declared before or after it. Each outcome is a working many-to-many, or a
# refusal whose carried edit, replacing the refused line and imported in the
# same mode, defines one.

_M2M_EXTRA_SPELLINGS = {
    "list under | None inside": "Relation[list[{target}] | None] = {marker}",
    "one row": "Relation[{target}] = {marker}",
    "plain list": "list[{target}] = {marker}",
}
_EVERY_SPELLING = {**_SPELLINGS, **_M2M_EXTRA_SPELLINGS}

_M2M_MARKERS = {
    "through": 'ManyToMany(through="post_tags")',
    "no through": "ManyToMany()",
    "empty through": 'ManyToMany(through="")',
    "no reverse index": 'ManyToMany(through="post_tags", reverse_index=False)',
    "Reverse": 'Reverse("tags")',
    "Reverse by keyword": 'Reverse(attribute="tags")',
}

_FORMS = {
    # form: (field, target, markers)
    "ManyToMany": (
        "tags",
        "Tag",
        ("through", "no through", "empty through", "no reverse index"),
    ),
    "Reverse": ("posts", "Post", ("Reverse", "Reverse by keyword")),
}


def _m2m_body(form: str, field_line: str, order: str) -> str:
    """The modelset whose ``form`` side is declared by ``field_line``."""
    post_line = 'tags: Relation[list["Tag"]] = ManyToMany(through="post_tags")'
    tag_line = 'posts: Relation[list["Post"]] = Reverse("tags")'
    if form == "ManyToMany":
        post_line = field_line
    else:
        tag_line = field_line
    post = f"""
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    {post_line}
"""
    tag = f"""
class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    {tag_line}
"""
    declaring, target = (post, tag) if form == "ManyToMany" else (tag, post)
    imports = "from typing import Annotated as A, Union\n\nfrom ferro import Reverse\n"
    return imports + (declaring + target if order == "after" else target + declaring)


def _m2m_field(form: str, spelling: str, quoted: bool, marker: str) -> str:
    field, target, _ = _FORMS[form]
    written = f'"{target}"' if quoted else target
    annotation = _EVERY_SPELLING[spelling].format(
        relation=f"Relation[list[{written}]]",
        plain=f"Relation[list[{target}]]",
        target=written,
        marker=_M2M_MARKERS[marker],
    )
    return f"{field}: {annotation}"


def _links_posts_and_tags(tmp_path, body: str, future: bool) -> None:
    """Import ``body`` and resolve it; both sides must read `post_tags`."""
    _wipe()
    mod = _import(tmp_path, body, future=future)
    ferro.ensure_resolved_modelset()
    tags = mod.Post.__ferro_reverse_specs__["tags"]
    posts = mod.Tag.__ferro_reverse_specs__["posts"]
    assert (tags.target, tags.is_m2m, tags.join_table) == (mod.Tag, True, "post_tags")
    assert (posts.target, posts.is_m2m, posts.join_table) == (
        mod.Post,
        True,
        "post_tags",
    )
    assert (posts.source_col, posts.target_col) == (tags.target_col, tags.source_col)


_M2M_SWEEP = [
    (form, spelling, future, quoted, order, marker)
    for form, (_, _, markers) in _FORMS.items()
    for spelling in _EVERY_SPELLING
    for future in (False, True)
    # A whole quoted annotation quotes nothing inside it.
    for quoted in ((False,) if "whole" in spelling else (False, True))
    for order in ("before", "after")
    for marker in markers
    # Without postponed annotations an unquoted later class is a NameError.
    if future or quoted or "whole" in spelling or order == "before"
]


@pytest.mark.parametrize(
    ("form", "spelling", "future", "quoted", "order", "marker"), _M2M_SWEEP
)
def test_every_spelling_is_a_many_to_many_or_a_refusal_whose_fix_works(
    tmp_path, form, spelling, future, quoted, order, marker
):
    body = _m2m_body(form, _m2m_field(form, spelling, quoted, marker), order)
    works = spelling in _ASSIGNED and marker not in ("no through", "empty through")
    _wipe()
    try:
        _import(tmp_path, body, future=future)
        ferro.ensure_resolved_modelset()
    except DeclarationRefusal as refusal:
        assert not works, f"{body}\nrefused: {refusal}"
        (edit,) = refusal.edits
        assert (edit.field, edit.action) == (_FORMS[form][0], "replace"), refusal
        (line,) = edit.lines
        if marker == "no reverse index":
            assert line.endswith(", reverse_index=False)"), line
        try:
            _links_posts_and_tags(tmp_path, _m2m_body(form, line, order), future)
        except Exception as error:
            raise AssertionError(
                f"the fix {line!r} for\n{body}\ndoes not work: {error}"
            ) from error
        return
    assert works, f"{body}\nwas accepted"
    _links_posts_and_tags(tmp_path, body, future)


def test_a_bare_many_to_many_nothing_names_says_what_to_annotate(tmp_path):
    body = """
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    tags: Relation = ManyToMany(through="post_tags")

class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
"""

    assert _refusal(tmp_path, body, future=False) == (
        "Post.tags: a relation is annotated Relation[...], and nothing names the "
        "model it links. Annotate tags as Relation[list[...]] of that model, "
        'keeping ManyToMany(through="post_tags")'
    )


_ELSEWHERE = {
    # What Player declares beside `tournaments`, and the fix it leads to.
    "nothing linking Team": (
        "",
        'players: Relation[list[{player}]] = ManyToMany(through="team_players")',
    ),
    "a key column to Team": (
        "team_id: int = Field(foreign_key=Team)",
        'players: Relation[list[{player}]] = Reverse("team_id")',
    ),
    "a many-to-many to Team": (
        'teams: Relation[list["Team"]] = ManyToMany(through="player_teams")',
        'players: Relation[list[{player}]] = Reverse("teams")',
    ),
}


def _elsewhere_body(team_line: str, player_line: str) -> str:
    return f"""
from ferro import Reverse

class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    {team_line}

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    tournaments: Relation[list["Tournament"]] = ManyToMany(through="player_tournaments")
    {player_line}

class Tournament(Model):
    id: int | None = Field(default=None, primary_key=True)
"""


@pytest.mark.parametrize("future", [False, True])
@pytest.mark.parametrize("player_side", sorted(_ELSEWHERE))
def test_a_reverse_naming_a_many_to_many_aimed_elsewhere_names_a_fix_that_works(
    tmp_path, future, player_side
):
    player_line, fix = _ELSEWHERE[player_side]
    refused = 'players: Relation[list["Player"]] = Reverse("tournaments")'
    message = _refusal(tmp_path, _elsewhere_body(refused, player_line), future)
    line = fix.format(player="Player" if future else '"Player"')

    assert message == (
        "Team.players: Player.tournaments points at Tournament, not Team. "
        f"Write: {line}"
    )
    _wipe()
    mod = _import(tmp_path, _elsewhere_body(line, player_line), future=future)
    ferro.ensure_resolved_modelset()
    assert mod.Team.__ferro_reverse_specs__["players"].target is mod.Player


def test_a_self_referential_many_to_many_is_a_recipe_for_a_join_model(tmp_path):
    body = """
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    friends: Relation[list["Player"]] = ManyToMany(through="player_friends")
"""

    assert _refusal(tmp_path, body, future=False) == (
        "Player.friends: a self-referential many-to-many is not supported, since "
        "both join-table columns would be player_id. Fix it by hand: (1) add the "
        'model `class PlayerFriends(Model): __ferro_table__ = "player_friends"; '
        "id: int | None = Field(default=None, primary_key=True); "
        'player_id: int = Field(foreign_key="Player"); '
        'other_player_id: int = Field(foreign_key="Player")`; (2) on Player, '
        "remove friends"
    )


def test_a_many_to_many_annotated_without_relation_says_so(tmp_path):
    body = """
class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)

class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    tags: Tag | None = ManyToMany(through="post_tags")
"""

    assert _refusal(tmp_path, body, future=False) == (
        "Post.tags: a relation is annotated Relation[...]. "
        'Write: tags: Relation[list[Tag]] = ManyToMany(through="post_tags")'
    )
