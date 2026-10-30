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
    assert mod.Post.ferro_relations["author"].to is mod.Author
    assert mod.Post.__ferro_relation_specs__["author"].shadow_column == "author_id"
    assert "author_id" in mod.Post.__ferro_columns__
    assert "BackRef" in mod.Author.ferro_relations.values()
    assert mod.Tag.ferro_relations["posts"].related_name == "tags"
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

    assert Pet.ferro_relations["owner"].to is Owner
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
