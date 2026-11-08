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

import re
import shutil
from pathlib import Path

import pytest

import ferro
from ferro import Field, ManyToMany, Model, Relation, Reverse
from ferro.ir.compiler import compile_registry_schema_ir
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
# refusal whose every Write line, pasted in place of the refused one and
# imported in the same mode, defines one.

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
    except TypeError as refusal:
        assert not works, f"{body}\nrefused: {refusal}"
        message = str(refusal)
        assert "Write: " in message, message
        for line in message.split("Write: ", 1)[1].split(" or "):
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


# -- every Write line passes every check ----------------------------------------------
#
# A refusal's fix is a promise that the declaration it prints passes every
# check, so each refusal path that writes a many-to-many, a reverse
# relation or a foreign-key column is swept across the model it links (another
# model, or the declaring model itself) and the storage of that model's key
# (the default for `int` and for `str`, and a declared `db_type`). Every line
# of every fix, pasted back into the models in the same __future__ mode, must
# give a working relation of the right kind, and a self-referential
# many-to-many's recipe a join model that saves and reads rows.

_KEYS = {
    # storage: (primary-key line, key type, a type that is not it, sample keys)
    "int": ("id: int | None = Field(default=None, primary_key=True)", "int", "str"),
    "str": ("code: str = Field(primary_key=True)", "str", "int"),
    "varchar(8)": (
        'code: str = Field(primary_key=True, db_type="varchar(8)")',
        "str",
        "int",
    ),
}

# A column whose storage is not its target key's, per target key storage.
_OTHER_STORAGE = {
    "int": 'Field(foreign_key="{t}", db_type="bigint")',
    "str": 'Field(foreign_key="{t}", db_type="varchar(8)")',
    "varchar(8)": 'Field(foreign_key="{t}")',
}


def _case(kind, field, *, owner="Player", no_pk=(), **models):
    """One refused modelset: the lines of each model, the refused ``field``
    on ``owner``, and the kind of relation its fix must give."""
    return {
        "kind": kind,
        "owner": owner,
        "field": field,
        "no_pk": no_pk,
        "models": models,
    }


