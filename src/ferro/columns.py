"""Column specs: each column fact derived exactly once.

One frozen ``ColumnSpec`` per column, built from the field declaration at
class-body time (provisional) and replaced during relationship resolution
(resolved). The SchemaIR compiler renders specs; runtime consumers read
``cls.__ferro_columns__`` at use time.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, ForwardRef, Union, get_args, get_origin, get_type_hints
from uuid import UUID

from pydantic import BaseModel, SecretBytes, SecretStr
from pydantic_core import PydanticUndefined, to_json

from ._annotation_utils import (
    annotation_allows_none,
    annotation_is_decimal,
    enum_subclass_from_annotation,
    validate_db_type_declaration,
    _is_json_family,
    _strip_optional_and_annotated,
)
from ._shadow_fk_types import _scalar_part_of_annotation
from .base import ForeignKey, foreign_key_allows_none

__all__ = [
    "ColumnSpec",
    "ForeignKeyRef",
    "RelationSpec",
    "ReverseSpec",
    "build_column_specs",
    "build_relation_specs",
    "build_reverse_specs",
    "fk_shadow_spec",
    "pk_spec",
    "primary_key_field_name",
    "refuse_back_ref_over_retired_relation",
    "retired_clause",
    "retired_column_specs",
]


def primary_key_field_name(model_name: str, specs: dict[str, ColumnSpec]) -> str | None:
    """Derive the model's single PK field name from its column specs.

    The one derivation site for "which column is the primary key" — cached on
    the class as ``__ferro_pk__`` at the compile choke point, so every
    consumer (save, descriptors, shadow-FK typing, traversal) reads the cached
    fact instead of re-looping specs. At most one ``primary_key=True`` column
    is legal; zero is a valid declaration (``None``) and the operations that
    need a PK raise their own errors.

    Args:
        model_name: The model's name, for the error message.
        specs: The model's column specs, keyed by column name.

    Returns:
        The primary key's field name, or `None` when no column is one.

    Raises:
        TypeError: More than one column declares `primary_key=True`. It is
            raised at class definition, naming the model and the fields,
            rather than settling the ambiguity by declaration order;
            `TypeError` is the declaration error the metaclass surfaces
            unwrapped.
    """
    pks = [name for name, spec in specs.items() if spec.primary_key]
    if len(pks) > 1:
        raise TypeError(
            f"Model {model_name!r} declares multiple primary-key columns: "
            f"{sorted(pks)!r}. At most one primary_key=True column is supported."
        )
    return pks[0] if pks else None


@dataclass(frozen=True, slots=True)
class ForeignKeyRef:
    """The target of a column's foreign key."""

    to_table: str
    """The referenced table's name."""
    on_delete: str | None
    """The `ON DELETE` action, such as `"CASCADE"`, or `None` for the database's default."""


@dataclass(frozen=True, slots=True)
class RelationSpec:
    """One declared forward foreign-key relation's traversal facts.

    Built alongside `ColumnSpec` at the same compile choke point
    (`compile_model_schema_ir`) and exposed as
    `cls.__ferro_relation_specs__`. Relation-traversal query planning
    resolves an attribute access like `post.author` to its target model and
    shadow foreign-key column through this lookup, hop by hop.
    """

    field_name: str
    """The relation field's name on the declaring model."""
    target: type
    """The model the relation points at."""
    shadow_column: str
    """The `<field>_id` column holding the target's primary key."""
    nullable: bool
    """Whether the shadow column is nullable."""


