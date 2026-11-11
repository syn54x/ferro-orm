"""A class statement becomes a registered model, or is refused.

Every test defines a model class and reads what the class publishes.
"""

import sys
from typing import Annotated

import pytest

from ferro import Field, ManyToMany, Model, Relation, Reverse
from ferro.base import FerroField, ManyToManyRelation, ReverseRelation


@pytest.mark.usefixtures("clean_registry")
class TestRelationMarkers:
    """A field's relation marker, read through a class definition."""

    def test_a_field_with_column_options_only_declares_no_relation(self):
        class Badge(Model):
            id: int | None = Field(default=None, primary_key=True)
            code: str = Field(index=True)

        assert Badge.__ferro_relations__ == {}

    def test_an_assigned_reverse_declares_a_reverse_relation(self):
        class Squad(Model):
            id: int | None = Field(default=None, primary_key=True)
            members: Relation[list["Member"]] = Reverse("squad_id")  # noqa: F821

        relation = Squad.__ferro_relations__["members"]
        assert isinstance(relation, ReverseRelation)
        assert relation.attribute == "squad_id"

    def test_an_assigned_many_to_many_declares_its_join_table(self):
        class Course(Model):
            id: int | None = Field(default=None, primary_key=True)
            pupils: Relation[list["Pupil"]] = ManyToMany(  # noqa: F821
                through="enrollments"
            )

        relation = Course.__ferro_relations__["pupils"]
        assert isinstance(relation, ManyToManyRelation)
        assert relation.through == "enrollments"

    def test_a_retired_many_to_many_is_refused(self):
        with pytest.raises(TypeError, match="a ManyToMany cannot be retired"):

            class Course(Model):
                id: int | None = Field(default=None, primary_key=True)
                pupils: Relation[list["Pupil"]] = ManyToMany(  # noqa: F821
                    through="enrollments", retired=True
                )

    @pytest.mark.parametrize(
        ("marker", "written"),
        [
            (Reverse("squad_id"), 'Reverse("squad_id")'),
            (
                ManyToMany(through="squad_members"),
                'ManyToMany(through="squad_members")',
            ),
        ],
        ids=["reverse", "many-to-many"],
    )
    def test_a_marker_in_annotated_metadata_is_refused(self, marker, written):
        class Member(Model):
            id: int | None = Field(default=None, primary_key=True)
            squad_id: int = Field(foreign_key="Squad")

        with pytest.raises(TypeError) as refusal:

            class Squad(Model):
                id: int | None = Field(default=None, primary_key=True)
                members: Annotated[Relation[list[Member]], marker]

        assert str(refusal.value) == (
            "Squad.members: a relation is an assignment, not Annotated metadata. "
            f"Write: members: Relation[list[Member]] = {written}"
        )


@pytest.mark.usefixtures("clean_registry")
class TestFerroFieldMetadata:
    """A field's Ferro options, read from `__ferro_fields__`."""

    def test_a_field_with_no_options_is_not_declared(self):
        class Plain(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str

        assert "name" not in Plain.__ferro_fields__

    def test_annotated_ferro_field_is_published_as_written(self):
        meta = FerroField(primary_key=True)

        class Annotated_(Model):
            id: Annotated[int, meta]

        assert Annotated_.__ferro_fields__["id"] is meta

    def test_field_options_are_published(self):
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True, autoincrement=True)

        declared = Player.__ferro_fields__["id"]
        assert isinstance(declared, FerroField)
        assert declared.primary_key is True
        assert declared.autoincrement is True

    def test_declaring_options_twice_is_refused(self):
        with pytest.raises(
            TypeError, match="cannot declare Ferro field metadata twice"
        ):

            class Twice(Model):
                id: Annotated[int, FerroField(primary_key=True)] = Field(
                    default=None, primary_key=True
                )

    def test_a_declared_relation_is_not_a_model_field(self):
        class Squad(Model):
            id: int | None = Field(default=None, primary_key=True)
            members: Relation[list["Member"]] = Reverse("squad_id")  # noqa: F821

        assert "members" not in Squad.model_fields
        assert "members" in Squad.__ferro_relations__


@pytest.mark.usefixtures("clean_registry")
def test_unevaluable_annotation_surfaces_real_error_not_misleading():
    """#155: a model whose annotation cannot be evaluated must raise the real
    cause, never Pydantic's misleading 'non-annotated attribute' error."""
    with pytest.raises(TypeError) as exc_info:

        class _Broken(Model):
            id: Annotated[int, FerroField(primary_key=True)] = 0
            status: Annotated[str, FerroField(default="draft")]  # invalid kwarg

    msg = str(exc_info.value)
    assert "default" in msg, "the real FerroField TypeError must surface"
    assert "non-annotated attribute" not in msg, "must not be the misleading error"
    if sys.version_info >= (3, 14):
        # On 3.14 the broken annotation is deferred -> the note names the model.
        notes = getattr(exc_info.value, "__notes__", [])
        assert any("_Broken" in note for note in notes)
        # key on the stable "ferro:" note prefix, not churn-prone prose
        assert any(note.startswith("ferro:") for note in notes)
