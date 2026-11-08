"""Converting a model to explicit foreign-key columns plans nothing (#876, I-1).

`tests/fixtures/pre_explicit_fk_snapshots.json` holds, for every casebook
side written with `Annotated[Team, ForeignKey(...)]`, the payload of the
`ir.json` that `ferro migrate new` wrote for it on `main` before any
conversion. The old form must keep compiling to it, and the converted form
must compile to it too, so `ferro migrate new` plans no operation:

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

from ferro import _core, clear_registry, reset_engine
from ferro.registry import REGISTRY
from tests import test_generate_backfill as backfill
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

_IMPORTS = "from ferro import Field, Forward, Reverse\n"

_OPTIONAL_TEAM = (
    '    team: Annotated[Team | None, ForeignKey(related_name="members", '
    'on_delete="SET NULL")] = None\n'
)

CONVERTED = {
    "C1-add-an-optional-foreign-key/after": _IMPORTS
    + CASEBOOK["C1-add-an-optional-foreign-key"]
    .after.replace(
        '    members: Relation[list["Author"]] = BackRef()\n',
        '    members: Relation[list["Author"]] = Reverse("team_id")\n',
    )
    .replace(
        _OPTIONAL_TEAM,
        "    team_id: int | None = Field(\n"
        '        default=None, foreign_key=Team, on_delete="SET NULL"\n'
        "    )\n"
        '    team: Relation[Team | None] = Forward("team_id")\n',
    ),
    "C1-add-a-required-foreign-key/after": _IMPORTS
    + CASEBOOK["C1-add-a-required-foreign-key"]
    .after.replace(
        backfill.MEMBERS, '    members: Relation[list["Author"]] = Reverse("team_id")\n'
    )
    .replace(
        backfill.REQUIRED_TEAM,
        '    team_id: int = Field(foreign_key=Team, on_delete="CASCADE")\n'
        '    team: Relation[Team] = Forward("team_id")\n',
    ),
}


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


@pytest.mark.parametrize("side", RECORDED_SIDES)
def test_the_old_form_still_compiles_to_its_recorded_snapshot(side, recorded, tmp_path):
    assert recorded_envelope(side_body(side), tmp_path)["payload"] == recorded[side]


@pytest.mark.parametrize("side", sorted(CONVERTED))
def test_a_converted_side_compiles_to_the_recorded_snapshot_and_plans_nothing(
    side, recorded, tmp_path
):
    converted = recorded_envelope(CONVERTED[side], tmp_path)

    assert converted["payload"] == recorded[side]
    for dialect in ("postgres", "sqlite"):
        plan = json.loads(
            _core._plan_from_ir(
                json.dumps({**converted, "payload": recorded[side]}),
                json.dumps(converted),
                dialect,
                json.dumps({"destructive": True}),
                True,
            )
        )
        assert plan["operations"] == []


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
