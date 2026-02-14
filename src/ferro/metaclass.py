import json
from typing import (
    Annotated,
    Any,
    ClassVar,
    ForwardRef,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, Field as PydanticField
from pydantic.fields import FieldInfo

from ._core import register_model_schema
from .base import FerroField, ForeignKey, ManyToManyField
from .fields import FERRO_FIELD_EXTRA_KEY
from .query import BackRef, FieldProxy
from .relations.descriptors import ForwardDescriptor
from .state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS


def _field_has_back_ref(obj: Any) -> bool:
    """Return True if obj is a FieldInfo with back_ref=True in its Ferro extra."""
    if not isinstance(obj, FieldInfo):
        return False
    extra = getattr(obj, "json_schema_extra", None)
    if not isinstance(extra, dict):
        return False
    return extra.get(FERRO_FIELD_EXTRA_KEY, {}).get("back_ref") is True


class ModelMetaclass(type(BaseModel)):
    """
    Metaclass for Ferro models that automatically registers the model schema with the Rust core.
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        # 1. Handle Python 3.14+ deferred annotations
        # We need a complete __annotations__ dict so we can safely modify it.
        if "__annotate_func__" in namespace and "__annotations__" not in namespace:
            try:
                # Format 1: Value (evaluated)
                namespace["__annotations__"] = namespace["__annotate_func__"](1)
            except Exception:
                try:
                    # Format 2: ForwardRef (non-evaluated objects)
                    namespace["__annotations__"] = namespace["__annotate_func__"](2)
                except Exception:
                    pass

        annotations = namespace.get("__annotations__", {})
        local_relations = {}
        fields_to_remove = []

        for field_name, hint in list(annotations.items()):
            origin = get_origin(hint)
            # Type-side back-ref: BackRef[...] in annotation (or inside Annotated)
            is_back_type = origin is BackRef
            if not is_back_type and origin is Annotated:
                args = get_args(hint)
                if args and get_origin(args[0]) is BackRef:
                    is_back_type = True
            if not is_back_type and isinstance(hint, str) and "BackRef" in hint:
                is_back_type = True
            if (
                not is_back_type
                and isinstance(hint, ForwardRef)
                and "BackRef" in hint.__forward_arg__
            ):
                is_back_type = True

            # Field-side back-ref: Field(back_ref=True) as default or in Annotated
            is_back_field = False
            default_val = namespace.get(field_name)
            if _field_has_back_ref(default_val):
                is_back_field = True
            if not is_back_field and origin is Annotated:
                for metadata in get_args(hint)[1:]:
                    if isinstance(metadata, FieldInfo) and _field_has_back_ref(
                        metadata
                    ):
                        is_back_field = True
                        break

            if is_back_type and is_back_field:
                raise TypeError(
                    f"Cannot use both BackRef and Field(back_ref=True) on the same "
                    f"field '{field_name}'."
                )

            if is_back_type or is_back_field:
                local_relations[field_name] = "BackRef"
                fields_to_remove.append(field_name)
                continue

            if origin is Annotated:
                args = get_args(hint)
                for metadata in args:
                    if isinstance(metadata, ForeignKey):
                        metadata.to = args[0]
                        local_relations[field_name] = metadata
                        _PENDING_RELATIONS.append((name, field_name, metadata))
                        fields_to_remove.append(field_name)

                        # INJECT SHADOW FIELD into annotations
                        id_field = f"{field_name}_id"
                        annotations[id_field] = Union[int, str, None]
                        # Set a default so Pydantic doesn't make it required
                        namespace[id_field] = PydanticField(default=None)
                        break

                    if isinstance(metadata, ManyToManyField):
                        origin = get_origin(args[0])
                        if origin is list:
                            inner_args = get_args(args[0])
                            if inner_args:
                                metadata.to = inner_args[0]
                        else:
                            metadata.to = args[0]
                        local_relations[field_name] = metadata
                        _PENDING_RELATIONS.append((name, field_name, metadata))
                        fields_to_remove.append(field_name)
                        break

        # Hide relationship fields from Pydantic by converting them to ClassVars
        for field_name in fields_to_remove:
            annotations[field_name] = ClassVar[Any]

        # FOR PYTHON 3.14+: If we evaluated annotations, we MUST remove the func
        # so Pydantic doesn't use it and ignore our modified __annotations__.
        if "__annotate_func__" in namespace:
            del namespace["__annotate_func__"]

        # 2. Create the class using Pydantic's internal logic
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # 3. Skip the 'Model' base class itself
        if name != "Model":
            _MODEL_REGISTRY_PY[name] = cls
            cls.ferro_relations = local_relations

            # Inject FieldProxy for each field to enable operator overloading on the class
            for field_name in cls.model_fields:
                setattr(cls, field_name, FieldProxy(field_name))

            # 4. Parse FerroField metadata
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
                        wrapped_metadata = FerroField(**wrapped_payload)

                if annotated_metadata and wrapped_metadata:
                    raise TypeError(
                        f"Field '{f_name}' cannot declare Ferro field metadata twice "
                        "(Annotated[...] + ferro.Field(...))."
                    )

                if annotated_metadata:
                    ferro_fields[f_name] = annotated_metadata
                elif wrapped_metadata:
                    ferro_fields[f_name] = wrapped_metadata

            # 5. Inject descriptors for ForeignKeys
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

                    setattr(cls, field_name, ForwardDescriptor(field_name, target_name))
                else:
                    setattr(cls, field_name, None)

            cls.ferro_fields = ferro_fields

            # 6. Initial Schema Generation (might fail if circular)
            try:
                try:
                    schema = cls.model_json_schema()
                except Exception:
                    schema = None

                if schema:
                    if "properties" in schema:
                        for f_name, metadata in ferro_fields.items():
                            if f_name in schema["properties"]:
                                schema["properties"][f_name][
                                    "primary_key"
                                ] = metadata.primary_key
                                prop = schema["properties"][f_name]
                                is_int = prop.get("type") == "integer" or any(
                                    item.get("type") == "integer"
                                    for item in prop.get("anyOf", [])
                                )
                                auto = metadata.autoincrement
                                if auto is None:
                                    auto = metadata.primary_key and is_int
                                metadata.autoincrement = auto
                                schema["properties"][f_name]["autoincrement"] = auto
                                schema["properties"][f_name]["unique"] = metadata.unique
                                schema["properties"][f_name]["index"] = metadata.index

                        for f_name, metadata in local_relations.items():
                            if isinstance(metadata, ForeignKey):
                                id_field = f"{f_name}_id"
                                if id_field in schema["properties"]:
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

                                    schema["properties"][id_field]["foreign_key"] = {
                                        "to_table": target_name.lower(),
                                        "on_delete": metadata.on_delete,
                                        "unique": metadata.unique,
                                    }

                    register_model_schema(name, json.dumps(schema))
            except Exception as e:
                raise RuntimeError(f"Ferro failed to register model '{name}': {e}")

        return cls
