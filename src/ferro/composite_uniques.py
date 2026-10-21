"""Table-level composite unique constraints declared on Ferro models.

Composite groups are declared on models as ``tuple[tuple[str, ...], ...]`` and
serialized to JSON for the Rust registry as nested lists (``list[list[str]]``),
since JSON has no tuple type.
"""

from __future__ import annotations

from typing import Any

FERRO_COMPOSITE_UNIQUES = "__ferro_composite_uniques__"
"""The class attribute a model declares its composite uniques in."""


def _normalized_groups(cls: type[Any]) -> tuple[tuple[str, ...], ...]:
    """Return validated, de-duplicated composite-unique column groups.

    ``cls`` must be a `ferro.models.Model` subclass; the base defines
    ``__ferro_composite_uniques__`` so normal access is always valid.

    Duplicate identical column tuples are dropped; the first occurrence is kept.
    """
    raw = cls.__ferro_composite_uniques__
    if raw in ((), None):
        return ()
    if not isinstance(raw, tuple):
        raise TypeError(
            f"{cls.__qualname__}.{FERRO_COMPOSITE_UNIQUES} must be a tuple of tuples of str, "
            f"not {type(raw).__name__}"
        )
    out: list[tuple[str, ...]] = []
    for i, group in enumerate(raw):
        if not isinstance(group, tuple):
            raise TypeError(
                f"{cls.__qualname__}.{FERRO_COMPOSITE_UNIQUES}[{i}] must be a tuple of str"
            )
        names: list[str] = []
        for j, col in enumerate(group):
            if not isinstance(col, str) or not col:
                raise TypeError(
                    f"{cls.__qualname__}.{FERRO_COMPOSITE_UNIQUES}[{i}][{j}] "
                    "must be a non-empty str"
                )
            names.append(col)
        if len(names) < 2:
            raise ValueError(
                f"{cls.__qualname__}.{FERRO_COMPOSITE_UNIQUES}[{i}] must name at least two columns "
                f"(for single-column uniqueness use Field(unique=True) or "
                f"Annotated[..., Field(unique=True)], "
                f"or Annotated[..., FerroField(unique=True)] if you use raw Ferro metadata)"
            )
        out.append(tuple(names))
    seen: set[tuple[str, ...]] = set()
    deduped: list[tuple[str, ...]] = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        deduped.append(t)
    return tuple(deduped)


def _validate_groups_against_columns(
    cls: type[Any], groups: tuple[tuple[str, ...], ...], column_names: frozenset[str]
) -> None:
    """Ensure each referenced column exists in ``column_names``."""
    for group in groups:
        for col in group:
            if col not in column_names:
                raise ValueError(
                    f"{cls.__qualname__}.{FERRO_COMPOSITE_UNIQUES} references unknown column "
                    f"{col!r}; known properties: {sorted(column_names)}"
                )


def normalized_composite_uniques(
    cls: type[Any], column_names: frozenset[str]
) -> tuple[tuple[str, ...], ...]:
    """Return the validated, de-duplicated composite-unique column groups of `cls`.

    The single validate-and-normalize choke point for composite uniques: both
    the schema compiler and the legacy dict pipeline route through it.

    Args:
        cls: The model class.
        column_names: The model's column names, which every group must use.

    Returns:
        The column groups, in declaration order.

    Raises:
        TypeError: The declaration is not a tuple of tuples of non-empty
            strings.
        ValueError: A group names fewer than two columns, or a column the
            model does not have.
    """
    groups = _normalized_groups(cls)
    if not groups:
        return ()
    _validate_groups_against_columns(cls, groups, column_names)
    return groups


def validate_composite_uniques_against_properties(
    cls: type[Any], properties: dict[str, Any]
) -> None:
    """Check that every column a group names is a JSON-schema property.

    Args:
        cls: The model class.
        properties: The model's JSON-schema properties.

    Raises:
        TypeError: The declaration is not a tuple of tuples of non-empty
            strings.
        ValueError: A group names fewer than two columns, or a column the
            model does not have.
    """
    normalized_composite_uniques(cls, frozenset(properties.keys()))


def ferro_composite_uniques_for_json(cls: type[Any]) -> list[list[str]] | None:
    """Serialize the composite-unique groups of `cls` for the Rust registry and Alembic.

    Args:
        cls: The model class.

    Returns:
        The groups as nested lists, since JSON has no tuple, or `None` when
        the model declares none.
    """
    groups = _normalized_groups(cls)
    if not groups:
        return None
    return [list(g) for g in groups]
