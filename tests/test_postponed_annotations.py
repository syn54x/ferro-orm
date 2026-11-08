"""A ForeignKey is detected in a string annotation and under an outer Optional.

Each case writes a real model module to ``tmp_path`` and imports it, so a
``from __future__ import annotations`` header is the one Python itself applies.
The DDL comparison builds the same models once with the header and once
without it (the twin) and asserts the ``CREATE TABLE`` statements are equal.
"""

from __future__ import annotations

import importlib
import itertools
import sys
import textwrap
from pathlib import Path
from typing import Annotated

import pytest

import ferro
from ferro import (
    BackRef,
    Field,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    reset_engine,
)
from ferro.registry import REGISTRY

_COUNTER = itertools.count()
FUTURE = "from __future__ import annotations\n"

HEADER = """
from typing import Annotated, Optional

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation
"""

AUTHOR = """
class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    posts: Relation[list["Post"]] = BackRef()
"""


def _wipe() -> None:
    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


def _import(tmp_path: Path, body: str, *, future: bool):
    """Write ``body`` as a module (with or without the __future__ import) and import it."""
    name = f"postponed_mod_{next(_COUNTER)}"
    source = (FUTURE if future else "") + HEADER + textwrap.dedent(body)
    (tmp_path / f"{name}.py").write_text(source)
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(name, None)


async def _create_statements(
    db_url: str, tmp_path: Path, body: str, *, future: bool, drop: tuple[str, ...]
) -> list[str]:
    """The CREATE TABLE statements ``create_tables()`` runs for ``body``."""
    _wipe()
    _import(tmp_path, body, future=future)
    await ferro.connect(db_url)
    async with ferro.engines.session():
        report = await ferro.create_tables()
        statements = [s.sql for s in report.statements]
        for table in drop:
            await ferro.raw.execute(f'DROP TABLE "{table}"')
    _wipe()
    return statements


def _post(annotation: str, *, extra: str = "") -> str:
    return (
        AUTHOR
        + f"""
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    author: {annotation}{extra}
"""
    )


FK = 'Annotated[Author, ForeignKey(related_name="posts")]'
INNER = 'Annotated[Author | None, ForeignKey(related_name="posts")]'


async def _same_ddl(db_url, tmp_path, with_import: str, plain: str, *, future: bool):
    got = await _create_statements(
        db_url, tmp_path, with_import, future=future, drop=("post", "author")
    )
    want = await _create_statements(
        db_url, tmp_path, plain, future=False, drop=("post", "author")
    )
    assert got == want
    return got


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_string_annotation_gives_the_same_ddl_as_the_twin(db_url, tmp_path):
    got = await _same_ddl(db_url, tmp_path, _post(FK), _post(FK), future=True)
    post = next(s for s in got if '"post"' in s and "CREATE TABLE" in s)
    assert '"author_id"' in post
    assert "fk_post_author_id_author" in post
    assert '"author" JSON' not in post


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_target_defined_below_binds_at_connect(db_url, tmp_path):
    forward = (
        """
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    author: Annotated[Author, ForeignKey(related_name="posts")]
"""
        + AUTHOR
    )
    plain = (
        AUTHOR
        + """
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    author: Annotated[Author, ForeignKey(related_name="posts")]
"""
    )
    await _same_ddl(db_url, tmp_path, forward, plain, future=True)

    _wipe()
    mod = _import(tmp_path, forward, future=True)
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await mod.Author(name="Ada").save()
        await mod.Post(title="Notes", author_id=1).save()
        post = await mod.Post.get(1)
        assert (await post.author).name == "Ada"


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_self_reference_under_the_import(db_url, tmp_path):
    body = """
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    mentor: Annotated["Player", ForeignKey(nullable=True, related_name="mentees")] = None
    mentees: Relation[list["Player"]] = BackRef()
"""
    got = await _same_ddl_one(db_url, tmp_path, body)
    assert "NOT NULL" not in got.split('"mentor_id"')[1].split(",")[0]


async def _same_ddl_one(db_url, tmp_path, body) -> str:
    got = await _create_statements(
        db_url, tmp_path, body, future=True, drop=("player",)
    )
    want = await _create_statements(
        db_url, tmp_path, body, future=False, drop=("player",)
    )
    assert got == want
    return next(s for s in got if "CREATE TABLE" in s)