@dataclass(frozen=True, slots=True)
class ReverseSpec:
    """One reverse (`BackRef` or many-to-many) relation's existence-test facts.

    Built beside `RelationSpec` at the same compile choke point and exposed
    as `cls.__ferro_reverse_specs__`. A reverse or many-to-many relation
    supports one predicate form, the existence test
    `author.posts.exists()`, so the spec carries only what a correlated
    `EXISTS` needs: the related model and the correlation columns (the
    child-side foreign key for a reverse relation, the join-table triple for
    a many-to-many one). Reverse relations are tested, never traversed;
    traversal facts stay on `RelationSpec`.
    """

    field_name: str
    """The relation field's name on this model."""
    target: type
    """The related model, which the inner lambda's parameter resolves against.

    It is the child that declares the foreign key for a reverse relation,
    or the far side of a many-to-many one.
    """
    child_fk_column: str | None
    """The child table's foreign-key column the `EXISTS` correlates on; `None` for many-to-many."""
    is_one_to_one: bool = False
    """Whether the child's foreign key is `unique=True`, a one-to-one `BackRef`.

    The rendering is the same correlated `EXISTS` at every cardinality; this
    is a compile-side fact, not a render switch.
    """
    is_m2m: bool = False
    """Whether the test correlates through a join table (two hops) instead of a child's foreign key."""
    join_table: str | None = None
    """Many-to-many only: the join table's name."""
    source_col: str | None = None
    """Many-to-many only: the join table's column referencing this side."""
    target_col: str | None = None
    """Many-to-many only: the join table's column referencing the target side."""


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """Every fact about one column, derived once from the field declaration."""

    name: str
    """The column's name."""
    logical_type: str
    """The JSON-schema type the column's storage is derived from, such as `"integer"`."""
    nullable: bool
    """Whether the column accepts `NULL`."""
    primary_key: bool
    """Whether the column is the table's primary key."""
    autoincrement: bool
    """Whether the database generates the column's values."""
    unique: bool
    """Whether the column has a single-column unique constraint."""
    index: bool
    """Whether the column has a single-column index."""
    default: Any
    """The JSON-schema default, or `None` when the field has none."""
    format: str | None
    """The JSON-schema format, such as `"uuid"` or `"date-time"`, or `None`."""
    python_type: Any
    """The field's Python type, with `Annotated` stripped.

    Producers differ: `build_column_specs` stores the stripped annotation,
    which may still be a union (`int | None` for an optional primary key);
    `pk_spec` and `fk_shadow_spec` store the bare scalar. A consumer that
    needs the scalar unwraps it (`_scalar_part_of_annotation`, or union
    handling).
    """
    enum_values: tuple[Any, ...] | None = None
    """An enum column's values, or `None`."""
    enum_type_name: str | None = None
    """An enum column's Postgres type name, or `None`."""
    enum_class: type[Enum] | None = None
    """An enum column's `Enum` class, or `None`."""
    secret_class: type[SecretStr] | type[SecretBytes] | None = None
    """The `SecretStr` or `SecretBytes` class of a secret column, or `None`: the column stores the raw value and hydrates it wrapped in this class again."""
    db_type: str | None = None
    """The column's SQL type token, or `None` for the derived type."""
    db_type_explicit: bool = False
    """Whether `db_type` was declared rather than derived."""
    db_check: bool = False
    """Whether the column has a `CHECK` constraint on an enum's values."""
    foreign_key: ForeignKeyRef | None = None
    """The column's foreign key, or `None`."""
    retired: bool = False
    """Whether the column is retired.

    A retired column stays in the schema snapshot, nullable, and is never in
    `__ferro_columns__`, the mapped columns the model reads and writes.
    """


def _derive_autoincrement(
    explicit: bool | None, primary_key: bool, integer_typed: bool
) -> bool:
    """Single derivation rule for autoincrement (ADR-0002).

    Explicit declaration wins; otherwise a primary key auto-increments iff it
    is integer-typed. Supersedes the raw-path ``pk``-only fallback from #153.
    """
    if explicit is not None:
        return explicit
    return primary_key and integer_typed


def _derive_nullable(
    *,
    explicit: bool | None,
    primary_key: bool,
    fk_nullable: bool | None,
    allows_none: bool,
) -> bool:
    """Single derivation site for nullability (PK clamp included, FF-B B5).

    Precedence: PK clamp (NOT NULL) > FK-derived > explicit > annotation. FK
    outranks ``explicit`` only nominally — a shadow ``{fk}_id`` column has no
    ``FerroField`` of its own, so its ``explicit`` is always ``None`` and the
    two never compete; ordering FK first keeps the FK branch self-contained.
    """
    if primary_key:
        return False
    if fk_nullable is not None:
        return fk_nullable
    if explicit is not None:
        return explicit
    return allows_none


def _property_is_integer(prop: dict[str, Any]) -> bool:
    return prop.get("type") == "integer" or any(
        item.get("type") == "integer" for item in prop.get("anyOf", [])
    )


