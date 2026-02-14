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

from pydantic import BaseModel, Field

from ._core import register_model_schema
from .base import FerroField, ForeignKey, ManyToManyField
from .query import BackRelationship, FieldProxy
from .relations.descriptors import ForwardDescriptor
from .state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS


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
            is_back = False
            origin = get_origin(hint)
            if origin is BackRelationship:
                is_back = True
            elif isinstance(hint, str) and "BackRelationship" in hint:
                is_back = True
            elif (
                isinstance(hint, ForwardRef)
                and "BackRelationship" in hint.__forward_arg__
            ):
                is_back = True

            if is_back:
                local_relations[field_name] = "BackRelationship"
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
                        namespace[id_field] = Field(default=None)
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
            try:
                for f_name, field_info in cls.model_fields.items():
                    for metadata in field_info.metadata:
                        if isinstance(metadata, FerroField):
                            ferro_fields[f_name] = metadata
                            break
            except Exception:
                pass

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
                                id_field = f"{field_name}_id"
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