OUTER = [
    ('Annotated[Author, ForeignKey(related_name="posts")] | None', " = None", False),
    ('Optional[Annotated[Author, ForeignKey(related_name="posts")]]', " = None", False),
    ('Annotated[Author, ForeignKey(related_name="posts")] | None', " = None", True),
    ('Optional[Annotated[Author, ForeignKey(related_name="posts")]]', " = None", True),
]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize(("annotation", "extra", "future"), OUTER)
async def test_outer_optional_equals_the_inner_form(
    db_url, tmp_path, annotation, extra, future
):
    got = await _same_ddl(
        db_url,
        tmp_path,
        _post(annotation, extra=extra),
        _post(INNER, extra=" = None"),
        future=future,
    )
    post = next(s for s in got if "CREATE TABLE" in s and '"post"' in s)
    assert '"author_id" integer NOT NULL' not in post
    assert "fk_post_author_id_author" in post


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize("future", [False, True])
async def test_outer_optional_with_explicit_nullable_false_is_not_null(
    db_url, tmp_path, future
):
    outer = 'Annotated[Author, ForeignKey(related_name="posts", nullable=False)] | None'
    inner = 'Annotated[Author | None, ForeignKey(related_name="posts", nullable=False)]'
    got = await _same_ddl(
        db_url,
        tmp_path,
        _post(outer, extra=" = None"),
        _post(inner, extra=" = None"),
        future=future,
    )
    post = next(s for s in got if "CREATE TABLE" in s and '"post"' in s)
    assert '"author_id" integer NOT NULL' in post


def test_outer_optional_set_null_with_nullable_false_is_refused(tmp_path):
    _wipe()
    body = _post(
        'Annotated[Author, ForeignKey(related_name="posts", on_delete="SET NULL", '
        "nullable=False)] | None",
        extra=" = None",
    )
    with pytest.raises(ValueError):
        _import(tmp_path, body, future=False)


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_outer_optional_include_of_a_null_row_is_none(db_url, tmp_path):
    _wipe()
    mod = _import(
        tmp_path,
        _post(
            'Annotated[Author, ForeignKey(related_name="posts")] | None',
            extra=" = None",
        ),
        future=True,
    )
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await mod.Author(name="Ada").save()
        await mod.Post(title="with", author_id=1).save()
        await mod.Post(title="without").save()
        posts = (
            await mod.Post.select()
            .include(lambda post: post.author)
            .order_by("id")
            .all()
        )
        assert posts[0].author.name == "Ada"
        assert posts[1].author is None


def test_unresolvable_name_outside_the_target_is_refused(tmp_path):
    _wipe()
    body = _post("Annotated[Author, ForeignKey(related_name=posts_name)]")
    with pytest.raises(TypeError) as excinfo:
        _import(tmp_path, body, future=True)
    text = str(excinfo.value)
    assert "Post.author" in text
    assert "'posts_name'" in text
    assert 'Annotated["Author", ForeignKey(...)]' in text
    assert not any(key.endswith(".Post") for key in REGISTRY.models())


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_target_never_defined_fails_at_connect(db_url, tmp_path):
    _wipe()
    _import(
        tmp_path,
        """
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    author: Annotated[Nowhere, ForeignKey(related_name="posts")]
""",
        future=True,
    )
    with pytest.raises(
        RuntimeError, match="Relationship resolution failed: 'Nowhere' not found"
    ):
        await ferro.connect(db_url, auto_migrate=True)


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_back_ref_and_many_to_many_still_resolve(db_url, tmp_path):
    _wipe()
    mod = _import(
        tmp_path,
        """
class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    posts: Relation[list["Post"]] = BackRef()

class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    posts: Relation[list["Post"]] = ManyToMany(related_name="tags")

class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    author: Annotated[Author, ForeignKey(related_name="posts")]
    tags: Relation[list["Tag"]] = ManyToMany(related_name="posts")
""",
        future=True,
    )
    await ferro.connect(db_url, auto_migrate=True)
    assert mod.Post.__ferro_relations__["author"].to is mod.Author
    assert mod.Post.__ferro_relation_specs__["author"].fk_column == "author_id"
    assert "author_id" in mod.Post.__ferro_columns__
    assert "BackRef" in mod.Author.__ferro_relations__.values()
    assert mod.Tag.__ferro_relations__["posts"].related_name == "tags"
    assert any(
        "post_tag" in name or "tag_post" in name for name in REGISTRY.join_tables()
    )