def _target_table_name(target: Any) -> str:
    from .registry import REGISTRY

    if isinstance(target, ForwardRef):
        target = target.__forward_arg__
    if isinstance(target, str):
        try:
            model = REGISTRY.resolve_reference(target, default=None)
        except RuntimeError:
            # Ambiguous short ref during first-pass schema build: defer the
            # loud, candidate-listing error to resolve_relationships (the
            # authoritative resolution point), same as a not-yet-defined
            # forward ref. Falling back here keeps the single-resolution-site
            # invariant instead of raising twice from two different phases.
            model = None
        if model is not None:
            return model.__ferro_table__
        # Provisional first-pass fallback for a not-yet-defined forward ref:
        # resolve_relationships' second pass (loud since FF-E E4) re-registers
        # with the target's real table before any DDL consumer runs.
        return target.lower()
    table = getattr(target, "__ferro_table__", None)
    if isinstance(table, str):
        return table
    if hasattr(target, "__name__"):
        return target.__name__.lower()
    return str(target).lower()


def _resolve_ref(schema: dict[str, Any], col_info: dict[str, Any]) -> dict[str, Any]:
    """Inline a local ``#/$defs/...`` reference into a property schema."""
    ref_path = col_info.get("$ref")
    if not isinstance(ref_path, str):
        return col_info
    if not ref_path.startswith("#/$defs/"):
        return col_info
    def_name = ref_path.split("/")[-1]
    resolved = schema.get("$defs", {}).get(def_name)
    if not isinstance(resolved, dict):
        return col_info
    return {
        **resolved,
        **{k: v for k, v in col_info.items() if k != "$ref"},
    }


def _resolve_nested_refs(
    schema: dict[str, Any], col_info: dict[str, Any]
) -> dict[str, Any]:
    """Resolve local refs in one-level nested ``anyOf`` entries."""
    any_of = col_info.get("anyOf")
    if not isinstance(any_of, list):
        return col_info
    resolved_any_of: list[Any] = []
    changed = False
    for candidate in any_of:
        if not isinstance(candidate, dict):
            resolved_any_of.append(candidate)
            continue
        resolved_candidate = _resolve_ref(schema, candidate)
        if resolved_candidate is not candidate:
            changed = True
        resolved_any_of.append(resolved_candidate)
    if not changed:
        return col_info
    return {**col_info, "anyOf": resolved_any_of}


def secret_class_from_annotation(
    hint: Any,
) -> type[SecretStr] | type[SecretBytes] | None:
    """Return the secret type ``hint`` declares, or ``None``.

    ``Annotated`` and ``| None`` are stripped first, so
    ``Annotated[SecretBytes | None, ...]`` gives ``SecretBytes``.
    """
    hint = _strip_optional_and_annotated(hint)
    if isinstance(hint, type) and issubclass(hint, (SecretStr, SecretBytes)):
        return hint
    return None


def _logical_type(col_info: dict[str, Any]) -> str:
    """Map schema type metadata to SchemaIR ``logical_type``."""
    field_type, field_format = _effective_type_and_format(col_info)
    if field_type == "integer":
        return "integer"
    if field_type == "number":
        return "decimal" if field_format == "decimal" else "number"
    if field_type == "boolean":
        return "boolean"
    if field_type == "string":
        if field_format == "date-time":
            return "datetime"
        if field_format == "date":
            return "date"
        if field_format == "time":
            return "time"
        if field_format == "uuid":
            return "uuid"
        if field_format == "binary":
            return "binary"
        return "string"
    if field_type in {"object", "array"}:
        return "json"
    return "unknown"


def _column_default(prop: dict[str, Any], field_info: Any, annotation: Any) -> Any:
    """JSON-schema default, or a one-shot json-family factory snapshot (#373).

    Pydantic omits ``default_factory`` from JSON Schema. On json-family fields
    (``dict`` / ``list`` / nested model) we call the factory once, JSON-dump,
    and store the result. Scalar factories are never invoked. A factory that
    raises, needs arguments, or dumps a non-container leaves the spec unset.
    """
    if "default" in prop:
        return prop["default"]
    if field_info is None or annotation is None:
        return None
    if not _is_json_family(_strip_optional_and_annotated(annotation)):
        return None
    factory = getattr(field_info, "default_factory", None)
    if factory is None or factory is PydanticUndefined or not callable(factory):
        return None
    try:
        value = factory()
    except Exception:
        return None
    if not isinstance(value, (dict, list, BaseModel)):
        return None
    try:
        return json.loads(to_json(value))
    except Exception:
        return None


