"""The metaclass that turns a model class declaration into a registered table."""

import __future__
import ast
import re
import sys
import warnings
from dataclasses import replace
from functools import partial
from enum import Enum
from types import UnionType
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    ForwardRef,
    Optional,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import AfterValidator, BaseModel, SecretBytes, SecretStr, ValidationError
from pydantic import Field as PydanticField
from pydantic.fields import FieldInfo
from pydantic_core import PydanticCustomError, PydanticUndefined

from ._annotation_eval import DeferredNames, evaluate_foreign_key_annotation
from ._core import prune_stale_relations
from ._annotation_utils import (
    _strip_optional_union as _annotation_utils_strip_optional_union,
)
from ._annotation_utils import annotation_allows_none, is_closed_domain_annotation
from ._shadow_fk_types import shadow_annotation_for_foreign_key
from .base import (
    FerroField,
    ForeignKey,
    ForwardRelation,
    ManyToManyRelation,
    ReverseRelation,
)
from .columns import build_column_specs, refuse_back_ref_over_retired_relation
from .fields import (
    _option_text,
    _RelationOptions,
    annotation_text,
    field_line,
    field_options,
)
from .ir import compile_model_schema_ir
from .ir.compiler import declared_retired_labels
from .query import Relation
from .registry import REGISTRY
from .relations import (
    check_declared_foreign_keys,
    key_column_line,
    relation_line,
    relation_refusal,
    written_type,
)
from .relations.descriptors import ForwardDescriptor

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")


def _assert_weakref_support(cls: type) -> None:
    """Reject model classes whose instances cannot be weakly referenced.

    The identity map holds weak references to instances (FF-D D1). A class
    that suppresses ``__weakref__`` would force a silent strong-ref fallback,
    which is exactly the unbounded-memory failure F3 removed — so it fails
    loudly at class definition time instead.
    """
    if not any("__weakref__" in getattr(base, "__dict__", {}) for base in cls.__mro__):
        raise TypeError(
            f"{cls.__name__} instances do not support weak references "
            "(no __weakref__ slot in the MRO). Ferro model instances must be "
            "weakly referenceable for identity mapping; remove __slots__ "
            "declarations that suppress __weakref__."
        )


def _type_name(annotation: Any) -> str:
    """``str`` for ``str``, ``Team`` for ``Team`` or ``"Team"``."""
    if isinstance(annotation, ForwardRef):
        return annotation.__forward_arg__
    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__name__", None) or repr(annotation)


def _retired_not_optional(model: str, field_name: str, annotation: Any) -> TypeError:
    return TypeError(
        f"{model}.{field_name} is retired but not optional: declare it "
        f"`{_type_name(annotation)} | None`"
    )


def _retired_not_nullable(model: str, field_name: str) -> TypeError:
    return TypeError(
        f"{model}.{field_name} is retired but declared `nullable=False`: a retired "
        "column stays nullable; drop `nullable=False`"
    )


def _retired_primary_key(model: str, field_name: str) -> TypeError:
    return TypeError(
        f"{model}.{field_name}: a primary key cannot be retired; drop "
        "`retired=True`. To remove the key, change the primary key first: see "
        "the Migrations docs § Changing a primary key"
    )


def _retired_and_renamed(model: str, field_name: str) -> TypeError:
    return TypeError(
        f"{model}.{field_name}: retire it or rename it, not both; drop "
        "`renamed_from` once its migration is generated"
    )


def _reverse_relation_retired(model: str, field_name: str, m2m: bool) -> TypeError:
    if m2m:
        return TypeError(
            f"{model}.{field_name}: a ManyToMany cannot be retired; it is a join "
            "table, not a column. Delete the declaration to drop the join table"
        )
    return TypeError(
        f"{model}.{field_name}: a BackRef cannot be retired; it is not a column. "
        "Delete the BackRef"
    )


# Each field option a `json_schema_extra` key once declared, and the `Field()`
# argument that declares it now.
_SCHEMA_EXTRA_OPTIONS = {
    "primary_key": "primary_key",
    "unique": "unique",
    "index": "index",
    "autoincrement": "autoincrement",
    "db_type": "db_type",
    "db_check": "db_check",
    "ferro_nullable": "nullable",
}

# The key the old `Field()` wrote its options under: a dict keyed by the
# `Field()` arguments themselves.
_OLD_FIELD_OPTIONS_KEY = "ferro_field"


def _refuse_options_in_schema_extra(
    model: str, field_name: str, field_info: FieldInfo
) -> None:
    """Refuse a field option given through ``json_schema_extra``.

    The options live in the field's metadata, so a key there would be printed
    into the JSON schema and read by nothing.
    """
    extra = field_info.json_schema_extra
    if not isinstance(extra, dict):
        return
    for key, value in extra.items():
        if key == _OLD_FIELD_OPTIONS_KEY:
            arguments = (
                ", ".join(f"{option}={given!r}" for option, given in value.items())
                if isinstance(value, dict) and value
                else "its options"
            )
        elif key in _SCHEMA_EXTRA_OPTIONS:
            arguments = f"{_SCHEMA_EXTRA_OPTIONS[key]}={value!r}"
        else:
            continue
        raise TypeError(
            f"{model}.{field_name} declares {key!r} in json_schema_extra, "
            f"which Ferro does not read. Pass {arguments} to Field() "
            "instead of json_schema_extra"
        )


def _parsed(text: str) -> ast.expr | None:
    """``text`` as an expression node, or ``None`` when it does not parse."""
    try:
        return ast.parse(text, mode="eval").body
    except SyntaxError:
        return None


def _head(node: ast.expr) -> str | None:
    """The name a node spells: ``Team`` for ``Team`` or ``models.Team``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_none(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and node.value is None) or _head(
        node
    ) == "None"


def _union_members(node: ast.expr) -> list[ast.expr] | None:
    """The members of ``A | B``, ``Optional[A]`` or ``Union[A, B]``, else ``None``."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*(_union_members(node.left) or [node.left]), node.right]
    if isinstance(node, ast.Subscript) and _head(node.value) == "Optional":
        return [node.slice, ast.Constant(None)]
    if isinstance(node, ast.Subscript) and _head(node.value) == "Union":
        inner = node.slice
        return list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
    return None


def _node_view(node: ast.expr | None) -> tuple[bool, Any, bool]:
    """``(is_list, target name, nullable)`` of a relation argument written as text."""
    if node is None:
        return False, "", False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _node_view(_parsed(node.value))
    if isinstance(node, ast.Subscript) and _head(node.value) in ("list", "List"):
        _, target, _ = _node_view(node.slice)
        return True, target, False
    members = _union_members(node)
    if members is not None:
        kept = [member for member in members if not _is_none(member)]
        _, target, _ = _node_view(kept[0] if kept else None)
        return False, target, len(kept) != len(members)
    return False, _head(node) or ast.unparse(node), False


def _relation_view(hint: Any) -> tuple[bool, Any, bool] | None:
    """What a `Relation[...]` annotation declares: ``(is_list, target, nullable)``.

    The target is a class, or its name when the annotation writes it as a
    string (as every annotation is under ``from __future__ import
    annotations``). ``None`` when the annotation is not ``Relation[...]``.
    """
    if isinstance(hint, ForwardRef):
        hint = hint.__forward_arg__
    if isinstance(hint, str):
        node = _parsed(hint)
        if not isinstance(node, ast.Subscript) or _head(node.value) != "Relation":
            return None
        return _node_view(node.slice)
    if get_origin(hint) is not Relation or not get_args(hint):
        return None
    return _relation_argument(get_args(hint)[0])