def test_a_model_defined_in_a_function_finds_its_sibling_target():
    class Owner(Model):
        id: int | None = Field(default=None, primary_key=True)
        pets: Relation[list[Pet]] = BackRef()

    class Pet(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner: Annotated[Owner, ForeignKey(related_name="pets")]

    assert Pet.__ferro_relations__["owner"].to is Owner
    assert "owner_id" in Pet.model_fields


def test_undefined_module_in_the_target_names_the_written_target(tmp_path):
    _wipe()
    body = """
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    author: Annotated[models.Author, ForeignKey(related_name="posts")]
"""
    with pytest.raises(TypeError) as excinfo:
        _import(tmp_path, body, future=True)
    text = str(excinfo.value)
    assert "'models'" in text
    assert 'Annotated["models.Author", ForeignKey(...)]' in text


def test_explicit_foreign_keys_resolve_under_postponed_annotations(tmp_path):
    _wipe()
    mod = _import(
        tmp_path,
        """
from ferro import Forward, Reverse

class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation[list["Player"]] = Reverse("team_id")

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team, index=True)
    team: Relation[Team] = Forward("team_id")
    mentor_id: int | None = Field(default=None, foreign_key="Player", on_delete="SET NULL")
    mentor: Relation[Player | None] = Forward("mentor_id")
""",
        future=True,
    )
    ferro.ensure_resolved_modelset()

    team = mod.Player.__ferro_relation_specs__["team"]
    mentor = mod.Player.__ferro_relation_specs__["mentor"]
    assert (team.target, team.fk_column, team.nullable) == (mod.Team, "team_id", False)
    assert (mentor.target, mentor.fk_column, mentor.nullable) == (
        mod.Player,
        "mentor_id",
        True,
    )
    assert mod.Team.__ferro_reverse_specs__["players"].child_fk_column == "team_id"


def test_typing_spellings_of_a_relation_resolve_under_postponed_annotations(
    tmp_path,
):
    _wipe()
    mod = _import(
        tmp_path,
        """
from typing import List, Union

from ferro import Forward, Reverse

class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation[List["Player"]] = Reverse("team_id")

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: Optional[int] = Field(default=None, foreign_key=Team)
    team: Relation[Optional[Team]] = Forward("team_id")
    mentor_id: Optional[int] = Field(default=None, foreign_key="Player")
    mentor: Relation[Union["Player", None]] = Forward("mentor_id")
""",
        future=True,
    )
    ferro.ensure_resolved_modelset()

    team = mod.Player.__ferro_relation_specs__["team"]
    mentor = mod.Player.__ferro_relation_specs__["mentor"]
    assert (team.target, team.nullable) == (mod.Team, True)
    assert (mentor.target, mentor.nullable) == (mod.Player, True)
    assert mod.Team.__ferro_reverse_specs__["players"].child_fk_column == "team_id"


def test_a_relation_in_annotated_metadata_is_refused_under_postponed_annotations(
    tmp_path,
):
    _wipe()
    body = """
from ferro import Forward

class Team(Model):
    id: int | None = Field(default=None, primary_key=True)

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)
    team: Annotated[Relation[Team], Forward("team_id")]
"""
    with pytest.raises(TypeError) as refusal:
        _import(tmp_path, body, future=True)

    assert str(refusal.value) == (
        "Player.team: a relation is an assignment, not Annotated metadata. "
        'Write: team: Relation[Team] = Forward("team_id")'
    )


# Each case below runs with and without the __future__ import, and with the
# target declared before the model that names it ("first") or after it
# ("second"), where the target is a name until relations resolve.

_TEAM = """
class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
"""

_CASES = [(future, order) for future in (False, True) for order in ("first", "second")]


def _modelset(order: str, team: str, player: str) -> str:
    imports = "from typing import Literal\n\nfrom ferro import Forward, Reverse\n"
    return imports + (team + player if order == "first" else player + team)


def _written(name: str, future: bool, later: bool) -> str:
    """How a Write line names a target: quoted when it is declared later in a
    module that evaluates its annotations."""
    return f'"{name}"' if later and not future else name


def _refusal(tmp_path, body: str, future: bool) -> str:
    """The refusal ``body`` meets, at class definition or when relations resolve."""
    _wipe()
    with pytest.raises(TypeError) as refusal:
        _import(tmp_path, body, future=future)
        ferro.ensure_resolved_modelset()
    return str(refusal.value)


@pytest.mark.parametrize(("future", "order"), _CASES)
def test_a_bare_reverse_relation_names_the_model_pointing_back(tmp_path, future, order):
    team = """
class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation = Reverse("team_id")
"""
    target = "Team" if order == "first" else '"Team"'
    player = f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key={target})