def _effective_type_and_format(col_info: dict[str, Any]) -> tuple[Any, Any]:
    """Resolve concrete type/format from direct fields or ``anyOf`` unions."""
    field_type = col_info.get("type")
    field_format = col_info.get("format")
    if field_type is not None:
        return field_type, field_format
    any_of = col_info.get("anyOf")
    if isinstance(any_of, list):
        for candidate in any_of:
            if not isinstance(candidate, dict):
                continue
            candidate_type = candidate.get("type")
            if candidate_type is None or candidate_type == "null":
                continue
            # A top-level format on a union is Ferro's own override (decimal,
            # SecretBytes → binary), so it outranks the member's.
            return candidate_type, field_format or candidate.get("format")
    return field_type, field_format


def _enum_values(col_info: dict[str, Any]) -> list[Any] | None:
    direct = col_info.get("enum")
    if isinstance(direct, list):
        return direct
    any_of = col_info.get("anyOf")
    if isinstance(any_of, list):
        for candidate in any_of:
            if not isinstance(candidate, dict):
                continue
            enum_values = candidate.get("enum")
            if isinstance(enum_values, list):
                return enum_values
    return None


def build_column_specs(model_cls: type[Any]) -> dict[str, ColumnSpec]:
    """Build one `ColumnSpec` per schema property, in model-field order.

    Reads Pydantic's `model_json_schema()` exactly once, the only place in
    Ferro that consumes it. Callers store the result on
    `cls.__ferro_columns__` and must replace it, never mutate it.

    Args:
        model_cls: The model class.

    Returns:
        The column specs, keyed by column name.
    """
    schema = model_cls.model_json_schema()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required_raw = schema.get("required", [])
    required = set(required_raw) if isinstance(required_raw, list) else set()
    model_fields = getattr(model_cls, "model_fields", {})
    ferro_fields = getattr(model_cls, "ferro_fields", {}) or {}
    ferro_relations = getattr(model_cls, "ferro_relations", {}) or {}
    try:
        resolved_annotations = get_type_hints(model_cls, include_extras=True)
    except Exception:
        resolved_annotations = {}

    fk_by_shadow = {
        f"{fname}_id": meta
        for fname, meta in ferro_relations.items()
        if isinstance(meta, ForeignKey)
    }

    specs: dict[str, ColumnSpec] = {}
    for field_name, raw_prop in properties.items():
        if not isinstance(raw_prop, dict):
            continue
        prop = _resolve_nested_refs(schema, _resolve_ref(schema, raw_prop))

        field_info = model_fields.get(field_name)
        ann = resolved_annotations.get(
            field_name, field_info.annotation if field_info is not None else None
        )
        if isinstance(ann, str):
            ann = field_info.annotation if field_info is not None else None
        python_type = _scalar_part_of_annotation(ann) if ann is not None else None

        declared = ferro_fields.get(field_name)
        fk_meta = fk_by_shadow.get(field_name)

        is_pk = (
            declared.primary_key
            if declared is not None
            else bool(prop.get("primary_key", False))
        )
        raw_autoincrement = prop.get("autoincrement")
        explicit_autoincrement = (
            declared.autoincrement
            if declared is not None
            else (raw_autoincrement if isinstance(raw_autoincrement, bool) else None)
        )
        autoincrement = _derive_autoincrement(
            explicit_autoincrement, is_pk, _property_is_integer(prop)
        )

        if fk_meta is not None:
            unique = bool(fk_meta.unique)
            index = bool(fk_meta.index)
            fk_ref = ForeignKeyRef(
                to_table=_target_table_name(fk_meta.to),
                on_delete=fk_meta.on_delete,
            )
            fk_nullable = foreign_key_allows_none(fk_meta)
        else:
            unique = (
                bool(declared.unique)
                if declared is not None
                else bool(prop.get("unique", False))
            )
            index = (
                bool(declared.index)
                if declared is not None
                else bool(prop.get("index", False))
            )
            fk_ref = None
            fk_nullable = None

        if declared is not None and isinstance(declared.nullable, bool):
            explicit_nullable: bool | None = declared.nullable
        else:
            raw_nullable = prop.get("ferro_nullable")
            explicit_nullable = raw_nullable if isinstance(raw_nullable, bool) else None
        allows_none = (
            annotation_allows_none(field_info.annotation)
            if field_info is not None
            else field_name not in required
        )
        nullable = _derive_nullable(
            explicit=explicit_nullable,
            primary_key=is_pk,
            fk_nullable=fk_nullable,
            allows_none=allows_none,
        )

        # Path-blind db_type validation (ADR-0003): both declaration paths
        # converge here, so token validity and annotation compatibility are
        # checked once, syntax-blind, at class-definition time.
        # A class derived from an IR snapshot (``__ferro_historical__``) skips
        # the author-facing compatibility half only; the token half still runs.
        compat = not model_cls.__dict__.get("__ferro_historical__", False)
        if declared is not None:
            db_type = validate_db_type_declaration(
                field_name, declared.db_type, ann, check_compatibility=compat
            )
            db_check = bool(declared.db_check)
        else:
            db_type = validate_db_type_declaration(
                field_name, prop.get("db_type"), ann, check_compatibility=compat
            )
            db_check = prop.get("db_check") is True

        secret_cls = secret_class_from_annotation(ann)
        if annotation_is_decimal(ann):
            fmt = "decimal"
        elif secret_cls is SecretBytes:
            # Pydantic schemas both secrets as ``format: password``; the
            # column stores what the secret wraps, and SecretBytes wraps bytes.
            fmt = "binary"
        else:
            fmt = prop.get("format")
        effective_prop = {**prop, "format": fmt} if fmt != prop.get("format") else prop

        enum_cls = enum_subclass_from_annotation(ann)
        if enum_cls is not None:
            enum_type_name: str | None = enum_cls.__name__.lower()
        else:
            raw_enum_name = prop.get("enum_type_name")
            enum_type_name = (
                raw_enum_name
                if isinstance(raw_enum_name, str) and raw_enum_name
                else None
            )
        enum_values = _enum_values(prop)

        specs[field_name] = ColumnSpec(
            name=field_name,
            logical_type=_logical_type(effective_prop),
            nullable=nullable,
            primary_key=is_pk,
            autoincrement=autoincrement,
            unique=unique,
            index=index,
            # A secret's default never becomes a schema default: the JSON
            # schema holds its mask, and the real value has no place in DDL,
            # snapshots or migration files.
            default=(
                None
                if secret_cls is not None
                else _column_default(prop, field_info, ann)
            ),
            format=fmt,
            python_type=python_type,
            enum_values=tuple(enum_values) if isinstance(enum_values, list) else None,
            enum_type_name=enum_type_name,
            enum_class=enum_cls,
            secret_class=secret_cls,
            db_type=db_type,
            db_type_explicit=bool(db_type),
            db_check=db_check,
            foreign_key=fk_ref,
        )
    return specs


