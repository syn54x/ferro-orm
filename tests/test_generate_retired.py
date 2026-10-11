# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for a retired column, then its drop (ADR-0053).

Release B retires ``Author.mood``, a required enum column with an index::

    mood: Annotated[Mood | None, FerroField(index=True, retired=True)] = None

```text
$ ferro migrate new retire_mood
  0002_retire_mood/01_schema.up.postgres.sql
    ALTER TABLE "author" ALTER COLUMN "mood" DROP NOT NULL;
```

The relaxation is the one change: the index and the ``mood`` type stay, so
release A, still writing ``mood``, keeps working beside B. Release C deletes
the declaration, which is the destructive ``DROP COLUMN`` and, the type's last
column gone, its ``DROP TYPE``.
"""

from typing import Annotated

import pytest

from ferro import Field, ForeignKey, Model
from ferro.base import FerroField

from tests.test_generate_columns import MOOD, no_bytecode  # noqa: F401 - fixtures
from tests.test_generate_sqlite_rebuild import (
    DATA_DEPENDENT,
    REBUILD,
    generate,
    rebuild_of,
    start,
    step_file,
)
from tests.test_generate_sqlite_rebuild import round_trip as rebuild_round_trip
from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
    tables_of,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    listing,
    pkg,
    project,
    run,
    statements,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix

REQUIRED_MOOD = MOOD + AUTHOR + "    mood: Annotated[Mood, FerroField(index=True)]\n"
"""Release A: ``mood`` is a required enum column with an index."""

RETIRED_MOOD = (
    MOOD
    + AUTHOR
    + "    mood: Annotated[Mood | None, FerroField(index=True, retired=True)] = None\n"
)
"""Release B: ``mood`` is retired; its column, type and index stay."""

MOOD_GONE = AUTHOR
"""Release C: the declaration (and its enum class) is deleted."""

OPTIONAL_BIO = AUTHOR + "    bio: str | None = None\n"
RETIRED_BIO = (
    AUTHOR + "    bio: Annotated[str | None, FerroField(retired=True)] = None\n"
)


def build_retired_column_models() -> None:
    """A retired scalar in each declaration style and a retired
    ``ForeignKey`` (ADR-0053). Pins the ``schema_retired_column_v2`` golden
    vector."""

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        bio: str | None = Field(index=True, retired=True)
        nickname: Annotated[str | None, FerroField(unique=True, retired=True)] = None
        team: Annotated[Team | None, ForeignKey(retired=True)] = None

    _ = (Team, Author)


def _ir_column(project, number: int, table: str, name: str) -> dict:
    model = next(
        m
        for m in snapshot(project, number)["payload"]["models"]
        if m["table_name"] == table
    )
    return next(c for c in model["columns"] if c["name"] == name)


# -- retiring a required column -------------------------------------------------------


@backend_matrix
def test_retiring_a_required_column_writes_only_its_drop_not_null(project, pkg, db):
    start(project, pkg, db, REQUIRED_MOOD)
    db.execute(
        "INSERT INTO author (name, status, mood) VALUES ('ada', 'draft', 'calm')"
    )

    number = generate(project, pkg, RETIRED_MOOD, "retire_mood")

    up = step_file(project, number, "up", db.backend)
    down = step_file(project, number, "down", db.backend)
    if db.backend == "postgres":
        assert up.read_text() == (
            'ALTER TABLE "author" ALTER COLUMN "mood" DROP NOT NULL;\n'
        )
        assert down.read_text() == (
            DATA_DEPENDENT + "\n"
            'ALTER TABLE "author" ALTER COLUMN "mood" SET NOT NULL;\n'
        )
    else:
        # SQLite relaxes NOT NULL by a table rebuild (ADR-0046): the new table
        # is the old one with "mood" nullable, its index recreated.
        copy = '("id", "mood", "name", "status") SELECT "id", "mood", "name", "status"'
        assert up.read_text().startswith(REBUILD + "\n")
        assert statements(up) == rebuild_of(project, number, "author", copy)
        assert 'CREATE INDEX IF NOT EXISTS "idx_author_mood"' in up.read_text()
    column = _ir_column(project, number, "author", "mood")
    assert column["retired"] is True and column["nullable"] is True
    assert "retired" not in _ir_column(project, number - 1, "author", "mood")
    rebuild_round_trip(project, db, number)
    # Release A's row keeps its value: nothing about the column but NOT NULL
    # changed.
    assert db.rows("SELECT name, mood FROM author") == [("ada", "calm")]


def test_retiring_an_optional_column_is_no_schema_change_and_check_is_clean(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, OPTIONAL_BIO)
    assert run("migrate", "new", "first") == 0
    before = listing(project / "migrations")
    write_models(project, pkg, RETIRED_BIO)
    capsys.readouterr()

    assert run("migrate", "new", "retire_bio") == 0

    assert capsys.readouterr().out == "no schema change: nothing written\n"
    assert listing(project / "migrations") == before
    assert run("migrate", "check") == 0


def test_a_retired_foreign_key_keeps_its_constraint_in_the_snapshot(project, pkg):
    team = (
        "class Team(Model):\n"
        "    id: Annotated[int | None, FerroField(primary_key=True)] = None\n\n\n"
    )
    members = '    members: Relation[list["Author"]] = BackRef()\n\n\n'
    write_config(project, pkg)
    write_models(
        project,
        pkg,
        team.rstrip("\n")
        + "\n"
        + members
        + AUTHOR
        + '    team: Annotated[Team | None, ForeignKey(related_name="members")] = None\n',
    )
    assert run("migrate", "new", "first") == 0
    first = listing(project / "migrations")
    write_models(
        project,
        pkg,
        team
        + AUTHOR
        + "    team: Annotated[Team | None, ForeignKey(retired=True)] = None\n",
    )

    assert run("migrate", "new", "retire_team") == 0

    # A nullable relation retired is no schema change.
    assert listing(project / "migrations") == first
    write_models(
        project,
        pkg,
        team
        + AUTHOR
        + "    team: Annotated[Team | None, ForeignKey(retired=True)] = None\n"
        + "    bio: str | None = None\n",
    )
    assert run("migrate", "new", "bio") == 0
    model = next(
        m
        for m in snapshot(project, 2)["payload"]["models"]
        if m["table_name"] == "author"
    )
    assert [fk["name"] for fk in model["foreign_keys"]] == ["fk_author_team_id_team"]
    assert _ir_column(project, 2, "author", "team_id")["retired"] is True


# -- deleting it a release later ------------------------------------------------------


@backend_matrix
def test_deleting_a_retired_enum_column_drops_it_and_its_orphaned_type(
    project, pkg, db
):
    start(project, pkg, db, RETIRED_MOOD)

    number = generate(project, pkg, MOOD_GONE, "drop_mood")

    up = step_file(project, number, "up", db.backend)
    assert up.read_text().startswith("-- ferro: destructive\n"), up.read_text()
    if db.backend == "postgres":
        assert statements(up) == [
            'DROP INDEX IF EXISTS "idx_author_mood"',
            'ALTER TABLE "author" DROP COLUMN "mood"',
            'DROP TYPE "mood"',
        ]
    else:
        assert statements(up) == [
            'DROP INDEX IF EXISTS "idx_author_mood"',
            'ALTER TABLE "author" DROP COLUMN "mood"',
        ]
    rebuild_round_trip(project, db, number)