def _relation_argument(argument: Any) -> tuple[bool, Any, bool]:
    if isinstance(argument, ForwardRef):
        argument = argument.__forward_arg__
    if isinstance(argument, str):
        return _node_view(_parsed(argument))
    if get_origin(argument) is list:
        inner = get_args(argument)
        _, target, _ = _relation_argument(inner[0] if inner else "")
        return True, target, False
    if get_origin(argument) in (Union, UnionType):
        members = [arg for arg in get_args(argument) if arg is not type(None)]
        _, target, _ = _relation_argument(members[0])
        return False, target, len(members) != len(get_args(argument))
    return False, argument, False


def _evaluated(hint: Any, namespace: dict, frame_ns: dict | None) -> Any:
    """``hint`` as Python evaluates it: a string annotation in the class's scope.

    A name not defined yet becomes a ``ForwardRef``, so a target declared
    later still evaluates, and an alias (``Annotated as A``) is the object it
    names. A quoted annotation in a postponed module is text twice over, so
    text is evaluated until it is not. ``hint`` itself when it is not a string
    or does not evaluate.
    """
    module_ns = getattr(
        sys.modules.get(namespace.get("__module__", "")), "__dict__", {}
    )
    names = DeferredNames({**(frame_ns or {}), **namespace}, module_ns)
    for _ in range(2):
        if not isinstance(hint, str):
            break
        try:
            # Read for its markers only: the class's own evaluation reports.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                hint = eval(hint, module_ns, names)
        except Exception:
            break
    return hint


def _layers(annotation: Any) -> tuple[Any, bool, list[Any]]:
    """The annotation under its ``| None`` and ``Annotated[...]`` wrappers.

    Returns:
        ``(core, wrapped_in_none, metadata)``: ``Annotated[Relation[Team],
        "doc"] | None`` is ``(Relation[Team], True, ["doc"])``.
    """
    wrapped, metadata = False, []
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            args = get_args(annotation)
            metadata += args[1:]
            annotation = args[0]
            continue
        if origin in (Union, UnionType):
            args = get_args(annotation)
            members = [arg for arg in args if arg is not type(None)]
            if len(members) == 1 and len(members) < len(args):
                wrapped = True
                annotation = members[0]
                continue
        return annotation, wrapped, metadata


def _relation_in_metadata(metadata: list[Any]) -> tuple[str, str] | None:
    """The ``(spelling, column)`` of a ``Forward``/``Reverse`` in ``Annotated`` metadata."""
    for item in metadata:
        if not isinstance(item, FieldInfo):
            continue
        for option in item.metadata:
            if isinstance(option, _RelationOptions):
                for spelling in ("Forward", "Reverse"):
                    if spelling.lower() in option.options:
                        return spelling, option.options[spelling.lower()]
    return None


def _is_bare_relation(core: Any) -> bool:
    if core is Relation:
        return True
    return isinstance(core, str) and _head(_parsed(core) or ast.Constant(0)) == (
        "Relation"
    )


def _foreign_key_name(foreign_key: Any) -> str:
    """The class name a foreign key or relation target is written as."""
    if isinstance(foreign_key, ForwardRef):
        foreign_key = foreign_key.__forward_arg__
    if isinstance(foreign_key, type):
        return foreign_key.__name__
    return str(foreign_key).rsplit(".", 1)[-1]


def _declared_target(
    column: str, namespace: dict, annotations: dict, frame_ns: dict | None
) -> tuple[str, bool] | None:
    """The target and nullability a column of the class body declares, if any."""
    annotation = _evaluated(annotations.get(column), namespace, frame_ns)
    core, wrapped, metadata = _layers(annotation)
    options = []
    value = namespace.get(column)
    if isinstance(value, FieldInfo):
        options += value.metadata
    for item in metadata:
        options += item.metadata if isinstance(item, FieldInfo) else [item]
    for option in options:
        if isinstance(option, FerroField) and option.foreign_key is not None:
            nullable = wrapped or (
                not isinstance(core, str) and annotation_allows_none(core)
            )
            return _foreign_key_name(option.foreign_key), nullable
    return None


def _model_pointing_back(model: str, column: str) -> str | None:
    """The one registered model whose ``column``, a foreign-key column or a
    many-to-many, points at ``model``, by name."""
    found = [
        other.__name__
        for other in REGISTRY.models().values()
        if (
            (meta := (other.__dict__.get("__ferro_fields__") or {}).get(column))
            is not None
            and meta.foreign_key is not None
            and _foreign_key_name(meta.foreign_key) == model
        )
        or (
            # A many-to-many never links its own model.
            other.__name__ != model
            and isinstance(
                linked := (other.__dict__.get("__ferro_relations__") or {}).get(column),
                ManyToManyRelation,
            )
            and linked.to is not None
            and _foreign_key_name(linked.to) == model
        )
    ]
    return found[0] if len(found) == 1 else None


_NOT_RELATION = "a relation is annotated Relation[...]"
_IN_METADATA = "a relation is an assignment, not Annotated metadata"
_NONE_OUTSIDE = "a relation is annotated Relation[...], with no | None around it"


def _declared_relation(
    model: str,
    field_name: str,
    annotation: Any,
    spelling: str,
    column: str,
    *,
    in_metadata: bool,
    namespace: dict,
    annotations: dict,
    frame_ns: dict | None,
    scope: frozenset[str] | None,
) -> ForwardRelation | ReverseRelation:
    """The record of a ``Forward`` or ``Reverse`` declaration over ``column``.

    A declaration that is not ``Relation[...]`` assigned the marker is
    refused with the line to write. When the class body cannot name the
    target that line needs (a bare ``Relation`` whose target is declared
    later), the record carries the refusal and relation resolution raises
    it with the target named.

    Args:
        model: The model's name.
        field_name: The relation's field name.
        annotation: The field's annotation, evaluated where it could be.
        spelling: ``"Forward"`` or ``"Reverse"``.
        column: The column the relation names.
        in_metadata: Whether the marker sits in ``Annotated`` metadata.
        namespace: The class body.
        annotations: The class body's annotations.
        frame_ns: The scope running the class statement, if a function.
        scope: The names defined where the class statement runs, so a fix
            line quotes a target defined later; ``None`` when the module
            postpones its annotations and no name needs quotes.

    Raises:
        TypeError: The relation is ``Annotated`` metadata, the annotation is
            not ``Relation[...]`` or wraps it in ``| None``, or it is not the
            shape the relation reads (one row forward, a list in reverse).
    """
    core, wrapped, _ = _layers(annotation)
    view = _relation_view(core)
    bound = None if scope is None else scope | frozenset(namespace)
    # The column the relation reads decides its target and nullability.
    declared = (
        _declared_target(column, namespace, annotations, frame_ns)
        if spelling == "Forward"
        else None
    )
    if in_metadata or wrapped or view is None:
        if in_metadata:
            reason = _IN_METADATA
        elif view is None:
            reason = _NOT_RELATION
        else:
            reason = _NONE_OUTSIDE
        if declared is not None:
            named: tuple[str, bool] | None = declared
        elif view is not None:
            _, target, nullable = view
            named = (annotation_text(target), nullable or wrapped)
        elif not _is_bare_relation(core):
            named = (annotation_text(core), wrapped)
        elif spelling == "Reverse":
            pointing_back = _model_pointing_back(model, column)
            named = (pointing_back, False) if pointing_back else None
        else:
            named = None
        if named is not None:
            raise relation_refusal(
                model, field_name, reason, spelling, column, named[0], named[1], bound
            )
        if spelling == "Forward":
            return ForwardRelation(column, None, False, refusal=reason, bound=bound)
        return ReverseRelation(column, None, refusal=reason, bound=bound)
    is_list, target, nullable = view
    if spelling == "Forward" and is_list:
        target_text, nullable = declared or (annotation_text(target), False)
        raise TypeError(
            f"{model}.{field_name}: a forward relation reads one row, so it is "
            "annotated Relation[...] of its target. Write: "
            + relation_line(field_name, "Forward", target_text, nullable, column, bound)
        )
    if spelling == "Reverse" and not is_list:
        target_text = annotation_text(target)
        raise TypeError(
            f"{model}.{field_name}: a reverse relation reads every row pointing "
            f"back, so it is annotated Relation[list[...]]. Write: "
            + relation_line(field_name, "Reverse", target_text, False, column, bound)
        )
    if spelling == "Forward":
        return ForwardRelation(column, target, nullable, bound=bound)
    return ReverseRelation(column, target, bound=bound)


