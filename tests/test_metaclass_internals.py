"""
Unit tests for ModelMetaclass internal methods.

Tests the extracted helper methods in isolation to ensure correct behavior
before and after refactoring.
"""

from typing import Annotated, ForwardRef, Union
from unittest.mock import Mock

import pytest
from pydantic.fields import FieldInfo

from ferro import BackRef, Model, Relation
from ferro.base import FerroField, ForeignKey, ManyToManyRelation
from ferro.fields import FERRO_FIELD_EXTRA_KEY
from ferro.metaclass import ModelMetaclass


class TestFieldFerroPayload:
    """Test _field_ferro_payload static method."""

    def test_non_field_info_returns_false(self):
        """Non-FieldInfo objects should return an empty payload."""
        assert ModelMetaclass._field_ferro_payload("not a field") == {}
        assert ModelMetaclass._field_ferro_payload(123) == {}
        assert ModelMetaclass._field_ferro_payload(None) == {}

    def test_field_info_without_extra_returns_false(self):
        """FieldInfo without json_schema_extra should return an empty payload."""
        field = FieldInfo(annotation=int, default=None)
        assert ModelMetaclass._field_ferro_payload(field) == {}

    def test_field_info_with_non_dict_extra_returns_false(self):
        """FieldInfo with non-dict json_schema_extra should return an empty payload."""
        field = FieldInfo(annotation=int, default=None)
        field.json_schema_extra = "not a dict"
        assert ModelMetaclass._field_ferro_payload(field) == {}

    def test_field_info_with_back_ref_true_returns_payload(self):
        """FieldInfo with Ferro extra should return that payload."""
        field = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={FERRO_FIELD_EXTRA_KEY: {"back_ref": True}},
        )
        assert ModelMetaclass._field_ferro_payload(field) == {"back_ref": True}

    def test_field_info_with_many_to_many_returns_payload(self):
        """FieldInfo with many_to_many metadata should return that payload."""
        field = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={
                FERRO_FIELD_EXTRA_KEY: {
                    "many_to_many": True,
                    "related_name": "users",
                }
            },
        )
        assert ModelMetaclass._field_ferro_payload(field)["related_name"] == "users"


class TestRelationshipFieldPayload:
    """Test relationship Field metadata helpers."""

    def test_backref_type_annotation_raises_migration_error(self):
        """BackRef[...] in annotation should now raise migration guidance."""
        with pytest.raises(
            TypeError, match="Relation\\[list\\[T\\]\\] = BackRef\\(\\)"
        ):
            BackRef[int]

    def test_string_with_backref_is_legacy(self):
        """String annotation containing 'BackRef' should be detected as legacy."""
        hint = "BackRef[User]"
        assert ModelMetaclass._annotation_looks_like_back_ref(hint) is True

    def test_forward_ref_with_backref_is_legacy(self):
        """ForwardRef containing 'BackRef' should be detected as legacy."""
        hint = ForwardRef("BackRef[User]")
        assert ModelMetaclass._annotation_looks_like_back_ref(hint) is True

    def test_field_with_back_ref_true(self):
        """Field(back_ref=True) in namespace should be returned as payload."""
        hint = Relation[list[int]]
        field = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={FERRO_FIELD_EXTRA_KEY: {"back_ref": True}},
        )
        namespace = {"field": field}
        payload = ModelMetaclass._relationship_field_payload("field", hint, namespace)
        assert payload == {"back_ref": True}

    def test_annotated_with_field_back_ref(self):
        """Annotated[..., Field(back_ref=True)] should be returned as payload."""
        field_info = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={FERRO_FIELD_EXTRA_KEY: {"back_ref": True}},
        )
        hint = Annotated[Relation[list[int]], field_info]
        namespace = {}
        payload = ModelMetaclass._relationship_field_payload("field", hint, namespace)
        assert payload == {"back_ref": True}

    def test_neither_type_nor_field(self):
        """Regular field should return no relationship payload."""
        hint = int
        namespace = {}
        assert (
            ModelMetaclass._relationship_field_payload("field", hint, namespace) == {}
        )

    def test_relation_target_from_annotation(self):
        """Relation[list[T]] should expose T for relationship metadata."""
        assert (
            ModelMetaclass._relation_target_from_annotation(
                "field", Relation[list[int]]
            )
            is int
        )

    def test_relation_target_from_string_annotation(self):
        """String Relation[list[T]] annotations should expose T."""
        assert (
            ModelMetaclass._relation_target_from_annotation(
                "field", 'Relation[list["Course"]]'
            )
            == "Course"
        )


