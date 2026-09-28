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
    hook: RenderItemFn = render_item
    assert hook is render_item
    assert _project_render_item is not None
    assert _env_py_configure is not None
