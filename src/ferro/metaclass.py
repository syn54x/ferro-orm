import json
import types
from typing import (
    Annotated,
    Any,
    ClassVar,
    ForwardRef,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel
from pydantic import Field as PydanticField
from pydantic.fields import FieldInfo

from ._core import register_model_schema
from ._shadow_fk_types import shadow_annotation_for_foreign_key
from .base import FerroField, ForeignKey, ManyToManyRelation
from .fields import FERRO_FIELD_EXTRA_KEY
from .query import FieldProxy, Relation
from .relations.descriptors import ForwardDescriptor
from .schema_metadata import build_model_schema
from .state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS


class ModelMetaclass(type(BaseModel)):
    """
    Metaclass for Ferro models that automatically registers the model schema with the Rust core.
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        # Phase 1: Annotation Processing
        annotations = mcs._resolve_deferred_annotations(namespace)
        namespace["__annotations__"] = annotations

        local_relations, fields_to_remove = mcs._scan_relationship_annotations(
            annotations, namespace, name
        )
        mcs._inject_shadow_fields(annotations, namespace, local_relations)
        mcs._prepare_namespace_for_pydantic(namespace, annotations, fields_to_remove)

        # Phase 2: Class Creation
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # Phase 3: Post-Creation Setup
        if name == "Model":
            return cls

        mcs._register_model_and_proxies(cls, name, local_relations)
        ferro_fields = mcs._parse_ferro_field_metadata(cls)
        cls.ferro_fields = ferro_fields
        mcs._inject_relation_descriptors(cls, local_relations)
        mcs._generate_and_register_schema(cls, name, ferro_fields, local_relations)

        return cls

    @staticmethod
    def _field_ferro_payload(obj: Any) -> dict[str, Any]:
        """Return Ferro metadata payload from a wrapped FieldInfo."""
        if not isinstance(obj, FieldInfo):
            return {}
        extra = getattr(obj, "json_schema_extra", None)
        if not isinstance(extra, dict):
            return {}
        payload = extra.get(FERRO_FIELD_EXTRA_KEY, {})
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _strip_optional_union(hint: Any) -> Any:
        """Unwrap ``T | None`` / ``Optional[T]`` to ``T`` for relationship detection."""
        while True:
            origin = get_origin(hint)
            if origin is Union or origin is types.UnionType:
                args = get_args(hint)
                non_none = [a for a in args if a is not type(None)]
                if len(non_none) == 1:
                    hint = non_none[0]
                    continue
            return hint

    @staticmethod
    def _relationship_marker_from_annotation(hint: Any) -> Any:
        """Inner type used to inspect relationship annotations."""
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            if args:
                return ModelMetaclass._strip_optional_union(args[0])
            return hint
        return ModelMetaclass._strip_optional_union(hint)

    @staticmethod
    def _legacy_back_ref_error(field_name: str) -> TypeError:
        return TypeError(
            f"Field '{field_name}' uses deprecated BackRef[...] annotation syntax. "
            "Use Relation[list[T]] = BackRef() for collection back-references."
        )

    @staticmethod
    def _relationship_field_payload(
        field_name: str, hint: Any, namespace: dict
    ) -> dict[str, Any]:
        """Return relationship metadata supplied by ferro.Field helpers."""
        origin = get_origin(hint)
        default_val = namespace.get(field_name)
        payload = ModelMetaclass._field_ferro_payload(default_val)
        if payload:
            return payload

        if origin is Annotated:
            for metadata in get_args(hint)[1:]:
                payload = ModelMetaclass._field_ferro_payload(metadata)
                if payload:
                    return payload

        return {}

    @staticmethod
    def _relation_target_from_annotation(field_name: str, hint: Any) -> Any:
        """Extract T from Relation[list[T]] for collection relationships."""
        marker = ModelMetaclass._relationship_marker_from_annotation(hint)
        if isinstance(marker, str):
            return ModelMetaclass._relation_target_from_string(field_name, marker)
        if get_origin(marker) is not Relation:
            raise TypeError(
                f"Field '{field_name}' must be annotated as Relation[list[T]] "
                "when using BackRef(), ManyToMany(), or relationship Field flags."
            )

        args = get_args(marker)
        if not args:
            raise TypeError(f"Field '{field_name}' must specify Relation[list[T]].")

        relation_arg = ModelMetaclass._strip_optional_union(args[0])
        if get_origin(relation_arg) is not list:
            raise TypeError(
                f"Field '{field_name}' must use Relation[list[T]] for collection relationships."
            )

        inner_args = get_args(relation_arg)
        if not inner_args:
            raise TypeError(f"Field '{field_name}' must specify Relation[list[T]].")
        return ModelMetaclass._strip_optional_union(inner_args[0])

    @staticmethod
    def _relation_target_from_string(field_name: str, hint: str) -> str:
        """Extract T from a string ``Relation[list[T]]`` annotation."""
        normalized = hint.replace(" ", "")
        prefix = "Relation[list["
        if not normalized.startswith(prefix) or not normalized.endswith("]]"):
            raise TypeError(
                f"Field '{field_name}' must be annotated as Relation[list[T]] "
                "when using BackRef(), ManyToMany(), or relationship Field flags."
            )
        target = normalized[len(prefix) : -2]
        return target.strip("\"'")

    @staticmethod
    def _annotation_is_plain_list(hint: Any) -> bool:
        marker = ModelMetaclass._relationship_marker_from_annotation(hint)
        if get_origin(marker) is list:
            return True
        return isinstance(marker, str) and marker.replace(" ", "").startswith("list[")

    @staticmethod
    def _annotation_looks_like_back_ref(hint: Any) -> bool:
        if isinstance(hint, str) and "BackRef" in hint:
            return True
        if isinstance(hint, ForwardRef) and "BackRef" in hint.__forward_arg__:
            return True
        return False

    @staticmethod
    def _resolve_deferred_annotations(namespace: dict) -> dict[str, Any]:
        """
        Resolve deferred annotations (PEP 649) if present.

        Returns:
            Dictionary of resolved annotations
        """
        # Handle Python 3.14+ deferred annotations
        # We need a complete __annotations__ dict so we can safely modify it.
        if "__annotate_func__" in namespace and "__annotations__" not in namespace:
            try:
                # Format 1: Value (evaluated)
                return namespace["__annotate_func__"](1)
            except Exception as value_error:
                if "BackRef[...]" in str(value_error):
                    raise value_error
                try:
                    # Format 2: ForwardRef (non-evaluated objects)
                    return namespace["__annotate_func__"](2)
                except Exception as forward_error:
                    if "BackRef[...]" in str(forward_error):
                        raise forward_error
                    pass

        return namespace.get("__annotations__", {})

    @staticmethod
    def _scan_relationship_annotations(
        annotations: dict, namespace: dict, model_name: str
    ) -> tuple[dict, list]:
        """
        Scan annotations for relationship fields (BackRef, ForeignKey, ManyToMany).

        Returns:
            (local_relations, fields_to_remove): Relationship metadata and fields to hide from Pydantic
        """
        local_relations = {}
        fields_to_remove = []

        for field_name, hint in list(annotations.items()):
            if ModelMetaclass._annotation_looks_like_back_ref(hint):
                raise ModelMetaclass._legacy_back_ref_error(field_name)

            relationship_payload = ModelMetaclass._relationship_field_payload(
                field_name, hint, namespace
            )
            is_back_field = relationship_payload.get("back_ref") is True
            is_m2m_field = relationship_payload.get("many_to_many") is True

            if is_back_field and is_m2m_field:
                raise TypeError(
                    f"Field '{field_name}' cannot be both back_ref and many_to_many."
                )

            if is_back_field:
                marker = ModelMetaclass._relationship_marker_from_annotation(hint)
                if get_origin(marker) is Relation:
                    ModelMetaclass._relation_target_from_annotation(field_name, hint)
                elif ModelMetaclass._annotation_is_plain_list(hint):
                    raise TypeError(
                        f"Field '{field_name}' uses a plain list annotation. Use "
                        "Relation[list[T]] = BackRef() for collection back-references."
                    )
                local_relations[field_name] = "BackRef"
                fields_to_remove.append(field_name)
                continue

            if is_m2m_field:
                target = ModelMetaclass._relation_target_from_annotation(
                    field_name, hint
                )
                related_name = relationship_payload.get("related_name")
                if not related_name:
                    raise TypeError(
                        f"Field '{field_name}' uses many_to_many=True but did not "
                        "provide related_name."
                    )
                metadata = ManyToManyRelation(
                    related_name=related_name,
                    through=relationship_payload.get("through"),
                )
                metadata.to = target
                local_relations[field_name] = metadata
                _PENDING_RELATIONS.append((model_name, field_name, metadata))
                fields_to_remove.append(field_name)
                continue

            origin = get_origin(hint)
            if origin is Annotated:
                args = get_args(hint)
                for metadata in args:
                    if isinstance(metadata, ForeignKey):
                        metadata.relation_annotation = args[0]
                        inner = ModelMetaclass._strip_optional_union(args[0])
                        metadata.to = inner
                        local_relations[field_name] = metadata
                        _PENDING_RELATIONS.append((model_name, field_name, metadata))
                        fields_to_remove.append(field_name)
                        break

                    if metadata.__class__.__name__ == "ManyToManyField":
                        raise TypeError(
                            "ManyToManyField(...) is no longer supported. Use "
                            "Relation[list[T]] = ManyToMany(...)."
                        )

        return local_relations, fields_to_remove

    @staticmethod
    def _inject_shadow_fields(
        annotations: dict, namespace: dict, local_relations: dict
    ) -> None:
        """
        Inject shadow {field_name}_id fields for ForeignKeys.

        When ``ForeignKey.to`` is already a concrete model class, the shadow type is
        derived from that model's primary key annotation; otherwise a broad fallback
        union is used until ``resolve_relationships()`` can reconcile it.

        Mutates annotations and namespace in place.
        """
        for field_name, metadata in local_relations.items():
            if isinstance(metadata, ForeignKey):
                # INJECT SHADOW FIELD into annotations
                id_field = f"{field_name}_id"
                annotations[id_field] = shadow_annotation_for_foreign_key(metadata)
                # Set a default so Pydantic doesn't make it required
                namespace[id_field] = PydanticField(default=None)

    @staticmethod
    def _prepare_namespace_for_pydantic(
        namespace: dict, annotations: dict, fields_to_remove: list
    ) -> None:
        """
        Hide relationship fields from Pydantic by converting them to ClassVars.

        Mutates namespace and annotations in place.
        """
        # Hide relationship fields from Pydantic by converting them to ClassVars
        for field_name in fields_to_remove:
            annotations[field_name] = ClassVar[Any]

        # FOR PYTHON 3.14+: If we evaluated annotations, we MUST remove the func
        # so Pydantic doesn't use it and ignore our modified __annotations__.
        if "__annotate_func__" in namespace:
            del namespace["__annotate_func__"]

    @staticmethod
    def _register_model_and_proxies(cls, name: str, local_relations: dict) -> None:
        """
        Register model in global registry and inject FieldProxy for query building.

        Mutates cls in place.
        """
        _MODEL_REGISTRY_PY[name] = cls
        cls.ferro_relations = local_relations

        # Inject FieldProxy for each field to enable operator overloading on the class
        for field_name in cls.model_fields:
            setattr(cls, field_name, FieldProxy(field_name))

    @staticmethod
    def _parse_ferro_field_metadata(cls) -> dict[str, FerroField]:
        """
        Parse Ferro column metadata from model fields.

        Sources: ``Annotated[..., FerroField(...)]``, assignment ``Field(...)``
        (Ferro kwargs live under ``json_schema_extra["ferro_field"]``), and
        ``Annotated[..., Field(...)]`` (Pydantic merges that ``Field`` into the
        same ``FieldInfo`` shape as assignment).

        Returns:
            Dictionary mapping field names to FerroField metadata

        Raises:
            TypeError: If FerroField is declared twice for the same field
        """
        ferro_fields = {}
        for f_name, field_info in cls.model_fields.items():
            annotated_metadata: FerroField | None = None
            for metadata in field_info.metadata:
                if isinstance(metadata, FerroField):
                    annotated_metadata = metadata
                    break
            wrapped_metadata = None
            extra = getattr(field_info, "json_schema_extra", None)
            if isinstance(extra, dict):
                wrapped_payload = extra.get(FERRO_FIELD_EXTRA_KEY)
                if wrapped_payload:
                    field_payload = {
                        key: wrapped_payload[key]
                        for key in (
                            "primary_key",
                            "autoincrement",
                            "unique",
                            "index",
                            "nullable",
                        )
                        if key in wrapped_payload
                    }
                    if field_payload:
                        wrapped_metadata = FerroField(**field_payload)

            if annotated_metadata and wrapped_metadata:
                raise TypeError(
                    f"Field '{f_name}' cannot declare Ferro field metadata twice "
                    "(Annotated[...] + ferro.Field(...))."
                )

            if annotated_metadata:
                ferro_fields[f_name] = annotated_metadata
            elif wrapped_metadata:
                ferro_fields[f_name] = wrapped_metadata

        return ferro_fields

    @staticmethod
    def _inject_relation_descriptors(cls, local_relations: dict) -> None:
        """
        Inject descriptors for relationship fields (ForeignKey, ManyToMany).

        Mutates cls in place.
        """
        for field_name, metadata in local_relations.items():
            if isinstance(metadata, ForeignKey):
                id_field_name = f"{field_name}_id"
                setattr(cls, id_field_name, FieldProxy(id_field_name))

                target_name = (
                    metadata.to
                    if isinstance(metadata.to, str)
                    else (
                        metadata.to.__name__
                        if hasattr(metadata.to, "__name__")
                        else str(metadata.to)
                    )
                )
                if isinstance(metadata.to, ForwardRef):
                    target_name = metadata.to.__forward_arg__

                setattr(
                    cls,
                    field_name,
                    ForwardDescriptor(
                        target_model_name=target_name,
                        field_name=field_name,
                    ),
                )
            else:
                setattr(cls, field_name, None)

    @staticmethod
    def _generate_and_register_schema(
        cls, name: str, ferro_fields: dict, local_relations: dict
    ) -> None:
        """
        Generate JSON schema with Ferro metadata and register with Rust core.

        Mutates cls in place (adds __ferro_schema__).

        Raises:
            RuntimeError: If schema generation or registration fails
        """
        try:
            schema = build_model_schema(cls)

            if schema:
                setattr(cls, "__ferro_schema__", schema)
                register_model_schema(name, json.dumps(schema))
        except Exception as e:
            raise RuntimeError(f"Ferro failed to register model '{name}': {e}")
