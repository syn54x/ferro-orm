"""
Unit tests for ModelMetaclass internal methods.

Tests the extracted helper methods in isolation to ensure correct behavior
before and after refactoring.
"""

import sys
from typing import Annotated, ForwardRef
from unittest.mock import Mock

import pytest
from pydantic.fields import FieldInfo

from ferro import Field, ManyToMany, Model, Relation, Reverse
from ferro.base import FerroField
from ferro.metaclass import ModelMetaclass


class TestFieldFerroPayload:
    """Test _field_ferro_payload static method."""

    def test_non_field_info_returns_false(self):
        """Non-FieldInfo objects should return an empty payload."""
        assert ModelMetaclass._field_ferro_payload("not a field") == {}
        assert ModelMetaclass._field_ferro_payload(123) == {}
        assert ModelMetaclass._field_ferro_payload(None) == {}

    def test_field_info_without_relation_keywords_returns_empty(self):
        """A FieldInfo with column options only carries no relation payload."""
        assert ModelMetaclass._field_ferro_payload(Field(index=True)) == {}

    def test_reverse_leaves_its_attribute_in_the_metadata(self):
        """Reverse("team_id") leaves the attribute it names in the metadata."""
        field = Reverse("team_id")
        assert ModelMetaclass._field_ferro_payload(field) == {"reverse": "team_id"}

    def test_many_to_many_leaves_its_options_in_the_metadata(self):
        """ManyToMany(through=...) leaves its join table in the metadata."""
        field = ManyToMany(through="enrollments")
        assert ModelMetaclass._field_ferro_payload(field)["through"] == "enrollments"

    def test_retired_many_to_many_reports_retired(self):
        """A retired ManyToMany carries retired=True so the scan can refuse it."""
        field = ManyToMany(through="enrollments", retired=True)
        assert ModelMetaclass._field_ferro_payload(field)["retired"] is True


class TestRelationshipFieldPayload:
    """Test relationship Field metadata helpers."""

    def test_assigned_reverse(self):
        """A Reverse assigned in the namespace should be returned as payload."""
        hint = Relation[list[int]]
        namespace = {"field": Reverse("team_id")}
        payload = ModelMetaclass._relationship_field_payload("field", hint, namespace)
        assert payload == {"reverse": "team_id"}

    def test_reverse_in_annotated_metadata(self):
        """Annotated[..., Reverse(...)] should be returned as payload."""
        hint = Annotated[Relation[list[int]], Reverse("team_id")]
        namespace = {}
        payload = ModelMetaclass._relationship_field_payload("field", hint, namespace)
        assert payload == {"reverse": "team_id"}

    def test_neither_type_nor_field(self):
        """Regular field should return no relationship payload."""
        hint = int
        namespace = {}
        assert (
            ModelMetaclass._relationship_field_payload("field", hint, namespace) == {}
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

    def test_annotate_func_both_fail_reraises_real_error(self):
        """Both formats failing must surface the real error, not swallow it (#155)."""

        def annotate_func(format_type):
            raise ValueError("Not supported")

        namespace = {"__annotate_func__": annotate_func, "__qualname__": "Demo"}
        with pytest.raises(ValueError, match="Not supported") as exc_info:
            ModelMetaclass._resolve_deferred_annotations(namespace)
        notes = getattr(exc_info.value, "__notes__", [])
        assert any("Demo" in note for note in notes)
        # key on the stable "ferro:" note prefix, not churn-prone prose
        assert any("ferro:" in note for note in notes)


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
        # On 3.14 the broken annotation is deferred -> the fixed branch adds the note.
        assert any(
            "_Broken" in note for note in getattr(exc_info.value, "__notes__", [])
        )


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
        mock_cls = Mock(__name__="Mocked")
        mock_cls.model_fields = {"name": FieldInfo(annotation=str, default=None)}

        result = ModelMetaclass._parse_ferro_field_metadata(mock_cls)

        assert result == {}

    def test_annotated_ferro_field(self):
        """FerroField in Annotated metadata should be detected."""
        ferro_meta = FerroField(primary_key=True)
        mock_cls = Mock(__name__="Mocked")
        field_info = FieldInfo(annotation=int, default=None)
        # Simulate Pydantic's metadata field
        field_info.metadata = [ferro_meta]
        mock_cls.model_fields = {"id": field_info}

        result = ModelMetaclass._parse_ferro_field_metadata(mock_cls)

        assert "id" in result
        assert result["id"] is ferro_meta

    def test_wrapped_ferro_field(self):
        """The FerroField a Field(...) leaves in its metadata should be detected."""
        mock_cls = Mock(__name__="Mocked")
        field_info = Field(default=None, primary_key=True, autoincrement=True)
        mock_cls.model_fields = {"id": field_info}

        result = ModelMetaclass._parse_ferro_field_metadata(mock_cls)

        assert "id" in result
        assert isinstance(result["id"], FerroField)
        assert result["id"].primary_key is True
        assert result["id"].autoincrement is True

    def test_dual_declaration_raises_error(self):
        """FerroField declared twice should raise TypeError."""
        mock_cls = Mock(__name__="Mocked")
        field_info = Field(default=None, primary_key=True)
        field_info.metadata.append(FerroField(primary_key=True))
        mock_cls.model_fields = {"id": field_info}

        with pytest.raises(
            TypeError, match="cannot declare Ferro field metadata twice"
        ):
            ModelMetaclass._parse_ferro_field_metadata(mock_cls)
