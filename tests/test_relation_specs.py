"""RelationSpec: forward-FK traversal facts at resolved registration (#268).

Prefactor for query-time joins (PRD #267 stage 1) — a per-model lookup of
declared forward-FK relations (field name -> target model, shadow FK column,
nullability), authoritative once ``resolve_relationships()`` has run. Zero
behavior change: this only adds ``cls.__ferro_relation_specs__`` alongside the
existing ``__ferro_columns__``.
"""

from typing import Annotated

from ferro import BackRef, ForeignKey, ManyToMany, Model, Relation
from ferro.base import FerroField
from ferro.columns import RelationSpec


class TestRelationSpecBasics:
    def test_single_fk_produces_relation_spec(self, clean_registry):
        class Account(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            entries: Relation[list["Entry"]] = BackRef()

        class Entry(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            account: Annotated[Account, ForeignKey(related_name="entries")]

        specs = Entry.__ferro_relation_specs__
        assert set(specs) == {"account"}
        spec = specs["account"]
        assert spec.field_name == "account"
        assert spec.target is Account
        assert spec.shadow_column == "account_id"
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
            projects: Relation[list["Project"]] = BackRef()

        class Project(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            org: Annotated[Org | None, ForeignKey(related_name="projects")] = None

        assert Project.__ferro_relation_specs__["org"].nullable is True

    def test_non_nullable_fk(self, clean_registry):
        class Org(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            projects: Relation[list["Project"]] = BackRef()

        class Project(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            org: Annotated[Org, ForeignKey(related_name="projects")]

        assert Project.__ferro_relation_specs__["org"].nullable is False

    def test_nullable_matches_compiled_shadow_column_spec(self, clean_registry):
        """Single-source: RelationSpec.nullable must equal the shadow ColumnSpec's."""

        class Org(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            projects: Relation[list["Project"]] = BackRef()

        class Project(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            org: Annotated[Org | None, ForeignKey(related_name="projects")] = None

        rel_spec = Project.__ferro_relation_specs__["org"]
        col_spec = Project.__ferro_columns__[rel_spec.shadow_column]
        assert rel_spec.nullable == col_spec.nullable


class TestMultipleAndSelfReferentialFKs:
    def test_two_fks_to_same_target_are_distinguishable(self, clean_registry):
        class Account(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        class Transfer(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            from_account: Annotated[Account, ForeignKey(related_name="transfers_out")]
            to_account: Annotated[Account, ForeignKey(related_name="transfers_in")]

        specs = Transfer.__ferro_relation_specs__
        assert set(specs) == {"from_account", "to_account"}
        assert specs["from_account"].shadow_column == "from_account_id"
        assert specs["to_account"].shadow_column == "to_account_id"
        assert specs["from_account"].target is Account
        assert specs["to_account"].target is Account
        assert specs["from_account"] != specs["to_account"]

    def test_self_referential_fk(self, clean_registry):
        from ferro.relations import resolve_relationships

        class Employee(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            manager: Annotated["Employee", ForeignKey(related_name="reports")]
            reports: Relation[list["Employee"]] = BackRef()

        # A self-FK is necessarily a forward (string) ref at class-body time —
        # the class doesn't exist yet — so the spec only completes once
        # resolve_relationships() binds ``rel.to`` to the real class.
        resolve_relationships()

        specs = Employee.__ferro_relation_specs__
        assert set(specs) == {"manager"}
        spec = specs["manager"]
        assert spec.target is Employee
        assert spec.shadow_column == "manager_id"
        assert spec.nullable is False


class TestExclusions:
    def test_many_to_many_field_absent_from_lookup(self, clean_registry):
        class Tag(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            posts: Relation[list["Post"]] = BackRef()

        class Post(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            tags: Relation[list[Tag]] = ManyToMany(related_name="posts")

        assert Post.__ferro_relation_specs__ == {}

    def test_back_ref_field_absent_from_lookup(self, clean_registry):
        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            books: Relation[list["Book"]] = BackRef()

        class Book(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[Author, ForeignKey(related_name="books")]

        assert "books" not in Author.__ferro_relation_specs__


class TestProvisionalVsResolved:
    def test_forward_ref_fk_is_complete_once_resolved(self, clean_registry):
        """Before resolve_relationships(), the target may not yet be a real class;
        after resolution the lookup must be complete and correct (design pin)."""
        from ferro.relations import resolve_relationships

        class FwdChild(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            parent: Annotated["FwdParent", ForeignKey(related_name="children")]

        class FwdParent(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            children: Relation[list["FwdChild"]] = BackRef()

        # Attribute exists pre-resolution regardless of contents (invariant).
        assert hasattr(FwdChild, "__ferro_relation_specs__")

        resolve_relationships()

        specs = FwdChild.__ferro_relation_specs__
        assert set(specs) == {"parent"}
        assert specs["parent"].target is FwdParent
        assert specs["parent"].shadow_column == "parent_id"
