"""Annotation helpers for schema bridges (Alembic, etc.)."""

from __future__ import annotations

import types
from typing import Annotated, Any, Union, get_args, get_origin


def annotation_allows_none(annotation: Any) -> bool:
    """True if the annotation permits ``None`` (unwraps ``Annotated``)."""
    hint: Any = annotation
    while True:
        alias_value = getattr(hint, "__value__", None)
        if alias_value is not None:
            hint = alias_value
            continue
        origin = get_origin(hint)
        if origin is Annotated:
            args = get_args(hint)
            if not args:
                return False
            hint = args[0]
            continue
        if origin is Union or origin is types.UnionType:
            if type(None) in get_args(hint):
                return True
            return False
        return False
