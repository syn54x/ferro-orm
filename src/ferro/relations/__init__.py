import json
from typing import ForwardRef

from .._core import register_model_schema
from .._shadow_fk_types import (
    pk_python_type_for_model,
    reconcile_shadow_fk_types,
    schema_fragment_for_pk,
)
from ..base import ForeignKey, ManyToManyRelation
from ..schema_metadata import build_model_schema
from ..state import (  # noqa: F401
    _JOIN_TABLE_REGISTRY,
    _MODEL_REGISTRY_PY,
    _PENDING_RELATIONS,
)
from .descriptors import RelationshipDescriptor


def resolve_relationships():
    """Finalize all pending relationships and cross-validate.

    After binding each ``ForeignKey.to`` to a concrete model, upgrades shadow
    ``{name}_id`` Pydantic annotations from the forward-ref fallback to the related
    model's PK type where applicable, then ``model_rebuild``s affected classes before
    the schema re-registration pass.
    """
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

        # 2. Cross-validate with declared reverse relation field.
        target_model = rel.to
        if not hasattr(target_model, rel.related_name):
            raise RuntimeError(
                f"Model '{model_name}' defines a relationship to '{target_model.__name__}' "
                f"with related_name='{rel.related_name}', but '{target_model.__name__}' "
                f"does not have that field defined as BackRef()/Field(back_ref=True)."
            )

        # 3. Inject Descriptor into target model
        if isinstance(rel, ForeignKey):
            setattr(
                target_model,
                rel.related_name,
                RelationshipDescriptor(
                    target_model_name=model_name,
                    field_name=field_name,
                    is_one_to_one=getattr(rel, "unique", False),
                ),
            )
        elif isinstance(rel, ManyToManyRelation):
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
                    target_model_name=target_model.__name__,
                    field_name=field_name,
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
                    target_model_name=model_name,
                    field_name=rel.related_name,
                    is_m2m=True,
                    join_table=join_table,
                    source_col=target_col,  # Reversed for the back side
                    target_col=source_col,
                ),
            )

            # 4. Register Join Table schema with Rust
            source_schema = schema_fragment_for_pk(
                pk_python_type_for_model(_MODEL_REGISTRY_PY[model_name])
            )
            target_schema = schema_fragment_for_pk(
                pk_python_type_for_model(target_model)
            )
            join_schema = {
                "properties": {
                    source_col: {
                        **source_schema,
                        "ferro_nullable": False,
                        "foreign_key": {
                            "to_table": model_name.lower(),
                            "on_delete": "CASCADE",
                        },
                    },
                    target_col: {
                        **target_schema,
                        "ferro_nullable": False,
                        "foreign_key": {
                            "to_table": target_model.__name__.lower(),
                            "on_delete": "CASCADE",
                        },
                    },
                },
                "required": [source_col, target_col],
                "ferro_composite_uniques": [[source_col, target_col]],
            }
            if rel.reverse_index:
                join_schema["ferro_composite_indexes"] = [[target_col, source_col]]
            register_model_schema(join_table, json.dumps(join_schema))
            _JOIN_TABLE_REGISTRY[join_table] = join_schema

    reconcile_shadow_fk_types(_MODEL_REGISTRY_PY)

    # Second pass: Re-register schemas
    for model_name, model_cls in _MODEL_REGISTRY_PY.items():
        try:
            schema = build_model_schema(model_cls)
            register_model_schema(model_name, json.dumps(schema))
        except Exception:
            pass

    _PENDING_RELATIONS.clear()
