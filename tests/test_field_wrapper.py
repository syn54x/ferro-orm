import json
from enum import StrEnum
from typing import Annotated

import pydantic
import pytest
from pydantic.fields import FieldInfo

from ferro import (
    BackRef,
    FerroField,
    Field,
    ManyToMany,
    Model,
    Relation,
    connect,
    create_tables,
    engines,
)
from ferro.columns import build_column_specs

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_ferro_field_wrapper_sets_metadata_and_pydantic_schema(db_url):
    class WrappedUser(Model):
        id: int | None = Field(
            default=None, primary_key=True, description="Primary key"
        )
        email: str = Field(unique=True, index=True, description="Email address")

    # Ferro metadata is captured for both fields.
    assert WrappedUser.__ferro_fields__["id"].primary_key is True
    assert WrappedUser.__ferro_fields__["email"].unique is True
    assert WrappedUser.__ferro_fields__["email"].index is True

    # Pydantic metadata still works via wrapped Field.
    schema = WrappedUser.model_json_schema()
    assert schema["properties"]["id"]["description"] == "Primary key"
    assert schema["properties"]["email"]["description"] == "Email address"

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        user = WrappedUser(email="one@example.com")
        await user.save()
        assert user.id is not None


def test_assignment_and_annotation_field_patterns_equivalent_ferro_metadata():
    """Assignment `x: T = Field(...)` and annotation `x: Annotated[T, Field(...)]` must
    yield the same Ferro column metadata (Pydantic merges Annotated Field into FieldInfo).
    """

    class ByAssignment(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: int = Field(default=0, unique=True, index=True)
        name: str

    class ByAnnotation(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        code: Annotated[int, Field(default=0, unique=True, index=True)]
        name: str

    assert set(ByAssignment.__ferro_fields__) == set(ByAnnotation.__ferro_fields__)
    for fname in ByAssignment.__ferro_fields__:
        a = ByAssignment.__ferro_fields__[fname]
        b = ByAnnotation.__ferro_fields__[fname]
        assert (a.primary_key, a.unique, a.index, a.autoincrement) == (
            b.primary_key,
            b.unique,
            b.index,
            b.autoincrement,
        )


@pytest.mark.asyncio
async def test_annotation_field_pattern_persists_like_assignment(db_url):
    """Annotated[..., Field(...)] should behave the same as assignment for DB round-trip."""

    class AnnotatedUser(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        email: Annotated[str, Field(unique=True, index=True, description="Login email")]

    assert AnnotatedUser.__ferro_fields__["id"].primary_key is True
    assert AnnotatedUser.__ferro_fields__["email"].unique is True
    schema = AnnotatedUser.model_json_schema()
    assert schema["properties"]["email"]["description"] == "Login email"

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        user = AnnotatedUser(email="ann@example.com")
        await user.save()
        assert user.id is not None


@pytest.mark.asyncio
async def test_optional_patterned_string_roundtrip(db_url):
    class WrappedInventory(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str | None = Field(default=None, pattern=r"^SKU-[0-9]+$")

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        item = await WrappedInventory.create(code="SKU-123")
        fetched = await WrappedInventory.get(item.id)

        assert fetched is not None
        assert fetched.code == "SKU-123"


def test_annotated_and_wrapped_ferro_field_conflict_raises():
    with pytest.raises(TypeError, match="cannot declare Ferro field metadata twice"):

        class InvalidUser(Model):
            id: Annotated[int, FerroField(primary_key=True)] = Field(primary_key=True)


# Field options live on the field, never in the JSON schema (#875).

_OPTION_NAMES = (
    "ferro_field",
    "primary_key",
    "autoincrement",
    "unique",
    "index",
    "nullable",
    "ferro_nullable",
    "db_type",
    "db_check",
    "renamed_from",
    "retired",
    "generated",
    "back_ref",
    "many_to_many",
    "related_name",
    "through",
    "reverse_index",
)


def _schema_option_names(model: type[Model]) -> set[str]:
    """Every Ferro option name found anywhere in the model's dumped JSON schema."""
    dumped = json.dumps(model.model_json_schema())
    return {name for name in _OPTION_NAMES if f'"{name}"' in dumped}


class _Mood(StrEnum):
    CALM = "calm"
    STORMY = "stormy"


def test_field_options_leave_the_json_schema_and_land_in_ferro_fields():
    class OptionedPlayer(Model):
        id: int | None = Field(default=None, primary_key=True, autoincrement=True)
        handle: str = Field(unique=True, min_length=2)
        rank: int = Field(index=True)
        nickname: str | None = Field(default=None, nullable=True)
        mood: _Mood = Field(db_type="text", db_check=True)
        score: int = Field(renamed_from="points")
        legacy: str | None = Field(default=None, retired=True)
        doubled: int = Field(generated=lambda optioned_player: optioned_player.rank * 2)

    assert _schema_option_names(OptionedPlayer) == set()
    assert OptionedPlayer.model_json_schema()["properties"]["handle"] == {
        "minLength": 2,
        "title": "Handle",
        "type": "string",
    }
    fields = OptionedPlayer.__ferro_fields__
    assert fields["id"].primary_key is True
    assert fields["id"].autoincrement is True
    assert fields["handle"].unique is True
    assert fields["rank"].index is True
    assert fields["nickname"].nullable is True
    assert (fields["mood"].db_type, fields["mood"].db_check) == ("text", True)
    assert fields["score"].renamed_from == "points"
    assert "legacy" in OptionedPlayer.__ferro_retired_fields__
    assert fields["doubled"].generated is not None


def _option_tuple(options: FerroField) -> tuple:
    return (
        options.primary_key,
        options.autoincrement,
        options.unique,
        options.index,
        options.nullable,
        options.db_type,
        options.db_check,
        options.renamed_from,
        options.retired,
        options.generated is not None,
    )


def test_annotated_field_options_equal_the_assignment_ones():
    class AssignedRoster(Model):
        id: int | None = Field(default=None, primary_key=True, autoincrement=True)
        handle: str = Field(unique=True, min_length=2)
        rank: int = Field(index=True)
        nickname: str | None = Field(default=None, nullable=True)
        mood: _Mood = Field(db_type="text", db_check=True)
        score: int = Field(renamed_from="points")
        legacy: str | None = Field(default=None, retired=True)
        doubled: int = Field(generated=lambda assigned_roster: assigned_roster.rank * 2)

    class AnnotatedRoster(Model):
        id: Annotated[int | None, Field(primary_key=True, autoincrement=True)] = None
        handle: Annotated[str, Field(unique=True, min_length=2)]
        rank: Annotated[int, Field(index=True)]
        nickname: Annotated[str | None, Field(nullable=True)] = None
        mood: Annotated[_Mood, Field(db_type="text", db_check=True)]
        score: Annotated[int, Field(renamed_from="points")]
        legacy: Annotated[str | None, Field(retired=True)] = None
        doubled: Annotated[
            int,
            Field(generated=lambda annotated_roster: annotated_roster.rank * 2),
        ] = None

    assigned = AssignedRoster.__ferro_fields__
    annotated = AnnotatedRoster.__ferro_fields__
    assert (
        set(assigned)
        == set(annotated)
        == {
            "id",
            "handle",
            "rank",
            "nickname",
            "mood",
            "score",
            "doubled",
        }
    )
    for name in assigned:
        assert _option_tuple(assigned[name]) == _option_tuple(annotated[name]), name
    assert set(AssignedRoster.__ferro_retired_fields__) == {"legacy"}
    assert set(AnnotatedRoster.__ferro_retired_fields__) == {"legacy"}
    assert _schema_option_names(AnnotatedRoster) == set()


def test_field_returns_one_ferro_field_in_its_metadata():
    info = Field(index=True, max_length=3)

    assert isinstance(info, FieldInfo)
    options = [item for item in info.metadata if isinstance(item, FerroField)]
    assert len(options) == 1
    assert options[0].index is True
    assert info.json_schema_extra is None


@pytest.mark.asyncio
async def test_relation_keywords_leave_the_json_schema_and_the_relation_loads(db_url):
    class SchemaTag(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str
        posts: Relation[list["SchemaArticle"]] = BackRef()

    class SchemaArticle(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        tags: Relation[list[SchemaTag]] = ManyToMany(
            related_name="posts", through="article_tags"
        )

    assert _schema_option_names(SchemaTag) == set()
    assert _schema_option_names(SchemaArticle) == set()

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await create_tables()
        tag = await SchemaTag.create(label="history")
        article = await SchemaArticle.create(title="Babbage's engines")
        await article.tags.add(tag)
        assert [loaded.label for loaded in await article.tags.all()] == ["history"]


def test_callable_json_schema_extra_combines_with_field_options():
    class Example(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str = Field(
            index=True, json_schema_extra=lambda schema: schema.update(example="x")
        )

    assert Example.__ferro_fields__["code"].index is True
    assert Example.model_json_schema()["properties"]["code"]["example"] == "x"
    assert Example.__ferro_columns__["code"].index is True


def test_schema_extra_without_an_option_key_still_passes_through():
    class Swatch(Model):
        id: int | None = Field(default=None, primary_key=True)
        color: str | None = pydantic.Field(
            default=None, json_schema_extra={"enum_type_name": "CategoryColor"}
        )

    assert build_column_specs(Swatch)["color"].enum_type_name == "CategoryColor"


def test_primary_key_in_json_schema_extra_is_refused():
    with pytest.raises(
        TypeError,
        match=r"Pass primary_key=True to Field\(\) instead of json_schema_extra",
    ):

        class RawKeyed(Model):
            id: int = pydantic.Field(json_schema_extra={"primary_key": True})


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (
            lambda: pydantic.Field(json_schema_extra={"unique": True}),
            r"Pass unique=True to Field\(\)",
        ),
        (
            lambda: pydantic.Field(json_schema_extra={"index": True}),
            r"Pass index=True to Field\(\)",
        ),
        (
            lambda: pydantic.Field(json_schema_extra={"autoincrement": True}),
            r"Pass autoincrement=True to Field\(\)",
        ),
        (
            lambda: pydantic.Field(json_schema_extra={"db_type": "text"}),
            r"Pass db_type='text' to Field\(\)",
        ),
        (
            lambda: pydantic.Field(json_schema_extra={"db_check": True}),
            r"Pass db_check=True to Field\(\)",
        ),
        (
            lambda: Field(json_schema_extra={"primary_key": True}),
            r"Pass primary_key=True to Field\(\)",
        ),
    ],
    ids=["unique", "index", "autoincrement", "db_type", "db_check", "ferro-field"],
)
def test_every_option_key_in_json_schema_extra_is_refused(declare, message):
    with pytest.raises(TypeError, match=message):

        class RawOption(Model):
            id: int | None = Field(default=None, primary_key=True)
            value: int = declare()


def test_ferro_nullable_in_json_schema_extra_names_nullable():
    with pytest.raises(TypeError, match=r"Pass nullable=True to Field\(\)"):

        class RawNullable(Model):
            id: int | None = Field(default=None, primary_key=True)
            value: int = pydantic.Field(json_schema_extra={"ferro_nullable": True})


def test_field_refuses_a_misspelled_option_naming_the_closest():
    with pytest.raises(TypeError, match=r"Did you mean 'index'"):
        Field(indx=True)


def test_field_refuses_an_unknown_keyword_listing_the_options():
    with pytest.raises(TypeError, match=r"'zzz'.*primary_key"):
        Field(zzz=1)


def test_back_ref_refuses_a_misspelled_option():
    with pytest.raises(TypeError, match=r"Did you mean 'index'"):
        BackRef(indx=True)


def test_field_still_accepts_the_relation_keywords():
    Field(back_ref=True)
    Field(many_to_many=True, related_name="x")
    Field(through="t")


def test_ferro_field_and_field_options_together_are_refused():
    with pytest.raises(TypeError, match="cannot declare Ferro field metadata twice"):

        class TwiceDeclared(Model):
            id: int | None = Field(default=None, primary_key=True)
            code: Annotated[int, FerroField(index=True)] = Field(unique=True)


# The class stores' names before they took dunders, spelled in two parts so a
# search for the old names finds no reader left behind.
_OLD_FIELD_STORE = "ferro_" + "fields"
_OLD_RELATION_STORE = "ferro_" + "relations"


def test_field_options_live_in_the_renamed_class_store():
    class Note(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str = Field(db_type="text")

    assert Note.__ferro_fields__["name"].db_type == "text"
    assert not hasattr(Note, _OLD_FIELD_STORE)


def test_relations_live_in_the_renamed_class_store():
    from ferro import ForeignKey

    class StoreAuthor(Model):
        id: int | None = Field(default=None, primary_key=True)
        posts: Relation[list["StorePost"]] = BackRef()

    class StorePost(Model):
        id: int | None = Field(default=None, primary_key=True)
        author: Annotated[StoreAuthor, ForeignKey(related_name="posts")]

    assert "author" in StorePost.__ferro_relations__
    assert not hasattr(StorePost, _OLD_RELATION_STORE)
