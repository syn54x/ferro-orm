"""``ferro migrate new`` (#518): new and dropped models, offline.

Every test builds a real project under ``tmp_path`` (a ``ferro.toml`` and a
models package), drives the CLI in-process through ``ferro.cli.main`` and reads
back the files a developer would review. The parity test then connects with
``auto_migrate=True`` and pins the generated up file to the statements the
create pass executes (AGENTS.md § I-1).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

import ferro
from ferro._core import _live_schema_ir, _plan_from_ir

pytestmark = pytest.mark.usefixtures("isolated_imports", "clean_registry")


# -- the project -----------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("FERRO_CONFIG", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def pkg(tmp_path: Path) -> str:
    """A package name unique to this test, so module caches never collide."""
    return "ferro_mnew_" + tmp_path.name.replace("-", "_").lower()


HEADER = """\
from enum import StrEnum
from typing import Annotated

from ferro import BackRef, ForeignKey, Model, Relation, RowPolicy, RowSecurity
from ferro.base import FerroField
"""

AUTHOR = """
class Status(StrEnum):
    DRAFT = "draft"
    LIVE = "live"


class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: Annotated[str, FerroField(unique=True)]
    status: Status = Status.DRAFT
"""

POST = """
class Kind(StrEnum):
    NOTE = "note"
    ESSAY = "essay"


