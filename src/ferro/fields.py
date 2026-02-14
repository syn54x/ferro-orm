"""Expose Ferro's wrapped Field helper on top of Pydantic."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, TypeVar, Unpack, overload

from pydantic.fields import Field as PydanticField
from pydantic.fields import _EmptyKwargs, _Unset
from pydantic_core import PydanticUndefined

if TYPE_CHECKING:
    import re

    import annotated_types
    from pydantic import types
    from pydantic.aliases import AliasChoices, AliasPath
    from pydantic.config import JsonDict
    from pydantic.fields import Deprecated, FieldInfo

FERRO_FIELD_EXTRA_KEY = "ferro_field"
_T = TypeVar("_T")


@overload
def Field(
    default: Literal[Ellipsis],
    *,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
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
    discriminator: str | types.Discriminator | None = ...,
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
    **extra: Any,
) -> Any: ...


@overload
def Field(
    default: Any,
    *,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
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
    discriminator: str | types.Discriminator | None = ...,
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
    **extra: Any,
) -> Any: ...


@overload
def Field(
    default: _T,
    *,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
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
    discriminator: str | types.Discriminator | None = ...,
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
    **extra: Any,
) -> _T: ...


@overload
def Field(
    *,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
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
    discriminator: str | types.Discriminator | None = ...,
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
    **extra: Any,
) -> Any: ...


@overload
def Field(
    *,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
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
    discriminator: str | types.Discriminator | None = ...,
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
    **extra: Any,
) -> _T: ...


@overload
def Field(
    *,
    primary_key: bool = ...,
    autoincrement: bool | None = ...,
    unique: bool = ...,
    index: bool = ...,
    back_ref: bool = ...,
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
    discriminator: str | types.Discriminator | None = ...,
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
    **extra: Any,
) -> Any: ...


def Field(
    default: Any = PydanticUndefined,
    *,
    primary_key: bool | Any = _Unset,
    autoincrement: bool | None | Any = _Unset,
    unique: bool | Any = _Unset,
    index: bool | Any = _Unset,
    back_ref: bool | Any = _Unset,
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
    discriminator: str | types.Discriminator | None = _Unset,
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
    **extra: Unpack[_EmptyKwargs],
) -> Any:
    """Build field metadata with Pydantic and Ferro options

    Args:
        default: Default value used when the field is not set.
        primary_key: Mark this column as the table primary key in Ferro.
        autoincrement: Override automatic increment behavior for primary key columns.
            When not provided, Ferro infers this for integer primary keys.
        unique: Add a uniqueness constraint for this column in Ferro.
        index: Request an index for this column in Ferro.
        back_ref: Mark this field as a reverse relationship (same as BackRef in the type).
            Do not use together with a BackRef annotation on the same field.
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
            See [Strict Mode](../concepts/strict_mode.md) for details.
        gt: Greater than. If set, value must be greater than this. Only applicable to numbers.
        ge: Greater than or equal. If set, value must be greater than or equal to this. Only applicable to numbers.
        lt: Less than. If set, value must be less than this. Only applicable to numbers.
        le: Less than or equal. If set, value must be less than or equal to this. Only applicable to numbers.
        multiple_of: Value must be a multiple of this. Only applicable to numbers.
        min_length: Minimum length for iterables.
        max_length: Maximum length for iterables.
        pattern: Pattern for strings (a regular expression).
        allow_inf_nan: Allow `inf`, `-inf`, `nan`. Only applicable to float and [`Decimal`][decimal.Decimal] numbers.
        max_digits: Maximum number of allow digits for strings.
        decimal_places: Maximum number of decimal places allowed for numbers.
        union_mode: The strategy to apply when validating a union. Can be `smart` (the default), or `left_to_right`.
            See [Union Mode](../concepts/unions.md#union-modes) for details.
        fail_fast: If `True`, validation will stop on the first error. If `False`, all validation errors will be collected.
            This option can be applied only to iterable types (list, tuple, set, and frozenset).
        extra: (Deprecated) Extra fields that will be included in the JSON schema.

            !!! warning Deprecated
                The `extra` kwargs is deprecated. Use `json_schema_extra` instead.

    Returns:
        A new [`FieldInfo`][pydantic.fields.FieldInfo]. The return annotation is `Any` so `Field` can be used on
            type-annotated fields without causing a type error.

    Raises:
        TypeError: If Ferro kwargs are provided together with callable `json_schema_extra`.

    Examples:
        >>> from ferro import Field, Model
        >>> class User(Model):
        ...     id: int | None = Field(default=None, primary_key=True)
        ...     username: str = Field(unique=True, min_length=3)
    """
    ferro_kwargs: dict[str, Any] = {}
    if primary_key is not _Unset:
        ferro_kwargs["primary_key"] = primary_key
    if autoincrement is not _Unset:
        ferro_kwargs["autoincrement"] = autoincrement
    if unique is not _Unset:
        ferro_kwargs["unique"] = unique
    if index is not _Unset:
        ferro_kwargs["index"] = index
    if back_ref is not _Unset:
        ferro_kwargs["back_ref"] = back_ref

    schema_extra = json_schema_extra
    if ferro_kwargs:
        if callable(schema_extra):
            raise TypeError(
                "ferro.Field(..., primary_key=...) cannot be combined with callable "
                "json_schema_extra"
            )
        base_extra: dict[str, Any]
        if schema_extra is _Unset or schema_extra is None:
            base_extra = {}
        else:
            base_extra = dict(schema_extra)
        merged_extra: dict[str, Any] = base_extra
        merged_extra[FERRO_FIELD_EXTRA_KEY] = ferro_kwargs
        schema_extra = merged_extra

    return PydanticField(
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
        json_schema_extra=schema_extra,
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
        **extra,
    )


__all__ = ["Field", "FERRO_FIELD_EXTRA_KEY"]
