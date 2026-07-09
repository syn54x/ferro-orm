from typing import ForwardRef

from ..ir import register_model_with_ir
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
    mark_modelset_resolved,
    resolve_model_reference,
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

    recompile_targets: set[str] = set()

    for model_name, field_name, rel in to_process:
        recompile_targets.add(model_name)
        # 1. Resolve 'to' model
        if isinstance(rel.to, (str, ForwardRef)):
            to_name = rel.to if isinstance(rel.to, str) else rel.to.__forward_arg__
            target_model = resolve_model_reference(to_name, default=None)
            if not target_model:
                raise RuntimeError(
                    f"Relationship resolution failed: '{to_name}' not found"
                )
            rel.to = target_model

        # 2. Cross-validate with declared reverse relation field.
        target_model = rel.to
        recompile_targets.add(target_model.__ferro_identity__)
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
            source_model = _MODEL_REGISTRY_PY[model_name]
            source_table = source_model.__ferro_table__
            target_table = target_model.__ferro_table__

            # Resolve join table
            if not rel.through:
                # Default join table name: source table + field name.
                join_table = f"{source_table}_{field_name}"
            else:
                join_table = rel.through

            for key, other in _MODEL_REGISTRY_PY.items():
                if getattr(other, "__ferro_table__", None) == join_table:
                    raise RuntimeError(
                        f"M2M relation '{model_name}.{field_name}' derives join "
                        f"table '{join_table}', which is already the table of "
                        f"model '{key}'. Set through= on the relation or "
                        "__ferro_table__ on the model to resolve the collision."
                    )

            source_col = f"{source_table}_id"
            target_col = f"{target_table}_id"

            # Inject M2M descriptors into BOTH sides
            # Source -> Target
            setattr(
                source_model,
                field_name,
                RelationshipDescriptor(
                    target_model_name=target_model.__ferro_identity__,
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
                pk_python_type_for_model(source_model)
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
                            "to_table": source_table,
                            "on_delete": "CASCADE",
                        },
                    },
                    target_col: {
                        **target_schema,
                        "ferro_nullable": False,
                        "foreign_key": {
                            "to_table": target_table,
                            "on_delete": "CASCADE",
                        },
                    },
                },
                "required": [source_col, target_col],
                "ferro_composite_uniques": [[source_col, target_col]],
            }
            if rel.reverse_index:
                join_schema["ferro_composite_indexes"] = [[target_col, source_col]]
            register_model_with_ir(join_table, join_schema, join_table)
            _JOIN_TABLE_REGISTRY[join_table] = join_schema

    rebuilt = reconcile_shadow_fk_types(_MODEL_REGISTRY_PY)
    for model_cls in rebuilt:
        recompile_targets.add(model_cls.__ferro_identity__)

    # Second pass: recompile only models whose schema changed during resolution
    # (relationship sources/targets and shadow-FK reconciliation targets).
    for model_name in sorted(recompile_targets):
        model_cls = _MODEL_REGISTRY_PY.get(model_name)
        if model_cls is None:
            continue
        try:
            schema = build_model_schema(model_cls)
        except Exception as exc:
            raise RuntimeError(
                f"Ferro failed to rebuild the schema for model '{model_name}' "
                f"while resolving relationships: {exc}"
            ) from exc
        register_model_with_ir(
            model_name, schema, model_cls.__ferro_table__
        )

    mark_modelset_resolved()
    _PENDING_RELATIONS.clear()
