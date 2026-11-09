"""`Field`, `Forward`, `Reverse`, `BackRef` and `ManyToMany`: Pydantic's `Field` with Ferro's database options."""

from __future__ import annotations

import ast
import difflib
import inspect
import json
import types
from collections.abc import Callable
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ForwardRef,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
    overload,
)

from pydantic.fields import Field as PydanticField
from pydantic.fields import _Unset
from pydantic_core import PydanticUndefined

from .base import (
    DbType,
    FerroField,
    FerroNullable,
    OnDelete,
    _validate_foreign_key,
    _validate_nullable_option,
    _validate_on_delete,
    _validate_renamed_from,
    _validate_generated,
    _validate_retired,
)

if TYPE_CHECKING:
    import re

    import annotated_types
    from pydantic import Discriminator
    from pydantic.aliases import AliasChoices, AliasPath
    from pydantic.config import JsonDict
    from pydantic.fields import Deprecated, FieldInfo

_T = TypeVar("_T")


class _RelationOptions:
    """The relation keywords of one `Field()` call, carried in the field's metadata.

    The metaclass reads them from there, so they never reach the field's JSON
    schema the way `json_schema_extra` would.
    """

    __slots__ = ("options",)

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def __repr__(self) -> str:
        return f"_RelationOptions({self.options!r})"


def _unknown_keyword(name: str) -> TypeError:
    """Refuse a keyword `Field()` does not take, naming the closest one it does.

    The known names come from `Field`'s own signature, so an option added as
    a parameter is known here with no second list.
    """
    parameters = inspect.signature(Field).parameters
    known = [
        parameter
        for parameter, spec in parameters.items()
        if spec.kind is not inspect.Parameter.VAR_KEYWORD
    ]
    close = difflib.get_close_matches(name, known, n=1)
    if close:
        hint = f"Did you mean {close[0]!r}?"
    else:
        pydantic_names = inspect.signature(PydanticField).parameters
        options = ", ".join(
            parameter for parameter in known if parameter not in pydantic_names
        )
        hint = f"Ferro's field options are: {options}."
    return TypeError(f"Field() got an unknown keyword {name!r}. {hint}")


