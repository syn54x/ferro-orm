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
import logging
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
def isolated_imports(monkeypatch: pytest.MonkeyPatch):
    """Restore ``sys.path`` and drop modules a test imported from ``tmp_path``."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        del sys.modules[name]


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


def run(*argv: str) -> int:
    from ferro.cli import main

    return main(list(argv))


def listing(directory: Path) -> list[str]:
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
    )


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
    assert (migration / "01_schema.down.postgres.sql").read_text() == (
        '-- ferro: destructive\n\nDROP TABLE "author";\n\nDROP TYPE "status";\n'
    )
    assert (migration / "01_schema.down.sqlite.sql").read_text() == (
        '-- ferro: destructive\n\nDROP TABLE "author";\n'
    )
    out = capsys.readouterr().out
    assert "migrations/0001_create_author/" in out
    assert "new models: Author" in out


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
    # The down recreates the dropped tables exactly as 0001's up created them.
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
    # its snapshot is a full copy of the parent's, linked to it.
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

    assert capsys.readouterr().out == "no schema change: nothing written\n"
    assert listing(project / "migrations") == before


# -- refusals -----------------------------------------------------------------------


def test_a_column_added_to_an_existing_table_is_refused_naming_its_ticket(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    write_models(project, pkg, AUTHOR + "    bio: str | None = None\n")
    before = listing(project / "migrations")
    capsys.readouterr()

    assert run("migrate", "new", "add_bio") == 1

    assert (
        "not generated yet: AddColumn on author (ticket #524)"
        in capsys.readouterr().err
    )
    assert listing(project / "migrations") == before


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


class _CreatePassStatements(logging.Handler):
    """Collect every statement auto-migrate logs before executing it."""

    prefix = "Ferro Engine: auto-migrate executing on '"

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.statements: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(self.prefix):
            self.statements.append(message.split("': ", 1)[1])


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

    logger = logging.getLogger("ferro")
    handler = _CreatePassStatements()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        await ferro.connect(db_url, auto_migrate=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    generated = statements(migration / f"01_schema.up.{db_backend}.sql")
    # Byte-identical statements. The create pass creates each enum type just
    # before the first table declaring it; the migration creates every type
    # the migration introduces first. Every other statement keeps its order.
    assert sorted(generated) == sorted(handler.statements)

    def tables(sqls: list[str]) -> list[str]:
        return [sql for sql in sqls if "CREATE TYPE" not in sql]

    assert tables(generated) == tables(handler.statements)
    if db_backend == "postgres":
        assert any("CREATE POLICY" in sql for sql in generated)

    # And the snapshot describes exactly what the create pass built.
    live, facts = await _live_schema_ir()
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