class TestResolveDeferredAnnotations:
    """Test _resolve_deferred_annotations static method."""

    def test_existing_annotations_returned(self):
        """Namespace with __annotations__ should return it."""
        namespace = {"__annotations__": {"field": int}}
        result = ModelMetaclass._resolve_deferred_annotations(namespace)
        assert result == {"field": int}

    def test_no_annotations_returns_empty(self):
        """Namespace without __annotations__ or __annotate_func__ should return empty."""
        namespace = {}
        result = ModelMetaclass._resolve_deferred_annotations(namespace)
        assert result == {}

    def test_annotate_func_format_1(self):
        """__annotate_func__(1) should be called for evaluated annotations."""

        def annotate_func(format_type):
            if format_type == 1:
                return {"field": int}
            raise ValueError("Wrong format")

        namespace = {"__annotate_func__": annotate_func}
        result = ModelMetaclass._resolve_deferred_annotations(namespace)
        assert result == {"field": int}

    def test_annotate_func_format_2_fallback(self):
        """Should try format 2 if format 1 fails."""

        def annotate_func(format_type):
            if format_type == 1:
                raise ValueError("Format 1 not supported")
            if format_type == 2:
                return {"field": ForwardRef("int")}
            raise ValueError("Wrong format")

        namespace = {"__annotate_func__": annotate_func}
        result = ModelMetaclass._resolve_deferred_annotations(namespace)
        assert "field" in result

    def test_annotate_func_both_fail(self):
        """Should return empty dict if both formats fail."""

        def annotate_func(format_type):
            raise ValueError("Not supported")

        namespace = {"__annotate_func__": annotate_func}
        result = ModelMetaclass._resolve_deferred_annotations(namespace)
        assert result == {}