def build_relation_specs(
    model_cls: type[Any], columns: dict[str, ColumnSpec]
) -> dict[str, RelationSpec]:
    """Build one `RelationSpec` per declared forward foreign-key relation.

    Reads ``ferro_relations`` (the same class-body-registered relation
    metadata ``build_column_specs`` reads via ``fk_by_shadow``) and pairs each
    entry with its already-compiled shadow ``{field}_id`` ColumnSpec — the
    single-source nullability derivation lives there, not here (design pin:
    do not re-derive). ``ManyToManyRelation`` and BackRef entries in
    ``ferro_relations`` are not ``ForeignKey`` instances and are skipped.

    A relation whose target hasn't been resolved to a concrete class yet
    (provisional, class-body time, before ``resolve_relationships()``) is
    omitted here; the resolved second pass recompiles with real targets so
    the lookup is complete once resolution finishes.

    Callers store the result on ``cls.__ferro_relation_specs__`` and must
    replace, never mutate — same convention as ``__ferro_columns__``.

    Args:
        model_cls: The model class.
        columns: The model's compiled column specs, keyed by column name.

    Returns:
        One spec per resolved forward relation, keyed by field name.
    """
    ferro_relations = getattr(model_cls, "ferro_relations", {}) or {}
    specs: dict[str, RelationSpec] = {}
    for field_name, meta in ferro_relations.items():
        if not isinstance(meta, ForeignKey):
            continue
        target = meta.to
        if not isinstance(target, type):
            continue
        shadow_column = f"{field_name}_id"
        shadow_spec = columns.get(shadow_column)
        if shadow_spec is None:
            continue
        specs[field_name] = RelationSpec(
            field_name=field_name,
            target=target,
            shadow_column=shadow_column,
            nullable=shadow_spec.nullable,
        )
    return specs


