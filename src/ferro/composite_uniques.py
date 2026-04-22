"""Table-level composite unique constraints declared on Ferro models."""

from __future__ import annotations

from typing import Any

FERRO_COMPOSITE_UNIQUES = "__ferro_composite_uniques__"


def _normalized_groups(cls: type[Any]) -> tuple[tuple[str, ...], ...]:
    """Return validated, de-duplicated composite-unique column groups.

    ``cls`` must be a :class:`ferro.models.Model` subclass; the base defines
    ``__ferro_composite_uniques__`` so normal access is always valid.
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
                f"{cls.__qualname__}.{FERRO_COMPOSITE_UNIQUES}[{i}] must name at least two columns"
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


def validate_composite_uniques_against_properties(
    cls: type[Any], properties: dict[str, Any]
) -> None:
    """Ensure each referenced column exists on the JSON schema ``properties``."""
    groups = _normalized_groups(cls)
    if not groups:
        return
    keys = set(properties.keys())
    for group in groups:
        for col in group:
            if col not in keys:
                raise ValueError(
                    f"{cls.__qualname__}.{FERRO_COMPOSITE_UNIQUES} references unknown column "
                    f"{col!r}; known properties: {sorted(keys)}"
                )


def ferro_composite_uniques_for_json(cls: type[Any]) -> list[list[str]] | None:
    """Serialize composite groups for the Rust registry / Alembic, or ``None`` to omit."""
    groups = _normalized_groups(cls)
    if not groups:
        return None
    return [list(g) for g in groups]


def apply_composite_uniques_to_schema(cls: type[Any], schema: dict[str, Any]) -> None:
    """Mutate ``schema`` with ``ferro_composite_uniques`` when the model declares any."""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    validate_composite_uniques_against_properties(cls, props)
    payload = ferro_composite_uniques_for_json(cls)
    if payload:
        schema["ferro_composite_uniques"] = payload
    else:
        schema.pop("ferro_composite_uniques", None)