"""
    assert _refusal(tmp_path, _modelset(order, team, player), future) == (
        "Team.players: a relation is annotated Relation[...]. "
        f"Write: players: Relation[list[{_written('Player', future, order == 'first')}]] = "
        'Reverse("team_id")'
    )


@pytest.mark.parametrize(("future", "order"), _CASES)
def test_a_bare_forward_relation_names_its_annotated_columns_target(
    tmp_path, future, order
):
    target = "Team" if order == "first" else '"Team"'
    player = f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: Annotated[int | None, Field(foreign_key={target})] = None
    team: Relation = Forward("team_id")
"""
    assert _refusal(tmp_path, _modelset(order, _TEAM, player), future) == (
        "Player.team: a relation is annotated Relation[...]. "
        f"Write: team: Relation[{_written('Team | None', future, order == 'second')}] = "
        'Forward("team_id")'
    )


@pytest.mark.parametrize(("future", "order"), _CASES)
@pytest.mark.parametrize("call", ['Forward(column="team_id")', "Forward(TEAM_COLUMN)"])
def test_a_relation_in_annotated_metadata_is_refused_however_it_is_spelled(
    tmp_path, future, order, call
):
    target = "Team" if order == "first" else '"Team"'
    player = f"""
TEAM_COLUMN = "team_id"

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key={target})
    team: Annotated[Relation[{target}], {call}]
"""
    assert _refusal(tmp_path, _modelset(order, _TEAM, player), future) == (
        "Player.team: a relation is an assignment, not Annotated metadata. "
        f"Write: team: Relation[{_written('Team', future, order == 'second')}] = "
        'Forward("team_id")'
    )


@pytest.mark.parametrize(("future", "order"), _CASES)
def test_an_annotated_relation_assigned_its_marker_is_a_relation(
    tmp_path, future, order
):
    _wipe()
    target = "Team" if order == "first" else '"Team"'
    player = f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key={target})
    team: Annotated[Relation[{target}], "the player's team"] = Forward("team_id")
"""
    mod = _import(tmp_path, _modelset(order, _TEAM, player), future=future)
    ferro.ensure_resolved_modelset()

    spec = mod.Player.__ferro_relation_specs__["team"]
    assert (spec.target, spec.fk_column) == (mod.Team, "team_id")


@pytest.mark.parametrize("future", [False, True])
def test_a_literal_value_with_a_quote_prints_as_it_compiles(tmp_path, future):
    player = """
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    side: Literal["it's", 'say "hi"'] | None = Field(default=None, on_delete="CASCADE")
"""
    message = _refusal(tmp_path, _modelset("first", "", player), future)

    line = message.split("Write: ", 1)[1]
    assert line == (
        """side: Literal["it's", "say \\"hi\\""] | None = Field(default=None)"""
    )
    compile(line, "<refusal>", "exec")


@pytest.mark.parametrize(("future", "order"), _CASES)
@pytest.mark.parametrize(
    "wrapper",
    [
        'Annotated[Relation[{target}], Forward("team_id")] | None',
        'Optional[Annotated[Relation[{target}], Forward("team_id")]]',
    ],
)
def test_a_relation_in_annotated_metadata_under_none_is_refused(
    tmp_path, future, order, wrapper
):
    target = "Team" if order == "first" else '"Team"'
    player = f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int | None = Field(default=None, foreign_key={target})
    team: {wrapper.format(target=target)} = None
"""
    assert _refusal(tmp_path, _modelset(order, _TEAM, player), future) == (
        "Player.team: a relation is an assignment, not Annotated metadata. "
        f"Write: team: Relation[{_written('Team | None', future, order == 'second')}] = "
        'Forward("team_id")'
    )


