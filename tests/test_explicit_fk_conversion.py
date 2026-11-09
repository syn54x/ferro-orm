"""Converting a model to explicit foreign-key columns plans nothing (#876, I-1).

`tests/fixtures/pre_explicit_fk_snapshots.json` holds, for every casebook
side once written with `Annotated[Team, ForeignKey(...)]`, the payload of the
`ir.json` that `ferro migrate new` wrote for it on `main` before any
conversion. The old form can no longer run, so that recording is what it
produced; the converted form must compile to it, so `ferro migrate new`
plans no operation:

```python
team: Annotated[Team | None, ForeignKey(related_name="members", on_delete="SET NULL")] = None
# converts to
team_id: int | None = Field(default=None, foreign_key=Team, on_delete="SET NULL")
team: Relation[Team | None] = Forward("team_id")
```
"""

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from ferro import clear_registry, reset_engine
from ferro.registry import REGISTRY
from tests._casebook import CASES
from tests.test_migrate_down import migration_dir
from tests.test_migrate_new import write_config, write_models
from tests.test_migrate_up import new

FIXTURE = Path(__file__).parent / "fixtures" / "pre_explicit_fk_snapshots.json"

RECORDED_SIDES = (
    "B2-drop-a-model/before",
    "B3-rename-a-model/before",
    "B3-rename-a-model/after",
    "C1-add-an-optional-foreign-key/after",
    "C1-add-a-required-foreign-key/after",
    "C2-drop-a-foreign-key-column/before",
    "C3-retarget-a-foreign-key/before",
    "C3-retarget-a-foreign-key/after",
    "C4-drop-a-foreign-key-keep-its-column/before",
    "F1-several-models-at-once/before",
    "F1-several-models-at-once/after",
)

CASEBOOK = {case.id: case for case in CASES}

# The sides that added a key in the old form, as the casebook now declares
# them with an explicit key column.
CONVERTED = (
    "C1-add-an-optional-foreign-key/after",
    "C1-add-a-required-foreign-key/after",
)


def side_body(side: str) -> str:
    """The `models.py` body of one casebook side, `"<case id>/<side>"`."""
    case_id, which = side.split("/")
    return getattr(CASEBOOK[case_id], which)


@contextlib.contextmanager
def _project(project: Path, pkg: str):
    with pytest.MonkeyPatch.context() as mp, contextlib.redirect_stdout(io.StringIO()):
        mp.delenv("FERRO_CONFIG", raising=False)
        mp.chdir(project)
        mp.setattr(sys, "path", [str(project), *sys.path])
        mp.setattr(sys, "dont_write_bytecode", True)
        try:
            yield
        finally:
            for name in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
                del sys.modules[name]
            reset_engine()
            clear_registry()
            REGISTRY.reset_for_test()


def recorded_envelope(body: str, root: Path) -> dict:
    """The `ir.json` `ferro migrate new` writes for `body`, in a new project."""
    pkg = f"ferro_p876_{len(list(root.iterdir()))}"
    project = root / pkg
    project.mkdir()
    with _project(project, pkg):
        write_config(project, pkg)
        write_models(project, pkg, body)
        new("record")
    return json.loads((migration_dir(project, 1) / "ir.json").read_text())


@pytest.fixture(scope="module")
def recorded() -> dict:
    return json.loads(FIXTURE.read_text())


def test_the_fixture_records_every_old_form_side(recorded):
    assert sorted(recorded) == sorted(RECORDED_SIDES)
    assert FIXTURE.read_text() == json.dumps(recorded, sort_keys=True, indent=2) + "\n"


# ADR-0062: a declared join table keeps its name through a model rename. The
# converted after side of B3 declares `through="writer_tags"`, where the old
# form derived `author_tags` and hinted it was renamed from `writer_tags`.
_JOIN_TABLE_KEPT = {"B3-rename-a-model/after": ("author_tags", "writer_tags")}


def expected_payload(side: str, recorded: dict) -> dict:
    """The recorded payload of `side`, its join table named as declared."""
    if side not in _JOIN_TABLE_KEPT:
        return recorded[side]
    derived, declared = _JOIN_TABLE_KEPT[side]
    models = []
    for model in recorded[side]["models"]:
        if model["table_name"] == derived:
            model = json.loads(json.dumps(model).replace(derived, declared))
            assert model.pop("renamed_from") == declared
        models.append(model)
    return {**recorded[side], "models": models}


@pytest.mark.parametrize("side", RECORDED_SIDES)
def test_each_converted_side_compiles_to_the_old_forms_snapshot(
    side, recorded, tmp_path
):
    assert recorded_envelope(side_body(side), tmp_path)["payload"] == expected_payload(
        side, recorded
    )


_WRITER = """
from ferro import Field

class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str


class Post(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    writer_id: int = Field(foreign_key=Author)
"""