class TestInjectShadowFields:
    """Test _inject_shadow_fields static method."""

    def test_no_foreign_keys_no_changes(self):
        """No ForeignKeys should leave annotations/namespace unchanged."""
        annotations = {"name": str}
        namespace = {}
        local_relations = {"posts": ManyToManyRelation(related_name="users")}

        ModelMetaclass._inject_shadow_fields(annotations, namespace, local_relations)

        assert annotations == {"name": str}
        assert namespace == {}

    def test_foreign_key_injects_id_field(self):
        """ForeignKey should inject {field_name}_id annotation and default."""
        annotations = {"name": str}
        namespace = {}
        fk = ForeignKey(related_name="posts")
        fk.to = "User"
        local_relations = {"owner": fk}

        ModelMetaclass._inject_shadow_fields(annotations, namespace, local_relations)

        assert "owner_id" in annotations
        assert annotations["owner_id"] == Union[int, str, None]
        assert "owner_id" in namespace
        assert isinstance(namespace["owner_id"], FieldInfo)

    def test_foreign_key_injects_id_field_concrete_pk(self):
        """Concrete FK target: shadow type follows related model PK annotation."""

        class ShadowOwner(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str

        annotations = {"name": str}
        namespace = {}
        fk = ForeignKey(related_name="items")
        fk.to = ShadowOwner
        local_relations = {"owner": fk}

        ModelMetaclass._inject_shadow_fields(annotations, namespace, local_relations)

        assert annotations["owner_id"] == (int | None)
        assert "owner_id" in namespace

    def test_multiple_foreign_keys(self):
        """Multiple ForeignKeys should inject multiple shadow fields."""
        annotations = {"name": str}
        namespace = {}
        fk1 = ForeignKey(related_name="posts")
        fk1.to = "User"
        fk2 = ForeignKey(related_name="posts")
        fk2.to = "Category"
        local_relations = {"owner": fk1, "category": fk2}

        ModelMetaclass._inject_shadow_fields(annotations, namespace, local_relations)

        assert "owner_id" in annotations
        assert "category_id" in annotations


class TestPrepareNamespaceForPydantic:
    """Test _prepare_namespace_for_pydantic static method."""

    def test_converts_fields_to_classvar(self):
        """Fields in fields_to_remove should become ClassVar."""
        annotations = {"name": str, "owner": "User", "posts": list}
        namespace = {}
        fields_to_remove = ["owner", "posts"]

        ModelMetaclass._prepare_namespace_for_pydantic(
            namespace, annotations, fields_to_remove
        )

        # Check ClassVar was added (checking the origin is ClassVar)
        from typing import ClassVar, get_origin

        assert get_origin(annotations["owner"]) is ClassVar
        assert get_origin(annotations["posts"]) is ClassVar
        assert annotations["name"] is str

    def test_removes_annotate_func(self):
        """__annotate_func__ should be removed if present."""
        annotations = {"name": str}
        namespace = {"__annotate_func__": lambda x: {}}
        fields_to_remove = []

        ModelMetaclass._prepare_namespace_for_pydantic(
            namespace, annotations, fields_to_remove
        )

        assert "__annotate_func__" not in namespace

    def test_no_annotate_func_no_error(self):
        """Should not error if __annotate_func__ not present."""
        annotations = {"name": str}
        namespace = {}
        fields_to_remove = []

        ModelMetaclass._prepare_namespace_for_pydantic(
            namespace, annotations, fields_to_remove
        )

        assert "__annotate_func__" not in namespace


class TestParseFerroFieldMetadata:
    """Test _parse_ferro_field_metadata static method."""

    def test_no_ferro_fields_returns_empty(self):
        """Model without FerroField metadata should return empty dict."""
        mock_cls = Mock()
        mock_cls.model_fields = {"name": FieldInfo(annotation=str, default=None)}

        result = ModelMetaclass._parse_ferro_field_metadata(mock_cls)

        assert result == {}

    def test_annotated_ferro_field(self):
        """FerroField in Annotated metadata should be detected."""
        ferro_meta = FerroField(primary_key=True)
        mock_cls = Mock()
        field_info = FieldInfo(annotation=int, default=None)
        # Simulate Pydantic's metadata field
        field_info.metadata = [ferro_meta]
        mock_cls.model_fields = {"id": field_info}

        result = ModelMetaclass._parse_ferro_field_metadata(mock_cls)

        assert "id" in result
        assert result["id"] is ferro_meta

    def test_wrapped_ferro_field(self):
        """FerroField in json_schema_extra should be detected."""
        mock_cls = Mock()
        field_info = FieldInfo(annotation=int, default=None)
        field_info.json_schema_extra = {
            FERRO_FIELD_EXTRA_KEY: {"primary_key": True, "autoincrement": True}
        }
        mock_cls.model_fields = {"id": field_info}

        result = ModelMetaclass._parse_ferro_field_metadata(mock_cls)

        assert "id" in result
        assert isinstance(result["id"], FerroField)
        assert result["id"].primary_key is True

    def test_dual_declaration_raises_error(self):
        """FerroField declared twice should raise TypeError."""
        ferro_meta = FerroField(primary_key=True)
        mock_cls = Mock()
        field_info = FieldInfo(annotation=int, default=None)
        field_info.metadata = [ferro_meta]
        field_info.json_schema_extra = {FERRO_FIELD_EXTRA_KEY: {"primary_key": True}}
        mock_cls.model_fields = {"id": field_info}

        with pytest.raises(
            TypeError, match="cannot declare Ferro field metadata twice"
        ):
            ModelMetaclass._parse_ferro_field_metadata(mock_cls)
