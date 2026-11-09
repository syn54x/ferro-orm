"""Relation resolution: binding each declared relation to its target model."""

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from types import UnionType
from typing import Any, ForwardRef, Union, get_args, get_origin

from pydantic_core import PydanticUndefined

from .. import _core
from .._annotation_utils import annotation_allows_none
from ..base import (
    FerroField,
    ForwardRelation,
    ManyToManyRelation,
    ReverseRelation,
)
from ..columns import (
    ColumnSpec,
    RetiredField,
    fk_column_spec,
    pk_python_type_for_model,
)
from ..fields import annotation_text, field_line, field_options
from ..ir.compiler import (
    RenameHints,
    _column_ir_from_spec,
    compile_model_schema_ir,
    register_model_with_ir,
)
from ..registry import REGISTRY
from .descriptors import RelationshipDescriptor
from .fixes import DeclarationRefusal, Edit


ON_ANOTHER_MODEL: frozenset[str] = frozenset()
"""The paste site of a line pasted on a model other than the refused one. A
builder given it as ``bound`` writes every model by name
(``foreign_key="Team"``, ``Relation[list["Team"]]``), which resolves
whatever order the models are defined in."""

_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")


def is_table_name(name: str) -> bool:
    """Whether ``name`` is a table name ``__ferro_table__`` accepts: an
    identifier of at most 63 characters."""
    return len(name) <= 63 and _TABLE_NAME.match(name) is not None


def _default_join_table(source_table: str, field_name: str) -> str:
    """The join table a fix writes for a ``ManyToMany`` whose ``through=`` it
    replaces: ``<table>_<field>``."""
    return f"{source_table}_{field_name}"


def _join_column(table: str) -> str:
    """The join table's column that points at ``table``."""
    return f"{table}_id"


def _join_table_rename_hints(
    source_model: type,
    target_model: type,
    field_name: str,
    relation: ManyToManyRelation,
) -> RenameHints:
    """What a join table and its columns were called before (ADR-0032).

    The table's previous name is the relation's ``renamed_from``. Its
    columns follow their models' table renames, named by the same function
    that names them now.
    """
    source_prev = getattr(source_model, "__ferro_renamed_from__", None)
    target_prev = getattr(target_model, "__ferro_renamed_from__", None)
    columns: dict[str, str] = {}
    if source_prev is not None:
        columns[_join_column(source_model.__ferro_table__)] = _join_column(source_prev)
    if target_prev is not None:
        columns[_join_column(target_model.__ferro_table__)] = _join_column(target_prev)
    return RenameHints(table=relation.renamed_from, columns=columns)


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


def _free_name(model_cls: Any, name: str) -> str:
    """``name``, or a variant of it no field of ``model_cls`` declares."""
    while name in model_cls.model_fields:
        name += "_ref"
    return name


def _without_none(python_type: Any) -> Any:
    """``python_type`` with ``| None`` set aside."""
    if get_origin(python_type) in (Union, UnionType):
        members = [arg for arg in get_args(python_type) if arg is not type(None)]
        if len(members) == 1:
            return members[0]
    return python_type


def _backs(key: ColumnSpec, column: ColumnSpec) -> bool:
    """Whether a primary key ``key`` backs the foreign-key column ``column``:
    the same type and the same storage."""
    return _scalar(key.python_type) == _scalar(column.python_type) and _storage(
        key
    ) == _storage(column)


Defining = tuple[Any, Mapping[str, ColumnSpec]]
"""A model whose schema is compiling, and the column specs it has not
published yet."""


def _columns_now(model: Any, defining: Defining | None) -> Mapping[str, ColumnSpec]:
    """``model``'s columns as they exist now: its published ones, or the specs
    of the model whose schema is compiling, so a check sees that model at class
    definition exactly as relation resolution sees it later."""
    if defining is not None and model is defining[0]:
        return defining[1]
    return model.__dict__.get("__ferro_columns__") or {}


def _primary_key_edits(
    model_cls: Any, needing: ColumnSpec | None, defining: Defining | None = None
) -> list[Edit]:
    """The edits giving ``model_cls`` a primary key of ``needing``'s type and storage.

    A plain ``id`` of that type and storage is marked the primary key;
    otherwise one is added. Every foreign-key column of a model that exists
    now, the one being defined included, that points at ``model_cls`` and
    the new key would not back is rewritten to it, so following the edits
    loads each of them.

    Args:
        model_cls: The model with no primary key.
        needing: The column the key is for, or `None` when there is none yet;
            then the first column already pointing at the model decides, and
            with none an `int` key is added.
        defining: The model whose schema is compiling and its column specs,
            when the check runs at its class definition. Defaults to `None`.

    Returns:
        The edits, the primary key's first.
    """
    model = model_cls.__name__
    models: list[Any] = list(REGISTRY.models().values())
    pointing = [
        (other, name, _columns_now(other, defining)[name])
        for other in models
        for name in _key_columns_to(other, model_cls)
        if name in _columns_now(other, defining)
    ]
    if needing is None and pointing:
        needing = pointing[0][2]
    plain = model_cls.__ferro_columns__.get("id")
    if plain is not None and (needing is None or _backs(plain, needing)):
        info = model_cls.model_fields["id"]
        options = field_options(model_cls.__ferro_fields__.get("id"), primary_key=True)
        line = field_line("id", annotation_text(info.annotation), options, info.default)
        edits = [Edit(model, "id", "replace", (line,))]
        key = plain
    else:
        key_type = int if needing is None else _without_none(needing.python_type)
        db_type = needing.db_type if needing and needing.db_type_explicit else None
        # The database numbers an integer key, so it starts as None.
        numbered = key_type is int
        written = annotation_text(key_type) + (" | None" if numbered else "")
        name = _free_name(model_cls, "id")
        line = field_line(
            name,
            written,
            field_options(None, primary_key=True, db_type=db_type),
            None if numbered else PydanticUndefined,
        )
        edits = [Edit(model, name, "add", (line,))]
        key = needing
    for other, name, column in pointing:
        if key is None or _backs(key, column):
            continue
        line = key_column_line(
            other,
            name,
            model_cls,
            nullable=annotation_allows_none(other.model_fields[name].annotation),
            meta=other.__ferro_fields__[name],
            key=key,
        )
        edits.append(Edit(other.__name__, name, "replace", (line,)))
    return edits