def build_reverse_specs(model_cls: type[Any]) -> dict[str, ReverseSpec]:
    """Build one `ReverseSpec` per resolved reverse (`BackRef`) relation.

    Reads the `RelationshipDescriptor`
    instances ``resolve_relationships`` installs on the class — the facts a
    correlated EXISTS needs (related model, child FK column or join-table
    triple, one-to-one flag) already live there, so this derives, never
    re-computes. At provisional class-body time no descriptors exist yet and
    the map is empty; the resolved second pass recompiles with descriptors
    installed, so the lookup is complete once resolution finishes (same
    lifecycle as `build_relation_specs`).

    Many-to-many descriptors map to a join-table spec; each side's descriptor
    already orients ``source_col``/``target_col`` for that side, so the spec
    is a straight copy of resolution's facts on both the declaring and the
    ``related_name`` side.

    Callers store the result on ``cls.__ferro_reverse_specs__`` and must
    replace, never mutate — same convention as ``__ferro_columns__``.

    Args:
        model_cls: The model class.

    Returns:
        One spec per resolved reverse relation, keyed by field name.
    """
    # Late imports: columns.py is a low-level leaf module; the relations
    # package (and the registry it pulls in) imports back into it.
    from .registry import REGISTRY
    from .relations.descriptors import RelationshipDescriptor

    specs: dict[str, ReverseSpec] = {}
    for klass in model_cls.__mro__:
        for name, attr in vars(klass).items():
            if not isinstance(attr, RelationshipDescriptor) or name in specs:
                continue
            target = REGISTRY.resolve_reference(attr.target_model_name, default=None)
            if target is None:
                continue
            if attr.is_m2m:
                specs[name] = ReverseSpec(
                    field_name=name,
                    target=target,
                    child_fk_column=None,
                    is_m2m=True,
                    join_table=attr.join_table,
                    source_col=attr.source_col,
                    target_col=attr.target_col,
                )
                continue
            specs[name] = ReverseSpec(
                field_name=name,
                target=target,
                # The child FK field is the descriptor's `field_name`; its
                # shadow column follows the single `{field}_id` convention
                # (same derivation as build_relation_specs).
                child_fk_column=f"{attr.field_name}_id",
                is_one_to_one=attr.is_one_to_one,
            )
    return specs


_PK_TYPE_FACTS: dict[Any, tuple[str, str | None]] = {
    int: ("integer", None),
    str: ("string", None),
    UUID: ("uuid", "uuid"),
    float: ("number", None),
    bool: ("boolean", None),
    bytes: ("binary", "binary"),
}


def pk_spec(name: str, python_type: Any) -> ColumnSpec:
    """Build the spec of a column typed like a primary key, for join tables.

    A `None` or unknown type falls back to a string column.

    Args:
        name: The column's name.
        python_type: The primary key's Python type.

    Returns:
        A non-null, non-key column spec of that type.
    """
    scalar = (
        _scalar_part_of_annotation(python_type) if python_type is not None else None
    )
    origin = get_origin(scalar)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(scalar) if a is not type(None)]
        if len(non_none) == 1:
            scalar = _scalar_part_of_annotation(non_none[0])
    logical_type, fmt = _PK_TYPE_FACTS.get(scalar, ("string", None))
    return ColumnSpec(
        name=name,
        logical_type=logical_type,
        nullable=False,
        primary_key=False,
        autoincrement=False,
        unique=False,
        index=False,
        default=None,
        format=fmt,
        python_type=scalar,
    )


