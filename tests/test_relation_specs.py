"""RelationSpec: the facts each forward relation of a model reads.

``cls.__ferro_relation_specs__`` maps each declared forward relation to its
target model, foreign-key column and nullability. The lookup is complete at
class definition: an entry and its key column exist before the target is
defined, and reading ``target`` resolves the modelset when the declaration
still holds the target's name.
"""

from typing import Annotated

import pytest

from ferro import Check, Field, Forward, ManyToMany, Model, Relation, Reverse
from ferro.base import FerroField
from ferro.columns import RelationSpec
from ferro.registry import REGISTRY


class TestRelationSpecBasics:
    def test_single_fk_produces_relation_spec(self, clean_registry):
        class Account(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            entries: Relation[list["Entry"]] = Reverse("account_id")

        class Entry(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            account_id: Annotated[int, FerroField(foreign_key=Account)]
            account: Relation[Account] = Forward("account_id")

        specs = Entry.__ferro_relation_specs__
        assert set(specs) == {"account"}
        spec = specs["account"]
        assert spec.field_name == "account"
        assert spec.target is Account
        assert spec.fk_column == "account_id"
        assert isinstance(spec, RelationSpec)

    def test_model_with_no_relations_has_empty_dict(self, clean_registry):
        class NoRelations(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str

        assert NoRelations.__ferro_relation_specs__ == {}

    def test_every_model_has_the_attribute(self, clean_registry):
        # No hasattr guards required by consumers (design pin).
        class Bare(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        assert hasattr(Bare, "__ferro_relation_specs__")
        assert Bare.__ferro_relation_specs__ == {}


class TestNullability:
    def test_nullable_fk(self, clean_registry):
        class Org(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            projects: Relation[list["Project"]] = Reverse("org_id")

        class Project(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            org_id: Annotated[int | None, FerroField(foreign_key=Org)] = None
            org: Relation[Org | None] = Forward("org_id")

        assert Project.__ferro_relation_specs__["org"].nullable is True

    def test_non_nullable_fk(self, clean_registry):
        class Org(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            projects: Relation[list["Project"]] = Reverse("org_id")

        class Project(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            org_id: Annotated[int, FerroField(foreign_key=Org)]
            org: Relation[Org] = Forward("org_id")

        assert Project.__ferro_relation_specs__["org"].nullable is False

    def test_nullable_matches_compiled_fk_column_spec(self, clean_registry):
        """Single-source: RelationSpec.nullable must equal the foreign-key ColumnSpec's."""

        class Org(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            projects: Relation[list["Project"]] = Reverse("org_id")

        class Project(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            org_id: Annotated[int | None, FerroField(foreign_key=Org)] = None
            org: Relation[Org | None] = Forward("org_id")

        rel_spec = Project.__ferro_relation_specs__["org"]
        col_spec = Project.__ferro_columns__[rel_spec.fk_column]
        assert rel_spec.nullable == col_spec.nullable


class TestMultipleAndSelfReferentialFKs:
    def test_two_fks_to_same_target_are_distinguishable(self, clean_registry):
        class Account(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        class Transfer(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            from_account_id: Annotated[int, FerroField(foreign_key=Account)]
            from_account: Relation[Account] = Forward("from_account_id")
            to_account_id: Annotated[int, FerroField(foreign_key=Account)]
            to_account: Relation[Account] = Forward("to_account_id")

        specs = Transfer.__ferro_relation_specs__
        assert set(specs) == {"from_account", "to_account"}
        assert specs["from_account"].fk_column == "from_account_id"
        assert specs["to_account"].fk_column == "to_account_id"
        assert specs["from_account"].target is Account
        assert specs["to_account"].target is Account
        assert specs["from_account"] != specs["to_account"]

    def test_self_referential_fk(self, clean_registry):
        from ferro.relations import resolve_relationships

        class Employee(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            manager_id: Annotated[int, FerroField(foreign_key="Employee")]
            manager: Relation["Employee"] = Forward("manager_id")
            reports: Relation[list["Employee"]] = Reverse("manager_id")

        # A self-FK is necessarily a forward (string) ref at class-body time —
        # the class doesn't exist yet — so the spec only completes once
        # resolve_relationships() binds ``rel.to`` to the real class.
        resolve_relationships()

        specs = Employee.__ferro_relation_specs__
        assert set(specs) == {"manager"}
        spec = specs["manager"]
        assert spec.target is Employee
        assert spec.fk_column == "manager_id"
        assert spec.nullable is False


class TestExclusions:
    def test_many_to_many_field_absent_from_lookup(self, clean_registry):
        class Tag(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            posts: Relation[list["Post"]] = Reverse("tags")

        class Post(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            tags: Relation[list[Tag]] = ManyToMany(through="post_tags")

        assert Post.__ferro_relation_specs__ == {}

    def test_reverse_field_absent_from_lookup(self, clean_registry):
        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            books: Relation[list["Book"]] = Reverse("author_id")

        class Book(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author_id: Annotated[int, FerroField(foreign_key=Author)]
            author: Relation[Author] = Forward("author_id")

        assert "books" not in Author.__ferro_relation_specs__


class TestProvisionalVsResolved:
    def test_forward_ref_fk_is_complete_at_class_definition(self, clean_registry):
        """The entry and its key column exist before the target is defined;
        reading `target` afterwards resolves it with no resolve call."""

        class FwdChild(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            parent_id: Annotated[int, FerroField(foreign_key="FwdParent")]
            parent: Relation["FwdParent"] = Forward("parent_id")

        specs = FwdChild.__ferro_relation_specs__
        assert set(specs) == {"parent"}
        assert specs["parent"].fk_column == "parent_id"

        class FwdParent(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            children: Relation[list["FwdChild"]] = Reverse("parent_id")

        assert FwdChild.__ferro_relation_specs__["parent"].target is FwdParent


class TestCompleteAtClassDefinition:
    """A forward relation's spec exists the moment its class does, target or not."""

    def test_the_spec_is_there_before_its_target_is_defined(self, clean_registry):
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key="Team")
            team: Relation["Team"] = Forward("team_id")  # noqa: F821

        specs = Player.__ferro_relation_specs__

        assert set(specs) == {"team"}
        assert (specs["team"].fk_column, specs["team"].nullable) == ("team_id", False)

    def test_reading_target_resolves_without_a_resolve_call(self, clean_registry):
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key="Team")
            team: Relation["Team"] = Forward("team_id")

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)

        assert Player.__ferro_relation_specs__["team"].target is Team

    def test_a_spec_read_before_its_target_existed_returns_it(self, clean_registry):
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key="Team")
            team: Relation["Team"] = Forward("team_id")

        spec = Player.__ferro_relation_specs__["team"]

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)

        assert spec.target is Team

    def test_a_target_no_model_takes_raises_the_resolution_refusal(
        self, clean_registry
    ):
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key="Ghost")
            team: Relation["Ghost"] = Forward("team_id")  # noqa: F821

        spec = Player.__ferro_relation_specs__["team"]

        with pytest.raises(RuntimeError, match="'Ghost' not found"):
            spec.target

    def test_the_forward_descriptor_keeps_no_key_column_of_its_own(self):
        from ferro.relations.descriptors import ForwardDescriptor

        assert "fk_column" not in ForwardDescriptor.model_fields

    def test_a_check_reads_a_relation_whose_target_is_not_defined_yet(
        self, clean_registry
    ):
        class Spelling(Model):
            __ferro_checks__ = (
                Check("via_relation", lambda spelling: spelling.target == None),  # noqa: E711
            )

            id: int | None = Field(default=None, primary_key=True)
            target_id: int | None = Field(default=None, foreign_key="SpellTarget")
            target: Relation["SpellTarget | None"] = Forward("target_id")  # noqa: F821

        envelope = REGISTRY.envelope(Spelling.__ferro_identity__)
        (model,) = envelope["payload"]["models"]

        assert [check["predicate"] for check in model["table_checks"]] == [
            {"kind": "is_null", "column": "target_id"}
        ]

    def test_assigning_a_relation_before_connect_sets_its_key(self, clean_registry):
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key="Team")
            team: Relation["Team"] = Forward("team_id")

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)

        player = Player(team_id=1)
        player.team = Team(id=7)

        assert player.team_id == 7


def test_a_relation_spec_names_its_declared_key_column(clean_registry):
    from ferro import Field

    class Owner(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    class Pet(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        keeper_owner_id: int = Field(foreign_key=Owner)
        keeper: Relation[Owner] = Forward("keeper_owner_id")

    spec = Pet.__ferro_relation_specs__["keeper"]

    assert (spec.target, spec.fk_column, spec.nullable) == (
        Owner,
        "keeper_owner_id",
        False,
    )
    assert RelationSpec.__slots__ == ("field_name", "fk_column", "nullable", "relation")


def test_assigning_a_relation_with_no_key_column_raises_its_refusal(clean_registry):
    # No mapped key column, so no spec: assigning resolves the modelset,
    # whose refusal names the column to add.
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team: Relation["Team"] = Forward("team_id")  # noqa: F821

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    assert "team" not in Player.__ferro_relation_specs__
    with pytest.raises(TypeError, match="Player has no column team_id"):
        Player(id=1).team = Team(id=7)


def test_key_columns_are_published_with_the_specs(clean_registry):
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")
        team: Relation["Team"] = Forward("team_id")  # noqa: F821

    assert Player.__ferro_relation_key_columns__ == frozenset({"team_id"})


def test_reading_target_raises_a_refusal_elsewhere_in_the_modelset(clean_registry):
    # The first read resolves the whole modelset, so another model's refused
    # declaration surfaces here as its TypeError.
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")
        team: Relation["Team"] = Forward("team_id")  # noqa: F821

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Coach(Model):
        id: int | None = Field(default=None, primary_key=True)
        team: Relation["Team"] = Forward("team_id")  # noqa: F821

    with pytest.raises(TypeError, match="Coach has no column team_id"):
        Player.__ferro_relation_specs__["team"].target