def _relations_over(model_cls: Any, column: str) -> list[tuple[Any, str, Any]]:
    """Every relation reading ``model_cls``'s ``column``: model, field, record."""
    found = [
        (model_cls, field, relation)
        for field, relation in (
            model_cls.__dict__.get("__ferro_relations__") or {}
        ).items()
        if isinstance(relation, ForwardRelation) and relation.fk_column == column
    ]
    for other in REGISTRY.models().values():
        for field, relation in (
            other.__dict__.get("__ferro_relations__") or {}
        ).items():
            if (
                isinstance(relation, ReverseRelation)
                and relation.attribute == column
                and _resolve_target(relation.to, final=False) is model_cls
            ):
                found.append((other, field, relation))
    return found


def _repoint_edits(model_cls: Any, column: str, new: str) -> list[Edit]:
    """The edits pointing every relation over ``column`` at ``new``."""
    edits = []
    for owner, field, relation in _relations_over(model_cls, column):
        if isinstance(relation, ForwardRelation):
            target = _resolve_target(relation.to, final=False)
            line = relation_line(
                field,
                "Forward",
                target.__name__ if target is not None else annotation_text(relation.to),
                relation.nullable,
                new,
                relation.bound,
            )
        else:
            line = relation_line(
                field, "Reverse", model_cls.__name__, False, new, relation.bound
            )
        edits.append(Edit(owner.__name__, field, "replace", (line,)))
    return edits


def keyless_target_refusal(
    model: str,
    column: str,
    target: Any,
    columns: Mapping[str, ColumnSpec],
    owner: Any = None,
) -> DeclarationRefusal:
    """The refusal of a foreign-key column whose target declares no primary key.

    One refusal for both times the check runs, from the same input: when
    the class is defined, for a target written as a class (the schema
    compile meets it first, before the model publishes its columns), and
    when relations resolve, for one written as a name.

    Args:
        model: The name of the model or join table declaring the column.
        column: The column's name.
        target: The model the column points at.
        columns: Every column spec of the declaring model, published or not.
        owner: The declaring model, or `None` for a join table. Defaults to
            `None`.

    Returns:
        The refusal, its fix the target's new primary key.
    """
    defining = None if owner is None else (owner, columns)
    return DeclarationRefusal(
        model,
        column,
        f"{target.__name__} declares no primary key for a foreign key to point at",
        _primary_key_edits(target, columns.get(column), defining),
    )


def _target_key(model_cls: Any, name: str, target: Any) -> tuple[str, Any]:
    """``target``'s primary-key column and its type, ``| None`` set aside."""
    pk = target.__ferro_pk__
    if pk is None:
        raise keyless_target_refusal(
            model_cls.__name__,
            name,
            target,
            _columns_now(model_cls, None),
            model_cls,
        )
    return pk, _without_none(target.__ferro_columns__[pk].python_type)


