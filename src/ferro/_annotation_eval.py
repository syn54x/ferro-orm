"""Evaluate a string annotation that declares a ``ForeignKey``.

A module under ``from __future__ import annotations`` hands the metaclass
every annotation as text. The relation scan needs the ``ForeignKey`` marker
inside it, so the text is evaluated here, in the defining module's namespace.

A name that is not defined yet becomes a ``ForwardRef`` and is allowed only as
the relation's target (``resolve_relationships()`` binds it at ``connect()``).
The same evaluation runs on every Python version, so Python 3.13 and 3.14
return the same object.
"""

from __future__ import annotations

import builtins
import sys
from typing import Annotated, Any, ForwardRef, get_args, get_origin

from ._annotation_utils import _strip_optional_union
from .base import ForeignKey


class _Names(dict):
    """Eval locals: the class body first, then the module, then builtins.

    A name found nowhere is recorded and answered with a ``ForwardRef`` so the
    rest of the annotation can still be built.
    """

    def __init__(self, localns: dict[str, Any], globalns: dict[str, Any]) -> None:
        super().__init__(localns)
        self._globalns = globalns
        self.missing: list[str] = []

    def __missing__(self, key: str) -> Any:
        if key in self._globalns:
            return self._globalns[key]
        if hasattr(builtins, key):
            return getattr(builtins, key)
        if key not in self.missing:
            self.missing.append(key)
        return ForwardRef(key)


def _foreign_key_of(annotation: Any) -> tuple[Any, ForeignKey] | None:
    """The ``Annotated`` core and its ``ForeignKey``, looking through an outer Optional."""
    core = _strip_optional_union(annotation)
    if get_origin(core) is Annotated:
        for metadata in get_args(core)[1:]:
            if isinstance(metadata, ForeignKey):
                return core, metadata
    return None


def _undefined_name(
    model: str, field: str, text: str, name: str, module: str, target: str
):
    return TypeError(
        f"{model}.{field}: the annotation {text!r} names {name!r}, which is not "
        f"defined in {module} when the class is created; only the ForeignKey "
        f"target may be named before it is defined. Import or define {name!r} "
        f'above the model, or write the target as a string: Annotated["{target}", '
        "ForeignKey(...)]"
    )


def evaluate_foreign_key_annotation(
    text: str,
    *,
    model: str,
    field: str,
    module: str,
    localns: dict[str, Any],
    frame_ns: dict[str, Any] | None = None,
) -> Any:
    """Evaluate ``text`` if it declares a ``ForeignKey``; otherwise return it unchanged.

    Args:
        text: The string annotation.
        model: The model's name, for error text.
        field: The field's name, for error text.
        module: The ``__module__`` of the class being defined.
        localns: The class body namespace.
        frame_ns: Locals of the scope that runs the class statement, for a
            model defined inside a function.

    Returns:
        The evaluated annotation when it holds a ``ForeignKey`` (its target
        may be a ``ForwardRef``), else ``text``.

    Raises:
        TypeError: The annotation declares a ``ForeignKey`` and names
            something undefined other than the target.
    """
    module_ns = getattr(sys.modules.get(module), "__dict__", {})
    names = _Names({**(frame_ns or {}), **localns}, module_ns)
    try:
        value = eval(text, module_ns, names)
    except Exception as error:
        if "ForeignKey" not in text:
            return text
        if names.missing:
            raise _undefined_name(
                model, field, text, names.missing[0], module, "Target"
            ) from error
        raise TypeError(
            f"{model}.{field}: could not evaluate the annotation {text!r}: {error}. "
            "Fix the expression, or write the field with a real annotation."
        ) from error

    found = _foreign_key_of(value)
    if found is None:
        return text
    core, _ = found
    target = _strip_optional_union(get_args(core)[0])
    target_name = (
        target.__forward_arg__ if isinstance(target, ForwardRef) else str(target)
    )
    for name in names.missing:
        if name != target_name:
            raise _undefined_name(model, field, text, name, module, target_name)
    return value