_NOT_LIST = (
    "a many-to-many reads a list of linked rows, so it is annotated Relation[list[...]]"
)
_NO_THROUGH = "a many-to-many needs through=, the name of its join table"


def _declared_many_to_many(
    model: str,
    field_name: str,
    annotation: Any,
    options: dict[str, Any],
    *,
    in_metadata: bool,
    table: str,
    namespace: dict,
    scope: frozenset[str] | None,
) -> ManyToManyRelation:
    """The record of a ``ManyToMany(through=...)`` declaration.

    A declaration that is not ``Relation[list[...]]`` assigned the marker,
    or names no join table, carries why as its refusal; a missing
    ``through=`` is recorded as the name the old form derived,
    ``<table>_<field>``, so a conversion keeps its table. The refusal is
    raised once the class exists, by `many_to_many_refusal`, which also
    refuses a target naming the model itself; a bare ``Relation`` names no
    target, and relation resolution raises it.

    Args:
        model: The model's name.
        field_name: The relation's field name.
        annotation: The field's annotation, evaluated where it could be.
        options: The relation keywords the marker carries.
        in_metadata: Whether the marker sits in ``Annotated`` metadata.
        table: The model's table name.
        namespace: The class body.
        scope: The names defined where the class statement runs, or ``None``
            when the module postpones its annotations.

    Returns:
        The relation, its ``refusal`` set when the declaration is refused.
    """
    core, wrapped, _ = _layers(annotation)
    view = _relation_view(core)
    through = options.get("through")
    reverse_index = options.get("reverse_index", True)
    # The order of `_declared_relation`: a missing `Relation[...]` is named
    # before the `| None` around it.
    if in_metadata:
        reason = _IN_METADATA
    elif view is None:
        reason = _NOT_RELATION
    elif wrapped:
        reason = _NONE_OUTSIDE
    elif not view[0]:
        reason = _NOT_LIST
    elif not isinstance(through, str) or not through:
        reason = _NO_THROUGH
    else:
        reason = None
    if not isinstance(through, str) or not through:
        through = f"{table}_{field_name}"
    if view is not None:
        target = view[1]
    elif not _is_bare_relation(core):
        target = _relation_argument(core)[1]
    else:
        target = None
    relation = ManyToManyRelation(
        through=through,
        reverse_index=reverse_index,
        refusal=reason,
        bound=None if scope is None else scope | frozenset(namespace),
    )
    relation.to = target
    return relation


def _refuse_foreign_key_options(cls: type, declared_fields: dict) -> None:
    """Refuse ``on_delete`` on a column with no ``foreign_key``, naming the fix.

    A foreign-key column's own refusals (the primary key, generated,
    ``on_delete="SET NULL"`` on a required column) are `check_key_column`'s,
    which writes its fix from the target key.

    Raises:
        TypeError: ``on_delete`` without ``foreign_key``.
    """
    for name, meta in declared_fields.items():
        if meta.foreign_key is None and meta.on_delete is not None:
            info = cls.model_fields[name]
            line = field_line(
                name,
                annotation_text(info.annotation),
                field_options(meta, on_delete=None),
                info.default,
            )
            raise TypeError(
                f"{cls.__name__}.{name}: on_delete is a foreign-key option and "
                f"this column has no foreign_key. Write: {line}"
            )


def _defined_names(frame: Any) -> frozenset[str] | None:
    """The names defined where a class statement runs, read from its frame.

    ``None`` when the module postpones its annotations
    (``from __future__ import annotations``): every annotation stays text
    there, so a fix line never needs to quote a target. A whole annotation
    quoted in a module without the import is evaluated, and gets quotes.
    """
    if frame.f_code.co_flags & __future__.annotations.compiler_flag:
        return None
    return frozenset(frame.f_globals) | frozenset(frame.f_locals)


class SecretHydrator:
    """A hydration converter that wraps a decoded value in its secret type.

    The database stores what the secret hides, so a ``SecretStr`` column hands
    back ``str`` and a ``SecretBytes`` column ``bytes``; any other value is
    corrupt data and is refused. The refusal names the types only, never the
    value, which may be the secret itself.
    """

    __slots__ = ("secret_class", "_stored_type")

    def __init__(self, secret_class: type[SecretStr] | type[SecretBytes]) -> None:
        self.secret_class = secret_class
        self._stored_type = bytes if issubclass(secret_class, SecretBytes) else str

    def __call__(self, value: Any) -> SecretStr | SecretBytes:
        if type(value) is not self._stored_type:
            raise TypeError(
                f"{self.secret_class.__name__} stores {self._stored_type.__name__}, "
                f"but the column holds {type(value).__name__}"
            )
        return self.secret_class(value)

    def __repr__(self) -> str:
        return f"SecretHydrator({self.secret_class.__name__})"


def _written_retired_label(value: Any, labels: frozenset[str]) -> str | None:
    """The retired label ``value`` would write, or ``None``.

    Compared by value: a ``StrEnum`` member, a plain ``Enum`` member and the
    raw label string all write the member's value, so each is caught.
    """
    raw = value.value if isinstance(value, Enum) else value
    if isinstance(raw, str) and raw in labels:
        return raw
    return None


def refuse_retired_label(
    enum_cls: type[Enum], labels: frozenset[str], value: Any
) -> Any:
    """Refuse a value that would write one of an enum's retired labels.

    Every path that writes an enum column refuses a retired label the same
    way: Pydantic validation calls this function, and
    [`refuse_retired_label_write`][ferro.metaclass.refuse_retired_label_write]
    calls it for the writes that skip validation. Each surfaces as a
    `ValidationError` of type `retired_enum_label`. See
    [Retiring an enum label](../guide/schema/renames-and-retirements.md#retiring-an-enum-label).

    Args:
        enum_cls: The column's enum class.
        labels: The enum's retired labels.
        value: The value being written: a member or a raw label.

    Returns:
        `value`, unchanged, when it writes no retired label.

    Raises:
        PydanticCustomError: The value writes a retired label; the error
            names the member.
    """
    label = _written_retired_label(value, labels)
    if label is None:
        return value
    member = next(m for m in enum_cls if m.value == label)
    raise PydanticCustomError(
        "retired_enum_label",
        f"{enum_cls.__name__}.{member.name} ('{label}') is retired in this release: "
        "the previous release may still write it, this one does not",
    )