def test_a_bare_reverse_names_every_model_pointing_back(tmp_path):
    body = """
from ferro import Reverse

class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation = Reverse("team_id")

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)

class Coach(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)
"""
    assert _refusal(tmp_path, body, future=False) == (
        "Team.players: a relation is annotated Relation[...], and Player and "
        "Coach each declare team_id pointing at Team. Write: players: "
        'Relation[list["Player"]] = Reverse("team_id") or players: '
        'Relation[list["Coach"]] = Reverse("team_id")'
    )


def test_a_bare_reverse_no_model_points_back_to_names_the_column_to_declare(
    tmp_path,
):
    body = """
from ferro import Reverse

class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation = Reverse("team_id")
"""
    assert _refusal(tmp_path, body, future=False) == (
        "Team.players: a relation is annotated Relation[...], and no model "
        "declares a foreign-key column team_id pointing at Team. Declare "
        "team_id: int = Field(foreign_key=Team) on the model the relation "
        "reads, and annotate players as Relation[list[...]] of that model"
    )


@pytest.mark.parametrize("column", ["team_id: int", ""])
def test_a_bare_forward_with_no_target_named_gives_the_recipe(tmp_path, column):
    body = f"""
from ferro import Forward

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    {column}
    team: Relation = Forward("team_id")
"""
    missing = "team_id has no foreign_key" if column else "Player has no column team_id"
    assert _refusal(tmp_path, body, future=False) == (
        f"Player.team: a relation is annotated Relation[...], and {missing} to "
        "name the model it reads. Declare team_id with Field(foreign_key=...) "
        "naming that model, and annotate team as Relation[...] of the same model"
    )


def test_an_aliased_annotated_holding_a_relation_is_refused(tmp_path):
    body = """
from typing import Annotated as A

from ferro import Forward

class Team(Model):
    id: int | None = Field(default=None, primary_key=True)

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)
    team: A[Relation[Team], Forward("team_id")]
"""
    assert _refusal(tmp_path, body, future=True) == (
        "Player.team: a relation is an assignment, not Annotated metadata. "
        'Write: team: Relation[Team] = Forward("team_id")'
    )


# -- the spelling sweep ---------------------------------------------------------
#
# Every relation form under every placement of its annotation, with and without
# postponed annotations, the target written as a class or a string and declared
# before or after the model that names it, over a required or a nullable key
# column, with the marker's column positional or by keyword. Each outcome is a
# working relation, or a refusal whose every Write line, pasted in place of the
# refused one, defines a working relation.

_SPELLINGS = {
    "assigned": "{relation} = {marker}",
    "assigned with Annotated documentation": (
        'Annotated[{relation}, "documentation"] = {marker}'
    ),
    "Annotated metadata": "Annotated[{relation}, {marker}]",
    "Annotated metadata | None": "Annotated[{relation}, {marker}] | None = None",
    "Optional Annotated metadata": ("Optional[Annotated[{relation}, {marker}]] = None"),
    "Union Annotated metadata": ("Union[Annotated[{relation}, {marker}], None] = None"),
    "aliased Annotated metadata": "A[{relation}, {marker}]",
    "assigned under | None": "{relation} | None = {marker}",
    "bare Relation": "Relation = {marker}",
    "bare Relation | None": "Relation | None = {marker}",
}

_ASSIGNED = ("assigned", "assigned with Annotated documentation")

# ManyToMany keeps its old-form declaration in this slice, which reads the
# marker from Annotated metadata too (as BackRef always has), so every
# Annotated placement is a working relation; its bare forms are left to the
# slice that redeclares it.
_M2M_SPELLINGS = [
    name
    for name in _SPELLINGS
    if "bare" not in name and name != "assigned under | None"
]

_FIELD = {"Forward": "team", "Reverse": "players", "ManyToMany": "tags"}

_MARKERS = {
    ("Forward", "positional"): 'Forward("team_id")',
    ("Forward", "keyword"): 'Forward(column="team_id")',
    ("Reverse", "positional"): 'Reverse("team_id")',
    ("Reverse", "keyword"): 'Reverse(attribute="team_id")',
    ("ManyToMany", "positional"): 'ManyToMany(related_name="posts")',
}


