"""
Unit tests for ModelMetaclass internal methods.

Tests the extracted helper methods in isolation to ensure correct behavior
before and after refactoring.
"""

from typing import Annotated, ForwardRef, Union
from unittest.mock import Mock

import pytest
from pydantic.fields import FieldInfo

from ferro.base import FerroField, ForeignKey, ManyToManyField
from ferro.fields import FERRO_FIELD_EXTRA_KEY
from ferro.metaclass import ModelMetaclass
from ferro.query import BackRef


class TestFieldHasBackRef:
    """Test _field_has_back_ref static method."""

    def test_non_field_info_returns_false(self):
        """Non-FieldInfo objects should return False."""
        assert not ModelMetaclass._field_has_back_ref("not a field")
        assert not ModelMetaclass._field_has_back_ref(123)
        assert not ModelMetaclass._field_has_back_ref(None)

    def test_field_info_without_extra_returns_false(self):
        """FieldInfo without json_schema_extra should return False."""
        field = FieldInfo(annotation=int, default=None)
        assert not ModelMetaclass._field_has_back_ref(field)

    def test_field_info_with_non_dict_extra_returns_false(self):
        """FieldInfo with non-dict json_schema_extra should return False."""
        field = FieldInfo(annotation=int, default=None)
        field.json_schema_extra = "not a dict"
        assert not ModelMetaclass._field_has_back_ref(field)

    def test_field_info_with_back_ref_true_returns_true(self):
        """FieldInfo with back_ref=True in Ferro extra should return True."""
        field = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={FERRO_FIELD_EXTRA_KEY: {"back_ref": True}},
        )
        assert ModelMetaclass._field_has_back_ref(field)

    def test_field_info_with_back_ref_false_returns_false(self):
        """FieldInfo with back_ref=False should return False."""
        field = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={FERRO_FIELD_EXTRA_KEY: {"back_ref": False}},
        )
        assert not ModelMetaclass._field_has_back_ref(field)


class TestIsBackRefField:
    """Test _is_back_ref_field static method."""

    def test_backref_type_annotation(self):
        """BackRef[...] in annotation should be detected."""
        hint = BackRef[int]
        namespace = {}
        is_type, is_field = ModelMetaclass._is_back_ref_field("field", hint, namespace)
        assert is_type is True
        assert is_field is False

    def test_annotated_backref(self):
        """Annotated[BackRef[...], ...] should be detected."""
        hint = Annotated[BackRef[int], "metadata"]
        namespace = {}
        is_type, is_field = ModelMetaclass._is_back_ref_field("field", hint, namespace)
        assert is_type is True
        assert is_field is False

    def test_string_with_backref(self):
        """String annotation containing 'BackRef' should be detected."""
        hint = "BackRef[User]"
        namespace = {}
        is_type, is_field = ModelMetaclass._is_back_ref_field("field", hint, namespace)
        assert is_type is True
        assert is_field is False

    def test_forward_ref_with_backref(self):
        """ForwardRef containing 'BackRef' should be detected."""
        hint = ForwardRef("BackRef[User]")
        namespace = {}
        is_type, is_field = ModelMetaclass._is_back_ref_field("field", hint, namespace)
        assert is_type is True
        assert is_field is False

    def test_field_with_back_ref_true(self):
        """Field(back_ref=True) in namespace should be detected."""
        hint = list[int]
        field = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={FERRO_FIELD_EXTRA_KEY: {"back_ref": True}},
        )
        namespace = {"field": field}
        is_type, is_field = ModelMetaclass._is_back_ref_field("field", hint, namespace)
        assert is_type is False
        assert is_field is True

    def test_annotated_with_field_back_ref(self):
        """Annotated[..., Field(back_ref=True)] should be detected."""
        field_info = FieldInfo(
            annotation=int,
            default=None,
            json_schema_extra={FERRO_FIELD_EXTRA_KEY: {"back_ref": True}},
        )
        hint = Annotated[list[int], field_info]
        namespace = {}
        is_type, is_field = ModelMetaclass._is_back_ref_field("field", hint, namespace)
        assert is_type is False
        assert is_field is True

    def test_neither_type_nor_field(self):
        """Regular field should return (False, False)."""
        hint = int
        namespace = {}
        is_type, is_field = ModelMetaclass._is_back_ref_field("field", hint, namespace)
        assert is_type is False
        assert is_field is False


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
        local_relations = {"posts": ManyToManyField(related_name="users")}

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
        assert annotations["name"] == str

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