def fk_shadow_spec(
    name: str,
    *,
    python_type: Any,
    to_table: str,
    on_delete: str | None = "CASCADE",
    unique: bool = False,
    index: bool = False,
    nullable: bool = False,
) -> ColumnSpec:
    """Build a foreign-key column's spec at a known target.

    Join tables and the resolve-time rebuilds use it.

    Args:
        name: The column's name.
        python_type: The target primary key's Python type.
        to_table: The target table's name.
        on_delete: The `ON DELETE` action. Defaults to `"CASCADE"`.
        unique: Add a unique constraint. Defaults to `False`.
        index: Add an index. Defaults to `False`.
        nullable: Allow `NULL`. Defaults to `False`.

    Returns:
        The column spec.
    """
    return replace(
        pk_spec(name, python_type),
        foreign_key=ForeignKeyRef(to_table=to_table, on_delete=on_delete),
        unique=unique,
        index=index,
        nullable=nullable,
    )


def retired_column_specs(model_cls: type[Any]) -> dict[str, ColumnSpec]:
    """Return the retired columns of `model_cls`, flagged and nullable.

    A retired scalar field's spec was derived once at class creation, before
    the field left the Pydantic model (`__ferro_retired_fields__`); a retired
    `ForeignKey`'s shadow column is derived here on every compile, as the
    mapped relation's would be, so it follows its target's resolution.

    Args:
        model_cls: The model class.

    Returns:
        The retired columns' specs, keyed by column name.
    """
    specs: dict[str, ColumnSpec] = dict(
        model_cls.__dict__.get("__ferro_retired_fields__", {})
    )
    relations = model_cls.__dict__.get("__ferro_retired_relations__", {})
    for field_name, meta in relations.items():
        from ._shadow_fk_types import is_concrete_ferro_model, pk_python_type_for_model

        target = meta.to
        python_type = (
            pk_python_type_for_model(target)
            if is_concrete_ferro_model(target)
            else None
        )
        name = f"{field_name}_id"
        specs[name] = replace(
            fk_shadow_spec(
                name,
                python_type=python_type,
                to_table=_target_table_name(target),
                on_delete=meta.on_delete,
                unique=bool(meta.unique),
                index=bool(meta.index),
                nullable=True,
            ),
            default=None,
            retired=True,
        )
    return specs


def retired_clause(model_cls: type[Any], name: str) -> str:
    """Return the clause an unknown-column error adds for a retired name.

    The usual unknown-column errors carry it, so the reader learns the name
    is gone on purpose, not misspelled.

    Args:
        model_cls: The model class.
        name: The field or column name the error is about.

    Returns:
        A sentence saying `name` is retired, or `""` when it is not one of
        the model's retired fields or columns.
    """
    retired = getattr(model_cls, "__ferro_retired_names__", frozenset())
    if name not in retired:
        return ""
    return (
        f" {model_cls.__name__}.{name} is retired in this release: the model "
        "no longer reads or writes it (ADR-0053)."
    )


def refuse_back_ref_over_retired_relation(
    model_cls: type[Any], field_name: str, meta: ForeignKey
) -> None:
    """Refuse a `BackRef` that reads a retired relation back.

    A retired ``ForeignKey`` leaves the model, so nothing fills the target's
    ``BackRef``; it renders no DDL, and the fix is to delete it. Checked when
    the target is a concrete class: at class creation when the target already
    exists, and again by relationship resolution for a forward reference.

    Args:
        model_cls: The model declaring the retired relation.
        field_name: The retired relation's field name.
        meta: The retired relation's `ForeignKey`.

    Raises:
        TypeError: The target declares `meta.related_name` as a `BackRef`.
    """
    target = meta.to
    if meta.related_name is None or not isinstance(target, type):
        return
    relations = getattr(target, "ferro_relations", None) or {}
    if relations.get(meta.related_name) == "BackRef":
        raise TypeError(
            f"{target.__name__}.{meta.related_name} is a BackRef over the retired "
            f"relation {model_cls.__name__}.{field_name}; delete the BackRef"
        )
