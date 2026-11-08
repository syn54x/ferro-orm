"""Relation resolution: binding each declared relation to its target model."""

import json
import re
from dataclasses import replace
from types import UnionType
from typing import Any, ForwardRef, Union, get_args, get_origin

from pydantic_core import PydanticUndefined

from .. import _core
from .._annotation_utils import annotation_allows_none
from .._shadow_fk_types import pk_python_type_for_model, reconcile_shadow_fk_types
from ..base import (
    FerroField,
    ForeignKey,
    ForwardRelation,
    ManyToManyRelation,
    ReverseRelation,
)
from ..columns import ColumnSpec, fk_shadow_spec, refuse_back_ref_over_retired_relation
from ..fields import annotation_text, field_line, field_options
from ..ir.compiler import (
    RenameHints,
    _column_ir_from_spec,
    compile_model_schema_ir,
    register_model_with_ir,
)
from ..registry import REGISTRY
from .descriptors import RelationshipDescriptor


def _default_join_table(source_table: str, field_name: str) -> str:
    """A ``ManyToMany``'s join table when ``through=`` is not set."""
    return f"{source_table}_{field_name}"


def _join_column(table: str) -> str:
    """The join table's column that points at ``table``."""
    return f"{table}_id"


def _join_table_rename_hints(
    source_model: type, target_model: type, field_name: str, through: str | None
) -> RenameHints:
    """What a join table and its columns were called before its source or
    target model's declared table rename (ADR-0032), named by the same two
    functions that name them now, so a table rename drags its join table."""
    source_prev = getattr(source_model, "__ferro_renamed_from__", None)
    target_prev = getattr(target_model, "__ferro_renamed_from__", None)
    columns: dict[str, str] = {}
    if source_prev is not None:
        columns[_join_column(source_model.__ferro_table__)] = _join_column(source_prev)
    if target_prev is not None:
        columns[_join_column(target_model.__ferro_table__)] = _join_column(target_prev)
    table = (
        _default_join_table(source_prev, field_name)
        if source_prev is not None and not through
        else None
    )
    return RenameHints(table=table, columns=columns)


# -- foreign-key columns and the relations that name them -----------------------------


def _resolve_target(target: Any, *, final: bool) -> Any:
    """The model ``target`` names: a class as it is, a name from the registry.

    Before relations resolve (``final=False``) a name nothing answers yet is
    ``None``, and the check that needed it waits for resolution.
    """
    if isinstance(target, type):
        return target
    name = target.__forward_arg__ if isinstance(target, ForwardRef) else target
    if not final:
        try:
            return REGISTRY.resolve_reference(name, default=None)
        except RuntimeError:
            return None
    model = REGISTRY.resolve_reference(name, default=None)
    if model is None:
        raise RuntimeError(f"Relationship resolution failed: '{name}' not found")
    return model