def refuse_retired_label_write(model_cls: type, column: str, value: Any) -> None:
    """Refuse a retired enum label written without Pydantic validation.

    Assigning an attribute (without `validate_assignment`) and `update()`
    write a column without validating it, so they check here and raise the
    `ValidationError` that validation would have raised.

    Args:
        model_cls: The model class being written.
        column: The column being written.
        value: The value being written.

    Raises:
        ValidationError: The value writes a retired label of the column's
            enum, with error type `retired_enum_label` at `(column,)`.
    """
    enum_cls = getattr(model_cls, "_enum_fields", {}).get(column)
    if enum_cls is None:
        return
    try:
        refuse_retired_label(enum_cls, declared_retired_labels(enum_cls), value)
    except PydanticCustomError as refusal:
        raise ValidationError.from_exception_data(
            model_cls.__name__,
            [{"type": refusal, "loc": (column,), "input": value}],
        ) from None


def _guarded_setattr(guarded: frozenset[str], inherited: Any):
    def __setattr__(self: BaseModel, name: str, value: Any) -> None:
        if name in guarded:
            refuse_retired_label_write(type(self), name, value)
        inherited(self, name, value)

    return __setattr__


def generated_refusal(model_cls: type, name: str) -> str:
    """The sentence a write to a generated column is refused with."""
    inputs = ", ".join(sorted(model_cls.__ferro_generated__[name]))
    return (
        f"{model_cls.__name__}.{name} is a generated column: the database "
        f"computes it from {inputs} on every write"
    )


def _generated_setattr(inherited: Any):
    """Refuse assigning a generated field; clear it when an input is assigned.

    The instance holds the value the database returned. Once a column the
    expression reads changes, that value is stale, so the field reads `None`
    until the next save reads it back (ADR-0058).
    """

    def __setattr__(self: BaseModel, name: str, value: Any) -> None:
        generated = type(self).__ferro_generated__
        if name in generated:
            raise TypeError(
                f"{generated_refusal(type(self), name)}; assign those columns "
                "and save() instead"
            )
        inherited(self, name, value)
        for field_name, inputs in generated.items():
            if name in inputs:
                self.__dict__[field_name] = None

    return __setattr__


def _resolved_relation_specs(model_cls: type, relations: Any) -> dict:
    """The model's relation specs, once every forward relation in `relations` has one.

    A relation whose target is a class-name string gets its spec when the
    modelset resolves, so the first assignment before `connect()` resolves it.
    """
    specs = model_cls.__ferro_relation_specs__
    if not specs.keys() >= relations:
        from . import ensure_resolved_modelset

        ensure_resolved_modelset()
        specs = model_cls.__ferro_relation_specs__
    return specs


def _a(name: str) -> str:
    """`name` after its indefinite article: "a Team", "an Author", "a User"."""
    first, rest = name[:1].upper(), name[1:3].lower()
    vowel = first in "AEIO" or (
        first == "U" and not (rest[:1] not in "aeiou" and rest[1:2] in "aeiou")
    )
    return f"{'an' if vowel else 'a'} {name}"


def _foreign_key_text(declared: ForeignKey) -> str:
    """A `ForeignKey(...)` as written, its default options left out."""
    defaults = {"on_delete": "CASCADE", "unique": False, "index": False}
    options = [
        f"{option}={_option_text(getattr(declared, option))}"
        for option in ("related_name", "on_delete", "unique", "index", "renamed_from")
        if getattr(declared, option) not in (None, defaults.get(option))
    ]
    return f"ForeignKey({', '.join(options)})"


def _none_refusal(model_cls: type, name: str, spec: Any) -> TypeError:
    """The refusal of `None` on a relation over a required key column, with the lines to write."""
    model = model_cls.__name__
    target = spec.target.__name__
    declared = model_cls.__ferro_relations__[name]
    if isinstance(declared, ForwardRelation):
        info = model_cls.model_fields[spec.fk_column]
        column = key_column_line(
            model_cls,
            spec.fk_column,
            spec.target,
            nullable=True,
            meta=model_cls.__ferro_fields__[spec.fk_column],
            annotation=written_type(info.annotation),
        )
        relation = relation_line(
            name, "Forward", target, True, spec.fk_column, declared.bound
        )
        fix = (
            f"add | None to {spec.fk_column}'s annotation. Write: {column} "
            f"and {relation}"
        )
    else:
        # The old form has no key column to edit: the relation's own
        # annotation decides whether the column allows NULL.
        written = declared.relation_annotation
        shown = f'"{target}"' if isinstance(written, (str, ForwardRef)) else target
        fix = (
            f"add | None to {name}'s annotation. Write: {name}: "
            f"Annotated[{shown}, {_foreign_key_text(declared)}] | None = None"
        )
    return TypeError(
        f"{model}.{name}: {spec.fk_column} is not nullable, so the relation "
        f"cannot be None. Assign {_a(target)}, or {fix}"
    )


def _assign_relation(self: BaseModel, name: str, value: Any, relations: Any) -> None:
    """Set the foreign-key column behind a forward relation and cache `value` as loaded.

    Every refusal raises before the key column or the cache changes.
    """
    model = type(self).__name__
    spec = _resolved_relation_specs(type(self), relations)[name]
    target = spec.target.__name__
    if value is None:
        if not spec.nullable:
            raise _none_refusal(type(self), name, spec)
        key = None
    # The exact class: a subclass model has a table of its own, so its key
    # would name another table's row.
    elif type(value) is not spec.target:
        raise TypeError(
            f"{model}.{name}: expected {_a(target)}, got {type(value).__name__}. "
            f"Assign a saved {target} instance, or set {spec.fk_column} directly"
        )
    else:
        pk = spec.target.__ferro_pk__
        key = getattr(value, pk)
        if key is None:
            raise ValueError(
                f"{model}.{name}: the {target} has no {pk} yet. Save it first, "
                "then assign it"
            )
    setattr(self, spec.fk_column, key)
    self.__dict__[name] = value


def _relation_setattr(key_columns: dict[str, str], inherited: Any):
    """Assign a forward relation through its key column; drop a stale one when the key changes.

    `key_columns` maps each forward relation to its foreign-key column.
    Assigning the column keeps a loaded relation only while the key still
    points at it, the refresh rule of ADR-0008.
    """
    keys = frozenset(key_columns.values())

    def __setattr__(self: BaseModel, name: str, value: Any) -> None:
        if name in key_columns:
            _assign_relation(self, name, value, key_columns.keys())
            return
        inherited(self, name, value)
        if name in keys:
            _resolved_relation_specs(type(self), key_columns.keys())
            prune_stale_relations(self)

    return __setattr__