class Post(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: Annotated[str, FerroField(index=True)]
    kind: Kind = Kind.NOTE
    author: Annotated[Author, ForeignKey(related_name="posts", on_delete="CASCADE")]
"""

LIBRARY = AUTHOR + '    posts: Relation[list["Post"]] = BackRef()\n' + POST


def write_models(project: Path, pkg: str, body: str) -> None:
    """(Re)write ``<pkg>/models.py`` and forget every model a previous
    version registered, as a fresh interpreter would."""
    from ferro import clear_registry, reset_engine
    from ferro.registry import REGISTRY

    package = project / pkg
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(HEADER + textwrap.dedent(body))
    for name in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        del sys.modules[name]
    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


def write_config(project: Path, pkg: str, dialects: str = '["postgres", "sqlite"]'):
    (project / "ferro.toml").write_text(
        f'models = ["{pkg}.models"]\ndialects = {dialects}\n'
    )


DIRECTORY_LINE = "default · migrations"
"""The database line ``new`` and ``check`` open with: the configured
database, then its migrations directory relative to the working directory."""


def run(*argv: str) -> int:
    from ferro.cli import main

    return main(list(argv))


def listing(directory: Path) -> list[str]:
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
    )


def words(out: str) -> list[str]:
    """Each line of ``out`` with its runs of spaces made one: a file line
    read without the column padding (``01_schema.up.sqlite.sql schema step,
    up (sqlite) kept``)."""
    return [" ".join(line.split()) for line in out.splitlines()]


def statements(sql_file: Path) -> list[str]:
    """The statements of a step file: its body split on the ``;`` that ends
    each statement, headers dropped."""
    text = sql_file.read_bytes().decode()
    body = "".join(
        line
        for line in text.splitlines(keepends=True)
        if not line.startswith("-- ferro:")
    )
    return [s.strip() for s in body.split(";\n") if s.strip()]


def sha384(path: Path) -> str:
    return hashlib.sha384(path.read_bytes()).hexdigest()


# -- the happy paths --------------------------------------------------------------


def test_the_first_migration_renders_every_target_dialect_and_a_root_snapshot(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)

    assert run("migrate", "new", "create_author") == 0

    migrations = project / "migrations"
    assert listing(migrations) == [
        ".gitattributes",
        "0001_create_author/01_schema.down.postgres.sql",
        "0001_create_author/01_schema.down.sqlite.sql",
        "0001_create_author/01_schema.up.postgres.sql",
        "0001_create_author/01_schema.up.sqlite.sql",
        "0001_create_author/ir.json",
    ]
    assert (migrations / ".gitattributes").read_text() == "* -text\n"
    migration = migrations / "0001_create_author"
    snapshot = json.loads((migration / "ir.json").read_text())
    assert snapshot["parent_checksum"] is None
    assert snapshot["ir_kind"] == "schema"
    assert [m["table_name"] for m in snapshot["payload"]["models"]] == ["author"]

    pg_up = statements(migration / "01_schema.up.postgres.sql")
    assert pg_up[0].startswith("DO $$") and 'CREATE TYPE "status"' in pg_up[0]
    assert pg_up[1].startswith('CREATE TABLE IF NOT EXISTS "author"')
    # A down drops what its up created, type after table, and is never marked
    # destructive (ADR-0033: nearly every down of an add is a drop).
    assert (migration / "01_schema.down.postgres.sql").read_text() == (
        'DROP TABLE "author";\n\nDROP TYPE "status";\n'
    )
    assert (migration / "01_schema.down.sqlite.sql").read_text() == (
        'DROP TABLE "author";\n'
    )
    out = capsys.readouterr().out
    assert "ok Wrote migrations/0001_create_author/\n" in out
    assert "\n  1 new model\n    Author\n  1 new enum type\n    status\n" in out


def test_a_child_model_chains_to_the_parent_snapshot_and_creates_its_type_first(
    project, pkg
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    write_models(project, pkg, LIBRARY)

    assert run("migrate", "new", "create_post") == 0

    first = project / "migrations/0001_create_author"
    second = project / "migrations/0002_create_post"
    snapshot = json.loads((second / "ir.json").read_text())
    assert snapshot["parent_checksum"] == sha384(first / "ir.json")

    up = statements(second / "01_schema.up.postgres.sql")
    assert 'CREATE TYPE "kind"' in up[0]
    assert up[1].startswith('CREATE TABLE IF NOT EXISTS "post"')
    assert '"fk_post_author_id_author"' in up[1]
    assert any(s.startswith('CREATE INDEX IF NOT EXISTS "idx_post_title"') for s in up)
    assert statements(second / "01_schema.down.postgres.sql") == [
        'DROP TABLE "post"',
        'DROP TYPE "kind"',
    ]
    assert statements(second / "01_schema.down.sqlite.sql") == ['DROP TABLE "post"']


def test_a_postgres_only_project_gets_no_sqlite_file(project, pkg):
    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, AUTHOR)

    assert run("migrate", "new", "create_author") == 0

    files = listing(project / "migrations")
    assert not [f for f in files if "sqlite" in f]
    assert "0001_create_author/01_schema.up.postgres.sql" in files


def test_dropping_a_model_drops_children_first_and_the_down_recreates_them(
    project, pkg
):
    write_config(project, pkg)
    write_models(project, pkg, LIBRARY)
    assert run("migrate", "new", "library") == 0
    first = project / "migrations/0001_library"
    write_models(
        project,
        pkg,
        """
        class Tag(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            label: str
        """,
    )

    assert run("migrate", "new", "drop_library") == 0

    second = project / "migrations/0002_drop_library"
    up = (second / "01_schema.up.postgres.sql").read_text()
    assert up.startswith("-- ferro: destructive\n")
    drops = [s for s in statements(second / "01_schema.up.postgres.sql") if "DROP" in s]
    assert drops == [
        'DROP TABLE "post"',
        'DROP TABLE "author"',
        'DROP TYPE "kind"',
        'DROP TYPE "status"',
    ]
    # The down recreates the dropped tables exactly as 0001's up created them,
    # empty: their rows are gone, so it is marked data-dependent.
    assert (
        (second / "01_schema.down.postgres.sql")
        .read_text()
        .startswith("-- ferro: data-dependent\n")
    )
    down = statements(second / "01_schema.down.postgres.sql")
    assert down[:-1] == statements(first / "01_schema.up.postgres.sql")
    assert down[-1] == 'DROP TABLE "tag"'
    assert statements(second / "01_schema.down.sqlite.sql")[:-1] == statements(
        first / "01_schema.up.sqlite.sql"
    )


def test_a_hand_written_sql_step_is_a_portable_placeholder(project, pkg):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author", "--sql-step", "fix_rows") == 0

    migration = project / "migrations/0001_create_author"
    assert (migration / "02_fix_rows.up.sql").read_text() == "-- write this step\n"
    assert (migration / "02_fix_rows.down.sql").read_text() == "-- write this step\n"
    assert (migration / "01_schema.up.sqlite.sql").exists()

    # With no schema change, the hand-written step is the whole migration and
    # its snapshot is the declared modelset, linked to the parent.
    assert run("migrate", "new", "touch_up", "--sql-step", "fix_more") == 0
    second = project / "migrations/0002_touch_up"
    assert listing(second) == [
        "01_fix_more.down.sql",
        "01_fix_more.up.sql",
        "ir.json",
    ]
    parent = json.loads((migration / "ir.json").read_text())
    child = json.loads((second / "ir.json").read_text())
    assert child["parent_checksum"] == sha384(migration / "ir.json")
    assert child["payload"] == parent["payload"]
    assert run("migrate", "check") == 0


def test_a_hand_step_after_an_edit_that_renders_no_ddl_stores_the_models(project, pkg):
    # The glossary's schema snapshot is the declared modelset as it was when
    # the migration was generated: a hand step after a Python default edit
    # records the edit, never the parent's copy.
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    write_models(project, pkg, AUTHOR.replace("Status.DRAFT", "Status.LIVE"))

    assert run("migrate", "new", "touch_up", "--sql-step", "fix_more") == 0

    first = project / "migrations/0001_create_author/ir.json"
    second = project / "migrations/0002_touch_up"
    assert listing(second) == [
        "01_fix_more.down.sql",
        "01_fix_more.up.sql",
        "ir.json",
    ]
    child = json.loads((second / "ir.json").read_text())
    assert child["parent_checksum"] == sha384(first)
    assert child["payload"] != json.loads(first.read_text())["payload"]
    status = next(
        col
        for col in child["payload"]["models"][0]["columns"]
        if col["name"] == "status"
    )
    assert status["default"] == "live"
    assert run("migrate", "check") == 0


def test_row_security_on_a_new_table_lands_in_the_postgres_rendering_only(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(
        project,
        pkg,
        """
        class Ledger(Model):
            __ferro_rls__ = RowSecurity(RowPolicy(column="owner_id", setting="app.owner_id"), force=True)

            id: Annotated[int | None, FerroField(primary_key=True)] = None
            owner_id: int
        """,
    )

    assert run("migrate", "new", "ledger") == 0

    migration = project / "migrations/0001_ledger"
    pg = statements(migration / "01_schema.up.postgres.sql")
    assert 'ALTER TABLE "ledger" ENABLE ROW LEVEL SECURITY' in pg
    assert 'ALTER TABLE "ledger" FORCE ROW LEVEL SECURITY' in pg
    assert any(s.startswith('CREATE POLICY "rls_ledger_owner_id"') for s in pg)
    sqlite = (migration / "01_schema.up.sqlite.sql").read_text()
    assert sqlite.startswith('CREATE TABLE IF NOT EXISTS "ledger"'), (
        "SQLite still has work"
    )
    assert "POLICY" not in sqlite and "not-applicable" not in sqlite
    err = capsys.readouterr().err
    assert err.startswith("warning: ") and "ledger" in err, err


# -- no schema change --------------------------------------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            AUTHOR.replace("Status.DRAFT", "Status.LIVE"), id="python-default"
        ),
        pytest.param(
            LIBRARY.replace("posts", "writings"),
            id="backref",
        ),
        pytest.param(
            AUTHOR
            + "\n    def shout(self) -> str:\n        return self.name.upper()\n",
            id="method",
        ),
    ],
)
def test_an_edit_that_renders_no_ddl_writes_nothing(project, pkg, capsys, edit):
    write_config(project, pkg)
    base = LIBRARY if "writings" in edit else AUTHOR
    write_models(project, pkg, base)
    assert run("migrate", "new", "first") == 0
    before = listing(project / "migrations")
    capsys.readouterr()
    write_models(project, pkg, edit)

    assert run("migrate", "new", "nothing") == 0

    assert capsys.readouterr().out == (
        f"{DIRECTORY_LINE}\nno schema change: nothing written\n"
    )
    assert listing(project / "migrations") == before


# -- refusals -----------------------------------------------------------------------


def test_a_change_new_cannot_generate_yet_is_refused_naming_its_ticket(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    write_models(project, pkg, AUTHOR.replace("    status: Status", "    status: str"))
    before = listing(project / "migrations")
    capsys.readouterr()

    assert run("migrate", "new", "status_text") == 1

    assert (
        '"author"."status" moves to or from a native enum type, which no statement '
        "converts in place: add a column of the new type"
    ) in capsys.readouterr().err
    assert listing(project / "migrations") == before


# -- the change summary (ADR-0055) ------------------------------------------------

LABEL_RENAMED = AUTHOR.replace(
    '    LIVE = "live"\n',
    '    OPEN = "open"\n    __ferro_renamed_labels__ = {"open": "live"}\n',
)
LABEL_REMOVED = AUTHOR.replace('    LIVE = "live"\n', "")
NAME_RETYPED = AUTHOR.replace(
    "    name: Annotated[str, FerroField(unique=True)]\n", "    full_name: str\n"
)
"""``name`` dropped and a required ``full_name`` added: a rename hint (the
generator's note) and a backfill to write (``new``'s own note)."""

FOUR_SCHEMA_FILES = (
    "  01_schema.up.postgres.sql\n  01_schema.down.postgres.sql\n"
    "  01_schema.up.sqlite.sql\n  01_schema.down.sqlite.sql\n  ir.json\n"
)


@pytest.mark.parametrize(
    ("before", "after", "flags", "files", "said", "changes"),
    [
        pytest.param(
            None,
            AUTHOR,
            (),
            FOUR_SCHEMA_FILES,
            "new models: Author; new enum types: status\n",
            (("new", "models", ("Author",)), ("new", "enum types", ("status",))),
            id="first",
        ),
        pytest.param(
            AUTHOR,
            AUTHOR + "    bio: str | None = None\n",
            (),
            FOUR_SCHEMA_FILES,
            "changed models: Author\n",
            (("changed", "models", ("Author",)),),
            id="change",
        ),
        pytest.param(
            LIBRARY,
            AUTHOR,
            (),
            FOUR_SCHEMA_FILES,
            "dropped models: Post; dropped enum types: kind\n",
            (("dropped", "models", ("Post",)), ("dropped", "enum types", ("kind",))),
            id="drop",
        ),
        pytest.param(
            AUTHOR,
            LABEL_RENAMED,
            (),
            FOUR_SCHEMA_FILES,
            "renamed enum labels: status.live → open\n",
            (("renamed", "enum labels", ("status.live → open",)),),
            id="label-rename",
        ),
        pytest.param(
            AUTHOR,
            LABEL_REMOVED,
            (),
            "  01_backfill_author.py\n"
            "  02_contract.up.postgres.sql\n  02_contract.down.postgres.sql\n"
            "  02_contract.up.sqlite.sql\n  02_contract.down.sqlite.sql\n  ir.json\n",
            "removed enum labels: status.live\n"
            "01_backfill_author.py needs writing where it says todo(...); if no "
            "author needs a value, delete 0002_b/ and run: ferro migrate new b "
            "--no-backfill author.status\n",
            (("removed", "enum labels", ("status.live",)),),
            id="label-removal",
        ),
        pytest.param(
            AUTHOR,
            NAME_RETYPED,
            ("--data-step", "Author"),
            "  01_expand.up.postgres.sql\n  01_expand.down.postgres.sql\n"
            "  01_expand.up.sqlite.sql\n  01_expand.down.sqlite.sql\n"
            "  02_backfill_author.py\n"
            "  03_add_constraint.up.postgres.sql\n"
            "  03_add_constraint.down.postgres.sql\n"
            "  03_add_constraint.up.sqlite.sql\n  03_add_constraint.down.sqlite.sql\n"
            "  04_backfill_author.py\n"
            "  05_contract.up.postgres.sql\n  05_contract.down.postgres.sql\n"
            "  05_contract.up.sqlite.sql\n  05_contract.down.sqlite.sql\n  ir.json\n",
            "changed models: Author\n"
            'author: if "name" became "full_name", declare renamed_from="name"\n'
            "02_backfill_author.py needs writing where it says todo(...); if no "
            "author needs a value, delete 0002_b/ and run: ferro migrate new b "
            "--no-backfill author.full_name\n",
            (("changed", "models", ("Author",)),),
            id="data-step-two-notes",
        ),
    ],
)
def test_new_prints_each_change_list_then_each_note(
    project, pkg, capsys, before, after, flags, files, said, changes
):
    """``said`` is the summary's plain rendering and the notes, one a line,
    as the library holds them; ``new`` prints the summary's lists under
    their count headings, one name a line in the plain form, then each
    note behind a bullet. Golden text for both forms is in
    ``tests/test_cli_output_new_check.py``."""
    from ferro import FerroSettings
    from ferro.migrations.generate import prepare
    from ferro.migrations.layout import Change, ChangeSummary

    write_config(project, pkg)
    number = "0001"
    if before is not None:
        write_models(project, pkg, before)
        assert run("migrate", "new", "a") == 0
        number = "0002"
    write_models(project, pkg, after)
    data_step = flags[1] if flags else None
    migration = prepare(FerroSettings().database(None), "b", data_step=data_step)
    capsys.readouterr()

    assert run("migrate", "new", "b", *flags) == 0

    out = capsys.readouterr().out.splitlines()
    names = [line.strip() for line in files.splitlines()]
    assert out[:2] == [DIRECTORY_LINE, f"ok Wrote migrations/{number}_b/"]
    assert [line.split()[0] for line in out[2 : 2 + len(names)]] == names
    lists = []
    for verb, kind, listed in changes:
        noun = kind.removesuffix("s") + ("" if len(listed) == 1 else "s")
        lists += [f"  {len(listed)} {verb} {noun}", *(f"    {n}" for n in listed)]
    notes = [f"  - {note}" for note in said.splitlines()[1:]]
    assert out[2 + len(names) :] == ["", *lists, *notes]
    assert isinstance(migration.summary, ChangeSummary)
    assert migration.summary.changes == tuple(
        Change(verb, kind, names) for verb, kind, names in changes
    )
    assert migration.summary.text == said.splitlines()[0]
    assert migration.notes == tuple(said.splitlines()[1:])


def test_a_summary_without_changes_is_false(project, pkg):
    from ferro.migrations.layout import Change, ChangeSummary

    assert not ChangeSummary((), "")
    assert ChangeSummary((Change("new", "models", ("Author",)),), "new models: Author")


def test_the_generator_json_carries_the_summary_as_data_and_no_notes(project, pkg):
    from ferro import FerroSettings
    from ferro._core import _generate_migration
    from ferro.migrations.generate import declared_modelset

    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    declared = declared_modelset(FerroSettings().database(None))

    raw = json.loads(
        _generate_migration(None, json.dumps(declared), ["postgres", "sqlite"])
    )

    assert raw["summary"] == {
        "changes": [
            {"verb": "new", "kind": "models", "names": ["Author"]},
            {"verb": "new", "kind": "enum types", "names": ["status"]},
        ],
        "text": "new models: Author; new enum types: status",
    }
    assert raw["notes"] == []


def test_a_backfill_to_write_names_the_command_that_skips_it(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    write_models(project, pkg, AUTHOR + "    bio: str\n")
    capsys.readouterr()

    assert run("migrate", "new", "add_bio") == 0

    out = capsys.readouterr().out
    assert (
        "02_backfill_author.py needs writing where it says todo(...); if no author "
        "needs a value, delete 0002_add_bio/ and run: ferro migrate new add_bio "
        "--no-backfill author.bio"
    ) in out, out


def test_a_duplicate_number_is_refused_naming_both_directories(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "a") == 0
    migrations = project / "migrations"
    # Two branches each generated their own 0001.
    shutil.copytree(migrations / "0001_a", migrations / "0001_b")
    capsys.readouterr()

    assert run("migrate", "new", "c") == 1

    err = capsys.readouterr().err
    assert "0001_a" in err and "0001_b" in err, err
    lines = err.splitlines()
    assert "  ferro migrate new a --replace 0001_a" in lines, err
    assert "  ferro migrate new b --replace 0001_b" in lines, err


def test_replace_composes_with_data_step_and_data_only(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "a") == 0
    write_models(project, pkg, AUTHOR + "    bio: str | None = None\n")
    assert run("migrate", "new", "b") == 0
    migrations = project / "migrations"
    capsys.readouterr()

    # --data-step adds a person's data step beside the regenerated DDL.
    assert (
        run("migrate", "new", "b", "--replace", "0002_b", "--data-step", "Author") == 0
    )
    assert sorted(p.name for p in (migrations / "0002_b").iterdir()) == [
        "01_schema.down.postgres.sql",
        "01_schema.down.sqlite.sql",
        "01_schema.up.postgres.sql",
        "01_schema.up.sqlite.sql",
        "02_backfill_author.py",
        "ir.json",
    ]
    assert "02_backfill_author.py data step new" in words(capsys.readouterr().out)

    # --data-only regenerates it as that data step alone; the one it had is
    # carried across, winning over the rescaffolded one of its name.
    hand = migrations / "0002_b/02_backfill_author.py"
    hand.write_text("# filled in\n")
    assert (
        run(
            "migrate",
            "new",
            "b",
            "--replace",
            "0002_b",
            "--data-step",
            "Author",
            "--data-only",
        )
        == 0
    )
    assert sorted(p.name for p in (migrations / "0002_b").iterdir()) == [
        "01_backfill_author.py",
        "ir.json",
    ]
    assert (migrations / "0002_b/01_backfill_author.py").read_text() == "# filled in\n"
    out = capsys.readouterr().out
    assert (
        "01_backfill_author.py data step carried, review it (was "
        "0002_b/02_backfill_author.py; it wins over the regenerated step of the "
        "same name)"
    ) in words(out), out


def test_a_gap_in_the_numbers_is_refused_naming_it(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "a") == 0
    write_models(project, pkg, LIBRARY)
    assert run("migrate", "new", "b") == 0
    migrations = project / "migrations"
    (migrations / "0002_b").rename(migrations / "0003_b")
    capsys.readouterr()

    assert run("migrate", "new", "c") == 1

    err = capsys.readouterr().err
    assert "0002" in err and "0003_b" in err, err


def test_a_config_without_dialects_is_refused_with_the_line_to_add(
    project, pkg, capsys
):
    (project / "ferro.toml").write_text(f'models = ["{pkg}.models"]\n')
    write_models(project, pkg, AUTHOR)

    assert run("migrate", "new", "a") == 1

    assert 'dialects = ["postgres"]' in capsys.readouterr().err
    assert not (project / "migrations").exists()


def test_new_reads_no_database_and_refuses_url(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)

    assert run("migrate", "new", "a", "--url", "sqlite::memory:") == 1

    assert "--url" in capsys.readouterr().err


def test_a_name_unusable_in_a_file_name_is_refused(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)

    assert run("migrate", "new", "Create Author") == 1

    assert "lowercase" in capsys.readouterr().err


# -- parity with auto-migrate (AGENTS.md § I-1) -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_the_up_file_is_what_auto_migrate_executes(
    project, pkg, db_url, db_backend
):
    write_config(project, pkg, f'["{db_backend}"]')
    body = LIBRARY
    if db_backend == "postgres":
        body = body.replace(
            "class Post(Model):",
            "class Post(Model):\n"
            '    __ferro_rls__ = RowSecurity(RowPolicy(column="author_id", setting="app.author_id"), force=True)\n',
        )
    write_models(project, pkg, body)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert run("migrate", "new", "library") == 0
    migration = project / "migrations/0001_library"

    await ferro.connect(db_url)
    report = await ferro.create_tables()

    generated = statements(migration / f"01_schema.up.{db_backend}.sql")
    # The same statements, byte for byte, in the same sequence: every enum
    # type first (by name), then the tables, parents first.
    assert generated == [s.sql for s in report.statements if s.role == "schema"]
    if db_backend == "postgres":
        assert [sql.split('"')[1] for sql in generated if "CREATE TYPE" in sql] == [
            "kind",
            "status",
        ]
    if db_backend == "postgres":
        assert any("CREATE POLICY" in sql for sql in generated)

    # And the snapshot describes exactly what the create pass built.
    live, facts = await _live_schema_ir(None, (migration / "ir.json").read_text())
    plan = json.loads(
        _plan_from_ir(
            live,
            (migration / "ir.json").read_text(),
            db_backend,
            '{"destructive": true}',
            facts_json=facts,
        )
    )
    assert plan["operations"] == []


def test_each_data_step_carries_the_role_the_generator_gave_it(project, pkg):
    """A generated backfill, a ``--data-step`` beside it of the same name and
    a ``--no-backfill`` guard are told apart by the generator's record, never
    by their names or their order."""
    from ferro import FerroSettings
    from ferro.migrations.generate import prepare

    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "a") == 0
    write_models(project, pkg, NAME_RETYPED)
    database = FerroSettings().database(None)

    hand = prepare(database, "b", data_step="Author")
    guarded = prepare(database, "b", no_backfill=("author.full_name",))

    assert [(s.name, s.role) for s in hand.steps if s.kind == "data"] == [
        ("backfill_author", "backfill"),
        ("backfill_author", "data_step"),
    ]
    assert [s.role for s in guarded.steps if s.kind == "data"] == ["guard"]
    assert {s.role for s in hand.steps if s.kind != "data"} == {None}
