"""Table-level composite (non-unique) indexes declared on Ferro models.

Composite groups are declared as ``tuple[tuple[str, ...], ...]`` and serialized
to JSON for the Rust registry / Alembic bridge as nested lists
(``list[list[str]]``), since JSON has no tuple type.
"""

from __future__ import annotations

import warnings
from typing import Any

FERRO_COMPOSITE_INDEXES = "__ferro_composite_indexes__"


def _normalized_groups(cls: type[Any]) -> tuple[tuple[str, ...], ...]:
    """Return validated, de-duplicated composite-index column groups.

    ``cls`` must be a :class:`ferro.models.Model` subclass; the base defines
    ``__ferro_composite_indexes__`` so normal access is always valid.

    Duplicate identical ordered column tuples are dropped; the first occurrence
    is kept. Order matters: ``("a", "b")`` and ``("b", "a")`` are distinct
    groups (different leftmost-prefix optimization).
    """
    raw = cls.__ferro_composite_indexes__
    if raw in ((), None):
        return ()
    if not isinstance(raw, tuple):
        raise TypeError(
            f"{cls.__qualname__}.{FERRO_COMPOSITE_INDEXES} must be a tuple of tuples of str, "
            f"not {type(raw).__name__}"
        )
    out: list[tuple[str, ...]] = []
    for i, group in enumerate(raw):
        if not isinstance(group, tuple):
            raise TypeError(
                f"{cls.__qualname__}.{FERRO_COMPOSITE_INDEXES}[{i}] must be a tuple of str"
            )
        names: list[str] = []
        for j, col in enumerate(group):
            if not isinstance(col, str) or not col:
                raise TypeError(
                    f"{cls.__qualname__}.{FERRO_COMPOSITE_INDEXES}[{i}][{j}] "
                    "must be a non-empty str"
                )
            names.append(col)
        if len(names) < 2:
            raise ValueError(
                f"{cls.__qualname__}.{FERRO_COMPOSITE_INDEXES}[{i}] must name at least two columns "
                f"(for single-column indexing use Field(index=True) or "
                f"Annotated[..., Field(index=True)])"
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
                    f"{cls.__qualname__}.{FERRO_COMPOSITE_INDEXES} references unknown column "
                    f"{col!r}; known properties: {sorted(column_names)}"
                )


def normalized_composite_indexes(
    cls: type[Any], column_names: frozenset[str]
) -> tuple[tuple[str, ...], ...]:
    """Return validated, de-duplicated composite-index column groups for ``cls``.

    The single validate-and-normalize choke point for composite indexes: both
    the SchemaIR compiler and the legacy dict pipeline route through this.
    """
    groups = _normalized_groups(cls)
    if not groups:
        return ()
    _validate_groups_against_columns(cls, groups, column_names)
    return groups


def validate_composite_indexes_against_properties(
    cls: type[Any], properties: dict[str, Any]
) -> None:
    """Ensure each referenced column exists on the JSON schema ``properties``."""
    normalized_composite_indexes(cls, frozenset(properties.keys()))


def ferro_composite_indexes_for_json(cls: type[Any]) -> list[list[str]] | None:
    """Serialize composite groups for the Rust registry / Alembic, or ``None`` to omit."""
    groups = _normalized_groups(cls)
    if not groups:
        return None
    return [list(g) for g in groups]


def drop_overlap_with_uniques(
    indexes: tuple[tuple[str, ...], ...],
    uniques: tuple[tuple[str, ...], ...],
    model_name: str,
) -> tuple[tuple[str, ...], ...]:
    """Warn and drop any composite index that duplicates an existing composite unique.

    Same ordered column tuple in both the composite-unique and composite-index
    groups is redundant: the unique constraint already creates an underlying
    index. Reordered tuples are kept (different leftmost-prefix optimization).
    """
    if not indexes or not uniques:
        return tuple(indexes)
    unique_set = set(uniques)
    kept: list[tuple[str, ...]] = []
    for group in indexes:
        if group in unique_set:
            warnings.warn(
                f"{model_name}.{FERRO_COMPOSITE_INDEXES} entry {group!r} "
                f"duplicates an existing __ferro_composite_uniques__ group; the unique "
                f"constraint already provides this index. Dropping the redundant "
                f"composite index.",
                UserWarning,
                stacklevel=2,
            )
            continue
        kept.append(group)
    return tuple(kept)