def test_a_key_columns_renamed_from_renames_it(tmp_path):
    project = tmp_path / "renamed"
    project.mkdir()
    with _project(project, "ferro_p876_renamed"):
        write_config(project, "ferro_p876_renamed")
        write_models(project, "ferro_p876_renamed", _WRITER)
        new("create")
        write_models(
            project,
            "ferro_p876_renamed",
            _WRITER.replace(
                "writer_id: int = Field(foreign_key=Author)",
                'author_id: int = Field(foreign_key=Author, renamed_from="writer_id")',
            ),
        )
        new("rename")
    migration = migration_dir(project, 2)
    postgres = "".join(
        path.read_text() for path in sorted(migration.glob("*.up.postgres.sql"))
    )
    every_dialect = "".join(path.read_text() for path in migration.glob("*.up.*.sql"))

    assert 'RENAME COLUMN "writer_id" TO "author_id"' in postgres
    assert "DROP COLUMN" not in every_dialect


def _plan_from(
    project: Path, pkg: str, recorded_envelope_: dict, body: str, name: str
) -> Path | None:
    """Plan `body` through `ferro migrate new` against a recorded `ir.json`.

    The recording is migration 0001; the result is the migration `new` writes
    after it, or `None` when it finds no schema change.
    """
    write_config(project, pkg)
    write_models(project, pkg, body)
    new("recorded")
    (migration_dir(project, 1) / "ir.json").write_text(json.dumps(recorded_envelope_))
    new(name)
    written = sorted((project / "migrations").glob("0002_*"))
    return written[0] if written else None


@pytest.mark.parametrize("side", CONVERTED)
def test_a_converted_key_plans_nothing_against_the_old_forms_snapshot(
    side, recorded, tmp_path
):
    envelope = recorded_envelope(side_body(side), tmp_path)
    project = tmp_path / "plan"
    project.mkdir()
    with _project(project, "ferro_p876_plan"):
        migration = _plan_from(
            project,
            "ferro_p876_plan",
            {**envelope, "payload": recorded[side]},
            side_body(side),
            "convert",
        )

    assert migration is None


BIGINT_TARGET = (
    Path(__file__).parent / "fixtures" / "pre_explicit_fk_bigint_target.json"
)

_BIGINT = """
from ferro import Field

class Team(Model):
    id: int | None = Field(default=None, primary_key=True, db_type="bigint")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team, db_type="bigint")
"""


def test_adding_db_type_to_a_converted_key_plans_only_its_widening(tmp_path):
    """The old form stored a key `integer` beside a `bigint` target, as
    `pre_explicit_fk_bigint_target.json` recorded it from `main`:

    ```python
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True, db_type="bigint")
        players: Relation[list["Player"]] = BackRef()

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team: Annotated[Team, ForeignKey(related_name="players")]
    ```

    Converting it with `db_type="bigint"` widens the column and changes
    nothing else.
    """
    project = tmp_path / "widen"
    project.mkdir()
    with _project(project, "rec_pkg"):
        migration = _plan_from(
            project, "rec_pkg", json.loads(BIGINT_TARGET.read_text()), _BIGINT, "widen"
        )

    assert migration is not None

    def statements(dialect: str) -> list[str]:
        text = "".join(
            path.read_text() for path in sorted(migration.glob(f"*.up.{dialect}.sql"))
        )
        lines = [line for line in text.splitlines() if not line.startswith("--")]
        return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]

    assert statements("postgres") == [
        'ALTER TABLE "player" ALTER COLUMN "team_id" TYPE bigint USING "team_id"::bigint'
    ]
    assert statements("sqlite") == [
        'CREATE TABLE IF NOT EXISTS "_ferro_new_player" ( "id" integer NOT NULL '
        'PRIMARY KEY AUTOINCREMENT, "team_id" bigint NOT NULL, CONSTRAINT '
        '"fk_player_team_id_team" FOREIGN KEY ("team_id") REFERENCES "team" ("id") '
        "ON DELETE CASCADE )",
        'INSERT INTO "_ferro_new_player" ("id", "team_id") SELECT "id", "team_id" '
        'FROM "player"',
        'CREATE TEMP TABLE "_ferro_rebuild_guard" ("value", CONSTRAINT "ferro: '
        'player.team_id has a value that cannot become bigint" CHECK (0))',
        'INSERT INTO "_ferro_rebuild_guard" ("value") SELECT "team_id" FROM '
        "\"_ferro_new_player\" WHERE typeof(\"team_id\") NOT IN ('integer', 'null')",
        'DROP TABLE "_ferro_rebuild_guard"',
        "DELETE FROM sqlite_sequence WHERE name = '_ferro_new_player'",
        "INSERT INTO sqlite_sequence (name, seq) SELECT '_ferro_new_player', seq "
        "FROM sqlite_sequence WHERE name = 'player'",
        'DROP TABLE "player"',
        'ALTER TABLE "_ferro_new_player" RENAME TO "player"',
    ]