class ModelMetaclass(type(BaseModel)):
    """The metaclass of [`Model`][ferro.Model], which records each model's schema when the class is defined.

    Defining a model class checks its declaration and records its table in
    the model registry. The database learns about it on the next
    `connect()`, `create_tables()` or `migrate()`.
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        # Phase 1: Annotation Processing
        annotations = mcs._resolve_deferred_annotations(namespace)
        namespace["__annotations__"] = annotations

        # The scope that runs the class statement: a model defined inside a
        # function names its sibling targets there, not in the module.
        caller = sys._getframe(1)
        frame_ns = (
            dict(caller.f_locals)
            if caller.f_globals.get("__name__") == namespace.get("__module__")
            and caller.f_locals is not caller.f_globals
            else None
        )
        local_relations, fields_to_remove, pending_relations, retired_relations = (
            mcs._scan_relationship_annotations(
                annotations,
                namespace,
                name,
                frame_ns=frame_ns,
                scope=_defined_names(caller),
            )
        )
        mcs._inject_shadow_fields(annotations, namespace, local_relations)
        mcs._prepare_namespace_for_pydantic(namespace, annotations, fields_to_remove)

        # Phase 2: Class Creation
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # Verify weakref support for identity mapping (FF-D D1)
        _assert_weakref_support(cls)

        # Phase 3: Post-Creation Setup
        if name == "Model":
            return cls

        cls.__ferro_identity__ = f"{cls.__module__}.{cls.__qualname__}"
        cls.__ferro_table__ = mcs._resolve_table_name(name, namespace)
        # Private seam for IR-derived classes (``migrations.historical``): read
        # from the class's own namespace only, never inherited, always set so
        # ``build_column_specs`` can tell a user model from a historical one.
        setattr(
            cls, "__ferro_historical__", namespace.get("__ferro_historical__") is True
        )
        cls.__ferro_renamed_from__ = mcs._resolve_renamed_from(name, namespace)

        # Refusals raise from the schema compile, which itself writes the
        # registry (the envelope, join tables), so no order puts every refusal
        # before every write: a refused class is rolled back out of every
        # store instead, leaving the registry as it was before the class body.
        before = REGISTRY.snapshot()
        try:
            for field_name, metadata in pending_relations:
                REGISTRY.defer_relation(cls.__ferro_identity__, field_name, metadata)

            mcs._register_model(cls, cls.__ferro_identity__, local_relations)
            declared_fields = mcs._parse_ferro_field_metadata(cls)
            cls.__ferro_fields__ = declared_fields
            _refuse_foreign_key_options(cls, declared_fields)
            mcs._validate_db_type_options(cls, declared_fields)
            mcs._validate_generated_options(cls, declared_fields)
            mcs._retire_fields(cls, retired_relations)
            mcs._inject_relation_descriptors(cls, local_relations)
            mcs._generate_and_register_schema(cls, cls.__ferro_identity__)
            check_declared_foreign_keys(cls)

            # Spec-derived runtime attributes (read specs at use time everywhere
            # else; these two bake *names only*, which are stable across epochs).
            cls.__ferro_query_columns__ = frozenset(cls.__ferro_columns__)
            mcs._register_enum_fields(cls)
            mcs._register_hydration_converters(cls)
        except BaseException:
            REGISTRY.restore(before)
            raise
        if cls.__ferro_generated__:
            cls.__setattr__ = _generated_setattr(cls.__setattr__)
        key_columns = {
            field_name: meta.fk_column
            if isinstance(meta, ForwardRelation)
            else f"{field_name}_id"
            for field_name, meta in local_relations.items()
            if isinstance(meta, (ForwardRelation, ForeignKey))
        }
        if key_columns:
            cls.__setattr__ = _relation_setattr(key_columns, cls.__setattr__)

        return cls

    @staticmethod
    def _retire_fields(cls, retired_relations: dict[str, ForeignKey]) -> None:
        """Take the retired fields out of the model (ADR-0053).

        A retired scalar field's column spec is derived here, once, while the
        field is still a Pydantic field (its annotation resolved, its ``db_type``
        and enum read the one way every column's are); then the field leaves
        the Pydantic model, so the model has no attribute, keyword or dump key
        for it. A retired relation never became a field: the class-body scan
        kept it out of ``__ferro_relations__`` and gave it no shadow column.

        Raises:
            TypeError: a retired primary key, a retired field that also
                declares ``renamed_from`` or ``nullable=False``, or one whose
                annotation does not allow ``None`` (each naming the fix).
        """
        model = cls.__name__
        retired = {
            field_name: meta
            for field_name, meta in cls.__ferro_fields__.items()
            if getattr(meta, "retired", False)
        }
        for field_name, meta in retired.items():
            if meta.primary_key:
                raise _retired_primary_key(model, field_name)
            if meta.renamed_from is not None:
                raise _retired_and_renamed(model, field_name)
            if meta.nullable is False:
                raise _retired_not_nullable(model, field_name)
            annotation = cls.model_fields[field_name].annotation
            if not annotation_allows_none(annotation):
                raise _retired_not_optional(model, field_name, annotation)

        retired_fields = {}
        if retired:
            specs = build_column_specs(cls)
            retired_fields = {
                field_name: replace(specs[field_name], nullable=True, retired=True)
                for field_name in retired
            }
            for field_name in retired:
                del cls.__pydantic_fields__[field_name]
                del cls.__ferro_fields__[field_name]
            annotations = dict(cls.__dict__.get("__annotations__", {}))
            for field_name in retired:
                annotations.pop(field_name, None)
            cls.__annotations__ = annotations
            cls.model_rebuild(force=True, raise_errors=False)

        for field_name, meta in retired_relations.items():
            refuse_back_ref_over_retired_relation(cls, field_name, meta)
        cls.__ferro_retired_fields__ = retired_fields
        cls.__ferro_retired_relations__ = retired_relations
        cls.__ferro_retired_names__ = frozenset(
            [
                *retired_fields,
                *retired_relations,
                *(f"{field_name}_id" for field_name in retired_relations),
            ]
        )

    @staticmethod
    def _resolve_table_name(name: str, namespace: dict) -> str:
        """Resolve the physical table name for a model class (FF-E E2).

        ``__ferro_table__`` is honored only when declared in the class's own
        body (the metaclass namespace) — a subclass never silently inherits
        its parent's physical table. Default: ``classname.lower()``.
        """
        configured = namespace.get("__ferro_table__")
        if configured is None:
            return name.lower()
        if not isinstance(configured, str):
            raise TypeError(
                f"__ferro_table__ on model '{name}' must be a str, "
                f"got {type(configured).__name__}"
            )
        if len(configured) > 63 or not _TABLE_NAME_RE.match(configured):
            raise ValueError(
                f"__ferro_table__ {configured!r} on model '{name}' is not a "
                "valid table name: expected a 1-63 character identifier "
                "matching [A-Za-z_][A-Za-z0-9_]*"
            )
        return configured

    @staticmethod
    def _resolve_renamed_from(name: str, namespace: dict) -> str | None:
        """Resolve a model's table rename hint (ADR-0032).

        ``__ferro_renamed_from__`` names the table this model's table was
        called before. Like ``__ferro_table__`` it is honored only in the
        class's own body: a subclass never inherits its parent's rename.
        """
        declared = namespace.get("__ferro_renamed_from__")
        if declared is None:
            return None
        if not isinstance(declared, str):
            raise TypeError(
                f"__ferro_renamed_from__ on model '{name}' must be a str, "
                f"got {type(declared).__name__}"
            )
        if len(declared) > 63 or not _TABLE_NAME_RE.match(declared):
            raise ValueError(
                f"__ferro_renamed_from__ {declared!r} on model '{name}' is not a "
                "valid table name: expected a 1-63 character identifier "
                "matching [A-Za-z_][A-Za-z0-9_]*"
            )
        return declared

    @staticmethod
    def _field_ferro_payload(obj: Any) -> dict[str, Any]:
        """Return the relation keywords a ``Field()`` left in its metadata.

        The payload also carries ``retired`` when the field's ``FerroField``
        sets it, since a retired reverse relation is refused.
        """
        if not isinstance(obj, FieldInfo):
            return {}
        payload: dict[str, Any] = {}
        for metadata in obj.metadata:
            if isinstance(metadata, _RelationOptions):
                payload.update(metadata.options)
            elif isinstance(metadata, FerroField) and metadata.retired:
                payload["retired"] = True
        return payload

    @staticmethod
    def _strip_optional_union(hint: Any) -> Any:
        """Unwrap ``T | None`` / ``Optional[T]`` to ``T`` for relationship detection."""
        return _annotation_utils_strip_optional_union(hint)

    @staticmethod
    def _relationship_marker_from_annotation(hint: Any) -> Any:
        """Inner type used to inspect relationship annotations."""
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            if args:
                return ModelMetaclass._strip_optional_union(args[0])
            return hint
        return ModelMetaclass._strip_optional_union(hint)

    @staticmethod
    def _legacy_back_ref_error(field_name: str) -> TypeError:
        return TypeError(
            f"Field '{field_name}' uses deprecated BackRef[...] annotation syntax. "
            "Use Relation[list[T]] = BackRef() for collection back-references."
        )

    @staticmethod
    def _relationship_field_payload(
        field_name: str, hint: Any, namespace: dict
    ) -> dict[str, Any]:
        """Return relationship metadata supplied by ferro.Field helpers.

        Read from the field's default, then from its ``Annotated`` metadata,
        under any ``| None`` wrapper.
        """
        default_val = namespace.get(field_name)
        payload = ModelMetaclass._field_ferro_payload(default_val)
        if payload:
            return payload

        for item in _layers(hint)[2]:
            payload = ModelMetaclass._field_ferro_payload(item)
            if payload:
                return payload

        return {}

    @staticmethod
    def _relation_target_from_annotation(field_name: str, hint: Any) -> Any:
        """Extract T from Relation[list[T]] for collection relationships."""
        marker = ModelMetaclass._relationship_marker_from_annotation(hint)
        if isinstance(marker, str):
            return ModelMetaclass._relation_target_from_string(field_name, marker)
        if get_origin(marker) is not Relation:
            raise TypeError(
                f"Field '{field_name}' must be annotated as Relation[list[T]] "
                "when using BackRef(), ManyToMany(), or relationship Field flags."
            )

        args = get_args(marker)
        if not args:
            raise TypeError(f"Field '{field_name}' must specify Relation[list[T]].")

        relation_arg = ModelMetaclass._strip_optional_union(args[0])
        if get_origin(relation_arg) is not list:
            raise TypeError(
                f"Field '{field_name}' must use Relation[list[T]] for collection relationships."
            )

        inner_args = get_args(relation_arg)
        if not inner_args:
            raise TypeError(f"Field '{field_name}' must specify Relation[list[T]].")
        return ModelMetaclass._strip_optional_union(inner_args[0])

    @staticmethod
    def _relation_target_from_string(field_name: str, hint: str) -> str:
        """Extract T from a string ``Relation[list[T]]`` annotation."""
        normalized = hint.replace(" ", "")
        prefix = "Relation[list["
        if not normalized.startswith(prefix) or not normalized.endswith("]]"):
            raise TypeError(
                f"Field '{field_name}' must be annotated as Relation[list[T]] "
                "when using BackRef(), ManyToMany(), or relationship Field flags."
            )
        target = normalized[len(prefix) : -2]
        return target.strip("\"'")

    @staticmethod
    def _annotation_is_plain_list(hint: Any) -> bool:
        marker = ModelMetaclass._relationship_marker_from_annotation(hint)
        if get_origin(marker) is list:
            return True
        return isinstance(marker, str) and marker.replace(" ", "").startswith("list[")

    @staticmethod
    def _annotation_looks_like_back_ref(hint: Any) -> bool:
        if isinstance(hint, str) and "BackRef" in hint:
            return True
        if isinstance(hint, ForwardRef) and "BackRef" in hint.__forward_arg__:
            return True
        return False

    @staticmethod
    def _resolve_deferred_annotations(namespace: dict) -> dict[str, Any]:
        """
        Resolve deferred annotations (PEP 649) if present.

        Returns:
            Dictionary of resolved annotations
        """
        # Handle Python 3.14+ deferred annotations
        # We need a complete __annotations__ dict so we can safely modify it.
        if "__annotate_func__" in namespace and "__annotations__" not in namespace:
            try:
                # Format 1: Value (evaluated)
                return namespace["__annotate_func__"](1)
            except Exception as value_error:
                if "BackRef[...]" in str(value_error):
                    raise value_error
                try:
                    # Format 2: ForwardRef (non-evaluated objects)
                    return namespace["__annotate_func__"](2)
                except Exception as forward_error:
                    if "BackRef[...]" in str(forward_error):
                        raise forward_error
                    # #155: was `pass` -> returned {} -> Pydantic then raised the
                    # misleading "non-annotated attribute" on the first class-body
                    # assignment. Surface the REAL cause (PEP 649 deferred-eval
                    # failure) instead of silently dropping every annotation.
                    model = (
                        namespace.get("__qualname__")
                        or namespace.get("__name__")
                        or "<model>"
                    )
                    value_error.add_note(
                        f"ferro: could not resolve deferred annotations for model "
                        f"'{model}' (PEP 649 / Python 3.14). An annotation on this "
                        f"model failed to evaluate; an attribute Pydantic may flag "
                        f"as 'non-annotated' is a symptom, not the cause."
                    )
                    raise value_error

        return namespace.get("__annotations__", {})

    @staticmethod
    def _scan_relationship_annotations(
        annotations: dict,
        namespace: dict,
        model_name: str,
        frame_ns: dict | None = None,
        scope: frozenset[str] | None = None,
    ) -> tuple[dict, list, list, dict]:
        """
        Scan annotations for relationship fields (BackRef, ForeignKey, ManyToMany).

        Returns:
            (local_relations, fields_to_remove, pending_relations,
            retired_relations): Relationship metadata, fields to hide from
            Pydantic, ``(field_name, metadata)`` entries deferred to the
            Registry's pending-relation queue (flushed under the model's
            resolved ``__ferro_identity__`` after the class object exists), and
            the retired ``ForeignKey`` relations (ADR-0053), which leave the
            class body entirely.
        """
        local_relations = {}
        fields_to_remove = []
        pending_relations = []
        retired_relations: dict[str, ForeignKey] = {}

        for field_name, hint in list(annotations.items()):
            if ModelMetaclass._annotation_looks_like_back_ref(hint):
                raise ModelMetaclass._legacy_back_ref_error(field_name)

            # Markers are read from the evaluated annotation, under any `| None`
            # or `Annotated[...]` wrapper, so every spelling is seen with or
            # without postponed annotations.
            evaluated = _evaluated(hint, namespace, frame_ns)
            core, _, field_metadata = _layers(evaluated)
            relationship_payload = ModelMetaclass._relationship_field_payload(
                field_name, evaluated, namespace
            )
            declared = _relation_in_metadata(field_metadata)
            in_metadata = declared is not None
            assigned = ModelMetaclass._field_ferro_payload(namespace.get(field_name))
            for spelling in ("Forward", "Reverse"):
                column = assigned.get(spelling.lower())
                if column is not None and declared is None:
                    declared = (spelling, column)
            if declared is not None:
                spelling, column = declared
                relation = _declared_relation(
                    model_name,
                    field_name,
                    evaluated,
                    spelling,
                    column,
                    in_metadata=in_metadata,
                    namespace=namespace,
                    annotations=annotations,
                    frame_ns=frame_ns,
                    scope=scope,
                )
                local_relations[field_name] = relation
                pending_relations.append((field_name, relation))
                fields_to_remove.append(field_name)
                continue
            is_back_field = relationship_payload.get("back_ref") is True
            is_m2m_field = relationship_payload.get("many_to_many") is True
            if relationship_payload.get("retired") is True and (
                is_back_field or is_m2m_field
            ):
                raise _reverse_relation_retired(model_name, field_name, is_m2m_field)

            if is_back_field and is_m2m_field:
                raise TypeError(
                    f"Field '{field_name}' cannot be both back_ref and many_to_many."
                )

            if is_back_field:
                marker = ModelMetaclass._relationship_marker_from_annotation(core)
                if get_origin(marker) is Relation:
                    ModelMetaclass._relation_target_from_annotation(field_name, core)
                elif ModelMetaclass._annotation_is_plain_list(core):
                    raise TypeError(
                        f"Field '{field_name}' uses a plain list annotation. Use "
                        "Relation[list[T]] = BackRef() for collection back-references."
                    )
                local_relations[field_name] = "BackRef"
                fields_to_remove.append(field_name)
                continue

            if is_m2m_field and relationship_payload.get("related_name") is None:
                relation = _declared_many_to_many(
                    model_name,
                    field_name,
                    evaluated,
                    relationship_payload,
                    in_metadata=assigned.get("many_to_many") is not True,
                    table=ModelMetaclass._resolve_table_name(model_name, namespace),
                    namespace=namespace,
                    scope=scope,
                )
                local_relations[field_name] = relation
                pending_relations.append((field_name, relation))
                fields_to_remove.append(field_name)
                continue

            if is_m2m_field:
                target = ModelMetaclass._relation_target_from_annotation(
                    field_name, core
                )
                related_name = relationship_payload.get("related_name")
                if not related_name:
                    raise TypeError(
                        f"Field '{field_name}' uses many_to_many=True but did not "
                        "provide related_name."
                    )
                metadata = ManyToManyRelation(
                    related_name=related_name,
                    through=relationship_payload.get("through"),
                    reverse_index=relationship_payload.get("reverse_index", True),
                )
                metadata.to = target
                local_relations[field_name] = metadata
                pending_relations.append((field_name, metadata))
                fields_to_remove.append(field_name)
                continue

            if isinstance(hint, str):
                hint = evaluate_foreign_key_annotation(
                    hint,
                    model=model_name,
                    field=field_name,
                    module=namespace.get("__module__", ""),
                    localns=namespace,
                    frame_ns=frame_ns,
                )
            # `Annotated[T, ForeignKey(...)] | None` is the same relation as
            # `Annotated[T | None, ForeignKey(...)]`.
            core = ModelMetaclass._strip_optional_union(hint)
            if get_origin(core) is Annotated:
                args = get_args(core)
                for metadata in args:
                    if isinstance(metadata, ForeignKey):
                        metadata.relation_annotation = (
                            args[0] if core is hint else Optional[args[0]]
                        )
                        inner = ModelMetaclass._strip_optional_union(args[0])
                        metadata.to = inner
                        if metadata.retired:
                            ModelMetaclass._retire_relation(
                                model_name, field_name, metadata, annotations, namespace
                            )
                            retired_relations[field_name] = metadata
                            break
                        local_relations[field_name] = metadata
                        pending_relations.append((field_name, metadata))
                        fields_to_remove.append(field_name)
                        break

                    if metadata.__class__.__name__ == "ManyToManyField":
                        raise TypeError(
                            "ManyToManyField(...) is no longer supported. Use "
                            "Relation[list[T]] = ManyToMany(...)."
                        )

        return local_relations, fields_to_remove, pending_relations, retired_relations

    @staticmethod
    def _retire_relation(
        model_name: str,
        field_name: str,
        metadata: ForeignKey,
        annotations: dict,
        namespace: dict,
    ) -> None:
        """Take a retired ``ForeignKey`` out of the class body (ADR-0053).

        The relation gets no descriptor and no shadow ``*_id`` field, so the
        model has neither attribute; its column is compiled from the
        ``ForeignKey`` alone (`ferro.columns.retired_column_specs`).
        """
        if metadata.renamed_from is not None:
            raise _retired_and_renamed(model_name, field_name)
        if not annotation_allows_none(metadata.relation_annotation):
            raise _retired_not_optional(model_name, field_name, metadata.to)
        if metadata.nullable is False:
            raise _retired_not_nullable(model_name, field_name)
        annotations.pop(field_name, None)
        namespace.pop(field_name, None)

    @staticmethod
    def _inject_shadow_fields(
        annotations: dict, namespace: dict, local_relations: dict
    ) -> None:
        """
        Inject shadow {field_name}_id fields for ForeignKeys.

        When ``ForeignKey.to`` is already a concrete model class, the shadow type is
        derived from that model's primary key annotation; otherwise a broad fallback
        union is used until ``resolve_relationships()`` can reconcile it.

        Mutates annotations and namespace in place.
        """
        for field_name, metadata in local_relations.items():
            if isinstance(metadata, ForeignKey):
                # INJECT SHADOW FIELD into annotations
                id_field = f"{field_name}_id"
                annotations[id_field] = shadow_annotation_for_foreign_key(metadata)
                # Set a default so Pydantic doesn't make it required
                namespace[id_field] = PydanticField(default=None)

    @staticmethod
    def _prepare_namespace_for_pydantic(
        namespace: dict, annotations: dict, fields_to_remove: list
    ) -> None:
        """
        Hide relationship fields from Pydantic by converting them to ClassVars.

        Mutates namespace and annotations in place.
        """
        # Hide relationship fields from Pydantic by converting them to ClassVars
        for field_name in fields_to_remove:
            annotations[field_name] = ClassVar[Any]

        # FOR PYTHON 3.14+: If we evaluated annotations, we MUST remove the func
        # so Pydantic doesn't use it and ignore our modified __annotations__.
        if "__annotate_func__" in namespace:
            del namespace["__annotate_func__"]

    @staticmethod
    def _register_model(cls, identity: str, local_relations: dict) -> None:
        """
        Register model in global registry and compute its queryable-column set.

        Raises:
            RuntimeError: When a *distinct* model already claims this model's
                resolved table name (FF-E E1). Re-registration under the same
                qualified identity is idempotent.
        """
        table_name = cls.__ferro_table__
        for key, other in REGISTRY.models().items():
            if key == identity:
                continue
            if getattr(other, "__ferro_table__", None) == table_name:
                raise RuntimeError(
                    f"Ferro model '{identity}' resolves to table '{table_name}', "
                    f"which is already registered by model '{key}'. Two distinct "
                    "models cannot share a table. Set __ferro_table__ on one of "
                    "them to give it a distinct table name."
                )
        REGISTRY.register(cls)
        cls.__ferro_relations__ = local_relations

    @staticmethod
    def _parse_ferro_field_metadata(cls) -> dict[str, FerroField]:
        """
        Collect each field's database options from its metadata.

        ``Field(...)`` and ``FerroField(...)`` each leave one ``FerroField``
        in the field's ``FieldInfo.metadata``, whether assigned or given inside
        ``Annotated[...]`` (Pydantic merges both into the same ``FieldInfo``).
        The options never live in ``json_schema_extra``: a key there that
        names an option is refused, naming the ``Field()`` argument to use.

        Returns:
            Dictionary mapping field names to FerroField metadata

        Raises:
            TypeError: A field declares its options twice, or its
                ``json_schema_extra`` carries an option key.
        """
        declared_fields = {}
        for f_name, field_info in cls.model_fields.items():
            _refuse_options_in_schema_extra(cls.__name__, f_name, field_info)
            declared = [
                metadata
                for metadata in field_info.metadata
                if isinstance(metadata, FerroField)
            ]
            if len(declared) > 1:
                raise TypeError(
                    f"Field '{f_name}' cannot declare Ferro field metadata twice "
                    "(Annotated[..., FerroField(...)] + ferro.Field(...)); give "
                    "every option to one of them."
                )
            if declared:
                declared_fields[f_name] = declared[0]

        return declared_fields

    @staticmethod
    def _register_enum_fields(cls) -> None:
        """Populate ``cls._enum_fields`` from column-spec python types."""
        enum_fields: dict[str, type[Enum]] = {}
        retired_labels: dict[str, tuple[type[Enum], frozenset[str]]] = {}
        for field_name, spec in cls.__ferro_columns__.items():
            enum_cls = spec.enum_class
            if enum_cls is not None:
                enum_fields[field_name] = enum_cls
                retired = declared_retired_labels(enum_cls)
                if retired and field_name in cls.model_fields:
                    retired_labels[field_name] = (enum_cls, retired)
        cls._enum_fields = enum_fields
        if retired_labels:
            ModelMetaclass._refuse_retired_labels(cls, retired_labels)

    @staticmethod
    def _register_hydration_converters(cls) -> None:
        """Populate ``cls._hydration_converters`` from the column specs.

        Hydration writes each column straight into the instance dict, never
        through ``__init__``, so a field whose declared type is not what the
        database hands back is converted, value by value, by the one entry
        recorded here: an enum field by its enum class, a secret field by a
        `SecretHydrator`, a JSON field the decoder cannot produce by its type
        adapter. Each entry is ``(kind, convert)``; ``kind`` names the
        field in a rejection (``Failed to hydrate enum field ...``).
        """
        converters: dict[str, tuple[str, Callable[[Any], Any]]] = {}
        for field_name, spec in cls.__ferro_columns__.items():
            if spec.enum_class is not None:
                converters[field_name] = ("enum", spec.enum_class)
            elif spec.secret_class is not None:
                converters[field_name] = ("secret", SecretHydrator(spec.secret_class))
            elif spec.json_adapter is not None:
                converters[field_name] = ("json", spec.json_adapter.validate_python)
        cls._hydration_converters = converters

    @staticmethod
    def _refuse_retired_labels(
        cls, retired_labels: dict[str, tuple[type[Enum], frozenset[str]]]
    ) -> None:
        """Refuse writing a retired enum label, field by field (ADR-0053).

        Only the enum fields whose enum declares ``__ferro_retired_labels__``
        gain an ``AfterValidator``: it runs on construction and, under
        ``validate_assignment``, for the assigned field alone. Without
        ``validate_assignment`` the class gets a ``__setattr__`` for the same
        fields. Hydration runs neither, so a row holding the label loads and
        saves back unchanged. A model without retired labels gains nothing.
        """
        for field_name, (enum_cls, labels) in retired_labels.items():
            cls.model_fields[field_name].metadata.append(
                AfterValidator(partial(refuse_retired_label, enum_cls, labels))
            )
        cls.model_rebuild(force=True, raise_errors=False)
        if not cls.model_config.get("validate_assignment"):
            cls.__setattr__ = _guarded_setattr(
                frozenset(retired_labels), cls.__setattr__
            )

    @staticmethod
    def _validate_db_type_options(cls, declared_fields: dict) -> None:
        """Strict validation of ``Field(db_type=..., db_check=...)`` combinations.

        Runs at class-definition time (Phase 3) so incoherent combinations
        surface as ``TypeError`` on import, never as a silent miscoercion at
        query or migration time. See U2 of the configurable-column-storage
        plan.
        """
        try:
            resolved = get_type_hints(cls, include_extras=True)
        except Exception:
            resolved = {}

        model_fields = getattr(cls, "model_fields", {})

        for field_name, metadata in declared_fields.items():
            db_type = getattr(metadata, "db_type", None)
            db_check = getattr(metadata, "db_check", False)
            if db_type is None and not db_check:
                continue

            annotation = resolved.get(field_name)
            if annotation is None:
                field_info = model_fields.get(field_name)
                if field_info is not None:
                    annotation = field_info.annotation

            # Token validity + annotation compatibility validate path-blind at
            # spec compilation (ADR-0003; ``ferro.columns``). Only the
            # ferro-path-only db_check combinations live here.
            if db_check:
                if db_type is None:
                    raise TypeError(
                        f"Field '{field_name}' db_check=True requires db_type to be "
                        f"set. db_check on default (native enum) storage is redundant "
                        f"because the native enum type already enforces values."
                    )
                if annotation is not None and not is_closed_domain_annotation(
                    annotation
                ):
                    raise TypeError(
                        f"Field '{field_name}' db_check=True is only valid for "
                        f"closed-domain types (enum.Enum subclasses). "
                        f"Annotation {annotation!r} is not closed-domain."
                    )

    @staticmethod
    def _validate_generated_options(cls, declared_fields: dict) -> None:
        """Refuse `generated=` beside an option the database cannot honour.

        A generated column's value comes only from its expression, so it has
        no key, sequence, default or enum check of its own.
        """
        for field_name, metadata in declared_fields.items():
            if getattr(metadata, "generated", None) is None:
                continue
            field_info = cls.model_fields[field_name]
            conflicts = [
                option
                for option, declared in (
                    ("primary_key=True", metadata.primary_key),
                    ("autoincrement=True", metadata.autoincrement),
                    ("db_check=True", metadata.db_check),
                    (
                        "default=",
                        field_info.default is not None
                        and field_info.default is not PydanticUndefined
                        or field_info.default_factory is not None,
                    ),
                )
                if declared
            ]
            if conflicts:
                raise TypeError(
                    f"{cls.__name__}.{field_name} is a generated column, so it "
                    f"cannot also take {conflicts[0]}: the database computes "
                    f"its value from the expression; drop {conflicts[0]} or "
                    "generated="
                )

    @staticmethod
    def _inject_relation_descriptors(cls, local_relations: dict) -> None:
        """
        Inject descriptors for relationship fields (ForeignKey, ManyToMany).

        Mutates cls in place.
        """
        for field_name, metadata in local_relations.items():
            if isinstance(metadata, ForwardRelation):
                target = metadata.to
                setattr(
                    cls,
                    field_name,
                    ForwardDescriptor(
                        target_model_name=getattr(target, "__ferro_identity__", target)
                        or "",
                        field_name=field_name,
                        fk_column=metadata.fk_column,
                    ),
                )
            elif isinstance(metadata, ForeignKey):
                target_name = (
                    metadata.to
                    if isinstance(metadata.to, str)
                    else (
                        metadata.to.__name__
                        if hasattr(metadata.to, "__name__")
                        else str(metadata.to)
                    )
                )
                if isinstance(metadata.to, ForwardRef):
                    target_name = metadata.to.__forward_arg__
                target_name = (
                    metadata.to.__ferro_identity__
                    if hasattr(metadata.to, "__ferro_identity__")
                    else target_name
                )

                setattr(
                    cls,
                    field_name,
                    ForwardDescriptor(
                        target_model_name=target_name,
                        field_name=field_name,
                        fk_column=f"{field_name}_id",
                    ),
                )
            else:
                setattr(cls, field_name, None)

    @staticmethod
    def _generate_and_register_schema(cls, name: str) -> None:
        """Build column specs and persist the SchemaIR envelope.

        Sets ``cls.__ferro_columns__`` (via the compile choke point). Rust
        registration is deferred to the bulk install seam at connect time (#246).

        Raises:
            RuntimeError: If spec build or registration fails
        """
        try:
            compile_model_schema_ir(name, cls)
        except TypeError:
            # Declaration errors (db_type validation, ADR-0003) are the
            # class-definition contract — surface unwrapped, never disguised
            # as a registration failure.
            raise
        except Exception as e:
            raise RuntimeError(f"Ferro failed to register model '{name}': {e}")
