from typing import ForwardRef

from .._shadow_fk_types import pk_python_type_for_model, reconcile_shadow_fk_types
from ..base import ForeignKey, ManyToManyRelation
from ..columns import fk_shadow_spec
from ..ir.compiler import compile_model_schema_ir, register_model_with_ir
from ..registry import REGISTRY
from .descriptors import RelationshipDescriptor


def resolve_relationships():
    """Finalize all pending relationships and cross-validate.

    After binding each ``ForeignKey.to`` to a concrete model, upgrades shadow
    ``{name}_id`` Pydantic annotations from the forward-ref fallback to the related
    model's PK type where applicable, then ``model_rebuild``s affected classes before
    the schema re-registration pass.
    """
    # Drain (copy-and-clear) so that we don't process the same relations
    # multiple times if resolve_relationships is called again (e.g. in tests)
    to_process = REGISTRY.drain_pending_relations()

    recompile_targets: set[str] = set()

    for model_name, field_name, rel in to_process:
        recompile_targets.add(model_name)
        # 1. Resolve 'to' model
        if isinstance(rel.to, (str, ForwardRef)):
            to_name = rel.to if isinstance(rel.to, str) else rel.to.__forward_arg__
            target_model = REGISTRY.resolve_reference(to_name, default=None)
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
            source_model = REGISTRY.models()[model_name]
            source_table = source_model.__ferro_table__
            target_table = target_model.__ferro_table__

            # Resolve join table
            if not rel.through:
                # Default join table name: source table + field name.
                join_table = f"{source_table}_{field_name}"
            else:
                join_table = rel.through

            for key, other in REGISTRY.models().items():
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
            join_columns = (
                fk_shadow_spec(
                    source_col,
                    python_type=pk_python_type_for_model(source_model),
                    to_table=source_table,
                ),
                fk_shadow_spec(
                    target_col,
                    python_type=pk_python_type_for_model(target_model),
                    to_table=target_table,
                ),
            )
            composite_uniques = ((source_col, target_col),)
            composite_indexes = (
                ((target_col, source_col),) if rel.reverse_index else ()
            )
            register_model_with_ir(
                join_table,
                join_columns,
                join_table,
                composite_uniques=composite_uniques,
                composite_indexes=composite_indexes,
            )
            REGISTRY.add_join_table(
                join_table,
                {
                    "columns": join_columns,
                    "composite_uniques": composite_uniques,
                    "composite_indexes": composite_indexes,
                },
            )

    rebuilt = reconcile_shadow_fk_types(REGISTRY.models())
    for model_cls in rebuilt:
        recompile_targets.add(model_cls.__ferro_identity__)

    # Second pass: rebuild specs only for models whose schema changed during
    # resolution (relationship sources/targets and shadow-FK reconciliation
    # targets). Replacement, never mutation (grilling Q3).
    for model_name in sorted(recompile_targets):
        model_cls = REGISTRY.models().get(model_name)
        if model_cls is None:
            continue
        try:
            compile_model_schema_ir(model_name, model_cls)
        except Exception as exc:
            raise RuntimeError(
                f"Ferro failed to rebuild the schema for model '{model_name}' "
                f"while resolving relationships: {exc}"
            ) from exc

    REGISTRY.mark_resolved()
    REGISTRY.drain_pending_relations()
