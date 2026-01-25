import json
from typing import ForwardRef

from .._core import register_model_schema
from ..base import ForeignKey, ManyToManyField
from ..state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS
from .descriptors import RelationshipDescriptor


def resolve_relationships():
    """Finalize all pending relationships and cross-validate."""
    global _PENDING_RELATIONS

    # Copy and clear so that we don't process the same relations multiple times
    # if resolve_relationships is called again (e.g. in tests)
    to_process = list(_PENDING_RELATIONS)
    _PENDING_RELATIONS.clear()

    for model_name, field_name, rel in to_process:
        # 1. Resolve 'to' model
        if isinstance(rel.to, (str, ForwardRef)):
            to_name = rel.to if isinstance(rel.to, str) else rel.to.__forward_arg__
            target_model = _MODEL_REGISTRY_PY.get(to_name)
            if not target_model:
                raise RuntimeError(
                    f"Relationship resolution failed: '{to_name}' not found"
                )
            rel.to = target_model

        # 2. Cross-validate with BackRelationship
        target_model = rel.to
        if not hasattr(target_model, rel.related_name):
            raise RuntimeError(
                f"Model '{model_name}' defines a relationship to '{target_model.__name__}' "
                f"with related_name='{rel.related_name}', but '{target_model.__name__}' "
                f"does not have that field defined as a BackRelationship."
            )

        # 3. Inject Descriptor into target model
        if isinstance(rel, ForeignKey):
            setattr(
                target_model,
                rel.related_name,
                RelationshipDescriptor(
                    model_name, field_name, is_one_to_one=getattr(rel, "unique", False)
                ),
            )
        elif isinstance(rel, ManyToManyField):
            # Resolve join table
            if not rel.through:
                # Default join table name: alphabetized model names
                # Actually, we should probably just use source_model_field_name
                # or similar to avoid confusion.
                join_table = f"{model_name.lower()}_{field_name}"
            else:
                join_table = rel.through

            source_col = f"{model_name.lower()}_id"
            target_col = f"{target_model.__name__.lower()}_id"

            # Inject M2M descriptors into BOTH sides
            # Source -> Target
            setattr(
                _MODEL_REGISTRY_PY[model_name],
                field_name,
                RelationshipDescriptor(
                    target_model.__name__,
                    field_name,
                    is_m2m=True,
                    join_table=join_table,
                    source_col=source_col,
                    target_col=target_col,
                ),
            )
            # Target -> Source
            setattr(
                target_model,
                rel.related_name,
                RelationshipDescriptor(
                    model_name,
                    rel.related_name,
                    is_m2m=True,
                    join_table=join_table,
                    source_col=target_col, # Reversed for the back side
                    target_col=source_col,
                ),
            )

            # 4. Register Join Table schema with Rust
            join_schema = {
                "properties": {
                    source_col: {
                        "type": "integer",
                        "foreign_key": {
                            "to_table": model_name.lower(),
                            "on_delete": "CASCADE",
                        },
                    },
                    target_col: {
                        "type": "integer",
                        "foreign_key": {
                            "to_table": target_model.__name__.lower(),
                            "on_delete": "CASCADE",
                        },
                    },
                }
            }
            register_model_schema(join_table, json.dumps(join_schema))

    # Second pass: Re-register schemas
    for model_name, model_cls in _MODEL_REGISTRY_PY.items():
        try:
            schema = model_cls.model_json_schema()
            if "properties" in schema:
                for f_name, metadata in model_cls.ferro_fields.items():
                    if f_name in schema["properties"]:
                        schema["properties"][f_name]["primary_key"] = (
                            metadata.primary_key
                        )
                        schema["properties"][f_name]["autoincrement"] = (
                            metadata.autoincrement
                        )
                        schema["properties"][f_name]["unique"] = metadata.unique
                        schema["properties"][f_name]["index"] = metadata.index

                for f_name, metadata in model_cls.ferro_relations.items():
                    if isinstance(metadata, ForeignKey):
                        id_field = f"{f_name}_id"
                        if id_field in schema["properties"]:
                            target_name = (
                                metadata.to.__name__
                                if hasattr(metadata.to, "__name__")
                                else str(metadata.to)
                            )
                            # Apply unique constraint to the ID column if it's a 1:1
                            if metadata.unique:
                                schema["properties"][id_field]["unique"] = True

                            schema["properties"][id_field]["foreign_key"] = {
                                "to_table": target_name.lower(),  # Rust expects lowercase
                                "on_delete": metadata.on_delete,
                                "unique": metadata.unique,
                            }
            register_model_schema(model_name, json.dumps(schema))
        except Exception:
            pass

    _PENDING_RELATIONS.clear()