# A retired field and a generated column are never `__init__` parameters:
# these two overloads mark them `init=False` for a type checker (PEP 681),
# as the constructor refuses both by name.
@overload
def Field(
    default: Any = ...,
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: Literal[True],
    generated: Callable[[Any], Any] | None = ...,
    default_factory: Callable[[], Any] | Callable[[dict[str, Any]], Any] | None = ...,
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: bool | None = ...,
    repr: bool = ...,
    init: Literal[False] = False,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> Any: ...


@overload
def Field(
    default: Any = ...,
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: bool = ...,
    generated: Callable[[Any], Any],
    default_factory: Callable[[], Any] | Callable[[dict[str, Any]], Any] | None = ...,
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: bool | None = ...,
    repr: bool = ...,
    init: Literal[False] = False,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> Any: ...


@overload
def Field(
    default: Literal[Ellipsis],
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: bool = ...,
    generated: Callable[[Any], Any] | None = ...,
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: bool | None = ...,
    repr: bool = ...,
    init: bool | None = ...,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> Any: ...


@overload
def Field(
    default: Any,
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: bool = ...,
    generated: Callable[[Any], Any] | None = ...,
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: Literal[True],
    repr: bool = ...,
    init: bool | None = ...,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> Any: ...


@overload
def Field(
    default: _T,
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: bool = ...,
    generated: Callable[[Any], Any] | None = ...,
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: Literal[False] = ...,
    repr: bool = ...,
    init: bool | None = ...,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> _T: ...


@overload
def Field(
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: bool = ...,
    generated: Callable[[Any], Any] | None = ...,
    default_factory: Callable[[], Any] | Callable[[dict[str, Any]], Any],
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: Literal[True],
    repr: bool = ...,
    init: bool | None = ...,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> Any: ...


@overload
def Field(
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: bool = ...,
    generated: Callable[[Any], Any] | None = ...,
    default_factory: Callable[[], _T] | Callable[[dict[str, Any]], _T],
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: Literal[False] | None = ...,
    repr: bool = ...,
    init: bool | None = ...,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> _T: ...


@overload
def Field(
    *,
    foreign_key: type[Any] | str | None = ...,
    on_delete: OnDelete = ...,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
    many_to_many: bool = ...,
    related_name: str | None = ...,
    through: str | None = ...,
    reverse_index: bool = ...,
    nullable: FerroNullable = ...,
    db_type: DbType | None = ...,
    db_check: bool = ...,
    renamed_from: str | None = ...,
    retired: bool = ...,
    generated: Callable[[Any], Any] | None = ...,
    alias: str | None = ...,
    alias_priority: int | None = ...,
    validation_alias: str | AliasPath | AliasChoices | None = ...,
    serialization_alias: str | None = ...,
    title: str | None = ...,
    field_title_generator: Callable[[str, FieldInfo], str] | None = ...,
    description: str | None = ...,
    examples: list[Any] | None = ...,
    exclude: bool | None = ...,
    exclude_if: Callable[[Any], bool] | None = ...,
    discriminator: str | Discriminator | None = ...,
    deprecated: Deprecated | str | bool | None = ...,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = ...,
    frozen: bool | None = ...,
    validate_default: bool | None = ...,
    repr: bool = ...,
    init: bool | None = ...,
    init_var: bool | None = ...,
    kw_only: bool | None = ...,
    pattern: str | re.Pattern[str] | None = ...,
    strict: bool | None = ...,
    coerce_numbers_to_str: bool | None = ...,
    gt: annotated_types.SupportsGt | None = ...,
    ge: annotated_types.SupportsGe | None = ...,
    lt: annotated_types.SupportsLt | None = ...,
    le: annotated_types.SupportsLe | None = ...,
    multiple_of: float | None = ...,
    allow_inf_nan: bool | None = ...,
    max_digits: int | None = ...,
    decimal_places: int | None = ...,
    min_length: int | None = ...,
    max_length: int | None = ...,
    union_mode: Literal["smart", "left_to_right"] = ...,
    fail_fast: bool | None = ...,
) -> Any: ...


def Field(
    default: Any = PydanticUndefined,
    *,
    foreign_key: type[Any] | str | None | Any = _Unset,
    on_delete: OnDelete | Any = _Unset,
    primary_key: bool | Any = _Unset,
    autoincrement: bool | None | Any = _Unset,
    unique: bool | Any = _Unset,
    index: bool | Any = _Unset,
    back_ref: bool | Any = _Unset,
    many_to_many: bool | Any = _Unset,
    related_name: str | None | Any = _Unset,
    through: str | None | Any = _Unset,
    reverse_index: bool | Any = _Unset,
    nullable: FerroNullable | Any = _Unset,
    db_type: DbType | None | Any = _Unset,
    db_check: bool | Any = _Unset,
    renamed_from: str | None | Any = _Unset,
    retired: bool | Any = _Unset,
    generated: Callable[[Any], Any] | None | Any = _Unset,
    default_factory: Callable[[], Any]
    | Callable[[dict[str, Any]], Any]
    | None = _Unset,
    alias: str | None = _Unset,
    alias_priority: int | None = _Unset,
    validation_alias: str | AliasPath | AliasChoices | None = _Unset,
    serialization_alias: str | None = _Unset,
    title: str | None = _Unset,
    field_title_generator: Callable[[str, FieldInfo], str] | None = _Unset,
    description: str | None = _Unset,
    examples: list[Any] | None = _Unset,
    exclude: bool | None = _Unset,
    exclude_if: Callable[[Any], bool] | None = _Unset,
    discriminator: str | Discriminator | None = _Unset,
    deprecated: Deprecated | str | bool | None = _Unset,
    json_schema_extra: JsonDict | Callable[[JsonDict], None] | None = _Unset,
    frozen: bool | None = _Unset,
    validate_default: bool | None = _Unset,
    repr: bool = _Unset,
    init: bool | None = _Unset,
    init_var: bool | None = _Unset,
    kw_only: bool | None = _Unset,
    pattern: str | re.Pattern[str] | None = _Unset,
    strict: bool | None = _Unset,
    coerce_numbers_to_str: bool | None = _Unset,
    gt: annotated_types.SupportsGt | None = _Unset,
    ge: annotated_types.SupportsGe | None = _Unset,
    lt: annotated_types.SupportsLt | None = _Unset,
    le: annotated_types.SupportsLe | None = _Unset,
    multiple_of: float | None = _Unset,
    allow_inf_nan: bool | None = _Unset,
    max_digits: int | None = _Unset,
    decimal_places: int | None = _Unset,
    min_length: int | None = _Unset,
    max_length: int | None = _Unset,
    union_mode: Literal["smart", "left_to_right"] = _Unset,
    fail_fast: bool | None = _Unset,
    **unknown: Any,
) -> Any:
    """Declare a model field with Pydantic's options and Ferro's database options.

    `Field` takes every argument of Pydantic's own `Field` and adds the
    database options Ferro reads: primary key, uniqueness, indexes, column
    type, relations and rename hints. Use it as the field's default, or
    inside `Annotated[...]`. See
    [Field options](../guide/models-and-fields.md#field-options) for each
    database option at work.

    Args:
        default: The value the field takes when it is not set.
        foreign_key: Make the column a foreign key to this model's primary
            key: the class, or its class name for a self reference or a
            model defined later. The column's type must be the target key's
            type, and a type that stores differently needs the target's
            `db_type`. See
            [Relationships](../guide/relationships.md).
        on_delete: What the database does to this row when the row the key
            points at is deleted: `"CASCADE"` (the default), `"RESTRICT"`,
            `"SET NULL"`, `"SET DEFAULT"` or `"NO ACTION"`. Needs
            `foreign_key`, and `"SET NULL"` needs an annotation that allows
            `None`.
        primary_key: Make the column the table's primary key.
        autoincrement: Whether the database generates the key. When not
            given, an integer primary key auto-increments.
        unique: Add a unique constraint on this column alone. For uniqueness
            across several columns, declare `__ferro_composite_uniques__` on
            the model (see
            [Composite uniques](../guide/models-and-fields.md#composite-uniques)).
        index: Add an index on the column.
        back_ref: Mark the field as the reading side of a relation. Assigning
            [`BackRef()`][ferro.BackRef] is the usual spelling.
        many_to_many: Mark the field as a many-to-many relation.
            [`ManyToMany(...)`][ferro.ManyToMany] is the usual spelling.
        related_name: The name of the relation on the target model that reads
            this one back.
        through: The many-to-many join table's name. When not given, Ferro
            derives one from the two models.
        reverse_index: Add an index on the many-to-many join table's
            `(target, source)` columns for queries from the other side. When
            not given, the index is added.
        nullable: `"infer"` (the default) makes the column nullable when the
            annotation allows `None`; `True` or `False` forces `NULL` or
            `NOT NULL` in every schema Ferro emits, whatever the annotation
            says.
        db_type: The column's SQL type, overriding the one Ferro derives from
            the annotation: a [`DbTypeToken`][ferro.DbTypeToken] or a
            [`varchar(N)`][ferro.varchar] name. When not given, the column
            takes the derived type. A type the annotation cannot be stored
            as is refused when the class is defined.
        db_check: Add a `CHECK` constraint, named `ck_<table>_<column>`, that
            limits the column to an enum's values. Needs `db_type`, since the
            default native enum type already enforces them. Defaults to
            `False`.
        renamed_from: The column's previous name, so that `ferro migrate new`
            renames the column, and every index and check named after it,
            instead of dropping and adding it. See
            [Rename hints](../guide/schema/renames-and-retirements.md#rename-hints).
        retired: Take the column out of the model while it stays in the
            schema, nullable, until a later release deletes the declaration.
            The annotation must allow `None`. Defaults to `False`. See
            [Retiring a column](../guide/schema/renames-and-retirements.md#retiring-a-column).
        generated: A lambda over the model's own columns, its parameter named
            after the model, from which the database computes the column on
            every write: arithmetic and the functions in
            [`ferro.fn`][ferro.fn]. The field defaults to `None` and holds the
            database's value once saved. Defaults to `None`, a plain column.
        default_factory: A callable to generate the default value. The callable can either take 0 arguments
            (in which case it is called as is) or a single argument containing the already validated data.
        alias: The name to use for the attribute when validating or serializing by alias.
            This is often used for things like converting between snake and camel case.
        alias_priority: Priority of the alias. This affects whether an alias generator is used.
        validation_alias: Like `alias`, but only affects validation, not serialization.
        serialization_alias: Like `alias`, but only affects serialization, not validation.
        title: Human-readable title.
        field_title_generator: A callable that takes a field name and returns title for it.
        description: Human-readable description.
        examples: Example values for this field.
        exclude: Whether to exclude the field from the model serialization.
        exclude_if: A callable that determines whether to exclude a field during serialization based on its value.
        discriminator: Field name or Discriminator for discriminating the type in a tagged union.
        deprecated: A deprecation message, an instance of `warnings.deprecated` or the `typing_extensions.deprecated` backport,
            or a boolean. If `True`, a default deprecation message will be emitted when accessing the field.
        json_schema_extra: A dict or callable to provide extra JSON schema properties.
        frozen: Whether the field is frozen. If true, attempts to change the value on an instance will raise an error.
        validate_default: If `True`, apply validation to the default value every time you create an instance.
            Otherwise, for performance reasons, the default value of the field is trusted and not validated.
        repr: A boolean indicating whether to include the field in the `__repr__` output.
        init: Whether the field should be included in the constructor of the dataclass.
            (Only applies to dataclasses.)
        init_var: Whether the field should _only_ be included in the constructor of the dataclass.
            (Only applies to dataclasses.)
        kw_only: Whether the field should be a keyword-only argument in the constructor of the dataclass.
            (Only applies to dataclasses.)
        coerce_numbers_to_str: Whether to enable coercion of any `Number` type to `str` (not applicable in `strict` mode).
        strict: If `True`, strict validation is applied to the field.
            See [Strict Mode](https://docs.pydantic.dev/latest/concepts/strict_mode/).
        gt: Greater than. If set, value must be greater than this. Only applicable to numbers.
        ge: Greater than or equal. If set, value must be greater than or equal to this. Only applicable to numbers.
        lt: Less than. If set, value must be less than this. Only applicable to numbers.
        le: Less than or equal. If set, value must be less than or equal to this. Only applicable to numbers.
        multiple_of: Value must be a multiple of this. Only applicable to numbers.
        min_length: Minimum length for iterables.
        max_length: Maximum length for iterables.
        pattern: Pattern for strings (a regular expression).
        allow_inf_nan: Allow `inf`, `-inf`, `nan`. Only applicable to float and [`Decimal`][decimal.Decimal] numbers.
        max_digits: Maximum number of allowed digits for strings.
        decimal_places: Maximum number of decimal places allowed for numbers.
        union_mode: The strategy to apply when validating a union. Can be `smart` (the default), or `left_to_right`.
            See [Union Mode](https://docs.pydantic.dev/latest/concepts/unions/#union-modes).
        fail_fast: If `True`, validation will stop on the first error. If `False`, all validation errors will be collected.
            This option can be applied only to iterable types (list, tuple, set, and frozenset).
        **unknown: Refused: a keyword not listed here raises `TypeError`
            naming the closest one that is.

    Returns:
        A new [`FieldInfo`][pydantic.fields.FieldInfo] that carries the
            database options in its `metadata`, as one
            [`FerroField`][ferro.FerroField], and never in its JSON schema.
            The return annotation is `Any` so that `Field` type-checks as any
            field's default.

    Raises:
        TypeError: A keyword `Field` does not take, or `nullable`,
            `renamed_from`, `retired`, `generated`, `foreign_key` or
            `on_delete` has a value of the wrong type.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import Field, Model, create_tables


        class Reader(Model):
            id: int | None = Field(default=None, primary_key=True)
            handle: str = Field(unique=True, min_length=3)
            nickname: str | None = Field(default=None, index=True)


        await create_tables()
        reader = await Reader.create(handle="ada")
        assert reader.id == 1
        ```

        The same model with the options inside `Annotated`:

        ```python {test=run prelude=reference}
        from typing import Annotated

        from ferro import Field, Model


        class Reader(Model):
            id: Annotated[int | None, Field(primary_key=True)] = None
            handle: Annotated[str, Field(unique=True, min_length=3)]
            nickname: Annotated[str | None, Field(index=True)] = None
        ```
    """
    for name in unknown:
        raise _unknown_keyword(name)

    if nullable is not _Unset:
        _validate_nullable_option(nullable, "Field")
    if renamed_from is not _Unset:
        _validate_renamed_from(renamed_from, "Field")
    if retired is not _Unset:
        _validate_retired(retired, "Field")
    if generated is not _Unset:
        _validate_generated(generated, "Field")
    if foreign_key is not _Unset:
        _validate_foreign_key(foreign_key, "Field")
    if on_delete is not _Unset:
        _validate_on_delete(on_delete, "Field")
    if generated is None:
        # `generated=None` is a plain column, the same as leaving it out.
        generated = _Unset
    # The options ride the field's metadata, never `json_schema_extra`, which
    # Pydantic prints into the JSON schema.
    column_options = {
        option: value
        for option, value in (
            ("primary_key", primary_key),
            ("autoincrement", autoincrement),
            ("unique", unique),
            ("index", index),
            ("nullable", nullable),
            ("db_type", db_type),
            ("db_check", db_check),
            ("renamed_from", renamed_from),
            ("retired", retired),
            ("generated", generated),
            ("foreign_key", foreign_key),
            ("on_delete", on_delete),
        )
        if value is not _Unset
    }
    relation_options = {
        option: value
        for option, value in (
            ("back_ref", back_ref),
            ("many_to_many", many_to_many),
            ("related_name", related_name),
            ("through", through),
            ("reverse_index", reverse_index),
        )
        if value is not _Unset
    }
    if (
        "generated" in column_options
        and default is PydanticUndefined
        and default_factory is _Unset
    ):
        # The database fills the value; the instance holds None until then.
        default = None

    info = PydanticField(
        default=default,
        default_factory=default_factory,
        alias=alias,
        alias_priority=alias_priority,
        validation_alias=validation_alias,
        serialization_alias=serialization_alias,
        title=title,
        field_title_generator=field_title_generator,
        description=description,
        examples=examples,
        exclude=exclude,
        exclude_if=exclude_if,
        discriminator=discriminator,
        deprecated=deprecated,
        json_schema_extra=json_schema_extra,
        frozen=frozen,
        validate_default=validate_default,
        repr=repr,
        init=init,
        init_var=init_var,
        kw_only=kw_only,
        pattern=pattern,
        strict=strict,
        coerce_numbers_to_str=coerce_numbers_to_str,
        gt=gt,
        ge=ge,
        lt=lt,
        le=le,
        multiple_of=multiple_of,
        allow_inf_nan=allow_inf_nan,
        max_digits=max_digits,
        decimal_places=decimal_places,
        min_length=min_length,
        max_length=max_length,
        union_mode=union_mode,
        fail_fast=fail_fast,
    )
    if column_options:
        info.metadata.append(FerroField(**column_options))
    if relation_options:
        info.metadata.append(_RelationOptions(relation_options))
    return info


class BackRef:
    """The reading side of a relation, assigned as the field's default.

    `BackRef()` is `Field(back_ref=True)`. Pair it with the
    [`ForeignKey`][ferro.ForeignKey] or [`ManyToMany`][ferro.ManyToMany]
    whose `related_name` names this field; the field adds no column. It
    takes the keyword arguments of [`Field`][ferro.Field] except
    `reverse_index`, which belongs on the `ManyToMany` side.

    Examples:
        ```python {test=run prelude=reference}
        ada = await Author.where(lambda author: author.name == "Ada Lovelace").first()
        titles = [post.title for post in await ada.posts.all()]
        assert titles == ["Notes on the Analytical Engine"]
        ```
    """

    def __new__(cls, **kwargs: Any) -> Any:
        if "reverse_index" in kwargs:
            raise TypeError(
                "BackRef() does not accept 'reverse_index'; this kwarg lives on the "
                "forward ManyToMany(...) declaration. Set reverse_index there instead."
            )
        return Field(back_ref=True, **kwargs)

    @classmethod
    def __class_getitem__(cls, _item: Any) -> Any:
        raise TypeError(
            "BackRef[...] is no longer a type annotation. Use "
            "Relation[list[T]] = BackRef() for collection back-references."
        )


if TYPE_CHECKING:
    # How a relation reads to a type checker: a field specifier that is never
    # an `__init__` parameter (PEP 681). At runtime each one refuses `init`,
    # and the constructor refuses a relation keyword itself.
    def ManyToMany(
        *,
        through: str | None = None,
        related_name: str | None = None,
        reverse_index: bool = True,
        init: Literal[False] = False,
        **kwargs: Any,
    ) -> Any: ...

    def Forward(column: str, *, init: Literal[False] = False) -> Any: ...

    def Reverse(attribute: str, *, init: Literal[False] = False) -> Any: ...

else:

    def ManyToMany(
        *,
        through: str | None = None,
        related_name: str | None = None,
        reverse_index: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Declare a many-to-many relation, assigned as the field's default.

        Annotate the field `Relation[list[Target]]` and name its join table.
        Ferro creates that table, with one `<table>_id` column per model, a
        unique index over the pair and an index for reads from the other side.
        The target reads the relation back through a
        [`Reverse`][ferro.Reverse] naming this field, which it may leave out.
        See [Many-to-many](../guide/relationships.md#many-to-many).

        Args:
            through: The join table's name. Required: a model with no `through=`
                is refused when its class is defined, naming the table to write.
            reverse_index: Add an index on the join table's `(target, source)`
                columns for queries from the other side. Defaults to `True`; turn
                it off for a write-heavy join table.
            **kwargs: Other keyword arguments of [`Field`][ferro.Field].

        Returns:
            The field's metadata, as [`Field`][ferro.Field] returns it.

        Examples:
            ```python {test=run prelude=reference}
            from ferro import Field, ManyToMany, Model, Relation, Reverse, create_tables


            class Tag(Model):
                id: int | None = Field(default=None, primary_key=True)
                label: str
                articles: Relation[list["Article"]] = Reverse("tags")


            class Article(Model):
                id: int | None = Field(default=None, primary_key=True)
                title: str
                tags: Relation[list[Tag]] = ManyToMany(through="article_tags")


            await create_tables()
            history = await Tag.create(label="history")
            article = await Article.create(title="Babbage's engines")
            await article.tags.add(history)
            assert [tag.label for tag in await article.tags.all()] == ["history"]
            assert [found.title for found in await history.articles.all()] == [
                "Babbage's engines"
            ]
            ```
        """
        return _many_to_many_field(through, related_name, reverse_index, kwargs)

    def Forward(column: str) -> Any:
        """Declare a forward relation over a foreign-key column of this model.

        Assign it to a field annotated `Relation[Target]`, or
        `Relation[Target | None]` when the column allows `None`. The relation
        reads the row the column points at: awaiting it loads the target, and a
        query's `include()` fills it in. It adds no column; the foreign-key
        column is the field declared with `Field(foreign_key=Target)`. See
        [Relationships](../guide/relationships.md).

        Args:
            column: The name of the foreign-key column on this model.

        Returns:
            The field's metadata, as [`Field`][ferro.Field] returns it.

        Raises:
            TypeError: `column` is not a non-empty string.

        Examples:
            ```python {test=run prelude=reference}
            from ferro import Field, Forward, Model, Relation, create_tables


            class Shelf(Model):
                id: int | None = Field(default=None, primary_key=True)
                label: str


            class Book(Model):
                id: int | None = Field(default=None, primary_key=True)
                title: str
                shelf_id: int = Field(foreign_key=Shelf, index=True)
                shelf: Relation[Shelf] = Forward("shelf_id")


            await create_tables()
            history = await Shelf.create(label="History")
            await Book.create(title="SPQR", shelf_id=history.id)
            book = await Book.select().include(lambda book: book.shelf).first()
            assert book.shelf.label == "History"
            ```
        """
        return _relation_field("Forward", "forward", column)

    def Reverse(attribute: str) -> Any:
        """Declare a reverse relation over a foreign-key column of another model.

        Assign it to a field annotated `Relation[list[Other]]`, where `Other`
        declares the foreign-key column with `Field(foreign_key=ThisModel)`. On
        an instance the relation is a query over the rows whose column holds
        this instance's primary key. Neither side names the other: a
        [`Forward`][ferro.Forward] needs no `Reverse`. See
        [Relationships](../guide/relationships.md).

        `attribute` may also name a [`ManyToMany`][ferro.ManyToMany] on the
        other model, and the relation reads the rows its join table links.

        Args:
            attribute: The name of the foreign-key column, or of the
                many-to-many, on the other model.

        Returns:
            The field's metadata, as [`Field`][ferro.Field] returns it.

        Raises:
            TypeError: `attribute` is not a non-empty string.

        Examples:
            ```python {test=run prelude=reference}
            from ferro import Field, Model, Relation, Reverse, create_tables


            class Shelf(Model):
                id: int | None = Field(default=None, primary_key=True)
                label: str
                books: Relation[list["Book"]] = Reverse("shelf_id")


            class Book(Model):
                id: int | None = Field(default=None, primary_key=True)
                title: str
                shelf_id: int = Field(foreign_key=Shelf)


            await create_tables()
            history = await Shelf.create(label="History")
            await Book.create(title="SPQR", shelf_id=history.id)
            titles = [book.title for book in await history.books.all()]
            assert titles == ["SPQR"]
            ```
        """
        return _relation_field("Reverse", "reverse", attribute)


def _many_to_many_field(
    through: str | None,
    related_name: str | None,
    reverse_index: bool,
    kwargs: dict[str, Any],
) -> Any:
    """The `FieldInfo` a `ManyToMany(...)` declares, refusing `init` as a relation."""
    if "init" in kwargs:
        raise TypeError(
            "ManyToMany() got an unexpected keyword argument 'init': a relation "
            "is never a constructor parameter, so drop init= from the declaration"
        )
    options: dict[str, Any] = (
        {} if related_name is None else {"related_name": related_name}
    )
    return Field(
        many_to_many=True,
        through=through,
        reverse_index=reverse_index,
        **options,
        **kwargs,
    )


def _relation_field(spelling: str, kind: str, name: Any) -> Any:
    """A `FieldInfo` whose metadata names the column a relation reads."""
    if not isinstance(name, str) or not name:
        raise TypeError(
            f"{spelling}() takes the foreign-key column's name as a string, "
            f'such as {spelling}("team_id"), not {name!r}'
        )
    info = PydanticField()
    info.metadata.append(_RelationOptions({kind: name}))
    return info


# -- declaration lines, as a refusal writes them -------------------------------------


def annotation_text(annotation: Any) -> str:
    """Write an annotation as it reads in source: `int | None`, `list[Player]`.

    A forward reference loses its quotes, since a refusal prints the line
    with the target named; a `Literal`'s values keep theirs.
    """
    if isinstance(annotation, str):
        return _unquoted(annotation)
    if isinstance(annotation, ForwardRef):
        return annotation_text(annotation.__forward_arg__)
    if annotation is None or annotation is type(None):
        return "None"
    origin = get_origin(annotation)
    if origin is Annotated:
        return annotation_text(get_args(annotation)[0])
    if origin is Union or origin is types.UnionType:
        return " | ".join(annotation_text(arg) for arg in get_args(annotation))
    if origin is Literal:
        values = ", ".join(_literal_text(value) for value in get_args(annotation))
        return f"Literal[{values}]"
    if origin is not None:
        name = getattr(origin, "__name__", None) or repr(origin)
        arguments = ", ".join(annotation_text(arg) for arg in get_args(annotation))
        return f"{name}[{arguments}]"
    return getattr(annotation, "__name__", None) or repr(annotation)


def _literal_text(value: Any) -> str:
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.name}"
    return (
        json.dumps(value, ensure_ascii=False) if isinstance(value, str) else repr(value)
    )


class _Unquote(ast.NodeTransformer):
    """Write each quoted forward reference as the name it holds."""

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        if (
            isinstance(node.value, (ast.Name, ast.Attribute))
            and (getattr(node.value, "id", None) or getattr(node.value, "attr", None))
            == "Literal"
        ):
            return node  # its values are strings, not references
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str):
            try:
                return self.visit(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return node
        return node


def _unquoted(text: str) -> str:
    """``text`` with its forward references unquoted: ``list["Player"]`` reads ``list[Player]``."""
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError:
        return text
    return ast.unparse(_Unquote().visit(tree))


def _option_text(value: Any) -> str:
    if isinstance(value, type):
        return value.__name__
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


_NO_OPTIONS = FerroField()


def field_options(meta: FerroField | None, **changes: Any) -> dict[str, Any]:
    """The options `meta` declares, `changes` applied, in `Field()`'s order.

    An option left at its default is left out, `on_delete="CASCADE"` (a
    foreign-key column's default) included, so a change back to the default
    drops the option.
    """
    options: dict[str, Any] = {}
    for name in inspect.signature(Field).parameters:
        if not hasattr(_NO_OPTIONS, name):
            continue
        value = changes[name] if name in changes else getattr(meta or _NO_OPTIONS, name)
        if value == getattr(_NO_OPTIONS, name) or (
            name == "on_delete" and value == "CASCADE"
        ):
            continue
        options[name] = value
    return options


def field_line(
    name: str,
    annotation: str,
    options: dict[str, Any],
    default: Any = PydanticUndefined,
) -> str:
    """Write one field declaration: `team_id: int = Field(foreign_key=Team)`.

    Args:
        name: The field's name.
        annotation: The annotation, as source text.
        options: The `Field()` database options, in the order written.
        default: The field's default, or `PydanticUndefined` for none.

    Returns:
        The declaration, as one line of source.
    """
    arguments = [] if default is PydanticUndefined else [f"default={default!r}"]
    arguments += [
        f"{option}={_option_text(value)}" for option, value in options.items()
    ]
    return f"{name}: {annotation} = Field({', '.join(arguments)})"


__all__ = ["Field", "BackRef", "ManyToMany", "Forward", "Reverse"]