def _cases(storage: str) -> dict[str, dict]:
    _, key, wrong = _KEYS[storage]
    # A key column the case does not refuse, stored as its target key is.
    stored = ', db_type="varchar(8)"' if storage == "varchar(8)" else ""
    cases = {}
    for t in ("Team", "Player"):
        cases[f"key of the wrong type to {t}"] = _case(
            "key", "team_id", Player=[f'team_id: {wrong} = Field(foreign_key="{t}")']
        )
        cases[f"key stored otherwise to {t}"] = _case(
            "key",
            "team_id",
            Player=[f"team_id: {key} = " + _OTHER_STORAGE[storage].format(t=t)],
        )
        cases[f"required SET NULL key to {t}"] = _case(
            "key",
            "team_id",
            Player=[f'team_id: {key} = Field(foreign_key="{t}", on_delete="SET NULL")'],
        )
        cases[f"forward with no column to {t}"] = _case(
            "forward", "team", Player=[f'team: Relation["{t}"] = Forward("team_id")']
        )
        cases[f"forward over a column with no foreign_key to {t}"] = _case(
            "forward",
            "team",
            Player=[f"team_id: {key}", f'team: Relation["{t}"] = Forward("team_id")'],
        )
        reverse = 'players: Relation[list["Player"]] = Reverse("team_id")'
        cases[f"reverse on {t} with no column"] = _case(
            "reverse", "players", owner=t, **{t: [reverse]}
        )
        cases[f"reverse on {t} over a column with no foreign_key"] = _case(
            "reverse",
            "players",
            owner=t,
            **_merge({"Player": [f"team_id: {key}"]}, {t: [reverse]}),
        )
        elsewhere = "Team" if t == "Player" else "Player"
        cases[f"reverse on {t} over a column pointing elsewhere"] = _case(
            "reverse",
            "players",
            owner=t,
            **_merge(
                {
                    "Player": [
                        f"team_id: {key} | None = "
                        f'Field(default=None, foreign_key="{elsewhere}"{stored})'
                    ]
                },
                {t: [reverse]},
            ),
        )
    tournaments = (
        'tournaments: Relation[list["Tournament"]] = '
        'ManyToMany(through="player_tournaments")'
    )
    elsewhere = 'players: Relation[list["Player"]] = Reverse("tournaments")'
    cases["reverse on Team over a many-to-many to Tournament"] = _case(
        "reverse", "players", owner="Team", Player=[tournaments], Team=[elsewhere]
    )
    cases["reverse on Team over a many-to-many, a key pointing back"] = _case(
        "reverse",
        "players",
        owner="Team",
        Player=[tournaments, f'team_id: {key} = Field(foreign_key="Team"{stored})'],
        Team=[elsewhere],
    )
    cases["reverse on Player over its own many-to-many, a self key"] = _case(
        "reverse",
        "players",
        Player=[
            tournaments,
            f"mentor_id: {key} | None = "
            f'Field(default=None, foreign_key="Player"{stored})',
            elsewhere,
        ],
    )
    cases["reverse on Player over its own many-to-many"] = _case(
        "reverse", "players", Player=[tournaments, elsewhere]
    )
    spellings = {
        "with no through": 'Relation[list["{t}"]] = ManyToMany()',
        "under | None": 'Relation[list["{t}"]] | None = ManyToMany(through="player_links")',
        "with | None inside": (
            'Relation[list["{t}"] | None] = ManyToMany(through="player_links")'
        ),
        "annotated as the model": '"{t}" = ManyToMany(through="player_links")',
        "in Annotated metadata": (
            'Annotated[Relation[list["{t}"]], ManyToMany(through="player_links")]'
        ),
        "as declared": 'Relation[list["{t}"]] = ManyToMany(through="player_links")',
    }
    for t in ("Tag", "Player"):
        for spelling, written in spellings.items():
            if spelling == "as declared" and t == "Tag":
                continue  # a working many-to-many, refused only for itself
            cases[f"many-to-many to {t} {spelling}"] = _case(
                "many-to-many", "links", Player=[f"links: {written.format(t=t)}"]
            )
        cases[f"bare many-to-many a Reverse on {t} names"] = _case(
            "many-to-many",
            "links",
            **_merge(
                {"Player": ['links: Relation = ManyToMany(through="player_links")']},
                {t: ['linked: Relation[list["Player"]] = Reverse("links")']},
            ),
        )
    # A join table name `__ferro_table__` does not accept as a model's table.
    for through in ("player-links", "2way_links"):
        cases[f"many-to-many to Player through {through}"] = _case(
            "many-to-many",
            "links",
            Player=[
                f'links: Relation[list["Player"]] = ManyToMany(through="{through}")'
            ],
        )
    # A one-to-one sharing its key: the key column is the primary key `id`.
    cases["key shared with the primary key"] = _case(
        "shared key",
        "id",
        no_pk=("Player",),
        Player=[f'id: {key} = Field(primary_key=True, foreign_key="Team"{stored})'],
    )
    if storage == "int":
        cases["generated key"] = _case(
            "key",
            "team_id",
            Player=[
                "team_id: int | None = "
                'Field(foreign_key="Team", generated=lambda player: player.id)'
            ],
        )
        cases["primary-key key"] = _case(
            "key",
            "team_id",
            no_pk=("Player",),
            Player=['team_id: int = Field(primary_key=True, foreign_key="Team")'],
        )
        cases["primary-key key beside a plain id"] = _case(
            "key",
            "team_id",
            no_pk=("Player",),
            Player=[
                "id: int = 0",
                'team_id: int = Field(primary_key=True, foreign_key="Team")',
            ],
        )
    return cases


def _merge(first: dict, second: dict) -> dict:
    merged = {name: list(lines) for name, lines in first.items()}
    for name, lines in second.items():
        merged.setdefault(name, []).extend(lines)
    return merged


_MODELS = ("Team", "Tag", "Tournament", "Player")

# Each modelset is defined in both orders, so every model a fix names is
# sometimes defined before the line that names it and sometimes after.
_ORDERS = {"Player last": _MODELS, "Player first": tuple(reversed(_MODELS))}


def _source(
    case: dict, models: dict, storage: str, module_lines: list[str], order: str
) -> str:
    pk = _KEYS[storage][0]
    classes = []
    for model in _ORDERS[order]:
        body = [] if model in case["no_pk"] else [pk]
        body += models.get(model, [])
        classes.append(
            f"class {model}(Model):\n" + "".join(f"    {line}\n" for line in body)
        )
    return (
        "from ferro import Forward, Reverse\n\n"
        + "\n".join(classes)
        + "".join(f"\n{line}\n" for line in module_lines)
    )


def _field_of(line: str) -> str:
    return line.split(":", 1)[0].strip()