def written_type(annotation: Any) -> str:
    """``annotation`` as source text, with ``| None`` set aside."""
    if get_origin(annotation) in (Union, UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        return " | ".join(annotation_text(member) for member in members)
    return annotation_text(annotation)


# Options a foreign-key column cannot take, at the value that leaves them out.
_NOT_ON_A_KEY = {
    option: getattr(FerroField(), option)
    for option in ("primary_key", "autoincrement", "generated", "nullable")
}


def key_column_line(
    model_cls: Any,
    column: str,
    target: Any,
    *,
    nullable: bool,
    meta: FerroField | None = None,
    annotation: str | None = None,
    foreign_key: Any = None,
    bound: frozenset[str] | None = None,
    key: ColumnSpec | None = None,
    **changes: Any,
) -> str:
    """Write a foreign-key column to ``target`` that the key checks accept.

    Every line a refusal writes for a foreign-key column comes from here, so
    the rules `check_key_column` applies decide it: the type is the target
    key's, the storage is the `db_type` the target key declares (none when
    it declares none), nullability comes from the annotation alone, with
    ``on_delete="SET NULL"`` making it nullable, and the options a key
    column cannot take are left out. Every other option ``meta`` declares
    is kept.

    It never writes a primary key: a foreign-key column cannot be one, so a
    fix that would make a primary key a foreign key gives the model another
    primary key first.

    Args:
        model_cls: The model declaring the column, or `None` for a
            line pasted where the refusal names no model.
        column: The column's name.
        target: The model the column points at, resolved.
        nullable: Whether the column allows `None`.
        meta: The column's declared options, if it has any. Defaults to `None`.
        annotation: The column's type as written, `| None` set aside, when a
            check already found it equal to the target key's; otherwise the
            target key's type is written. Defaults to `None`.
        foreign_key: How `foreign_key=` is written. Defaults to the column's
            own, or the target as `bound` decides.
        bound: Where the line is pasted: the names defined where the class
            being defined runs (`None` under postponed annotations), so a
            target defined later is written by name; or `ON_ANOTHER_MODEL`
            for a line pasted on another model, which writes it by name.
        key: The primary key the column is written for, when a fix gives
            ``target`` a new one. Defaults to ``target``'s own.
        **changes: Further options to set, as `field_options` takes them.

    Returns:
        The declaration, as one line of source.

    Raises:
        ValueError: ``column`` is ``model_cls``'s primary key, which no
            refusal may write as a foreign key.
    """
    if model_cls is not None and column == model_cls.__ferro_pk__:
        raise ValueError(
            f"ferro: a refusal tried to write {model_cls.__name__}.{column}, a "
            "primary key, as a foreign-key column"
        )
    if key is None:
        pk, key_type = _target_key(model_cls or target, column, target)
        key = target.__ferro_columns__[pk]
    else:
        key_type = _without_none(key.python_type)
    if foreign_key is None:
        foreign_key = (
            meta.foreign_key
            if meta is not None and meta.foreign_key is not None
            else _foreign_key_option(model_cls, target, bound)
        )
    options = field_options(
        meta,
        **{
            **_NOT_ON_A_KEY,
            "foreign_key": foreign_key,
            "db_type": key.db_type if key.db_type_explicit else None,
            **changes,
        },
    )
    nullable = nullable or options.get("on_delete") == "SET NULL"
    written = (annotation or annotation_text(key_type)) + (
        " | None" if nullable else ""
    )
    return field_line(column, written, options, None if nullable else PydanticUndefined)


def check_key_column(model_cls: Any, name: str, meta: FerroField, target: Any) -> None:
    """Refuse a foreign-key column its target key does not back.

    Args:
        model_cls: The model declaring the column.
        name: The column's name.
        meta: The column's options.
        target: The model the column points at.

    Raises:
        TypeError: The column is the primary key or generated; it takes
            `on_delete="SET NULL"` without allowing `None`; its type, with
            `| None` set aside and a `NewType` read as its supertype, is not
            the target key's type; or it stores differently from the target
            key. Each names the line `key_column_line` writes.
    """
    info = model_cls.model_fields.get(name)
    if info is None:
        return  # retired: no field to check, its foreign key resolved above
    model = model_cls.__name__
    pk, key_type = _target_key(model_cls, name, target)
    nullable = annotation_allows_none(info.annotation)
    if meta.primary_key:
        reason = "a foreign-key column cannot be the primary key"
        if name == "id":
            # Keep `id` the primary key; the foreign key moves to its own column.
            new = _free_name(model_cls, f"{_snake(target.__name__)}_id")
            key_line = key_column_line(
                model_cls, new, target, nullable=False, meta=meta, unique=True
            )
            pk_line = field_line(
                name,
                annotation_text(info.annotation),
                field_options(meta, foreign_key=None, on_delete=None),
                info.default,
            )
            raise DeclarationRefusal(
                model,
                name,
                reason,
                [
                    Edit(model, new, "add", (key_line,)),
                    Edit(model, name, "replace", (pk_line,)),
                    *_repoint_edits(model_cls, name, new),
                ],
            )
        # The column stops being the primary key, so the model needs another,
        # of the same type: every key pointing at the model still fits it.
        line = key_column_line(
            None, name, target, nullable=nullable, meta=meta, unique=True
        )
        raise DeclarationRefusal(
            model,
            name,
            reason,
            [
                *_primary_key_edits(model_cls, model_cls.__ferro_columns__[name]),
                Edit(model, name, "replace", (line,)),
            ],
        )
    if meta.generated is not None:
        line = key_column_line(model_cls, name, target, nullable=nullable, meta=meta)
        raise DeclarationRefusal(
            model,
            name,
            "a foreign-key column cannot be generated",
            [Edit(model, name, "replace", (line,))],
        )
    if meta.on_delete == "SET NULL" and not nullable:
        line = key_column_line(model_cls, name, target, nullable=True, meta=meta)
        # A forward relation reading the column becomes nullable with it.
        readers = [
            Edit(
                model,
                field,
                "replace",
                (
                    relation_line(
                        field, "Forward", target.__name__, True, name, relation.bound
                    ),
                ),
            )
            for _, field, relation in _relations_over(model_cls, name)
            if isinstance(relation, ForwardRelation) and not relation.nullable
        ]
        raise DeclarationRefusal(
            model,
            name,
            'on_delete="SET NULL" needs a nullable column',
            [Edit(model, name, "replace", (line,)), *readers],
        )
    if _scalar(info.annotation) != _scalar(key_type):
        key_text = annotation_text(key_type)
        line = key_column_line(model_cls, name, target, nullable=nullable, meta=meta)
        raise DeclarationRefusal(
            model,
            name,
            f"{target.__name__}.{pk} is {key_text}, so its foreign-key column is "
            f"{key_text} too",
            [Edit(model, name, "replace", (line,))],
        )
    column = _storage(model_cls.__ferro_columns__[name])
    key = _storage(target.__ferro_columns__[pk])
    for dialect in key:
        if column[dialect] != key[dialect]:
            line = key_column_line(
                model_cls,
                name,
                target,
                nullable=nullable,
                meta=meta,
                annotation=written_type(info.annotation),
            )
            raise DeclarationRefusal(
                model,
                name,
                f"{target.__name__}.{pk} is stored as {key[dialect]} and this "
                f"column as {column[dialect]}",
                [Edit(model, name, "replace", (line,))],
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
    """How ``model_cls`` writes ``foreign_key=``: the class where the paste
    site is known to define it, its name everywhere else (a self reference,
    a later model, another model's paste site). ``foreign_key=`` is a value,
    evaluated when the class body runs even under postponed annotations, so
    ``bound=None`` binds nothing here."""
    defined = bound is not None and target.__name__ in bound
    return target if defined and target is not model_cls else target.__name__


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
        bound: Where the line is pasted: the names defined where the class
            being defined runs, or ``None`` under postponed annotations; or
            `ON_ANOTHER_MODEL` for a line pasted on another model, whose
            target is always written by name.

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


SELF_MANY_TO_MANY = "a self-referential many-to-many is not supported"
BAD_THROUGH = (
    "a join table's name is a table name: letters, digits and underscores, "
    "not starting with a digit, at most 63 characters"
)


def _tables_taken(owner: Any, field_name: str) -> set[str]:
    """Every table name a declaration other than ``owner.field_name`` holds:
    each model's table, each registered join table, and the join table every
    other many-to-many names."""
    taken = set(REGISTRY.join_tables())
    for model in REGISTRY.models().values():
        taken.add(getattr(model, "__ferro_table__", ""))
        for name, relation in (model.__dict__.get("__ferro_relations__") or {}).items():
            if (
                isinstance(relation, ManyToManyRelation)
                and relation.through
                and (model, name) != (owner, field_name)
            ):
                taken.add(relation.through)
    return taken


def _free_table_name(name: str, taken: set[str]) -> str:
    """``name`` as a table name ``taken`` does not hold, the way `_free_name`
    picks a field name: cut to 63 characters, then ``_2``, ``_3`` and on
    until it is free. ``name`` joins identifiers, so only its length can be
    wrong."""
    free, number = name[:63], 1
    while free in taken:
        number += 1
        suffix = f"_{number}"
        free = name[: 63 - len(suffix)] + suffix
    return free


def _many_to_many_line(
    field_name: str,
    target: str,
    through: str,
    reverse_index: bool,
    bound: frozenset[str] | None,
    renamed_from: str | None = None,
) -> str:
    """Write a many-to-many: ``tags: Relation[list[Tag]] = ManyToMany(through="post_tags")``.

    A ``renamed_from`` the line could not keep (see `_renamed_from_reason`)
    is left out, so the line is one Ferro accepts.
    """
    options = [f"through={json.dumps(through)}"]
    if renamed_from is not None and _renamed_from_reason(renamed_from, through) is None:
        options.append(f"renamed_from={json.dumps(renamed_from)}")
    if not reverse_index:
        options.append("reverse_index=False")
    shown = f'"{target}"' if bound is not None and target not in bound else target
    return f"{field_name}: Relation[list[{shown}]] = ManyToMany({', '.join(options)})"


def _renamed_from_reason(renamed_from: str, through: str) -> str | None:
    """Why a join table's ``renamed_from`` cannot be its previous name, or ``None``."""
    if not is_table_name(renamed_from):
        return (
            "renamed_from is the join table's previous name, a table name: letters, "
            "digits and underscores, not starting with a digit, at most 63 characters"
        )
    if renamed_from == through:
        return (
            f"renamed_from is the join table's previous name, and {through} is the "
            "name through= gives it now"
        )
    return None


def check_join_table_rename(
    model_cls: Any, name: str, relation: ManyToManyRelation
) -> None:
    """Refuse a many-to-many's ``renamed_from`` that cannot be its join table's
    previous name: one that is no table name, or the name ``through=`` gives
    the table now. A name another declaration holds is the planner's to
    refuse, as for every table hint.

    Raises:
        DeclarationRefusal: The hint cannot be meant; the fix drops it.
    """
    if relation.renamed_from is None:
        return
    reason = _renamed_from_reason(relation.renamed_from, relation.through)
    if reason is None:
        return
    line = _many_to_many_line(
        name,
        annotation_text(relation.to),
        relation.through,
        relation.reverse_index,
        relation.bound,
    )
    model = model_cls.__name__
    raise DeclarationRefusal(
        model, name, reason, [Edit(model, name, "replace", (line,))]
    )


def many_to_many_refusal(
    owner: Any,
    field_name: str,
    reason: str,
    targets: list[str],
    through: str,
    reverse_index: bool,
    bound: frozenset[str] | None,
    renamed_from: str | None = None,
) -> DeclarationRefusal:
    """The refusal whose fix is a many-to-many on ``owner`` linking one of ``targets``.

    Every refusal that writes a many-to-many builds it here. A target that
    names ``owner`` itself is checked first, whatever else is wrong: both
    join-table columns would be ``<table>_id``, so the fix declares a join
    model of its own. Otherwise each target is a way to fix it, the local
    line ``tags: Relation[list[Tag]] = ManyToMany(through="post_tags")``;
    a ``Reverse`` on another of ``targets`` that reads the relation then
    declares a many-to-many of its own. The join table is named so that no
    other declaration holds it.

    Args:
        owner: The model the many-to-many is declared on.
        field_name: The relation's field name.
        reason: What is wrong with the declaration as written.
        targets: The models the many-to-many could link, by name.
        through: The join table's name: the declared one, or
            ``<table>_<field>`` when that is no table name.
        reverse_index: Whether the join table keeps its reverse index.
        bound: The names defined where ``owner``'s class statement runs, or
            ``None`` under postponed annotations; a target outside them is
            written by name.
        renamed_from: The join table's previous name the declaration gives,
            kept on the line when it can be. Defaults to `None`.

    Returns:
        The refusal, carrying one way to fix it per target.
    """
    model = owner.__name__
    taken = _tables_taken(owner, field_name)
    through = _free_table_name(through, taken)
    if model in targets:
        column = _join_column(owner.__ferro_table__)
        join_model = model + "".join(
            part.capitalize() for part in field_name.split("_")
        )
        readers = [
            Edit(
                model,
                reader,
                "replace",
                (
                    relation_line(
                        reader, "Reverse", join_model, False, column, relation.bound
                    ),
                ),
            )
            for reader, relation in (
                owner.__dict__.get("__ferro_relations__") or {}
            ).items()
            if isinstance(relation, ReverseRelation)
            and relation.attribute == field_name
        ]
        join = (
            f"__ferro_table__ = {json.dumps(through)}",
            "id: int | None = Field(default=None, primary_key=True)",
            *(
                key_column_line(None, name, owner, nullable=False)
                for name in (column, f"other_{column}")
            ),
        )
        return DeclarationRefusal(
            model,
            field_name,
            f"{SELF_MANY_TO_MANY}, since both join-table columns would be {column}",
            [
                Edit(join_model, "", "add", join),
                Edit(model, field_name, "remove"),
                *readers,
            ],
        )
    ways = []
    for target in targets:
        line = _many_to_many_line(
            field_name,
            target,
            through,
            reverse_index,
            bound,
            renamed_from=renamed_from,
        )
        way = [Edit(model, field_name, "replace", (line,))]
        claimed = taken | {through}
        others: list[Any] = list(REGISTRY.models().values())
        for other in others:
            if other.__name__ == target or other.__name__ not in targets:
                continue
            for reader, relation in (
                other.__dict__.get("__ferro_relations__") or {}
            ).items():
                if (
                    isinstance(relation, ReverseRelation)
                    and relation.attribute == field_name
                    and _resolve_target(relation.to, final=False) is owner
                ):
                    own = _free_table_name(f"{other.__ferro_table__}_{reader}", claimed)
                    claimed.add(own)
                    line = _many_to_many_line(
                        reader, model, own, True, ON_ANOTHER_MODEL
                    )
                    way.append(Edit(other.__name__, reader, "replace", (line,)))
        ways.append(way)
    return DeclarationRefusal(model, field_name, reason, alternatives=ways)


def _many_to_many_to(model_cls: Any, target: Any) -> list[str]:
    """``model_cls``'s many-to-many relations that link ``target``; never its own
    model, which a many-to-many cannot link."""
    if model_cls is target:
        return []
    return [
        name
        for name, relation in (
            model_cls.__dict__.get("__ferro_relations__") or {}
        ).items()
        if isinstance(relation, ManyToManyRelation)
        and relation.refusal is None
        and _resolve_target(relation.to, final=False) is target
    ]


def _refuse_unnamed_many_to_many(
    model_cls: Any, name: str, relation: ManyToManyRelation
) -> None:
    """Raise a bare ``Relation`` refusal, naming the model a ``Reverse`` reads it from."""
    model = model_cls.__name__
    naming = [
        other.__name__
        for other in REGISTRY.models().values()
        for reverse in (other.__dict__.get("__ferro_relations__") or {}).values()
        if isinstance(reverse, ReverseRelation)
        and reverse.attribute == name
        and reverse.refusal is None
        and _resolve_target(reverse.to, final=False) is model_cls
    ]
    if naming:
        raise many_to_many_refusal(
            model_cls,
            name,
            relation.refusal or "",
            naming,
            relation.through,
            relation.reverse_index,
            relation.bound,
            relation.renamed_from,
        )
    # No edit can be written: nothing names the model the relation links.
    raise DeclarationRefusal(
        model,
        name,
        f"{relation.refusal}, and nothing names the model it links. Annotate "
        f"{name} as Relation[list[...]] of that model, keeping "
        f'ManyToMany(through="{relation.through}")',
    )


def relation_refusal(
    model: str,
    field_name: str,
    reason: str,
    spelling: str,
    column: str,
    target: str,
    nullable: bool,
    bound: frozenset[str] | None = None,
) -> DeclarationRefusal:
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
        The refusal, its fix the line to write in place of the relation.
    """
    line = relation_line(field_name, spelling, target, nullable, column, bound)
    return DeclarationRefusal(
        model, field_name, reason, [Edit(model, field_name, "replace", (line,))]
    )


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
    # No edit can be written: nothing names the model the relation reads.
    raise DeclarationRefusal(
        model,
        name,
        f"{relation.refusal}, and {missing} to name the model it reads. Declare "
        f"{column} with Field(foreign_key=...) naming that model, and annotate "
        f"{name} as Relation[...] of the same model",
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
        if (
            (meta := (other.__dict__.get("__ferro_fields__") or {}).get(column))
            is not None
            and meta.foreign_key is not None
            and _resolve_target(meta.foreign_key, final=False) is model_cls
        )
        or column in _many_to_many_to(other, model_cls)
    ]
    if pointing_back:
        # One way to fix it per model whose column points back.
        names = [other.__name__ for other in pointing_back]
        reason = relation.refusal or ""
        if len(names) > 1:
            reason += (
                f", and {_listed(names)} each declare {column} pointing at {model}"
            )
        raise DeclarationRefusal(
            model,
            name,
            reason,
            alternatives=[
                [
                    Edit(
                        model,
                        name,
                        "replace",
                        (
                            relation_line(
                                name, "Reverse", other, False, column, relation.bound
                            ),
                        ),
                    )
                ]
                for other in names
            ],
        )
    # No edit can be written: no model is the one the relation reads.
    key = key_column_line(None, column, model_cls, nullable=False)
    raise DeclarationRefusal(
        model,
        name,
        f"{relation.refusal}, and no model declares a foreign-key column {column} "
        f"pointing at {model}. Declare {key} on the model the relation reads, and "
        f"annotate {name} as Relation[list[...]] of that model",
    )


def _listed(names: list[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``."""
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]


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
        raise DeclarationRefusal(
            model,
            name,
            f"{column} is retired, so the relation is retired with it",
            [Edit(model, name, "remove")],
        )

    def forward(points_at: Any, nullable: bool, over: str) -> Edit:
        """This relation, rewritten in place to read ``over``."""
        line = relation_line(
            name, "Forward", points_at.__name__, nullable, over, relation.bound
        )
        return Edit(model, name, "replace", (line,))

    if info is None:
        keys = _key_columns_to(model_cls, target)
        if len(keys) == 1:
            nullable = annotation_allows_none(
                model_cls.model_fields[keys[0]].annotation
            )
            edits = [forward(target, nullable, keys[0])]
        else:
            line = key_column_line(
                model_cls,
                column,
                target,
                nullable=relation.nullable,
                bound=relation.bound,
            )
            edits = [Edit(model, column, "add", (line,))]
        raise DeclarationRefusal(model, name, f"{model} has no column {column}", edits)
    if meta is None or meta.foreign_key is None:
        if column == model_cls.__ferro_pk__:
            new = _free_name(model_cls, f"{_snake(target.__name__)}_id")
            key_line = key_column_line(
                model_cls, new, target, nullable=relation.nullable, bound=relation.bound
            )
            raise DeclarationRefusal(
                model,
                name,
                f"{column} is {model}'s primary key, so it cannot be the "
                "relation's foreign-key column",
                [
                    Edit(model, new, "add", (key_line,)),
                    forward(target, relation.nullable, new),
                ],
            )
        # The column takes the relation's nullability, so both lines agree.
        line = key_column_line(
            model_cls, column, target, nullable=relation.nullable, meta=meta
        )
        raise DeclarationRefusal(
            model,
            name,
            f"{column} has no foreign_key",
            [Edit(model, column, "replace", (line,))],
        )
    points_at = _resolve_target(meta.foreign_key, final=final)
    if points_at is None:
        return
    nullable = annotation_allows_none(info.annotation)
    if points_at is not target:
        raise DeclarationRefusal(
            model,
            name,
            f"{column} points at {points_at.__name__}, not {target.__name__}",
            [forward(points_at, nullable, column)],
        )
    if nullable != relation.nullable:
        raise DeclarationRefusal(
            model,
            name,
            f"{column} is {'nullable' if nullable else 'required'}, so the "
            "relation is too",
            [forward(target, nullable, column)],
        )


def _retired_fields(model_cls: Any) -> dict[str, RetiredField]:
    """The model's retired fields, whose columns a relation can no longer read."""
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
    linked = (other.__dict__.get("__ferro_relations__") or {}).get(column)
    if isinstance(linked, ManyToManyRelation):
        _check_reverse_many_to_many(model_cls, name, relation, linked, final=final)
        return
    if column in _retired_fields(other):
        raise DeclarationRefusal(
            model,
            name,
            f"{other.__name__}.{column} is retired, so the relation is retired with it",
            [Edit(model, name, "remove")],
        )

    def reverse(over: str) -> Edit:
        """This relation, rewritten in place to read ``other``'s ``over``."""
        line = relation_line(
            name, "Reverse", other.__name__, False, over, relation.bound
        )
        return Edit(model, name, "replace", (line,))

    keys = _key_columns_to(other, model_cls)
    info = other.model_fields.get(column)
    meta = other.__ferro_fields__.get(column)
    if info is None:
        reason = f"{other.__name__} has no column {column}"
    elif meta is None or meta.foreign_key is None:
        reason = f"{other.__name__}.{column} has no foreign_key"
    else:
        points_at = _resolve_target(meta.foreign_key, final=final)
        if points_at is None or points_at is model_cls:
            return
        reason = (
            f"{other.__name__}.{column} points at {points_at.__name__}, not {model}"
        )
    if len(keys) == 1:
        raise DeclarationRefusal(model, name, reason, [reverse(keys[0])])
    if info is not None and (meta is None or meta.foreign_key is None):
        if column == other.__ferro_pk__:
            reason += f", and it is {other.__name__}'s primary key"
        else:
            line = key_column_line(
                other,
                column,
                model_cls,
                nullable=annotation_allows_none(info.annotation),
                meta=meta,
            )
            raise DeclarationRefusal(
                model,
                name,
                reason,
                [Edit(other.__name__, column, "replace", (line,))],
            )
    # A new key column on the other model, and this relation over it. Pasted
    # in this model's own class, the key column is bound where the relation is.
    new = column if info is None else _free_name(other, f"{_snake(model)}_id")
    key_line = key_column_line(
        other,
        new,
        model_cls,
        nullable=False,
        bound=relation.bound if other is model_cls else None,
    )
    edits = [Edit(other.__name__, new, "add", (key_line,))]
    if new != column:
        edits.append(reverse(new))
    raise DeclarationRefusal(model, name, reason, edits)


def _check_reverse_many_to_many(
    model_cls: Any,
    name: str,
    relation: ReverseRelation,
    linked: ManyToManyRelation,
    *,
    final: bool,
) -> None:
    """Refuse a reverse relation naming a many-to-many that links another model.

    The fix reads back the one relation of the other model that does link
    this one; with none, or several, it declares this side's own
    many-to-many, whose join table is named the way a conversion names it.
    On the many-to-many's own model, which no many-to-many can link, it
    reads back a self-referencing foreign-key column instead, or moves the
    relation to the model the many-to-many links.
    """
    if linked.refusal is not None:
        return  # the many-to-many's own refusal raises at resolution
    other = relation.to
    points_at = _resolve_target(linked.to, final=final)
    if points_at is None or points_at is model_cls:
        return
    refused = (
        f"{other.__name__}.{relation.attribute} points at {points_at.__name__}, "
        f"not {model_cls.__name__}"
    )
    candidates = _key_columns_to(other, model_cls) + _many_to_many_to(other, model_cls)
    model = model_cls.__name__
    if len(candidates) == 1 or (other is model_cls and candidates):
        # One way to fix it per relation of the other model that links this one.
        raise DeclarationRefusal(
            model,
            name,
            refused,
            alternatives=[
                [
                    Edit(
                        model,
                        name,
                        "replace",
                        (
                            relation_line(
                                name,
                                "Reverse",
                                other.__name__,
                                False,
                                key,
                                relation.bound,
                            ),
                        ),
                    )
                ]
                for key in candidates
            ],
        )
    if other is model_cls:
        # No line on this model works: the relation moves to the linked model.
        moved = relation_line(
            name, "Reverse", other.__name__, False, relation.attribute, ON_ANOTHER_MODEL
        )
        raise DeclarationRefusal(
            model,
            name,
            f"{refused}, and no foreign-key column of {model} points at {model} "
            "for it to read, since a self-referential many-to-many is not supported",
            [
                Edit(model, name, "remove"),
                Edit(points_at.__name__, name, "add", (moved,)),
            ],
        )
    raise many_to_many_refusal(
        model_cls,
        name,
        refused,
        [other.__name__],
        f"{model_cls.__ferro_table__}_{name}",
        True,
        relation.bound,
    )


def check_declared_foreign_keys(model_cls: Any) -> None:
    """Check a new class's foreign-key columns and relations against their targets.

    A target written as a class is checked now; one written as a name is
    queued, and resolution checks it once every model is known.

    Args:
        model_cls: The class being defined, its schema compiled.

    Raises:
        TypeError: A check refuses a declaration; see `many_to_many_refusal`,
            `check_key_column`, `check_forward_relation` and
            `check_reverse_relation`.
    """
    for name, relation in model_cls.__ferro_relations__.items():
        # A many-to-many the class body refused, or one naming this model,
        # whose target the class body names; a bare one waits for resolution.
        if isinstance(relation, ManyToManyRelation) and relation.to is not None:
            target = annotation_text(relation.to)
            if relation.refusal is not None or target == model_cls.__name__:
                raise many_to_many_refusal(
                    model_cls,
                    name,
                    relation.refusal or SELF_MANY_TO_MANY,
                    [target],
                    relation.through,
                    relation.reverse_index,
                    relation.bound,
                    relation.renamed_from,
                )
            check_join_table_rename(model_cls, name, relation)
    # A retired key column keeps its foreign key, so its target resolves too.
    key_columns = {
        **model_cls.__ferro_fields__,
        **{name: field.options for name, field in _retired_fields(model_cls).items()},
    }
    for name, meta in key_columns.items():
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
        linked = getattr(other, declared.attribute, None)
        if isinstance(linked, RelationshipDescriptor) and linked.is_m2m:
            # The many-to-many's own join table, read from the other end.
            descriptor = RelationshipDescriptor(
                target_model_name=other.__ferro_identity__,
                field_name=field_name,
                is_m2m=True,
                join_table=linked.join_table,
                source_col=linked.target_col,
                target_col=linked.source_col,
            )
        else:
            descriptor = RelationshipDescriptor(
                target_model_name=other.__ferro_identity__,
                field_name=field_name,
                fk_column=declared.attribute,
                is_one_to_one=other.__ferro_columns__[declared.attribute].unique,
            )
        setattr(model_cls, field_name, descriptor)


_DECLARED_ORDER = {FerroField: 0, ForwardRelation: 1, ReverseRelation: 2}


def _refuse_taken_join_table(
    source_model: Any,
    field_name: str,
    relation: ManyToManyRelation,
    target_model: Any,
    join_table: str,
) -> None:
    """Refuse a many-to-many whose join table is a model's table, or one
    another many-to-many declares.

    A join table over a model's table would write link rows into that
    model's rows, and two relations over one join table would read and write
    each other's rows, so the declaration is refused, its fix a join table
    no other declaration holds.
    """
    owner = next(
        (
            model.__name__
            for model in REGISTRY.models().values()
            if getattr(model, "__ferro_table__", None) == join_table
        ),
        None,
    )
    held = REGISTRY.join_tables().get(join_table)
    declared_by = (source_model.__ferro_identity__, field_name)
    if owner is not None:
        reason = (
            f"through={json.dumps(join_table)} names the table of the model "
            f"{owner}, and a join table needs a table of its own"
        )
    elif isinstance(held, dict) and held.get("declared_by") not in (
        None,
        declared_by,
    ):
        holder, holder_field = held["declared_by"]
        holder_name = REGISTRY.models()[holder].__name__
        reason = (
            f"{holder_name}.{holder_field} already declares the join table "
            f"{join_table}, and two relations over one join table share their rows"
        )
    else:
        return
    through = _free_table_name(
        _default_join_table(source_model.__ferro_table__, field_name),
        _tables_taken(source_model, field_name) | {join_table},
    )
    line = _many_to_many_line(
        field_name,
        target_model.__name__,
        through,
        relation.reverse_index,
        relation.bound,
        relation.renamed_from,
    )
    model = source_model.__name__
    raise DeclarationRefusal(
        model, field_name, reason, [Edit(model, field_name, "replace", (line,))]
    )


def resolve_relationships():
    """Bind every declared relation to its target model and check the pair.

    Ferro calls this on `connect()` and before creating tables. Each
    foreign-key column and relation is checked against its target, each
    many-to-many registers its join table, and the affected models are
    compiled and registered again.

    Raises:
        RuntimeError: A relation names a model that is not defined, a
            many-to-many join table's name is another model's table, or a
            model's schema fails to rebuild.
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
        # Everything else queued is a many-to-many.
        if rel.refusal is not None:
            _refuse_unnamed_many_to_many(REGISTRY.models()[model_name], field_name, rel)
        if isinstance(rel.to, (str, ForwardRef)):
            to_name = rel.to if isinstance(rel.to, str) else rel.to.__forward_arg__
            target_model = REGISTRY.resolve_reference(to_name, default=None)
            if not target_model:
                raise RuntimeError(
                    f"Relationship resolution failed: '{to_name}' not found"
                )
            rel.to = target_model

        target_model = rel.to
        if target_model is REGISTRY.models()[model_name]:
            # The class body names a target by the name its model is
            # registered under, so class definition already refused this.
            raise RuntimeError(
                f"ferro: {model_name}.{field_name} is a many-to-many linking its "
                "own model, which class definition should have refused"
            )
        recompile_targets.add(target_model.__ferro_identity__)
        source_model = REGISTRY.models()[model_name]
        source_table = source_model.__ferro_table__
        target_table = target_model.__ferro_table__
        join_table = rel.through

        _refuse_taken_join_table(
            source_model, field_name, rel, target_model, join_table
        )

        source_col = _join_column(source_table)
        target_col = _join_column(target_table)
        join_hints = _join_table_rename_hints(
            source_model, target_model, field_name, rel
        )

        # The declaring side reads the relation; a `Reverse` on the target,
        # if any, reads it back once the queued declarations resolve below.
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

        join_columns = (
            fk_column_spec(
                source_col,
                python_type=pk_python_type_for_model(source_model),
                to_table=source_table,
            ),
            fk_column_spec(
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
                "declared_by": (model_name, field_name),
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

    # Second pass: rebuild specs only for models whose schema changed during
    # resolution (relationship sources and targets). Replacement, never
    # mutation (grilling Q3).
    for model_name in sorted(recompile_targets):
        model_cls = REGISTRY.models().get(model_name)
        if model_cls is None:
            continue
        try:
            compile_model_schema_ir(model_name, model_cls)
        except TypeError:
            # Declaration errors are the class-definition contract and surface
            # unwrapped, exactly as the metaclass surfaces them on the first
            # pass, never as an internal rebuild failure.
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Ferro failed to rebuild the schema for model '{model_name}' "
                f"while resolving relationships: {exc}"
            ) from exc

    REGISTRY.mark_resolved()
    REGISTRY.drain_pending_relations()
