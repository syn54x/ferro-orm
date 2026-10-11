"""Retired columns: a field leaves the model one release before it is dropped.

```python
class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    bio: str | None = Field(retired=True)
```

From this release on ``author.bio`` raises ``AttributeError``,
``Author(bio="x")`` raises ``TypeError``, and no ``SELECT``, ``INSERT`` or
``UPDATE`` the model issues names ``bio``, so the code works whether the column
is still there or the next release already dropped it. The column stays in the
schema snapshot, nullable, with its index (ADR-0053).
"""

from enum import StrEnum
from typing import Annotated, Optional

import pytest

import ferro
from ferro import (
    BackRef,
    Check,
    Field,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
)
from ferro.base import FerroField
from ferro.ir import compile_registry_schema_ir
from ferro.raw import execute, fetch_all
from ferro.relations import resolve_relationships
from tests._alembic_harness import autogenerate, run_revision
from tests._pass_harness import schema_steps, warning_texts

pytestmark = pytest.mark.usefixtures("clean_registry")

RETIRED = "retired in this release"


def _model(table: str) -> dict:
    envelope = compile_registry_schema_ir()
    return next(m for m in envelope["payload"]["models"] if m["table_name"] == table)


def _column(table: str, name: str) -> dict:
    return next(c for c in _model(table)["columns"] if c["name"] == name)


def _assignment_author() -> type[Model]:
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        bio: str | None = Field(index=True, retired=True)

    return Author


def _annotated_author() -> type[Model]:
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        bio: Annotated[str | None, FerroField(index=True, retired=True)] = None

    return Author


STYLES = pytest.mark.parametrize(
    "declare", [_assignment_author, _annotated_author], ids=["assignment", "annotated"]
)


# -- the declaration ------------------------------------------------------------------


@STYLES
def test_a_retired_field_leaves_the_model_and_stays_in_the_snapshot(declare):
    Author = declare()

    assert "bio" not in Author.model_fields
    assert "bio" not in Author.__ferro_columns__
    assert "bio" not in Author.__ferro_query_columns__
    with pytest.raises(AttributeError, match=RETIRED):
        _ = Author(name="a").bio

    column = _column("author", "bio")
    assert column["retired"] is True
    assert column["nullable"] is True
    assert column["logical_type"] == "string"
    assert "idx_author_bio" in {index["name"] for index in _model("author")["indexes"]}
    assert "retired" not in _column("author", "name")


@STYLES
def test_constructing_with_a_retired_field_is_a_type_error_naming_it(declare):
    Author = declare()

    with pytest.raises(TypeError, match=r"Author\.bio is retired"):
        Author(name="a", bio="x")


def test_a_retired_required_column_is_nullable_in_the_snapshot():
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        nickname: str | None = Field(retired=True, nullable=False)

    assert _column("author", "nickname")["nullable"] is True


