# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A primary-key change is refused with the restructure recipe (#536, B4).

```python
class PkrAuthor(Model):
    id: int | None = None                                        # was the key
    name: Annotated[str, FerroField(primary_key=True)]           # is the key now
```

```text
$ ferro migrate new rekey
changing the primary key of "pkrauthor" is not generated: write it as a new table
(ferro migrate new --data-step …), a backfill of parent and children, and a drop;
see the Migrations docs § Changing a primary key
```

No migration door changes a key in place: the key moved to another column,
made composite, or its column's type changed are each refused before
anything is written, on every dialect. Table names start with ``pkr`` so this
module never shares a name with another suite on the same Postgres server.
"""

from __future__ import annotations

import contextlib
import io
import sys

import pytest

from tests.test_migrate_new import (  # noqa: F401 - fixtures
    listing,
    pkg,
    project,
    run,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    db,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: a rewritten ``models.py`` of the same size inside
    the same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


KEYED = """
class PkrAuthor(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
"""

SHAPES = {
    # The key moved to another column.
    "moved": """
class PkrAuthor(Model):
    id: int | None = None
    name: Annotated[str, FerroField(primary_key=True)]
""",
    # A composite key cannot be declared on a model (one `primary_key=True`
    # column at most, refused at class definition); its IR shape is pinned in
    # `cargo test` (`a_primary_key_change_is_refused_with_the_recipe_in_each_of_its_shapes`).
    # The key column's type changed.
    "retyped": """
from uuid import UUID


class PkrAuthor(Model):
    id: Annotated[UUID, FerroField(primary_key=True)]
    name: str
""",
}

RECIPE = (
    'changing the primary key of "pkrauthor" is not generated: write it as a new '
    "table (ferro migrate new --data-step …), a backfill of parent and children, "
    "and a drop; see the Migrations docs § Changing a primary key\n"
)


@pytest.mark.backend_matrix
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_primary_key_change_is_refused_with_the_recipe_and_writes_nothing(
    project, pkg, db, shape
):
    write_config(project, pkg)
    write_models(project, pkg, KEYED)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0
    write_models(project, pkg, SHAPES[shape])
    written = listing(project / "migrations")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = run("migrate", "new", "rekey")

    assert code == 1
    assert err.getvalue() == RECIPE
    assert listing(project / "migrations") == written