def _key(target: str, nullable: bool) -> str:
    if nullable:
        return f"team_id: int | None = Field(default=None, foreign_key={target})"
    return f"team_id: int = Field(foreign_key={target})"


def _sweep_body(form: str, field_line: str, nullable: bool, order: str) -> str:
    """The modelset whose relation field is declared by ``field_line``."""
    late = order == "after"
    if form == "Forward":
        owner = f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    {_key('"Team"' if late else "Team", nullable)}
    {field_line}
"""
        other = _TEAM
    elif form == "Reverse":
        owner = f"""
class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    {field_line}
"""
        other = f"""
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    {_key('"Team"', nullable)}
"""
    else:
        owner = f"""
class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    {field_line}
"""
        other = """
class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    posts: Relation[list["Post"]] = BackRef()
"""
    imports = (
        "from typing import Annotated as A, Union\n\n"
        "from ferro import Forward, Reverse\n"
    )
    return imports + (owner + other if late else other + owner)


def _declared_field(form: str, spelling: str, quoted: bool, marker: str) -> str:
    target = {"Forward": "Team", "Reverse": "Player", "ManyToMany": "Tag"}[form]
    written = f'"{target}"' if quoted else target
    relation = (
        f"Relation[{written}]" if form == "Forward" else f"Relation[list[{written}]]"
    )
    annotation = _SPELLINGS[spelling].format(
        relation=relation, marker=_MARKERS[(form, marker)]
    )
    return f"{_FIELD[form]}: {annotation}"


def _defines_the_relation(tmp_path, form: str, body: str, future: bool) -> None:
    """Import ``body`` and resolve it; the relation must work."""
    _wipe()
    mod = _import(tmp_path, body, future=future)
    ferro.ensure_resolved_modelset()
    if form == "Forward":
        assert mod.Player.__ferro_relation_specs__["team"].target is mod.Team
    elif form == "Reverse":
        assert mod.Team.__ferro_reverse_specs__["players"].target is mod.Player
    else:
        assert mod.Post.__ferro_reverse_specs__["tags"].is_m2m


_SWEEP = [
    (form, spelling, future, quoted, order, nullable, marker)
    for form in ("Forward", "Reverse", "ManyToMany")
    for spelling in (_M2M_SPELLINGS if form == "ManyToMany" else _SPELLINGS)
    for future in (False, True)
    for quoted in (False, True)
    for order in ("before", "after")
    for nullable in ((False,) if form == "ManyToMany" else (False, True))
    for marker in (
        ("positional",) if form == "ManyToMany" else ("positional", "keyword")
    )
    # Without postponed annotations an unquoted later class is a NameError.
    if future or quoted or order == "before"
]


@pytest.mark.parametrize(
    ("form", "spelling", "future", "quoted", "order", "nullable", "marker"), _SWEEP
)
def test_every_spelling_is_a_relation_or_a_refusal_whose_fix_works(
    tmp_path, form, spelling, future, quoted, order, nullable, marker
):
    field = _declared_field(form, spelling, quoted, marker)
    body = _sweep_body(form, field, nullable, order)
    # Only a Relation[...] assigned its marker is a relation, and a forward
    # one over a nullable column must say `| None`; a ManyToMany keeps the
    # old form's reading of Annotated metadata.
    works = form == "ManyToMany" or (
        spelling in _ASSIGNED and not (form == "Forward" and nullable)
    )
    _wipe()
    try:
        _import(tmp_path, body, future=future)
        ferro.ensure_resolved_modelset()
    except TypeError as refusal:
        assert not works, f"{body}\nrefused: {refusal}"
        message = str(refusal)
        assert "Write: " in message, message
        for line in message.split("Write: ", 1)[1].split(" or "):
            fixed = _sweep_body(form, line, nullable, order)
            try:
                _defines_the_relation(tmp_path, form, fixed, future)
            except Exception as error:
                raise AssertionError(
                    f"the fix {line!r} for\n{body}\ndoes not work: {error}"
                ) from error
        return
    assert works, f"{body}\nwas accepted"
    _defines_the_relation(tmp_path, form, body, future)