def _fixes(message: str, case: dict) -> list[tuple[dict, list[str]]]:
    """Each fix ``message`` offers, as the models it gives and the
    module-level lines it adds."""
    owner, field = case["owner"], case["field"]
    if "Write: " not in message:
        without = {
            name: [line for line in lines if _field_of(line) != field or name != owner]
            for name, lines in case["models"].items()
        }
        recipe = re.search(
            r"beside \w+: (class .*?)((?:; and in place of .*)*)$", message
        )
        if recipe:
            for reader in re.finditer(
                r"; and in place of (\w+)\.(\w+), which reads it: ([^;]*)",
                recipe.group(2),
            ):
                model, name, line = reader.groups()
                without[model] = [
                    line if _field_of(existing) == name else existing
                    for existing in without[model]
                ]
            return [(without, [recipe.group(1)])]
        moved = re.search(r"Declare \w+ on (\w+), which .* links: (.*)$", message)
        assert moved, f"no fix to paste in: {message}"
        return [(_merge(without, {moved.group(1): [moved.group(2)]}), [])]
    fixes = []
    for alternative in message.split("Write: ", 1)[1].split(" or "):
        models = _merge(case["models"], {})
        for part in alternative.split(" and "):
            on = re.fullmatch(r"(.*) on (\w+)", part)
            line, model = (on.group(1), on.group(2)) if on else (part, owner)
            lines = models.setdefault(model, [])
            names = [_field_of(existing) for existing in lines]
            if _field_of(line) in names:
                lines[names.index(_field_of(line))] = line
            else:
                lines.append(line)
        fixes.append((models, []))
    return fixes


async def _works(mod, case: dict, module_lines: list[str]) -> None:
    """The relation the fix declares is of the case's kind, and works."""
    ferro.ensure_resolved_modelset()
    field = case["field"]
    models = [getattr(mod, name) for name in _MODELS]
    if case["kind"] == "key":
        column = mod.Player.__ferro_columns__[field]
        assert column.foreign_key is not None, column
        return
    if case["kind"] == "shared key":
        columns = mod.Player.__ferro_columns__
        assert mod.Player.__ferro_pk__ == "id"
        assert columns["id"].foreign_key is None
        (key,) = [column for column in columns.values() if column.foreign_key]
        assert (key.foreign_key.to_table, key.unique) == ("team", True)
        return
    if case["kind"] == "forward":
        assert field in mod.Player.__ferro_relation_specs__
        return
    if module_lines:  # the self-referential recipe: a join model of its own
        (join,) = [
            value
            for name, value in vars(mod).items()
            if isinstance(value, type)
            and issubclass(value, Model)
            and value is not Model
            and name not in _MODELS
        ]
        await ferro.connect("sqlite::memory:", auto_migrate=True)
        async with ferro.engines.session():
            pk = mod.Player.__ferro_pk__
            # Explicit keys: an insert naming no column is not valid SQLite.
            keys = ({pk: 1}, {pk: 2}) if pk == "id" else ({pk: "a1"}, {pk: "b2"})
            first = await mod.Player.create(**keys[0])
            second = await mod.Player.create(**keys[1])
            columns = [name for name in join.__ferro_fields__ if name.endswith("_id")]
            await join.create(
                **{columns[0]: getattr(first, pk), columns[1]: getattr(second, pk)}
            )
            (row,) = await join.all()
            assert [getattr(row, column) for column in columns] == [
                getattr(first, pk),
                getattr(second, pk),
            ]
        return
    specs = [model.__ferro_reverse_specs__.get(field) for model in models]
    found = [spec for spec in specs if spec is not None]
    assert found, f"no relation {field}"
    if case["kind"] == "many-to-many":
        assert found[0].is_m2m


_SWEEP_CASES = [
    (storage, name, order, future)
    for storage in _KEYS
    for name in _cases(storage)
    for order in _ORDERS
    for future in (False, True)
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("storage", "name", "order", "future"), _SWEEP_CASES)
async def test_every_fix_passes_every_check(tmp_path, storage, name, order, future):
    case = _cases(storage)[name]
    refused = _source(case, case["models"], storage, [], order)
    message = _refusal(tmp_path, refused, future)
    assert message.startswith(f"{case['owner']}.{case['field']}: "), message
    for models, module_lines in _fixes(message, case):
        fixed = _source(case, models, storage, module_lines, order)
        _wipe()
        try:
            mod = _import(tmp_path, fixed, future=future)
            await _works(mod, case, module_lines)
        except Exception as error:
            raise AssertionError(
                f"the fix in\n{message}\ndoes not work:\n{fixed}\n{error!r}"
            ) from error
        finally:
            _wipe()


def test_a_self_referential_many_to_many_is_refused_naming_a_join_model(tmp_path):
    body = """
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    friends: Relation[list["Player"]] = ManyToMany(through="player_friends")
"""

    assert _refusal(tmp_path, body, future=False) == (
        "Player.friends: a self-referential many-to-many is not supported, since "
        "both join-table columns would be player_id. Declare the join table as a "
        "model of its own, beside Player: class PlayerFriends(Model): "
        '__ferro_table__ = "player_friends"; '
        "id: int | None = Field(default=None, primary_key=True); "
        'player_id: int = Field(foreign_key="Player"); '
        'other_player_id: int = Field(foreign_key="Player")'
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