def _scalar(annotation: Any) -> Any:
    """``annotation`` with ``| None`` set aside and a ``NewType`` read as its supertype."""
    if get_origin(annotation) in (Union, UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(members) == 1:
            annotation = members[0]
    while hasattr(annotation, "__supertype__"):
        annotation = annotation.__supertype__
    return annotation


def _storage(spec: ColumnSpec) -> dict[str, str]:
    """The column's storage per dialect, as the one storage decision names it."""
    column = _column_ir_from_spec(
        replace(spec, primary_key=False, autoincrement=False, foreign_key=None)
    )
    storage = {}
    for dialect in ("postgres", "sqlite"):
        resolved = json.loads(_core._resolve_storage_type(json.dumps(column), dialect))
        storage[dialect] = (
            f"enum {resolved['name']}"
            if resolved["kind"] == "pg_enum"
            else resolved["token"]
        )
    return storage


def _target_key(model_cls: Any, name: str, target: Any) -> tuple[str, Any]:
    """``target``'s primary-key column and its type, ``| None`` set aside."""
    pk = target.__ferro_pk__
    if pk is None:
        raise TypeError(
            f"{model_cls.__name__}.{name}: {target.__name__} has no primary key "
            "for a foreign key to point at. Write: id: int | None = "
            f"Field(default=None, primary_key=True) on {target.__name__}"
        )
    python_type = target.__ferro_columns__[pk].python_type
    if get_origin(python_type) in (Union, UnionType):
        members = [arg for arg in get_args(python_type) if arg is not type(None)]
        if len(members) == 1:
            python_type = members[0]
    return pk, python_type


def check_key_column(model_cls: Any, name: str, meta: FerroField, target: Any) -> None:
    """Refuse a foreign-key column whose type or storage is not its target key's.

    Args:
        model_cls: The model declaring the column.
        name: The column's name.
        meta: The column's options.
        target: The model the column points at.

    Raises:
        TypeError: The column's type, with `| None` set aside and a
            `NewType` read as its supertype, is not the target key's type;
            or the column stores differently from the target key, naming
            the `db_type` to add.
    """
    info = model_cls.model_fields.get(name)
    if info is None:
        return  # retired: its column keeps the spec it was compiled with
    model = model_cls.__name__
    pk, key_type = _target_key(model_cls, name, target)
    nullable = annotation_allows_none(info.annotation)
    if _scalar(info.annotation) != _scalar(key_type):
        key_text = annotation_text(key_type)
        line = field_line(
            name,
            key_text + (" | None" if nullable else ""),
            field_options(meta),
            info.default,
        )
        raise TypeError(
            f"{model}.{name}: {target.__name__}.{pk} is {key_text}, so its "
            f"foreign-key column is {key_text} too. Write: {line}"
        )
    column = _storage(model_cls.__ferro_columns__[name])
    key = _storage(target.__ferro_columns__[pk])
    for dialect in key:
        if column[dialect] != key[dialect]:
            line = field_line(
                name,
                annotation_text(info.annotation),
                field_options(meta, db_type=key[dialect]),
                info.default,
            )
            raise TypeError(
                f"{model}.{name}: {target.__name__}.{pk} is stored as "
                f"{key[dialect]} and this column as {column[dialect]}. Write: {line}"
            )


def _key_columns_to(model_cls: Any, target: Any) -> list[str]:
    """``model_cls``'s foreign-key columns that point at ``target``."""
    return [
        name
        for name, meta in model_cls.__ferro_fields__.items()
        if meta.foreign_key is not None
        and _resolve_target(meta.foreign_key, final=False) is target
    ]


def _foreign_key_option(
    model_cls: Any, target: Any, bound: frozenset[str] | None = None
) -> Any:
    """How ``model_cls`` writes ``foreign_key=``: by name for a target not
    defined yet where it is declared (a self reference among them)."""
    later = bound is not None and target.__name__ not in bound
    return target.__name__ if target is model_cls or later else target


def _new_key_line(
    model_cls: Any,
    column: str,
    target: Any,
    nullable: bool,
    bound: frozenset[str] | None = None,
) -> str:
    """The declaration of a foreign-key column ``column`` to ``target``."""
    _, key_type = _target_key(model_cls, column, target)
    return field_line(
        column,
        annotation_text(key_type) + (" | None" if nullable else ""),
        field_options(None, foreign_key=_foreign_key_option(model_cls, target, bound)),
        None if nullable else PydanticUndefined,
    )


def relation_line(
    field_name: str,
    spelling: str,
    target: str,
    nullable: bool,
    column: str,
    bound: frozenset[str] | None,
) -> str:
    """Write a relation's declaration: ``team: Relation[Team] = Forward("team_id")``.

    A target that was not defined yet where the relation is declared, in a
    module that evaluates its annotations, is written as a string, so the
    line works pasted where the refused one stood.

    Args:
        field_name: The relation's field name.
        spelling: ``"Forward"`` or ``"Reverse"``.
        target: The model the relation reads, by name.
        nullable: Whether a forward relation's column allows ``None``.
        column: The column the relation names.
        bound: The names defined where the relation is declared, or ``None``
            under postponed annotations.

    Returns:
        The declaration, as one line of source.
    """
    if target.startswith("list[") and target.endswith("]"):
        target = target[len("list[") : -1]
    quoted = bound is not None and target not in bound
    if spelling == "Reverse":
        shown = f'list["{target}"]' if quoted else f"list[{target}]"
    else:
        shown = target + (" | None" if nullable else "")
        shown = f'"{shown}"' if quoted else shown
    return f'{field_name}: Relation[{shown}] = {spelling}("{column}")'


def relation_refusal(
    model: str,
    field_name: str,
    reason: str,
    spelling: str,
    column: str,
    target: str,
    nullable: bool,
    bound: frozenset[str] | None = None,
) -> TypeError:
    """The refusal of a relation not declared ``name: Relation[...] = Forward(...)``.

    Args:
        model: The model's name.
        field_name: The relation's field name.
        reason: What is wrong with the declaration.
        spelling: ``"Forward"`` or ``"Reverse"``.
        column: The column the relation names.
        target: The model the relation reads, by name.
        nullable: Whether a forward relation's column allows ``None``.
        bound: The names defined where the relation is declared, or ``None``
            under postponed annotations. Defaults to ``None``.

    Returns:
        The error, its message ending with the line to write.
    """
    line = relation_line(field_name, spelling, target, nullable, column, bound)
    return TypeError(f"{model}.{field_name}: {reason}. Write: {line}")


def _refuse_unnamed_forward(
    model_cls: Any, name: str, relation: ForwardRelation
) -> None:
    """Raise a bare ``Relation`` refusal the class body could not name a target for."""
    model = model_cls.__name__
    column = relation.fk_column
    meta = model_cls.__ferro_fields__.get(column)
    info = model_cls.model_fields.get(column)
    if meta is not None and meta.foreign_key is not None and info is not None:
        target = _resolve_target(meta.foreign_key, final=True)
        raise relation_refusal(
            model,
            name,
            relation.refusal or "",
            "Forward",
            column,
            target.__name__,
            annotation_allows_none(info.annotation),
            relation.bound,
        )
    missing = (
        f"{column} has no foreign_key"
        if info is not None
        else f"{model} has no column {column}"
    )
    # No line can be written: nothing names the model the relation reads.
    raise TypeError(
        f"{model}.{name}: {relation.refusal}, and {missing} to name the model "
        f"it reads. Declare {column} with Field(foreign_key=...) naming that "
        f"model, and annotate {name} as Relation[...] of the same model"
    )


def _refuse_unnamed_reverse(
    model_cls: Any, name: str, relation: ReverseRelation
) -> None:
    """Raise a bare ``Relation`` refusal the class body could not name a model for."""
    model = model_cls.__name__
    column = relation.attribute
    pointing_back = [
        other
        for other in REGISTRY.models().values()
        if (meta := (other.__dict__.get("__ferro_fields__") or {}).get(column))
        is not None
        and meta.foreign_key is not None
        and _resolve_target(meta.foreign_key, final=False) is model_cls
    ]
    if len(pointing_back) == 1:
        raise relation_refusal(
            model,
            name,
            relation.refusal or "",
            "Reverse",
            column,
            pointing_back[0].__name__,
            False,
            relation.bound,
        )
    if pointing_back:
        names = [other.__name__ for other in pointing_back]
        declaring = ", ".join(names[:-1]) + f" and {names[-1]}"
        lines = " or ".join(
            relation_line(name, "Reverse", other, False, column, relation.bound)
            for other in names
        )
        raise TypeError(
            f"{model}.{name}: {relation.refusal}, and {declaring} each declare "
            f"{column} pointing at {model}. Write: {lines}"
        )
    _, key_type = _target_key(model_cls, name, model_cls)
    key = field_line(column, annotation_text(key_type), {"foreign_key": model_cls})
    # No line can be written: no model is the one the relation reads.
    raise TypeError(
        f"{model}.{name}: {relation.refusal}, and no model declares a "
        f"foreign-key column {column} pointing at {model}. Declare {key} on the "
        f"model the relation reads, and annotate {name} as Relation[list[...]] "
        "of that model"
    )


def check_forward_relation(
    model_cls: Any, name: str, relation: ForwardRelation, *, final: bool
) -> None:
    """Refuse a forward relation its foreign-key column does not back.

    Args:
        model_cls: The model declaring the relation.
        name: The relation's field name.
        relation: The relation's declaration; its target is bound here.
        final: Whether relations are resolving, so every name must resolve.

    Raises:
        TypeError: The column is missing or has no `foreign_key`, points at
            another model, or disagrees with the relation on `| None`.
    """
    if relation.refusal is not None:
        if final:
            _refuse_unnamed_forward(model_cls, name, relation)
        return
    target = _resolve_target(relation.to, final=final)
    if target is None:
        return
    relation.to = target
    model = model_cls.__name__
    column = relation.fk_column
    info = model_cls.model_fields.get(column)
    meta = model_cls.__ferro_fields__.get(column)
    if column in _retired_fields(model_cls):
        raise TypeError(
            f"{model}.{name}: {column} is retired, so the relation is retired "
            "with it. Delete: "
            + relation_line(
                name,
                "Forward",
                target.__name__,
                relation.nullable,
                column,
                relation.bound,
            )
        )
    if info is None:
        keys = _key_columns_to(model_cls, target)
        if len(keys) == 1:
            nullable = annotation_allows_none(
                model_cls.model_fields[keys[0]].annotation
            )
            fix = relation_line(
                name, "Forward", target.__name__, nullable, keys[0], relation.bound
            )
        else:
            fix = _new_key_line(
                model_cls, column, target, relation.nullable, relation.bound
            )
        raise TypeError(f"{model}.{name}: {model} has no column {column}. Write: {fix}")
    if meta is None or meta.foreign_key is None:
        line = field_line(
            column,
            annotation_text(info.annotation),
            field_options(
                meta,
                foreign_key=_foreign_key_option(model_cls, target, relation.bound),
            ),
            info.default,
        )
        raise TypeError(f"{model}.{name}: {column} has no foreign_key. Write: {line}")
    points_at = _resolve_target(meta.foreign_key, final=final)
    if points_at is None:
        return
    nullable = annotation_allows_none(info.annotation)
    if points_at is not target:
        raise TypeError(
            f"{model}.{name}: {column} points at {points_at.__name__}, not "
            f"{target.__name__}. Write: "
            + relation_line(
                name, "Forward", points_at.__name__, nullable, column, relation.bound
            )
        )
    if nullable != relation.nullable:
        raise TypeError(
            f"{model}.{name}: {column} is {'nullable' if nullable else 'required'}, "
            "so the relation is too. Write: "
            + relation_line(
                name, "Forward", target.__name__, nullable, column, relation.bound
            )
        )


def _retired_fields(model_cls: Any) -> dict:
    """The model's retired columns, which a relation can no longer read."""
    return model_cls.__dict__.get("__ferro_retired_fields__") or {}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def check_reverse_relation(
    model_cls: Any, name: str, relation: ReverseRelation, *, final: bool
) -> None:
    """Refuse a reverse relation whose column does not point back at its model.

    Args:
        model_cls: The model declaring the relation.
        name: The relation's field name.
        relation: The relation's declaration; its other model is bound here.
        final: Whether relations are resolving, so every name must resolve.

    Raises:
        TypeError: The other model has no such column, the column has no
            `foreign_key`, or it points at another model.
    """
    if relation.refusal is not None:
        if final:
            _refuse_unnamed_reverse(model_cls, name, relation)
        return
    other = _resolve_target(relation.to, final=final)
    if other is None:
        return
    relation.to = other
    model = model_cls.__name__
    column = relation.attribute
    if column in _retired_fields(other):
        raise TypeError(
            f"{model}.{name}: {other.__name__}.{column} is retired, so the relation "
            "is retired with it. Delete: "
            + relation_line(
                name, "Reverse", other.__name__, False, column, relation.bound
            )
        )
    keys = _key_columns_to(other, model_cls)
    if len(keys) == 1:
        fix = relation_line(
            name, "Reverse", other.__name__, False, keys[0], relation.bound
        )
    else:
        added = _new_key_line(other, f"{_snake(model)}_id", model_cls, False)
        fix = f"{added} on {other.__name__}"
    info = other.model_fields.get(column)
    meta = other.__ferro_fields__.get(column)
    if info is None:
        raise TypeError(
            f"{model}.{name}: {other.__name__} has no column {column}. Write: {fix}"
        )
    if meta is None or meta.foreign_key is None:
        line = field_line(
            column,
            annotation_text(info.annotation),
            field_options(meta, foreign_key=_foreign_key_option(other, model_cls)),
            info.default,
        )
        raise TypeError(
            f"{model}.{name}: {other.__name__}.{column} has no foreign_key. "
            f"Write: {line} on {other.__name__}"
        )
    points_at = _resolve_target(meta.foreign_key, final=final)
    if points_at is None:
        return
    if points_at is not model_cls:
        raise TypeError(
            f"{model}.{name}: {other.__name__}.{column} points at "
            f"{points_at.__name__}, not {model}. Write: {fix}"
        )


def check_declared_foreign_keys(model_cls: Any) -> None:
    """Check a new class's foreign-key columns and relations against their targets.

    A target written as a class is checked now; one written as a name is
    queued, and resolution checks it once every model is known.

    Args:
        model_cls: The class being defined, its schema compiled.

    Raises:
        TypeError: A check refuses a declaration; see `check_key_column`,
            `check_forward_relation` and `check_reverse_relation`.
    """
    for name, meta in model_cls.__ferro_fields__.items():
        if meta.foreign_key is None:
            continue
        if isinstance(meta.foreign_key, type):
            check_key_column(model_cls, name, meta, meta.foreign_key)
        else:
            REGISTRY.defer_relation(model_cls.__ferro_identity__, name, meta)
    for name, relation in model_cls.__ferro_relations__.items():
        if isinstance(relation, ForwardRelation) and isinstance(relation.to, type):
            check_forward_relation(model_cls, name, relation, final=False)
        elif isinstance(relation, ReverseRelation) and isinstance(relation.to, type):
            check_reverse_relation(model_cls, name, relation, final=False)


def _resolve_declared_foreign_key(
    model_name: str,
    field_name: str,
    declared: FerroField | ForwardRelation | ReverseRelation,
) -> None:
    """Check one queued foreign-key column or relation; install a reverse one."""
    model_cls = REGISTRY.models()[model_name]
    if isinstance(declared, FerroField):
        target = _resolve_target(declared.foreign_key, final=True)
        check_key_column(model_cls, field_name, declared, target)
    elif isinstance(declared, ForwardRelation):
        check_forward_relation(model_cls, field_name, declared, final=True)
    else:
        check_reverse_relation(model_cls, field_name, declared, final=True)
        other = declared.to
        setattr(
            model_cls,
            field_name,
            RelationshipDescriptor(
                target_model_name=other.__ferro_identity__,
                field_name=field_name,
                fk_column=declared.attribute,
                is_one_to_one=other.__ferro_columns__[declared.attribute].unique,
            ),
        )


_DECLARED_ORDER = {FerroField: 0, ForwardRelation: 1, ReverseRelation: 2}


def resolve_relationships():
    """Bind every declared relation to its target model and check the pair.

    Ferro calls this on `connect()` and before creating tables. Each
    foreign key's `<field>_id` column takes the type of the target's primary
    key, and the affected models are rebuilt and registered again.

    Raises:
        RuntimeError: A relation names a model that is not defined, the
            target has no field named by `related_name`, a many-to-many join
            table's name is another model's table, or a model's schema fails
            to rebuild.
        TypeError: A model's declaration is invalid once its relations are
            known.
    """
    # Drain (copy-and-clear) so that we don't process the same relations
    # multiple times if resolve_relationships is called again (e.g. in tests)
    to_process = REGISTRY.drain_pending_relations()

    recompile_targets: set[str] = set()

    declared: list[tuple[str, str, Any]] = []
    for model_name, field_name, rel in to_process:
        recompile_targets.add(model_name)
        if type(rel) in _DECLARED_ORDER:
            declared.append((model_name, field_name, rel))
            continue
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
                    fk_column=f"{field_name}_id",
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
                join_table = _default_join_table(source_table, field_name)
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

            source_col = _join_column(source_table)
            target_col = _join_column(target_table)
            join_hints = _join_table_rename_hints(
                source_model, target_model, field_name, rel.through
            )

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
            composite_indexes = ((target_col, source_col),) if rel.reverse_index else ()
            register_model_with_ir(
                join_table,
                join_columns,
                join_table,
                composite_uniques=composite_uniques,
                composite_indexes=composite_indexes,
                rename_hints=join_hints,
            )
            REGISTRY.add_join_table(
                join_table,
                {
                    "columns": join_columns,
                    "composite_uniques": composite_uniques,
                    "composite_indexes": composite_indexes,
                    "rename_hints": join_hints,
                },
            )

    # Key columns first, so a relation's check reads a bound column.
    for model_name, field_name, rel in sorted(
        declared, key=lambda entry: _DECLARED_ORDER[type(entry[2])]
    ):
        _resolve_declared_foreign_key(model_name, field_name, rel)

    # Retired relations (ADR-0053) never enter the pending queue: their shadow
    # column compiles from the ForeignKey alone, so each needs its target
    # resolved, a recompile against it, and no BackRef reading it back.
    for model_name, model_cls in REGISTRY.models().items():
        retired = model_cls.__dict__.get("__ferro_retired_relations__") or {}
        for field_name, rel in retired.items():
            if isinstance(rel.to, (str, ForwardRef)):
                to_name = rel.to if isinstance(rel.to, str) else rel.to.__forward_arg__
                target_model = REGISTRY.resolve_reference(to_name, default=None)
                if not target_model:
                    raise RuntimeError(
                        f"Relationship resolution failed: '{to_name}' not found"
                    )
                rel.to = target_model
            refuse_back_ref_over_retired_relation(model_cls, field_name, rel)
            recompile_targets.add(model_name)

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
        except TypeError:
            # Declaration errors are the class-definition contract and surface
            # unwrapped, exactly as the metaclass surfaces them on the first
            # pass. A shadow FK column only learns its real storage here, so a
            # `__ferro_rls__` shorthand over a forward-referenced FK is first
            # provably wrong on THIS pass — and it must still read as the
            # declaration error it is, not as an internal rebuild failure.
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Ferro failed to rebuild the schema for model '{model_name}' "
                f"while resolving relationships: {exc}"
            ) from exc

    REGISTRY.mark_resolved()
    REGISTRY.drain_pending_relations()