def test_a_retired_foreign_key_leaves_the_model_and_keeps_its_constraint():
    class Team(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        team: Annotated[Team | None, ForeignKey(retired=True, index=True)] = None

    resolve_relationships()

    assert "team_id" not in Author.model_fields
    assert "team_id" not in Author.__ferro_columns__
    assert "team" not in Author.__ferro_relation_specs__
    author = Author(name="a")
    for attribute in ("team", "team_id"):
        with pytest.raises(AttributeError, match=RETIRED):
            getattr(author, attribute)
    for keyword in ("team", "team_id"):
        with pytest.raises(TypeError, match=rf"Author\.{keyword} is retired"):
            Author(name="a", **{keyword: 1})

    column = _column("author", "team_id")
    assert column["retired"] is True
    assert column["nullable"] is True
    assert column["logical_type"] == "integer"
    model = _model("author")
    assert [fk["name"] for fk in model["foreign_keys"]] == ["fk_author_team_id_team"]
    assert "idx_author_team_id" in {index["name"] for index in model["indexes"]}


def test_a_retired_foreign_key_column_is_the_mapped_one_but_for_the_flag():
    """The shadow column a retired relation keeps is the column the mapped
    relation declares, nullable: the IR differs by the flag alone."""

    class Team(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        team: Annotated[Team | None, ForeignKey(retired=True)] = None

    resolve_relationships()
    retired = _column("author", "team_id")
    ferro.clear_registry()

    class Team(Model):  # noqa: F811 - the same model, mapped
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        authors: Relation[list["Author"]] = BackRef()

    class Author(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        team: Annotated[Team | None, ForeignKey(related_name="authors")] = None

    resolve_relationships()
    assert {**_column("author", "team_id"), "retired": True} == retired


def test_a_table_check_composite_index_and_policy_may_name_a_retired_column():
    class Author(Model):
        __ferro_checks__ = (Check("bio_short", lambda author: author.bio != "x"),)
        __ferro_composite_indexes__ = (("name", "bio"),)
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        bio: str | None = Field(retired=True)

    model = _model("author")
    assert [check["name"] for check in model["table_checks"]] == ["ck_author_bio_short"]
    assert "idx_author_name_bio" in {index["name"] for index in model["indexes"]}


# -- every query surface refuses it ---------------------------------------------------


def test_a_query_lambda_naming_a_retired_column_raises_naming_it():
    Author = _assignment_author()

    with pytest.raises(AttributeError, match=RETIRED):
        Author.where(lambda author: author.bio == "x")
    with pytest.raises(AttributeError, match=RETIRED):
        Author.select().order_by(lambda author: author.bio)


@pytest.mark.asyncio
async def test_update_naming_a_retired_column_raises_naming_it():
    Author = _assignment_author()

    with pytest.raises(AttributeError, match=RETIRED):
        await Author.where(lambda author: author.name == "a").update(bio="x")


# -- refused at class creation --------------------------------------------------------


def test_retiring_a_non_optional_field_is_refused_naming_the_fix():
    with pytest.raises(
        TypeError,
        match=r"Author\.bio is retired but not optional: declare it `str \| None`",
    ):

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            bio: str = Field(retired=True)


def test_retiring_a_non_optional_foreign_key_is_refused_naming_the_fix():
    class Team(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    with pytest.raises(
        TypeError,
        match=r"Author\.team is retired but not optional: declare it `Team \| None`",
    ):

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            team: Annotated[Team, ForeignKey(retired=True)]


def test_retiring_a_primary_key_is_refused():
    with pytest.raises(TypeError, match=r"Author\.id: a primary key cannot be retired"):

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True, retired=True)] = None


def test_retiring_a_renamed_field_is_refused_naming_the_fix():
    with pytest.raises(
        TypeError,
        match=(
            r"Author\.bio: retire it or rename it, not both; drop `renamed_from` "
            r"once its migration is generated"
        ),
    ):

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            bio: str | None = Field(retired=True, renamed_from="about")


def test_a_back_ref_over_a_retired_foreign_key_is_refused_naming_the_fix():
    class Team(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        members: Relation[list["Author"]] = BackRef()

    with pytest.raises(
        TypeError,
        match=(
            r"Team\.members is a BackRef over the retired relation Author\.team; "
            r"delete the BackRef"
        ),
    ):

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            team: Annotated[
                Team | None, ForeignKey(related_name="members", retired=True)
            ] = None

        resolve_relationships()


def test_a_back_ref_declared_after_its_retired_foreign_key_is_refused_at_resolution():
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        team: Annotated[
            Optional["Team"], ForeignKey(related_name="members", retired=True)
        ] = None

    class Team(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        members: Relation[list["Author"]] = BackRef()

    with pytest.raises(
        TypeError,
        match=r"Team\.members is a BackRef over the retired relation Author\.team",
    ):
        resolve_relationships()


def test_a_back_ref_and_a_many_to_many_cannot_be_retired():
    with pytest.raises(TypeError, match=r"Team\.members: a BackRef cannot be retired"):

        class Team(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            members: Relation[list["Author"]] = BackRef(retired=True)  # noqa: F821

    with pytest.raises(TypeError, match=r"Post\.tags: a ManyToMany cannot be retired"):

        class Post(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            tags: Relation[list["Tag"]] = ManyToMany(  # noqa: F821
                related_name="posts", retired=True
            )


def test_a_foreign_key_without_related_name_is_refused_unless_retired():
    with pytest.raises(TypeError, match="related_name"):
        ForeignKey()
    assert ForeignKey(retired=True).retired is True


# -- the round trip -------------------------------------------------------------------


async def _round_trip(Author: type[Model]) -> None:
    saved = Author(name="saved")
    await saved.save()
    assert await Author.bulk_create([Author(name="b1"), Author(name="b2")]) == 2
    updated = await Author.where(lambda author: author.name == "b1").update(name="b9")
    assert updated == 1
    saved.name = "resaved"
    await saved.save(only={"name"})
    with pytest.raises(ValueError, match=RETIRED):
        await saved.save(only={"bio"})

    rows = await Author.select().order_by(lambda author: author.name).all()
    assert [row.name for row in rows] == ["b2", "b9", "resaved"]
    for row in rows:
        assert "bio" not in row.__dict__
        assert "bio" not in row.model_dump()
        with pytest.raises(AttributeError, match=RETIRED):
            _ = row.bio
    fetched = await Author.get(saved.id)
    assert fetched is not None and "bio" not in fetched.__dict__


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@STYLES
async def test_a_retired_column_the_table_still_holds_is_never_read_or_written(
    declare, db_url
):
    Author = declare()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        # A row the previous release wrote: the new release never hydrates it.
        await execute("INSERT INTO author (name, bio) VALUES ('old', 'kept')")
        old = await Author.where(lambda author: author.name == "old").first()
        assert old is not None and "bio" not in old.__dict__
        await Author.where(lambda author: author.name == "old").delete()

        await _round_trip(Author)
        held = await fetch_all("SELECT bio FROM author WHERE bio IS NOT NULL")
        assert held == []


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_retired_column_dropped_by_hand_changes_nothing(db_url):
    class Team(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        bio: str | None = Field(retired=True)
        team: Annotated[Team | None, ForeignKey(retired=True)] = None

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await execute("ALTER TABLE author DROP COLUMN bio")
        if not db_url.startswith("sqlite"):
            # SQLite cannot drop a column its FOREIGN KEY clause names.
            await execute("ALTER TABLE author DROP COLUMN team_id")
    ferro.reset_engine()
    await ferro.connect(db_url)
    async with ferro.engines.session():
        await _round_trip(Author)


# -- the reconciliation pass and the Alembic bridge -----------------------------------


class Mood(StrEnum):
    CALM = "calm"
    LOUD = "loud"


def _release_a() -> type[Model]:
    """The previous release: ``mood`` is required, with an index."""

    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        mood: Annotated[Mood, FerroField(index=True)]

    return Author


def _release_b() -> type[Model]:
    """This release: ``mood`` is retired."""

    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        mood: Annotated[Mood | None, FerroField(index=True, retired=True)] = None

    return Author


def _rewind() -> None:
    from ferro.registry import REGISTRY

    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()


async def _live_nullable(db_url: str, column: str) -> bool:
    """Whether the live ``author.<column>`` accepts ``NULL``."""
    async with ferro.engines.session():
        if db_url.startswith("sqlite"):
            rows = await fetch_all("PRAGMA table_info(author)")
            return next(r for r in rows if r["name"] == column)["notnull"] == 0
        rows = await fetch_all(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'author' AND column_name = $1 "
            "AND table_schema = current_schema()",
            column,
        )
        return rows[0]["is_nullable"] == "YES"


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_the_create_pass_creates_a_retired_column_nullable_with_its_index(
    db_url,
):
    _release_b()
    await ferro.connect(db_url, auto_migrate=True)

    assert await _live_nullable(db_url, "mood")
    async with ferro.engines.session():
        report = await ferro.migrate(destructive=True)
    assert schema_steps(report) == []


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_migrate_updates_relaxes_not_null_and_destructive_never_drops(db_url):
    _release_a()
    await ferro.connect(db_url, auto_migrate=True)
    assert not await _live_nullable(db_url, "mood")
    _rewind()
    _release_b()

    await ferro.connect(db_url, auto_migrate=True, migrate_updates=True)

    assert await _live_nullable(db_url, "mood")
    async with ferro.engines.session():
        report = await ferro.migrate(destructive=True)
    # Nothing is left to change, and the destructive rung drops nothing: the
    # retired column, its index and its type are all declared.
    assert schema_steps(report) == []
    assert warning_texts(report) == []


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_alembic_autogenerate_renders_only_the_relaxation(
    db_url, postgres_base_url, db_schema_name
):
    _release_a()
    await ferro.connect(db_url, auto_migrate=True)
    _rewind()
    _release_b()

    upgrade, _ = autogenerate(db_url, postgres_base_url, db_schema_name)

    lines = [line.strip() for line in upgrade.splitlines() if line.strip()]
    ops = [line for line in lines if line.startswith("op.")]
    assert len(ops) == 1, upgrade
    assert ops[0].startswith("op.alter_column('author', 'mood'"), upgrade
    assert "nullable=True" in upgrade, upgrade
    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    await ferro.connect(db_url)
    assert await _live_nullable(db_url, "mood")
