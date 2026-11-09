"""The names a string annotation is evaluated against.

A module under ``from __future__ import annotations`` hands the metaclass
every annotation as text. The relation scan reads its markers from the
evaluated annotation, so the text is evaluated against the class body, the
defining module and builtins, and a name not defined yet becomes a
``ForwardRef``. The same evaluation runs on every Python version, so Python
3.13 and 3.14 return the same object.
"""

from __future__ import annotations

import builtins
from typing import Any, ForwardRef


class DeferredNames(dict):
    """Eval locals: the class body first, then the module, then builtins.

    A name found nowhere is answered with a ``ForwardRef`` so the rest of the
    annotation can still be built.
    """

    def __init__(self, localns: dict[str, Any], globalns: dict[str, Any]) -> None:
        super().__init__(localns)
        self._globalns = globalns

    def __missing__(self, key: str) -> Any:
        if key in self._globalns:
            return self._globalns[key]
        if hasattr(builtins, key):
            return getattr(builtins, key)
        return ForwardRef(key)
