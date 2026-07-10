"""Annotation helpers for schema bridges (Alembic, etc.)."""

from __future__ import annotations

import datetime as _dt
import re
import types
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Annotated, Any, Union, get_args, get_origin
from uuid import UUID


def _strip_optional_union(hint: Any) -> Any:
    """Unwrap ``T | None`` to ``T`` (same as ``ModelMetaclass``)."""
    while True:
        origin = get_origin(hint)
        if origin is Union or origin is types.UnionType:
            args = get_args(hint)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                hint = non_none[0]
                continue
        return hint


def enum_subclass_from_annotation(hint: Any) -> type[Enum] | None:
    hint = _strip_optional_union(hint)
    if get_origin(hint) is Annotated:
        args = get_args(hint)
        if args:
            return enum_subclass_from_annotation(args[0])
        return None
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint
    return None


def annotation_is_decimal(hint: Any) -> bool:
    hint = _strip_optional_union(hint)
    if get_origin(hint) is Annotated:
        args = get_args(hint)
        if args:
            return annotation_is_decimal(args[0])
        return False
    return hint is Decimal


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


# ---------------------------------------------------------------------------
# db_type canonical vocabulary (U2 of configurable-column-storage-types plan).
#
# This vocabulary is the source of truth consumed by both the Alembic bridge
# (Python) and the Rust runtime emitter. Adding a token here without wiring
# both emitters in the same change breaks AGENTS.md I-1 (cross-emitter DDL
# parity). See docs/plans/2026-05-13-001-feat-configurable-column-storage-
# types-plan.md and docs/solutions/patterns/cross-emitter-ddl-parity.md.
# ---------------------------------------------------------------------------

CANONICAL_DB_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "smallint",
        "int",
        "bigint",
        "uuid",
        "timestamp",
        "timestamptz",
        "date",
        "time",
    }
)

_VARCHAR_RE = re.compile(r"^varchar\((\d+)\)$")


def is_valid_db_type_token(token: str) -> bool:
    """True when ``token`` is in the canonical vocabulary or a well-formed ``varchar(N)``."""
    if token in CANONICAL_DB_TYPES:
        return True
    match = _VARCHAR_RE.match(token)
    if match is None:
        return False
    return int(match.group(1)) > 0


def _strip_optional_and_annotated(annotation: Any) -> Any:
    """Unwrap ``Annotated[...]`` and ``T | None`` down to the inner Python type."""
    hint: Any = annotation
    while True:
        alias_value = getattr(hint, "__value__", None)
        if alias_value is not None:
            hint = alias_value
            continue
        origin = get_origin(hint)
        if origin is Annotated:
            args = get_args(hint)
            if args:
                hint = args[0]
                continue
            return hint
        if origin is Union or origin is types.UnionType:
            non_none = [a for a in get_args(hint) if a is not type(None)]
            if len(non_none) == 1:
                hint = non_none[0]
                continue
        return hint


def _enum_value_kind(enum_cls: type[Enum]) -> str:
    """Classify an Enum subclass by the value type of its first member.

    Returns ``"int"`` for IntEnum-shaped enums, ``"str"`` for StrEnum or
    string-valued enums, and ``"mixed"`` otherwise.
    """
    if issubclass(enum_cls, IntEnum):
        return "int"
    members = list(enum_cls)
    if not members:
        return "mixed"
    sample = members[0].value
    if isinstance(sample, int):
        return "int"
    if isinstance(sample, str):
        return "str"
    return "mixed"


# Compatibility matrix: every canonical token maps to a predicate over the
# resolved Python annotation. Predicates take the stripped (no Optional,
# no Annotated) inner hint.

_STRING_FAMILY_TOKENS = {"text"}


def _is_string_family(hint: Any) -> bool:
    if hint is str:
        return True
    if isinstance(hint, type) and issubclass(hint, Enum):
        return _enum_value_kind(hint) == "str"
    if hint is UUID:
        # uuid stored as text is the canonical portable-storage move
        return True
    return False


def _is_int_family(hint: Any) -> bool:
    if hint is int:
        return True
    if isinstance(hint, type) and issubclass(hint, Enum):
        return _enum_value_kind(hint) == "int"
    return False


def _is_uuid(hint: Any) -> bool:
    return hint is UUID


def _is_datetime(hint: Any) -> bool:
    return hint is _dt.datetime


def _is_date(hint: Any) -> bool:
    # datetime is a subclass of date; treat them as distinct.
    return hint is _dt.date


def _is_time(hint: Any) -> bool:
    return hint is _dt.time


def validate_db_type_declaration(
    field_name: str, db_type: Any, annotation: Any
) -> str | None:
    """Validate a ``db_type`` declaration from either declaration path (ADR-0003).

    Runs at spec compilation — the single site where the ferro path
    (``Field``/``FerroField``) and the raw path (``json_schema_extra``)
    converge — so incoherent declarations raise ``TypeError`` at
    class-definition time regardless of declaration syntax.

    Returns the normalized token, or ``None`` when the declaration is absent
    (``None`` or empty string).
    """
    if db_type is None or db_type == "":
        return None
    if not isinstance(db_type, str):
        raise TypeError(
            f"Field '{field_name}' db_type must be a string token, "
            f"got {type(db_type).__name__}."
        )
    if not is_valid_db_type_token(db_type):
        valid = ", ".join(sorted(CANONICAL_DB_TYPES))
        raise TypeError(
            f"Field '{field_name}' db_type={db_type!r} is not in the "
            f"canonical vocabulary. Valid tokens: {valid}, varchar(N)."
        )
    if annotation is not None and not db_type_is_compatible(db_type, annotation):
        raise TypeError(
            f"Field '{field_name}' db_type={db_type!r} is incompatible "
            f"with annotation {annotation!r}. See the canonical "
            f"compatibility matrix in src/ferro/_annotation_utils.py."
        )
    return db_type


def db_type_is_compatible(token: str, annotation: Any) -> bool:
    """True if ``token`` is a legal storage choice for the Python ``annotation``.

    Caller must have already verified ``is_valid_db_type_token(token)``.
    """
    hint = _strip_optional_and_annotated(annotation)
    if token in _STRING_FAMILY_TOKENS or _VARCHAR_RE.match(token):
        return _is_string_family(hint)
    if token in {"smallint", "int", "bigint"}:
        return _is_int_family(hint)
    if token == "uuid":
        return _is_uuid(hint)
    if token in {"timestamp", "timestamptz"}:
        return _is_datetime(hint)
    if token == "date":
        return _is_date(hint)
    if token == "time":
        return _is_time(hint)
    return False


def is_closed_domain_annotation(annotation: Any) -> bool:
    """True if the annotation is a closed-domain type eligible for ``db_check``.

    Phase 1 ships ``enum.Enum`` subclasses only. ``Literal[...]`` support is
    deferred (see plan U2 Open Questions).
    """
    hint = _strip_optional_and_annotated(annotation)
    return isinstance(hint, type) and issubclass(hint, Enum)
