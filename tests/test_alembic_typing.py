"""Static typing contract for the Alembic bridge's public surface (#446).

Type-checked by the project's ``ty`` gate (see ``just check`` and the CI
workflow), so a regression here is a red gate, not a user-side
``# ty: ignore``. The one contract pinned: ``ferro.migrations.render_item``
is a valid ``render_item=`` argument to ``context.configure(...)`` — the
line the migrations guide tells every project to add to ``env.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from alembic import context
from alembic.runtime.environment import RenderItemFn

from ferro.migrations import get_metadata, render_item

if TYPE_CHECKING:
    from alembic.autogenerate.api import AutogenContext

pytestmark = pytest.mark.sqlite_only


def _env_py_configure() -> None:
    """The documented ``env.py`` recipe, verbatim, so ``ty`` checks the
    exact call a user writes. Never executed: ``alembic.context`` is a
    proxy that only works while Alembic runs ``env.py``."""
    context.configure(target_metadata=get_metadata(), render_item=render_item)


def _project_render_item(
    type_: str, obj: Any, autogen_context: AutogenContext
) -> str | bool:
    """A project's own hook composing ferro's the documented way: call it
    first, fall through on ``False``. Its return type is the project's
    business; ferro's must narrow to Alembic's ``str | Literal[False]``."""
    rendered = render_item(type_, obj, autogen_context)
    if rendered is not False:
        return rendered
    return False


def test_render_item_is_an_alembic_render_item_fn() -> None:
    """The static contract, checked by ``ty`` on the assignment below and on
    the two helpers above; the runtime assertions pin the annotation text so
    a plain ``pytest`` run also fails if it regresses to ``str | bool`` or
    to an untyped context parameter (#446)."""
    hook: RenderItemFn = render_item
    annotations = hook.__annotations__
    assert annotations["return"] == "str | Literal[False]"
    assert annotations["autogen_context"] == "AutogenContext"
    assert annotations["type_"] is str
    assert annotations["obj"] is Any


def test_ferro_migrations_imports_without_alembic_installed() -> None:
    """``AutogenContext`` is imported under ``TYPE_CHECKING`` only: Alembic
    stays an optional dependency of ``ferro.migrations`` (#446). Run in a
    subprocess with Alembic made unimportable, so this process's imports
    cannot mask a regression."""
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'alembic' or name.startswith('alembic.'):\n"
        "            raise ImportError(name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import ferro.migrations\n"
        "print(ferro.migrations.render_item.__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "render_item"
